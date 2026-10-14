@echo off
chcp 65001 >NUL
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run-jasna-picture.ps1" %*
exit /b %ERRORLEVEL%
