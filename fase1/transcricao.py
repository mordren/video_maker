"""Etapas 1 e 2: áudio e transcrição (SRT ao lado do vídeo ou Whisper).

As duas rotas saem no mesmo formato interno, uma lista de segmentos:
    {"start": float, "end": float, "text": str, "words": [{"word", "start", "end"}]}
Vindo de SRT, `words` fica vazio: a granularidade é por segmento, não por palavra.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("fase1")

_TEMPO = re.compile(r"(\d+:\d\d:\d\d[,.]\d+)\s+-->\s+(\d+:\d\d:\d\d[,.]\d+)")
# Tags de formatação e anotações do tipo "[música]" / "[aplausos]".
_TAGS = re.compile(r"<[^>]+>|\{[^}]*\}|\[[^\]]*\]")
_LOCUTOR = re.compile(r">>|»")


# ---------------------------------------------------------------------------
# Etapa 1 — áudio
# ---------------------------------------------------------------------------

def extrair_audio(video: Path, destino: Path) -> Path:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg não encontrado no PATH.")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vn",
                    "-ac", "1", "-ar", "16000", str(destino)], check=True)
    return destino


# ---------------------------------------------------------------------------
# Etapa 2, rota A — SRT
# ---------------------------------------------------------------------------

def candidatos_srt(video: Path) -> list[Path]:
    """SRTs do vídeo, na ordem de preferência.

    1. <nome>.srt  2. <nome>.pt*.srt (pt-BR > pt > pt-orig > outros pt)
    3. <nome>.en.srt  4. qualquer outro <nome>.*.srt
    """
    stem = video.stem
    ordem_pt = {"pt-br": 0, "pt": 1, "pt-orig": 2}
    achados: list[tuple[tuple, Path]] = []
    for p in video.parent.iterdir():
        if not p.is_file() or not p.name.lower().endswith(".srt"):
            continue
        if p.name == f"{stem}.srt":
            achados.append(((0, 0), p))
        elif p.name.startswith(f"{stem}."):
            lang = p.name[len(stem) + 1:-4].lower()
            if lang.startswith("pt"):
                achados.append(((1, ordem_pt.get(lang, 3)), p))
            elif lang == "en":
                achados.append(((2, 0), p))
            else:
                achados.append(((3, 0), p))
    return [p for _, p in sorted(achados, key=lambda t: (t[0], t[1].name))]


def ler_srt(path: Path) -> list[dict]:
    try:
        conteudo = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        conteudo = path.read_text(encoding="cp1252", errors="replace")
    brutos: list[tuple[float, float, str]] = []
    for entrada in re.split(r"\r?\n\s*\r?\n", conteudo.strip()):
        linhas = [l.strip() for l in entrada.splitlines() if l.strip()]
        i = next((k for k, l in enumerate(linhas) if _TEMPO.search(l)), None)
        if i is None:
            continue
        m = _TEMPO.search(linhas[i])
        texto = " ".join(linhas[i + 1:])
        texto = re.sub(r"\s+", " ", _LOCUTOR.sub(" ", _TAGS.sub(" ", texto))).strip()
        if texto:
            brutos.append((_segundos(m.group(1)), _segundos(m.group(2)), texto))
    return [{"start": round(s, 3), "end": round(e, 3), "text": t, "words": []}
            for s, e, t in _limpa_rolante(brutos)]


def _segundos(valor: str) -> float:
    h, m, s = valor.replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def _norm(texto: str) -> str:
    return re.sub(r"\s+", " ", texto).strip().strip(".,!?;:…").lower()


def _limpa_rolante(segs: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Tira as repetições da legenda automática do YouTube.

    Mesma regra do `utils.clean_caption_segments` do app: a legenda "rolante"
    repete ou estende a mesma frase por alguns segundos ("não" -> "não dá" ->
    "não dá pra negociar"). Fica só o texto mais completo, com o tempo estendido.
    Sem isso o texto de cada janela sairia com cada frase duas ou três vezes.
    """
    limpos: list[list] = []
    for start, end, texto in sorted(segs, key=lambda t: t[0]):
        norm = _norm(texto)
        dup = None
        for i in range(len(limpos) - 1, -1, -1):
            if limpos[i][1] < start - 6.0:
                break
            ant = _norm(limpos[i][2])
            if ant == norm or (ant and norm and (ant.startswith(norm) or norm.startswith(ant))):
                dup = i
                if len(texto) > len(limpos[i][2]):
                    limpos[i][2] = texto
                break
        if dup is not None:
            limpos[dup][1] = max(limpos[dup][1], end)
        else:
            limpos.append([start, end, texto])
    for i in range(len(limpos) - 1):
        if limpos[i][1] > limpos[i + 1][0]:
            limpos[i][1] = limpos[i + 1][0]
    return [(s, e, t) for s, e, t in limpos if e - s >= 0.12]


# ---------------------------------------------------------------------------
# Etapa 2, rota B — Whisper
# ---------------------------------------------------------------------------

def whisper_bin() -> str | None:
    achado = shutil.which("whisper")
    if achado:
        return achado
    for nome in ("whisper.exe", "whisper"):
        candidato = Path(sys.executable).parent / nome
        if candidato.exists():
            return str(candidato)
    return None


def rodar_whisper(audio: Path, pasta: Path, modelo: str, idioma: str) -> list[dict]:
    binario = whisper_bin()
    if not binario:
        raise RuntimeError("Whisper não encontrado (pip install openai-whisper).")
    cmd = [binario, str(audio), "--model", modelo, "--language", idioma,
           "--task", "transcribe", "--word_timestamps", "True",
           "--output_format", "json", "--output_dir", str(pasta)]
    try:
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError:
        # GPUs pequenas (ex.: 4GB) ficam sem VRAM quando o crop dinâmico já
        # está com os modelos de rosto carregados durante o lote de blocos —
        # cai para CPU em vez de derrubar o bloco inteiro.
        subprocess.run([*cmd, "--device", "cpu"], check=True)
    dados = json.loads((pasta / f"{audio.stem}.json").read_text(encoding="utf-8"))
    segs = []
    for s in dados.get("segments", []):
        texto = s.get("text", "").strip()
        if not texto:
            continue
        palavras = [{"word": w["word"].strip(), "start": round(w["start"], 3), "end": round(w["end"], 3)}
                    for w in s.get("words", []) if w.get("word", "").strip()]
        segs.append({"start": round(s["start"], 3), "end": round(s["end"], 3),
                     "text": texto, "words": palavras})
    return segs


# ---------------------------------------------------------------------------
# Etapa 2 — escolha da rota
# ---------------------------------------------------------------------------

def obter_transcricao(video: Path, audio: Path | None, pasta: Path, cfg: dict) -> tuple[list[dict], str]:
    """Devolve (segmentos, fonte). Fonte é "srt: <arquivo>" ou "whisper"."""
    for srt in candidatos_srt(video):
        try:
            segs = ler_srt(srt)
        except (OSError, ValueError) as exc:
            log.warning("SRT %s ilegível (%s); tentando o próximo", srt.name, exc)
            continue
        if len(segs) >= 5:
            return segs, f"srt: {srt.name}"
        log.warning("SRT %s vazio ou corrompido (%d legendas); tentando o próximo", srt.name, len(segs))
    if audio is None:
        raise RuntimeError("Sem SRT utilizável e sem áudio para rodar o Whisper.")
    log.info("Nenhum SRT utilizável ao lado do vídeo; transcrevendo com Whisper (%s)", cfg["whisper_modelo"])
    return rodar_whisper(audio, pasta, cfg["whisper_modelo"], cfg["whisper_idioma"]), "whisper"
