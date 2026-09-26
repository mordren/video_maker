"""Etapas 1 e 2: áudio e transcrição (SRT ao lado do vídeo ou Whisper).

As três rotas saem no mesmo formato interno, uma lista de segmentos:
    {"start": float, "end": float, "text": str, "words": [{"word", "start", "end"}]}
Vindo de SRT, `words` fica vazio: a granularidade é por segmento, não por palavra.

Entre as duas rotas de Whisper, `transcrever_audio` tenta primeiro a API
(openai/whisper-large-v3-turbo via OpenRouter, mesma chave do JEV/DeepSeek):
~10x mais rápido que o modelo local e um custo trivial (~US$0,0002/min de
áudio, medido em 26/09/2026). Cai pro Whisper local sem OPENROUTER_API_KEY
configurada ou se a chamada falhar.
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
# Etapa 2, rota C — Whisper pela API (OpenRouter, sem GPU/CPU local)
# ---------------------------------------------------------------------------

_API_MODELO = "openai/whisper-large-v3-turbo"
_API_JANELA = 600.0  # s por chamada — mantém o corpo da requisição (base64) num tamanho razoável


def _duracao_audio(audio: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(audio)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def _monta_segmentos(r: dict, offset: float) -> list[dict]:
    """A API devolve `words` achatado (todo o trecho) e `segments` sem as
    palavras dentro — reencaixa cada palavra no segmento mais próximo pelo
    tempo (não por uma janela com folga: duas janelas vizinhas se tocando
    duplicava a palavra da borda nos dois segmentos), no mesmo formato de
    rodar_whisper."""
    palavras = [{"word": w["word"].strip(), "start": round(w["start"] + offset, 3),
                "end": round(w["end"] + offset, 3)}
               for w in (r.get("words") or []) if w.get("word", "").strip()]
    segs = [{"start": round(s["start"] + offset, 3), "end": round(s["end"] + offset, 3),
            "text": s["text"].strip(), "words": []}
           for s in (r.get("segments") or []) if (s.get("text") or "").strip()]
    for w in palavras:
        if not segs:
            break
        alvo = min(segs, key=lambda s: abs(w["start"] - (s["start"] + s["end"]) / 2))
        alvo["words"].append(w)
    return segs


def _transcrever_trecho_api(audio_wav: Path, offset: float, duracao: float, idioma: str) -> list[dict]:
    import base64

    from openrouter import post

    recorte = audio_wav.with_name(f"{audio_wav.stem}_{int(offset)}.wav")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{offset:.3f}", "-i", str(audio_wav),
                    "-t", f"{duracao:.3f}", "-c", "copy", str(recorte)], check=True)
    try:
        payload = {
            "model": _API_MODELO,
            "input_audio": {"data": base64.b64encode(recorte.read_bytes()).decode(), "format": "wav"},
            "language": idioma,
            "response_format": "verbose_json",
            "timestamp_granularities": ["word", "segment"],
        }
        r = post("/v1/audio/transcriptions", payload, timeout=120, tentativas=3)
    finally:
        recorte.unlink(missing_ok=True)
    return _monta_segmentos(r, offset)


def rodar_whisper_api(audio_wav: Path, idioma: str) -> list[dict]:
    """Mesmo formato de rodar_whisper, mas pela API. Corta em janelas de
    10min (a maioria dos casos — um corte de alguns minutos — sai numa
    chamada só) pra manter o corpo da requisição num tamanho razoável."""
    duracao = _duracao_audio(audio_wav)
    segs: list[dict] = []
    inicio = 0.0
    while inicio < duracao:
        janela = min(_API_JANELA, duracao - inicio)
        segs += _transcrever_trecho_api(audio_wav, inicio, janela, idioma)
        inicio += janela
    return segs


def transcrever_audio(midia: Path, pasta: Path, modelo: str, idioma: str) -> tuple[list[dict], str]:
    """`midia` pode ser vídeo ou áudio. Tenta a API primeiro; sem
    OPENROUTER_API_KEY ou se a chamada falhar, cai pro Whisper local
    (rodar_whisper, que aceita vídeo ou áudio igual). Devolve (segmentos,
    fonte: "whisper-api" ou "whisper")."""
    from openrouter import APIError, api_key
    try:
        api_key()
    except APIError:
        return rodar_whisper(midia, pasta, modelo, idioma), "whisper"
    wav, limpar = (midia, False) if midia.suffix.lower() == ".wav" else \
        (pasta / f"{midia.stem}_api.wav", True)
    try:
        if limpar:
            extrair_audio(midia, wav)
        return rodar_whisper_api(wav, idioma), "whisper-api"
    except Exception as exc:  # noqa: BLE001 — API falhou, cai pro Whisper local
        log.warning("Whisper pela API falhou (%s); caindo pro Whisper local", exc)
        return rodar_whisper(midia, pasta, modelo, idioma), "whisper"
    finally:
        if limpar:
            wav.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Etapa 2 — escolha da rota
# ---------------------------------------------------------------------------

def obter_transcricao(video: Path, audio: Path | None, pasta: Path, cfg: dict) -> tuple[list[dict], str]:
    """Devolve (segmentos, fonte). Fonte é "srt: <arquivo>", "whisper-api" ou "whisper"."""
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
    log.info("Nenhum SRT utilizável ao lado do vídeo; transcrevendo")
    segs, fonte = transcrever_audio(audio, pasta, cfg["whisper_modelo"], cfg["whisper_idioma"])
    log.info("   transcrito com %s", fonte)
    return segs, fonte
