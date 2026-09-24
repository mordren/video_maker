"""Candidatos a gancho: um micro-trecho (~2,5s) do próprio bloco para tocar no
início do clipe, antes do corte principal — a técnica de "mostrar o pico
primeiro" para prender atenção nos primeiros segundos.

Os candidatos vêm do texto BRUTO (antes do corte de silêncio/recomeço), nunca
cruzando um trecho que o corte principal já removeu — senão o gancho mostraria
algo que "some" quando o clipe de verdade começa. Cada candidato é ancorado no
início de uma palavra e, quando possível, termina em pontuação de frase (frase
cortada no meio de uma oração prende menos que uma frase fechada).
"""

from __future__ import annotations

import re

_PONTUACAO_FINAL = re.compile(r"[.!?…]$")


def _cabe_fora_dos_cortes(inicio: float, fim: float, cortes: list[dict]) -> bool:
    return all(fim <= c["inicio"] or inicio >= c["fim"] for c in cortes)


def candidatos(palavras: list[dict], cortes: list[dict], duracao_min: float,
               duracao_max: float, max_candidatos: int) -> list[dict]:
    """Janelas de `duracao_min`..`duracao_max` segundos, ancoradas em início de
    palavra, que não cruzam nenhum corte já decidido (silêncio/recomeço)."""
    saida: list[dict] = []
    n = len(palavras)
    for i in range(n):
        inicio = palavras[i]["start"]
        melhor: dict | None = None
        for j in range(i, n):
            fim = palavras[j]["end"]
            duracao = fim - inicio
            if duracao > duracao_max:
                break
            if duracao < duracao_min:
                continue
            if not _cabe_fora_dos_cortes(inicio, fim, cortes):
                continue
            texto = " ".join(p["word"] for p in palavras[i:j + 1]).strip()
            candidato = {"inicio": round(inicio, 3), "fim": round(fim, 3), "texto": texto}
            # prefere terminar em pontuação de frase; se não achar nenhuma opção
            # assim dentro da faixa, fica com a última janela válida (mais completa)
            if _PONTUACAO_FINAL.search(palavras[j]["word"]):
                melhor = candidato
                break
            melhor = candidato
        if melhor:
            saida.append(melhor)
    return _espalha(saida, max_candidatos)


def _espalha(candidatos_: list[dict], maximo: int) -> list[dict]:
    """Reduz a lista mantendo cobertura ao longo do bloco, não só os primeiros."""
    if len(candidatos_) <= maximo:
        return candidatos_
    passo = len(candidatos_) / maximo
    return [candidatos_[round(i * passo)] for i in range(maximo)]
