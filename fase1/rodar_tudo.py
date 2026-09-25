"""Roda a Parte A (seleção) e, se sair algum bloco, a Parte B (corte) em seguida.

    python rodar_tudo.py <video.mp4> [--workspace pasta]

Um comando só: escolhe os melhores trechos do vídeo e, terminando isso com
sucesso, já corta cada um (silêncio, recomeço, loudness, abertura). Se a Parte
A não gerar nenhum bloco (vídeo sem trecho forte o bastante), para aqui — não
tem sentido chamar a Parte B sem nada para cortar.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

AQUI = Path(__file__).resolve().parent


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="Roda a Parte A e a Parte B em sequência.")
    ap.add_argument("video", type=Path)
    ap.add_argument("--workspace", type=Path, default=None,
                    help="pasta da Parte A (padrão: fase1/workspace_<data_hora>)")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        print(f"Vídeo não encontrado: {video}", file=sys.stderr)
        return 1
    ws = (args.workspace or AQUI / f"workspace_{datetime.now():%Y%m%d_%H%M%S}").resolve()

    print(f"=== Parte A: escolhendo os melhores cortes de \"{video.name}\" ===")
    r1 = subprocess.run([sys.executable, str(AQUI / "pipeline.py"), str(video),
                         "--workspace", str(ws)])
    if r1.returncode != 0:
        print("\nParte A terminou com erro; não vou tentar cortar. Veja o log acima.",
              file=sys.stderr)
        return r1.returncode

    blocos_path = ws / "blocos_finais.json"
    if not blocos_path.exists():
        print(f"\n{blocos_path} não foi gerado; abortando.", file=sys.stderr)
        return 1
    dados = json.loads(blocos_path.read_text(encoding="utf-8"))
    n = len(dados.get("blocos") or [])
    if n == 0:
        print("\nA Parte A não achou nenhum trecho forte o bastante neste vídeo. Nada a cortar.")
        return 0

    print(f"\n=== Parte B: cortando {n} bloco(s) ===")
    r2 = subprocess.run([sys.executable, str(AQUI / "pipeline_cortes.py"), str(blocos_path)])
    if r2.returncode == 0:
        print(f"\nPronto. Clipes em: {ws / 'cortes'}")
    return r2.returncode


if __name__ == "__main__":
    sys.exit(main())
