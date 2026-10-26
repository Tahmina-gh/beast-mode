@echo off
setlocal
title BEAST MODE
cd /d "%~dp0"

rem ---------- find Python ----------
set PY=python
where python >nul 2>&1
if not errorlevel 1 goto havepy
where py >nul 2>&1
if errorlevel 1 goto nopy
set PY=py -3
:havepy

rem ---------- 1. virtual environment (first run only) ----------
if exist venv\Scripts\activate.bat goto venvok
echo [1/4] Creating virtual environment...
%PY% -m venv venv
if errorlevel 1 goto fail
:venvok
call venv\Scripts\activate.bat

rem ---------- 2. packages (first run only) ----------
if exist venv\.deps_installed goto depsok
echo [2/4] Installing packages - this takes a minute, first run only...
python -m pip install --upgrade pip >nul 2>&1
pip install -r requirements.txt
if errorlevel 1 goto fail
echo ok> venv\.deps_installed
:depsok

rem ---------- 3. settings file with a fresh secret key (first run only) ----------
if exist .env goto envok
echo [3/4] Creating .env with a new secret key...
copy /y .env.example .env >nul
python -c "import re,secrets;p='.env';s=open(p).read();s=re.sub(r'(?m)^AUTH_SECRET=.*$','AUTH_SECRET='+secrets.token_hex(32),s);open(p,'w').write(s)"
:envok

rem ---------- 4. database, admin account, starter data (first run only) ----------
if not exist instance mkdir instance
if exist instance\.setup_done goto run
echo.
echo [4/4] First-time setup
echo Create your admin account (you will use it to log in).
set /p ADMIN_EMAIL=Admin email: 
set /p ADMIN_PASSWORD=Admin password (min 8 characters, letters + numbers): 
flask --app app create-admin
flask --app app seed-baseline
echo.
choice /m "Also add demo data (fake members, trainers, classes) for testing"
if errorlevel 2 goto setupdone
flask --app app seed-demo
:setupdone
set ADMIN_PASSWORD=
echo done> instance\.setup_done

rem ---------- run ----------
:run
echo.
echo ==============================================================
echo   BEAST MODE is starting at  http://127.0.0.1:5000
echo   Admin login:  http://127.0.0.1:5000/login
echo   To stop the site: press Ctrl+C or close this window.
echo ==============================================================
echo.
start "" /min cmd /c "ping -n 5 127.0.0.1 >nul & start http://127.0.0.1:5000"
python app.py
pause
exit /b 0

:nopy
echo Python is not installed (or not on PATH).
echo Download it from https://www.python.org/downloads/ and tick "Add Python to PATH" during install.
pause
exit /b 1

:fail
echo.
echo Something went wrong during setup. Scroll up to read the error, then press any key.
pause
exit /b 1
