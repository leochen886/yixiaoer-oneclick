@echo off
chcp 65001 >nul
setlocal
title OneClick Publish - WeChat Channels (ego)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\oneclick-sph-ego.ps1" %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
