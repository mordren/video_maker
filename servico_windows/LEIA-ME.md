# Estúdio + Publicador na máquina da GPU (Windows, 192.168.31.133)

Dois serviços web, os dois só na rede de casa, sem senha:

| Serviço | Endereço | O que faz |
|---|---|---|
| **Estúdio** | http://192.168.31.133:8090 | Link do YouTube, arquivo ou lote CSV → cortes 9:16 prontos → revisão |
| **Publicador** | http://192.168.31.133:8080 | Fila que publica no YouTube/TikTok (o mesmo que rodava no Debian) |

Fluxo: no Estúdio, cola o link e escolhe canal + visual → o trabalho roda
sozinho (Fase 1 com JEV, Fase 2 com abertura em preto e branco, crop dinâmico
que segue quem fala, legenda, GC, marca d'água, censura, trilha, capa) → os
cortes aparecem em **Revisar** → *Enviar para a fila* manda para o Publicador,
que publica no ritmo configurado de cada canal. Nada é publicado sem passar
pela revisão.

## Onde fica cada coisa

```
C:\VideoMaker\
  estudio\trabalhos\<id>\     vídeo baixado, fases, cortes finais e o log de cada trabalho
  estudio\transicoes\         sons de transição (whoosh) da abertura
  publicador\                 fila.json, config.json, cookies dos canais, vídeos na fila
  CortaLegenda\config.json    perfis visuais, lista de censura, chave do DeepSeek (a mesma do app)
  marcas\  trilhas\           logos dos perfis e trilhas de fundo
  ffmpeg\                     ffmpeg.exe, ffprobe.exe e DLLs
  deno\deno.exe               runtime de JavaScript que o yt-dlp exige para baixar do YouTube
  cache\                      modelos do Whisper
  logs\                       estudio.log, publicador.log
```

O código fica no repositório (`C:\Users\mordren\Documents\video_maker`); a
chave do OpenRouter (JEV) em `fase1\.env`, fora do git.

## Instalar / atualizar

PowerShell **como administrador**, na pasta do repositório:

```powershell
git pull
powershell -ExecutionPolicy Bypass -File servico_windows\instalar.ps1
```

Cria as tarefas agendadas *VideoMaker estudio* e *VideoMaker publicador*
(sobem com o Windows, como SYSTEM, sem ninguém logado) e reinicia as duas.
Rodar de novo só atualiza — trabalhos, fila e cookies não são tocados.

## Ajustes da máquina (feitos à mão, uma vez)

Mexem em rede, firewall e energia do Windows — ficam fora do instalador de
propósito. Todos no PowerShell como administrador.

1. **Liberar as portas na rede local** (sem isso, só a própria máquina abre as páginas):
   ```powershell
   New-NetFirewallRule -DisplayName "VideoMaker (Estudio e Publicador)" -Direction Inbound -Protocol TCP -LocalPort 8080,8090 -RemoteAddress LocalSubnet -Action Allow
   ```
2. **Não dormir** (máquina dormindo = serviço fora do ar e fila parada):
   ```powershell
   powercfg /change standby-timeout-ac 0
   powercfg /change hibernate-timeout-ac 0
   ```
3. **IP fixo 192.168.31.133** — o jeito mais simples e que não quebra é reservar
   o IP no roteador (DHCP → reserva de endereço para o MAC desta máquina). Se
   preferir fixar no Windows: Configurações → Rede → Ethernet/Wi-Fi → Editar
   atribuição de IP → Manual → IP 192.168.31.133, máscara 255.255.255.0,
   gateway e DNS iguais aos de hoje (`ipconfig /all` mostra).

## Manutenção

```powershell
Get-ScheduledTask "VideoMaker*" | Select TaskName, State   # estão rodando?
Get-Content C:\VideoMaker\logs\estudio.log -Tail 50         # erros do Estúdio
Get-Content C:\VideoMaker\logs\publicador.log -Tail 50      # erros do Publicador
Start-ScheduledTask "VideoMaker estudio"                    # subir de novo na mão
```

O log de cada trabalho (download, Fase 1, Fase 2, acabamento) aparece na
própria página do Estúdio, no botão *log*.

## API (para automatizar depois — ex.: bot do Telegram)

```
POST /api/trabalhos   {"url": "https://youtu.be/...", "canal": "info", "perfil": "info_nacional"}
GET  /api/estado      trabalhos, cortes esperando revisão, canais e perfis
POST /api/cortes/<trabalho>/<corte>/enviar     manda um corte revisado para a fila
```
