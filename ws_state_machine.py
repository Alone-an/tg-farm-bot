"""
ws_state_machine.py — 健壮、合规的异步 WebSocket 长连接状态机。

在原"单向发送队列"基础上，升级为**状态感知的请求-响应（RPC）客户端**，
并提供与现有 ws.FarmWS 同签名的 action()/get_plots()，可被 tasks.py 鸭子替换。

三大能力：
  1. 严格退避限速（Backoff & Rate Limiting）
     - 发送侧队列 + 节流（最小发包间隔），submit/call 满队列自动背压。
     - 收到服务端 throttle/429 或被 1013/1008 关闭码踢下线时，自动切
       COOLING_DOWN，用"指数退避 + 抖动"冷却，恢复后维持一段降速——尊重
       服务端保护，绝不硬冲。

  2. 优雅断线重连与重新认证（Reconnection & Re-Auth）
     - 断线后清理旧会话；冷却结束后调用 auth_provider(force) 重新认证，
       重建连接；
     - 连续失败达阈值 -> 触发 on_alert 报警 + 进入更大周期长休眠；
     - 鉴权类失败时，下次重连会 force 重新认证。

  3. 请求-响应语义（RPC）+ 业务无缝挂起
     - call(action_key, data) 发一条动作并等结果回包（按 rid 路由 future）。
     - 两段式超时：在队列里等"真正发出"的时间（因冷却/暂停而挂起）**不计**
       结果超时；只有真正发出后才开始计结果超时。于是 COOLING_DOWN/重连期间
       业务调用会自然挂起，状态恢复后继续。
     - 断线重连会**放弃所有在途请求**（返回 None，等业务下一轮重试），而不是
       重发——避免重复 harvest/plant 这类有副作用的动作。



依赖：websockets（新旧版本 header 参数名都兼容）。
"""
import asyncio
import enum
import json
import random


MAX_COOLDOWN_SECONDS = 3600.0


def _now():
    """当前事件循环时钟（单调）。仅在协程上下文调用。"""
    return asyncio.get_running_loop().time()


def _clamp_seconds(value, default=None, *, label="cooldown"):
    """把服务端给出的等待秒数规整到安全范围，避免异常值拖垮收发循环。"""
    if value is None and default is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        if default is None:
            print(f"[sched] 忽略非法 {label}: {value!r}")
            return None
        seconds = float(default)
    if seconds < 0:
        print(f"[sched] 忽略负数 {label}: {seconds!r}")
        return None
    if seconds > MAX_COOLDOWN_SECONDS:
        print(f"[sched] {label}={seconds:.1f}s 过大，限制为 {MAX_COOLDOWN_SECONDS:.0f}s")
        return MAX_COOLDOWN_SECONDS
    return seconds


class State(enum.Enum):
    DISCONNECTED = "DISCONNECTED"   # 初始 / 未连接
    CONNECTING = "CONNECTING"       # 正在建连 + 握手
    CONNECTED = "CONNECTED"         # 已就绪，可正常收发
    PAUSED = "PAUSED"               # 手动暂停发送（连接仍在）
    CHALLENGE = "CHALLENGE"         # 等待外部合法质询组件返回临时凭证
    COOLING_DOWN = "COOLING_DOWN"   # 收到限速信号，定时冷却中（连接可能仍在）
    RECONNECTING = "RECONNECTING"   # 断开后，退避等待重连
    CLOSED = "CLOSED"               # 终态：主动关闭


class HandshakeError(Exception):
    """握手/鉴权阶段失败——触发下次重连时 force 重新认证。"""


class Backoff:
    """指数退避 + 抖动（full-jitter 的简化版）。"""

    def __init__(self, base=1.0, factor=2.0, max_delay=60.0, jitter=0.3):
        self.base = base
        self.factor = factor
        self.max_delay = max_delay
        self.jitter = jitter
        self._attempt = 0

    def reset(self):
        self._attempt = 0

    def next(self):
        raw = min(self.max_delay, self.base * (self.factor ** self._attempt))
        self._attempt += 1
        delta = raw * self.jitter
        return max(0.0, raw + random.uniform(-delta, delta))


def default_detect_limit(msg, close_code):
    """
    默认限速信号识别器：返回"建议冷却秒数"，不是限速信号则 None。
    只读取服务端明确下发的节流指示并照做。请按 目标游戏 真实协议调整字段。
    """
    if close_code in (1013, 1008):     # 1013=稍后再试, 1008=策略违规
        return 30.0
    if not isinstance(msg, dict):
        return None
    t = msg.get("type")
    if t in ("rate_limit", "too_many_requests", "throttle"):
        return _clamp_seconds(
            msg.get("retry_after", msg.get("cooldown", 5.0)),
            default=5.0,
            label="retry_after/cooldown")
    if t == "error" and str(msg.get("code")) in ("429", "rate_limited"):
        return _clamp_seconds(msg.get("retry_after", 5.0),
                              default=5.0,
                              label="retry_after")
    return None


class _Call:
    """一条排队中的发送条目。call() 用它做请求-响应；submit() 只用 payload。"""
    __slots__ = ("rid", "payload", "future", "sent", "challenge_attempts")

    def __init__(self, rid, payload):
        self.rid = rid              # str 或 None（submit 单向发送时为 None）
        self.payload = payload      # 真正发出的 dict/str/bytes
        self.future = None          # rid 对应的结果 future（call 才有）
        self.sent = asyncio.Event()  # "已真正发出"信号（两段式超时用）
        self.challenge_attempts = 0


class WSStateMachine:
    """
    状态感知的异步 WebSocket RPC 客户端。

    关键参数：
      url            : ws/wss 地址（如 config.WS_URL）
      auth_provider  : async (force: bool) -> {"token": JWT, "headers": {...}}
                       每次（重）连接前调用；force=True 表示需强制重新认证。
      action_map     : dict，action_key -> 真实 action 名（传 endpoints.WS_ACTIONS）。
      on_message     : async (msg) -> None，非 result/plots/限速 的入站消息回调。
      on_alert       : (text) -> None，连续失败达阈值时的报警回调（可推微信）。
      detect_limit   : (msg, close_code) -> float|None，限速信号识别器。
      send_interval  : 正常最小发包间隔（秒）。
      action_timeout : 单条 call 发出后等结果的超时（秒）。
      max_pause      : 单条 call 在队列里等"真正发出"的最长挂起（秒），防卡死。
      alert_threshold: 连续失败多少次后报警 + 长休眠。
      long_sleep     : 触发报警后的长休眠秒数。
    """

    def __init__(self, url, *, auth_provider, action_map=None, on_message=None,
                 on_alert=None, on_send_confirm=None, challenge_solver=None,
                 challenge_timeout=150.0, challenge_token_field="captcha_token",
                 challenge_code="challenge_required", challenge_inject=True,
                 pass_provider=None, pass_actions=None,
                 detect_limit=None, send_interval=0.2,
                 backoff=None, max_queue=1000, ping_interval=20,
                 handshake_timeout=15, action_timeout=15, max_pause=180.0,
                 alert_threshold=5, long_sleep=1800.0, proxy=None):
        self.url = url
        self._auth_provider = auth_provider
        self._action_map = action_map or {}
        self._on_message = on_message
        self._on_alert = on_alert
        self._on_send_confirm = on_send_confirm
        self._challenge_solver = challenge_solver
        self._challenge_timeout = challenge_timeout
        self._challenge_token_field = challenge_token_field
        self._challenge_code = challenge_code
        self._challenge_inject = challenge_inject
        # 通行证主动注入：对 pass_actions 里的动作，发送前从 pass_provider() 取通行证塞进
        # data（目标游戏：harvest/plant 等 WS 动作要带 data.human_pass，机制同前端 _m 集合）。
        self._pass_provider = pass_provider
        self._pass_actions = frozenset(pass_actions or ())
        self._detect_limit = detect_limit or default_detect_limit
        self._send_interval = send_interval
        self._backoff = backoff or Backoff()
        self._ping_interval = ping_interval
        self._handshake_timeout = handshake_timeout
        self._action_timeout = action_timeout
        self._max_pause = max_pause
        self._alert_threshold = alert_threshold
        self._long_sleep = long_sleep
        # 出口代理（如 http://127.0.0.1:7890）：仅在显式传入时把 wss 经它走（HTTP CONNECT），
        # 供云端从干净机房 IP 出去过 Cloudflare。为 None 时不传 proxy kwarg，保持 websockets
        # 默认行为（读环境变量，本地/云端均无 -> 直连），与接入前一致。
        self._proxy = proxy

        self._outbound = asyncio.Queue(maxsize=max_queue)
        self._inflight = None            # 取出但尚未确认发出的 _Call

        self._can_send = asyncio.Event()       # 发送闸门：set=可发, clear=暂停
        self._connected_event = asyncio.Event()  # 已就绪（供 wait_connected）
        self._closing_event = asyncio.Event()  # 主动关闭（用于可中断 sleep）

        self._state = State.DISCONNECTED
        self._ws = None
        self._closing = False

        self._rid = 0
        self._pending = {}               # rid(str) -> _Call（等结果的在途请求）
        self._plots = []                 # 服务器握手后被动推送的地块缓存

        self._dynamic_interval = 0.0     # 服务端要求的临时降速间隔
        self._resume_at = 0.0            # 冷却恢复时间戳
        self._cooldown_task = None
        self._relax_task = None
        self._supervisor_task = None

        self._need_reauth = False        # 下次重连是否强制重新认证
        self._consecutive_failures = 0   # 连续会话失败次数
        self._manual_paused = False      # 用户显式 pause() 意图，不能被冷却/重连覆盖

        self.on_state_change = None      # 可选： (old, new) -> None

    # ---------- 状态 ----------
    @property
    def state(self):
        return self._state

    def _set_state(self, new):
        if new == self._state:
            return
        old, self._state = self._state, new
        print(f"[sched] 状态: {old.value} -> {new.value}")
        if self.on_state_change:
            try:
                self.on_state_change(old, new)
            except Exception as e:
                print(f"[sched] on_state_change 异常: {e!r}")

    # ---------- 对外 API ----------
    def start(self):
        """启动监督协程（连接/重连生命周期）。返回该 task。"""
        if self._supervisor_task and not self._supervisor_task.done():
            print("[sched] start 已调用，复用现有 ws-supervisor")
            return self._supervisor_task
        self._closing = False
        self._closing_event.clear()
        self._supervisor_task = asyncio.create_task(self._run(), name="ws-supervisor")
        return self._supervisor_task

    async def wait_connected(self, timeout=60):
        """等待首次（或重连后）进入 CONNECTED。超时返回 False。"""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def submit(self, payload):
        """单向发送一条消息（不等结果）。payload 为 dict/str/bytes。"""
        await self._outbound.put(_Call(None, payload))

    async def call(self, action_key, data=None, timeout=None):
        """
        发一个动作并等结果回包（请求-响应）。与 ws.FarmWS.action 同语义：
        成功返回 result 的 data 部分，失败/超时/被放弃返回 None。

        冷却/暂停/重连期间，本调用会自然挂起，直到消息真正发出后才计结果超时。
        """
        if self._closing:
            return None
        real = self._action_map.get(action_key, action_key)
        self._rid += 1
        rid = str(self._rid)
        env = _Call(rid, {"type": "action", "rid": rid,
                          "action": real, "data": data or {}})
        env.future = asyncio.get_running_loop().create_future()
        self._pending[rid] = env
        await self._outbound.put(env)

        # 第 1 段：等"真正发出"。冷却/暂停期间在此挂起，不计结果超时。
        try:
            await asyncio.wait_for(env.sent.wait(), timeout=self._max_pause)
        except asyncio.TimeoutError:
            self._pending.pop(rid, None)
            print(f"[sched] 动作 {real} 久未发出（挂起超时 {self._max_pause:.0f}s）")
            return None

        # 第 2 段：发出后才计结果超时。
        try:
            msg = await asyncio.wait_for(env.future, timeout=timeout or self._action_timeout)
        except asyncio.TimeoutError:
            # 命中质询时打码耗时（数十秒）远超 action_timeout：只要该请求已进入打码流程，
            # 就改用更长的 challenge_timeout 再等一次，避免 token 解出来时结果已被丢弃。
            if env.challenge_attempts > 0 and not env.future.done():
                print(f"[sched] 动作 {real} 命中质询，等待打码（最多 {self._challenge_timeout:.0f}s）")
                try:
                    msg = await asyncio.wait_for(env.future, timeout=self._challenge_timeout)
                except asyncio.TimeoutError:
                    self._pending.pop(rid, None)
                    print(f"[sched] 动作 {real} 打码/续发超时")
                    return None
            else:
                self._pending.pop(rid, None)
                print(f"[sched] 动作 {real} 等结果超时")
                return None

        if msg is None:                      # 被重连放弃
            return None
        if not isinstance(msg, dict):
            print(f"[sched] 动作 {real} 收到非法结果类型: {type(msg).__name__}")
            return None
        if not msg.get("ok", False):
            print(f"[sched] 动作 {real} 被拒: {msg.get('error') or msg}")
            return None
        return msg.get("data", {})

    # ---------- 与 ws.FarmWS 鸭子兼容（tasks.py 无需改动）----------
    async def action(self, action_key, data=None):
        """等价于 FarmWS.action：发动作、等结果、返回 data 或 None。"""
        return await self.call(action_key, data)

    async def get_plots(self, force=False):
        """
        返回地块列表。目标游戏 在握手成功后**主动推送** {"type":"plots",...}，
        没有客户端主动拉取的动作（主动发会被拒），所以这里只等被动推送的缓存。
        与 FarmWS.get_plots 行为一致。
        """
        if self._plots and not force:
            return self._plots
        for _ in range(10):
            await asyncio.sleep(0.5)
            if self._plots:
                break
        return self._plots

    def pause(self):
        """手动暂停发送（连接保持）。"""
        self._manual_paused = True
        self._can_send.clear()
        if self._state == State.CONNECTED:
            self._set_state(State.PAUSED)

    def resume(self):
        """解除手动暂停。"""
        self._manual_paused = False
        if self._state == State.PAUSED:
            self._set_state(State.CONNECTED)
            self._can_send.set()
            self._schedule_relax_window()

    async def aclose(self):
        """优雅关闭：停止重连、放弃在途请求、关连接、等监督协程退出。"""
        self._closing = True
        self._closing_event.set()
        self._can_send.set()
        if self._cooldown_task and not self._cooldown_task.done():
            self._cooldown_task.cancel()
        if self._relax_task and not self._relax_task.done():
            self._relax_task.cancel()
        self._abandon_pending()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._supervisor_task:
            try:
                await asyncio.wait_for(self._supervisor_task, timeout=10)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._supervisor_task.cancel()

    # ---------- 监督：连接 / 重连生命周期 ----------
    async def _run(self):
        while not self._closing:
            close_code = None
            try:
                self._set_state(State.CONNECTING)
                creds = await self._auth_provider(self._need_reauth)
                await self._open(creds)                  # 建连 + 协议握手
                self._backoff.reset()
                self._consecutive_failures = 0
                self._need_reauth = False
                self._connected_event.set()
                if self._manual_paused:
                    self._set_state(State.PAUSED)
                    self._can_send.clear()
                    print("[sched] 重连成功，但保持用户手动暂停状态")
                else:
                    self._set_state(State.CONNECTED)
                    self._can_send.set()
                    self._schedule_relax_window()
                await self._session()                    # 跑收发，直到断开/异常
            except asyncio.CancelledError:
                raise
            except HandshakeError as e:
                self._need_reauth = True                 # 鉴权可疑 -> 下次强制重认证
                self._consecutive_failures += 1
                print(f"[sched] 握手/鉴权失败（连续 {self._consecutive_failures}）: {e}")
            except Exception as e:
                close_code = _close_code(e)
                if close_code in (1008, 4001, 4003):     # 鉴权/策略类关闭码
                    self._need_reauth = True
                self._consecutive_failures += 1
                print(f"[sched] 会话结束（连续 {self._consecutive_failures}）: {e!r}")
            finally:
                self._connected_event.clear()
                self._can_send.clear()
                await self._teardown()

            if self._closing:
                break

            # 连续失败达阈值 -> 报警 + 更大周期休眠（用户诉求 2）
            if self._consecutive_failures >= self._alert_threshold:
                text = (f"目标游戏 WS 连续失败 {self._consecutive_failures} 次"
                        f"（疑似持续鉴权失败/限速），进入 {self._long_sleep/60:.0f} 分钟长休眠")
                print(f"[sched] {text}")
                self._fire_alert(text)
                await self._sleep_interruptible(self._long_sleep)
                self._backoff.reset()
                if self._closing:
                    break

            # 服务端以"稍后再试"类关闭码踢人 -> 额外冷却
            extra = _clamp_seconds(self._detect_limit(None, close_code),
                                   default=None,
                                   label="close_code cooldown")
            if extra:
                print(f"[sched] 服务端关闭码={close_code}，额外冷却 {extra:.1f}s")
                await self._sleep_interruptible(extra)

            self._set_state(State.RECONNECTING)
            delay = self._backoff.next()
            print(f"[sched] {delay:.1f}s 后重连（force_reauth={self._need_reauth}）")
            await self._sleep_interruptible(delay)

        self._set_state(State.CLOSED)

    async def _open(self, creds):
        """建立连接并完成协议握手（{"type":"auth"} -> 等 auth_ok）。"""
        import websockets

        headers = creds.get("headers") or {}
        try:
            kwargs = dict(additional_headers=headers,
                          ping_interval=self._ping_interval,
                          open_timeout=self._handshake_timeout)
            if self._proxy:                              # 仅显式配置时经代理（HTTP CONNECT over wss）
                kwargs["proxy"] = self._proxy
            self._ws = await websockets.connect(self.url, **kwargs)
        except TypeError:                                # 兼容旧版 websockets（无 additional_headers/proxy）
            self._ws = await websockets.connect(
                self.url, extra_headers=headers,
                ping_interval=self._ping_interval)

        try:
            await self._ws.send(json.dumps(
                {"type": "auth", "token": creds["token"]}, ensure_ascii=False))
            deadline = _now() + self._handshake_timeout
            while True:
                left = deadline - _now()
                if left <= 0:
                    raise HandshakeError("等待 auth_ok 超时")
                raw = await asyncio.wait_for(self._ws.recv(), timeout=left)
                msg = self._decode(raw)
                if msg is None:
                    continue
                if not isinstance(msg, dict):
                    print(f"[sched] 握手阶段忽略非对象消息: {type(msg).__name__}")
                    continue
                if msg.get("type") == "auth_ok":
                    print("[sched] 握手成功 (auth_ok)")
                    return
                if msg.get("type") in ("auth_error", "auth_failed"):
                    raise HandshakeError(f"鉴权被拒: {msg}")
                await self._handle_inbound(msg)          # 握手期间的其它推送也别丢
        except HandshakeError:
            raise
        except (asyncio.TimeoutError, OSError) as e:
            raise HandshakeError(f"握手网络异常: {e!r}")

    async def _session(self):
        """并发跑收/发两个循环，任一结束即收尾（异常抛出去触发重连）。"""
        recv = asyncio.create_task(self._recv_loop(), name="recv")
        send = asyncio.create_task(self._send_loop(), name="send")
        done, pending = await asyncio.wait(
            {recv, send}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for t in done:
            exc = t.exception()
            if exc:
                raise exc

    async def _teardown(self):
        """清理旧会话；放弃在途请求；保留 outbound 队列。"""
        if self._cooldown_task and not self._cooldown_task.done():
            self._cooldown_task.cancel()
        self._cooldown_task = None
        self._resume_at = 0.0

        # 只保留"已取出但尚未真正发出"的 inflight，重连后优先续发。
        # 已经 send() 返回的请求不在这里重发，避免 harvest/plant 这类动作重复执行。
        retry_inflight = None
        keep_rid = None
        if self._inflight is not None and not self._inflight.sent.is_set():
            retry_inflight = self._inflight
            keep_rid = retry_inflight.rid
        dropped = self._abandon_pending(keep_rid=keep_rid)
        self._purge_outbound(dropped)
        self._inflight = retry_inflight

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        self._ws = None

    def _abandon_pending(self, keep_rid=None):
        """把所有在途请求置为 None 返回，并解除其挂起。"""
        dropped = set()
        kept = {}
        for rid, env in list(self._pending.items()):
            if keep_rid is not None and rid == keep_rid:
                kept[rid] = env
                continue
            if env.future is not None and not env.future.done():
                env.future.set_result(None)
            env.sent.set()
            dropped.add(rid)
        self._pending = kept
        return dropped

    def _purge_outbound(self, dropped_rids):
        """清理已放弃的排队请求，避免调用方已返回后仍被后台发出。"""
        if not dropped_rids or self._outbound.empty():
            return
        kept = []
        while True:
            try:
                env = self._outbound.get_nowait()
            except asyncio.QueueEmpty:
                break
            if env.rid in dropped_rids:
                continue
            kept.append(env)
        for env in kept:
            self._outbound.put_nowait(env)

    def _fire_alert(self, text):
        if self._on_alert:
            try:
                self._on_alert(text)
            except Exception as e:
                print(f"[sched] on_alert 异常: {e!r}")

    # ---------- 收 ----------
    async def _recv_loop(self):
        async for raw in self._ws:
            msg = self._decode(raw)
            if msg is None:
                continue
            await self._handle_inbound(msg)
        print("[sched] 服务端关闭了连接")          # async for 正常退出 => 对端关闭

    async def _handle_inbound(self, msg):
        # 1) 先看是不是限速信号
        cooldown = self._detect_limit(msg, None)
        if cooldown is not None:
            self._enter_cooldown(cooldown, reason="服务端限速信号")
            return
        if not isinstance(msg, dict):
            print(f"[sched] 忽略非对象入站消息: {type(msg).__name__}")
            return
        # 2) result 回包 -> 按 rid 路由给等待的 call
        if msg.get("type") == "result":
            rid = str(msg.get("rid"))
            env = self._pending.get(rid)
            if env is not None and self._is_challenge_required(msg):
                asyncio.create_task(
                    self._handle_challenge(env, msg),
                    name=f"challenge-{rid}",
                )
                return
            env = self._pending.pop(rid, None)
            if env is not None and env.future is not None and not env.future.done():
                env.future.set_result(msg)
            return
        # 3) 服务器主动推的地块
        if msg.get("type") == "plots":
            self._plots = msg.get("plots", []) or []
            return
        # 4) 其它消息交业务回调
        if self._on_message:
            try:
                await self._on_message(msg)
            except Exception as e:
                print(f"[sched] on_message 处理异常: {e!r}")

    def _is_challenge_required(self, msg):
        # 真实 目标游戏 用 code（如 NEED_HUMAN_VERIFICATION）；CTF/测试用 error。两者都认。
        if not (isinstance(msg, dict) and self._challenge_solver is not None):
            return False
        return self._challenge_code in (msg.get("code"), msg.get("error"))

    async def _handle_challenge(self, env, challenge):
        if env.challenge_attempts >= 1:
            rid = env.rid
            if self._pending.pop(rid, None) is env and not env.future.done():
                env.future.set_result(challenge)
            return
        env.challenge_attempts += 1

        previous_state = self._state
        self._set_state(State.CHALLENGE)
        self._can_send.clear()
        try:
            token = await self._challenge_solver(challenge)
            if not token:
                raise RuntimeError("empty challenge token")
            if self._challenge_inject:        # CTF/测试模式：把 token 注入原 payload 重发
                self._inject_challenge_token(env.payload, token)
            # 目标游戏 模式(challenge_inject=False)：token 已用于带外 human-verify 解锁，原样重发
            await self._ws.send(self._encode(env.payload))
            self._mark_sent(env)
            print(f"[sched] 人机验证已处理，重发 rid={env.rid}")
        except Exception as e:
            rid = env.rid
            print(f"[sched] challenge 处理失败 rid={rid}: {e!r}")
            if self._pending.pop(rid, None) is env and not env.future.done():
                env.future.set_result(None)
        finally:
            if self._closing:
                return
            if previous_state == State.PAUSED or self._manual_paused:
                self._set_state(State.PAUSED)
                self._can_send.clear()
            elif self._state == State.CHALLENGE and self._ws is not None:
                self._set_state(State.CONNECTED)
                self._can_send.set()

    def _inject_challenge_token(self, payload, token):
        if not isinstance(payload, dict):
            raise TypeError("challenge payload must be a dict")
        data = payload.setdefault("data", {})
        if not isinstance(data, dict):
            raise TypeError("challenge payload data must be a dict")
        data[self._challenge_token_field] = token

    # ---------- 发 ----------
    async def _send_loop(self):
        while True:
            await self._can_send.wait()              # 暂停点：PAUSED/冷却时阻塞
            if self._closing:
                return
            if self._inflight is None:
                try:
                    self._inflight = await asyncio.wait_for(
                        self._outbound.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
            await self._pace()                       # 节流
            if not self._can_send.is_set():          # pace 期间可能进入冷却
                continue                             # 保留 inflight，回到暂停等待
            try:
                self._maybe_inject_pass(self._inflight.payload)   # 发送前主动带上人机通行证
                await self._ws.send(self._encode(self._inflight.payload))
                self._mark_sent(self._inflight)      # 通知 call：已真正发出
                self._inflight = None
            except Exception as e:
                print(f"[sched] 发送失败，触发重连: {e!r}")
                raise

    def _maybe_inject_pass(self, payload):
        """对 pass_actions 里的动作，发送前把 pass_provider() 的通行证塞进 data。

        与前端一致：harvest/plant 等 WS 动作每次发送都带 data.human_pass（持有有效通行证时），
        这样在 900s 时效内首发即放行，免去「先失败再重发」的一来一回。无通行证则不动 payload。
        """
        if not (self._pass_provider and isinstance(payload, dict)):
            return
        if payload.get("action") not in self._pass_actions:
            return
        try:
            token = self._pass_provider()
        except Exception as e:
            print(f"[sched] 取人机通行证异常: {e!r}")
            return
        if not token:
            return
        data = payload.setdefault("data", {})
        if isinstance(data, dict):
            data[self._challenge_token_field] = token

    def _mark_sent(self, env):
        """
        发送确认扩展点。

        当前确认语义是 ws.send() 已返回，代表消息已交给 WebSocket 层；如果后续要接入
        业务 ACK / 幂等重试，可通过 on_send_confirm 或 result 路由继续扩展。
        """
        env.sent.set()
        if self._on_send_confirm:
            try:
                self._on_send_confirm(env.payload, env.rid)
            except Exception as e:
                print(f"[sched] on_send_confirm 异常: {e!r}")

    async def _pace(self):
        interval = max(self._send_interval, self._dynamic_interval)
        if interval > 0:
            await asyncio.sleep(interval)

    # ---------- 冷却（限速信号 -> 退避 -> 恢复 + 降速窗口）----------
    def _enter_cooldown(self, seconds, *, reason=""):
        seconds = _clamp_seconds(seconds, default=None, label="cooldown")
        if seconds is None:
            return
        self._set_state(State.COOLING_DOWN)
        self._can_send.clear()
        self._dynamic_interval = max(self._dynamic_interval, min(seconds, 5.0))
        self._resume_at = max(self._resume_at, _now() + seconds)
        print(f"[sched] 进入冷却 {seconds:.1f}s（{reason}）")
        if self._cooldown_task is None or self._cooldown_task.done():
            self._cooldown_task = asyncio.create_task(self._cooldown_loop())

    async def _cooldown_loop(self):
        while not self._closing:
            wait = self._resume_at - _now()
            if wait <= 0:
                break
            await asyncio.sleep(wait)                 # _resume_at 可能被后续信号延长
        if self._state == State.COOLING_DOWN and self._ws is not None:
            if self._manual_paused:
                self._set_state(State.PAUSED)
                self._can_send.clear()
                print("[sched] 冷却结束，但保持用户手动暂停状态")
            else:
                self._set_state(State.CONNECTED)
                self._can_send.set()
                print("[sched] 冷却结束，恢复发送（降速生效中）")
                self._schedule_relax_window()

    def _schedule_relax_window(self, calm_seconds=30.0):
        """连接恢复后安排降速释放；已有释放任务时不重复创建。"""
        if self._dynamic_interval <= 0 or self._closing:
            return
        if self._relax_task and not self._relax_task.done():
            return
        self._relax_task = asyncio.create_task(self._relax_after(calm_seconds))

    async def _relax_after(self, calm_seconds):
        await asyncio.sleep(calm_seconds)
        if (not self._closing and self._state == State.CONNECTED
                and not self._manual_paused and self._dynamic_interval > 0):
            self._dynamic_interval = 0.0
            print("[sched] 平稳期已过，解除降速")

    # ---------- 杂项 ----------
    async def _sleep_interruptible(self, delay):
        try:
            await asyncio.wait_for(self._closing_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    def _encode(self, payload):
        if isinstance(payload, (str, bytes)):
            return payload
        return json.dumps(payload, ensure_ascii=False)

    def _decode(self, raw):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError, TypeError):
            return None


def _close_code(exc):
    """尽量从 websockets 的 ConnectionClosed 异常取出关闭码（新旧版本兼容）。

    新版 websockets(13.1+) 废弃了 exc.code，改用 exc.rcvd.code。其异常对象一定带
    rcvd 属性（物理断线无 Close 帧时 rcvd 为 None，即没有关闭码）。所以只要 rcvd
    属性存在，就只走非废弃接口、绝不读 exc.code，从根上避免 DeprecationWarning。
    """
    if hasattr(exc, "rcvd"):                     # 新版异常
        rcvd = exc.rcvd                          # 对端发来的 Close 帧（可能为 None）
        if rcvd is not None:
            return getattr(rcvd, "code", None)
        return getattr(exc, "close_code", None)  # 非废弃；abrupt close 时通常为 None
    return getattr(exc, "code", None)            # 旧版兜底（仅旧版才有 .code）


# =========================================================================
# 示例：如何接现有 auth.py / config.py / endpoints.py（仅演示，按需启用）
# =========================================================================
async def example_main():
    import config
    import auth
    from endpoints import WS_ACTIONS

    async def auth_provider(force):
        # force=True 时强制重新登录换 JWT（401/被踢后）
        await auth.refresh_auth(force=force)
        cookie_str = "; ".join(f"{k}={v}" for k, v in (auth._cf_cookies or {}).items())
        headers = {
            "Origin": config.MINI_APP_URL.rstrip("/"),
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/120.0.0.0 Safari/537.36"),
        }
        if cookie_str:
            headers["Cookie"] = cookie_str
        return {"token": auth.get_token(), "headers": headers}

    sm = WSStateMachine(
        config.WS_URL,
        auth_provider=auth_provider,
        action_map=WS_ACTIONS,
        action_timeout=config.WS_ACTION_TIMEOUT,
        on_alert=lambda text: print(f"[ALERT] {text}"),   # scheduler 里换成推微信
    )
    sm.start()
    if not await sm.wait_connected(timeout=60):
        print("首连失败")
        await sm.aclose()
        return

    # 鸭子兼容：和 FarmWS 一样用 .action / .get_plots
    plots = await sm.get_plots(force=True)
    print(f"[demo] 地块数: {len(plots)}")
    data = await sm.action("get_inventory")
    print(f"[demo] inventory: {str(data)[:120]}")

    await sm.aclose()


if __name__ == "__main__":
    asyncio.run(example_main())
