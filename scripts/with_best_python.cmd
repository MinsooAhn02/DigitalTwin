@echo off
setlocal EnableExtensions

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "CUDA_PY=%ROOT%\.venv-cuda\Scripts\python.exe"
set "DEFAULT_PY=%ROOT%\.venv\Scripts\python.exe"
set "SELECTED="
set "SOURCE="

call :prefer_cuda "%CUDA_PY%" ".venv-cuda"
if defined SELECTED goto run
call :prefer_cuda "%DEFAULT_PY%" ".venv"
if defined SELECTED goto run
call :prefer_cuda "python" "python"
if defined SELECTED goto run

call :prefer_existing "%CUDA_PY%" ".venv-cuda"
if defined SELECTED goto run
call :prefer_existing "%DEFAULT_PY%" ".venv"
if defined SELECTED goto run

set "SELECTED=python"
set "SOURCE=python"

:run
set "PYTHONUTF8=1"
echo [with_best_python] using %SOURCE%
"%SELECTED%" %*
exit /b %ERRORLEVEL%

:prefer_cuda
set "CANDIDATE=%~1"
set "LABEL=%~2"
call :can_run "%CANDIDATE%"
if errorlevel 1 exit /b 0
"%CANDIDATE%" -c "import sys; import torch; sys.exit(0 if torch.cuda.is_available() else 1)" >nul 2>&1
if not errorlevel 1 (
    set "SELECTED=%CANDIDATE%"
    set "SOURCE=%LABEL% (CUDA)"
)
exit /b 0

:prefer_existing
set "CANDIDATE=%~1"
set "LABEL=%~2"
call :can_run "%CANDIDATE%"
if errorlevel 1 exit /b 0
set "SELECTED=%CANDIDATE%"
set "SOURCE=%LABEL%"
exit /b 0

:can_run
set "CANDIDATE=%~1"
if /I "%CANDIDATE%"=="python" (
    python -c "import sys" >nul 2>&1
    exit /b %ERRORLEVEL%
)
if exist "%CANDIDATE%" (
    "%CANDIDATE%" -c "import sys" >nul 2>&1
    exit /b %ERRORLEVEL%
)
exit /b 1
