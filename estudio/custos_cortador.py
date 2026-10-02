"""Custos do Cortador para a aba Custos: o que fase1/custos_api.py anotou em
$ESTUDIO_DATA/custos.jsonl, somado por período, modelo, etapa e trabalho, e o
gasto real das contas (OpenRouter /key e saldo do DeepSeek), que pega também o
que foi gasto fora do Estúdio com a mesma chave.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import requests

_cache: dict[str, tuple[float, dict]] = {}
_trava = threading.Lock()
ETAPAS = {"fase1": "seleção dos trechos (Fase 1)", "longo": "vídeo longo (busca)", "proposta": "sugestão de formato",
          "producao": "produção (Fase 2 + acabamento)", "": "sem etapa"}


def _linhas(data_dir: Path) -> list[dict]:
    arq = data_dir / "custos.jsonl"
    if not arq.exists():
        return []
    saida = []
    with arq.open(encoding="utf-8", errors="replace") as f:
        for linha in f:
            try:
                saida.append(json.loads(linha))
            except ValueError:
                continue
    return saida


def resumo(data_dir: Path, trabalhos: list[dict]) -> dict:
    linhas = _linhas(data_dir)
    agora = datetime.now()
    limites = {"hoje": agora.replace(hour=0, minute=0, second=0, microsecond=0),
               "7 dias": agora - timedelta(days=7), "30 dias": agora - timedelta(days=30)}
    periodos = {k: 0.0 for k in (*limites, "total")}
    por_modelo: dict[tuple, dict] = defaultdict(lambda: {"chamadas": 0, "valor": 0.0, "reais": 0})
    por_etapa: dict[str, float] = defaultdict(float)
    por_trabalho: dict[str, dict] = defaultdict(lambda: {"chamadas": 0, "valor": 0.0})
    por_dia: dict[str, float] = defaultdict(float)
    for ln in linhas:
        v = float(ln.get("valor_usd") or 0)
        try:
            quando = datetime.fromisoformat(ln.get("em", ""))
        except ValueError:
            quando = agora
        periodos["total"] += v
        for k, lim in limites.items():
            if quando >= lim:
                periodos[k] += v
        m = por_modelo[(ln.get("servico", ""), ln.get("tipo", ""), ln.get("modelo", ""))]
        m["chamadas"] += 1
        m["valor"] += v
        m["reais"] += 1 if ln.get("real") else 0
        por_etapa[ln.get("etapa", "")] += v
        if ln.get("trabalho"):
            t = por_trabalho[ln["trabalho"]]
            t["chamadas"] += 1
            t["valor"] += v
        if quando >= agora - timedelta(days=30):
            por_dia[quando.strftime("%Y-%m-%d")] += v
    nomes = {t["id"]: t for t in trabalhos}
    trabalhos_lista = sorted(
        ({"id": tid, "titulo": (nomes.get(tid) or {}).get("titulo_video") or tid,
          "canal": (nomes.get(tid) or {}).get("canal") or "", "existe": tid in nomes, **d}
         for tid, d in por_trabalho.items()), key=lambda x: x["id"], reverse=True)
    return {
        "desde": linhas[0]["em"] if linhas else None,
        "chamadas": len(linhas),
        "periodos": periodos,
        "por_modelo": sorted(({"servico": s, "tipo": t, "modelo": mo, "chamadas": d["chamadas"],
                               "valor": d["valor"], "todos_reais": d["reais"] == d["chamadas"],
                               "algum_real": d["reais"] > 0}
                              for (s, t, mo), d in por_modelo.items()), key=lambda x: -x["valor"]),
        "por_etapa": [{"etapa": ETAPAS.get(e, e), "valor": v} for e, v in sorted(por_etapa.items(), key=lambda x: -x[1])],
        "por_trabalho": trabalhos_lista[:60],
        "por_dia": [{"dia": d, "valor": por_dia[d]} for d in sorted(por_dia)],
    }


def _em_cache(chave: str, segundos: float, fn):
    with _trava:
        if chave in _cache and time.time() - _cache[chave][0] < segundos:
            return _cache[chave][1]
    valor = fn()
    with _trava:
        _cache[chave] = (time.time(), valor)
    return valor


def _openrouter(chave: str) -> dict:
    try:
        r = requests.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {chave}"}, timeout=12)
        if r.status_code != 200:
            return {"erro": f"HTTP {r.status_code}"}
        d = (r.json() or {}).get("data") or {}
        # sem o "label": ele traz o começo da chave
        return {k: d.get(k) for k in ("usage", "usage_daily", "usage_weekly", "usage_monthly", "limit",
                                      "limit_remaining", "is_free_tier")}
    except requests.RequestException as e:
        return {"erro": type(e).__name__}


def _deepseek(chave: str) -> dict:
    try:
        r = requests.get("https://api.deepseek.com/user/balance", headers={"Authorization": f"Bearer {chave}"},
                         timeout=12)
        if r.status_code != 200:
            return {"erro": f"HTTP {r.status_code}"}
        d = r.json() or {}
        return {"disponivel": d.get("is_available"), "saldos": d.get("balance_infos") or []}
    except requests.RequestException as e:
        return {"erro": type(e).__name__}


def contas(chaves: list[tuple[str, str, str]]) -> list[dict]:
    """chaves: [(serviço, rótulo, chave)]. A mesma chave em dois lugares aparece uma vez só.
    Consulta no máximo a cada 5 min (as duas APIs são de graça, mas não precisa martelar)."""
    vistas: dict[str, dict] = {}
    for servico, rotulo, chave in chaves:
        if not chave:
            continue
        if chave in vistas:
            vistas[chave]["usado_por"].append(rotulo)
            continue
        fn = (lambda c=chave: _openrouter(c)) if servico == "openrouter" else (lambda c=chave: _deepseek(c))
        dados = _em_cache(f"{servico}:{chave}", 300, fn)
        vistas[chave] = {"servico": servico, "usado_por": [rotulo], "final": chave[-4:], **dados}
    return list(vistas.values())
