@echo off
setlocal
set "VISTORA_PWSH=%~dp0var\tools\powershell-7.6.5\pwsh.exe"
if exist "%VISTORA_PWSH%" goto run
where pwsh.exe >nul 2>nul
if errorlevel 1 goto install
set "VISTORA_PWSH=pwsh.exe"
goto run
:install
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\bootstrap-powershell.ps1"
if errorlevel 1 exit /b 1
:run
"%VISTORA_PWSH%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
exit /b %errorlevel%
