@echo off
chcp 65001 >nul
cd /d "%~dp0"
py release.py
echo.
pause
