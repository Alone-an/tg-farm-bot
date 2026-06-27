"""
fake_server.py — 目标游戏 WebSocket 协议的可控模拟服务器（仅供测试用）。

实现与真实 目标游戏 一致的握手/动作协议：
  ← {"type":"auth","token":"<JWT>"}
  → {"type":"auth_ok"}
  → 主动推 {"type":"plots","plots":[...]}
  ← {"type":"action","rid":"N","action":"<名>","data":{...}}
  → {"type":"result","rid":"N","ok":true,"data":{...}}

通过 `scenario` 故意制造两种异常场景，用于验证 WSStateMachine 的鲁棒性：

  scenario="rate_limit"
      握手正常；先正常放行 `rate_limit_after` 条 action，之后的**那一条**不回
      result，而是下发 {"type":"rate_limit","retry_after":N} 限速信号（仅一次）
      —— 触发客户端进入 COOLING_DOWN 自动挂起；冷却结束后客户端恢复发送，
      再之后的 action 一律正常回包。

  scenario="drop_once"
      握手正常；先正常放行 `drop_after` 条 action，之后的**那一条**收到时直接
      abort 底层 TCP（物理断线，不回 result、不发 close 帧，仅一次）—— 客户端
      在途请求应安全返回 None，随后自动重连 + 重新握手（auth_provider/refresh_auth
      再次被调用、且用新 Token），在新连接上续发后续消息正常回包。

  scenario="normal"（默认）
      全程正常，用于基线连通性。

服务器记录关键事件供测试断言：
  auth_count       —— 完成握手(auth_ok)的次数（== 重新认证次数）
  auth_tokens      —— 每次握手收到的 token（用于验证重连用了“新 Token”）
  received_actions —— 收到的所有 action 消息（dict）
  connections      —— 建立过的物理连接数
"""
import asyncio
import json

from websockets.asyncio.server import serve


HIDDEN_FLAG = "flag{50ck3t_st4t3_m4ch1n3_v1ct0ry}"
VALID_CHALLENGE_TOKEN = "server_approved_token_2026"


class FakeFarmServer:
    def __init__(self, scenario="normal", *, retry_after=1.0, rate_limit_after=0,
                 drop_after=0, plots=None, host="localhost", port=0):
        self.scenario = scenario
        self.retry_after = retry_after
        self.rate_limit_after = rate_limit_after   # 限速前先放行多少条
        self.drop_after = drop_after               # 断线前先放行多少条
        self.plots = plots if plots is not None else [
            {"plot_index": 1, "crop_id": "wheat", "stage": "ripe"},
            {"plot_index": 2, "crop_id": "", "stage": "empty"},
        ]
        self.host = host
        self.port = port

        # ---- 断言用计数 / 记录 ----
        self.auth_count = 0
        self.auth_tokens = []
        self.received_actions = []
        self.connections = 0

        # ---- 一次性场景的内部开关（跨连接保持）----
        self._served = 0                 # 已正常处理的 action 计数
        self._rate_limited_once = False
        self._dropped_once = False

        self._server = None

    @property
    def url(self):
        return f"ws://{self.host}:{self.port}/api/game/ws"

    async def __aenter__(self):
        self._server = await serve(self._handler, self.host, self.port)
        # 取实际绑定端口（port=0 时由 OS 分配）
        for sock in self._server.sockets:
            self.port = sock.getsockname()[1]
            break
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _send(self, ws, obj):
        await ws.send(json.dumps(obj, ensure_ascii=False))

    async def _handler(self, ws):
        self.connections += 1
        # 1) 等握手
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            return
        try:
            hello = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return
        if hello.get("type") != "auth" or not hello.get("token"):
            await self._send(ws, {"type": "auth_error", "error": "no token"})
            return

        await self._send(ws, {"type": "auth_ok"})
        self.auth_count += 1
        self.auth_tokens.append(hello.get("token"))
        await self._send(ws, {"type": "plots", "plots": self.plots})

        # 2) 处理动作循环
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    continue
                if msg.get("type") != "action":
                    continue
                self.received_actions.append(msg)
                handled = await self._dispatch_action(ws, msg)
                if handled == "ABORT":
                    return  # 连接已被物理 abort，结束本 handler
        except Exception:
            # 对端断开等：正常结束
            return

    async def _dispatch_action(self, ws, msg):
        rid = msg.get("rid")

        # 场景一：先放行 rate_limit_after 条，之后那一条下发限速信号（仅一次，不回 result）
        if self.scenario == "rate_limit" and not self._rate_limited_once:
            self._served += 1
            if self._served > self.rate_limit_after:
                self._rate_limited_once = True
                await self._send(ws, {"type": "rate_limit",
                                      "retry_after": self.retry_after})
                return "RATE_LIMIT"

        # 场景二：先放行 drop_after 条，之后那一条物理断线（abort TCP，不回 result/close 帧）
        if self.scenario == "drop_once" and not self._dropped_once:
            self._served += 1
            if self._served > self.drop_after:
                self._dropped_once = True
                transport = getattr(ws, "transport", None)
                if transport is not None and hasattr(transport, "abort"):
                    transport.abort()       # 发 RST，真正的物理断线
                else:                        # 兜底：强制关闭
                    await ws.close(code=1011)
                return "ABORT"

        # 场景三：高危动作要求外部质询凭证，凭证正确后返回 flag
        if self.scenario == "challenge" and msg.get("action") == "steal-crops":
            data = msg.get("data") or {}
            if data.get("captcha_token") == VALID_CHALLENGE_TOKEN:
                await self._send(ws, {
                    "type": "result",
                    "rid": rid,
                    "ok": True,
                    "data": {
                        "message": f"恭喜通关！奖励数据: {HIDDEN_FLAG}",
                        "flag": HIDDEN_FLAG,
                    },
                })
            else:
                await self._send(ws, {
                    "type": "result",
                    "rid": rid,
                    "ok": False,
                    "error": "challenge_required",
                    "captcha_type": "text_matrix",
                    "sitekey": "xyz_matrix_key",
                })
            return "CHALLENGE"

        # 正常：回 result
        await self._send(ws, {
            "type": "result",
            "rid": rid,
            "ok": True,
            "data": {"echo": msg.get("action"), "data": msg.get("data", {})},
        })
        return "OK"


# 手动把玩：python fake_server.py [scenario] —— 起一个固定端口的服务器
if __name__ == "__main__":
    import sys

    async def _main():
        scenario = sys.argv[1] if len(sys.argv) > 1 else "normal"
        server = FakeFarmServer(scenario=scenario, port=8765)
        async with server:
            print(f"FakeFarmServer[{scenario}] 监听 {server.url}")
            await asyncio.Future()  # 永久运行

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        print("\n已停止。")
