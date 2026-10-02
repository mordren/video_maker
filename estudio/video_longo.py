"""Produção do vídeo longo (15-20 min) que a Fase 1 apontou (fase1/trechos_longos.py).

Sem crop, sem legenda, sem GC: o vídeo fica em 16:9, dentro da moldura do
canal (moldura.py). O que muda em relação à live:

- saem os pedaços que o DeepSeek marcou em "remover" (superchat fora do
  tema, problema técnico, recado do canal);
- sai o silêncio longo: pausa de mais de 4 s vira 0,5 s. Não é o
  compacto dos shorts (que tira quase toda respiração) — num vídeo longo a
  fala precisa respirar, só o "buraco" incomoda;
- o áudio é nivelado a -14 LUFS (e conferido pelo audio_qa, como nos shorts).

Tudo num passo só do ffmpeg: select/aselect com os pedaços que ficam, a
moldura por cima e o h264 na placa (NVENC).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import audio_qa
import moldura
from silencio import detectar_silencios

log = logging.getLogger("estudio")

SILENCIO_DB = -30          # mesmo limiar da Fase 2 (config_cortes.yaml)
SILENCIO_LONGO = 4.0       # pausa acima disso é cortada...
SILENCIO_SOBRA = 0.5       # ...e vira isso (metade de cada lado)
PEDACO_MINIMO = 0.4        # sobra menor que isso entre dois cortes some junto


def _duracao(arquivo: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(arquivo)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def pedacos_que_ficam(duracao: float, remover: list[tuple[float, float]],
                      silencios: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """[0, duracao] menos as remoções e o miolo dos silêncios longos."""
    fora = [(a, b) for a, b in remover]
    meio = SILENCIO_SOBRA / 2
    fora += [(a + meio, b - meio) for a, b in silencios if b - a > SILENCIO_LONGO]
    fora = sorted((max(0.0, a), min(duracao, b)) for a, b in fora if b > a)
    ficam, t = [], 0.0
    for a, b in fora:
        if a > t:
            ficam.append((t, a))
        t = max(t, b)
    if t < duracao:
        ficam.append((t, duracao))
    return [(a, b) for a, b in ficam if b - a >= PEDACO_MINIMO]


def _codificar(cmd_base: list[str], saida: Path) -> None:
    # cq 21 deu ~4,5 Mbps numa live de estúdio (18 min = 595 MB); o YouTube
    # recomenda ~8 Mbps em 1080p30. O teto só segura cena de muito movimento.
    teto = ["-maxrate", "12M", "-bufsize", "24M"]
    nvenc = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0", *teto]
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *teto]
    resto = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-movflags", "+faststart", str(saida)]
    try:
        subprocess.run(cmd_base + nvenc + resto, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        log.warning("   NVENC falhou (%s); codificando na CPU", (exc.stderr or "").strip()[-200:])
        r = subprocess.run(cmd_base + x264 + resto, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"ffmpeg falhou: {(r.stderr or '').strip()[-400:]}")


def produzir(fonte: Path, inicio: float, fim: float, remover: list[dict], perfil_nome: str,
             pasta: Path, titulo: str) -> dict:
    """`fonte` é o vídeo da live inteira (ou o recorte bruto, com `inicio`=0);
    `inicio`/`fim` e as remoções estão no tempo dela. Grava <pasta>/final.mp4
    e devolve os metadados para a tela de revisão."""
    pasta.mkdir(parents=True, exist_ok=True)
    duracao = fim - inicio
    rel = [(max(0.0, r["inicio"] - inicio), min(duracao, r["fim"] - inicio)) for r in remover]

    wav = pasta / "audio.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{inicio:.3f}", "-t", f"{duracao:.3f}",
                    "-i", str(fonte), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
    try:
        silencios = detectar_silencios(wav, SILENCIO_DB, SILENCIO_LONGO, duracao)
    finally:
        wav.unlink(missing_ok=True)
    longos = [(a, b) for a, b in silencios if b - a > SILENCIO_LONGO]
    ficam = pedacos_que_ficam(duracao, rel, silencios)
    if not ficam:
        raise RuntimeError("nada sobrou do trecho depois dos cortes")
    esperado = sum(b - a for a, b in ficam)
    log.info("   %d pedaço(s): %.1f min → %.1f min (%d remoção(ões), %d silêncio(s) > %.0f s, −%.0f s)",
             len(ficam), duracao / 60, esperado / 60, len(rel), len(longos), SILENCIO_LONGO,
             sum(b - a - SILENCIO_SOBRA for a, b in longos))

    png = moldura.gerar(perfil_nome, pasta / "moldura.png")
    escolha = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ficam)
    filtro = (f"[0:v]select='{escolha}',setpts=N/FRAME_RATE/TB[_sel];"
              + moldura.filtro("_sel", "1:v", "v") + ";"
              f"[0:a]aselect='{escolha}',asetpts=N/SR/TB,loudnorm=I=-14:TP=-2:LRA=11[a]")
    final = pasta / "final.mp4"
    _codificar(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{inicio:.3f}", "-t", f"{duracao:.3f}",
                "-i", str(fonte), "-loop", "1", "-i", str(png), "-filter_complex", filtro,
                "-map", "[v]", "-map", "[a]", "-shortest"], final)

    dur_final = _duracao(final)
    capa = pasta / "capa.jpg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(60.0, dur_final / 4):.1f}",
                    "-i", str(final), "-frames:v", "1", "-q:v", "3", str(capa)], check=False)
    audio = audio_qa.qa(final)
    if audio:
        log.info("   %s", audio_qa.linha_log(audio))
    return {
        "arquivo": final.name,
        "capa": capa.name if capa.exists() else "",
        "titulo": titulo,
        "subtitulo": "",
        "duracao": round(dur_final, 1),
        "silencios_cortados": len(longos),
        "remocoes": len(rel),
        "audio": audio,
    }
