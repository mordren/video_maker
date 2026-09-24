"""Fase 1 — seleção e qualificação de blocos de um vídeo longo.

    python pipeline.py <video.mp4> [--workspace pasta] [--ate-etapa N]

Etapas: 1 áudio · 2 transcrição (SRT ou Whisper) · 3 janelas · 4 JEV coerência ·
5 JEV potencial viral · 6 JEV qualidade/limites · 7 consolidação · 8 LLM contextual ·
9 ranqueamento · 10 blocos_finais.json.

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

import segmenter as sg
from jev_client import JEV
from llm_client import LLM
from openrouter import APIError, api_key
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
    ordem = sorted(blocos, key=lambda b: (b["score_viral"], b["prob_coerente"]), reverse=True)
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
    cj, cl, cjan = cfg["jev"], cfg["llm"], cfg["janela"]
    minimo = cjan["duracao_minima_bloco"]

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

    # 3 ─ janelas
    with etapa(3, "pré-segmentação por tempo"):
        janelas = sg.janelas(segs, cjan["duracao"], cjan["sobreposicao"], cjan["palavras_minimas"])
        gravar_json(ws / "janelas.json", janelas)
        log.info("   %d janelas de ~%ss (sobreposição %ss)", len(janelas), cjan["duracao"], cjan["sobreposicao"])
    if ate_etapa <= 3:
        return 0

    try:
        api_key()
    except APIError as exc:
        log.error("%s", exc)
        log.error("Depois de preencher, continue com: python pipeline.py \"%s\" --workspace \"%s\"", video, ws)
        return 2
    jev = JEV(cj)

    # 4 ─ JEV: coerência
    with etapa(4, "JEV — é um bloco coerente?"):
        r4 = em_paralelo(ws / "jev_etapa4_coerencia.json", janelas,
                         lambda j: {"prob_coerente": jev.coerente(j["texto"])}, cj["paralelo"])
        aprovadas = [dict(j, prob_coerente=round(r4[j["id"]]["prob_coerente"], 4)) for j in janelas
                     if r4[j["id"]].get("prob_coerente", -1) >= cj["limiar_coerente"]]
        log.info("   entraram %d · aprovadas %d (limiar %.2f)", len(janelas), len(aprovadas), cj["limiar_coerente"])

    # 5 ─ JEV: potencial viral
    with etapa(5, "JEV — potencial viral"):
        r5 = em_paralelo(ws / "jev_etapa5_viral.json", aprovadas,
                         lambda b: {"score_viral": jev.viral(b["texto"])}, cj["paralelo"])
        pontuados = [dict(b, score_viral=round(r5[b["id"]]["score_viral"], 2)) for b in aprovadas
                     if "score_viral" in r5[b["id"]]]
        log.info("   entraram %d · pontuados %d", len(aprovadas), len(pontuados))

    # 6 ─ JEV: qualidade interna e limites
    with etapa(6, "JEV — qualidade interna e ajuste de limites"):
        def pergunta6(b):
            return jev.qualidade(sg.trecho(segs, b["inicio"], b["fim"]), cj["opcoes_limite"])

        r6 = em_paralelo(ws / "jev_etapa6_qualidade.json", pontuados, pergunta6, cj["paralelo"])
        ajustados = 0
        for b in pontuados:
            r = r6[b["id"]]
            b["qualidade_interna"] = round(r["ritmo"], 4) if "ritmo" in r else None
            b["precisa_melhora"], b["motivo_melhora"] = False, None
            if "erro" in r:
                continue
            dentro = sg.trecho(segs, b["inicio"], b["fim"])
            ini, fim = r["inicio_idx"], r["fim_idx"]
            corta_ini, corta_fim = ini > 0, fim < len(dentro) - 1
            if r["precisa_melhora"] < cj["limiar_precisa_melhora"] or not (corta_ini or corta_fim) or fim <= ini:
                continue
            motivo = {(True, False): "cortar o começo", (False, True): "cortar o fim",
                      (True, True): "cortar o começo e o fim"}[(corta_ini, corta_fim)]
            b["precisa_melhora"] = True
            b["motivo_melhora"] = f"{motivo} (JEV {r['precisa_melhora']:.2f})"
            b["sugestao_jev"] = {"inicio": dentro[ini]["start"], "fim": dentro[fim]["end"]}
            ajustados += ajusta_limites(b, segs, dentro[ini]["start"], dentro[fim]["end"],
                                        "jev", motivo, minimo)
        log.info("   %d blocos · %d com limites ajustados", len(pontuados), ajustados)

    # 7 ─ consolidação
    with etapa(7, "consolidar blocos"):
        blocos = deduplica(pontuados, cfg["consolidacao"]["sobreposicao_maxima"])
        for b in blocos:
            b["origem"] = b.pop("id")
            b["id"] = b["origem"].replace("janela", "bloco")
        gravar_json(ws / "blocos_jev.json", blocos)
        log.info("   entraram %d · ficaram %d", len(pontuados), len(blocos))
    if ate_etapa <= 7:
        return 0

    # 8 ─ LLM contextual
    with etapa(8, f"LLM contextual ({cl['modelo']})"):
        llm = LLM(cl)

        def pergunta8(b):
            antes, depois = sg.contexto(segs, b["inicio"], b["fim"], cl["contexto_segundos"])
            return {"analise": llm.analisar(antes, sg.trecho(segs, b["inicio"], b["fim"]), depois)}

        r8 = em_paralelo(ws / "llm_etapa8.json", blocos, pergunta8, cl["paralelo"])
        ajustados = 0
        for b in blocos:
            r = r8[b["id"]]
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
                 len(r8), ajustados, len(blocos))

    # 9 ─ ranqueamento
    with etapa(9, "ranqueamento final"):
        pesos = cfg["ranking"]
        for b in blocos:
            b["nota_llm"] = round(nota_llm(b.get("analise_llm")), 4)
            b["nota_final"] = round(pesos["peso_viral"] * (b["score_viral"] - 1) / 4
                                    + pesos["peso_coerencia"] * b["prob_coerente"]
                                    + pesos["peso_llm"] * b["nota_llm"], 4)
        blocos.sort(key=lambda b: b["nota_final"], reverse=True)
        for i, b in enumerate(blocos, 1):
            b["rank"] = i

    # 10 ─ JSON final
    with etapa(10, "salvar blocos_finais.json"):
        campos = ["id", "rank", "inicio", "fim", "duracao", "texto", "palavras", "fonte_transcricao",
                  "score_viral", "prob_coerente", "qualidade_interna", "precisa_melhora",
                  "motivo_melhora", "analise_llm", "nota_llm", "nota_final", "origem", "ajustes", "erros"]
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
                    help="para depois desta etapa (3 = só janelas, sem gastar API)")
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
