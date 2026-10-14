@echo off
setlocal
set "PYTHONPATH=%~dp0..\src;%PYTHONPATH%"
if defined TRANSLATION_FLOW_PYTHON (
    set "TF_SCRIPT_PYTHON=%TRANSLATION_FLOW_PYTHON%"
) else if exist "%~dp0..\.venv\Scripts\python.exe" (
    set "TF_SCRIPT_PYTHON=%~dp0..\.venv\Scripts\python.exe"
) else (
    set "TF_SCRIPT_PYTHON=python.exe"
)
pushd "%~dp0.."
if errorlevel 1 exit /b 1
"%TF_SCRIPT_PYTHON%" %*
set "TF_SCRIPT_EXIT=%ERRORLEVEL%"
popd
exit /b %TF_SCRIPT_EXIT%
