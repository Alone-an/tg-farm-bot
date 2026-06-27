"""
auth.py — 鉴权模块（按 example.com 真实流程）。

真实流程（抓包确认）：
  1. Telethon 取 Telegram initData（新版格式，含 signature）。
  2. (可选) DrissionPage 过 Cloudflare 拿 cookies。
  3. POST /api/auth/login  body={"init_data": initData}  ->  {"token": JWT, "user": {...}}
  4. 之后所有 REST 请求带 Authorization: Bearer <JWT>；WS 握手也用这个 JWT。
"""
import time
import urllib.parse

from telethon.tl.functions.messages import RequestWebViewRequest

import config

# ---- 模块级缓存 ----
_last_token_time = 0.0
_last_cf_time = 0.0
_init_data = None
_cf_cookies = None
_token = None        # 当前 JWT
_user = None         # login 返回的 user 对象（含 coins/level 等）
_headers = None
_client = None


async def get_init_data(client):
    """通过 Telethon 取 initData（tgWebAppData 解码值）。"""
    result = await client(
        RequestWebViewRequest(
            peer=config.BOT_USERNAME,
            bot=config.BOT_USERNAME,
            platform="android",
            from_bot_menu=False,
            url=config.MINI_APP_URL or None,
        )
    )
    raw_url = result.url
    fragment = raw_url.split("#", 1)[1] if "#" in raw_url else raw_url
    params = urllib.parse.parse_qs(fragment)
    if "tgWebAppData" not in params:
        raise RuntimeError(f"未能从 WebView URL 提取 tgWebAppData: {raw_url}")
    init_data = params["tgWebAppData"][0]
    print("[auth] initData 提取成功")
    return init_data


def _find_browser():
    """探测本机可用的 Chromium 内核浏览器路径：Chrome 优先、回退 Edge，跨平台。

    顺序：config.CF_BROWSER_PATH 显式指定 > Windows/macOS 常见安装路径 >
    Linux/PATH 上的可执行名。都找不到返回 None（由上层优雅降级，不闪退）。
    """
    import os
    import shutil

    if config.CF_BROWSER_PATH:
        return config.CF_BROWSER_PATH if os.path.exists(config.CF_BROWSER_PATH) else None

    candidates = [
        # Windows —— Chrome 优先
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
        # Windows —— 回退 Edge（Win10/11 预装）
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ]
    for p in candidates:
        if p and os.path.exists(p):
            return p
    # Linux / 已加入 PATH 的浏览器
    for name in ("google-chrome", "google-chrome-stable", "chromium",
                 "chromium-browser", "microsoft-edge", "microsoft-edge-stable"):
        p = shutil.which(name)
        if p:
            return p
    return None


def _page_cookies(page):
    """兼容不同 DrissionPage 版本，取当前页 cookies 为 {name: value} dict。"""
    try:
        return dict(page.cookies(as_dict=True) or {})
    except TypeError:
        ck = page.cookies()
        try:
            return dict(ck.as_dict())
        except AttributeError:
            return {c.get("name"): c.get("value") for c in (ck or [])}


def get_cf_cookies(mini_app_url):
    """用 DrissionPage 过 Cloudflare 网络层质询，返回 cookies dict（含 cf_clearance）。

    跨平台鲁棒性「完全体」：
      - 自动适配环境：浏览器 Chrome 优先、自动回退 Edge（见 _find_browser）；
      - 隔离实例：auto_port() 自动分配调试端口 + 独立临时用户目录，不干扰你正在用的浏览器；
      - 可选无头：设 FARM_CF_HEADLESS=1 走无头（CF 对无头较严，默认有头更稳）；
      - 绝不闪退：没装 DrissionPage / 找不到浏览器 / 过盾异常，都只告警并返回 {}，
        让上层无 cookie 继续，由服务端决定是否放行。

   
    
    云端 CLOUD=1 无浏览器，直接跳过，靠 curl_cffi 指纹直连。
    """
    if config.CLOUD:
        print("[auth] 云端模式：跳过 CF 浏览器步骤（依赖 curl_cffi 指纹直连）")
        return {}

    browser_path = _find_browser()
    if not browser_path:
        print("[auth] 警告：未找到 Chrome/Edge，跳过过 CF（无 cookie 继续）。"
              "需过真正的 CF 质询时请装 Chrome 或用 CF_BROWSER_PATH 指定浏览器。")
        return {}

    try:
        from DrissionPage import ChromiumOptions, ChromiumPage
    except ImportError:
        print("[auth] 警告：未安装 DrissionPage，跳过过 CF（无 cookie 继续）。"
              "`pip install DrissionPage` 可启用。")
        return {}

    co = ChromiumOptions()
    co.set_browser_path(browser_path)
    co.auto_port()                                   # 隔离实例：独立端口 + 临时用户目录
    co.set_argument("--no-first-run")
    co.set_argument("--no-default-browser-check")
    if config.CF_HEADLESS:
        co.headless()
        co.set_argument("--disable-gpu")
        co.set_argument("--no-sandbox")

    browser_name = "Edge" if "edge" in browser_path.lower() else "Chrome"
    mode = "无头" if config.CF_HEADLESS else "有头"
    print(f"[auth] 过 CF：{browser_name}（{mode}，隔离实例）打开 {mini_app_url}")

    page = None
    try:
        page = ChromiumPage(co)
        page.get(mini_app_url)
        deadline = time.time() + 30
        cookies = {}
        while time.time() < deadline:
            cookies = _page_cookies(page)
            if "cf_clearance" in cookies:
                break
            time.sleep(2)
        cookies = _page_cookies(page)
        if "cf_clearance" in cookies:
            print("[auth] CF 验证通过（已拿到 cf_clearance）")
        else:
            print("[auth] 提示：本次未出现 CF 质询（无 cf_clearance），按当前 cookies 继续")
        return cookies
    except Exception as e:
        print(f"[auth] 过 CF 异常（忽略，无 cookie 继续）: {e!r}")
        return {}
    finally:
        if page is not None:
            try:
                page.quit()
            except Exception:
                pass


async def login(init_data, cf_cookies):
    """
    POST /api/auth/login 用 initData 换 JWT。
    返回 (token, user)。
    """
    from curl_cffi.requests import AsyncSession
    from endpoints import ENDPOINTS

    cookie_str = "; ".join(f"{k}={v}" for k, v in (cf_cookies or {}).items())
    parsed = urllib.parse.urlparse(config.MINI_APP_URL)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""

    url = config.BASE_API_URL.rstrip("/") + ENDPOINTS["login"]
    headers = {
        "Content-Type": "application/json",
        "Origin": origin,
        "Referer": config.MINI_APP_URL,
    }
    if cookie_str:
        headers["Cookie"] = cookie_str

    async with AsyncSession(impersonate="chrome120", proxies=config.proxies()) as s:
        resp = await s.post(url, headers=headers,
                            json={"init_data": init_data},
                            timeout=config.REQUEST_TIMEOUT)
        data = resp.json()

    token = data.get("token")
    if not token:
        raise RuntimeError(f"登录失败，未返回 token: {str(data)[:300]}")
    user = data.get("user", {})
    print(f"[auth] 登录成功 tg_id={user.get('tg_id')} "
          f"level={user.get('level')} coins={user.get('coins')}")
    return token, user


def build_headers(token, cf_cookies):
    """构建 REST 请求头：Authorization: Bearer <JWT>。"""
    cookie_str = "; ".join(f"{k}={v}" for k, v in (cf_cookies or {}).items())
    parsed = urllib.parse.urlparse(config.MINI_APP_URL)
    origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="120", "Not(A:Brand";v="24", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-site": "same-origin",
        "sec-fetch-mode": "cors",
        "sec-fetch-dest": "empty",
        "Referer": config.MINI_APP_URL,
        "Origin": origin,
    }
    if cookie_str:
        headers["Cookie"] = cookie_str
    return headers


def get_token():
    """供 WebSocket 握手使用的当前 JWT。"""
    return _token


def get_user():
    """login 返回的 user 对象。"""
    return _user


async def refresh_auth(client=None, force=False):
    """
    刷新鉴权：必要时过 CF、取 initData、登录换 JWT，返回最新 headers。
    - JWT 每 TOKEN_REFRESH_MIN 分钟重登一次
    - CF cookies 每 CF_COOKIE_REFRESH_MIN 分钟刷新
    - force=True 强制全部刷新（401 / CF 拦截后）
    """
    global _last_token_time, _last_cf_time
    global _init_data, _cf_cookies, _token, _user, _headers, _client

    if client is not None:
        _client = client
    client = _client
    if client is None:
        raise RuntimeError("refresh_auth 缺少 Telegram client（首次调用必须传入）")

    now = time.time()
    cf_expired = force or _cf_cookies is None or (
        now - _last_cf_time > config.CF_COOKIE_REFRESH_MIN * 60
    )
    token_expired = force or _token is None or (
        now - _last_token_time > config.TOKEN_REFRESH_MIN * 60
    )

    if cf_expired:
        try:
            _cf_cookies = get_cf_cookies(config.MINI_APP_URL)
        except Exception as e:
            print(f"[auth] 过 CF 失败（忽略，尝试无 cookie 继续）: {e}")
            _cf_cookies = _cf_cookies or {}
        _last_cf_time = now

    if token_expired:
        _init_data = await get_init_data(client)
        _token, _user = await login(_init_data, _cf_cookies)
        _last_token_time = now

    if cf_expired or token_expired or _headers is None:
        _headers = build_headers(_token, _cf_cookies)
        print("[auth] headers 已更新")

    return _headers
