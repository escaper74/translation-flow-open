@echo off
chcp 65001 >NUL
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-final-mux.ps1" %*
exit /b %ERRORLEVEL%
