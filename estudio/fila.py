"""Fila de trabalho do Estúdio, no Redis — só a ORDEM do que processar a
seguir. `trabalhos.json` continua sendo a fonte da verdade dos dados de cada
trabalho/corte; o Redis só guarda os ids pendentes, para o laço (estudio.py)
não precisar mais varrer o arquivo inteiro a cada poucos segundos e poder
bloquear esperando o próximo item (`proximo_trabalho`) em vez de `sleep(3)`.

Ao subir, `reconstruir()` apaga as duas filas e recoloca nelas tudo que
`trabalhos.json` diz que está pendente — inclusive um trabalho que ficou
preso em "processando" (o serviço caiu no meio) ou um corte preso em
"produzindo"/"refazendo". Por isso sobrevive a reinício sem nada travar ou
duplicar: não importa se o Redis reiniciou junto (fila fica vazia até a
reconstrução) ou não (fila antiga é substituída, não somada).
"""

from __future__ import annotations

import os

import redis

_r = redis.Redis(host="127.0.0.1", port=6379, decode_responses=True, socket_connect_timeout=2)

# Outro prefixo = outra fila: uma instância de teste não apaga nem rouba a fila da produção.
_PREFIXO = os.environ.get("ESTUDIO_FILA_PREFIXO") or "estudio"
_FILA_TRABALHOS = f"{_PREFIXO}:fila:trabalhos"
_FILA_CORTES = f"{_PREFIXO}:fila:cortes"


def enfileirar_trabalho(tid: str) -> None:
    _r.rpush(_FILA_TRABALHOS, tid)


def proximo_trabalho(timeout: int = 3) -> str | None:
    """Bloqueia até `timeout` segundos esperando um id; None se não veio nenhum."""
    item = _r.blpop([_FILA_TRABALHOS], timeout=timeout)
    return item[1] if item else None


def enfileirar_corte(tid: str, cid: str) -> None:
    _r.rpush(_FILA_CORTES, f"{tid}:{cid}")


def proximo_corte() -> tuple[str, str] | None:
    """Não bloqueia — cortes têm prioridade sobre trabalho novo (ver _laco)."""
    item = _r.lpop(_FILA_CORTES)
    if item is None:
        return None
    tid, cid = item.split(":", 1)
    return tid, cid


def reconstruir(trabalhos: list[dict]) -> None:
    """Chamado uma vez, ao subir (ver o bloco de módulo no fim de estudio.py)."""
    with _r.pipeline() as p:
        p.delete(_FILA_TRABALHOS, _FILA_CORTES)
        for t in trabalhos:
            if t.get("status") in ("aguardando", "processando"):
                p.rpush(_FILA_TRABALHOS, t["id"])
            for c in t.get("cortes", []):
                if c.get("status") in ("produzindo", "refazendo"):
                    p.rpush(_FILA_CORTES, f"{t['id']}:{c['id']}")
        p.execute()
