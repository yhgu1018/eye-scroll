@echo off
chcp 65001 >nul
cd /d "%~dp0"
python eye_scroll.py
if errorlevel 1 pause
