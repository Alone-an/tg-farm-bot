"""
test_tasks_sell.py — tasks 新增的卖果实/扩地/挑高级种子逻辑单元测试。

不连网：FakeWS 模拟 WS 动作。验证 _sell_all_fruits / _unlock_plots / _pick_seed_crop。
"""
import asyncio

import pytest

import tasks


class FakeWS:
    """最小 WS 替身：按动作名返回预设数据，并记录调用。"""

    def __init__(self, *, inventory=None, fruit=None,
                 sell_resp=lambda d: {"coins": 0, "coins_gain": d["count"] * 10},
                 unlock_results=None):
        self._inventory = inventory or {"data": []}
        self._fruit = fruit or {"data": []}
        self._sell_resp = sell_resp
        self._unlock_results = list(unlock_results or [])
        self.sold = []          # [(crop_id, count), ...]
        self.unlock_calls = 0

    async def action(self, name, data=None):
        if name == "get_inventory":
            return self._inventory
        if name == "get_fruit_inventory":
            return self._fruit
        if name == "sell_fruits":
            self.sold.append((data["crop_id"], data["count"]))
            return self._sell_resp(data)
        if name == "unlock_plot":
            self.unlock_calls += 1
            if self._unlock_results:
                return self._unlock_results.pop(0)
            return None
        raise AssertionError(f"未预期的动作: {name}")


@pytest.fixture(autouse=True)
def _no_delay(monkeypatch):
    async def _fast():
        return
    monkeypatch.setattr(tasks, "_delay", _fast)


# ---------------- _sell_all_fruits ----------------
def test_sell_all_fruits_sells_every_nonzero():
    ws = FakeWS(fruit={"data": [
        {"crop_id": "a", "crop_name": "西瓜", "count": 200},
        {"crop_id": "b", "crop_name": "白菜", "count": 74},
        {"crop_id": "c", "crop_name": "空果", "count": 0},
        {"crop_name": "无id", "count": 5},
    ]})
    sold, gain = asyncio.run(tasks._sell_all_fruits(ws))
    assert sold == 2
    assert ws.sold == [("a", 200), ("b", 74)]
    assert gain == (200 + 74) * 10


def test_sell_all_fruits_empty():
    ws = FakeWS(fruit={"data": []})
    assert asyncio.run(tasks._sell_all_fruits(ws)) == (0, 0)
    assert ws.sold == []


def test_sell_all_fruits_failed_sell_not_counted():
    ws = FakeWS(fruit={"data": [{"crop_id": "a", "count": 5}]},
                sell_resp=lambda d: None)
    sold, gain = asyncio.run(tasks._sell_all_fruits(ws))
    assert (sold, gain) == (0, 0)
    assert ws.sold == [("a", 5)]


def test_sell_all_fruits_missing_gain_field():
    ws = FakeWS(fruit={"data": [{"crop_id": "a", "count": 5}]},
                sell_resp=lambda d: {"coins": 123})
    sold, gain = asyncio.run(tasks._sell_all_fruits(ws))
    assert sold == 1 and gain == 0


# ---------------- _unlock_plots ----------------
def test_unlock_plots_stops_on_first_failure():
    ws = FakeWS(unlock_results=[{"ok": True}, {"ok": True}])
    assert asyncio.run(tasks._unlock_plots(ws)) == 2
    assert ws.unlock_calls == 3


def test_unlock_plots_none_immediately():
    ws = FakeWS(unlock_results=[])
    assert asyncio.run(tasks._unlock_plots(ws)) == 0
    assert ws.unlock_calls == 1


def test_unlock_plots_respects_max_attempts():
    ws = FakeWS(unlock_results=[{"ok": True}] * 100)
    assert asyncio.run(tasks._unlock_plots(ws, max_attempts=3)) == 3
    assert ws.unlock_calls == 3


# ---------------- _pick_seed_crop ----------------
def test_pick_seed_crop_prefers_highest_grade():
    ws = FakeWS(inventory={"data": [
        {"crop_id": "low", "crop_name": "大蒜", "count": 5, "crop_grade": 1},
        {"crop_id": "high", "crop_name": "西瓜大王", "count": 8, "crop_grade": 15},
        {"crop_id": "mid", "crop_name": "南瓜", "count": 17, "crop_grade": 13},
        {"crop_id": "zero", "crop_name": "无货", "count": 0, "crop_grade": 99},
    ]})
    cid, name = asyncio.run(tasks._pick_seed_crop(ws))
    assert cid == "high" and name == "西瓜大王"


def test_pick_seed_crop_empty_returns_none():
    ws = FakeWS(inventory={"data": []})
    assert asyncio.run(tasks._pick_seed_crop(ws)) == (None, None)
