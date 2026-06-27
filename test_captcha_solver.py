"""
test_captcha_solver.py — captcha_solver 单元测试（不联网）。

用进程内 fake 替换 curl_cffi 的 AsyncSession，模拟 2captcha in.php/res.php，验证：
提交→轮询→token 提取、按类型选 method/参数名、无 key 降级、轮询超时、提交报错，
以及 make_captcha_solver 的启用/禁用分支。

既可 `pytest test_captcha_solver.py` 跑，也可 `python test_captcha_solver.py` 直接跑
（standalone，无需 pytest-asyncio）。
"""
import asyncio
import contextlib
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import pytest

import captcha_solver
import config
import curl_cffi.requests as _cr


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeSession:
    """模拟 curl_cffi AsyncSession：in.php 用 in_resp，res.php 按 res_seq 顺序返回（末项重复）。"""

    def __init__(self, in_resp, res_seq):
        self.in_resp = in_resp
        self.res_seq = list(res_seq)
        self.posts = []
        self.gets = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, timeout=None):
        self.posts.append({"url": url, "data": data})
        return _FakeResp(self.in_resp)

    async def get(self, url, params=None, timeout=None):
        self.gets += 1
        idx = min(self.gets - 1, len(self.res_seq) - 1)
        return _FakeResp(self.res_seq[idx])


@contextlib.contextmanager
def _patched(session=None, **cfg):
    """临时覆盖 config 属性 + curl_cffi.AsyncSession，退出时还原。"""
    saved = {k: getattr(config, k) for k in cfg}
    saved_sess = _cr.AsyncSession
    try:
        for k, v in cfg.items():
            setattr(config, k, v)
        if session is not None:
            _cr.AsyncSession = lambda *a, **k: session
        yield
    finally:
        for k, v in saved.items():
            setattr(config, k, v)
        _cr.AsyncSession = saved_sess


@pytest.mark.asyncio
async def test_solve_success():
    fake = _FakeSession(
        in_resp={"status": 1, "request": "task-123"},
        res_seq=[{"status": 1, "request": "TOKEN_OK"}],
    )
    with _patched(session=fake, CAPTCHA_API_KEY="k", CAPTCHA_TYPE="turnstile",
                  CAPTCHA_POLL_INTERVAL=1, CAPTCHA_POLL_TIMEOUT=10):
        token = await captcha_solver.solve("site-key-1", page_url="https://x/")
    assert token == "TOKEN_OK"
    assert fake.posts and fake.posts[0]["url"].endswith("/in.php")
    assert fake.posts[0]["data"]["sitekey"] == "site-key-1"
    assert fake.posts[0]["data"]["method"] == "turnstile"
    assert fake.posts[0]["data"]["pageurl"] == "https://x/"
    assert fake.gets >= 1


@pytest.mark.asyncio
async def test_solve_recaptcha_uses_googlekey():
    fake = _FakeSession(
        in_resp={"status": 1, "request": "t"},
        res_seq=[{"status": 1, "request": "TOK"}],
    )
    with _patched(session=fake, CAPTCHA_API_KEY="k",
                  CAPTCHA_POLL_INTERVAL=1, CAPTCHA_POLL_TIMEOUT=10):
        token = await captcha_solver.solve("gk", captcha_type="recaptcha_v2")
    assert token == "TOK"
    assert fake.posts[0]["data"]["method"] == "userrecaptcha"
    assert fake.posts[0]["data"]["googlekey"] == "gk"


@pytest.mark.asyncio
async def test_solve_no_key():
    with _patched(CAPTCHA_API_KEY=""):
        token = await captcha_solver.solve("site-key-1")
    assert token is None


@pytest.mark.asyncio
async def test_solve_poll_timeout():
    fake = _FakeSession(
        in_resp={"status": 1, "request": "task-x"},
        res_seq=[{"status": 0, "request": "CAPCHA_NOT_READY"}],
    )
    with _patched(session=fake, CAPTCHA_API_KEY="k",
                  CAPTCHA_POLL_INTERVAL=1, CAPTCHA_POLL_TIMEOUT=1):
        token = await captcha_solver.solve("site-key-1")
    assert token is None
    assert fake.gets >= 1


@pytest.mark.asyncio
async def test_solve_submit_error():
    fake = _FakeSession(
        in_resp={"status": 0, "request": "ERROR_WRONG_USER_KEY"},
        res_seq=[{"status": 1, "request": "never"}],
    )
    with _patched(session=fake, CAPTCHA_API_KEY="k"):
        token = await captcha_solver.solve("site-key-1")
    assert token is None
    assert fake.gets == 0  # 提交失败不应进入轮询


@pytest.mark.asyncio
async def test_make_solver_disabled_alerts():
    alerts = []
    with _patched(CAPTCHA_ENABLED=False, CAPTCHA_API_KEY=""):
        solver = captcha_solver.make_captcha_solver(on_alert=alerts.append)
        token = await solver({"sitekey": "k"})
    assert token is None
    assert any(("未启用" in a) or ("API key" in a) for a in alerts)


@pytest.mark.asyncio
async def test_make_solver_success():
    async def fake_solve(sitekey, **kw):
        assert sitekey == "from-challenge"
        return "TOK_FROM_SOLVE"

    saved = captcha_solver.solve
    try:
        captcha_solver.solve = fake_solve
        with _patched(CAPTCHA_ENABLED=True, CAPTCHA_API_KEY="k",
                      CAPTCHA_SITEKEY_FIELD="sitekey"):
            solver = captcha_solver.make_captcha_solver()
            token = await solver({"sitekey": "from-challenge"})
        assert token == "TOK_FROM_SOLVE"
    finally:
        captcha_solver.solve = saved


# =========================================================================
# 无 pytest 时的独立运行入口
# =========================================================================
async def _run_all():
    print("\n===== captcha_solver 单元测试（standalone）=====")
    cases = [
        ("solve 成功（turnstile）", test_solve_success),
        ("solve recaptcha 用 googlekey", test_solve_recaptcha_uses_googlekey),
        ("solve 无 key 降级", test_solve_no_key),
        ("solve 轮询超时", test_solve_poll_timeout),
        ("solve 提交报错不轮询", test_solve_submit_error),
        ("make_solver 禁用→告警+None", test_make_solver_disabled_alerts),
        ("make_solver 启用→透传 token", test_make_solver_success),
    ]
    failures = 0
    for name, fn in cases:
        print(f"\n>>> {name}")
        try:
            await fn()
            print("  PASS")
        except AssertionError as e:
            failures += 1
            print(f"  FAIL: {e}")
        except Exception as e:
            failures += 1
            print(f"  ERROR: {e!r}")
    print("\n[OK] 全部通过" if not failures else f"\n[FAIL] {failures} 条未通过")
    return failures


if __name__ == "__main__":
    raise SystemExit(1 if asyncio.run(_run_all()) else 0)
