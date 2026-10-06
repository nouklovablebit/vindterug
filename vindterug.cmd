@echo off
rem Start VindTerug. Works from any folder, without installation.
setlocal
set "HERE=%~dp0"
set "PYTHONPATH=%HERE%;%PYTHONPATH%"
rem The Windows console defaults to cp1252. Dutch text with characters
rem such as - and -> makes Python crash there while printing; utf-8 prevents that.
set "PYTHONIOENCODING=utf-8"

where python >nul 2>nul
if errorlevel 1 (
    echo Python was not found on this system.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during the installation.
    exit /b 3
)

python -m vindterug %*
set "CODE=%ERRORLEVEL%"
if not "%CODE%"=="0" (
    echo.
    echo VindTerug stopped with code %CODE%.
)
exit /b %CODE%
