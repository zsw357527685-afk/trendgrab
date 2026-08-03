@echo off
cd /d "%~dp0.."
title trend_grab - Public Mode

echo [1/2] Starting web server on port 8001...
taskkill /f /fi "WINDOWTITLE eq tg_web" >nul 2>&1
start "tg_web" C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe web\server.py
timeout /t 4 /nobreak >nul

echo [2/2] Starting frp tunnel...
echo.
echo Public URL: https://trend-grab.giant-starlly.com
echo Press Ctrl+C to stop.
echo.

:restart_frp
taskkill /f /im frpc.exe >nul 2>&1
C:\tools\frp\frpc.exe -c C:\tools\frp\frpc.toml

echo.
echo frp disconnected. Restarting in 3 seconds...
timeout /t 3 /nobreak >nul
goto restart_frp
