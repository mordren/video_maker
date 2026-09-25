"""Monta o plano de corte final (silêncio encolhido + recomeço) e renderiza com FFmpeg.

Um corte aqui é sempre um intervalo [inicio, fim] a REMOVER do clipe. A lista
final de cortes vira o complementar — os trechos a MANTER — e o FFmpeg concatena
esses trechos com `trim`/`atrim` + `concat`, sem reencode entre pedaços dentro do
mesmo filtro (um único encode no final). O áudio ganha `loudnorm` na mesma
passada.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def mescla_cortes(cortes: list[dict]) -> list[dict]:
    """Cortes que se sobrepõem (ex.: um recomeço dentro de uma pausa longa) viram um só."""
    ordenados = sorted(cortes, key=lambda c: c["inicio"])
    mesclados: list[dict] = []
    for c in ordenados:
        if mesclados and c["inicio"] <= mesclados[-1]["fim"]:
            mesclados[-1]["fim"] = max(mesclados[-1]["fim"], c["fim"])
            mesclados[-1]["motivo"] += " + " + c["motivo"]
        else:
            mesclados.append(dict(c))
    return mesclados


def trechos_a_manter(cortes: list[dict], inicio: float, fim: float,
                     duracao_minima: float = 0.05) -> list[tuple[float, float]]:
    """Complementar dos cortes dentro de [inicio, fim] — o que sobra no clipe final."""
    manter = []
    cursor = inicio
    for c in cortes:
        if c["inicio"] > cursor:
            manter.append((cursor, min(c["inicio"], fim)))
        cursor = max(cursor, c["fim"])
    if cursor < fim:
        manter.append((cursor, fim))
    return [(round(a, 3), round(b, 3)) for a, b in manter if b - a >= duracao_minima]


def _filtro_corte(manter: list[tuple[float, float]], tem_video: bool, cfg_loud: dict,
                  crossfade_ms: int = 15, escala_de_cinza: bool = False) -> tuple[str, list[str]]:
    n = len(manter)
    fade_s = crossfade_ms / 1000
    partes = []
    for idx, (ini, fim) in enumerate(manter):
        if tem_video:
            cadeia_v = f"[0:v]trim=start={ini}:end={fim},setpts=PTS-STARTPTS"
            if escala_de_cinza:
                cadeia_v += ",hue=s=0"  # dessatura sem trocar o pixel format (mais seguro que format=gray)
            partes.append(f"{cadeia_v}[v{idx}]")
        cadeia = f"[0:a]atrim=start={ini}:end={fim},asetpts=PTS-STARTPTS"
        # fade só nas junções internas (não no começo/fim do clipe inteiro) — evita
        # o "clique" audível no ponto de corte sem perder o início/fim reais do bloco.
        dur = fim - ini
        if 2 * fade_s < dur:
            if idx > 0:
                cadeia += f",afade=t=in:st=0:d={fade_s}"
            if idx < n - 1:
                cadeia += f",afade=t=out:st={dur - fade_s}:d={fade_s}"
        partes.append(f"{cadeia}[a{idx}]")
    loud = (f"loudnorm=I={cfg_loud['alvo_lufs']}:TP={cfg_loud['true_peak']}"
            f":LRA={cfg_loud['faixa_lra']}")
    if tem_video:
        entradas = "".join(f"[v{i}][a{i}]" for i in range(n))
        partes.append(f"{entradas}concat=n={n}:v=1:a=1[vraw][araw]")
        partes.append(f"[araw]{loud}[aout]")
        filtro = ";".join(partes)
        return filtro, ["-map", "[vraw]", "-map", "[aout]"]
    entradas = "".join(f"[a{i}]" for i in range(n))
    partes.append(f"{entradas}concat=n={n}:v=0:a=1[araw]")
    partes.append(f"[araw]{loud}[aout]")
    filtro = ";".join(partes)
    return filtro, ["-map", "[aout]"]


def renderizar(origem: Path, destino: Path, manter: list[tuple[float, float]],
               tem_video: bool, cfg_loud: dict, crossfade_ms: int = 15,
               escala_de_cinza: bool = False) -> None:
    if not manter:
        raise ValueError("nada sobrou para renderizar (todos os trechos foram cortados)")
    filtro, mapas = _filtro_corte(manter, tem_video, cfg_loud, crossfade_ms, escala_de_cinza)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(origem), "-filter_complex", filtro,
           *mapas]
    if tem_video:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "192k"]
    cmd.append(str(destino))
    subprocess.run(cmd, check=True, capture_output=True)


def duracao_de(arquivo: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(arquivo)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def concatenar_com_fade(clipe1: Path, clipe2: Path, destino: Path, duracao_fade: float = 0.35,
                        tipo: str = "fade") -> None:
    """Cola `clipe2` depois de `clipe1` com um crossfade de vídeo+áudio, não um
    corte seco — `xfade` sobrepõe os últimos `duracao_fade` segundos de um com
    os primeiros do outro (a duração final é dur1+dur2-fade). `tipo` é
    qualquer transição do filtro `xfade` do FFmpeg ("fade" é o dissolve
    clássico). Reencode: precisa, o corte seco anterior (demuxer concat) não
    permite misturar os dois clipes."""
    dur1 = duracao_de(clipe1)
    dur2 = duracao_de(clipe2)
    fade = max(0.05, min(duracao_fade, dur1 * 0.9, dur2 * 0.9))
    offset = max(0.0, dur1 - fade)
    filtro = (
        f"[0:v][1:v]xfade=transition={tipo}:duration={fade}:offset={offset}[vout];"
        f"[0:a][1:a]acrossfade=d={fade}[aout]"
    )
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clipe1), "-i", str(clipe2),
                    "-filter_complex", filtro, "-map", "[vout]", "-map", "[aout]",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                    "-c:a", "aac", "-b:a", "192k", str(destino)], check=True, capture_output=True)


def concatenar(clipes: list[Path], destino: Path) -> None:
    """Concatena clipes JÁ RENDERIZADOS com o mesmo codec (ex.: gancho + corte
    principal) sem reencode — usa o demuxer concat do FFmpeg, que só copia os
    streams. Sem crossfade — ver `concatenar_com_fade` para isso."""
    lista = destino.with_suffix(".txt")
    lista.write_text("".join(f"file '{c.resolve().as_posix()}'\n" for c in clipes), encoding="utf-8")
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", str(lista), "-c", "copy", str(destino)], check=True, capture_output=True)
    finally:
        lista.unlink(missing_ok=True)
