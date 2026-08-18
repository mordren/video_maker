@echo off
REM Script de instalação do Corta+Legenda
REM Copia o app para a pasta de programas do usuário

setlocal enabledelayedexpansion

set SOURCE_DIR=%~dp0
set DEST_DIR=%LOCALAPPDATA%\CortaLegenda

echo.
echo ================================
echo  Instalando Corta+Legenda
echo ================================
echo.

REM Cria a pasta de destino se não existir
if not exist "%DEST_DIR%" mkdir "%DEST_DIR%"

REM Copia o executável e dependências
echo Copiando arquivos...
xcopy /E /I /Y "%SOURCE_DIR%dist\Corta+Legenda.exe" "%DEST_DIR%" >nul
xcopy /E /I /Y "%SOURCE_DIR%dist\CortaLegenda" "%DEST_DIR%\_internal" >nul

REM Cria um atalho no Desktop (opcional)
echo.
echo Criando atalho no Desktop...
powershell -NoProfile -Command ^
  "$WshShell = New-Object -ComObject WScript.Shell; " ^
  "$ShortCut = $WshShell.CreateShortcut([Environment]::GetFolderPath('Desktop')+'\Corta+Legenda.lnk'); " ^
  "$ShortCut.TargetPath = '%DEST_DIR%\Corta+Legenda.exe'; " ^
  "$ShortCut.WorkingDirectory = '%DEST_DIR%'; " ^
  "$ShortCut.Save()"

echo.
echo ================================
echo  Instalação concluída!
echo ================================
echo.
echo O app foi instalado em:
echo   %DEST_DIR%
echo.
echo Um atalho foi criado no Desktop.
echo.
pause
