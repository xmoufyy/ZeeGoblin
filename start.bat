@echo off
setlocal
rem ZeeGoblin launcher: uses .venv if present, checks setup, then starts the bot.
cd /d "%~dp0"

set "PY="
if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    where py >nul 2>nul && set "PY=py -3.12"
    if not defined PY (
        where python >nul 2>nul && set "PY=python"
    )
)
if not defined PY (
    echo [ZeeGoblin] Python 3.12+ was not found. Install it from https://www.python.org/downloads/
    echo             and tick "Add python.exe to PATH" during setup.
    goto :fail
)

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)"
if errorlevel 1 (
    echo [ZeeGoblin] Python 3.12 or newer is required.
    goto :fail
)

if not exist ".env" (
    echo [ZeeGoblin] No .env file found. Copy .env.example to .env and fill in your bot token,
    echo             server ID, owner ID and staff role IDs. See README.md.
    goto :fail
)

%PY% -c "import discord, sqlalchemy, aiosqlite, alembic, pydantic_settings" >nul 2>nul
if errorlevel 1 (
    echo [ZeeGoblin] Dependencies are missing. Run:
    echo             %PY% -m pip install -r requirements.txt
    goto :fail
)

echo [ZeeGoblin] Starting... press Ctrl+C to stop.
%PY% main.py
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo [ZeeGoblin] Exited with code %CODE%. Check the messages above and data\logs\zeegoblin.log
    goto :fail
)
endlocal
exit /b 0

:fail
pause
endlocal
exit /b 1
