@echo off
setlocal enabledelayedexpansion

set "DEFAULT_TIMEOUT=3"
set "MIMO_DISABLE_PROXY=1"
set "MIMO_PROXY_DEBUG=0"

cls
echo.
echo ==============================================
echo        MiMo 代理启动器 v2.5
echo ==============================================
echo.

echo 正在读取系统环境变量...
if defined MIMO_API_KEY (
    set "API_KEY_DISPLAY=!MIMO_API_KEY:~0,8!****************"
    echo   已找到 API Key: !API_KEY_DISPLAY!
) else (
    echo   警告：未找到 MIMO_API_KEY 环境变量！
    echo   请先设置环境变量。
    pause
    exit /b 1
)

echo.

:choose_proxy
echo 禁用系统代理？(直接连接上游服务器)
echo   [1] 是 (禁用代理，默认)
echo   [2] 否 (使用系统代理)
choice /c 12 /t !DEFAULT_TIMEOUT! /d 1 /m "等待 !DEFAULT_TIMEOUT! 秒自动选择..."
set "CHOICE=!errorlevel!"

if "!CHOICE!"=="1" (
    set "MIMO_DISABLE_PROXY=1"
    echo   已选择：禁用代理
) else (
    set "MIMO_DISABLE_PROXY=0"
    echo   已选择：使用系统代理
)

echo.

:choose_debug
echo 启用调试模式？(显示详细日志)
echo   [1] 是
echo   [2] 否 (默认)
choice /c 12 /t !DEFAULT_TIMEOUT! /d 2 /m "等待 !DEFAULT_TIMEOUT! 秒自动选择..."
set "CHOICE=!errorlevel!"

if "!CHOICE!"=="1" (
    set "MIMO_PROXY_DEBUG=1"
    echo   已选择：启用调试模式
) else (
    set "MIMO_PROXY_DEBUG=0"
    echo   已选择：禁用调试模式
)

echo.
echo ==============================================
echo 启动配置：
echo   API Key: !API_KEY_DISPLAY!
echo   禁用代理: !MIMO_DISABLE_PROXY!
echo   调试模式: !MIMO_PROXY_DEBUG!
echo ==============================================
echo.

@REM :confirm_start
@REM echo 确认启动代理？
@REM echo   [Y] 是 (默认)
@REM echo   [N] 否
@REM choice /c YN /t !DEFAULT_TIMEOUT! /d Y /m "等待 !DEFAULT_TIMEOUT! 秒自动启动..."
@REM set "CHOICE=!errorlevel!"

@REM if "!CHOICE!"=="1" (
@REM     goto :start_proxy
@REM ) else (
@REM     echo 启动已取消。
@REM     pause
@REM     exit /b
@REM )

:start_proxy
echo.
echo 正在启动 MiMo 代理...
echo 按 Ctrl+C 停止
echo.
python xm_proxy_v2.5.py
pause
exit /b
