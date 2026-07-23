@echo off
REM Start Open STL-3DMap Studio (vanuit broncode) en open de browser.
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtuele omgeving ontbreekt. Draai eerst install.bat
    pause
    exit /b 1
)

start "" http://127.0.0.1:8765
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8765
pause
