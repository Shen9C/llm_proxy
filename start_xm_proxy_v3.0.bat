@echo off
setlocal enabledelayedexpansion

set "DEFAULT_TIMEOUT=5"
set "MIMO_DISABLE_PROXY=1"
set "MIMO_PROXY_DEBUG=0"
set "MIMO_FALLBACK_STRATEGY=strip"

cls
echo.
echo ==============================================
echo        MiMo 代理启动器 v3.0
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

if defined MIMO_PROXY_PORT (
    echo   监听端口: !MIMO_PROXY_PORT!
) else (
    echo   监听端口: 8765 (默认)
)

if defined MIMO_BASE_URL (
    echo   上游地址: !MIMO_BASE_URL!
) else (
    echo   上游地址: https://api.xiaomimimo.com (默认)
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

:choose_strategy
echo 选择缓存缺失回退策略：
echo   [1] strip - 移除 tool_calls（默认）
echo   [2] error - 返回 400 错误
echo   [3] disable_thinking - 禁用 thinking 模式
choice /c 123 /t !DEFAULT_TIMEOUT! /d 1 /m "等待 !DEFAULT_TIMEOUT! 秒自动选择..."
set "CHOICE=!errorlevel!"

if "!CHOICE!"=="1" (
    set "MIMO_FALLBACK_STRATEGY=strip"
    echo   已选择：strip
) else if "!CHOICE!"=="2" (
    set "MIMO_FALLBACK_STRATEGY=error"
    echo   已选择：error
) else (
    set "MIMO_FALLBACK_STRATEGY=disable_thinking"
    echo   已选择：disable_thinking
)

echo.
echo ==============================================
echo 启动配置：
echo   API Key: !API_KEY_DISPLAY!
echo   禁用代理: !MIMO_DISABLE_PROXY!
echo   调试模式: !MIMO_PROXY_DEBUG!
echo   回退策略: !MIMO_FALLBACK_STRATEGY!
echo ==============================================
echo.

:main_menu
echo 请选择操作：
echo   [1] 启动代理
echo   [2] 查看日志
echo   [3] 停止代理
echo   [4] 退出
choice /c 1234 /t !DEFAULT_TIMEOUT! /d 1 /m "等待 !DEFAULT_TIMEOUT! 秒自动选择..."
set "CHOICE=!errorlevel!"

if "!CHOICE!"=="1" (
    goto :start_proxy
) else if "!CHOICE!"=="2" (
    goto :view_logs
) else if "!CHOICE!"=="3" (
    goto :stop_proxy
) else (
    echo 退出程序...
    exit /b
)

:start_proxy
echo.
echo 正在启动 MiMo 代理 v3.0...
echo 按 Ctrl+C 停止代理，返回按任意键
echo.

if "!MIMO_DISABLE_PROXY!"=="1" (
    set "NO_PROXY=*"
    set "http_proxy="
    set "https_proxy="
    echo 已禁用系统代理
)

start "MiMo Proxy v3.0" python xm_proxy_v3.0.py
echo 代理已启动，请等待几秒后检查状态...
timeout /t 3 /nobreak >nul
echo.
echo 健康检查：
curl -s http://localhost:8765/health 2>nul | findstr "ok" >nul
if !errorlevel! equ 0 (
    echo   [OK] 代理运行正常
) else (
    echo   [ERR] 代理启动失败，请检查日志
)
echo.
goto :main_menu

:view_logs
echo.
echo ==============================================
echo 最近日志（最后 50 行）
echo ==============================================
echo.
if exist mimo_proxy.log (
    powershell -Command "Get-Content -Path 'mimo_proxy.log' -Tail 50"
) else (
    echo 日志文件不存在，代理可能尚未启动
)
echo.
echo 按任意键返回菜单...
pause >nul
goto :main_menu

:stop_proxy
echo.
echo 正在停止 MiMo 代理...
taskkill /f /im python.exe /fi "windowtitle eq MiMo Proxy v3.0" 2>nul
if !errorlevel! equ 0 (
    echo   [OK] 代理已停止
) else (
    echo   [WARN] 未找到运行中的代理进程
)
echo.
goto :main_menu

:exit
echo 退出程序...
exit /b
