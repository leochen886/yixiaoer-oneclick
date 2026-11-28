@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
title Review Workbench - WeChat Channels
python -X utf8 "%~dp0scripts\review_workbench.py" --open %*
set RC=%ERRORLEVEL%
if not "%RC%"=="0" (
  echo.
  pause
)
exit /b %RC%
