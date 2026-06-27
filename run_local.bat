@echo off
chcp 65001 >nul
cd /d "%~dp0"

:: 本地连 Telegram 需要走代理（mihomo 端口 10810）
set TG_PROXY=socks5://127.0.0.1:10810

:: 本地模式：不设 FARM_CLOUD，auth.py 会用 DrissionPage 过 CF
:: .session_string 已在项目目录，config.py 自动读取

python run_once.py

:: 非交互式运行完自动退出，不暂停
