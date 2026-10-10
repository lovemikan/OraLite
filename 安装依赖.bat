@echo off
chcp 65001 >nul
title 安装依赖 - Oracle 轻量客户端
cd /d "%~dp0"

echo ============================================================
echo   Oracle 轻量客户端 —— 依赖安装
echo ============================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没有找到 Python。
    echo 请先安装 Python 3.9 及以上版本（安装时务必勾选 Add Python to PATH），
    echo 然后重新运行本脚本。下载地址：https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

python -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [警告] 当前 Python 缺少 tkinter（界面库）。
    echo 请重新运行 Python 安装程序，选择 Modify，确认勾选了 "tcl/tk and IDLE"。
    echo.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] 正在创建独立的虚拟环境 .venv ...
    python -m venv .venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败。
        pause
        exit /b 1
    )
) else (
    echo [1/3] 已存在虚拟环境 .venv，跳过创建。
)

echo [2/3] 升级 pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q

echo [3/3] 安装依赖包（oracledb / openpyxl / ttkbootstrap / pillow）...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [错误] 依赖安装失败，请检查网络或代理设置后重试。
    pause
    exit /b 1
)

echo.
".venv\Scripts\python.exe" -c "import tkinter, oracledb, openpyxl, ttkbootstrap; print('[自检] 依赖检查通过')"
if errorlevel 1 (
    echo [错误] 依赖检查未通过。
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   安装完成！现在可以双击 “启动客户端.bat” 运行程序。
echo ============================================================
echo.
pause
