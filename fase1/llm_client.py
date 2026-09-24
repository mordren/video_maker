"""LLM contextual (etapa 8), via OpenRouter (mesma chave do JEV).

Modelo padrão: um gratuito do OpenRouter (ver config.yaml), enquanto os
parâmetros do pipeline ainda estão sendo ajustados. Só roda nos blocos que
passaram pelo JEV. Recebe o bloco segmento a segmento, com os tempos, mais um
pouco de fala antes e depois como contexto, e devolve coerência, coesão,
problemas e uma sugestão de corte em segundos.
"""

from __future__ import annotations

import json
import re

from openrouter import APIError, post

ENDPOINT = "/v1/chat/completions"

_SISTEMA = """Você é editor de cortes de vídeo para redes sociais (podcasts, entrevistas, \
lives em português do Brasil). Recebe a transcrição de um bloco candidato, uma linha por \
segmento no formato "[inicio-fim] texto", com tempos em segundos. As linhas marcadas \
(contexto) ficam fora do bloco e servem só para você entender o que vem antes e depois.

Avalie o bloco:
1. coerente: começa e termina em pontos que fazem sentido?
2. coeso: as ideias dentro do bloco estão conectadas, sem saltos estranhos?
3. problemas: frases cortadas, referências a algo dito fora do bloco ("como eu disse \
antes"), buracos lógicos. Frases curtas, em português.
4. sugestao_corte: se o bloco melhora com outros limites, dê os novos inicio e fim em \
segundos, usando os tempos das linhas (pode avançar para o contexto se isso completar a \
ideia). Se os limites já estão bons, use null.

Responda só com JSON:
{"coerente": true, "coeso": true, "problemas": ["..."], \
"sugestao_corte": {"inicio": 123.45, "fim": 187.9, "motivo": "..."} ou null}"""


class LLM:
    def __init__(self, cfg: dict):
        self.modelo = cfg["modelo"]
        self.timeout = cfg.get("timeout", 120)
        self.tentativas = cfg.get("tentativas", 3)

    def analisar(self, antes: list[dict], bloco: list[dict], depois: list[dict]) -> dict:
        linhas = ([_linha(s, "(contexto) ") for s in antes] + [_linha(s) for s in bloco]
                  + [_linha(s, "(contexto) ") for s in depois])
        resp = post(ENDPOINT, {
            "model": self.modelo,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": _SISTEMA},
                {"role": "user", "content": "\n".join(linhas)},
            ],
        }, self.timeout, self.tentativas)
        try:
            msg = resp["choices"][0]["message"]
            # Alguns modelos gratuitos põem o texto em "reasoning_content" ou
            # cortam por limite de tokens e devolvem "content": null.
            conteudo = msg.get("content") or msg.get("reasoning_content")
            if not conteudo or not conteudo.strip():
                raise ValueError(f"content vazio (finish_reason={resp['choices'][0].get('finish_reason')})")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise APIError(f"resposta do LLM sem conteúdo: {exc} — {str(resp)[:300]}") from exc
        return _normaliza(_json(conteudo))


def _linha(s: dict, prefixo: str = "") -> str:
    return f"{prefixo}[{s['start']:.2f}-{s['end']:.2f}] {s['text']}"


def _json(texto: str) -> dict:
    texto = re.sub(r"^```(?:json)?|```$", "", texto.strip(), flags=re.M).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", texto, re.S)
        if not m:
            raise APIError(f"LLM não devolveu JSON: {texto[:300]}")
        return json.loads(m.group(0))


def _normaliza(d: dict) -> dict:
    sug = d.get("sugestao_corte")
    if isinstance(sug, dict):
        try:
            sug = {"inicio": float(sug["inicio"]), "fim": float(sug["fim"]),
                   "motivo": str(sug.get("motivo") or "")}
        except (KeyError, TypeError, ValueError):
            sug = None
    else:
        sug = None
    problemas = d.get("problemas") or []
    if not isinstance(problemas, list):
        problemas = [str(problemas)]
    return {
        "coerente": bool(d.get("coerente")),
        "coeso": bool(d.get("coeso")),
        "problemas": [str(p) for p in problemas],
        "sugestao_corte": sug,
    }
