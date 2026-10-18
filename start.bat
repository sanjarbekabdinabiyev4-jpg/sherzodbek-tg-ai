@echo off
title Sherzodbek AI Yordamchi
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" bot.py
) else (
    py bot.py
)
pause
