@echo off
setlocal DisableDelayedExpansion
chcp 65001 >nul
title Koubo Preview
python -u -X utf8 "%~dp0scripts\koubo.py" --open-folder -- "%~1"
if not errorlevel 1 exit /b 0
pause
exit /b 1
