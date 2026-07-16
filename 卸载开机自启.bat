@echo off
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0remove-autostart.ps1"
set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
  echo PI OS autostart removal failed. Exit code: %code%
) else (
  echo PI OS autostart removal finished.
)
pause
exit /b %code%
