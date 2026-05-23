@echo off

echo.
echo Starting MiMo Proxy...
echo Press Ctrl+C to stop
echo.

set "MIMO_PROXY_DEBUG=1"
python xm_proxy_v3.0.py
