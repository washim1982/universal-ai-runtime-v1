@echo off
rem Build UAR Admin as a single UarAdmin.exe in admin-app\dist.
rem   build.cmd                  needs the .NET 8 (or newer) Desktop Runtime on the PC (small exe)
rem   build.cmd --self-contained runs on any 64-bit Windows 10/11 PC without .NET (about 70 MB)
setlocal
set "HERE=%~dp0"
set "SC=false"
if /i "%~1"=="--self-contained" set "SC=true"
where dotnet >nul 2>nul || (echo The .NET SDK 8 or newer is required: https://dotnet.microsoft.com/download & exit /b 1)
dotnet publish "%HERE%UarAdmin\UarAdmin.csproj" -c Release -r win-x64 --self-contained %SC% ^
  -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -p:DebugType=none -o "%HERE%dist" || exit /b 1
echo.
echo Built "%HERE%dist\UarAdmin.exe"
