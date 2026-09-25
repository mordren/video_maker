# Instala (ou atualiza) o Estudio e o Publicador como servicos na maquina da
# GPU: duas tarefas agendadas que sobem junto com o Windows, como SYSTEM, sem
# precisar de ninguem logado.
#
#   powershell -ExecutionPolicy Bypass -File servico_windows\instalar.ps1
#
# Precisa de PowerShell como administrador. Rodar de novo atualiza: reinstala
# as bibliotecas que faltarem e reinicia os dois servicos - os dados em
# C:\VideoMaker (trabalhos, fila, cookies, config) nao sao tocados.

$ErrorActionPreference = "Stop"
$VM = "C:\VideoMaker"
$repo = Split-Path -Parent $PSScriptRoot
$py = if (Test-Path "$repo\venv\Scripts\python.exe") { "$repo\venv\Scripts\python.exe" } else { "$repo\.venv\Scripts\python.exe" }

Write-Output "==> Pastas em $VM"
foreach ($p in "estudio", "estudio\transicoes", "publicador", "publicador\videos", "cache", "logs",
               "tmp", "ffmpeg", "trilhas", "marcas", "CortaLegenda") {
    New-Item -ItemType Directory -Force "$VM\$p" | Out-Null
}
if (-not (Test-Path "$VM\ffmpeg\ffmpeg.exe")) {
    Write-Warning "Falta o ffmpeg em $VM\ffmpeg (ffmpeg.exe, ffprobe.exe e as DLLs)."
}
if (-not (Test-Path "$VM\deno\deno.exe")) {
    Write-Warning "Falta o deno em $VM\deno\deno.exe - sem ele o yt-dlp nao baixa do YouTube (HTTP 403)."
}

Write-Output "==> Bibliotecas do Python (Publicador + Estudio)"
& $py -m pip install --quiet -r "$repo\servidor\requirements.txt" -r "$repo\fase1\requirements_crop.txt" requests
& $py -m pip install --quiet resemblyzer --no-deps
Write-Output "==> Chromium do Playwright (envios ao YouTube e ao TikTok)"
$env:PLAYWRIGHT_BROWSERS_PATH = "0"
& $py -m playwright install chromium

Write-Output "==> Parando versoes antigas"
foreach ($nome in "estudio", "publicador") {
    Stop-ScheduledTask -TaskName "VideoMaker $nome" -ErrorAction SilentlyContinue
}
# Parar a tarefa encerra o cmd.exe, mas nao o waitress que ele abriu.
Get-CimInstance Win32_Process -Filter "Name='waitress-serve.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -match "estudio:app|publicador:app" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }

Write-Output "==> Registrando as tarefas"
foreach ($nome in "estudio", "publicador") {
    $acao = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$PSScriptRoot\$nome.cmd`""
    $gatilho = New-ScheduledTaskTrigger -AtStartup
    $quem = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
    $cfg = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew
    Register-ScheduledTask -TaskName "VideoMaker $nome" -Action $acao -Trigger $gatilho -Principal $quem `
        -Settings $cfg -Force | Out-Null
    Start-ScheduledTask -TaskName "VideoMaker $nome"
}

Start-Sleep -Seconds 8
foreach ($porta in 8090, 8080) {
    try {
        $r = Invoke-WebRequest -UseBasicParsing -TimeoutSec 20 "http://127.0.0.1:$porta/"
        Write-Output "OK: porta $porta respondeu ($($r.StatusCode))"
    } catch {
        Write-Warning "porta $porta ainda nao respondeu - veja $VM\logs"
    }
}
Write-Output ""
Write-Output "Estudio:    http://192.168.31.133:8090"
Write-Output "Publicador: http://192.168.31.133:8080"
