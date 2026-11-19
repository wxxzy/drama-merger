@echo off
chcp 936 >nul 2>&1
setlocal
cd /d "%~dp0"
title 短剧合并工具 - 命令行

REM ============================================================
REM  短剧合并工具 · 命令行模式
REM  用法1：把短剧目录直接拖到本文件图标上
REM  用法2：命令行执行  合并.bat "D://短剧//某剧"
REM ============================================================

set "DIR=%~1"
if "%DIR%"=="" (
    echo 用法：把短剧目录拖到本文件上，或在命令行执行：
    echo     %~nx0 "D://短剧//某剧"
    echo.
    set /p DIR="请输入短剧目录路径: "
)

if "%DIR%"=="" (
    echo 未提供目录，已取消。
    pause
    goto :eof
)

echo.
echo 待处理目录：%DIR%
echo.

REM ---- 优先用打包版 ----
if exist "dist\短剧合并工具\短剧合并工具-命令行.exe" (
    "dist\短剧合并工具\短剧合并工具-命令行.exe" merge "%DIR%" --subtitles
    goto :done
)

REM ---- 回退到系统 Python ----
set "PYCMD="
py -3.11 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.11"
if not defined PYCMD (
    python -c "import sys" >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
    python3 -c "import sys" >nul 2>&1 && set "PYCMD=python3"
)

if not defined PYCMD (
    echo  [错误] 未找到可用的 Python 环境。
    echo  请双击「打包.bat」生成免安装程序，或安装 Python 3.11。
    pause
    goto :eof
)

%PYCMD% cli.py merge "%DIR%" --subtitles

:done
echo.
pause
