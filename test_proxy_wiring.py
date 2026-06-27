"""
test_proxy_wiring.py — 验证「全流量出口代理」接线在四条通道上都能穿透，且未配置时
（FARM_PROXY_URL 为空）行为与接入前一致（不传任何 proxy 参数）。

四通道：
  1. config          —— PROXY_URL / proxies() 总开关语义
  2. websockets WS   —— WSStateMachine 把 proxy 透传给 websockets.connect（仅显式配置时）
  3. ws_session 工厂 —— make_state_machine 从 config.PROXY_URL 取出口代理
  4. nodriver 浏览器 —— local_turnstile 给浏览器加 --proxy-server（仅显式配置时）
  5. curl_cffi REST  —— 四处 AsyncSession 静态确认带 proxies=config.proxies()

纯单测、零网络：用 monkeypatch 替掉 websockets.connect / nodriver uc.start，只断言「传了什么」。
"""
import asyncio
import json
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import pytest

import config


PROXY = "http://127.0.0.1:7890"


# =========================================================================
# 1) config 总开关语义
# =========================================================================
def test_config_proxies_off(monkeypatch):
    monkeypatch.setattr(config, "PROXY_URL", "")
    assert config.proxies() is None, "未配置出口代理时应返回 None（=直连）"


def test_config_proxies_on(monkeypatch):
    monkeypatch.setattr(config, "PROXY_URL", PROXY)
    assert config.proxies() == {"http": PROXY, "https": PROXY}


# =========================================================================
# 2) websockets: WSStateMachine 透传 proxy
# =========================================================================
class _FakeWS:
    """最小 ws：握手阶段 send 任意、recv 一次 auth_ok 即让 _open 返回。"""
    def __init__(self):
        self._msgs = [json.dumps({"type": "auth_ok"})]

    async def send(self, _msg):
        pass

    async def recv(self):
        if self._msgs:
            return self._msgs.pop(0)
        await asyncio.sleep(3600)

    async def close(self):
        pass


def _patch_ws_connect(monkeypatch):
    """把 websockets.connect 换成捕获 kwargs 的假实现，返回 captured 字典。"""
    import websockets
    captured = {}

    async def fake_connect(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _FakeWS()

    monkeypatch.setattr(websockets, "connect", fake_connect)
    return captured


async def _dummy_auth(force):
    return {"token": "t", "headers": {}}


def _build_sm(**overrides):
    from ws_state_machine import WSStateMachine
    return WSStateMachine("wss://example/ws", auth_provider=_dummy_auth,
                          ping_interval=None, handshake_timeout=3.0, **overrides)


@pytest.mark.asyncio
async def test_ws_passes_proxy_when_configured(monkeypatch):
    captured = _patch_ws_connect(monkeypatch)
    sm = _build_sm(proxy=PROXY)
    assert sm._proxy == PROXY
    await sm._open({"token": "t", "headers": {}})
    assert captured["kwargs"].get("proxy") == PROXY, "配置出口代理时 connect 应带 proxy=该地址"


@pytest.mark.asyncio
async def test_ws_omits_proxy_when_not_configured(monkeypatch):
    captured = _patch_ws_connect(monkeypatch)
    sm = _build_sm()                       # proxy 默认 None
    assert sm._proxy is None
    await sm._open({"token": "t", "headers": {}})
    assert "proxy" not in captured["kwargs"], \
        "未配置代理时不应传 proxy kwarg（保持 websockets 默认行为，与接入前一致）"


# =========================================================================
# 3) ws_session 工厂从 config.PROXY_URL 取出口代理
# =========================================================================
def test_ws_session_wires_proxy_from_config(monkeypatch):
    import ws_session
    monkeypatch.setattr(config, "PROXY_URL", PROXY)
    sm = ws_session.make_state_machine(client=None)
    assert sm._proxy == PROXY, "make_state_machine 应把 config.PROXY_URL 接到状态机 proxy"


def test_ws_session_no_proxy_by_default(monkeypatch):
    import ws_session
    monkeypatch.setattr(config, "PROXY_URL", "")
    sm = ws_session.make_state_machine(client=None)
    assert sm._proxy is None


# =========================================================================
# 4) nodriver: local_turnstile 给浏览器加 --proxy-server
# =========================================================================
class _FakeBrowser:
    async def get(self, _url):
        raise RuntimeError("short-circuit: 只验证启动参数，不真开页面")

    def stop(self):
        pass


def _patch_uc_start(monkeypatch):
    import local_turnstile
    captured = {}

    async def fake_start(**kwargs):
        captured["kwargs"] = kwargs
        return _FakeBrowser()

    monkeypatch.setattr(local_turnstile, "_find_browser", lambda: "/fake/chrome")
    monkeypatch.setattr(local_turnstile.uc, "start", fake_start)
    return captured


@pytest.mark.asyncio
async def test_turnstile_adds_proxy_arg_when_configured(monkeypatch):
    import local_turnstile
    captured = _patch_uc_start(monkeypatch)
    monkeypatch.setattr(config, "PROXY_URL", PROXY)
    monkeypatch.setattr(config, "CLOUD", False)
    await local_turnstile.solve_local("sitekey", "https://example/")
    args = captured["kwargs"].get("browser_args") or []
    assert f"--proxy-server={PROXY}" in args, "配置出口代理时浏览器应带 --proxy-server"


@pytest.mark.asyncio
async def test_turnstile_no_proxy_arg_by_default(monkeypatch):
    import local_turnstile
    captured = _patch_uc_start(monkeypatch)
    monkeypatch.setattr(config, "PROXY_URL", "")
    monkeypatch.setattr(config, "CLOUD", False)
    await local_turnstile.solve_local("sitekey", "https://example/")
    args = captured["kwargs"].get("browser_args") or []
    assert not any("--proxy-server" in a for a in args), \
        "未配置代理时不应加 --proxy-server（本地行为不变）"


# =========================================================================
# 5) curl_cffi: 四处 AsyncSession 都带 proxies=config.proxies()（静态确认，防回归）
# =========================================================================
def test_curl_cffi_sessions_carry_proxies():
    for fname in ("run_once.py", "main.py", "scheduler.py", "auth.py"):
        src = open(fname, encoding="utf-8").read()
        assert "AsyncSession(impersonate=\"chrome120\", proxies=config.proxies())" in src, \
            f"{fname} 的 AsyncSession 应带 proxies=config.proxies()"
