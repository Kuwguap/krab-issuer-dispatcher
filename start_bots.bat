@echo off
cd /d "%~dp0"
python start_bots.py
if errorlevel 1 pause
