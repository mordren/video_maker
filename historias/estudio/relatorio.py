"""Exporta o que aconteceu com cada vídeo, para analisar e melhorar o programa.

- historico.log: por vídeo, em ordem: eventos, custos, versões com as notas do Jev, relatório do humanizer
  e, se o vídeo estiver ligado ao YouTube, as métricas.
- resumo_videos.jsonl: uma linha por vídeo com os números que importam para calibrar (notas, rodadas, custos,
  retenção). É o arquivo para cruzar nota do gancho com retenção.
"""
import json
from datetime import datetime

from . import config, db, historia
from .registro import PASTA_LOGS


def _metricas(pid: int) -> dict:
    m = db.um("SELECT * FROM metricas WHERE projeto_id = ?", (pid,))
    return db.carregar_json(m["dados_json"], {}) if m else {}


def resumo_projeto(p: dict) -> dict:
    pid = p["id"]
    versoes = db.todos("SELECT versao, origem, nota_geral, passou, checagem_json, avaliacao_json FROM historias "
                       "WHERE projeto_id = ? ORDER BY versao", (pid,))
    falhas: dict[str, int] = {}
    for v in versoes:
        aval = db.carregar_json(v["avaliacao_json"], {}) or {}
        for k in aval.get("falhas", []):
            falhas[k] = falhas.get(k, 0) + 1
    aval_final = db.carregar_json(p.get("avaliacao_json"), {}) or {}
    h = db.carregar_json(p.get("historia_json"), {}) or {}
    custos_etapa = {r["etapa"]: round(r["v"], 6) for r in
                    db.todos("SELECT etapa, SUM(valor_usd) AS v FROM custos WHERE projeto_id = ? GROUP BY etapa", (pid,))}
    canal = db.um("SELECT nome FROM canais WHERE id = ?", (p["canal_id"],)) or {}
    met = _metricas(pid)
    return {
        "id": pid, "canal": canal.get("nome"), "assunto": p["assunto"], "titulo": h.get("titulo"),
        "status": p["status"], "criado_em": p["criado_em"], "ressalva": p.get("ressalva"), "erro": p.get("erro"),
        "versoes": len(versoes),
        "reescritas": sum(1 for v in versoes if v["origem"] == "reescrita"),
        "falhas_por_pergunta": falhas,
        "notas_finais": {k: q["valor"] for k, q in (aval_final.get("perguntas") or {}).items()},
        "nota_geral_final": aval_final.get("nota_geral"),
        "gancho": h.get("gancho"),
        "palavras": historia.contar_palavras(historia.narracao_completa(h)),
        "cenas": len(h.get("cenas", [])),
        "custos_por_etapa": custos_etapa, "custo_total": round(sum(custos_etapa.values()), 6),
        "youtube_id": p.get("youtube_id"),
        "youtube": {k: met.get(k) for k in ("titulo", "publicado_em", "views", "views_engajadas", "likes",
                                             "comentarios", "compartilhamentos", "duracao_s", "media_visualizacao_s",
                                             "media_percentual", "retencao_3s", "retencao_fim", "atualizado_em")
                    if met.get(k) is not None} if met else None,
    }


def exportar() -> dict:
    """Gera os dois arquivos em dados/logs e devolve os caminhos."""
    carimbo = datetime.now().strftime("%Y-%m-%d_%H%M")
    arq_log = PASTA_LOGS / f"historico_{carimbo}.log"
    arq_resumo = PASTA_LOGS / "resumo_videos.jsonl"
    projetos = db.todos("SELECT * FROM projetos ORDER BY id")
    with open(arq_log, "w", encoding="utf-8") as f, open(arq_resumo, "w", encoding="utf-8") as r:
        f.write(f"Histórico exportado em {datetime.now():%Y-%m-%d %H:%M} · {len(projetos)} vídeo(s)\n")
        f.write(f"Modelos: texto {config.modelo_texto()} · juiz {config.MODELO_JUIZ} · imagem {config.MODELO_IMAGEM}\n")
        for p in projetos:
            res = resumo_projeto(p)
            r.write(json.dumps(res, ensure_ascii=False) + "\n")
            f.write("\n" + "=" * 100 + "\n")
            f.write(f"VÍDEO #{p['id']} · {res['canal']} · {p['assunto']}\n")
            f.write(f"Título: {res['titulo'] or '-'} · status: {p['status']} · criado: {p['criado_em']} · "
                    f"custo: US$ {res['custo_total']:.4f}\n")
            if res["ressalva"]:
                f.write(f"Ressalva: {res['ressalva']}\n")
            if res["youtube"]:
                f.write(f"YouTube {p.get('youtube_id')}: {json.dumps(res['youtube'], ensure_ascii=False)}\n")
            f.write("-" * 100 + "\n")
            linhas = [(e["criado_em"], f"{e['nivel'].upper():<5} | EVENTO | {e['msg']}")
                      for e in db.todos("SELECT * FROM eventos WHERE projeto_id = ?", (p["id"],))]
            linhas += [(c["criado_em"], f"INFO  | CUSTO  | {c['etapa']} · {c['servico']} · {c['modelo']} · "
                                        f"US$ {c['valor_usd']:.6f} · {c['detalhe'] or ''}")
                       for c in db.todos("SELECT * FROM custos WHERE projeto_id = ?", (p["id"],))]
            for v in db.todos("SELECT * FROM historias WHERE projeto_id = ?", (p["id"],)):
                aval = db.carregar_json(v["avaliacao_json"], {}) or {}
                ch = db.carregar_json(v["checagem_json"], {}) or {}
                notas = ", ".join(f"{k}={q['valor']:.2f}{'' if q['passou'] else '✗'}"
                                  for k, q in (aval.get("perguntas") or {}).items())
                h = db.carregar_json(v["historia_json"], {}) or {}
                texto = historia.narracao_completa(h)
                linhas.append((v["criado_em"], f"INFO  | JEV    | versão {v['versao']} ({v['origem']}) · "
                                               f"nota geral {v['nota_geral'] if v['nota_geral'] is not None else '-'} · "
                                               f"{notas or 'sem avaliação'}"
                                               f"{' · problemas: ' + '; '.join(ch['problemas']) if ch.get('problemas') else ''}"
                                               f"\n      narração: {texto}"))
            for quando, linha in sorted(linhas, key=lambda x: x[0] or ""):
                f.write(f"{quando} | {linha}\n")
    return {"historico": arq_log, "resumo": arq_resumo}
