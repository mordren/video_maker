# Documentação Completa — Corta+Legenda

**Versão**: 1.0 | **Linguagem**: Python 3.11+ | **GUI**: PySide6 | **Dependências**: FFmpeg, yt-dlp, Whisper

---

## 📋 Índice

1. [Visão Geral](#visão-geral)
2. [Arquitetura](#arquitetura)
3. [Módulos](#módulos)
4. [Funcionalidades](#funcionalidades)
5. [Fluxos de Trabalho](#fluxos-de-trabalho)
6. [Configuração](#configuração)
7. [API Interna](#api-interna)

---

## 🎯 Visão Geral

**Corta+Legenda** é um editor de vídeo desktop 100% offline que:
- Corta trechos de vídeo por tempo inicial/final
- Converte para formato vertical (9:16) com várias opções de enquadramento
- Gera legendas automaticamente via Whisper (local, sem enviar dados)
- Aplica logo, texto e trilha de fundo
- Revisa e melhora legendas com IA (DeepSeek, opcional)
- Censura palavras automaticamente (silencia áudio + modifica legenda)
- Processa cortes em lote (CSV) ou lives ao vivo

**Nenhum vídeo sai da máquina** — toda transcrição, legendas e processamento acontecem localmente.

---

## 🏗️ Arquitetura

### Stack Técnico
```
┌─────────────────────────────────────────────┐
│         Interface Qt (PySide6)              │
├─────────────────────────────────────────────┤
│  app.py (3968 linhas — lógica + UI)         │
├─────────────────────────────────────────────┤
│ utils.py | ai_srt.py | censor.py | trilhas │
│ cg_generator.py                             │
├─────────────────────────────────────────────┤
│  FFmpeg (corte, formato, mixagem)           │
│  Whisper (transcrição)                      │
│  yt-dlp (download de YouTube)               │
│  DeepSeek API (revisão IA, opcional)        │
└─────────────────────────────────────────────┘
```

### Fluxo de Dados Típico
```
URL YouTube    →  yt-dlp  →  MP4 local
                                ↓
Seleção recorte (HH:MM:SS)
                                ↓
Whisper → transcrição SRT
                                ↓
(Opcional) DeepSeek → revisão/título
                                ↓
FFmpeg com filtros → vídeo final (9:16)
                                ↓
MP4 + SRT + .txt (legenda Instagram)
```

---

## 📦 Módulos

### **app.py** (3968 linhas)
Aplicação principal com interface Qt.

#### Classes Principais
- `MainWindow` — janela principal com 4 abas
- `SrtReviewWorker` — thread para revisão de legendas com IA
- `CutsSuggestWorker` — thread para sugestão de cortes (IA)
- `ReelsCaptionWorker` — thread para gerar legenda de Instagram
- `WheelGuard` — previne scroll do mouse em combos/spinboxes

#### Abas (Tabs)
1. **🎬 Edição** — Cortar um vídeo individual
2. **📊 CSV Lotes** — Processar múltiplos cortes em lote
3. **🔴 Live** — Gravar e cortar lives do YouTube em tempo real
4. **⚙️ Configuração** — Identidade visual (logo, cores, marca d'água)

---

### **utils.py** (851 linhas)
Funções utilitárias compartilhadas entre módulos.

#### Principais Funções
```python
# Parsing e conversão
as_time(seconds: float) -> str           # Converte segundos em HH:MM:SS
parse_time_string(s: str) -> float       # Converte "01:30:45" em segundos

# SRT (legendas)
parse_srt_segments(path: Path)           # Lê arquivo SRT
segments_to_srt(segs, start, end, out)   # Recorta SRT para faixa de tempo
shorten_srt_captions(path)               # Encurta blocos > 18 caracteres
clean_srt_file(path)                     # Remove blocos repetidos

# Vídeo
build_clip_filter(...)                   # Monta filtro FFmpeg para recorte
build_srt_for_clip(...)                  # Gera SRT sincronizado do corte
filter_path(s)                           # Escapa caracteres para libass

# Whisper
whisper_path() -> Path                   # Retorna caminho do executável
transcript_with_timestamps(path)         # Transcreve com Whisper

# yt-dlp
yt_dlp_path() -> Path                    # Retorna caminho do executável
find_video_subtitle(video_path)          # Procura .srt ao lado do vídeo

# CSV
parse_csv_moments(csv_path)              # Lê e valida CSV com cortes

# Templates (Instagram Reels)
build_reels_prompt(srt_path)             # Monta prompt para legenda Reels
write_reels_text(dest, text)             # Salva .txt com legenda

# Config
load_config() -> dict                    # Carrega `config.json`
```

#### Widget Personalizado
```python
class TimestampInput(QLineEdit):
    """Campo de entrada de tempo em HH:MM:SS.
    
    - Exibe em formato HH:MM:SS (ex: 01:30:45)
    - Aceita MM:SS ou HH:MM:SS na entrada (retrocompatível)
    - Emite signal `secondsChanged` quando valor muda
    - Suporta setMaximum() para validar limite
    """
```

---

### **ai_srt.py** (592 linhas)
Integração com DeepSeek para revisão de legendas e IA.

#### Principais Funções
```python
# Revisar legenda
review_srt_with_ai(srt_path, api_key, model, ...)
    → (blocos_mudados, total, usage, titulo, subtitulo, sensiveis, musica)

# Sugerir cortes
suggest_cuts(transcript, api_key, model, ...)
    → (lista_de_cortes, usage)

# Comunicação básica
ask(prompt, api_key, model) -> (resposta, usage)

# Utilitários
list_models(api_key)          # Lista modelos disponíveis
api_key_from_env()            # Lê DEEPSEEK_API_KEY do sistema
load_config() / save_config() # Persiste config em config.json
```

#### API DeepSeek
- Chave via interface ou variável `DEEPSEEK_API_KEY`
- Modelo recomendado: `deepseek-chat` (barato e rápido)
- Texto da legenda é enviado; **tempos nunca saem do computador**
- Resposta inclui:
  - Legendas revisadas
  - Sugestão de título (chapéu do GC)
  - Sugestão de subtítulo (manchete)
  - Trechos marcados como sensíveis
  - Clima/trilha recomendado

---

### **censor.py** (310 linhas)
Censura de palavrões e palavras bloqueáveis.

#### Funcionalidade
- Define lista padrão de palavras bloqueáveis
- Aceita padrões com `*` (ex: `put*` → puta, putas, putaria)
- Duas ações simultâneas:
  1. **Silenciar áudio** — muta o trecho na faixa de som
  2. **Disfarçar na legenda** — matar → m4tar, casa → c@sa, etc.

#### Principais Funções
```python
censor_text(text, patterns)          # Disfarça na legenda
censor_srt(path, patterns)           # Aplica censoramento ao .srt
mute_spans(srt_path, patterns)       # Acha momentos onde falar a palavra
mute_filter(spans)                   # Gera filtro FFmpeg para silenciar
```

#### Configuração
- Palavras carregadas de `config.json` (chave `censor_words`)
- Recuperáveis para padrão com botão na interface
- Funcionam apenas onde há legenda (SRT dita os tempos)

---

### **cg_generator.py** (262 linhas)
Geração de lower-third (tarja com logo, título e subtítulo).

#### Output
Imagem PNG que é queimada no vídeo com:
- Logo do canal (esquerda)
- Título/chapéu (topo direita) — Montserrat 18px, amarelo
- Subtítulo/manchete (embaixo direita) — Montserrat 48px, amarelo
- Fundo preto com transparência

#### Principais Funções
```python
create_lower_third(titulo, subtitulo, output_dir, logo_path, colors)
    → Path ao PNG gerado (salva em work_dir)

merge_colors(brand_colors)           # Aplica cores da marca
```

#### Configuração
Cores via `config.json`:
```json
{
  "brand_name": "Informativo Nacional",
  "brand_colors": {
    "logo_bg": "#FFFFFF",
    "title_fg": "#FFFF00"
  }
}
```

---

### **trilhas.py** (95 linhas)
Catálogo de trilhas de fundo e mixagem.

#### Uso
1. Joga arquivos MP3/M4A/WAV/OGG/FLAC em `~/Videos/CortaLegenda/trilhas/`
2. Nome do arquivo vira rótulo (ex: `05-confronto.mp3` → "confronto")
3. A IA escolhe qual trilha usar
4. FFmpeg applica a trilha com:
   - Normalização de volume (`loudnorm`)
   - Ducking (música abaixa quando fala)
   - Limitador de pico (`alimiter`)

#### Principais Funções
```python
list_tracks(folder) -> [(rótulo, caminho), ...]
find_track(label, folder) -> Path | None
mix_chain(speech_label, music_input) -> str  # Filtro FFmpeg
```

---

## 🎬 Funcionalidades

### 1. **Download de YouTube** (Aba Edição)
```
URL → yt-dlp → MP4 local

Opções:
  • Intervalo de tempo: "Tudo" (padrão) ou "Personalizado" (HH:MM:SS)
  • Conexões simultâneas (1–16, padrão 8)
  • Baixar legendas/transcrição (pt-br, oficial ou automática)
```

**Novo**: Usa `--download-sections "*00:05:30-00:10:15"` para baixar só o trecho especificado, sem necessidade de pós-processamento.

---

### 2. **Edição Individual** (Aba 🎬)
```
Entrada:
  • Vídeo local ou baixado
  • Tempo inicial e final (HH:MM:SS)
  • Formato saída: original, vertical 9:16 (crop/blur/imagem)

Processamento:
  1. Recorte temporal (FFmpeg -ss -t)
  2. Transcrição (Whisper)
  3. Revisão legendas (DeepSeek, opcional)
  4. Aplicar lower-third (logo + texto)
  5. Censura (som + legenda)
  6. Mixar trilha de fundo
  7. Exportar MP4 H.264

Saída:
  • MP4 final (9:16 ou original)
  • .srt com legendas (sincronizado)
  • .txt com legenda de Instagram (gerada por IA)
  • .png frame de post (primeiro frame do vídeo)
```

---

### 3. **Processamento em Lote** (Aba 📊 CSV)
```
Entrada: CSV com colunas
  • inicio / fim — MM:SS ou HH:MM:SS
  • titulo — nome do arquivo (opcional)
  • formato — "estender", "transparente", "imagem", "original"
  • imagem — caminho PNG/JPG (se formato=imagem)
  • comentario — notas (exibidas na tabela)

Saída:
  • Cada linha gera 1 vídeo 9:16 em OUT_DIR/
  • Legendas geradas por Whisper (linha por linha)
  • Mesmas opções de censura, trilha, revisão que edição
  
Dica: CSVs antigos (sem cabeçalho) ainda funcionam com formato por posição.
```

---

### 4. **Gravação e Corte de Live** (Aba 🔴)
```
Fluxo:
  1. Cola URL de live do YouTube
  2. ⏺ Gravar live — baixa desde o começo (yt-dlp --live-from-start)
     • Arquivo cresce em tempo real (20 MB/min)
     • Player atualiza a cada 10s
  3. 🔄 Atualizar — recarrega arquivo (pega trecho novo)
  4. ⏩ Ao vivo — pula para ponto mais recente
  5. Marcar início/fim no player → define título
  6. ✂️ Exportar corte
     • Extrai áudio do período
     • Gera legendas Whisper
     • Aplica logo, título, trilha, censura
     • Queima tudo no MP4

Detalhe: Marcar fim com ~10s de folga (precisa estar gravado no disco).
```

---

### 5. **Revisão de Legendas com IA**
```
Integração DeepSeek (opcional):

Entrada: Arquivo .srt (legenda bruta do Whisper)

Processamento:
  1. Envia texto das legendas (não envia tempos)
  2. IA revisa erros de transcrição
  3. IA sugere título/subtítulo (contexto da fala)
  4. IA marca trechos "sensíveis" (palavrões mesmo sem censura)
  5. IA escolhe trilha (clima) do catálogo

Saída:
  • .srt melhorado
  • Título + subtítulo preenchidos
  • Lista de sensibilidades
  • Trilha escolhida tocada em preview

Config: config.json
  • ai_enabled: ativa revisão automática pós-Whisper
  • api_key: chave DeepSeek (ou env DEEPSEEK_API_KEY)
  • model: "deepseek-chat" recomendado
  • ai_reels: gera legenda Instagram com IA
```

---

### 6. **Censura Automática**
```
Setup:
  1. Defina palavras em "6. Palavrões e palavras bloqueadas"
  2. Padrões com * (put* = puta, putas, putaria)
  
Ação (simultânea):
  • Áudio: silencia o exato segundo da palavra (lookup no .srt)
  • Legenda: matar → m4tar (preserva estrutura)
  
Pré-requisito: Legenda presente (arquivo .srt)
```

---

### 7. **Trilha de Fundo**
```
Setup:
  1. Joga .mp3 em ~/Videos/CortaLegenda/trilhas/
     Nome vira rótulo (ex: "05-confronto.mp3" → "confronto")
  2. Ativa "7. Trilha de fundo" na interface
  
Fluxo:
  • IA escolhe trilha ao revisar legendas (só na aba Edição)
  • Você ouve preview com botão ▶
  • Exportação mescla: fala + trilha com ducking (música cede pra fala)
  • Fade in de 1.5s e fade out no fim do vídeo
```

---

### 8. **Configuração (Identidade Visual)**
```
Aba ⚙️ Configuração:
  • Nome do canal (ex: "Informativo Nacional")
  • Logo PNG (esq. inferior do GC)
  • Marca d'água texto (ex: "@RENANSANTOSMBL") — topo do vídeo
  • Cor da marca d'água (ex: amarelo #FFFF00)
  • Cores do lower-third (logo bg, títulos)

Persiste em config.json na pasta do projeto.
```

---

## 🔄 Fluxos de Trabalho

### Fluxo A: Editar um vídeo local
```
1. Escolher arquivo
   → pick_video() lê duração com VLC
   → timeline atualiza range (100ms polling)
   
2. Marcar tempo de recorte
   → timeline slider ou campos HH:MM:SS
   → preview mostra vídeo original (não o recorte)
   
3. Gerar legendas
   → FFmpeg extrai áudio do trecho
   → Whisper transcreve (1ª vez: baixa model small ~500MB)
   → (Opcional) DeepSeek revisa + sugere título
   → .srt salvo no work_dir
   
4. Preencher texto (título/subtítulo)
   → Podem vir preenchidos pela IA
   
5. Exportar vídeo
   → FFmpeg aplica: recorte, formato, logo, legendas, trilha, censura
   → Salva MP4 em OUT_DIR
   → Salva .srt ao lado (mesma transcrição)
   → (Opcional) Gera legenda Instagram (DeepSeek) → .txt
   → (Opcional) Salva frame de post → .png
```

### Fluxo B: Processar em lote (CSV)
```
1. Preparar CSV
   → Cabeçalho: inicio,fim,titulo,formato,imagem,comentario
   → Linhas: tempos, nomes, formatos
   
2. Escolher vídeo (mesmo para todos os cortes)
   
3. Carregar CSV
   → Validação: tempos, caminhos de imagem
   → Preview: tabela com comentários
   
4. Gerar lote
   → Loop cada linha:
      - Recorta temporalmente (FFmpeg)
      - Transcreve (Whisper)
      - Aplica processamento (logo, censura, trilha)
      - Exporta MP4 em OUT_DIR/
   → Log mostra progresso
   
5. Saídas
   → Lote de vídeos 9:16 prontos para shorts
   → Cada qual com .srt + .txt + .png
```

### Fluxo C: Gravar e cortar live
```
1. Cola URL de live
   → yt-dlp --live-from-start baixa desde o começo
   
2. ⏺ Gravar
   → Arquivo cresce em ~/Videos/CortaLegenda/Lives/live_<data>/
   → Player atualiza a cada 10s (🔄 Atualizar)
   
3. Navegar na live (enquanto grava)
   → ⏪ volta 10s | ⏩ ao vivo | 1× / 1.5× / 2×
   
4. Marcar recorte
   → Botão 📍 Agora em Início/Fim (usa posição player)
   → Ou digita HH:MM:SS manualmente
   → Define título que virará o do GC
   
5. ✂️ Exportar corte
   → (Mesmo processamento que edição)
   → Sem esperar a live acabar
   
6. Resultado
   → MP4 + .srt + .txt + .png na pasta do projeto
```

---

## ⚙️ Configuração

### Diretórios
```
~/Videos/CortaLegenda/
  ├── Downloads/          # yt-dlp salva aqui
  ├── Out/                # Vídeos exportados
  ├── trilhas/            # .mp3 com climatização
  └── Lives/              # Gravações de live
      └── live_2025-09-06/
          ├── video.mp4   # Cresce enquanto grava
          └── audio.m4a

Projeto:
  ├── config.json         # Marca d'água, logo, cores, IA
  ├── app.py              # Executável
  └── .venv/              # Ambiente virtual (dev)
```

### config.json
```json
{
  "brand_name": "Informativo Nacional",
  "brand_colors": {
    "logo_bg": "#FFFFFF",
    "title_fg": "#FFFF00"
  },
  "watermark": "@RENANSANTOSMBL",
  "watermark_color": "#FFFF00",
  "ai_enabled": true,
  "api_key": "sk-...",
  "model": "deepseek-chat",
  "ai_reels": true,
  "censor_enabled": true,
  "censor_mute": true,
  "censor_caption": true,
  "censor_words": "palavra1, palavra2, put*",
  "music_enabled": true,
  "music_dir": "/path/to/trilhas",
  "whisper_model": "small"
}
```

### Variáveis de Ambiente
```bash
DEEPSEEK_API_KEY=sk-...   # API key da IA (alternativa a config.json)
```

---

## 🔧 API Interna

### MainWindow (app.py)

#### Métodos Públicos
```python
def pick_video()                    # Diálogo abrir arquivo
def pick_fixed_image()              # Escolher imagem fixa (9:16)
def pick_music_dir()                # Escolher pasta de trilhas

def download_video()                # yt-dlp com URL
def generate_captions()             # Whisper + revisão IA
def export_video()                  # FFmpeg cortar e processar

def toggle_play()                   # Play/pause do player VLC
def set_speed(rate)                 # Mudar velocidade (1.0, 1.5, 2.0)
def sync_cut_range()                # Validar fim > início

def cancel_edit()                   # Interromper processamento
def apply_style()                   # Aplicar tema escuro

def deliver_reels_caption()         # Enfileirar legenda Instagram
```

#### Threads (Workers)
```python
class SrtReviewWorker(QThread):
    progress = Signal(int, int)          # linhas prontas / total
    done = Signal(bool, str, str, str)   # ok?, msg, título, subtítulo
    flagged = Signal(list)               # trechos sensíveis
    music = Signal(str)                  # trilha escolhida

class CutsSuggestWorker(QThread):
    done = Signal(bool, str, list)       # ok?, msg, lista_cortes

class ReelsCaptionWorker(QThread):
    done = Signal(bool, str, object)     # ok?, msg, caminho .txt
```

#### VLC Player
```python
self.vlc_player                     # Player principal (vídeo)
self.csv_vlc_player                 # Player secundário (CSV preview)
self.live_vlc_player                # Player tertiary (live)

# Polling a 100ms via _poll_vlc()
self.timeline.setValue(pos_ms)      # Atualiza slider
self.time_label.setText(...)        # Mostra HH:MM:SS / HH:MM:SS
```

### Constantes Globais

```python
APP_NAME = "Corta+Legenda"
DEFAULT_WATERMARK = "@RENANSANTOSMBL"
DEFAULT_WM_COLOR = "#FFFF00"
MAX_CUT_SECONDS = 152  # Teto de duração dos cortes sugeridos pela IA

# Margem de legenda (libass units)
LT_CAPTION_MARGIN_V = 26
```

---

## 🐛 Debug e Troubleshooting

### Logs
- **Log de exportação** — visível na interface (painel abaixo do vídeo)
- **Saída do processo** — capturada linha a linha (FFmpeg, Whisper, yt-dlp)
- **Console Python** — erros não tratados (rodando via terminal)

### Arquivos Temporários
```
self.work_dir = Path(tempfile.mkdtemp(prefix="corta_legenda_"))
# Típico: C:\Users\...\AppData\Local\Temp\corta_legenda_abc123/
# Contém: audio.wav, .srt temporários, PNG do lower-third
```

### Problemas Comuns

| Problema | Causa | Solução |
|---|---|---|
| "FFmpeg não encontrado" | FFmpeg não está no PATH | Instalar FFmpeg e adicionar ao PATH |
| "Whisper não encontrado" | Dependências não instaladas | `pip install -r requirements.txt` |
| Legenda não aparece no vídeo | Arquivo .srt vazio | Verificar se Whisper transcreve |
| yt-dlp retorna HTTP 403 | URLs do YT expiraram | `--retries 10` tenta reextrair URLs |
| Áudio desincronizado | FFmpeg bug com AAC | Usar `aac -b:a 128k` (padrão) |
| Marca d'água sobrepõe legenda | Coordenadas configuradas | Ajustar MarginV em cg_generator.py |

---

## 📊 Estrutura de Dados

### SRT (Legendas)
```
1
00:00:00,000 --> 00:00:05,000
Primeira fala do vídeo.

2
00:00:05,000 --> 00:00:10,000
Segunda fala.
```

### CSV (Lote)
```csv
inicio,fim,titulo,formato,imagem,comentario
00:10,00:25,Abertura,estender,,Boa abertura
01:30,01:55,Reação,imagem,capas/reacao.png,Reação do público
```

### JSON (Config)
```json
{
  "brand_name": "...",
  "brand_colors": { "logo_bg": "...", ... },
  "ai_enabled": true,
  "api_key": "sk-...",
  ...
}
```

### Whisper JSON (Temporário)
```json
{
  "text": "Transcrição completa do áudio",
  "segments": [
    { "id": 0, "seek": 0, "start": 0.0, "end": 5.5, "text": " Primeira" },
    { "id": 1, "seek": ..., "start": 5.5, "end": 10.2, "text": " Segunda" }
  ]
}
```

---

## 🚀 Performance

### Tempos Típicos (vídeo 1 min, arquivo 100 MB)

| Operação | Tempo | Hardware |
|---|---|---|
| Download YT (HLS) | 30–60s | 8 conexões |
| Whisper (transcr.) | 20–40s | GPU ou CPU |
| DeepSeek (revisão) | 10–30s | Rede (API) |
| FFmpeg (exportação) | 15–30s | CPU |
| **Total com tudo** | 75–160s | ~2–3 min |

### Otimizações
- VLC: hardware decoding desligado (`QT_FFMPEG_NO_HWACCEL=1`)
- FFmpeg: preset `veryfast` (qualidade aceitável, rápido)
- Whisper: modelo `small` (equilibra qualidade/velocidade)
- yt-dlp: 8 conexões (padrão para HLS/DASH)

---

## 📝 Desenvolvimento

### Executar em Dev
```bash
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

### Build Executável
```bash
pyinstaller --noconsole --onefile --name CortaLegenda app.py
# Resultado: dist/CortaLegenda.exe (~50 MB, sem FFmpeg/Whisper)
```

### Adicionar Recurso
1. Editar `app.py` (UI) ou módulo relevante (lógica)
2. Testar via `python app.py`
3. Confirmar com `python -m py_compile app.py`
4. Build novo executável

### Testes
Não há suite de testes automatizados. Testes manuais:
1. Carregar vídeo local
2. Marcar recorte → exportar
3. Baixar YouTube → exportar
4. Lote CSV → gerar lote
5. Live → gravar + cortar
6. Legendas com/sem IA
7. Censura (áudio + texto)

---

## 📄 Licença e Créditos

- **FFmpeg** — LGPL
- **Whisper** — MIT (OpenAI)
- **yt-dlp** — Unlicense
- **PySide6** — LGPL
- **DeepSeek** — Comercial (API)

---

## 🔗 Referências

- [FFmpeg Docs](https://ffmpeg.org/ffmpeg.html)
- [Whisper GitHub](https://github.com/openai/whisper)
- [yt-dlp Docs](https://github.com/yt-dlp/yt-dlp)
- [PySide6 Docs](https://doc.qt.io/qtforpython-6/)
- [DeepSeek API](https://platform.deepseek.com/)

---

**Documentação atualizada em 2025-09-06**
