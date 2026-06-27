# tg-farm-bot

某 Telegram Mini App 农场游戏的自动化脚本。每天自动完成日常 / 周常 / 成就 / 新手任务、
拜访好友偷菜、卖菜与扩地、领取邮件奖励。

REST + WebSocket 双通道架构:好友 / 任务 / 邮件等走 REST 接口,农场实时动作走 WebSocket
状态机;用 DrissionPage + curl_cffi 处理 Cloudflare 人机验证(Turnstile)。

> ⚠️ 本脚本设计为**本地运行**:DrissionPage 需要本地真实 Chromium 浏览器来过 CF。
> 仓库内不含任何账号 / 登录态 / 密钥,接口地址与目标游戏需自行抓包填入(见下文「配置」)。

## 目录结构

```
.
├── main.py              # 本地常驻入口:登录 → 跑一轮 → 进入 scheduler 每天定时
├── run_once.py          # 无人值守入口:连接 → 跑一轮 → 退出(配合外部 cron)
├── config.py            # 全局配置,全部从环境变量读取
├── auth.py              # 过 CF + 取 initData + 换 JWT + 鉴权头组装 + 缓存刷新
├── api.py               # curl_cffi REST 接口封装 + 重试 + safe_call 兜底
├── endpoints.py         # REST 路径与 WS 动作常量
├── tasks.py             # run_all_tasks 日常任务主流程(含卖菜 / 扩地)
├── friends.py           # run_friend_visits 好友拜访偷菜
├── ws_session.py        # WebSocket 会话封装
├── ws_state_machine.py  # WS 高可用状态机(重连 / 风控节流 / 动作排队)
├── human_verify.py      # 应用层 Turnstile 人机验证自动过
├── captcha_solver.py    # 打码:本地解 / 第三方打码平台(可选)
├── local_turnstile.py   # 本地 nodriver 解 Turnstile
├── scheduler.py         # asyncio 定时调度
├── fake_server.py       # 测试用:可控的 WS 协议模拟服务器
├── test_*.py            # pytest 测试
├── requirements.txt
└── .env.example         # 配置模板
```

## 安装

```bash
pip install -r requirements.txt
```

需本地装有 Chrome / Chromium 供 DrissionPage 与 nodriver 驱动。

## 配置

复制 `.env.example` 为 `.env` 并填写(接口地址 / 目标游戏均需自行抓包确认):

```
TG_API_ID=          # https://my.telegram.org 申请
TG_API_HASH=
BOT_USERNAME=       # 目标游戏的 Telegram bot username(不含 @)
MINI_APP_URL=       # Mini App 页面地址(用于过 CF、作 Referer/Origin)
BASE_API_URL=       # REST 接口根地址,不含末尾斜杠
WS_URL=             # WebSocket 地址,如 wss://<host>/api/game/ws
```

Telegram 登录态(base64 的 `.session`,约 38KB)**不要**写进 `.env`(会超过 Windows 单个
环境变量长度上限)。本地放到独立文件 `.session_string`,`config.py` 会自动读取;无人值守
环境可作为 Secret 注入名为 `TG_SESSION` 的环境变量。

`config.py` 里还有一批可选 `FARM_*` / `CAPTCHA_*` 环境变量(代理出口、超时预算、打码方式、
人机验证协议字段名等),均有默认值,按需覆盖即可。

## 运行

```bash
# 本地常驻(首次会交互式登录 Telegram,生成 .session_string 后续免登录)
python main.py

# 或:跑一轮就退出(无人值守 / 调试)
python run_once.py
```

Windows 可直接双击 `run_local.bat`(其中示例性地把 Telegram 连接走本地 socks5 代理,
按自己环境改 `TG_PROXY` 端口或删掉该行)。

## 抓包适配说明

接口路径、鉴权方式、返回字段名因目标游戏而异。代码用了防御性工具(`_as_list` / `_get` /
`_is`)兼容多种字段命名;`endpoints.py` 集中管理路径与 WS 动作名。换目标游戏时:

1. 用抓包工具(Fiddler / Charles / mitmproxy)抓 Mini App 真实请求;
2. 在 `.env` 填好 `BASE_API_URL` / `WS_URL` / `MINI_APP_URL` / `BOT_USERNAME`;
3. 按 `endpoints.py` 与各模块注释核对路径、方法、body 字段;
4. 人机验证相关的 `CAPTCHA_SITEKEY` / `CAPTCHA_VERIFY_PATH` 等按实际页面填。

## 错误处理

- **401**:自动 `refresh_auth(force)` 刷新鉴权后重试一次
- **429**:等待后重试
- **CF 拦截**(非 JSON / 含 `cf-ray`):重新过 CF 后重试
- **网络超时**:间隔重试,有上限
- **偷菜冷却**(403 / 自定义 code):跳过该地块
- WS 断线由状态机自动重连;所有最终失败都记录并跳过,不抛未捕获异常

## 测试

```bash
pip install -r requirements-dev.txt
pytest -q
```

> 注:`test_proxy_wiring.py` 中 2 个用例需本地装有 nodriver 可驱动的 Chrome,
> 无浏览器环境会失败,与脚本逻辑无关。

## 免责声明

仅供学习与技术研究。是否使用、以及由此产生的一切后果由使用者自行承担,
请遵守目标服务的用户协议与当地法律。

---

作者:yl
