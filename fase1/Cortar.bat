@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" (
    set /p VIDEO="Caminho do video: "
) else (
    set "VIDEO=%~1"
)

if not exist "%VIDEO%" (
    echo Video nao encontrado: %VIDEO%
    pause
    exit /b 1
)

"%~dp0..\.venv\Scripts\python.exe" "%~dp0rodar_tudo.py" "%VIDEO%"

echo.
pause
