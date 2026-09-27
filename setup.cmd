@echo off
rem Opens the UAR setup dashboard in your browser. Keep this window open while you use it.
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (
  py -3 setup-dashboard\dashboard.py %*
  goto :end
)
where python >nul 2>nul
if %errorlevel%==0 (
  python setup-dashboard\dashboard.py %*
  goto :end
)
echo.
echo   Python 3.12 or newer is required and was not found.
echo   Install it with:   winget install -e --id Python.Python.3.13
echo   (or from https://www.python.org/downloads/ - tick "Add python.exe to PATH"),
echo   then close this window and double-click setup.cmd again.
echo.
:end
pause
