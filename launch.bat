@echo off
setlocal

set ROOT=C:\Users\mmuel\Documents\Gambling
set BACKEND=%ROOT%\backend
set FRONTEND_BUILD=%ROOT%\frontend\build\index.html
set PYTHON_EXE=%BACKEND%\venv\Scripts\python.exe

if not exist "%PYTHON_EXE%" (
  echo Backend virtual environment not found.
  echo Create it first:
  echo   cd /d %BACKEND%
  echo   python -m venv venv
  echo   venv\Scripts\activate
  echo   pip install -r requirements.txt
  pause
  exit /b 1
)

if not exist "%BACKEND%\.env" (
  echo backend\.env not found.
  echo Add your Odds API key to:
  echo   %BACKEND%\.env
  echo Example:
  echo   ODDS_API_KEY=your_key_here
  pause
  exit /b 1
)

if not exist "%FRONTEND_BUILD%" (
  echo Frontend build not found.
  echo Build it first:
  echo   cd /d %ROOT%\frontend
  echo   npm install
  echo   npm run build
  pause
  exit /b 1
)

PowerShell -Command "Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }"
timeout /t 1 /nobreak >nul

cd /d "%BACKEND%"
start /min "" cmd /c ""%PYTHON_EXE%" run.py"
timeout /t 2 /nobreak >nul
start http://localhost:8000
