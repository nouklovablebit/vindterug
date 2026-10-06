@echo off
rem Builds VindTerug.exe (standalone executable) in the dist\ folder.
setlocal
set "HERE=%~dp0"
cd /d "%HERE%"

python -c "import PyInstaller" 2>nul
if errorlevel 1 (
    echo PyInstaller is missing. I am installing it now...
    python -m pip install pyinstaller
    if errorlevel 1 goto :error
)

python -m PyInstaller --noconfirm --clean --windowed --onefile ^
    --name "VindTerug" ^
    --distpath "%HERE%dist" ^
    --workpath "%HERE%build" ^
    --specpath "%HERE%build" ^
    "run_vindterug.py"
if errorlevel 1 goto :error

echo.
echo Done. The program is in: %HERE%dist\VindTerug.exe
exit /b 0

:error
echo.
echo The build failed. Read the message above.
exit /b 1
