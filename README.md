# tg-farm-bot

Telegram Mini App 农场游戏 **@farmtg**(farmtg.top)的自动化脚本。
每天自动:收获 / 补种(优先高级种子)/ 卖菜变现 / 扩地 / 拜访好友偷菜 / 除草除虫 /
领取任务与邮件奖励。REST + WebSocket 架构,自动处理 Cloudflare 人机验证(Turnstile)。

> 别人想用?**你只要填自己的 Telegram 凭据并登录一次即可**,游戏接口已内置,无需抓包。

---

## 🚀 快速开始(三步)

### 1. 准备环境

- 安装 **Python 3.10+**
- 安装 **Chrome / Edge 浏览器**(脚本用它过 Cloudflare 人机验证)
- 下载本仓库并安装依赖:

```bash
git clone https://github.com/Alone-an/tg-farm-bot
cd tg-farm-bot
pip install -r requirements.txt
```

### 2. 填自己的 Telegram 凭据

去 https://my.telegram.org 免费申请 `API_ID` 和 `API_HASH`(每个人用自己的),
然后复制配置模板并填入:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

编辑 `.env`,只需填两行:

```
TG_API_ID=12345678
TG_API_HASH=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

> 在大陆等需要代理才能连 Telegram 的网络,再加一行
> `TG_PROXY=socks5://127.0.0.1:10808`(改成你自己的代理端口)。

### 3. 运行

```bash
python main.py
```

**首次运行**会让你用**手机号 + Telegram 验证码**登录一次,成功后生成 `.session_string`,
之后免登录。登录后脚本立即跑一轮,并每天 08:00 自动执行。

只想跑一轮就退出(适合配合系统定时任务):

```bash
python run_once.py
```

Windows 用户也可直接双击 `run_local.bat`。

---

## ❓ 常见问题

- **连不上 Telegram / 卡在登录**:你的网络需要代理,在 `.env` 设 `TG_PROXY`。
- **人机验证过不去**:确保本地装了 Chrome 或 Edge;部分网络环境(尤其机房 IP)可能被
  Cloudflare 判定为机器人而失败,换用住宅网络通常可解。
- **我的账号 / 登录态会泄露吗**:不会。`.env` 和 `.session_string` 已被 `.gitignore`
  排除,绝不会被提交;每个人各用各的,互不影响。
- **想自动每天跑**:`main.py` 自带每日调度;或用系统计划任务(Windows 计划任务 /
  Linux cron)定时调 `python run_once.py`。

## ⚙️ 可选配置

`config.py` 里有一批可选环境变量(都有默认值,一般不用动):

- `TG_PROXY` —— Telegram 连接代理
- `FARM_PROXY_URL` —— 让游戏业务流量(REST/WS/浏览器)统一走某个出口代理
- `CAPTCHA_PROVIDER` —— 打码方式:`local`(默认,本地浏览器免费过)/ `2captcha`(付费)/ `auto`
- `FARM_RUN_ONCE_TIMEOUT` 等 —— 各类超时预算

## 🧩 想给别的 Mini App 游戏用?

在 `.env` 里覆盖 `BOT_USERNAME` / `MINI_APP_URL` / `BASE_API_URL` / `WS_URL`,
并按 `endpoints.py` 与各模块注释核对该游戏的真实接口路径与字段名即可。

## 📁 目录结构

```
main.py / run_once.py   入口(常驻 / 跑一轮即退出)
config.py               配置(全部从环境变量读取,游戏接口为默认值)
auth.py                 过 CF + 取 initData + 换 JWT + 鉴权
api.py / endpoints.py   REST 接口封装 + 路径/动作常量
tasks.py / friends.py   日常任务主流程 / 好友拜访偷菜
ws_session.py / ws_state_machine.py   WebSocket 高可用状态机
human_verify.py / captcha_solver.py / local_turnstile.py   人机验证
scheduler.py            定时调度
test_*.py               pytest 测试
```

## ⚠️ 免责声明

本项目仅供学习与技术研究。使用本脚本可能违反目标游戏的用户协议、导致账号被封禁
或其他后果,是否使用及一切风险与责任由使用者自行承担。请遵守目标服务条款与当地法律。

---

作者:yl
