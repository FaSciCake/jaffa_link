@echo off
REM Double-click launcher for sender.py - forwards any args to send.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0send.ps1" %*
if errorlevel 1 pause
