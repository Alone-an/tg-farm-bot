"""
farm_status.py — 纯只读农场盘点（只查不动）。

查询：地块种了啥(get_plots) / 果实背包能卖啥(get_fruit_inventory) /
种子背包(get_inventory) / 道具(get_tool_inventory) / 角色等级金币(profile) /
等级表(levels)。

这些 get_* 动作不在 WS_PASS_ACTIONS 风控名单里，纯只读，不触发反作弊，
也不需要过 Turnstile（不会弹 Edge）。安全盘点用。
"""
import asyncio
import json
import sys

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from curl_cffi.requests import AsyncSession

import api
import auth
import config
from run_once import _build_client
from ws_session import open_state_machine


def _j(label, data):
    print(f"\n===== {label} =====")
    try:
        print(json.dumps(data, ensure_ascii=False, indent=2)[:6000])
    except Exception:
        print(repr(data)[:3000])


async def _ws_q(sm, name):
    try:
        return await sm.action(name)
    except Exception as e:
        return {"_error": f"{name} 失败: {e}"}


async def run_query(client):
    async with AsyncSession(impersonate="chrome120") as session:
        await auth.refresh_auth(client)
        print("[status] 鉴权成功（已换取 JWT）")
        headers = auth._headers

        # ---- REST 只读 ----
        prof = await api.safe_call(api.get_profile, session, [headers])
        _j("角色 profile（等级/金币/经验）", prof)
        try:
            levels = await api._request(session, headers, "GET", api._path("levels"))
            _j("等级表 levels（升级所需经验/解锁）", levels)
        except Exception as e:
            print(f"[status] levels 查询失败: {e}")

        # ---- WS 只读 ----
        async with open_state_machine(client) as (sm, connected):
            if not connected:
                print("[status] WS 首连/握手失败")
                return
            _j("地块 plots（种了啥菜/成熟没）", await sm.get_plots(force=True))
            _j("果实背包 get_fruit_inventory（这些能卖钱）", await _ws_q(sm, "get_fruit_inventory"))
            _j("种子背包 get_inventory（能种啥）", await _ws_q(sm, "get_inventory"))
            _j("道具背包 get_tool_inventory", await _ws_q(sm, "get_tool_inventory"))


async def main():
    config.validate()
    client, tmp_path = await _build_client()
    try:
        await run_query(client)
    except Exception as e:
        print(f"[status] 执行异常（已捕获）: {e}")
    finally:
        await client.disconnect()
        if tmp_path:
            tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
