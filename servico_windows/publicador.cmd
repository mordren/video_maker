@echo off
rem Serviço do Publicador (fila YouTube/TikTok) — http://<esta máquina>:8080
rem Um processo só, com threads: exatamente um laço de envio (nunca dois
rem uploads ao mesmo tempo na mesma conta). Se cair, sobe de novo em 5 s.
call "%~dp0ambiente.cmd"
cd /d "%REPO%\servidor"
:laco
echo [%date% %time%] iniciando o Publicador >> "%VM%\logs\publicador.log"
"%PY%\waitress-serve.exe" --listen=0.0.0.0:8080 --threads=8 --max-request-body-size=12884901888 publicador:app >> "%VM%\logs\publicador.log" 2>&1
ping -n 6 127.0.0.1 >nul
goto laco
