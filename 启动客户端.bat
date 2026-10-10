@echo off
chcp 65001 >nul
title Oracle 轻量客户端
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" goto HAVE_VENV
echo 尚未安装依赖，正在自动安装...
call "安装依赖.bat"
if not exist ".venv\Scripts\python.exe" exit /b 1

:HAVE_VENV
".venv\Scripts\python.exe" -c "import ttkbootstrap, oracledb, openpyxl" >nul 2>nul
if errorlevel 1 (
    echo 依赖缺失，正在重新安装...
    call "安装依赖.bat"
)

if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" -m oracle_client
) else (
    ".venv\Scripts\python.exe" -m oracle_client
)
