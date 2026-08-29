"""Utilitários partilhados entre os módulos do Corta+Legenda."""

from __future__ import annotations

import csv
import re
import shutil
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLineEdit

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------

APP_NAME = "Corta+Legenda"
OUTPUT_DIR = Path.home() / "Videos" / "CortaLegenda"
PROJECT_DIR = Path(__file__).resolve().parent
YTDLP_BUNDLED = PROJECT_DIR / "tools" / "yt-dlp.exe"
YTDLP_SYSTEM = Path(r"C:\Program Files (x86)\ytdlp\yt-dlp.exe")
FONT_DIR = PROJECT_DIR / "assets" / "fonts"
DOWNLOAD_DIR = OUTPUT_DIR / "Downloads"

# ---------------------------------------------------------------------------
# Funções utilitárias
# ---------------------------------------------------------------------------


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def yt_dlp_path() -> Path | None:
    """Prefere a cópia atualizada que acompanha o projeto."""
    if YTDLP_BUNDLED.exists():
        return YTDLP_BUNDLED
    if YTDLP_SYSTEM.exists():
        return YTDLP_SYSTEM
    found = shutil.which("yt-dlp")
    return Path(found) if found else None


def whisper_path() -> str | None:
    """
    Localiza o executável do Whisper.

    `shutil.which("whisper")` falha quando o app roda pelo python.exe do venv
    sem o ambiente ativado (o Scripts/ não está no PATH). Por isso procuramos
    também ao lado do interpretador atual — que é onde o pip instala o
    whisper.exe dentro do venv.
    """
    found = shutil.which("whisper")
    if found:
        return found
    scripts_dir = Path(sys.executable).parent
    for name in ("whisper.exe", "whisper"):
        candidate = scripts_dir / name
        if candidate.exists():
            return str(candidate)
    return None


def filter_path(path: Path) -> str:
    """Escapa um caminho local para uso dentro de filtro FFmpeg."""
    value = path.resolve().as_posix().replace(":", r"\:")
    return value.replace("'", r"\'")


def escape_drawtext(text: str) -> str:
    """Escapa texto para usar em filtro drawtext do FFmpeg.

    Usa syntaxe: text='...' com escape de caracteres especiais.
    """
    # Escape order: backslash first, then outros
    text = text.replace("\\", "\\\\")  # \ -> \\
    text = text.replace("'", "\\'")    # ' -> \'
    text = text.replace(":", "\\:")    # : -> \:
    text = text.replace("%", "\\%")    # % -> \%
    return text


def format_title_for_video(title: str) -> str:
    """Processa título para exibição em vídeo: UPPERCASE + escape para FFmpeg."""
    title = title.strip().upper()
    return escape_drawtext(title)


def srt_has_content(path: Path | None) -> bool:
    """True se o SRT existe e tem ao menos uma legenda com marcação de tempo.

    O libass falha ("Unable to open") num .srt vazio — o que acontece quando o
    Whisper não detecta fala no trecho. Esta checagem evita quebrar o FFmpeg.
    """
    if not path or not path.exists():
        return False
    try:
        content = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return False
    return "-->" in content


def find_video_subtitle(video_path: Path | None) -> Path | None:
    """Procura uma legenda .srt em pt ao lado do vídeo (ex.: baixada pelo yt-dlp).

    Aceita "<nome>.srt" e "<nome>.<lang>.srt" quando <lang> começa com "pt"
    (pt-BR, pt, pt-orig...). Preferência: pt-BR > pt > pt-orig > "<nome>.srt".
    Devolve o primeiro com conteúdo válido, ou None.
    """
    if not video_path:
        return None
    folder = video_path.parent
    stem = video_path.stem
    if not folder.exists():
        return None
    candidates: list[tuple[int, Path]] = []
    for p in folder.iterdir():
        if not p.is_file() or not p.name.lower().endswith(".srt"):
            continue
        if p.name == f"{stem}.srt":
            candidates.append((3, p))
        elif p.name.startswith(f"{stem}."):
            lang = p.name[len(stem) + 1:-4].lower()   # parte entre o nome e .srt
            if lang.startswith("pt"):
                rank = {"pt-br": 0, "pt": 1, "pt-orig": 2}.get(lang, 2)
                candidates.append((rank, p))
    candidates.sort(key=lambda t: t[0])
    for _, p in candidates:
        if srt_has_content(p):
            return p
    return None


def srt_seconds(value: str) -> float:
    hours, minutes, rest = value.replace(",", ".").split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(rest)


def srt_timestamp(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"


def as_time(seconds: float) -> str:
    """Converte segundos para string HH:MM:SS ou MM:SS."""
    total_seconds = max(0, int(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def parse_time_string(raw: str) -> float:
    """Interpreta MM:SS, HH:MM:SS ou segundos puros. Retorna segundos."""
    raw = raw.strip()
    if not raw:
        return 0.0
    if ":" in raw:
        parts = [float(p) for p in raw.replace(",", ".").split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
    try:
        return float(raw)
    except ValueError:
        return 0.0


def caption_groups(text: str, duration: float) -> list[str]:
    """Divide uma fala longa em blocos curtos, adequados a vídeo vertical."""
    words = text.split()
    if len(words) <= 4:
        return [text]
    groups: list[str] = []
    current: list[str] = []
    for word in words:
        candidate = " ".join([*current, word])
        if current and (len(current) >= 4 or len(candidate) > 25):
            groups.append(" ".join(current))
            current = [word]
        else:
            current.append(word)
    if current:
        groups.append(" ".join(current))
    return groups


def shorten_srt_captions(path: Path) -> None:
    """Reescreve o SRT com até quatro palavras por legenda e tempos proporcionais."""
    content = path.read_text(encoding="utf-8-sig")
    entries = re.split(r"\r?\n\s*\r?\n", content.strip())
    output: list[str] = []
    number = 1
    time_pattern = re.compile(r"(\d\d:\d\d:\d\d[,.]\d\d\d)\s+-->\s+(\d\d:\d\d:\d\d[,.]\d\d\d)")
    for entry in entries:
        lines = [line.strip() for line in entry.splitlines() if line.strip()]
        time_index = next((index for index, line in enumerate(lines) if time_pattern.fullmatch(line)), None)
        if time_index is None:
            continue
        match = time_pattern.fullmatch(lines[time_index])
        assert match is not None
        start, end = srt_seconds(match.group(1)), srt_seconds(match.group(2))
        text = " ".join(lines[time_index + 1:])
        groups = caption_groups(text, end - start)
        weights = [max(1, len(group)) for group in groups]
        total_weight = sum(weights)
        current = start
        for index, group in enumerate(groups):
            next_time = end if index == len(groups) - 1 else current + (end - start) * weights[index] / total_weight
            output.extend([str(number), f"{srt_timestamp(current)} --> {srt_timestamp(next_time)}", group, ""])
            number += 1
            current = next_time
    path.write_text("\n".join(output), encoding="utf-8")


# Formatos de saída reconhecidos e seus apelidos na coluna "formato" do CSV.
FORMAT_ALIASES = {
    "estender": {"estender", "preencher", "crop", "fill", "short", "9:16"},
    "transparente": {"transparente", "desfocado", "blur", "centralizado", "centro"},
    "imagem": {"imagem", "imagem fixa", "image", "capa", "foto", "split"},
    "original": {"original", "longo", "16:9", "cheio", "nenhum"},
}

# Nomes de coluna aceitos no cabeçalho do CSV (tudo minúsculo, sem acento importa).
_COLUMN_ALIASES = {
    "start": {"inicio", "início", "start", "in", "comeco", "começo"},
    "end": {"fim", "end", "out", "final"},
    "label": {"titulo", "título", "title", "nome", "label"},
    "subtitulo": {"subtitulo", "subtítulo", "subtitle", "chapeu", "chapéu", "kicker"},
    "legenda": {"legenda", "caption", "texto"},
    "formato": {"formato", "format", "modo", "mode", "tipo"},
    "imagem": {"imagem", "image", "img", "capa", "foto"},
}


def normalize_format(raw: str) -> str:
    """Converte o texto da coluna 'formato' num dos modos internos, ou "" se vazio/desconhecido."""
    value = raw.strip().lower()
    if not value:
        return ""
    for mode, aliases in FORMAT_ALIASES.items():
        if value in aliases:
            return mode
    return ""


def parse_csv_moments(csv_path: Path) -> list[dict]:
    """
    Lê um CSV com momentos para cortar.

    Aceita duas formas:
      • Com cabeçalho (recomendado): colunas inicio, fim, titulo, legenda,
        formato, imagem — em qualquer ordem, identificadas pelo nome.
      • Sem cabeçalho (legado): posições inicio, fim, titulo, legenda.

    Colunas novas e opcionais:
      • formato: estender / transparente / imagem / original. Vazio = usa o
        formato padrão escolhido na interface.
      • imagem: caminho da imagem fixa (usado só quando formato=imagem).
        Caminhos relativos são resolvidos a partir da pasta do próprio CSV.

    Devolve lista de dicts: {start_s, end_s, label, subtitulo, legenda, formato, image_path}.
    """
    base_dir = csv_path.resolve().parent
    # Tenta múltiplas codificações
    encodings = ["utf-8-sig", "utf-8", "latin-1", "cp1252", "iso-8859-1"]
    data = None
    for enc in encodings:
        try:
            with csv_path.open("r", encoding=enc, newline="") as f:
                data = [row for row in csv.reader(f)]
            break
        except (UnicodeDecodeError, LookupError):
            continue
    if data is None:
        return []
    data = [row for row in data if row and any(c.strip() for c in row)]
    if not data:
        return []

    header = [c.strip().lower() for c in data[0]]
    has_header = any(name in _COLUMN_ALIASES["start"] for name in header)
    if has_header:
        col: dict[str, int] = {}
        for idx, name in enumerate(header):
            for key, aliases in _COLUMN_ALIASES.items():
                if name in aliases and key not in col:
                    col[key] = idx
        body = data[1:]
    else:
        col = {"start": 0, "end": 1, "label": 2, "legenda": 3}
        body = data

    def cell(row: list[str], key: str) -> str:
        idx = col.get(key)
        if idx is None or idx >= len(row):
            return ""
        return row[idx].strip()

    rows: list[dict] = []
    for row in body:
        start_raw, end_raw = cell(row, "start"), cell(row, "end")
        if not start_raw or not end_raw:
            continue
        start_s, end_s = parse_time_string(start_raw), parse_time_string(end_raw)
        if end_s <= start_s:
            continue
        titulo = cell(row, "label").strip("'\"") or f"Corte {len(rows) + 1}"
        imagem = cell(row, "imagem")
        image_path: Path | None = None
        if imagem:
            candidate = Path(imagem)
            image_path = candidate if candidate.is_absolute() else base_dir / candidate
        rows.append({
            "start_s": start_s,
            "end_s": end_s,
            "label": titulo,
            "subtitulo": cell(row, "subtitulo").strip("'\""),
            "legenda": cell(row, "legenda"),
            "formato": normalize_format(cell(row, "formato")),
            "image_path": image_path,
        })
    return rows


def build_clip_filter(mode: str, has_image: bool, image_input: int = 1) -> str:
    """
    Monta a cadeia filter_complex que converte o vídeo (entrada 0) para 9:16,
    terminando no rótulo [base]. Quando mode=="imagem", a entrada `image_input`
    é a imagem fixa que vai no topo.
    """
    if mode == "estender":
        return ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                "crop=1080:1920,setsar=1[base]")
    if mode == "transparente":
        # Fundo desfocado em baixa resolução (540x960) e depois ampliado — o
        # resultado é visualmente igual, mas ~6x mais rápido que desfocar em
        # 1080x1920.
        return ("[0:v]split=2[bg][fg];"
                "[bg]scale=540:960:force_original_aspect_ratio=increase,"
                "crop=540:960,boxblur=10:2,scale=1080:1920[blur];"
                "[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fit];"
                "[blur][fit]overlay=(W-w)/2:(H-h)/2,setsar=1[base]")
    if mode == "imagem" and has_image:
        # Dois blocos 16:9 (1080x608) empilhados: imagem em cima, corte embaixo,
        # centralizados no quadro 1080x1920 com bordas pretas.
        return (f"[{image_input}:v]scale=1080:608:force_original_aspect_ratio=decrease,"
                "pad=1080:608:(ow-iw)/2:(oh-ih)/2:black,setsar=1[top];"
                "[0:v]scale=1080:608:force_original_aspect_ratio=decrease,"
                "pad=1080:608:(ow-iw)/2:(oh-ih)/2:black,setsar=1[bot];"
                "[top][bot]vstack=inputs=2[stack];"
                "[stack]pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1[base]")
    # original / longo / fallback: mantém o vídeo como está.
    return "[0:v]null[base]"


def parse_srt_segments(path: Path) -> list[tuple[float, float, str]]:
    """Lê um SRT e devolve [(inicio_s, fim_s, texto), ...]."""
    try:
        content = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        content = path.read_text(encoding="cp1252", errors="replace")
    time_pattern = re.compile(r"(\d\d:\d\d:\d\d[,.]\d\d\d)\s+-->\s+(\d\d:\d\d:\d\d[,.]\d\d\d)")
    segments: list[tuple[float, float, str]] = []
    for entry in re.split(r"\r?\n\s*\r?\n", content.strip()):
        lines = [line.strip() for line in entry.splitlines() if line.strip()]
        time_index = next((i for i, line in enumerate(lines) if time_pattern.fullmatch(line)), None)
        if time_index is None:
            continue
        match = time_pattern.fullmatch(lines[time_index])
        text = " ".join(lines[time_index + 1:]).strip()
        if text:
            segments.append((srt_seconds(match.group(1)), srt_seconds(match.group(2)), text))
    return segments


def segments_to_srt(segments: list[tuple[float, float, str]], clip_start: float,
                    clip_end: float, output_path: Path) -> bool:
    """
    Filtra os segmentos que caem dentro de [clip_start, clip_end] e escreve um
    SRT com tempos relativos ao início do corte. Devolve False se nada caiu no
    intervalo.
    """
    selected = [(max(s, clip_start) - clip_start, min(e, clip_end) - clip_start, text)
                for s, e, text in segments if e > clip_start and s < clip_end]
    if not selected:
        return False
    lines: list[str] = []
    for i, (s, e, text) in enumerate(selected, start=1):
        lines.extend([str(i), f"{srt_timestamp(s)} --> {srt_timestamp(e)}", text, ""])
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return True


def build_srt_for_clip(start_s: float, end_s: float, text: str, output_path: Path) -> None:
    """
    Cria um ficheiro SRT com uma única legenda que cobre a duração inteira do clipe.
    O texto é dividido em blocos de até 4 palavras.
    """
    groups = caption_groups(text, end_s - start_s) if text else [" "]
    lines: list[str] = []
    weights = [max(1, len(g)) for g in groups]
    total_weight = sum(weights)
    current = 0.0
    for i, group in enumerate(groups):
        next_t = end_s - start_s if i == len(groups) - 1 else current + (end_s - start_s) * weights[i] / total_weight
        lines.append(str(i + 1))
        lines.append(f"{srt_timestamp(current)} --> {srt_timestamp(next_t)}")
        lines.append(group)
        lines.append("")
        current = next_t
    output_path.write_text("\n".join(lines), encoding="utf-8")


def review_srt_with_ai(path: Path, api_key: str, model: str = "", context: str = "",
                       progress=None, with_title: bool = False
                       ) -> tuple[int, int, dict, str, str, list[dict]]:
    """Manda as falas do SRT para a IA e regrava o arquivo já corrigido.

    Os tempos são os do arquivo original — a IA só devolve texto, e o SRT é
    remontado aqui. Assim, mesmo que o modelo responda algo estranho, a legenda
    não sai do lugar em relação ao áudio.

    Com `with_title`, a mesma requisição também devolve um título e um subtítulo
    (um único JSON com as chaves titulo/subtitulo/linhas) — usado só na Edição.

    Devolve (linhas alteradas, total de linhas, uso de tokens, título, subtítulo,
    trechos sensíveis). Título e subtítulo vêm vazios quando `with_title` é
    False; os trechos sensíveis vêm nos dois casos.
    """
    import ai_srt

    segments = parse_srt_segments(path)
    if not segments:
        return 0, 0, {}, "", "", []

    originals = [text for _, _, text in segments]
    if with_title:
        titulo, subtitulo, fixed, sensiveis, usage = ai_srt.review_lines_with_title(
            originals, api_key, model, context)
        if progress:
            progress(len(fixed), len(fixed))
    else:
        fixed, sensiveis, usage = ai_srt.correct_lines(
            originals, api_key, model, context, progress)
        titulo, subtitulo = "", ""

    lines: list[str] = []
    changed = 0
    for i, ((start, end, old), new) in enumerate(zip(segments, fixed), start=1):
        if new != old:
            changed += 1
        lines.extend([str(i), f"{srt_timestamp(start)} --> {srt_timestamp(end)}", new, ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    # O tempo de cada achado sai do SRT, não da IA: ela só devolve o número da
    # linha, e os tempos nunca saem daqui.
    for item in sensiveis:
        index = item["linha"] - 1
        item["inicio"] = segments[index][0] if 0 <= index < len(segments) else 0.0
    return changed, len(segments), usage, titulo, subtitulo, sensiveis


# Prompt da legenda de Instagram, mandado ao DeepSeek com a fala do corte.
#
# A resposta vai inteira para um .txt que o usuário cola no Instagram, e é isso
# que dita as regras aqui: nada de markdown (o Instagram mostra os asteriscos
# como estão e o post fica sujo), nada de títulos de seção, e nada de sugestão
# de cortes — o vídeo já é um short pronto. Só o texto que vai no post.
REELS_PROMPT_TEMPLATE = """Você escreve legendas de Instagram para Reels de vídeos \
políticos curtos.

Abaixo está a transcrição da fala de um trecho de entrevista ou debate. Com base \
APENAS no que é dito nela, escreva a legenda do post.

Formato da resposta — siga à risca:
- Responda SOMENTE com o texto da legenda, exatamente como ele vai ser colado no \
Instagram. Nada de título, cabeçalho de seção, numeração, introdução, explicação \
ou comentário seu.
- Texto puro. Nada de asteriscos, markdown, negrito, itálico ou marcadores: o \
Instagram exibe esses símbolos literalmente e o post fica sujo.
- Emojis pode, com parcimônia.
- Não sugira cortes nem trilha sonora: o vídeo já está pronto.

Escreva nesta ordem, separando cada parte com uma linha em branco:
1. Uma frase de impacto tirada da fala, entre aspas, seguida de uma pergunta que \
provoque o leitor.
2. Dois parágrafos curtos sobre o embate, apresentando os dois lados sem tomar \
partido.
3. Uma pergunta direta ao leitor, chamando o comentário.
4. Uma última linha só com as hashtags.

Hashtags: comece por #eleicao2026 e #renansantos e acrescente de 5 a 8 sobre o \
tema. Cada hashtag é uma palavra só — sem espaço no meio, sem acento, em \
minúsculas.

Escreva em português do Brasil, com tom direto e engajador. Não invente fatos que \
não estão na fala.

Transcrição:
{legenda}"""


def reels_prompt_path(dest: Path) -> Path:
    """Caminho do .txt de legenda para o Reels, ao lado do vídeo."""
    return dest.with_name(f"{dest.stem} - legenda instagram.txt")


def build_reels_prompt(srt_path: Path) -> str:
    """Monta o prompt de legenda com a fala do corte.

    Vai só o texto, sem os tempos: o modelo escreve a legenda do post, não
    sugestões de corte, e mandar os timestamps só gastaria tokens à toa.
    Devolve "" quando não há legenda — sem a fala não há o que resumir.
    """
    if not srt_has_content(srt_path):
        return ""
    fala = " ".join(text for _, _, text in parse_srt_segments(srt_path) if text)
    if not fala.strip():
        return ""
    return REELS_PROMPT_TEMPLATE.format(legenda=fala.strip())


# Asteriscos, títulos e marcadores viram lixo visível na caixa do Instagram.
# O prompt já pede texto puro, mas pedir não garante — isto é a rede de baixo.
_MD_EMPHASIS = re.compile(r"\*{1,3}(.+?)\*{1,3}")


def strip_markdown(text: str) -> str:
    """Tira a formatação que o Instagram não entende, deixando o texto limpo."""
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if len(line) >= 3 and set(line) <= set("-—_=*#"):
            continue                                  # linha separadora
        # O espaço depois dos # é o que separa um título markdown ("## Bloco")
        # de uma hashtag ("#eleicao2026") — sem ele, a primeira hashtag da
        # última linha perderia o #.
        line = re.sub(r"^#{1,6}\s+", "", line)         # ### título
        line = re.sub(r"^\d+\.\s+", "", line)          # 1. numeração
        line = re.sub(r"^[-*•]\s+", "", line)          # - marcador
        line = _MD_EMPHASIS.sub(r"\1", line)           # **negrito**, *itálico*
        lines.append(line.replace("`", ""))
    # Uma linha em branco basta para separar parágrafos; sobras viram espaço morto.
    clean: list[str] = []
    for line in lines:
        if line or (clean and clean[-1]):
            clean.append(line)
    return "\n".join(clean).strip()


def write_reels_text(dest: Path, content: str) -> Path | None:
    """Grava o .txt de legenda ao lado do vídeo. Devolve o caminho, ou None."""
    if not content:
        return None
    txt_path = reels_prompt_path(dest)
    try:
        txt_path.write_text(content, encoding="utf-8")
    except OSError:
        return None
    return txt_path


def write_reels_prompt(srt_path: Path, dest: Path) -> Path | None:
    """Grava o prompt sem preencher — a saída para quando a IA não está à mão.

    Sem chave da API (ou com a chamada falhando), o usuário ainda leva o texto
    pronto para colar numa IA por conta própria, em vez de ficar sem nada.
    """
    prompt = build_reels_prompt(srt_path)
    if not prompt:
        return None
    return write_reels_text(dest, prompt)


def thumbnail_path(video_path: Path) -> Path:
    """Caminho do PNG de post que acompanha o vídeo exportado."""
    return video_path.with_suffix(".png")


def render_thumbnail(command: list[str], thumb_path: Path) -> Path | None:
    """Renderiza o frame de post com um comando FFmpeg de 1 frame.

    O comando é o mesmo da exportação, mas sem o filtro `subtitles` — assim a
    imagem sai com o GC e sem legenda escrita. Retorna o PNG, ou None se falhar.
    """
    import subprocess

    try:
        subprocess.run(command, capture_output=True, timeout=60, check=True)
        return thumb_path if thumb_path.exists() else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Widget reutilizável
# ---------------------------------------------------------------------------


class TimestampInput(QLineEdit):
    """Campo de tempo no formato MM:SS, armazenado internamente em segundos."""

    secondsChanged = Signal(float)

    def __init__(self) -> None:
        super().__init__("00:00")
        self._seconds = 0.0
        self._maximum = 0.0
        self.setPlaceholderText("00:00")
        self.setToolTip("Formato MM:SS, por exemplo 30:10 para 30 minutos e 10 segundos.")
        self.editingFinished.connect(self.commit)

    def value(self) -> float:
        return self._seconds

    def setMaximum(self, maximum: float) -> None:
        self._maximum = max(0.0, maximum)
        self.setValue(self._seconds)

    def setValue(self, seconds: float) -> None:
        value = min(max(0.0, seconds), self._maximum) if self._maximum else max(0.0, seconds)
        changed = value != self._seconds
        self._seconds = value
        total = int(round(value))
        minutes, remainder = divmod(total, 60)
        self.setText(f"{minutes:02d}:{remainder:02d}")
        if changed:
            self.secondsChanged.emit(value)

    def commit(self) -> None:
        raw = self.text().strip()
        try:
            minutes_text, seconds_text = raw.split(":", 1)
            minutes, seconds = int(minutes_text), int(seconds_text)
            if minutes < 0 or not 0 <= seconds < 60:
                raise ValueError
            self.setValue(minutes * 60 + seconds)
        except ValueError:
            self.setValue(self._seconds)
