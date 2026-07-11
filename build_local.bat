@echo off
chcp 65001 >nul
cd /d "%~dp0"
py build_local.py %*
echo.
pause
