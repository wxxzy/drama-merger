@echo off
chcp 936 >nul 2>&1
setlocal
cd /d "%~dp0"
title 短剧合并工具 - 图形界面

REM ============================================================
REM  短剧合并工具 · 图形界面启动器
REM  优先使用打包好的独立程序（免安装、无需 Python）
REM ============================================================

if exist "dist\短剧合并工具\短剧合并工具.exe" (
    start "" "dist\短剧合并工具\短剧合并工具.exe"
    goto :eof
)
if exist "短剧合并工具.exe" (
    start "" "短剧合并工具.exe"
    goto :eof
)

echo 未找到打包版程序，改用系统 Python 启动...
echo.

call :try_python gui
if not errorlevel 1 goto :ok

echo.
echo  [错误] 未找到可用的 Python 环境。
echo.
echo  解决办法（任选其一）：
echo    1. 双击运行「打包.bat」，生成免安装独立程序（推荐）
echo    2. 安装 Python 3.11，安装时勾选 Add Python to PATH
echo.
pause
goto :eof

:ok
echo.
echo  程序已退出。

REM ---- 依次尝试 py 启动器 / python / python3 ----
:try_python
py -3.11 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    py -3.11 cli.py %1
    exit /b 0
)
python -c "import sys" >nul 2>&1
if not errorlevel 1 (
    python cli.py %1
    exit /b 0
)
python3 -c "import sys" >nul 2>&1
if not errorlevel 1 (
    python3 cli.py %1
    exit /b 0
)
exit /b 1
