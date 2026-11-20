@echo off
setlocal
chcp 65001 >nul
if "%~1"=="" (
    echo 把一个口播视频拖到这个图标上即可开始剪辑。
    pause
    exit /b 1
)
python -X utf8 "%~dp0scripts\koubo.py" "%~1" --open-folder
if errorlevel 1 (
    pause
    exit /b 1
)
