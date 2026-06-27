"""
ws_session.py — 统一构建 / 启动 WSStateMachine 的工厂（单一事实来源）。

把"环境伪装(Origin/UA/Cookie) + 鉴权回调(auth_provider) + 退避(Backoff) +
限速冷却 + 断线重连重认证"收拢到一处，供三个入口共享，避免实现漂移：
  - main.run_once      （本地常驻：首轮初始化）
  - run_once.run_cycle （云端无人值守：跑一轮）
  - scheduler._run_daily（本地常驻：每日定时）

这样"首轮初始化"和"后续每日调度"用的是同一套状态机配置——同样的 header、
同样的退避曲线、同样的重连重认证逻辑。
"""
import contextlib

import auth
import config
import human_verify
from endpoints import WS_ACTIONS, WS_PASS_ACTIONS
from ws_state_machine import Backoff, WSStateMachine


def build_ws_headers():
    """WS 握手用的 headers（Origin/UA/Cookie），与原 FarmWS.connect 完全一致。"""
    cookie_str = "; ".join(f"{k}={v}" for k, v in (auth._cf_cookies or {}).items())
    headers = {
        "Origin": config.MINI_APP_URL.rstrip("/"),
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36"),
    }
    if cookie_str:
        headers["Cookie"] = cookie_str
    return headers


def print_alert(text):
    """默认告警：仅打印（一次性入口用）。scheduler 会换成推微信。"""
    print(f"[ws] ALERT: {text}")


def default_backoff():
    return Backoff(base=1.0, factor=2.0, max_delay=60.0, jitter=0.3)


def make_state_machine(client, *, on_alert=None, backoff=None, **overrides):
    """
    构建一个 WSStateMachine。

    auth_provider 闭包绑定传入的 Telegram client：状态机每次（重）连前调用，
    force=True 表示需强制重新认证（被踢 / 鉴权失败后）—— 即"自动触发
    refresh_auth 重新握手"。
    """
    async def auth_provider(force):
        await auth.refresh_auth(client, force=force)
        return {"token": auth.get_token(), "headers": build_ws_headers()}

    alert = on_alert or print_alert

    async def challenge_solver(challenge):
        # 目标游戏 应用层 Turnstile：带外 POST /api/game/human-verify 解锁，拿到通行证后由状态机
        # 注入 data.human_pass 重发动作。返回真实通行证（hp_...）而非占位符，供 _inject 使用。
        ok = await human_verify.ensure_passed(on_alert=alert)
        return human_verify.current_pass() if ok else None

    params = dict(
        auth_provider=auth_provider,
        action_map=WS_ACTIONS,                 # action_key -> 真实动作名
        action_timeout=config.WS_ACTION_TIMEOUT,
        on_alert=alert,
        backoff=backoff or default_backoff(),
        challenge_solver=challenge_solver,     # 遇 NEED_HUMAN_VERIFICATION 自动过 Turnstile
        challenge_inject=True,                 # 解锁后把通行证注入 data.human_pass 重发
        challenge_token_field=config.CAPTCHA_PASS_RESP_FIELD,  # "human_pass"
        challenge_code=config.CAPTCHA_CHALLENGE_CODE,
        pass_provider=human_verify.current_pass,   # 发送前主动带通行证
        pass_actions=WS_PASS_ACTIONS,              # 需带通行证的 WS 动作(harvest/plant)
        proxy=config.PROXY_URL or None,            # 出口代理(云端走机房IP过CF);本地空=直连
    )
    params.update(overrides)
    return WSStateMachine(config.WS_URL, **params)


@contextlib.asynccontextmanager
async def open_state_machine(client, *, on_alert=None, connect_timeout=60,
                             backoff=None, **overrides):
    """
    异步上下文管理器：构建并 start 状态机，等待首连，退出时优雅 aclose。

    用法：
        async with open_state_machine(client, on_alert=_alert) as (sm, connected):
            if not connected:
                ...  # 首连/握手失败
            else:
                await run_all_tasks(session, headers, sm)
        # 退出 with：自动放弃在途请求、关连接、停重连

    yield 出 (sm, connected)：connected 为 False 表示在 connect_timeout 内
    未能进入 CONNECTED（首连/握手失败）。
    """
    sm = make_state_machine(client, on_alert=on_alert, backoff=backoff, **overrides)
    sm.start()
    try:
        connected = await sm.wait_connected(timeout=connect_timeout)
        yield sm, connected
    finally:
        await sm.aclose()
