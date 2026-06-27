"""
test_ws_state_machine.py — WSStateMachine 高可用沙盒测试（pytest + fake server）。

用 fake_server.FakeFarmServer 在进程内拉起一个“会风控”的 目标游戏 协议服务器，
驱动真实的 ws_state_machine.WSStateMachine，验证两条黄金路径：

  路径一 限速冷却：业务正常发送几条消息后，服务器对下一条下发 rate_limit 限速信号，
                  验证状态机瞬间切到 COOLING_DOWN、后续 ws.action() 优雅卡住，
                  冷却(retry_after)结束后自动恢复，把卡住的消息续发成功。

  路径二 断线重连+重认证：消息发送途中服务器粗暴 abort 连接（物理断线），
                  验证在途请求安全返回 None、状态机进入 RECONNECTING，并自动且成功地
                  调用 auth_provider 重新拿到“新 Token”完成握手，最后让断线时排队
                  没发出的业务请求从断点续发成功。

运行方式见文件末尾 / README。既可 `pytest` 跑，也可 `python test_ws_state_machine.py`
直接跑（无需 pytest，无需真实 .env）。
"""
import asyncio
import sys

# Windows 控制台默认 GBK，打印中文/状态机日志可能编码报错；统一切到 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import pytest

from fake_server import FakeFarmServer, HIDDEN_FLAG, VALID_CHALLENGE_TOKEN
from ws_state_machine import Backoff, State, WSStateMachine


# =========================================================================
# 测试夹具 / 工具
# =========================================================================
def _make_auth_provider():
    """
    伪 auth_provider：每次（重）连前由状态机调用，模拟 auth.refresh_auth。
    每次都返回一个**新的** token，便于断言“重连时确实重新认证拿了新 Token”。
    返回 (auth_provider, stats)；stats 记录调用次数、force 标志、发出的 token 序列。
    """
    stats = {"calls": 0, "forces": [], "tokens": []}

    async def auth_provider(force):
        stats["calls"] += 1
        stats["forces"].append(bool(force))
        token = f"jwt-token-{stats['calls']}"
        stats["tokens"].append(token)
        return {"token": token, "headers": {}}

    return auth_provider, stats


def _make_sm(url, auth_provider, **overrides):
    """构造一台“为测试调快了各项超时”的状态机，并记录其状态变迁序列。"""
    params = dict(
        auth_provider=auth_provider,
        action_map={"harvest": "harvest", "plant": "plant"},
        send_interval=0.02,
        action_timeout=3.0,        # 段2：发出后等结果超时
        max_pause=10.0,            # 段1：等“真正发出”的最长挂起
        handshake_timeout=4.0,
        ping_interval=None,        # 测试里关掉心跳，避免干扰
        backoff=Backoff(base=0.1, factor=2.0, max_delay=0.8, jitter=0.1),
    )
    params.update(overrides)
    sm = WSStateMachine(url, **params)
    seen = []
    sm.on_state_change = lambda old, new: seen.append(new)
    return sm, seen


async def _wait_until(predicate, timeout=5.0, step=0.05):
    """轮询直到 predicate() 为真或超时；返回最终的 predicate() 真假。"""
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        await asyncio.sleep(step)
        waited += step
    return predicate()


# =========================================================================
# 路径一：限速冷却 —— 正常几条 -> rate_limit -> COOLING_DOWN -> 卡住 -> 恢复续发
# =========================================================================
@pytest.mark.asyncio
async def test_rate_limit_cooldown_and_resume():
    # 先放行 2 条，对第 3 条下发限速；冷却 2 秒（对应用户设定的“退避 2 秒”）
    async with FakeFarmServer(scenario="rate_limit",
                              rate_limit_after=2, retry_after=2.0) as server:
        auth_provider, _ = _make_auth_provider()
        sm, seen = _make_sm(server.url, auth_provider)
        sm.start()
        try:
            assert await sm.wait_connected(timeout=5), "首连/握手应成功"
            assert await sm.get_plots(force=True), "握手后应收到服务器推送的 plots"

            # 1) 业务正常发送几条消息 —— 都应成功
            r1 = await sm.action("harvest", {"plot_index": 1})
            r2 = await sm.action("harvest", {"plot_index": 2})
            assert r1 is not None and r2 is not None, "限速前的正常消息应成功"
            assert sm.state == State.CONNECTED, "正常期应处于 CONNECTED"

            # 2) 第 3 条触发服务端限速信号 -> 状态机应瞬间切到 COOLING_DOWN
            limited = asyncio.create_task(sm.action("harvest", {"plot_index": 3}))
            assert await _wait_until(lambda: sm.state == State.COOLING_DOWN,
                                     timeout=3.0), "收到限速信号应进入 COOLING_DOWN"
            assert State.COOLING_DOWN in seen

            # 3) 冷却期间后续 ws.action() 应优雅“卡住”（被自然挂起，不立刻完成）
            stuck = asyncio.create_task(sm.action("plant", {"plot_index": 3}))
            await asyncio.sleep(0.5)
            assert not stuck.done(), "冷却期间业务调用应被挂起，不应完成"
            assert sm.state == State.COOLING_DOWN, "此刻仍应在冷却中"

            # 4) 冷却(retry_after=2s)结束后自动恢复，卡住的消息续发成功
            res_stuck = await asyncio.wait_for(stuck, timeout=6)
            assert res_stuck is not None, "冷却恢复后被卡住的消息应续发成功"
            assert sm.state == State.CONNECTED, "恢复后应回到 CONNECTED"

            # 触发限速的那一条按设计被服务端吞掉 -> 结果超时后安全返回 None
            assert await asyncio.wait_for(limited, timeout=6) is None, \
                "被限速吞掉的在途请求应安全返回 None（业务可下一轮重试）"
        finally:
            await sm.aclose()


# =========================================================================
# 路径二：断线重连 + 重认证 —— 发送途中物理断线 -> RECONNECTING ->
#         auto auth_provider(新 Token)重新握手 -> 断点排队消息续发成功
# =========================================================================
@pytest.mark.asyncio
async def test_disconnect_reconnect_reauth_and_resume_from_breakpoint():
    # 先放行 2 条，对第 3 条物理断线
    async with FakeFarmServer(scenario="drop_once", drop_after=2) as server:
        auth_provider, stats = _make_auth_provider()
        sm, seen = _make_sm(server.url, auth_provider)
        sm.start()
        try:
            assert await sm.wait_connected(timeout=5), "首连/握手应成功"
            assert server.auth_count == 1, "首连应完成一次握手"
            calls_after_connect = stats["calls"]
            first_token = stats["tokens"][0]

            # 1) 业务正常发送几条消息（发送途中）
            assert await sm.action("harvest", {"plot_index": 1}) is not None
            assert await sm.action("harvest", {"plot_index": 2}) is not None

            # 2) 第 3 条发送途中服务器粗暴断线 -> 在途请求应安全返回 None
            res_inflight = await asyncio.wait_for(
                sm.action("harvest", {"plot_index": 3}), timeout=6)
            assert res_inflight is None, "发送途中物理断线，在途请求应安全返回 None"

            # 3) 断点排队：在“线路已断、尚未重连”的窗口排入一条业务消息。
            #    它没有立刻发出，应被保留在发送队列里，等重连恢复后从断点续发。
            await sm.submit({"type": "action", "rid": "resume-1",
                             "action": "harvest", "data": {"plot_index": 99}})

            # 4) 状态机应进入 RECONNECTING，并自动重连
            assert State.RECONNECTING in seen, "断线后应进入 RECONNECTING"
            assert await sm.wait_connected(timeout=6), "应自动重连并重新握手成功"

            # 5) 重连过程中自动且成功地调用了 auth_provider，并拿到“新 Token”完成握手
            assert stats["calls"] > calls_after_connect, \
                "重连应再次调用 auth_provider(refresh_auth)"
            assert server.auth_count >= 2, "服务器应记录到第二次握手"
            assert server.connections >= 2, "应建立过至少两条物理连接"
            assert len(server.auth_tokens) >= 2, "服务器应收到两次握手 token"
            assert server.auth_tokens[1] != first_token, \
                "重连握手应使用重新认证拿到的新 Token"
            assert server.auth_tokens[1] == stats["tokens"][-1], \
                "服务器收到的新 Token 应与 auth_provider 最新返回的一致"

            # 6) 断点续发：排队消息应在重连后的新连接上被真正发出
            assert await _wait_until(
                lambda: any(a.get("rid") == "resume-1"
                            for a in server.received_actions),
                timeout=6), "断线时排队没发出的消息应在重连后从断点续发成功"

            # 7) 业务彻底恢复：新连接上后续请求-响应正常
            assert await sm.action("harvest", {"plot_index": 4}) is not None, \
                "重连恢复后业务请求-响应应正常"
            assert sm.state == State.CONNECTED, "最终应处于 CONNECTED"
        finally:
            await sm.aclose()


# =========================================================================
# 路径三：应用层 challenge -> 外部组件返回 token -> 注入原请求重发 -> 拿到 flag
# =========================================================================
@pytest.mark.asyncio
async def test_challenge_solver_injects_token_and_returns_flag():
    async with FakeFarmServer(scenario="challenge") as server:
        auth_provider, _ = _make_auth_provider()
        solver_calls = []

        async def challenge_solver(challenge):
            solver_calls.append(challenge)
            assert challenge.get("sitekey") == "xyz_matrix_key"
            await asyncio.sleep(0.05)
            return VALID_CHALLENGE_TOKEN

        sm, seen = _make_sm(
            server.url,
            auth_provider,
            action_map={"steal": "steal-crops"},
            challenge_solver=challenge_solver,
            action_timeout=5.0,
        )
        sm.start()
        try:
            assert await sm.wait_connected(timeout=5), "首次连接/握手应成功"

            data = await sm.action("steal", {})
            assert data is not None, "challenge 续发后应返回成功数据"
            assert data.get("flag") == HIDDEN_FLAG
            assert HIDDEN_FLAG in data.get("message", "")
            assert len(solver_calls) == 1, "应只调用一次外部质询组件"
            assert State.CHALLENGE in seen, "收到 challenge_required 应进入 CHALLENGE"

            steal_actions = [
                a for a in server.received_actions
                if a.get("action") == "steal-crops"
            ]
            assert len(steal_actions) == 2, "应先触发 challenge，再携带 token 重发"
            assert steal_actions[0].get("data", {}).get("captcha_token") is None
            assert steal_actions[1].get("data", {}).get("captcha_token") == VALID_CHALLENGE_TOKEN
        finally:
            await sm.aclose()


# =========================================================================
# 无 pytest 时的独立运行入口
# =========================================================================
async def _run_all():
    print("\n===== WSStateMachine 高可用沙盒测试（standalone）=====")
    cases = [
        ("路径一 限速冷却→挂起→续发", test_rate_limit_cooldown_and_resume),
        ("路径二 断线重连→重认证→断点续发", test_disconnect_reconnect_reauth_and_resume_from_breakpoint),
        ("路径三 challenge→外部组件→token 续发", test_challenge_solver_injects_token_and_returns_flag),
    ]
    failures = 0
    for name, fn in cases:
        print(f"\n>>> {name}")
        try:
            await fn()
            print("  PASS")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR: {e!r}")
    print("\n===== 测试结束 =====")
    print(f"[OK] 全部通过" if not failures else f"[FAIL] {failures} 条路径未通过")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if asyncio.run(_run_all()) else 0)
