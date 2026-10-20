@echo off
chcp 65001 >nul
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -STA -File "%~dp0src\desktop\Start-TranslationFlow.ps1"
if errorlevel 1 (
  echo.
  echo 翻译流启动失败。
  pause
)
