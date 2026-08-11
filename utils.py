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
DEFAULT_LOGO = PROJECT_DIR / "assets" / "default-logo.png"
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
    "legenda": {"legenda", "caption", "subtitle", "texto"},
    "formato": {"formato", "format", "modo", "mode", "tipo"},
    "imagem": {"imagem", "image", "img", "capa", "foto"},
    "comentario": {"comentario", "comentário", "comment", "notas", "observacao", "observação"},
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

    Devolve lista de dicts: {start_s, end_s, label, legenda, formato, image_path, comentario}.
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
            "legenda": cell(row, "legenda"),
            "formato": normalize_format(cell(row, "formato")),
            "image_path": image_path,
            "comentario": cell(row, "comentario"),
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
