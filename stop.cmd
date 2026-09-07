@echo off
setlocal
set "VISTORA_PWSH=%~dp0var\tools\powershell-7.6.5\pwsh.exe"
if exist "%VISTORA_PWSH%" goto run
where pwsh.exe >nul 2>nul
if errorlevel 1 (
  echo PowerShell 7.2 or newer is required.
  exit /b 1
)
set "VISTORA_PWSH=pwsh.exe"
:run
"%VISTORA_PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
exit /b %errorlevel%
