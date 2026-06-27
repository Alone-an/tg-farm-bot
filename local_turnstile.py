"""
local_turnstile.py — 本地浏览器（nodriver 反检测）免费解 Cloudflare Turnstile。

nodriver 隐藏自动化特征、指纹接近真人，CF 通常判定为「无感」直接放行——不用点击、
不花钱、不经过第三方打码平台。

⚠️ CI 关键坑（已实测定位）：GitHub Actions 上 chrome 冷启动到 CDP 就绪需 ~7s，而
nodriver 0.50.3 起浏览器后只等 ~2.75s（core/browser.py 写死 0.25 + 5×0.5）就放弃，
抛出误导性的 "Failed to connect ... running as root ... no_sandbox=True"（其实 chrome
已起来、只是还没就绪）。外层重试也无解——每次都 kill 重起、又要 7s 冷启动。

解法【自管进程 + connect_existing】：不让 nodriver 自己起浏览器，改为自己用 subprocess
起浏览器（固定调试端口）、自己轮询 CDP 直到就绪（想等多久等多久），再用
uc.start(host, port) 连这个已就绪实例——connect_existing 下 nodriver 不再起进程，
第一次 get(version) 就成功，永不触发那个短超时。本机已用 Edge 验证可行（CDP 1s 就绪、
连接 + evaluate 均 OK）。浏览器用谁由 _find_browser / CF_BROWSER_PATH 决定。
"""
import asyncio
import socket
import subprocess
import tempfile
import urllib.request

import nodriver as uc

import config
from auth import _find_browser

_INJECT = """
window.__cf_token = null; window.__cf_err = null;
(function(){
  function render(){
    try {
      var c = document.createElement('div');
      c.id = '__cf_box';
      c.style.position = 'fixed'; c.style.top = '8px'; c.style.left = '8px';
      c.style.zIndex = 999999;
      document.body.appendChild(c);
      window.turnstile.render('#__cf_box', {
        sitekey: '__SITEKEY__',
        callback: function(t){ window.__cf_token = t; }
      });
    } catch(e){ window.__cf_err = String(e); }
  }
  if (window.turnstile && window.turnstile.render) { render(); return; }
  var s = document.createElement('script');
  s.src = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit';
  s.async = true; s.defer = true;
  s.onload = render;
  s.onerror = function(){ window.__cf_err = 'turnstile api.js 加载失败'; };
  document.head.appendChild(s);
})();
"""

# 复刻 nodriver 的关键默认启动参数。我们自己起进程、不再经过 nodriver 的 Config()，
# 必须手动补上——尤其 --remote-allow-origins=*：否则新版 chrome 会拒绝 CDP 的跨 origin
# websocket 握手，导致 nodriver 连不上已就绪的实例。其余对齐 nodriver 默认行为。
_NODRIVER_DEFAULT_ARGS = [
    "--remote-allow-origins=*",
    "--no-first-run",
    "--no-service-autorun",
    "--no-default-browser-check",
    "--homepage=about:blank",
    "--no-pings",
    "--password-store=basic",
    "--disable-infobars",
    "--disable-breakpad",
    "--disable-dev-shm-usage",
    "--disable-session-crashed-bubble",
    "--disable-search-engine-choice-screen",
]


# WebGL 指纹探针：打印 UNMASKED vendor/renderer。云端 xvfb 无真实 GPU 时通常退化为
# SwiftShader（软件渲染），CF Turnstile 据此判机器人——本机有真 GPU 则正常。用于坐实
# 「token 拿不到」是否源于 WebGL 指纹（仅 CLOUD 下打）。
_WEBGL_PROBE = """
(function(){
  try{
    var c=document.createElement('canvas');
    var gl=c.getContext('webgl')||c.getContext('experimental-webgl');
    if(!gl) return 'NO_WEBGL';
    var dbg=gl.getExtension('WEBGL_debug_renderer_info');
    var vendor=dbg?gl.getParameter(dbg.UNMASKED_VENDOR_WEBGL):gl.getParameter(gl.VENDOR);
    var renderer=dbg?gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL):gl.getParameter(gl.RENDERER);
    return String(vendor)+' || '+String(renderer);
  }catch(e){return 'ERR '+String(e);}
})()
"""


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _cdp_probe(port):
    """同步探一次 CDP /json/version；就绪返回 True。"""
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
        return r.status == 200


async def _wait_cdp_ready(port, proc, timeout=30):
    """轮询 CDP 直到就绪。就绪 True；浏览器进程中途退出或超时 False。
    urlopen 是阻塞的，丢线程里跑，避免卡住事件循环（telethon/ws 在后台）。"""
    for i in range(timeout):
        await asyncio.sleep(1)
        try:
            if await asyncio.to_thread(_cdp_probe, port):
                print(f"[local-turnstile] 浏览器 CDP 第 {i+1}s 就绪")
                return True
        except Exception:
            pass
        if proc.poll() is not None:
            print(f"[local-turnstile] 浏览器进程已退出(rc={proc.returncode})，启动失败"
                  "（看上方 chrome stderr 找崩溃原因）")
            return False
    print(f"[local-turnstile] {timeout}s 内 CDP 未就绪，放弃")
    return False


async def _start_browser_connect(bp, extra_args):
    """自管进程 + connect_existing 起浏览器，绕开 nodriver 写死的 ~2.75s 连接超时。
    返回 (browser, proc)；失败返回 (None, None)。proc 由调用方在 finally 里关闭。"""
    port = _free_port()
    user_data = tempfile.mkdtemp(prefix="uc_ci_")
    args = ([bp] + _NODRIVER_DEFAULT_ARGS + list(extra_args)
            + [f"--remote-debugging-port={port}", f"--user-data-dir={user_data}",
               "about:blank"])
    # ⚠️ stderr/stdout 不重定向，直接继承到当前进程 → 进 CI 日志（nodriver 会 PIPE 吞掉，
    # 这里专门让 chrome 的真实崩溃原因可见）。
    try:
        proc = subprocess.Popen(args)
    except Exception as e:
        print(f"[local-turnstile] 起浏览器进程失败: {e!r}")
        return None, None
    print(f"[local-turnstile] 已起浏览器 pid={proc.pid} port={port}，等 CDP 就绪…")
    if not await _wait_cdp_ready(port, proc):
        try:
            proc.terminate()
        except Exception:
            pass
        return None, None
    try:
        # connect_existing：传 host+port，nodriver 直接连已就绪实例、不自己起进程。
        # 仍传 browser_executable_path：Config 构造时会 find_chrome_executable()，
        # 只装 Edge 时找不到 Chrome 会 raise，显式给出可执行路径规避。
        browser = await uc.start(host="127.0.0.1", port=port,
                                 browser_executable_path=bp)
        return browser, proc
    except Exception as e:
        print(f"[local-turnstile] nodriver 连接已就绪实例失败: {e!r}")
        try:
            proc.terminate()
        except Exception:
            pass
        return None, None


async def _log_exit_ip(browser):
    """云端诊断：打印浏览器经代理看到的出口 IP/地区，确认流量真从 AWS 机房出去
    （区分『IP 问题』与『浏览器指纹问题』——排错关键，仅 CLOUD 下打）。"""
    try:
        t = await browser.get("https://www.cloudflare.com/cdn-cgi/trace")
        await t.sleep(3)
        trace = str(await t.evaluate("document.body.innerText") or "")
        ip = next((l for l in trace.splitlines() if l.startswith("ip=")), "ip=?")
        loc = next((l for l in trace.splitlines() if l.startswith("loc=")), "loc=?")
        print(f"[local-turnstile] 浏览器出口确认: {ip} {loc}")
    except Exception as e:
        print(f"[local-turnstile] 出口 IP 诊断失败: {e!r}")


async def solve_local(sitekey, page_url, token_timeout=60):
    """用 nodriver 打开页面、注入 Turnstile widget、等无感回调拿 token。失败返回 None。"""
    bp = _find_browser()
    if not bp:
        print("[local-turnstile] 未找到 Chrome/Edge")
        return None
    # 云端/CI(无显示器、共享内存小)下 Chrome 的稳定+反检测参数；本地不加，行为不变。
    # CI 端由 workflow 的 xvfb-run 提供虚拟显示器。
    # ⚠️ 实测(2026-06-26)：xvfb 无真实 GPU，新版 Chrome 默认把 SwiftShader 的 WebGL 拉黑
    #   (日志 "WebGL1/2 blocklisted" → getContext('webgl') 返回 null → WebGL: NO_WEBGL)，
    #   而「完全没有 WebGL」是比软件渲染更强的机器人信号，CF Turnstile 直接不发 token。
    #   故显式 --enable-unsafe-swiftshader 解禁 + --use-gl=angle/--use-angle=swiftshader
    #   指定软件渲染后端，让 WebGL 至少以 SwiftShader 跑起来(出一份 WebGL 指纹)。
    if config.CLOUD:
        extra = ["--no-sandbox",
                 "--disable-blink-features=AutomationControlled",
                 "--window-size=1280,800", "--lang=en-US",
                 "--enable-unsafe-swiftshader",
                 "--ignore-gpu-blocklist",
                 "--use-gl=angle", "--use-angle=swiftshader"]
    else:
        extra = []
    if config.CF_HEADLESS:
        extra.append("--headless=new")
    # 出口代理：配了 FARM_PROXY_URL 就让浏览器经它出去（云端从干净机房 IP 解 Turnstile）。
    # 本地不配=不加此参数，行为不变。
    if config.PROXY_URL:
        extra.append(f"--proxy-server={config.PROXY_URL}")

    browser, proc = await _start_browser_connect(bp, extra)
    if not browser:
        print("[local-turnstile] 浏览器启动/连接失败")
        return None
    try:
        if config.CLOUD:
            await _log_exit_ip(browser)
        tab = await browser.get(page_url)
        await tab.sleep(5)

        if config.CLOUD:
            try:
                wg = await tab.evaluate(_WEBGL_PROBE)
                print(f"[local-turnstile] WebGL: {wg}")
            except Exception as e:
                print(f"[local-turnstile] WebGL 探测失败: {e!r}")

        await tab.evaluate(_INJECT.replace("__SITEKEY__", sitekey))

        for i in range(token_timeout):
            await tab.sleep(1)
            try:
                token = await tab.evaluate("window.__cf_token")
            except Exception:
                token = None
            if token:
                return token
            if i == 10:
                try:
                    err = await tab.evaluate("window.__cf_err")
                except Exception:
                    err = None
                if err:
                    print(f"[local-turnstile] 渲染失败: {err}")
                    return None
        # 超时未拿到 token：补一条诊断，便于区分「widget 没渲染」与
        # 「渲染了但 CF 不放行(数据中心 IP 被判低分 / 浏览器指纹被判机器人)」。
        try:
            err = await tab.evaluate("window.__cf_err")
        except Exception:
            err = None
        print(f"[local-turnstile] {token_timeout}s 未取得 token"
              f"（__cf_err={err!r}；widget 已渲染却拿不到 token = IP 或浏览器指纹被 Cloudflare 判低分）")
        return None
    except Exception as e:
        print(f"[local-turnstile] 异常: {e!r}")
        return None
    finally:
        try:
            browser.stop()
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass
