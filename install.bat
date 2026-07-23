@echo off
REM Eenmalige installatie: virtuele omgeving + dependencies.
cd /d "%~dp0"

where uv >nul 2>nul
if errorlevel 1 (
    echo uv is niet gevonden. Installeer het met:
    echo   powershell -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    pause
    exit /b 1
)

uv venv --python 3.12 .venv
set VIRTUAL_ENV=%CD%\.venv
uv pip install -r requirements.txt

echo.
echo Klaar. Start de tool met start.bat
pause
