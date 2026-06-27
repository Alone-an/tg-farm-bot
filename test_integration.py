"""
test_integration.py — 本地 Fake WebSocket 集成测试。

直接运行：
    python test_integration.py

覆盖三条生产关键路径：
  1. 发送中断后重连续发，不丢 submit 业务包。
  2. 服务端限速冷却期间暂停发送，队列满载时 submit 产生背压。
  3. 持续连接失败时，scheduler.monitor_loop 触发全盘熔断并优雅关闭。
"""
import asyncio
import json

import websockets

from scheduler import Scheduler
from ws_state_machine import Backoff, State, WSStateMachine


async def wait_until(predicate, timeout=5.0, interval=0.05, label="条件"):
    """轮询等待异步状态变化，超时直接断言失败。"""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError(f"等待 {label} 超时")


async def auth_provider(force=False):
    """Fake 鉴权：只给状态机一个可用 token。"""
    return {"token": "fake-token", "headers": {}}


class FakeWSServer:
    """最小本地 WS 服务端，按 handler 模拟不同风控/异常场景。"""

    def __init__(self, handler):
        self._handler = handler
        self._server = None
        self.url = None

    async def __aenter__(self):
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        print(f"[test] Fake WS Server 启动: {self.url}")
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._server.close()
        await self._server.wait_closed()
        print("[test] Fake WS Server 已关闭")


class SendFailOnceProxy:
    """包装真实 websocket，让第一条业务 send 在客户端本地失败。"""

    def __init__(self, ws, owner):
        self._ws = ws
        self._owner = owner

    def __getattr__(self, name):
        return getattr(self._ws, name)

    def __aiter__(self):
        return self._ws.__aiter__()

    async def send(self, data):
        if not self._owner.failed_once:
            self._owner.failed_once = True
            print("[test] 注入一次发送中断")
            raise OSError("fake send interrupted")
        return await self._ws.send(data)


class FailOnceStateMachine(WSStateMachine):
    """首次业务发送失败，用于稳定验证 _inflight 重连续发。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.failed_once = False

    async def _open(self, creds):
        await super()._open(creds)
        if not self.failed_once:
            self._ws = SendFailOnceProxy(self._ws, self)


async def test_reconnect_resume_path():
    print("\n========== 测试1：断线后续发 ==========")
    received = []
    got_payload = asyncio.Event()

    async def handler(ws, path=None):
        raw = await ws.recv()
        auth_msg = json.loads(raw)
        assert auth_msg["type"] == "auth"
        await ws.send(json.dumps({"type": "auth_ok"}))
        async for raw in ws:
            msg = json.loads(raw)
            received.append(msg)
            print(f"[test-server] 收到业务包: {msg}")
            if msg.get("seq") == 1:
                got_payload.set()

    async with FakeWSServer(handler) as server:
        sm = FailOnceStateMachine(
            server.url,
            auth_provider=auth_provider,
            backoff=Backoff(base=0.01, max_delay=0.01, jitter=0),
            send_interval=0.01,
        )
        sm.start()
        assert await sm.wait_connected(timeout=2)

        await sm.submit({"type": "biz", "seq": 1})
        await asyncio.wait_for(got_payload.wait(), timeout=5)

        assert sm.failed_once is True
        assert received == [{"type": "biz", "seq": 1}]
        await sm.aclose()
    print("[test] 断线后续发通过")


async def test_cooldown_backpressure_path():
    print("\n========== 测试2：限速冷却与背压 ==========")
    received = []
    cooldown_seen = asyncio.Event()

    async def handler(ws, path=None):
        raw = await ws.recv()
        auth_msg = json.loads(raw)
        assert auth_msg["type"] == "auth"
        await ws.send(json.dumps({"type": "auth_ok"}))
        await ws.send(json.dumps({"type": "rate_limit", "cooldown": 3}))
        async for raw in ws:
            msg = json.loads(raw)
            received.append(msg)
            print(f"[test-server] 冷却后收到: {msg}")

    async with FakeWSServer(handler) as server:
        sm = WSStateMachine(
            server.url,
            auth_provider=auth_provider,
            backoff=Backoff(base=0.01, max_delay=0.01, jitter=0),
            send_interval=0.01,
            max_queue=1,
        )

        old_schedule_relax = sm._schedule_relax_window
        sm._schedule_relax_window = lambda calm_seconds=30.0: old_schedule_relax(0.1)

        def on_state_change(old, new):
            if new == State.COOLING_DOWN:
                cooldown_seen.set()

        sm.on_state_change = on_state_change
        sm.start()
        assert await sm.wait_connected(timeout=2)
        await asyncio.wait_for(cooldown_seen.wait(), timeout=2)
        assert sm.state == State.COOLING_DOWN

        first = asyncio.create_task(sm.submit({"type": "biz", "seq": 1}))
        await asyncio.wait_for(first, timeout=1)
        second = asyncio.create_task(sm.submit({"type": "biz", "seq": 2}))
        await asyncio.sleep(0.3)
        assert not second.done(), "队列满且冷却中时，第二个 submit 必须产生背压"

        await asyncio.wait_for(second, timeout=5)
        await wait_until(lambda: sm.state == State.CONNECTED, timeout=5, label="冷却恢复")
        await wait_until(lambda: sm._dynamic_interval == 0.0,
                         timeout=2,
                         label="临时降速解除")
        await wait_until(lambda: len(received) >= 1, timeout=5, label="冷却后恢复发送")

        await sm.aclose()
    print("[test] 限速冷却与背压通过")


async def test_circuit_breaker_path():
    print("\n========== 测试3：触发熔断保护 ==========")

    async def handler(ws, path=None):
        await ws.close(code=1013, reason="try again later")

    async with FakeWSServer(handler) as server:
        sm = WSStateMachine(
            server.url,
            auth_provider=auth_provider,
            detect_limit=lambda msg, close_code: None,
            backoff=Backoff(base=0.2, max_delay=0.2, jitter=0),
            handshake_timeout=0.2,
            send_interval=0.01,
            alert_threshold=999,
        )
        scheduler = Scheduler(client=None, sm=sm)
        sm.start()
        monitor = asyncio.create_task(scheduler.monitor_loop(), name="test-monitor")

        await wait_until(lambda: scheduler._stop_event.is_set(),
                         timeout=8,
                         label="熔断触发")
        assert scheduler._circuit_open is True
        await wait_until(lambda: sm.state == State.CLOSED,
                         timeout=2,
                         label="状态机关闭")

        await asyncio.gather(monitor, return_exceptions=True)
    print("[test] 熔断保护通过")


async def main():
    await test_reconnect_resume_path()
    await test_cooldown_backpressure_path()
    await test_circuit_breaker_path()
    print("\n========== 全部本地集成测试通过 ==========")


if __name__ == "__main__":
    asyncio.run(main())
