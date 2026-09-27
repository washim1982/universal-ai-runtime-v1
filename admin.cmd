@echo off
rem Start UAR Admin, the Windows administration app (builds it on first use).
setlocal
set "ROOT=%~dp0"
if not exist "%ROOT%admin-app\dist\UarAdmin.exe" (
  echo Building UAR Admin (first run only)...
  call "%ROOT%admin-app\build.cmd" || (pause & exit /b 1)
)
start "" "%ROOT%admin-app\dist\UarAdmin.exe"
