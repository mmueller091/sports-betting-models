@echo off
:: Started by Task Scheduler daily — boots the backend without opening a browser.
PowerShell -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul
cd /d C:\Users\mmuel\Documents\Gambling\backend
start /min "" "C:\Users\mmuel\Documents\Gambling\backend\venv\Scripts\python.exe" run.py
