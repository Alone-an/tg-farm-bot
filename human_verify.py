"""
human_verify.py — 目标游戏 应用层人机验证（Cloudflare Turnstile）自动过。

抓包（前端 index-*.js）确认的真实流程：
  风控触发 -> 回包 code=NEED_HUMAN_VERIFICATION
  前端弹 Turnstile(sitekey=0x4AAAAAADmotcK0lqq38R89) -> 用户过 -> 拿 turnstile token
  -> POST /api/game/human-verify  Authorization: Bearer <JWT>  {"captcha_token": token}
  -> 通过后服务端给该账号一段「人机已通过」时效（前端缓存 farm_human_pass + expire）

本模块把「用户手动过 Turnstile」换成「2captcha 打 Turnstile」，其余照搬：
  ensure_passed() -> 打码 + POST human-verify + 缓存通过时效。
WS（收获）与 REST（偷菜）遇到 NEED_HUMAN_VERIFICATION 都调它解锁，再重试原动作。

「解锁是带外的、且有时效」是关键：所以做成全局缓存（_passed_until）+ 单飞锁
（_lock），同一时效内的所有动作复用一次验证，不重复打码浪费钱。
"""
import asyncio
import time

import auth
import captcha_solver
import config

_passed_until = 0.0
_human_pass = ""        # human-verify 返回的通行证（后续 REST 请求带 X-Human-Pass）
_lock = None   # 延迟创建 asyncio.Lock（绑定到运行中的事件循环）


def _get_lock():
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def is_passed():
    """当前是否处于「人机已通过」时效内。"""
    return time.time() < _passed_until


def current_pass():
    """未过期的通行证字符串；过期 / 无返回 ""。"""
    return _human_pass if is_passed() else ""


def pass_header():
    """供 REST 请求带上的人机验证通行证头（前端即 X-Human-Pass）。"""
    p = current_pass()
    return {"X-Human-Pass": p} if p else {}


def reset():
    """清除通过状态（被踢 / 401 后调用，强制下次重新验证）。"""
    global _passed_until, _human_pass
    _passed_until = 0.0
    _human_pass = ""


def _alert(on_alert, text):
    print(f"[human-verify] {text}")
    if on_alert:
        try:
            on_alert(f"[human-verify] {text}")
        except Exception as e:
            print(f"[human-verify] on_alert 异常: {e!r}")


async def ensure_passed(*, on_alert=None):
    """
    确保已通过 目标游戏 人机验证。时效内直接 True；否则取 Turnstile token + POST human-verify。
    未启用 / 取 token 失败 / 提交失败 -> False（上层据此放弃该动作、下轮重试）。
    """
    global _passed_until, _human_pass
    if is_passed():
        return True
    if not config.CAPTCHA_ENABLED:
        _alert(on_alert, "遇到人机验证但打码未启用，放弃")
        return False

    async with _get_lock():
        if is_passed():                       # 等锁期间别的协程已过
            return True
        token = await _obtain_token(on_alert)
        if not token:
            _alert(on_alert, "未取得 Turnstile token")
            return False
        result = await _submit(token)
        if result:
            _human_pass = result.get("human_pass", "")
            ttl = float(result.get("expires_in") or config.CAPTCHA_PASS_TTL)
            _passed_until = time.time() + max(0.0, ttl - 5)   # 提前 5s，与前端一致
            print(f"[human-verify] 已通过，{int(ttl)}s 内免再验证（通行证已缓存）")
            return True
        _alert(on_alert, "human-verify 提交未通过")
        return False


async def _obtain_token(on_alert=None):
    """按 config.CAPTCHA_PROVIDER 取 Turnstile token：local(免费) / 2captcha(付费) / auto。"""
    provider = config.CAPTCHA_PROVIDER
    if provider in ("local", "auto"):
        try:
            import local_turnstile
            token = await local_turnstile.solve_local(
                config.CAPTCHA_SITEKEY, config.CAPTCHA_PAGE_URL)
        except Exception as e:
            print(f"[human-verify] 本地解 Turnstile 异常: {e!r}")
            token = None
        if token:
            print("[human-verify] 本地浏览器免费取得 token")
            return token
        if provider == "local":
            _alert(on_alert, "本地浏览器未取得 token（检查 Chrome/Edge 与 DrissionPage）")
            return None
        print("[human-verify] 本地解失败，回退 2captcha")
    if config.CAPTCHA_API_KEY:
        return await captcha_solver.solve(
            config.CAPTCHA_SITEKEY, captcha_type="turnstile",
            page_url=config.CAPTCHA_PAGE_URL)
    _alert(on_alert, "2captcha 未配置 API key")
    return None


async def _submit(token):
    """把 turnstile token 提交到 /api/game/human-verify 解锁。
    成功返回 {"human_pass":..., "expires_in":...}，失败返回 None。"""
    from curl_cffi.requests import AsyncSession

    jwt = auth.get_token()
    if not jwt:
        print("[human-verify] 当前无 JWT，无法提交")
        return None
    url = config.BASE_API_URL.rstrip("/") + config.CAPTCHA_VERIFY_PATH
    headers = dict(auth._headers or {})       # 复用 REST 头（含 Cookie/UA），再确保鉴权头
    headers["Authorization"] = f"Bearer {jwt}"
    headers["Content-Type"] = "application/json"
    try:
        async with AsyncSession(impersonate="chrome120") as s:
            resp = await s.post(url, headers=headers,
                                json={config.CAPTCHA_TOKEN_FIELD: token},
                                timeout=config.REQUEST_TIMEOUT)
            if resp.status_code >= 400:
                print(f"[human-verify] 提交失败 ({resp.status_code}): {resp.text[:200]}")
                return None
            try:
                data = resp.json()
            except Exception:
                data = {}
            hp = data.get("human_pass")
            if not hp:
                print(f"[human-verify] 提交返回无 human_pass: {str(data)[:200]}")
                return None
            return {"human_pass": hp, "expires_in": data.get("expires_in")}
    except Exception as e:
        print(f"[human-verify] 提交异常: {e!r}")
        return None
