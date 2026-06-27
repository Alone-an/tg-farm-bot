"""
captcha_solver.py — 验证码打码服务对接（2captcha 兼容协议）。

被 WS 状态机（ws_session.make_state_machine 注入）与 REST 层（api.py）共用：
给定 challenge 的 sitekey 与类型，调打码平台拿到临时 token。

协议用经典的 in.php(提交) + res.php(轮询)，CapSolver / YesCaptcha 等多家平台都
兼容，换平台只改 config.CAPTCHA_API_BASE。所有失败 / 未配置一律返回 None（不抛、
不卡死收发循环），让上层优雅放弃该请求、等下一轮重试。

打码平台是第三方域名，用**独立**的 curl_cffi 会话，不带 目标游戏 的 cookie / 指纹。

⚠️ 真实协议字段（challenge 里 sitekey 的键名、token 回填字段、验证码类型）尚未用
真实报文验证，均做成 config 可配（CAPTCHA_SITEKEY_FIELD / CAPTCHA_TOKEN_FIELD /
CAPTCHA_TYPE）。
"""
import asyncio

import config


# 验证码类型 -> (2captcha method, sitekey 参数名)
_METHOD = {
    "turnstile":    ("turnstile", "sitekey"),
    "recaptcha_v2": ("userrecaptcha", "googlekey"),
    "hcaptcha":     ("hcaptcha", "sitekey"),
}


def _log(msg):
    print(f"[captcha] {msg}")


async def solve(sitekey, *, captcha_type=None, page_url=None):
    """
    提交一道验证码给打码平台并轮询取 token。

    成功返回 token 字符串；未配置 key / 参数缺失 / 平台报错 / 轮询超时 -> None。
    """
    if not config.CAPTCHA_API_KEY:
        _log("未配置 CAPTCHA_API_KEY，跳过打码")
        return None
    if not sitekey:
        _log("challenge 缺少 sitekey（检查 CAPTCHA_SITEKEY_FIELD 是否与真实报文一致）")
        return None

    ctype = (captcha_type or config.CAPTCHA_TYPE or "turnstile").lower()
    if ctype not in _METHOD:
        _log(f"不支持的验证码类型: {ctype!r}（可选 {list(_METHOD)}）")
        return None
    method, sitekey_param = _METHOD[ctype]
    page_url = page_url or config.CAPTCHA_PAGE_URL
    base = config.CAPTCHA_API_BASE.rstrip("/")

    submit_data = {
        "key": config.CAPTCHA_API_KEY,
        "method": method,
        sitekey_param: sitekey,
        "pageurl": page_url,
        "json": 1,
    }

    from curl_cffi.requests import AsyncSession
    try:
        async with AsyncSession() as s:
            # 1) 提交任务
            resp = await s.post(f"{base}/in.php", data=submit_data,
                                timeout=config.REQUEST_TIMEOUT)
            j = resp.json()
            if str(j.get("status")) != "1":
                _log(f"提交打码失败: {j.get('request') or j}")
                return None
            task_id = j.get("request")
            _log(f"已提交打码任务 id={task_id} type={ctype}")

            # 2) 轮询结果（总上限 CAPTCHA_POLL_TIMEOUT，间隔 CAPTCHA_POLL_INTERVAL）
            waited = 0.0
            interval = max(1, config.CAPTCHA_POLL_INTERVAL)
            while waited < config.CAPTCHA_POLL_TIMEOUT:
                await asyncio.sleep(interval)
                waited += interval
                r = await s.get(f"{base}/res.php",
                                params={"key": config.CAPTCHA_API_KEY, "action": "get",
                                        "id": task_id, "json": 1},
                                timeout=config.REQUEST_TIMEOUT)
                jr = r.json()
                req = jr.get("request")
                if str(jr.get("status")) == "1":
                    _log(f"打码成功 id={task_id}（耗时约 {waited:.0f}s）")
                    return req
                if req and req != "CAPCHA_NOT_READY":
                    _log(f"打码出错 id={task_id}: {req}")
                    return None
            _log(f"打码轮询超时（>{config.CAPTCHA_POLL_TIMEOUT}s）id={task_id}")
            return None
    except Exception as e:
        _log(f"打码请求异常: {e!r}")
        return None


def make_captcha_solver(*, on_alert=None):
    """
    返回一个签名匹配状态机 challenge_solver 的协程：async solver(challenge) -> token|None。

    未启用 / 无 key 时返回的仍是「降级版」（告警 + None），而不是 None 本身——这样
    状态机的 _is_challenge_required 仍为 True，遇质询会进入 CHALLENGE 状态并告警
    （可观测），而不是把质询回包当普通失败静默吞掉。
    """
    def _alert(text):
        _log(text)
        if on_alert:
            try:
                on_alert(f"[captcha] {text}")
            except Exception as e:
                _log(f"on_alert 异常: {e!r}")

    async def _solver(challenge):
        if not (config.CAPTCHA_ENABLED and config.CAPTCHA_API_KEY):
            _alert("收到质询但打码未启用 / 无 API key，放弃该请求")
            return None
        sitekey = (challenge or {}).get(config.CAPTCHA_SITEKEY_FIELD)
        token = await solve(sitekey, page_url=config.CAPTCHA_PAGE_URL)
        if not token:
            _alert("打码未取得 token，放弃该请求")
        return token

    return _solver
