"""
farm_sell_steal.py — 卖光果实背包变现 + 偷一轮好友菜。

- 卖菜：WS sell_fruits(crop_id,count) 把果实背包全变现（不在 WS_PASS_ACTIONS，低风险）。
- 偷菜：REST visit + steal-crops（需人机通行证，会过 Turnstile）。
用户已明确授权「多搞点钱 + 偷个菜」。只卖果实(harvest 产物)，不动种子/道具。
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
from tasks import _as_list, _get
from ws_session import open_state_machine


async def run(client):
    async with AsyncSession(impersonate="chrome120") as session:
        await auth.refresh_auth(client)
        print("[run] 鉴权成功（已换取 JWT）")
        headers = auth._headers

        # ===== 1) 卖光果实背包 =====
        async with open_state_machine(client) as (sm, connected):
            if not connected:
                print("[run] WS 首连失败，跳过卖菜")
            else:
                fruit = await sm.action("get_fruit_inventory")
                est = 0
                sold = 0
                for f in _as_list(fruit, "data"):
                    cid = _get(f, "crop_id")
                    cnt = _get(f, "count", default=0)
                    name = _get(f, "crop_name", default=cid)
                    unit = _get(f, "fruit_coins", default=0)
                    if not cid or not cnt:
                        continue
                    r = await sm.action("sell_fruits", {"crop_id": cid, "count": cnt})
                    ok = r is not None
                    print(f"[sell] {name} x{cnt} (单价~{unit}) -> "
                          f"{'OK' if ok else '失败'}: {json.dumps(r, ensure_ascii=False)[:240]}")
                    if ok:
                        sold += 1
                        est += cnt * unit
                    await asyncio.sleep(1.5)
                print(f"[sell] 共卖出 {sold} 种果实，预计入账约 {est} coins（以服务端结算为准）")

        # ===== 2) 偷一轮好友菜（REST） =====
        friends = await api.safe_call(api.get_friends, session, [headers], 1)
        flist = _as_list(friends, "data", "friends")
        print(f"[steal] 好友 {len(flist)} 个，开始拜访+偷菜")
        stolen = 0
        for fr in flist:
            key = _get(fr, "player_key", "target_key", "key", "id")
            if not key:
                continue
            await api.safe_call(api.visit_friend, session, [headers], key)
            await asyncio.sleep(1.2)
            r = await api.safe_call(api.steal_crops, session, [headers], key)
            if r is not None:
                stolen += 1
                print(f"[steal] 偷 {key} OK: {json.dumps(r, ensure_ascii=False)[:200]}")
            await asyncio.sleep(2)
        print(f"[steal] 偷菜成功 {stolen} 家")

        # ===== 3) 查最新余额 =====
        try:
            await auth.refresh_auth(client, force=True)
            print("[done] 最新余额见上方 [auth] 登录成功那行的 coins")
        except Exception as e:
            print(f"[done] 余额刷新失败（不影响前面动作）: {e}")


async def main():
    config.validate()
    client, tmp = await _build_client()
    try:
        await run(client)
    except Exception as e:
        print(f"[err] 执行异常（已捕获）: {e}")
    finally:
        await client.disconnect()
        if tmp:
            tmp.unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
