"""Transição sonora + slow motion entre a abertura (P&B) e o corte principal.

O `pipeline_cortes.py` encolhe as pausas longas (silêncio) do vídeo — de
propósito, é o que faz o corte ficar enxuto. Só que isso também tira a
"gordura" de vídeo neutro que normalmente cobriria um efeito sonoro de
transição sem parecer um buraco. Em vez de colar abertura+principal direto
(como antes), o FINAL da abertura é desacelerado até durar exatamente o
tempo do som de transição sorteado, a voz original é abafada nesse trecho, e
o efeito sonoro (whoosh/câmera) entra por cima — o vídeo "estica" visualmente
para caber o som, em vez de deixar um vazio.
"""

from __future__ import annotations

import random
import subprocess
from pathlib import Path

_EXTENSOES = ("*.mp3", "*.MP3", "*.wav", "*.WAV")


def sortear_som(pasta: Path) -> Path | None:
    sons: list[Path] = []
    for padrao in _EXTENSOES:
        sons.extend(pasta.glob(padrao))
    return random.choice(sorted(set(sons))) if sons else None


def duracao_de(arquivo: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(arquivo)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def aplicar(abertura: Path, whoosh: Path, fator_slow: float, volume_whoosh: float,
           volume_voz_no_slow: float) -> None:
    """Reescreve `abertura` (in-place) com o slow motion + o som sobreposto no final.

    O trecho que vira slow motion é sempre `duracao_do_whoosh / fator_slow`
    segundos do FINAL da abertura — depois de esticado por `fator_slow`, esse
    trecho passa a durar exatamente `duracao_do_whoosh`, então o efeito sonoro
    cabe perfeitamente sobre ele, sem sobrar nem faltar tempo de vídeo.
    """
    dur_whoosh = duracao_de(whoosh)
    dur_abertura = duracao_de(abertura)
    duracao_slow_original = min(dur_whoosh / fator_slow, dur_abertura * 0.9)
    corte = max(0.0, dur_abertura - duracao_slow_original)
    duracao_slow_esticada = (dur_abertura - corte) * fator_slow

    # atempo só aceita 0.5..2.0 por chamada — fator_slow fica limitado a 2x
    # nesta implementação (documentado em config_cortes.yaml).
    inv_fator = max(0.5, min(2.0, 1 / fator_slow))

    intermediario = abertura.with_name(abertura.stem + "_slow.mp4")
    final = abertura.with_name(abertura.stem + "_transicao.mp4")
    filtro1 = (
        f"[0:v]trim=start=0:end={corte},setpts=PTS-STARTPTS[v0];"
        f"[0:v]trim=start={corte},setpts=PTS-STARTPTS,setpts={fator_slow}*PTS[v1];"
        f"[v0][v1]concat=n=2:v=1:a=0[vout];"
        f"[0:a]atrim=start=0:end={corte},asetpts=PTS-STARTPTS,aformat=channel_layouts=stereo[a0];"
        f"[0:a]atrim=start={corte},asetpts=PTS-STARTPTS,atempo={inv_fator},"
        f"volume={volume_voz_no_slow},afade=t=out:st=0:d={duracao_slow_esticada},"
        f"aformat=channel_layouts=stereo[a1];"
        f"[a0][a1]concat=n=2:v=0:a=1[aout]"
    )
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(abertura),
                    "-filter_complex", filtro1, "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k", str(intermediario)],
                   check=True, capture_output=True)

    corte_ms = int(round(corte * 1000))
    filtro2 = (
        f"[1:a]aformat=channel_layouts=stereo,adelay={corte_ms}|{corte_ms},volume={volume_whoosh}[w];"
        f"[0:a]aformat=channel_layouts=stereo[voz];"
        f"[voz][w]amix=inputs=2:duration=first:dropout_transition=0[aout]"
    )
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(intermediario),
                        "-i", str(whoosh), "-filter_complex", filtro2,
                        "-map", "0:v", "-map", "[aout]",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(final)],
                       check=True, capture_output=True)
    finally:
        intermediario.unlink(missing_ok=True)

    final.replace(abertura)
