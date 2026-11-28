@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
title OneClick Publish - Maitian (8 platforms)
if "%~1"=="" goto :run_interactive
set "FIRST=%~1"
if "%FIRST:~0,1%"=="-" goto :run_flags
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\oneclick-maitian.ps1" "%~1"
goto :done
:run_interactive
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\oneclick-maitian.ps1"
goto :done
:run_flags
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\oneclick-maitian.ps1" %*
:done
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
