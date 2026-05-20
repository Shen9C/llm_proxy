@echo off
setlocal enabledelayedexpansion

set "DEFAULT_TIMEOUT=5"
set "MIMO_DISABLE_PROXY=1"
set "MIMO_PROXY_DEBUG=0"

cls
echo.
echo ==============================================
echo        MiMo Proxy Launcher v2.5
echo ==============================================
echo.

echo Reading system environment variables...
if defined MIMO_API_KEY (
    set "API_KEY_DISPLAY=%MIMO_API_KEY:~0,8%****************"
    echo   Found API Key: !API_KEY_DISPLAY!
) else (
    echo   WARNING: MIMO_API_KEY environment variable not found!
    echo   Please set the environment variable first.
    pause
    exit /b 1
)

echo.

:choose_proxy
echo Disable system proxy? (Direct connection to upstream)
echo   [1] Yes (Disable proxy, default)
echo   [2] No (Use system proxy)
choice /c 12 /t %DEFAULT_TIMEOUT% /d 1 /m "Waiting %DEFAULT_TIMEOUT% seconds for automatic selection..."
set "CHOICE=%errorlevel%"

if "!CHOICE!"=="1" (
    set "MIMO_DISABLE_PROXY=1"
    echo   Selected: Disable proxy
) else (
    set "MIMO_DISABLE_PROXY=0"
    echo   Selected: Use system proxy
)

echo.

:choose_debug
echo Enable debug mode? (Show detailed logs)
echo   [1] Yes
echo   [2] No (default)
choice /c 12 /t %DEFAULT_TIMEOUT% /d 2 /m "Waiting %DEFAULT_TIMEOUT% seconds for automatic selection..."
set "CHOICE=%errorlevel%"

if "!CHOICE!"=="1" (
    set "MIMO_PROXY_DEBUG=1"
    echo   Selected: Enable debug mode
) else (
    set "MIMO_PROXY_DEBUG=0"
    echo   Selected: Disable debug mode
)

echo.
echo ==============================================
echo Launch Configuration:
echo   API Key: !API_KEY_DISPLAY!
echo   Disable Proxy: !MIMO_DISABLE_PROXY!
echo   Debug Mode: !MIMO_PROXY_DEBUG!
echo ==============================================
echo.

@REM :confirm_start
@REM echo Confirm to start proxy?
@REM echo   [Y] Yes (default)
@REM echo   [N] No
@REM choice /c YN /t %DEFAULT_TIMEOUT% /d Y /m "Waiting %DEFAULT_TIMEOUT% seconds for automatic start..."
@REM set "CHOICE=%errorlevel%"

@REM if "!CHOICE!"=="1" (
@REM     goto :start_proxy
@REM ) else (
@REM     echo Launch cancelled.
@REM     pause
@REM     exit /b
@REM )

:start_proxy
echo.
echo Starting MiMo Proxy...
echo Press Ctrl+C to stop
echo.
python xm_proxy_v2.5.py
pause
exit /b