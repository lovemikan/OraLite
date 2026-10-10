@echo off
chcp 65001 >nul
title Oracle 轻量客户端 - 便携版打包
cd /d "%~dp0"

echo ============================================================
echo   Oracle 轻量客户端 · 打包成单个 exe
echo ============================================================
echo.
echo   本脚本会打包出 dist\Oracle轻量客户端.exe
echo   它自带 Python 运行环境和全部依赖，可拷贝到任意 Windows
echo   电脑（含未安装 Python 的机器）直接双击运行，无需安装。
echo.
echo   打包需要几分钟，请耐心等待...
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [1/2] 未发现虚拟环境，先安装依赖...
    call "安装依赖.bat"
) else (
    echo [1/2] 已存在虚拟环境，跳过安装依赖。
)
if errorlevel 1 goto failed

echo.
echo [2/2] 正在打包...
".venv\Scripts\python.exe" -u tools\build_exe.py --mode onefile
if errorlevel 1 goto failed

echo.
echo ============================================================
echo   打包完成！产物在 dist 目录：
echo     Oracle轻量客户端.exe          正式版
echo     Oracle轻量客户端^(调试版^).exe   出错时会显示控制台
echo ============================================================
explorer "%~dp0dist"
echo.
pause
exit /b 0

:failed
echo.
echo 打包失败，请看上面红色错误信息。
echo 若提示权限不足，请以普通用户身份重试，或关闭杀毒软件后再试。
echo.
pause
exit /b 1
