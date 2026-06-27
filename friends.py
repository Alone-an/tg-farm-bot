"""
friends.py — 好友拜访 / 偷菜 / 放草放虫。✅ 全 REST，按源码确认。

每个好友：visit 拜访 → steal-crops 偷菜(一次偷全部) →
在其地块放草/放虫(friend-farm/mark) 以推进今日"放草5次/放虫5次"。
好友用 player_key 标识。visited_today 当天去重，跨天清空。
"""
import datetime

import api
import config
from tasks import _as_list, _get, _delay

visited_today = set()
_visited_date = None

# 今日放草/放虫计数（任务各需 5 次）
WEED_TARGET = 5
PEST_TARGET = 5

# 一次性诊断标志：放草放虫常凑不满目标，疑似 _plot_indices 解析不到好友地块而退回 [0]
_plot_diag_done = False


def _reset_if_new_day():
    global _visited_date
    today = datetime.date.today()
    if _visited_date != today:
        visited_today.clear()
        _visited_date = today
        print(f"[friends] 新的一天 {today}，清空今日访问记录")


def _total_pages(data):
    if isinstance(data, dict):
        for k in ("total_pages", "totalPages", "pages", "page_count"):
            v = data.get(k)
            if isinstance(v, int) and v > 0:
                return v
        total = data.get("total")
        size = data.get("page_size") or data.get("pageSize")
        if isinstance(total, int) and isinstance(size, int) and size > 0:
            return (total + size - 1) // size
    return 1


def _friend_key(f):
    return _get(f, "player_key", "target_key", "tg_id", "id")


def _plot_indices(visit_resp):
    """从 visit 返回里尽量取出好友地块的 plot_index 列表，取不到则退回 [0]。"""
    global _plot_diag_done
    plots = _as_list(visit_resp, "plots", "farm", "data")
    idxs = [_get(p, "plot_index", "index", "id") for p in plots]
    idxs = [i for i in idxs if i is not None]
    if not idxs and not _plot_diag_done:
        # 一次性诊断（仅打印不改行为）：暴露 visit 响应结构，便于据此修正字段映射后真修。
        _plot_diag_done = True
        if not plots and isinstance(visit_resp, dict):
            print(f"[friends] 诊断: 地块容器未命中, visit keys={list(visit_resp.keys())}")
        elif plots and isinstance(plots[0], dict):
            print(f"[friends] 诊断: 地块字段未命中, plot 样例 keys={list(plots[0].keys())}")
        else:
            print(f"[friends] 诊断: visit={type(visit_resp).__name__}, plots 数={len(plots)}")
    return idxs or [0]


async def run_friend_visits(session, headers):
    _reset_if_new_day()
    hdrs = [headers]
    print("\n========== 开始拜访好友 ==========")

    first = await api.safe_call(api.get_friends, session, hdrs, 1)
    if first is None:
        print("[friends] 获取好友列表失败，跳过")
        return hdrs[0]

    pages = _total_pages(first)
    friends = []

    def _collect(data):
        for f in _as_list(data, "data", "friends", "list"):
            k = _friend_key(f)
            if k is not None:
                friends.append(k)

    _collect(first)
    for page in range(2, pages + 1):
        data = await api.safe_call(api.get_friends, session, hdrs, page)
        if data is not None:
            _collect(data)
        await _delay()

    friends = [f for f in dict.fromkeys(friends) if f not in visited_today]
    print(f"[friends] 待访问 {len(friends)} 个好友")

    weed_placed = 0
    pest_placed = 0

    for key in friends:
        # a) 拜访
        vr = await api.safe_call(api.visit_friend, session, hdrs, key)
        if vr is None:
            print(f"[friends] 拜访 {key} 失败，跳过")
            continue
        print(f"[friends] 拜访好友 {key}")
        visited_today.add(key)
        await _delay()

        # b) 偷菜（一次性偷该好友全部可偷作物）
        sr = await api.safe_call(api.steal_crops, session, hdrs, key)
        if sr is not None:
            got = _get(sr, "stolen", "crops", "items", default=None)
            print(f"[friends]   偷菜完成 {('('+str(got)+')') if got else ''}")
        await _delay()

        # c) 放草放虫，推进今日任务（够 5 次就停）
        if weed_placed < WEED_TARGET or pest_placed < PEST_TARGET:
            for idx in _plot_indices(vr):
                if weed_placed < WEED_TARGET:
                    r = await api.safe_call(api.place_weed, session, hdrs, key, idx)
                    if r is not None:
                        weed_placed += 1
                        print(f"[friends]   放草 {key} plot={idx} "
                              f"({weed_placed}/{WEED_TARGET})")
                    await _delay()
                if pest_placed < PEST_TARGET:
                    r = await api.safe_call(api.place_pest, session, hdrs, key, idx)
                    if r is not None:
                        pest_placed += 1
                        print(f"[friends]   放虫 {key} plot={idx} "
                              f"({pest_placed}/{PEST_TARGET})")
                    await _delay()
                if weed_placed >= WEED_TARGET and pest_placed >= PEST_TARGET:
                    break
        await _delay()

    print(f"[friends] 完成：放草 {weed_placed}/{WEED_TARGET}，"
          f"放虫 {pest_placed}/{PEST_TARGET}")
    print("========== 好友拜访执行完毕 ==========")
    return hdrs[0]
