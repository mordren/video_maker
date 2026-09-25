"""Fase 2 — depuração mecânica de cada bloco selecionado por `pipeline.py` (Fase 1).

    python pipeline_cortes.py <blocos_finais.json> [--saida pasta] [--config config_cortes.yaml]

Para cada bloco:
1. Recorta vídeo+áudio do vídeo original (ffmpeg -ss -t) — um clipe bruto, ainda
   sem cortes internos. Os passos seguintes trabalham só nesse recorte, então
   todo timestamp daqui em diante é relativo ao INÍCIO DO BLOCO, não do vídeo.
2. Roda Whisper (--word_timestamps) nesse clipe — granularidade de palavra, que
   o SRT não tem e os cortes de recomeço/gancho precisam.
3. Detecta silêncios (ffmpeg silencedetect); pausas longas são encolhidas, não
   removidas inteiras; apara o excesso de silêncio nas bordas do clipe.
4. Detecta recomeço de frase (repetição de prefixo de palavras) — conservador,
   só corta repetição literal com pausa curta entre as duas tentativas.
5. Mescla os dois planos de corte, calcula os trechos a manter, renderiza o
   corte principal (trim+concat, loudnorm, fade curto em cada junção interna).
6. Se `abertura.ativo`: gera candidatos de ~2-3s do bloco (fora dos trechos já
   cortados), o JEV escolhe o mais forte sozinho, e esse trecho é renderizado
   à parte e colado na frente do corte principal — a técnica de mostrar o
   pico primeiro para prender atenção nos primeiros segundos.
7. Salva um relatório por bloco: duração antes/depois e cada corte com motivo.

A abertura é a única parte desta fase que usa IA (JEV) — o resto é tudo regra
determinística sobre sinal de áudio/texto.
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import yaml

import gancho
import recomeco
import renderiza
import silencio
from jev_client import JEV
from openrouter import APIError, api_key
from transcricao import rodar_whisper  # reaproveita o wrapper do Whisper da Fase 1

AQUI = Path(__file__).resolve().parent
log = logging.getLogger("fase2")


@contextmanager
def etapa(nome: str):
    t0 = time.perf_counter()
    yield
    log.info("   %s: %.1fs", nome, time.perf_counter() - t0)


def gravar_json(path: Path, dados) -> None:
    texto = json.dumps(dados, ensure_ascii=False, indent=2)
    # O projeto roda dentro do OneDrive: ele pode segurar um handle no arquivo
    # por sincronização, e a escrita falha com "Access is denied" numa janela
    # curta — não é erro de permissão real. Tenta de novo com espera.
    for tentativa in range(6):
        try:
            path.write_text(texto, encoding="utf-8")
            return
        except PermissionError:
            if tentativa == 5:
                raise
            time.sleep(0.2 * (tentativa + 1))


def recorta_video(video: Path, inicio: float, duracao: float, destino: Path) -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("ffmpeg não encontrado no PATH.")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(inicio), "-i", str(video),
                    "-t", str(duracao), "-c", "copy", "-avoid_negative_ts", "make_zero", str(destino)],
                   check=True)


def duracao_de(arquivo: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(arquivo)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def processa_bloco(bloco: dict, video: Path, pasta: Path, cfg: dict, jev: JEV | None) -> dict:
    bid = bloco["id"]
    pbloco = pasta / bid
    pbloco.mkdir(parents=True, exist_ok=True)
    clipe_bruto = pbloco / "bruto.mp4"

    with etapa("recorte do vídeo original"):
        recorta_video(video, bloco["inicio"], bloco["duracao"] + 0.5, clipe_bruto)
    duracao_bruta = duracao_de(clipe_bruto)

    with etapa("transcrição por palavra (Whisper)"):
        segs = rodar_whisper(clipe_bruto, pbloco, cfg["whisper"]["modelo"], cfg["whisper"]["idioma"])
    palavras = [w for s in segs for w in s["words"]]
    if not palavras:
        log.warning("   %s: Whisper não achou palavras; bloco copiado sem cortes internos", bid)
        destino = pasta / f"{bid}.mp4"
        shutil.copy(clipe_bruto, destino)
        clipe_bruto.unlink(missing_ok=True)
        return {"id": bid, "duracao_antes": duracao_bruta, "duracao_depois": duracao_bruta,
               "cortes": [], "aviso": "sem transcrição por palavra"}

    cs = cfg["silencio"]
    with etapa("detecção de silêncio"):
        silencios = silencio.detectar_silencios(clipe_bruto, cs["noise_db"], cs["duracao_minima"],
                                                 duracao_bruta)
    inicio_ok, fim_ok = silencio.apara_bordas(silencios, duracao_bruta, cfg["bordas"]["max_silencio_borda"])
    cortes_silencio = silencio.plano_de_encolhimento(silencios, cs["limiar_corte"], cs["duracao_alvo"],
                                                      cs["margem_seguranca"])

    cortes_recomeco = []
    if cfg["recomeco"]["ativo"]:
        with etapa("detecção de recomeço de frase"):
            cr = cfg["recomeco"]
            cortes_recomeco = recomeco.detectar_recomecos(palavras, cr["min_palavras"],
                                                           cr["max_palavras"], cr["gap_maximo"],
                                                           cr["gap_minimo"])

    todos_cortes = renderiza.mescla_cortes(cortes_silencio + cortes_recomeco)
    manter = renderiza.trechos_a_manter(todos_cortes, inicio_ok, fim_ok)

    principal = pbloco / "principal.mp4"
    with etapa("renderização (corte + loudnorm)"):
        renderiza.renderizar(clipe_bruto, principal, manter, tem_video=True, cfg_loud=cfg["loudness"],
                             crossfade_ms=cfg["saida"]["crossfade_ms"])

    destino = pasta / f"{bid}.mp4"
    abertura_info = None
    ca = cfg["abertura"]
    if ca["ativo"] and jev is not None:
        with etapa("abertura (candidatos + JEV)"):
            candidatos = gancho.candidatos(palavras, todos_cortes, ca["duracao_minima"],
                                           ca["duracao_maxima"], ca["max_candidatos"])
            if candidatos:
                try:
                    escolhido = jev.escolher_gancho(candidatos, bloco.get("gancho", ""),
                                                    bloco.get("comentario", ""))
                    abertura = pbloco / "abertura.mp4"
                    renderiza.renderizar(clipe_bruto, abertura, [(escolhido["inicio"], escolhido["fim"])],
                                         tem_video=True, cfg_loud=cfg["loudness"], crossfade_ms=0,
                                         escala_de_cinza=ca["escala_de_cinza"])
                    renderiza.concatenar([abertura, principal], destino)
                    abertura_info = {"inicio": escolhido["inicio"], "fim": escolhido["fim"],
                                     "texto": escolhido["texto"]}
                except Exception as exc:  # noqa: BLE001 — sem abertura não é motivo de falhar o bloco
                    log.warning("   %s: abertura falhou (%s); seguindo sem ela", bid, exc)
            else:
                log.info("   %s: nenhum candidato de abertura dentro da faixa de duração", bid)
    if abertura_info is None:
        shutil.copy(principal, destino)
    duracao_final = duracao_de(destino)

    # Limpa os intermediários (bruto/principal/abertura) — só o .mp4 final, o
    # relatório e a transcrição (bruto.json, pequena e útil para auditoria)
    # ficam. Sem isso, cada bloco deixa 3-4 cópias de dezenas de MB para trás.
    for intermediario in (clipe_bruto, principal, pbloco / "abertura.mp4"):
        intermediario.unlink(missing_ok=True)

    relatorio = {
        "id": bid,
        "duracao_antes": round(duracao_bruta, 3),
        "duracao_depois": round(duracao_final, 3),
        "removido": round(duracao_bruta - duracao_final, 3),
        "apara_bordas": {"inicio": inicio_ok, "fim": fim_ok},
        "cortes": todos_cortes,
        "abertura": abertura_info,
    }
    gravar_json(pbloco / "relatorio.json", relatorio)
    log.info("   %s: %.1fs -> %.1fs (-%.1fs, %d cortes%s)", bid, duracao_bruta, duracao_final,
             duracao_bruta - duracao_final, len(todos_cortes),
             ", com abertura" if abertura_info else "")
    return relatorio


def main() -> int:
    ap = argparse.ArgumentParser(description="Fase 2: depuração mecânica dos blocos da Fase 1.")
    ap.add_argument("blocos_finais", type=Path, help="blocos_finais.json de uma execução da Fase 1")
    ap.add_argument("--saida", type=Path, default=None)
    ap.add_argument("--config", type=Path, default=AQUI / "config_cortes.yaml")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)

    dados = json.loads(args.blocos_finais.read_text(encoding="utf-8"))
    video = Path(dados["video"])
    if not video.exists():
        log.error("Vídeo original não encontrado: %s", video)
        return 1
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    saida = args.saida or (args.blocos_finais.parent / cfg["saida"]["pasta"])
    saida.mkdir(parents=True, exist_ok=True)

    jev = None
    if cfg["abertura"]["ativo"]:
        try:
            api_key()
        except APIError as exc:
            log.warning("%s — seguindo sem abertura (defina OPENROUTER_API_KEY para ativar,"
                       " ou abertura.ativo: false no config para tirar este aviso)", exc)
        else:
            jev = JEV(cfg["jev"])

    log.info("Vídeo: %s", video)
    log.info("%d blocos -> %s", len(dados["blocos"]), saida)
    relatorios = []
    for i, bloco in enumerate(dados["blocos"], 1):
        log.info("── Bloco %d/%d: %s (%s)", i, len(dados["blocos"]), bloco["id"], bloco.get("gancho", ""))
        try:
            relatorios.append(processa_bloco(bloco, video, saida, cfg, jev))
        except Exception as exc:  # noqa: BLE001 — um bloco falhar não derruba os outros
            log.error("   %s falhou: %s", bloco["id"], exc)
            relatorios.append({"id": bloco["id"], "erro": str(exc)})
    gravar_json(saida / "relatorio_geral.json", relatorios)
    ok = sum(1 for r in relatorios if "erro" not in r)
    log.info("Pronto: %d/%d blocos processados -> %s", ok, len(relatorios), saida)
    return 0


if __name__ == "__main__":
    sys.exit(main())
