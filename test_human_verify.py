"""
test_human_verify.py — human_verify 单元测试（不联网、不开浏览器、不打码）。

monkeypatch 掉取 token（_obtain_token / local_turnstile.solve_local / captcha_solver.solve）
与提交（_submit），验证：
  - ensure_passed：解锁成功 + 通过时效缓存（不重复取 token）、未启用降级、取 token 失败、提交失败；
  - _obtain_token 的 provider 分发：local 优先、auto 回退 2captcha、2captcha 直连。

既可 `pytest test_human_verify.py` 跑，也可 `python test_human_verify.py` 直接跑。
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
import human_verify
import local_turnstile


@contextlib.contextmanager
def _patched(*, obtain=None, submit=None, local_solve=None, cap_solve=None, **cfg):
    """临时覆盖 config + 各 token 来源 + _submit，并清干净缓存/锁。"""
    saved_cfg = {k: getattr(config, k) for k in cfg}
    saved = {
        "obtain": human_verify._obtain_token,
        "submit": human_verify._submit,
        "local": local_turnstile.solve_local,
        "cap": captcha_solver.solve,
    }
    human_verify.reset()
    human_verify._lock = None
    try:
        for k, v in cfg.items():
            setattr(config, k, v)
        if obtain is not None:
            human_verify._obtain_token = obtain
        if submit is not None:
            human_verify._submit = submit
        if local_solve is not None:
            local_turnstile.solve_local = local_solve
        if cap_solve is not None:
            captcha_solver.solve = cap_solve
        yield
    finally:
        for k, v in saved_cfg.items():
            setattr(config, k, v)
        human_verify._obtain_token = saved["obtain"]
        human_verify._submit = saved["submit"]
        local_turnstile.solve_local = saved["local"]
        captcha_solver.solve = saved["cap"]
        human_verify.reset()
        human_verify._lock = None


# ---- ensure_passed 主逻辑 ----
@pytest.mark.asyncio
async def test_ensure_passed_success_and_cache():
    calls = {"obtain": 0, "submit": 0}

    async def obtain(on_alert=None):
        calls["obtain"] += 1
        return "tok"

    async def submit(token):
        calls["submit"] += 1
        assert token == "tok"
        return {"human_pass": "hp_xyz", "expires_in": 900}

    with _patched(obtain=obtain, submit=submit, CAPTCHA_ENABLED=True):
        assert await human_verify.ensure_passed() is True
        assert human_verify.is_passed() is True
        assert human_verify.current_pass() == "hp_xyz"
        assert human_verify.pass_header() == {"X-Human-Pass": "hp_xyz"}
        # 时效内第二次：直接返回，不再取 token / 不再提交
        assert await human_verify.ensure_passed() is True
        assert calls == {"obtain": 1, "submit": 1}


@pytest.mark.asyncio
async def test_ensure_passed_disabled():
    with _patched(CAPTCHA_ENABLED=False):
        assert await human_verify.ensure_passed() is False
        assert human_verify.is_passed() is False


@pytest.mark.asyncio
async def test_ensure_passed_obtain_fail():
    async def obtain(on_alert=None):
        return None

    with _patched(obtain=obtain, CAPTCHA_ENABLED=True):
        assert await human_verify.ensure_passed() is False
        assert human_verify.is_passed() is False


@pytest.mark.asyncio
async def test_ensure_passed_submit_fail():
    async def obtain(on_alert=None):
        return "tok"

    async def submit(token):
        return None

    with _patched(obtain=obtain, submit=submit, CAPTCHA_ENABLED=True):
        assert await human_verify.ensure_passed() is False
        assert human_verify.is_passed() is False
        assert human_verify.pass_header() == {}


@pytest.mark.asyncio
async def test_reset_clears_pass():
    async def obtain(on_alert=None):
        return "tok"

    async def submit(token):
        return {"human_pass": "hp", "expires_in": 900}

    with _patched(obtain=obtain, submit=submit, CAPTCHA_ENABLED=True):
        assert await human_verify.ensure_passed() is True
        human_verify.reset()
        assert human_verify.is_passed() is False
        assert human_verify.current_pass() == ""


# ---- _obtain_token 的 provider 分发 ----
@pytest.mark.asyncio
async def test_obtain_token_local_first():
    async def local_solve(sitekey, page_url):
        return "local-tok"

    async def cap_solve(sitekey, **kw):
        raise AssertionError("local 模式不应调 2captcha")

    with _patched(local_solve=local_solve, cap_solve=cap_solve, CAPTCHA_PROVIDER="local"):
        assert await human_verify._obtain_token() == "local-tok"


@pytest.mark.asyncio
async def test_obtain_token_auto_fallback():
    async def local_solve(sitekey, page_url):
        return None                      # 本地失败

    async def cap_solve(sitekey, **kw):
        return "cap-tok"

    with _patched(local_solve=local_solve, cap_solve=cap_solve,
                  CAPTCHA_PROVIDER="auto", CAPTCHA_API_KEY="k"):
        assert await human_verify._obtain_token() == "cap-tok"


@pytest.mark.asyncio
async def test_obtain_token_2captcha():
    async def cap_solve(sitekey, **kw):
        return "cap-tok"

    with _patched(cap_solve=cap_solve, CAPTCHA_PROVIDER="2captcha", CAPTCHA_API_KEY="k"):
        assert await human_verify._obtain_token() == "cap-tok"


# =========================================================================
# 无 pytest 时的独立运行入口
# =========================================================================
async def _run_all():
    print("\n===== human_verify 单元测试（standalone）=====")
    cases = [
        ("解锁成功 + 时效缓存", test_ensure_passed_success_and_cache),
        ("未启用 -> False", test_ensure_passed_disabled),
        ("取 token 失败 -> False", test_ensure_passed_obtain_fail),
        ("提交失败 -> False", test_ensure_passed_submit_fail),
        ("reset 清除通过状态", test_reset_clears_pass),
        ("provider=local 优先本地", test_obtain_token_local_first),
        ("provider=auto 本地失败回退 2captcha", test_obtain_token_auto_fallback),
        ("provider=2captcha 直连", test_obtain_token_2captcha),
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
