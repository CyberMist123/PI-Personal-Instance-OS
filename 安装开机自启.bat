@echo off
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-autostart.ps1"
set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
  echo PI OS autostart installation failed. Exit code: %code%
) else (
  echo PI OS autostart installation finished.
)
pause
exit /b %code%
