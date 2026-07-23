@echo off
REM Bouwt de complete Windows-installer: icoon -> losse exe -> setup.exe
REM Resultaat: packaging\Output\3DKaartBouwerNL-<versie>-Setup.exe
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Virtuele omgeving ontbreekt. Draai eerst install.bat
    exit /b 1
)

echo.
echo [1/3] Icoon genereren...
".venv\Scripts\python.exe" make_icon.py || exit /b 1

echo.
echo [2/3] Applicatie bundelen met PyInstaller...
REM Een draaiende versie houdt bestanden vast en laat de build stuklopen.
taskkill /IM "Open STL-3DMap Studio.exe" /F >nul 2>&1
".venv\Scripts\pyinstaller.exe" packaging\3dmaps.spec --noconfirm --distpath dist --workpath build || exit /b 1

echo.
echo [3/3] Installer bouwen met Inno Setup...
set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" (
    echo Inno Setup niet gevonden. Installeer het met:
    echo   winget install --id JRSoftware.InnoSetup
    exit /b 1
)
"%ISCC%" packaging\3dmaps.iss || exit /b 1

echo.
echo Klaar. De installer staat in packaging\Output
dir /b packaging\Output\*.exe
