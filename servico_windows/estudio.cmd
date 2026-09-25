@echo off
rem Serviço do Estúdio (produção dos cortes) — http://<esta máquina>:8090
rem Roda para sempre: se o servidor cair, sobe de novo em 5 s.
call "%~dp0ambiente.cmd"
cd /d "%REPO%\estudio"
:laco
echo [%date% %time%] iniciando o Estudio >> "%VM%\logs\estudio.log"
"%PY%\waitress-serve.exe" --listen=0.0.0.0:8090 --threads=8 --max-request-body-size=12884901888 estudio:app >> "%VM%\logs\estudio.log" 2>&1
rem "timeout" não funciona sem console (tarefa do SYSTEM); o ping espera ~5 s.
ping -n 6 127.0.0.1 >nul
goto laco
