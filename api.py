"""
api.py — REST 接口封装（example.com）。✅ 路径/方法/body 均来自源码确认。

REST：任务、邮件、好友、拜访、偷菜、放草放虫、自家除草除虫。
WS  ：农场自身动作（收获/种植等），见 ws.py。
"""
import asyncio
import json

import config
from endpoints import (ENDPOINTS, FRIENDS_PAGE_PARAM, FRIENDS_PAGESIZE_PARAM,
                       FRIENDS_PAGE_SIZE, MARK_PEST, MARK_WEED)

_CF_MARKERS = ("cf-ray", "cloudflare", "just a moment", "attention required")


class AuthExpiredError(Exception):
    """401：鉴权失效。"""


class CFBlockedError(Exception):
    """被 Cloudflare 拦截。"""


def _looks_like_cf(text):
    low = (text or "").lower()
    return any(m in low for m in _CF_MARKERS)


def _looks_like_challenge(status, data):
    """REST 应用层质询判定（抓包确认：目标游戏 在 body 里返回 code=NEED_HUMAN_VERIFICATION）。"""
    return isinstance(data, dict) and data.get("code") == config.CAPTCHA_CHALLENGE_CODE


def _path(key, **fmt):
    p = ENDPOINTS[key]
    return p.format(**fmt) if fmt else p


async def _request(session, headers, method, path, json_body=None, params=None,
                   _challenge_retried=False):
    import human_verify
    url = config.BASE_API_URL.rstrip("/") + path
    last_err = None
    for attempt in range(config.RETRY_TIMES + 1):
        try:
            req_headers = {**headers, **human_verify.pass_header()}   # 带人机验证通行证(X-Human-Pass)
            if method == "GET":
                resp = await session.get(url, headers=req_headers, params=params,
                                         timeout=config.REQUEST_TIMEOUT)
            else:
                resp = await session.post(url, headers=req_headers, json=json_body,
                                          timeout=config.REQUEST_TIMEOUT)
            status = resp.status_code
            text = resp.text

            if status == 401:
                raise AuthExpiredError(f"{method} {path} 返回 401")
            if status == 429:
                print(f"[api] 429 限流，等 30 秒重试: {path}")
                await asyncio.sleep(30)
                continue
            if status == 403 and _looks_like_cf(text):
                raise CFBlockedError(f"{method} {path} 被 CF 拦截 (403)")

            try:
                data = resp.json()
            except (json.JSONDecodeError, ValueError):
                if _looks_like_cf(text):
                    raise CFBlockedError(f"{method} {path} 返回非 JSON 且疑似 CF 页面")
                # 有些 POST 成功返回空体，按成功处理
                if 200 <= status < 300:
                    return {"ok": True, "raw": text}
                raise RuntimeError(
                    f"{method} {path} 返回非 JSON ({status}): {text[:200]}")

            # REST 应用层人机验证：带外过 Turnstile + POST /api/game/human-verify 解锁后，原样重放一次。
            # 放在 4xx 判定之前——目标游戏 用 403 + code=NEED_HUMAN_VERIFICATION。
            if not _challenge_retried and _looks_like_challenge(status, data):
                import human_verify
                if await human_verify.ensure_passed():
                    print(f"[api] {method} {path} 人机验证已通过，重试一次")
                    return await _request(session, headers, method, path,
                                          json_body=json_body, params=params,
                                          _challenge_retried=True)
                print(f"[api] {method} {path} 人机验证未通过，放弃")

            # 4xx/5xx 即便带 JSON 体也是失败：不能打印 OK 或当数据返回，否则上层会把
            # 被拒的偷菜(403)/放草放虫(400)当成功计数，虚报今日任务进度。直接 return None
            # （safe_call 的成功口径是「非 None」），不进重试（确定性 4xx 重试也是 4xx）。
            if status >= 400:
                msg = ""
                if isinstance(data, dict):
                    msg = data.get("message") or data.get("error") or data.get("msg") or ""
                print(f"[api] {method} {path} 失败 ({status}): {msg or str(data)[:120]}")
                return None

            print(f"[api] {method} {path} OK ({status})")
            return data
        except (AuthExpiredError, CFBlockedError):
            raise
        except Exception as e:
            last_err = e
            if attempt < config.RETRY_TIMES:
                print(f"[api] {method} {path} 第 {attempt+1} 次失败: {e}，"
                      f"{config.RETRY_INTERVAL} 秒后重试")
                await asyncio.sleep(config.RETRY_INTERVAL)
            else:
                print(f"[api] {method} {path} 重试耗尽，放弃: {e}")
    raise last_err if last_err else RuntimeError(f"{method} {path} 未知失败")


async def safe_call(func, session, headers_holder, *args, **kwargs):
    """REST 兜底：401/CF 自动 refresh_auth 重试一次，其它异常返回 None。"""
    import auth
    try:
        return await func(session, headers_holder[0], *args, **kwargs)
    except AuthExpiredError as e:
        print(f"[api] 鉴权失效({e})，刷新后重试一次")
        try:
            headers_holder[0] = await auth.refresh_auth(force=True)
            return await func(session, headers_holder[0], *args, **kwargs)
        except Exception as e2:
            print(f"[api] 刷新后仍失败，跳过: {e2}")
            return None
    except CFBlockedError as e:
        print(f"[api] CF 拦截({e})，重新过盾后重试一次")
        try:
            headers_holder[0] = await auth.refresh_auth(force=True)
            return await func(session, headers_holder[0], *args, **kwargs)
        except Exception as e2:
            print(f"[api] 重新过 CF 后仍失败，跳过: {e2}")
            return None
    except Exception as e:
        print(f"[api] {getattr(func,'__name__',func)} 调用失败，跳过: {e}")
        return None


# =========================================================================
# 角色 / 任务
# =========================================================================
async def get_profile(session, headers):
    return await _request(session, headers, "GET", _path("profile"))


async def get_tasks(session, headers, category=None):
    params = {"category": category} if category else None
    return await _request(session, headers, "GET", _path("tasks"), params=params)


async def claim_task(session, headers, task_code):
    return await _request(session, headers, "POST",
                          _path("claim_task", task_code=task_code))


# =========================================================================
# 邮件
# =========================================================================
async def get_mailbox(session, headers):
    return await _request(session, headers, "GET", _path("mails"))


async def read_mail(session, headers, mail_id):
    return await _request(session, headers, "POST",
                          _path("read_mail", mail_id=mail_id))


async def claim_mail(session, headers, mail_id):
    return await _request(session, headers, "POST",
                          _path("claim_mail", mail_id=mail_id))


# =========================================================================
# 好友
# =========================================================================
async def get_friends(session, headers, page=1):
    return await _request(session, headers, "GET", _path("friends"),
                          params={FRIENDS_PAGE_PARAM: page,
                                  FRIENDS_PAGESIZE_PARAM: FRIENDS_PAGE_SIZE})


async def visit_friend(session, headers, target_key):
    return await _request(session, headers, "POST", _path("visit"),
                          json_body={"target_key": target_key})


async def steal_crops(session, headers, target_key):
    """偷菜：一次性偷该好友所有可偷作物（body 只需 target_key）。"""
    return await _request(session, headers, "POST", _path("steal"),
                          json_body={"target_key": target_key})


# 放草放虫（在好友农场打标记）
async def place_weed(session, headers, target_key, plot_index):
    return await _request(session, headers, "POST", _path("friend_mark"),
                          json_body={"target_key": target_key,
                                     "plot_index": plot_index,
                                     "mark_type": MARK_WEED})


async def place_pest(session, headers, target_key, plot_index):
    return await _request(session, headers, "POST", _path("friend_mark"),
                          json_body={"target_key": target_key,
                                     "plot_index": plot_index,
                                     "mark_type": MARK_PEST})


# 在好友农场帮忙除草除虫
async def clean_friend(session, headers, target_key, clean_type):
    return await _request(session, headers, "POST", _path("friend_clean"),
                          json_body={"target_key": target_key,
                                     "clean_type": clean_type})


# =========================================================================
# 自家农场除草除虫（清理别人放在你地里的草/虫 -> 今日除草/除虫任务）
# =========================================================================
async def clean_own_weed(session, headers):
    return await _request(session, headers, "POST", _path("clean_marks"),
                          json_body={"clean_type": MARK_WEED})


async def clean_own_pest(session, headers):
    return await _request(session, headers, "POST", _path("clean_marks"),
                          json_body={"clean_type": MARK_PEST})
