@echo off
chcp 65001 >nul
title Oracle 轻量客户端 - 便携目录版打包
cd /d "%~dp0"

echo ============================================================
echo   Oracle 轻量客户端 · 打包成“目录版”
echo ============================================================
echo.
echo   目录版 = dist\Oracle轻量客户端-便携目录版\ 整个文件夹：
echo     · 启动比单文件版快（不用每次解包）
echo     · 几乎不会被杀毒软件误报
echo     · 缺点是必须整个文件夹一起拷贝，不能只拷一个 exe
echo.

if not exist ".venv\Scripts\python.exe" (
    call "安装依赖.bat"
)

".venv\Scripts\python.exe" -u tools\build_exe.py --mode dir --target dir
if errorlevel 1 goto failed

echo.
echo ============================================================
echo   完成！把 dist\Oracle轻量客户端-便携目录版 整个文件夹
echo   拷到目标电脑，运行里面的 exe 即可。
echo ============================================================
explorer "%~dp0dist"
echo.
pause
exit /b 0

:failed
echo.
echo 打包失败，请看上面错误信息。
pause
exit /b 1
