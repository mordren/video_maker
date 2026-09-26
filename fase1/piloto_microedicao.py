"""Piloto da micro-edição: um clipe da Fase 2 vira dois vídeos finais para comparar.

    python piloto_microedicao.py <clipe.mp4> <relatorio.json> <pasta_saida> [opções]

Saída: <pasta>/sem/final.mp4 (acabamento de hoje) e <pasta>/com/final.mp4 (micro-edição
+ o mesmo acabamento), e <pasta>/com/micro_plano/plano.json com o que foi feito e por quê.
Os dois usam a mesma trilha, título e manchete (a IA do acabamento roda uma vez só),
para que a única diferença entre eles seja a micro-edição.

Tudo que é caro fica em cache na pasta (transcrição, análise da voz, plano da IA,
imagens, revisão do acabamento, a versão "sem"). Rodar de novo refaz só o vídeo:
    --replanejar   pede outro plano à IA (o resto continua do cache)
    --so-render    usa com/micro_plano/plano.json como está (edite à mão e rode)
    --previa       só a micro-edição em 540x960 (com/previa.mp4), sem acabamento — segundos
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
for p in (AQUI, AQUI.parent, AQUI.parent / "estudio"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import ai_srt  # noqa: E402
import finalizar  # noqa: E402
import microedicao  # noqa: E402
import trilhas  # noqa: E402
from utils import shorten_srt_captions  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clipe", type=Path)
    ap.add_argument("relatorio", type=Path)
    ap.add_argument("saida", type=Path)
    ap.add_argument("--perfil", default="info_nacional")
    ap.add_argument("--replanejar", action="store_true")
    ap.add_argument("--so-render", action="store_true")
    ap.add_argument("--previa", action="store_true")
    ap.add_argument("--sem-zoom", action="store_true", help="só as imagens")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    log = logging.getLogger("piloto")
    inicio = time.time()

    sem, com = args.saida / "sem", args.saida / "com"
    for d in (sem, com):
        d.mkdir(parents=True, exist_ok=True)

    cache_palavras = args.saida / "palavras.json"
    if cache_palavras.exists():
        palavras = json.loads(cache_palavras.read_text(encoding="utf-8"))
    else:
        log.info("transcrevendo o clipe")
        _srt, palavras = finalizar.transcrever(args.clipe, sem)
        shutil.copy(sem / "legenda.srt", args.saida / "legenda_bruta.srt")
        cache_palavras.write_text(json.dumps(palavras, ensure_ascii=False), encoding="utf-8")

    log.info("micro-edição")
    micro = com / ("previa.mp4" if args.previa else "micro.mp4")
    plano = microedicao.processar(args.clipe, json.loads(args.relatorio.read_text(encoding="utf-8")),
                                  palavras, micro, com / "micro_plano", replanejar=args.replanejar,
                                  previa=args.previa, so_render=args.so_render, zoom=not args.sem_zoom)
    for e in plano["eventos"]:
        log.info("   %6.2fs %-7s %-11s ag=%.2f %s", e["t"], e["tipo"], e.get("emocao", ""),
                 e.get("agitacao", 0), e.get("rotulo") or e.get("palavra") or e.get("motivo", ""))
    if args.previa:
        log.info("prévia pronta em %.0fs: %s", time.time() - inicio, micro)
        return 0

    ia_cache = args.saida / "acabamento_ia.json"
    perfil = finalizar.carregar_perfil(args.perfil)
    cfg = ai_srt.load_config()
    pasta_trilhas = Path(cfg["music_dir"]) if cfg.get("music_dir") else None
    if ia_cache.exists():
        ia = json.loads(ia_cache.read_text(encoding="utf-8"))
    else:
        log.info("revisão do acabamento (IA, uma vez para os dois)")
        srt = args.saida / "legenda.srt"
        shutil.copy(args.saida / "legenda_bruta.srt", srt)
        ia = finalizar.revisar_legenda(srt, "", pasta_trilhas)
        shorten_srt_captions(srt)
        ia_cache.write_text(json.dumps(ia, ensure_ascii=False), encoding="utf-8")
    trilha = trilhas.find_track(ia["musica"], pasta_trilhas) if (ia["musica"] and pasta_trilhas) else None
    censura = finalizar.palavras_censuradas(ia["sensiveis"])

    versoes = [(com, micro)] + ([] if (sem / "final.mp4").exists() else [(sem, args.clipe)])
    for pasta, video in versoes:
        # a censura reescreve o .srt no lugar; cada versão usa a sua cópia
        shutil.copy(args.saida / "legenda.srt", pasta / "legenda.srt")
        finalizar.renderizar_final(video, pasta / "final.mp4", pasta / "legenda.srt", ia["titulo"],
                                   ia["subtitulo"], perfil, censura, trilha, pasta)
    log.info("pronto em %.0fs: %s | %s | trilha %s", time.time() - inicio, ia["titulo"],
             ia["subtitulo"], trilha.name if trilha else "-")
    return 0


if __name__ == "__main__":
    sys.exit(main())
