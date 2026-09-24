"""Utilitários de tempo: recorte da transcrição e encaixe de limites de bloco.

A segmentação em si (etapa 3) é semântica, feita pelo LLM em `segmentador_llm.py`
— aqui ficam só as operações sobre a transcrição que todas as etapas reusam:
encaixar um intervalo nas bordas de segmento, montar o texto/palavras de um
bloco, pegar o contexto ao redor e medir sobreposição entre dois blocos.

Todo limite de bloco é encaixado na borda de um segmento da transcrição. Um
segmento pertence a um intervalo quando o seu ponto médio cai dentro dele.
Assim texto e tempo nunca se descolam: o texto de um bloco é sempre
reconstruído a partir da transcrição e dos limites atuais.
"""

from __future__ import annotations

import bisect


def _meio(s: dict) -> float:
    return (s["start"] + s["end"]) / 2


def indices(segs: list[dict], inicio: float, fim: float) -> list[int]:
    return [i for i, s in enumerate(segs) if inicio <= _meio(s) <= fim]


def trecho(segs: list[dict], inicio: float, fim: float) -> list[dict]:
    return [segs[i] for i in indices(segs, inicio, fim)]


def texto(segs: list[dict]) -> str:
    return " ".join(s["text"] for s in segs).strip()


def palavras(segs: list[dict]) -> list[dict]:
    return [w for s in segs for w in s["words"]]


def encaixa(segs: list[dict], inicio: float, fim: float) -> tuple[float, float] | None:
    """Leva (inicio, fim) às bordas dos segmentos que o intervalo cobre."""
    dentro = trecho(segs, inicio, fim)
    if not dentro:
        return None
    return dentro[0]["start"], dentro[-1]["end"]


def monta_bloco(segs: list[dict], inicio: float, fim: float) -> dict | None:
    """Campos de tempo e texto de um bloco, já encaixados na transcrição."""
    limites = encaixa(segs, inicio, fim)
    if not limites:
        return None
    ini, fi = limites
    dentro = trecho(segs, ini, fi)
    return {
        "inicio": round(ini, 3),
        "fim": round(fi, 3),
        "duracao": round(fi - ini, 3),
        "texto": texto(dentro),
        "palavras": palavras(dentro),
    }


def contexto(segs: list[dict], inicio: float, fim: float, segundos: float) -> tuple[list[dict], list[dict]]:
    """Segmentos logo antes e logo depois de [inicio, fim], até `segundos` de distância."""
    starts = [s["start"] for s in segs]
    i = bisect.bisect_left(starts, inicio)
    antes = [s for s in segs[:i] if s["end"] > inicio - segundos and _meio(s) < inicio]
    depois = [s for s in segs if _meio(s) > fim and s["start"] < fim + segundos]
    return antes, depois


def sobreposicao(a: dict, b: dict) -> float:
    """Fração do bloco mais curto que está coberta pelo outro."""
    inter = min(a["fim"], b["fim"]) - max(a["inicio"], b["inicio"])
    if inter <= 0:
        return 0.0
    return inter / max(1e-6, min(a["duracao"], b["duracao"]))
