@echo off
chcp 65001 >nul
title Oracle 轻量客户端 - 调试模式（保留控制台，能看到报错）
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m oracle_client
) else (
    python -m oracle_client
)

echo.
echo 程序已退出（若上面有报错信息，请把内容反馈给开发者）。
pause
