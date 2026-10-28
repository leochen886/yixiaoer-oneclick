@echo off
chcp 65001 >nul
setlocal
title OneClick Publish - WeChat Channels
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\oneclick-sph.ps1" %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
