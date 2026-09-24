"""Fase 1 — seleção e qualificação de blocos de um vídeo longo.

    python pipeline.py <video.mp4> [--workspace pasta] [--ate-etapa N]

Etapas: 1 áudio · 2 transcrição (SRT ou Whisper) · 3 segmentação (DeepSeek aponta
os blocos candidatos, por conteúdo) · 4 JEV qualifica (viral + ritmo + ajuste
fino de limites, numa chamada) · 5 consolidação · 6 LLM revisão fina ·
7 ranqueamento · 8 blocos_finais.json.

A segmentação é por conteúdo, não por tempo fixo: o DeepSeek lê a transcrição
e aponta onde cada bloco candidato começa e termina (etapa 3). O JEV, mais
rápido e barato, qualifica cada candidato (etapa 4). O LLM contextual entra
só no final, para os poucos blocos que sobraram da consolidação (etapa 6).

Cada etapa grava o seu JSON na pasta de trabalho. Rodando de novo com
`--workspace` numa pasta existente, o que já foi feito é reaproveitado e só os
itens que faltaram ou deram erro voltam para a API.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import yaml

import deepseek_client
import segmenter as sg
from jev_client import JEV
from llm_client import LLM
from openrouter import APIError as OpenRouterError
from openrouter import api_key as openrouter_key
from segmentador_llm import Segmentador, segmentar
from transcricao import extrair_audio, obter_transcricao

AQUI = Path(__file__).resolve().parent
log = logging.getLogger("fase1")


# ---------------------------------------------------------------------------
# Infra: JSON, logs, cronômetro, execução em paralelo com retomada
# ---------------------------------------------------------------------------

def ler_json(path: Path, padrao=None):
    if not path.exists():
        return padrao
    return json.loads(path.read_text(encoding="utf-8"))


def gravar_json(path: Path, dados) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


@contextmanager
def etapa(numero: int, nome: str):
    log.info("── Etapa %d: %s", numero, nome)
    t0 = time.perf_counter()
    yield
    log.info("   etapa %d levou %.1fs", numero, time.perf_counter() - t0)


def _chave(item: dict) -> str:
    return f"{item['inicio']:.3f}-{item['fim']:.3f}"


def em_paralelo(arquivo: Path, itens: list[dict], fn, paralelo: int) -> dict:
    """Roda `fn(item)` para cada item, guardando {id: resultado} em `arquivo`.

    Resultado já gravado para o mesmo id e os mesmos limites é reaproveitado.
    Falha de um item vira {"erro": ...} e não interrompe os outros.
    """
    cache: dict = ler_json(arquivo, {})
    pendentes = [it for it in itens
                 if "erro" in cache.get(it["id"], {"erro": 1}) or cache[it["id"]].get("chave") != _chave(it)]
    if len(itens) - len(pendentes):
        log.info("   %d reaproveitados da execução anterior", len(itens) - len(pendentes))
    feitos = 0
    with ThreadPoolExecutor(max_workers=max(1, paralelo)) as ex:
        futuros = {ex.submit(fn, it): it for it in pendentes}
        for fut in as_completed(futuros):
            it = futuros[fut]
            try:
                res = fut.result()
            except Exception as exc:  # noqa: BLE001 — qualquer falha fica registrada no item
                log.error("   %s: %s", it["id"], exc)
                res = {"erro": str(exc)}
            res["chave"] = _chave(it)
            cache[it["id"]] = res
            feitos += 1
            if feitos % 10 == 0:
                gravar_json(arquivo, cache)
                log.info("   %d/%d", feitos, len(pendentes))
    gravar_json(arquivo, cache)
    erros = sum(1 for it in itens if "erro" in cache[it["id"]])
    if erros:
        log.warning("   %d de %d itens falharam (rode de novo com --workspace para repetir só esses)",
                    erros, len(itens))
    return cache


def ajusta_limites(bloco: dict, segs: list[dict], inicio: float, fim: float,
                   etapa_nome: str, motivo: str, minimo: float) -> bool:
    """Aplica novos limites ao bloco, recalculando texto e palavras pela transcrição."""
    novo = sg.monta_bloco(segs, inicio, fim)
    if not novo or novo["duracao"] < minimo:
        log.info("   %s: ajuste %s ignorado (bloco ficaria com %.1fs)", bloco["id"], etapa_nome,
                 novo["duracao"] if novo else 0)
        return False
    if (novo["inicio"], novo["fim"]) == (bloco["inicio"], bloco["fim"]):
        return False
    bloco.setdefault("ajustes", []).append({
        "etapa": etapa_nome, "de": [bloco["inicio"], bloco["fim"]],
        "para": [novo["inicio"], novo["fim"]], "motivo": motivo})
    bloco.update(novo)
    return True


def deduplica(blocos: list[dict], limite: float) -> list[dict]:
    """Entre blocos que se sobrepõem mais que `limite`, fica o de maior score."""
    ordem = sorted(blocos, key=lambda b: (b["score_viral"], b["qualidade_interna"]), reverse=True)
    mantidos: list[dict] = []
    for b in ordem:
        if all(sg.sobreposicao(b, m) <= limite for m in mantidos):
            mantidos.append(b)
    return sorted(mantidos, key=lambda b: b["inicio"])


def nota_llm(analise: dict | None) -> float:
    """coerente + coeso + sem problemas, de 0 a 1. Sem análise (falha da API) vale 0.5."""
    if not analise:
        return 0.5
    return (analise["coerente"] + analise["coeso"] + (not analise["problemas"])) / 3


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def rodar(video: Path, ws: Path, cfg: dict, ate_etapa: int) -> int:
    cseg, cj, cl, cb = cfg["segmentacao"], cfg["jev"], cfg["llm"], cfg["bloco"]
    minimo = cb["duracao_minima"]

    # 1 ─ áudio
    with etapa(1, "extrair áudio"):
        audio = ws / "audio.wav"
        if audio.exists():
            log.info("   audio.wav já existe")
        else:
            try:
                extrair_audio(video, audio)
                log.info("   audio.wav gerado")
            except Exception as exc:  # noqa: BLE001 — só é indispensável se não houver SRT
                log.warning("   falha ao extrair áudio (%s); segue, mas o Whisper não poderá rodar", exc)
                audio = None

    # 2 ─ transcrição
    with etapa(2, "transcrição"):
        tpath = ws / "transcricao.json"
        if tpath.exists():
            dados = ler_json(tpath)
            segs, fonte = dados["segmentos"], dados["fonte"]
            log.info("   transcricao.json reaproveitada")
        else:
            segs, fonte = obter_transcricao(video, audio, ws, cfg["transcricao"])
            gravar_json(tpath, {"fonte": fonte, "segmentos": segs})
            (ws / "fonte_transcricao.txt").write_text(fonte + "\n", encoding="utf-8")
        tipo_fonte = "whisper" if fonte == "whisper" else "srt"
        log.info("   fonte da transcrição: %s · %d segmentos · granularidade por %s",
                 fonte, len(segs), "palavra" if tipo_fonte == "whisper" else "segmento")
    if ate_etapa <= 2:
        return 0

    try:
        deepseek_client.api_key()
    except deepseek_client.APIError as exc:
        log.error("%s", exc)
        return 2

    # 3 ─ segmentação semântica (DeepSeek aponta os blocos candidatos)
    with etapa(3, "segmentação (DeepSeek aponta os candidatos)"):
        cpath = ws / "candidatos.json"
        if cpath.exists():
            candidatos = ler_json(cpath)
            log.info("   candidatos.json reaproveitada")
        else:
            segmentador = Segmentador(deepseek_client, cseg)
            candidatos = segmentar(segs, segmentador, cseg["duracao_chunk_min"], cseg["contexto_seg"])
            gravar_json(cpath, candidatos)
        blocos: list[dict] = []
        descartados = 0
        for i, c in enumerate(candidatos, 1):
            bloco = sg.monta_bloco(segs, c["inicio"], c["fim"])
            if not bloco or not (cb["duracao_minima"] <= bloco["duracao"] <= cb["duracao_maxima"]):
                descartados += 1
                continue
            bloco["id"] = f"cand_{i:03d}"
            bloco["gancho"] = c.get("gancho", "")
            bloco["pedaco_origem"] = c.get("pedaco")
            blocos.append(bloco)
        gravar_json(ws / "blocos_candidatos.json", blocos)
        log.info("   %d candidatos brutos · %d fora da faixa de duração (%.0f-%.0fs) · %d seguem",
                 len(candidatos), descartados, cb["duracao_minima"], cb["duracao_maxima"], len(blocos))
    if ate_etapa <= 3:
        return 0
    if not blocos:
        log.warning("Nenhum candidato sobrou da segmentação; nada a qualificar.")
        gravar_json(ws / "blocos_finais.json", {"video": str(video), "blocos": []})
        return 0

    try:
        openrouter_key()
    except OpenRouterError as exc:
        log.error("%s", exc)
        log.error("Depois de preencher, continue com: python pipeline.py \"%s\" --workspace \"%s\"", video, ws)
        return 2
    jev = JEV(cj)

    # 4 ─ JEV: qualifica cada candidato (viral + ritmo + ajuste fino, numa chamada)
    with etapa(4, "JEV — qualifica os candidatos"):
        def pergunta4(b):
            return jev.qualificar(sg.trecho(segs, b["inicio"], b["fim"]), cj["opcoes_limite"])

        r4 = em_paralelo(ws / "jev_qualificacao.json", blocos, pergunta4, cj["paralelo"])
        pontuados, ajustados = [], 0
        for b in blocos:
            r = r4[b["id"]]
            if "erro" in r:
                b.setdefault("erros", []).append(f"jev: {r['erro']}")
                continue
            b["score_viral"] = round(r["score_viral"], 2)
            b["qualidade_interna"] = round(r["ritmo"], 4)
            b["precisa_melhora"], b["motivo_melhora"] = False, None
            dentro = sg.trecho(segs, b["inicio"], b["fim"])
            ini, fim = r["inicio_idx"], r["fim_idx"]
            corta_ini, corta_fim = ini > 0, fim < len(dentro) - 1
            if r["precisa_melhora"] >= cj["limiar_precisa_melhora"] and (corta_ini or corta_fim) and fim > ini:
                motivo = {(True, False): "cortar o começo", (False, True): "cortar o fim",
                          (True, True): "cortar o começo e o fim"}[(corta_ini, corta_fim)]
                b["precisa_melhora"] = True
                b["motivo_melhora"] = f"{motivo} (JEV {r['precisa_melhora']:.2f})"
                ajustados += ajusta_limites(b, segs, dentro[ini]["start"], dentro[fim]["end"],
                                            "jev", motivo, minimo)
            pontuados.append(b)
        log.info("   %d candidatos · %d qualificados · %d com ajuste fino de limites",
                 len(blocos), len(pontuados), ajustados)

    # 5 ─ consolidação
    with etapa(5, "consolidar blocos (dedup entre candidatos sobrepostos)"):
        blocos = deduplica(pontuados, cfg["consolidacao"]["sobreposicao_maxima"])
        for b in blocos:
            b["origem"] = b.pop("id")
            b["id"] = b["origem"].replace("cand_", "bloco_")
        gravar_json(ws / "blocos_jev.json", blocos)
        log.info("   entraram %d · ficaram %d", len(pontuados), len(blocos))
    if ate_etapa <= 5:
        return 0

    # 6 ─ LLM contextual (revisão fina: coerência, coesão, problemas, sugestão de corte)
    with etapa(6, f"LLM revisão fina ({cl['modelo']})"):
        llm = LLM(cl)

        def pergunta6(b):
            antes, depois = sg.contexto(segs, b["inicio"], b["fim"], cl["contexto_segundos"])
            return {"analise": llm.analisar(antes, sg.trecho(segs, b["inicio"], b["fim"]), depois)}

        r6 = em_paralelo(ws / "llm_revisao.json", blocos, pergunta6, cl["paralelo"])
        ajustados = 0
        for b in blocos:
            r = r6[b["id"]]
            b["analise_llm"] = r.get("analise")
            if "erro" in r:
                b.setdefault("erros", []).append(f"llm: {r['erro']}")
                continue
            sug = b["analise_llm"]["sugestao_corte"]
            if not sug:
                continue
            # O LLM só pode mexer dentro do que viu: o bloco mais o contexto.
            antes, depois = sg.contexto(segs, b["inicio"], b["fim"], cl["contexto_segundos"])
            piso = antes[0]["start"] if antes else b["inicio"]
            teto = depois[-1]["end"] if depois else b["fim"]
            ini, fim = max(piso, sug["inicio"]), min(teto, sug["fim"])
            if fim > ini:
                ajustados += ajusta_limites(b, segs, ini, fim, "llm", sug["motivo"], minimo)
        blocos = deduplica(blocos, cfg["consolidacao"]["sobreposicao_maxima"])
        gravar_json(ws / "blocos_llm.json", blocos)
        log.info("   %d analisados · %d com limites ajustados · %d após nova deduplicação",
                 len(r6), ajustados, len(blocos))

    # 7 ─ ranqueamento
    with etapa(7, "ranqueamento final"):
        pesos = cfg["ranking"]
        for b in blocos:
            b["nota_llm"] = round(nota_llm(b.get("analise_llm")), 4)
            b["nota_final"] = round(pesos["peso_viral"] * (b["score_viral"] - 1) / 4
                                    + pesos["peso_ritmo"] * b["qualidade_interna"]
                                    + pesos["peso_llm"] * b["nota_llm"], 4)
        blocos.sort(key=lambda b: b["nota_final"], reverse=True)
        for i, b in enumerate(blocos, 1):
            b["rank"] = i

    # 8 ─ JSON final
    with etapa(8, "salvar blocos_finais.json"):
        campos = ["id", "rank", "inicio", "fim", "duracao", "texto", "palavras", "fonte_transcricao",
                  "gancho", "score_viral", "qualidade_interna", "precisa_melhora", "motivo_melhora",
                  "analise_llm", "nota_llm", "nota_final", "origem", "pedaco_origem", "ajustes", "erros"]
        finais = []
        for b in blocos:
            b["fonte_transcricao"] = tipo_fonte
            finais.append({c: b[c] for c in campos if c in b})
        gravar_json(ws / "blocos_finais.json", {
            "video": str(video),
            "fonte_transcricao": fonte,
            "granularidade": "palavra" if tipo_fonte == "whisper" else "segmento",
            "gerado_em": datetime.now().isoformat(timespec="seconds"),
            "config": cfg,
            "blocos": finais,
        })
        log.info("   %d blocos → %s", len(finais), ws / "blocos_finais.json")
        for b in finais[:5]:
            log.info("   #%d %s  %s-%s  viral %.2f  nota %.3f  %s", b["rank"], b["id"],
                     _hms(b["inicio"]), _hms(b["fim"]), b["score_viral"], b["nota_final"], b["texto"][:70])
    return 0


def _hms(s: float) -> str:
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def main() -> int:
    ap = argparse.ArgumentParser(description="Fase 1: seleção de blocos de um vídeo longo.")
    ap.add_argument("video", type=Path)
    ap.add_argument("--workspace", type=Path, help="pasta de trabalho existente, para retomar")
    ap.add_argument("--config", type=Path, default=AQUI / "config.yaml")
    ap.add_argument("--ate-etapa", type=int, default=10,
                    help="para depois desta etapa (2 = só transcrição, sem gastar API)")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        print(f"Vídeo não encontrado: {video}", file=sys.stderr)
        return 1
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    ws = (args.workspace or Path.cwd() / f"workspace_{datetime.now():%Y%m%d_%H%M%S}").resolve()
    ws.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")
    log.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(ws / "pipeline.log", encoding="utf-8")):
        h.setFormatter(fmt)
        log.addHandler(h)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    log.info("Vídeo: %s", video)
    log.info("Pasta de trabalho: %s", ws)
    try:
        return rodar(video, ws, cfg, args.ate_etapa)
    except KeyboardInterrupt:
        log.warning("Interrompido. Retome com --workspace \"%s\"", ws)
        return 130


if __name__ == "__main__":
    sys.exit(main())
