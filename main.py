"""
main.py — 程序入口（REST + WebSocket 架构）。

流程：
  1. 校验配置 → 登录 Telegram
  2. refresh_auth：过 CF → 取 initData → POST /api/auth/login 换 JWT
  3. 建立 WebSocket（auth 握手）
  4. 立即跑一轮 run_all_tasks + run_friend_visits
  5. 进入 scheduler 定时循环

本脚本本地运行：DrissionPage 需要本地 Chromium。
"""
import asyncio
import sys

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
from scheduler import run_scheduler
from tasks import run_all_tasks
from ws_session import open_state_machine


async def run_once(session, client):
    """建立 WS 状态机并跑一轮任务 + 好友。

    与每日调度共享同一套环境伪装 / 退避 / 重连重认证逻辑（见 ws_session）：
    跑任务期间被限速会自动冷却、断线会自动重连重认证，业务调用无缝挂起。
    """
    headers = auth._headers
    async with open_state_machine(client) as (sm, connected):
        if not connected:
            print("[main] WS 首连/握手失败，跳过本轮任务")
            return
        # sm 与原 FarmWS 鸭子兼容（.action / .get_plots），tasks.py 无需改动
        headers = await run_all_tasks(session, headers, sm)
    # 好友互动是纯 REST，不需要 WS，放在 WS 关闭后
    await run_friend_visits(session, headers)


async def main():
    config.validate()

    client = TelegramClient("目标游戏_session", int(config.API_ID), config.API_HASH, proxy=config.PROXY)
    await client.start()
    print("Telegram 登录成功")

    await auth.refresh_auth(client)
    print("鉴权成功（已换取 JWT）")

    try:
        async with AsyncSession(impersonate="chrome120", proxies=config.proxies()) as session:
            await run_once(session, client)
    except Exception as e:
        print(f"[main] 首轮执行异常（已捕获）: {e}")

    try:
        await run_scheduler(client)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n已手动停止。")
