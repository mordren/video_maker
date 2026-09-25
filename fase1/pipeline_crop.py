"""Crop dinâmico 9:16 — última etapa, roda nos clipes prontos da Fase 2.

    python pipeline_crop.py <video.mp4>            # um clipe
    python pipeline_crop.py <pasta/com/cortes>      # todos os .mp4 da pasta

Depende de mediapipe, opencv-python e resemblyzer (não estão no
requirements.txt principal porque só esta etapa usa — ver requirements_crop.txt).
Não usa nenhuma API/IA de nuvem; roda tudo local (GPU se houver CUDA).
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

import crop_dinamico
import falantes
from transcricao import rodar_whisper

AQUI = Path(__file__).resolve().parent
log = logging.getLogger("fase1")


@contextmanager
def etapa(nome: str):
    t0 = time.perf_counter()
    yield
    log.info("   %s: %.1fs", nome, time.perf_counter() - t0)


def turnos_de_fala(video: Path, cfg: dict) -> list[dict]:
    cf = cfg["falantes"]
    if not cf["ativo"]:
        return []
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "audio.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-ac", "1",
                        "-ar", "16000", str(wav)], check=True)
        with etapa("whisper"):
            segs = rodar_whisper(wav, Path(tmp), cf["whisper_modelo"], cf["idioma"])
        palavras = [w for s in segs for w in s.get("words", [])]
        with etapa("falantes"):
            turnos = falantes.detectar(wav, palavras, n_falantes=cf["n_falantes"], limiar=cf["limiar"])
    for t in turnos:
        log.info("     %6.1f-%6.1fs  falante %d: %s", t["inicio"], t["fim"], t["falante"], t["texto"][:70])
    return turnos


def processa_video(video: Path, saida: Path, cfg: dict) -> None:
    destino = saida / f"{video.stem}_vertical.mp4"
    with etapa(video.name):
        turnos = turnos_de_fala(video, cfg)
        info = crop_dinamico.processar(video, destino, turnos, cfg)
    (saida / f"{video.stem}_falantes.json").write_text(
        json.dumps(turnos, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("   -> %s (%d planos, %d keyframes)", destino, info["planos"], info["keyframes"])


def main() -> int:
    ap = argparse.ArgumentParser(description="Crop dinâmico 9:16 dos clipes da Fase 2.")
    ap.add_argument("entrada", type=Path, help="um .mp4, ou uma pasta com vários")
    ap.add_argument("--saida", type=Path, default=None)
    ap.add_argument("--config", type=Path, default=AQUI / "config_crop.yaml")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)

    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    entrada = args.entrada.resolve()
    if not entrada.exists():
        log.error("Não encontrado: %s", entrada)
        return 1
    videos = sorted(p for p in entrada.glob("*.mp4") if not p.stem.endswith("_vertical")) \
        if entrada.is_dir() else [entrada]
    if not videos:
        log.error("Nenhum .mp4 em %s", entrada)
        return 1
    saida = (args.saida or (entrada if entrada.is_dir() else entrada.parent) / cfg["saida"]["pasta"])
    saida.mkdir(parents=True, exist_ok=True)

    log.info("%d vídeo(s) -> %s", len(videos), saida)
    ok = 0
    for video in videos:
        try:
            processa_video(video, saida, cfg)
            ok += 1
        except Exception as exc:  # noqa: BLE001 — um vídeo falhar não derruba os outros
            log.exception("   %s falhou: %s", video.name, exc)
    log.info("Pronto: %d/%d processados -> %s", ok, len(videos), saida)
    return 0


if __name__ == "__main__":
    sys.exit(main())
