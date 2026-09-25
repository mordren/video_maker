"""Crop dinâmico 9:16 — última etapa, roda nos clipes prontos da Fase 2.

    python pipeline_crop.py <video.mp4>            # um clipe
    python pipeline_crop.py <pasta/com/cortes>      # todos os .mp4 da pasta

Depende de mediapipe e opencv-python (não estão no requirements.txt
principal porque só esta etapa usa — instale à parte: veja README).
Não usa nenhuma API/IA de nuvem; roda tudo local (CPU ou GPU, se o mediapipe
achar CUDA disponível).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

import crop_dinamico

AQUI = Path(__file__).resolve().parent
log = logging.getLogger("fase1")


@contextmanager
def etapa(nome: str):
    t0 = time.perf_counter()
    yield
    log.info("   %s: %.1fs", nome, time.perf_counter() - t0)


def processa_video(video: Path, saida: Path, cfg: dict) -> None:
    destino = saida / f"{video.stem}_vertical.mp4"
    cd, cs, csa = cfg["deteccao"], cfg["suavizacao"], cfg["saida"]
    with etapa(f"{video.name}"):
        crop_dinamico.processar(video, destino, passo_seg=cd["passo_seg"],
                                janela_suavizacao_seg=cs["janela_seg"],
                                confianca_minima=cd["confianca_minima"],
                                largura_saida=csa["largura"], altura_saida=csa["altura"])
    log.info("   -> %s", destino)


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
    videos = sorted(entrada.glob("*.mp4")) if entrada.is_dir() else [entrada]
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
            log.error("   %s falhou: %s", video.name, exc)
    log.info("Pronto: %d/%d processados -> %s", ok, len(videos), saida)
    return 0


if __name__ == "__main__":
    sys.exit(main())
