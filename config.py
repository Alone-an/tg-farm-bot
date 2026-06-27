"""
config.py — 全局配置。已根据抓包结果填好 example.com 的真实地址。
敏感凭据仍从环境变量 / .env 读取。
"""
import os

from dotenv import load_dotenv

try:
    load_dotenv()
except ValueError as e:
    # Windows 下单个环境变量超过 32767 字符时，os.environ 赋值会抛 ValueError。
    # 正常情况下不该发生（超长串应放独立文件，见 _read_tg_session）；这里兜底
    # 跳过，避免一项超长就让整个程序无法启动。
    print(f"[config] 警告：.env 加载被跳过（{e}）。"
          "请把超长变量(如 TG_SESSION)移到独立文件，不要放进 .env。")


def _read_tg_session():
    """读取 Telegram 登录态（base64 的 .session）。

    优先级：环境变量 TG_SESSION（云端 CI/Secret 注入，Linux 无长度限制）
           > TG_SESSION_FILE 指向的文件
           > 项目目录下的 .session_string

    放独立文件是为了绕开 Windows 单个环境变量 32767 字符上限——base64 的
    .session 约 38KB，直接写进 .env 会让 load_dotenv() 在赋值时崩溃。
    """
    val = os.getenv("TG_SESSION", "").strip()
    if val:
        return val
    path = os.getenv("TG_SESSION_FILE", "").strip() or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), ".session_string")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except (FileNotFoundError, OSError):
        return ""

# ---- Telegram 凭据（在 https://my.telegram.org 申请）----
API_ID = os.getenv("TG_API_ID")
API_HASH = os.getenv("TG_API_HASH")

# ---- 云端 / 无人值守模式 ----
# GitHub Actions 里设 FARM_CLOUD=1：跳过 DrissionPage 过 CF（云端无浏览器）。
CLOUD = os.getenv("FARM_CLOUD", "").strip().lower() in {"1", "true", "yes", "on"}
# Telegram 登录态：本地从 .session_string 文件读，云端从 Secret 注入 TG_SESSION 环境变量。
TG_SESSION = _read_tg_session()


# ---- 本地代理（仅用于本地连 Telegram）----
# 本地(大陆)直连 Telegram 的 MTProto 数据中心通常不通，且 Telethon 不走系统 HTTP 代理，
# 必须显式传 proxy。设 TG_PROXY=socks5://127.0.0.1:10808（或简写 host:port）即可；
# 云端(GitHub Actions 海外 IP)不设此项 -> PROXY=None -> 直连，两端互不影响。
def _parse_proxy(val):
    """把 socks5://host:port / http://host:port / host:port 解析成 Telethon 的 proxy 元组。"""
    val = (val or "").strip()
    if not val:
        return None
    import urllib.parse
    u = urllib.parse.urlparse(val if "://" in val else "socks5://" + val)
    kind = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4",
            "http": "http", "https": "http"}.get((u.scheme or "socks5").lower(), "socks5")
    return (kind, u.hostname or "127.0.0.1", u.port or 1080)


PROXY = _parse_proxy(os.getenv("TG_PROXY"))


# ---- 全流量出口代理（HTTP/HTTPS/WS/浏览器统一走它）----
# 与上面 TG_PROXY 完全独立：TG_PROXY 只让 Telethon 连 Telegram；FARM_PROXY_URL 是给
# 「目标游戏 业务流量」（curl_cffi REST + websockets WS + nodriver 过 Turnstile）用的统一出口。
# 用途：云端 GitHub Actions 的机房 IP（Azure 段）被 Cloudflare Turnstile 判低分过不了，
# 经 mihomo 把流量从干净的 AWS 新加坡/香港机房 IP 出去即可过（本地 probe 已实测 AWS SG01 通）。
# 本地不设 -> PROXY_URL="" -> 三通道全直连，行为与接入前字节级一致；
# 云端 daily.yml 起 mihomo 后设 FARM_PROXY_URL=http://127.0.0.1:7890 即全量走代理。
# 形如 http://127.0.0.1:7890（mihomo mixed-port，HTTP CONNECT，wss 也走它）。
PROXY_URL = os.getenv("FARM_PROXY_URL", "").strip()


def proxies():
    """curl_cffi/requests 风格的 proxies 字典；未配置出口代理时返回 None（=直连）。"""
    return {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None


# ---- 过 Cloudflare 的浏览器配置（仅本地；云端 CLOUD=1 跳过浏览器）----
# 无头模式：CF 对无头检测较严、成功率偏低，默认 False=有头；无显示器/纯静默环境可设
# FARM_CF_HEADLESS=1 启用无头。
CF_HEADLESS = os.getenv("FARM_CF_HEADLESS", "").strip().lower() in {"1", "true", "yes", "on"}
# 强制指定浏览器可执行文件路径（Chrome/Edge）；留空则自动探测（Chrome 优先、回退 Edge）。
CF_BROWSER_PATH = os.getenv("CF_BROWSER_PATH", "").strip()

# ---- 目标 bot ----
BOT_USERNAME = os.getenv("BOT_USERNAME", "farmtg")

# ---- 真实地址（抓包确认）----
# Mini App 页面地址：用于 DrissionPage 过 CF 取 cookie，也用作 Referer/Origin
MINI_APP_URL = os.getenv("MINI_APP_URL", "https://farmtg.top/")
# REST 接口根地址（不含末尾斜杠）
BASE_API_URL = os.getenv("BASE_API_URL", "https://farmtg.top")
# WebSocket 地址（核心玩法走这里）
WS_URL = os.getenv("WS_URL", "wss://farmtg.top/api/game/ws")

# ---- 操作延迟（秒）----
DELAY_MIN = 1.5
DELAY_MAX = 4.0

# ---- 刷新间隔（分钟）----
# 登录拿到的 JWT 默认有效期很长（抓包里约 30 天），但 initData 有时效，
# 这里偏保守：到点重新走一遍 login 换新 token。
TOKEN_REFRESH_MIN = 720    # JWT 每 12 小时重新登录刷新一次
CF_COOKIE_REFRESH_MIN = 110  # CF cookies 刷新周期

# ---- 网络请求参数 ----
REQUEST_TIMEOUT = 15
RETRY_TIMES = 2
RETRY_INTERVAL = 3

# ---- WebSocket 参数 ----
WS_ACTION_TIMEOUT = 15   # 单个 WS action 等待 result 的超时秒数

# ---- 云端 run_once 收尾 / 防挂起超时（秒）----
# run_once 是一次性入口：跑完必须干净退出，别让好友循环网络等待 / WS 优雅关闭 /
# Telegram 断连卡住进程，导致 GitHub Actions 空耗到 job 级超时。三道兜底超时 + 看门狗。
RUN_ONCE_TIMEOUT = int(os.getenv("FARM_RUN_ONCE_TIMEOUT", "600"))      # 整轮(任务+好友)总预算
FRIEND_VISITS_TIMEOUT = int(os.getenv("FARM_FRIEND_TIMEOUT", "300"))   # 好友拜访循环单独预算
DISCONNECT_TIMEOUT = int(os.getenv("FARM_DISCONNECT_TIMEOUT", "15"))   # client.disconnect 兜底


# ---- 验证码打码服务（2captcha 兼容协议；CapSolver/YesCaptcha 等可换 base_url）----
def _truthy(v):
    return (v or "").strip().lower() in {"1", "true", "yes", "on"}


CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")
# 打码来源：local=本地浏览器免费解（默认，需 Chrome/Edge + DrissionPage）；
#           2captcha=付费打码（需 CAPTCHA_API_KEY）；auto=先 local，失败回退 2captcha。
CAPTCHA_PROVIDER = os.getenv("CAPTCHA_PROVIDER", "local").strip().lower()
# 启用：local/auto 默认开（不需 key）；2captcha 需有 key。显式 CAPTCHA_ENABLED=0/1 覆盖。
_CAPTCHA_ENABLED_ENV = os.getenv("CAPTCHA_ENABLED")
if _CAPTCHA_ENABLED_ENV:
    CAPTCHA_ENABLED = _truthy(_CAPTCHA_ENABLED_ENV)
else:
    CAPTCHA_ENABLED = CAPTCHA_PROVIDER in ("local", "auto") or bool(CAPTCHA_API_KEY)
CAPTCHA_API_BASE = os.getenv("CAPTCHA_API_BASE", "https://2captcha.com").rstrip("/")
CAPTCHA_TYPE = os.getenv("CAPTCHA_TYPE", "turnstile")  # turnstile|recaptcha_v2|hcaptcha
CAPTCHA_PAGE_URL = os.getenv("CAPTCHA_PAGE_URL", MINI_APP_URL)
CAPTCHA_POLL_TIMEOUT = int(os.getenv("CAPTCHA_POLL_TIMEOUT", "120"))   # 轮询结果总上限(秒)
CAPTCHA_POLL_INTERVAL = int(os.getenv("CAPTCHA_POLL_INTERVAL", "5"))   # 轮询间隔(秒)
# 协议字段映射（⚠️ 需用真实质询报文校准）——默认值对齐现有 ws_state_machine / 测试
CAPTCHA_SITEKEY_FIELD = os.getenv("CAPTCHA_SITEKEY_FIELD", "sitekey")
CAPTCHA_TOKEN_FIELD = os.getenv("CAPTCHA_TOKEN_FIELD", "captcha_token")
# 服务端「需要人机验证」的判定值（抓包确认：目标游戏 在 WS result / REST body 里返回 code=此值）
CAPTCHA_CHALLENGE_CODE = os.getenv("CAPTCHA_CHALLENGE_CODE", "NEED_HUMAN_VERIFICATION")
# 目标游戏 应用层 Turnstile（抓包自前端 index-*.js）：sitekey、token 提交端点、通过后免验证时长
CAPTCHA_SITEKEY = os.getenv("CAPTCHA_SITEKEY", "0x4AAAAAADmotcK0lqq38R89")
CAPTCHA_VERIFY_PATH = os.getenv("CAPTCHA_VERIFY_PATH", "/api/game/human-verify")
# 通过后免再验证时长：服务端在 human-verify 回包里给真实值(expires_in，抓包=900s)，
# 这里仅作回包缺该字段时的兜底（与前端一致留 5s 余量在 human_verify 里扣）。
CAPTCHA_PASS_TTL = int(os.getenv("CAPTCHA_PASS_TTL", "900"))
# human-verify 成功后服务端发回的「通行证」机制（抓包自前端 index-*.js 确认）：
#   回包 body: {"human_pass":"hp_...","expires_in":900}
#   之后每个 REST 请求带 header  X-Human-Pass: <human_pass>
#   之后每个 WS 农场动作(harvest/plant)在 data 里带  human_pass: <human_pass>
CAPTCHA_PASS_RESP_FIELD = os.getenv("CAPTCHA_PASS_RESP_FIELD", "human_pass")   # 回包里通行证字段名
CAPTCHA_EXPIRES_FIELD = os.getenv("CAPTCHA_EXPIRES_FIELD", "expires_in")       # 回包里有效期(秒)字段名
CAPTCHA_PASS_HEADER = os.getenv("CAPTCHA_PASS_HEADER", "X-Human-Pass")         # REST 携带通行证的请求头名
CAPTCHA_PASS_MARGIN = int(os.getenv("CAPTCHA_PASS_MARGIN", "5"))               # 通行证提前过期的安全余量(秒)


def validate():
    """启动时校验关键配置。"""
    missing = []
    if not API_ID:
        missing.append("TG_API_ID")
    if not API_HASH:
        missing.append("TG_API_HASH")
    if missing:
        raise RuntimeError(
            f"缺少必要配置: {', '.join(missing)}。请在 .env 或环境变量中设置。"
        )
    try:
        int(API_ID)
    except (TypeError, ValueError):
        raise RuntimeError("TG_API_ID 必须是数字。")

    if CAPTCHA_ENABLED and not CAPTCHA_API_KEY:
        print("[config] 警告：打码已启用(CAPTCHA_ENABLED)但未设置 CAPTCHA_API_KEY，"
              "遇到验证码将降级为告警并放弃该请求。")
