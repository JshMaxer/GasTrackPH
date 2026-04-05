@echo off
cd /d "%~dp0"
start "GasTrack PH Server" cmd /k python server.py
timeout /t 2 /nobreak >nul
start "" http://127.0.0.1:8000
