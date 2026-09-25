# Instala a chave SSH desta máquina na máquina de processamento (Windows,
# com a GTX 1650), para o envio/processamento remoto não pedir senha.
# Roda UMA vez, daqui (máquina local); pede a senha do usuário remoto uma vez.
#
#   powershell -ExecutionPolicy Bypass -File fase1\remoto\instalar_chave.ps1

param(
    [string]$Destino = "mordren@192.168.31.133",
    [string]$ChavePublica = "$HOME\.ssh\id_ed25519_video_maker_win.pub"
)

$chave = (Get-Content $ChavePublica -Raw).Trim()

# Script que roda LÁ. Usuário admin no Windows ignora o authorized_keys da
# home: o OpenSSH só lê C:\ProgramData\ssh\administrators_authorized_keys,
# e exige que só Administradores/SYSTEM tenham acesso ao arquivo.
$remoto = @"
`$k = '$chave'
`$admin = [Security.Principal.WindowsIdentity]::GetCurrent().Groups.Value -contains 'S-1-5-32-544'
if (`$admin) { `$f = "`$env:ProgramData\ssh\administrators_authorized_keys" }
else { New-Item -ItemType Directory -Force "`$HOME\.ssh" | Out-Null; `$f = "`$HOME\.ssh\authorized_keys" }
if (-not ((Test-Path `$f) -and (Select-String -Path `$f -SimpleMatch `$k -Quiet))) { Add-Content -Path `$f -Value `$k -Encoding ascii }
if (`$admin) { icacls `$f /inheritance:r /grant '*S-1-5-32-544:F' /grant '*S-1-5-18:F' | Out-Null }
Write-Output "OK: chave instalada em `$f (admin=`$admin)"
"@

# Base64 evita o inferno de aspas PowerShell -> ssh -> cmd remoto -> PowerShell.
$codificado = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($remoto))
ssh -o PubkeyAuthentication=no $Destino "powershell -NoProfile -EncodedCommand $codificado"
if ($LASTEXITCODE -ne 0) { Write-Error "falhou (código $LASTEXITCODE)"; exit 1 }

Write-Output "Testando login sem senha..."
ssh -o BatchMode=yes video-maker-win "echo LOGIN SEM SENHA OK"
