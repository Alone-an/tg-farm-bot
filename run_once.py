"""
run_once.py — 云端 / 无人值守入口：跑一轮就退出（给 GitHub Actions 用）。

与 main.py 的区别：
  - main.py 是本地常驻：交互式登录 + 内置 scheduler 死循环每天 08:00 跑。
  - run_once.py 不交互、不常驻：连接 → 跑一轮任务 → 退出。定时交给 Actions 的 cron。

鉴权方式参考 tg_auto：
  - Telegram 登录态以 base64 存在 TG_SESSION 环境变量（Secret），
    运行时解码成临时 .session，连接后直接用，绝不弹交互登录。
  - 没有 TG_SESSION 时回退到本地持久 session 文件（首次会交互登录），方便本地调试。
  - 云端 FARM_CLOUD=1：auth 里会跳过 DrissionPage 过 CF（无浏览器），
    依赖 curl_cffi 的 chrome120 指纹直连。
"""
import asyncio
import base64
import os
import sys
import tempfile
import threading
from pathlib import Path

# Windows 控制台默认 GBK，日志里的 ✓/× 等 Unicode 会触发 UnicodeEncodeError 中断流程；
# 入口处把 stdout/stderr 重配为 UTF-8（errors=replace 兜底），云端本就是 UTF-8 不受影响。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from curl_cffi.requests import AsyncSession
from telethon import TelegramClient

import auth
import config
from friends import run_friend_visits
from tasks import run_all_tasks
from ws_session import open_state_machine


def _decode_session_to_file(session_b64: str) -> Path:
    """把 base64 的 .session 解码到临时文件，返回路径。"""
    raw = base64.b64decode(session_b64)
    tmp = tempfile.NamedTemporaryFile(prefix="farm_", suffix=".session", delete=False)
    try:
        tmp.write(raw)
        tmp.close()
        print(f"[run_once] 已从 TG_SESSION 解码 session（{len(raw)} 字节）")
        return Path(tmp.name)
    except Exception:
        tmp.close()
        Path(tmp.name).unlink(missing_ok=True)
        raise


async def _build_client():
    """
    构建 TelegramClient。
    返回 (client, tmp_path)：tmp_path 为临时 session 文件（用完删除），本地模式为 None。
    """
    api_id = int(config.API_ID)
    api_hash = config.API_HASH
    session_b64 = (config.TG_SESSION or "").strip()

    if session_b64:
        path = _decode_session_to_file(session_b64)
        client = TelegramClient(str(path), api_id, api_hash, proxy=config.PROXY)
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            path.unlink(missing_ok=True)
            raise RuntimeError(
                "TG_SESSION 无效或未授权。请在本地用 login.py 重新生成 base64 session 后更新 Secret。"
            )
        print("[run_once] 用 TG_SESSION 连接成功（非交互）")
        return client, path

    # 本地回退：持久 session 文件，首次会交互登录
    client = TelegramClient("目标游戏_session", api_id, api_hash, proxy=config.PROXY)
    await client.start()
    print("[run_once] 用本地 目标游戏_session 登录成功")
    return client, None


async def run_cycle(client):
    """跑一轮：鉴权 → 建 WS 状态机 → 任务 → 关 WS → 好友互动。

    WS 与每日调度共享同一套环境伪装 / 退避 / 重连重认证逻辑（见 ws_session）。
    好友互动单独包一层超时：某个好友 REST 卡住，不至于拖垮整轮、阻塞退出。
    """
    async with AsyncSession(impersonate="chrome120", proxies=config.proxies()) as session:
        await auth.refresh_auth(client)
        print("[run_once] 鉴权成功（已换取 JWT）")

        headers = auth._headers
        async with open_state_machine(client) as (sm, connected):
            if not connected:
                print("[run_once] WS 首连/握手失败，跳过本轮任务")
                return
            # sm 与原 FarmWS 鸭子兼容（.action / .get_plots），tasks.py 无需改动
            headers = await run_all_tasks(session, headers, sm)

        # 好友互动是纯 REST，不需要 WS；单独超时兜底，卡住就跳过未完成部分继续收尾
        try:
            await asyncio.wait_for(run_friend_visits(session, headers),
                                   timeout=config.FRIEND_VISITS_TIMEOUT)
        except asyncio.TimeoutError:
            print(f"[run_once] 好友互动超时（>{config.FRIEND_VISITS_TIMEOUT}s），"
                  "跳过未完成部分，继续收尾退出")


async def main():
    config.validate()
    client, tmp_path = await _build_client()
    try:
        # 整轮总预算：任何子环节（任务 / 好友 / WS 关闭）卡死都在此被 wait_for 打断
        await asyncio.wait_for(run_cycle(client), timeout=config.RUN_ONCE_TIMEOUT)
    except asyncio.TimeoutError:
        print(f"[run_once] 本轮整体超时（>{config.RUN_ONCE_TIMEOUT}s），强制收尾退出")
    except Exception as e:
        print(f"[run_once] 执行异常（已捕获）: {e}")
    finally:
        # disconnect 本身也可能卡网络，再包一层超时；失败就忽略，反正马上强制退出
        try:
            res = client.disconnect()
            if res is not None:
                await asyncio.wait_for(res, timeout=config.DISCONNECT_TIMEOUT)
        except Exception as e:
            print(f"[run_once] 断开 Telegram 超时/异常（忽略）: {e}")
        if tmp_path:
            tmp_path.unlink(missing_ok=True)
            print("[run_once] 已清理临时 session 文件")


def _arm_watchdog(seconds, code=0):
    """硬退出看门狗（守护线程）。

    asyncio.wait_for 在超时取消时会 await 取消完成，若卡点恰在 async with 的
    __aexit__（如 WS 优雅关闭）里，取消本身也可能挂起，wait_for 随之卡住、
    进程不退；加之 curl_cffi / telethon / 浏览器子进程可能留存非守护线程，
    asyncio.run() 返回后进程仍可能不终止。看门狗用独立线程到点 os._exit，
    无论主线程卡在哪都能强杀，确保云端 Actions 不空耗。
    """
    def _kill():
        print(f"[run_once] ⏰ 看门狗 {seconds}s 到，强制退出（防 teardown 卡死）",
              flush=True)
        os._exit(code)

    t = threading.Timer(seconds, _kill)
    t.daemon = True
    t.start()
    return t


if __name__ == "__main__":
    # 看门狗截止 = 整轮预算 + 60s 缓冲：正常情况下 main 早已优雅退出，
    # 只有真卡死（含取消时 __aexit__ 挂起）才由看门狗兜底强杀。
    _arm_watchdog(config.RUN_ONCE_TIMEOUT + 60, code=0)
    exit_code = 0
    try:
        asyncio.run(main())
    except BaseException as e:  # 含 KeyboardInterrupt / 配置错误等
        print(f"[run_once] 致命错误，退出: {e}", flush=True)
        exit_code = 1
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        # 一次性入口：日常动作已在 main 内完成并落日志，这里立即终止，
        # 不被残留线程 / 子进程拖住（os._exit 跳过 atexit，故先手动 flush）。
        os._exit(exit_code)
