@echo off
rem Ambiente comum aos dois serviços (Estúdio e Publicador) na máquina da GPU.
rem Eles rodam como SYSTEM (tarefa agendada ao ligar a máquina), que não tem o
rem PATH nem o perfil do usuário — por isso tudo é apontado para C:\VideoMaker,
rem uma pasta fora de qualquer perfil de usuário.

set "VM=C:\VideoMaker"
for %%i in ("%~dp0..") do set "REPO=%%~fi"
if exist "%REPO%\venv\Scripts\python.exe" (set "PY=%REPO%\venv\Scripts") else (set "PY=%REPO%\.venv\Scripts")

rem deno: o yt-dlp precisa de um runtime de JavaScript para baixar do YouTube.
set "PATH=%VM%\ffmpeg;%VM%\deno;%PY%;%PATH%"
set "PYTHONPATH=%REPO%"
set "PYTHONUTF8=1"
set "PYTHONUNBUFFERED=1"
rem O programa de desktop guarda perfis visuais, lista de censura, pasta de
rem trilhas e chave do DeepSeek em %%LOCALAPPDATA%%\CortaLegenda\config.json;
rem aqui ele passa a ser C:\VideoMaker\CortaLegenda\config.json.
set "LOCALAPPDATA=%VM%"
rem Modelos do Whisper (~/.cache/whisper) — sem isto iriam para o perfil do SYSTEM.
set "XDG_CACHE_HOME=%VM%\cache"
set "TMP=%VM%\tmp"
set "TEMP=%VM%\tmp"
set "ESTUDIO_DATA=%VM%\estudio"
set "PUBLICADOR_DATA=%VM%\publicador"
set "PUBLICADOR_URL=http://192.168.31.133:8080"
rem Chromium dos envios (Playwright) fica dentro do venv, não no perfil do usuário.
set "PLAYWRIGHT_BROWSERS_PATH=0"

rem E-mail de aviso do Publicador (opcional), mesmo formato do Debian: CHAVE=valor.
if exist "%PUBLICADOR_DATA%\publicador.env" (
  for /f "usebackq eol=# tokens=1* delims==" %%a in ("%PUBLICADOR_DATA%\publicador.env") do set "%%a=%%b"
)
