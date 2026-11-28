@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
title OneClick Publish - WeChat Channels (ego)
if "%~1"=="" goto :run_interactive
set "FIRST=%~1"
if "%FIRST:~0,1%"=="-" goto :run_flags
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\oneclick-sph-ego.ps1" -CutKoubo "%~1"
goto :done
:run_interactive
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\oneclick-sph-ego.ps1" -CutKoubo
goto :done
:run_flags
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\oneclick-sph-ego.ps1" -CutKoubo %*
:done
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
