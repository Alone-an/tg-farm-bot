"""
scheduler.py — 纯 asyncio 定时调度（REST + WebSocket 状态机）。

每天 08:00：refresh_auth → 等 WS 状态机就绪 → run_all_tasks → run_friend_visits
周一 08:30：周常任务领取检查。
底层 WebSocket 统一交给 WSStateMachine 管理：发送队列、背压、冷却、重连、重认证。
"""
import asyncio
import datetime

import auth
from friends import run_friend_visits
from tasks import _claim_completed_tasks, run_all_tasks
from ws_session import make_state_machine
from ws_state_machine import State


def _seconds_until(hour, minute):
    now = datetime.datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


def build_state_machine(client):
    """为调度器创建状态机。

    环境伪装(Origin/UA/Cookie) + auth_provider(重连重认证) + 退避(Backoff)
    统一走 ws_session.make_state_machine，与首轮 run_once 共享同一套配置；
    调度器只负责其生命周期管理（持久复用、监控、熔断）。
    """
    return make_state_machine(
        client,
        on_alert=lambda text: print(f"[scheduler] WS 告警: {text}"),
    )


class Scheduler:
    """
    生产调度器。

    Scheduler 不再直接创建/关闭旧连接对象；所有 WS 发送都进入 WSStateMachine。
    状态机队列满时 submit/call 会自然阻塞，让业务层自动获得背压。
    """

    INACTIVE_STATES = {
        State.DISCONNECTED,
        State.CONNECTING,
        State.COOLING_DOWN,
        State.RECONNECTING,
        State.CLOSED,
    }

    def __init__(self, client, sm):
        if sm is None:
            raise ValueError("Scheduler 初始化必须传入 WSStateMachine 实例")
        self.client = client
        self.sm = sm

        self._stop_event = asyncio.Event()
        self._monitor_task = None
        self._ws_inactive_since = None
        self._circuit_open = False

        # 状态机状态回调：让调度层实时感知冷却/重连。
        self.sm.on_state_change = self._on_ws_state_change

    # ---------- 状态感知 ----------
    def _on_ws_state_change(self, old, new):
        now = datetime.datetime.now()
        if new in self.INACTIVE_STATES:
            if self._ws_inactive_since is None:
                self._ws_inactive_since = now
            if new == State.COOLING_DOWN:
                print("[scheduler] 感知到底层 WS 冷却中，业务发送将自动背压")
            elif new == State.RECONNECTING:
                print("[scheduler] 感知到底层 WS 重连中，业务发送将自动挂起")
        else:
            self._ws_inactive_since = None
            if old in self.INACTIVE_STATES:
                print("[scheduler] 底层 WS 已恢复活跃")

    def _is_ws_inactive(self):
        return self.sm.state in self.INACTIVE_STATES

    async def _sleep_or_stop(self, seconds):
        """可被熔断/退出唤醒的 sleep；返回 True 表示应停止。"""
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
            return True
        except asyncio.TimeoutError:
            return False

    # ---------- WS 发送接口 ----------
    async def submit(self, payload):
        """
        调度器对外的原始 WS 发送入口。

        任何新增的直接发包逻辑都必须走这里，最终统一进入状态机队列。
        队列满、冷却或重连时 await 会自然挂起，形成业务背压。
        """
        await self.sm.submit(payload)

    async def _ensure_ws_started(self):
        if self.sm.state == State.CLOSED:
            raise RuntimeError("WS 状态机已关闭，调度器不能继续发起业务")
        if getattr(self.sm, "_supervisor_task", None) is None:
            self.sm.start()
            print("[scheduler] WS 状态机已启动")

    async def _ensure_ws_ready(self):
        await self._ensure_ws_started()
        if self.sm.state == State.CONNECTED:
            return True
        ok = await self.sm.wait_connected(timeout=90)
        if not ok:
            print("[scheduler] WS 首次/恢复连接超时，本轮任务跳过")
        return ok

    # ---------- 业务流程 ----------
    async def run_daily(self, session):
        try:
            headers = await auth.refresh_auth(self.client)
            if not await self._ensure_ws_ready():
                return

            # run_all_tasks 内部的 ws.action/get_plots 已由 WSStateMachine 接管。
            headers = await run_all_tasks(session, headers, self.sm)
            if self._stop_event.is_set():
                return

            # 好友互动仍是 REST，不需要 WS，但复用最新 headers。
            await run_friend_visits(session, headers)
        except Exception as e:
            print(f"[scheduler] 每日流程异常（已捕获）: {e}")

    async def run_weekly(self, session):
        try:
            hdrs = [await auth.refresh_auth(self.client)]
            n = await _claim_completed_tasks(session, hdrs, "周常任务")
            print(f"[scheduler] 周常检查完成，领取 {n} 个")
        except Exception as e:
            print(f"[scheduler] 周常流程异常（已捕获）: {e}")

    # ---------- 大周期熔断 ----------
    async def monitor_loop(self):
        """
        每 5 秒检查底层状态机。

        当连续重连退避次数大于 10，且状态机仍处于非活跃状态时，触发全盘熔断：
        停止调度循环，并关闭 WS 状态机，让所有挂起的业务调用尽快返回。
        """
        while not self._stop_event.is_set():
            await asyncio.sleep(5)

            attempts = getattr(getattr(self.sm, "_backoff", None), "_attempt", 0)
            inactive = self._is_ws_inactive()
            if inactive and self._ws_inactive_since is None:
                self._ws_inactive_since = datetime.datetime.now()
            elif not inactive:
                self._ws_inactive_since = None

            if attempts > 10 and inactive:
                self._circuit_open = True
                since = self._ws_inactive_since or datetime.datetime.now()
                duration = (datetime.datetime.now() - since).total_seconds()
                print(
                    "[scheduler] 熔断触发："
                    f"连续重连 attempts={attempts}，"
                    f"非活跃 {duration:.0f}s，停止所有业务循环"
                )
                self._stop_event.set()
                await self.sm.aclose()
                break

    # ---------- 主循环 ----------
    async def run(self):
        import config
        from curl_cffi.requests import AsyncSession

        await self._ensure_ws_started()
        self._monitor_task = asyncio.create_task(
            self.monitor_loop(), name="scheduler-monitor"
        )

        try:
            async with AsyncSession(impersonate="chrome120", proxies=config.proxies()) as session:
                while not self._stop_event.is_set():
                    try:
                        wait = _seconds_until(8, 0)
                        print(
                            f"[scheduler] 距下次 08:00 还有 "
                            f"{wait/3600:.2f} 小时，等待中…"
                        )
                        if await self._sleep_or_stop(wait):
                            break

                        print(f"[scheduler] {datetime.datetime.now()} 触发每日任务")
                        await self.run_daily(session)
                        if self._stop_event.is_set():
                            break

                        if datetime.datetime.now().weekday() == 0:
                            wait2 = _seconds_until(8, 30)
                            if wait2 <= 35 * 60:
                                print(
                                    f"[scheduler] 周一，"
                                    f"{wait2/60:.1f} 分钟后周常检查"
                                )
                                if await self._sleep_or_stop(wait2):
                                    break
                                await self.run_weekly(session)

                        if await self._sleep_or_stop(60):
                            break
                    except Exception as e:
                        print(f"[scheduler] 主循环异常（已捕获，继续）: {e}")
                        if await self._sleep_or_stop(60):
                            break
        finally:
            self._stop_event.set()
            if self._monitor_task and not self._monitor_task.done():
                self._monitor_task.cancel()
                await asyncio.gather(self._monitor_task, return_exceptions=True)
            await self.sm.aclose()
            if self._circuit_open:
                print("[scheduler] 已因 WS 熔断退出")


async def run_scheduler(client):
    sm = build_state_machine(client)
    scheduler = Scheduler(client, sm)
    await scheduler.run()
