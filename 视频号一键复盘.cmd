@echo off
chcp 65001 >nul
setlocal
title OneClick Review - WeChat Channels (ego)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\review-all-sph.ps1" %*
set RC=%ERRORLEVEL%
echo.
pause
exit /b %RC%
