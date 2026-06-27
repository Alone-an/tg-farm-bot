"""
tasks.py — 日常任务主流程。✅ 全部按源码确认的真实接口。

流程：
  邮件领取 → 领已完成任务 → harvest_all 收获 → 空地补种 →
  自家除草除虫(clean-marks) → 二次领任务
（放草放虫是对好友的操作，在 friends.py 里做）
"""
import asyncio
import random

import api
import config


def _as_list(data, *keys):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
        d = data.get("data")
        if isinstance(d, list):
            return d
        if isinstance(d, dict):
            for k in keys:
                v = d.get(k)
                if isinstance(v, list):
                    return v
    return []


def _get(item, *keys, default=None):
    if not isinstance(item, dict):
        return default
    for k in keys:
        if k in item and item[k] is not None:
            return item[k]
    return default


def _is(item, value, *keys):
    s = _get(item, *keys)
    return isinstance(s, str) and s.lower() == value.lower()


async def _delay():
    await asyncio.sleep(random.uniform(config.DELAY_MIN, config.DELAY_MAX))


def _reward_of(task):
    if isinstance(task, dict):
        rewards = task.get("rewards")
        if isinstance(rewards, list) and rewards:
            return " 奖励:" + ",".join(
                f"{_get(r,'reward_name','reward_type',default='?')}x{_get(r,'amount',default='?')}"
                for r in rewards)
    return ""


async def _pick_seed_crop(ws):
    """从种子背包(get_inventory)挑 grade 最高且 count>0 的 crop_id 用于补种。

    优先种高级作物（crop_grade 越大越高级，收益/经验更高）。
    无 grade 字段的按 0 处理；背包为空返回 (None, None)。
    """
    data = await ws.action("get_inventory")
    best, best_grade = None, None
    for s in _as_list(data, "data"):
        if not (_get(s, "count", default=0) and _get(s, "crop_id")):
            continue
        grade = _get(s, "crop_grade", "grade", default=0) or 0
        if best is None or grade > best_grade:
            best, best_grade = s, grade
    if best is None:
        return None, None
    return _get(best, "crop_id"), _get(best, "crop_name", default="")


async def _sell_all_fruits(ws):
    """把果实背包(get_fruit_inventory)里所有 count>0 的果实 sell_fruits 变现。

    sell_fruits 不在 WS_PASS_ACTIONS 风控名单，无需人机通行证。
    返回 (卖出种类数, 累计入账 coins_gain)。卖失败(None)不计数；
    回包缺 coins_gain 字段按 0 计。
    """
    fruit = await ws.action("get_fruit_inventory")
    sold, gain = 0, 0
    for f in _as_list(fruit, "data"):
        cid = _get(f, "crop_id")
        cnt = _get(f, "count", default=0)
        if not cid or not cnt:
            continue
        name = _get(f, "crop_name", default=cid)
        resp = await ws.action("sell_fruits", {"crop_id": cid, "count": cnt})
        if resp is not None:
            sold += 1
            g = _get(resp, "coins_gain", default=0) or 0
            gain += g
            print(f"[tasks] 卖出 {name} x{cnt} (+{g} coins)")
        await _delay()
    return sold, gain


async def _unlock_plots(ws, max_attempts=20):
    """连续 unlock_plot 解锁新地块，直到失败(None)或达 max_attempts。

    unlock_plot 在 WS 但不在风控名单。等级 + 金币足够才成功；
    服务端拒绝（金币不足 / 无更多可解锁）时 ws.action 返回 None，即停止。
    返回成功解锁的地块数。
    """
    unlocked = 0
    for _ in range(max_attempts):
        resp = await ws.action("unlock_plot")
        if resp is None:
            break
        unlocked += 1
        print(f"[tasks] 解锁新地块 #{unlocked}")
        await _delay()
    return unlocked


async def _claim_completed_tasks(session, hdrs, label="任务"):
    tasks_data = await api.safe_call(api.get_tasks, session, hdrs)
    if tasks_data is None:
        return 0
    count = 0
    for t in _as_list(tasks_data, "data", "tasks"):
        completed = _get(t, "completed", default=False) is True or \
            _is(t, "completed", "status") or _is(t, "claimable", "status")
        claimed = _get(t, "claimed", default=False) is True
        if completed and not claimed:
            code = _get(t, "task_code", "id")
            if code is None:
                continue
            resp = await api.safe_call(api.claim_task, session, hdrs, code)
            if resp is not None:
                count += 1
                print(f"[tasks] 领取{label} {code}{_reward_of(t)}")
            await _delay()
    return count


async def run_all_tasks(session, headers, ws):
    hdrs = [headers]
    print("\n========== 开始执行日常任务 ==========")

    # 1) 邮件：先 read 再 claim 未领取的
    mailbox = await api.safe_call(api.get_mailbox, session, hdrs)
    mc = 0
    for m in _as_list(mailbox, "data", "mails"):
        if _get(m, "claimed", default=False) is True:
            continue
        mid = _get(m, "id", "mail_id")
        if mid is None:
            continue
        await api.safe_call(api.claim_mail, session, hdrs, mid)
        mc += 1
        print(f"[tasks] 领取邮件 {mid}")
        await _delay()
    print(f"[tasks] 步骤1 邮箱：领取 {mc} 封")

    # 2) 领已完成任务
    n = await _claim_completed_tasks(session, hdrs, "已完成任务")
    print(f"[tasks] 步骤2 任务：领取 {n} 个")

    # 3) 逐块收获成熟作物
   
    #    改用游戏自带的单块 harvest{plot_index}、逐块收 + 块间随机延迟，模拟真人逐块点击。
    #    成熟与否交服务器判定：空地跳过，有作物的逐块尝试，未成熟会被拒（返回 None 不计数）。
    plots = await ws.get_plots(force=True)
    harvested = 0
    for p in plots:
        crop = _get(p, "crop_id", default="")
        if crop in ("", None, "000000000000000000000000"):
            continue                       # 空地无作物可收，跳过
        idx = _get(p, "plot_index", "index", "id")
        if idx is None:
            continue
        rh = await ws.action("harvest", {"plot_index": idx})
        if rh is not None:
            harvested += 1
            print(f"[tasks] 收获 plot_index={idx}")
        await _delay()                     # 块间人类节奏延迟
    print(f"[tasks] 步骤3 收获：逐块 harvest 成功 {harvested} 块")
    await _delay()

    # 4) 卖果实变现（sell_fruits 不在风控名单，把收获的果子全部变现）
    sold, gain = await _sell_all_fruits(ws)
    print(f"[tasks] 步骤4 卖果实：卖出 {sold} 种，入账 {gain} coins")
    await _delay()

    # 5) 扩地：尝试解锁新地块（等级 / 金币够才成功，连续失败即止）
    newp = await _unlock_plots(ws)
    print(f"[tasks] 步骤5 扩地：新解锁 {newp} 块")
    await _delay()

    # 6) 空地补种（含新解锁的地块；优先种 grade 最高的高级作物）
    plots = await ws.get_plots(force=True)
    crop_id, crop_name = await _pick_seed_crop(ws)
    planted = 0
    if crop_id is None:
        print("[tasks] 步骤6 种植：种子背包为空，跳过")
    else:
        for p in plots:
            empty = _is(p, "empty", "stage") or _is(p, "idle", "stage") or \
                _get(p, "crop_id", default="") in ("", None,
                                                   "000000000000000000000000")
            if empty:
                idx = _get(p, "plot_index", "index", "id")
                if idx is None:
                    continue
                rp = await ws.action("plant", {"plot_index": idx,
                                               "crop_id": crop_id})
                if rp is not None:
                    planted += 1
                    print(f"[tasks] 种植 plot_index={idx} {crop_name}")
                await _delay()
        print(f"[tasks] 步骤6 种植：{planted} 块（{crop_name}）")

    # 7) 自家除草除虫（清理别人放你地里的草/虫 -> 今日除草/除虫）
    rw = await api.safe_call(api.clean_own_weed, session, hdrs)
    await _delay()
    rp = await api.safe_call(api.clean_own_pest, session, hdrs)
    await _delay()
    print(f"[tasks] 步骤7 自家除草除虫：草{'✓' if rw is not None else '×'} "
          f"虫{'✓' if rp is not None else '×'}")

    # 8) 二次领取
    n2 = await _claim_completed_tasks(session, hdrs, "新完成任务")
    print(f"[tasks] 步骤8 二次领取：{n2} 个")

    print("========== 日常任务执行完毕 ==========")
    return hdrs[0]
