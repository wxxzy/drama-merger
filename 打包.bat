@echo off
chcp 936 >nul 2>&1
setlocal
cd /d "%~dp0"
title 短剧合并工具 - 打包

REM ============================================================
REM  打包成免安装独立程序
REM  产物：dist\短剧合并工具\  整个文件夹可直接拷到别的电脑用
REM ============================================================

echo ============================================================
echo  正在打包短剧合并工具（首次约 1 分钟）
echo ============================================================
echo.

set "PYCMD="
py -3.11 -c "import sys" >nul 2>&1 && set "PYCMD=py -3.11"
if not defined PYCMD (
    python -c "import sys" >nul 2>&1 && set "PYCMD=python"
)
if not defined PYCMD (
    echo  [错误] 未找到 Python，无法打包。请先安装 Python 3.11。
    pause
    goto :eof
)

echo [1/3] 检查 PyInstaller...
%PYCMD% -c "import PyInstaller" >nul 2>&1
if errorlevel 1 (
    echo       未安装，正在安装...
    %PYCMD% -m pip install pyinstaller -i https://pypi.tuna.tsinghua.edu.cn/simple
    if errorlevel 1 (
        echo  [错误] PyInstaller 安装失败，请检查网络。
        pause
        goto :eof
    )
)

echo [2/3] 清理旧产物...
if exist "dist" rmdir /s /q "dist" 2>nul
if exist "build" rmdir /s /q "build" 2>nul

echo [3/3] 开始打包...
%PYCMD% -m PyInstaller --clean --noconfirm build.spec

if errorlevel 1 (
    echo.
    echo  [错误] 打包失败，请把上面的输出发给开发者。
    pause
    goto :eof
)

echo.
echo ============================================================
echo  打包完成！
echo  产物目录：dist\短剧合并工具\
echo     - 短剧合并工具.exe          双击启动图形界面
echo     - 短剧合并工具-命令行.exe    拖目录或命令行使用
echo  整个文件夹可直接拷贝到其他电脑，无需安装 Python。
echo ============================================================
echo.
pause
