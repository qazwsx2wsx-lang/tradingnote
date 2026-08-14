@echo off
setlocal
cd /d "%~dp0"

rem ============================================================
rem  tradingnote GUI launcher (Windows; counterpart of the mac .command)
rem  First run creates .venv and installs requirements.txt.
rem
rem  Pinned to Python 3.12 on purpose: Python 3.14 ships OpenSSL 3.5,
rem  which rejects the TWSE government TLS cert (missing Subject Key
rem  Identifier), breaking every urllib fetch. 3.12 uses OpenSSL 3.0,
rem  matching the original macOS environment.
rem  NOTE: keep this file ASCII-only. cmd.exe misparses non-ASCII .bat.
rem ============================================================

set "VPY=.venv\Scripts\python.exe"

if not exist "%VPY%" (
    echo [tradingnote] First run: creating the Python 3.12 environment...
    py -3.12 --version >nul 2>nul
    if errorlevel 1 (
        echo.
        echo [tradingnote] Python 3.12 was not found. Install it first:
        echo     Option 1 - run in a terminal:   py install 3.12
        echo     Option 2 - download from https://www.python.org/downloads/
        echo.
        pause
        exit /b 1
    )
    py -3.12 -m venv ".venv"
    if not exist "%VPY%" (
        echo [tradingnote] Failed to create the virtual environment.
        pause
        exit /b 1
    )
    "%VPY%" -m pip install --upgrade pip
    "%VPY%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [tradingnote] Dependency install failed. Check your network and retry.
        pause
        exit /b 1
    )
    echo [tradingnote] Environment ready.
)

"%VPY%" tradingnote_gui.py
if errorlevel 1 pause
endlocal
