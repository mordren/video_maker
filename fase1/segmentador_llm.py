"""Etapa 3 (v2): segmentação semântica via DeepSeek, no lugar da janela fixa.

Por quê: uma grade de tempo fixo (a v1 usava janelas de 90s) corta às cegas —
raramente coincide com o início/fim real de uma ideia. Aqui é o contrário: o
LLM lê a transcrição e aponta os próprios limites dos blocos candidatos.

Para não deixar o LLM inventar timestamp (ele erra número decimal fácil), a
transcrição é mandada com um ID curto por segmento (`s0042`) e ele só
referencia esses IDs; o tempo real vem sempre da transcrição, nunca do texto
que o modelo escreve.

Um vídeo de 1h não cabe (ou não deveria caber, por segurança) numa única
chamada, então a transcrição é dividida em pedaços de tempo fixo (bem maiores
que uma janela de corte — ~15 min) só para caber no contexto do modelo, com
uma margem de segundos de contexto extra em cada borda para não perder um
bloco que atravessa a fronteira do pedaço. Isso é chunking por limite de
contexto, não segmentação de conteúdo — os blocos candidatos é que carregam o
sentido.
"""

from __future__ import annotations

import json
import logging
import re

ENDPOINT = "/chat/completions"  # deepseek_client.BASE_URL já termina em /v1
log = logging.getLogger("fase1")

_SISTEMA = """Você separa a transcrição de um vídeo falado (podcast, entrevista, live, em \
português do Brasil) nos melhores trechos para virarem cortes de rede social.

A transcrição vem numerada por segmento, um por linha: "[ID] texto". Alguns segmentos no \
início e no fim (marcados "(contexto, fora do intervalo)") são só para você entender o que \
vem antes/depois deste pedaço — nunca proponha um bloco que comece ou termine só neles.

Para cada bloco que valha a pena cortar:
- Escolha o ID de início e o ID de fim (o bloco cobre do início do segmento inicial ao fim \
do segmento final).
- O bloco deve ter começo e fim que fazem sentido sozinhos: começa numa ideia nova (não no \
meio de uma frase) e termina quando essa ideia se fecha (pergunta respondida, argumento \
concluído, frase de efeito) — não corte no meio de um raciocínio.
- Prefira blocos entre 20 e 180 segundos. Pode haver blocos que se sobrepõem (dois cortes \
possíveis do mesmo trecho, um mais curto e direto, outro mais longo e completo) — liste os \
dois, a etapa seguinte escolhe.
- Ignore trechos que são só transição, saudação, chamada, sumário de pauta ou sem assunto.
- Não invente texto: só use os IDs que existem na numeração.

Responda só com JSON:
{"blocos": [{"inicio_id": "s0012", "fim_id": "s0034", "gancho": "resumo de 6-10 palavras \
do que torna este trecho forte"}, ...]}
Se não houver nenhum bloco bom neste pedaço, devolva {"blocos": []}."""


class Segmentador:
    def __init__(self, cliente_llm, cfg: dict):
        """`cliente_llm` é um módulo com `.post(path, payload, timeout, tentativas)` e
        `.APIError` — hoje `deepseek_client`, mas qualquer cliente chat-completions serve."""
        self.cliente = cliente_llm
        self.modelo = cfg["modelo"]
        self.timeout = cfg.get("timeout", 180)
        self.tentativas = cfg.get("tentativas", 3)

    def segmentar_pedaco(self, linhas: list[str]) -> list[dict]:
        resp = self.cliente.post(ENDPOINT, {
            "model": self.modelo,
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": _SISTEMA},
                {"role": "user", "content": "\n".join(linhas)},
            ],
        }, self.timeout, self.tentativas)
        try:
            msg = resp["choices"][0]["message"]
            conteudo = msg.get("content") or msg.get("reasoning_content")
            if not conteudo or not conteudo.strip():
                raise ValueError(f"content vazio (finish_reason={resp['choices'][0].get('finish_reason')})")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise self.cliente.APIError(f"resposta do segmentador sem conteúdo: {exc}") from exc
        dados = _json(conteudo, self.cliente.APIError)
        blocos = dados.get("blocos")
        return blocos if isinstance(blocos, list) else []


def _json(texto: str, erro_cls) -> dict:
    texto = re.sub(r"^```(?:json)?|```$", "", texto.strip(), flags=re.M).strip()
    try:
        return json.loads(texto)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", texto, re.S)
        if not m:
            raise erro_cls(f"segmentador não devolveu JSON: {texto[:300]}")
        return json.loads(m.group(0))


# ---------------------------------------------------------------------------
# Chunking por tempo (só para caber no contexto do modelo) + montagem dos blocos
# ---------------------------------------------------------------------------

def _pedacos(segs: list[dict], duracao_min: float, contexto_seg: float):
    """Gera (contexto_antes, nucleo, contexto_depois) cobrindo o vídeo inteiro."""
    if not segs:
        return
    duracao = duracao_min * 60
    total = segs[-1]["end"]
    t = segs[0]["start"]
    while t < total:
        fim_nucleo = t + duracao
        nucleo = [s for s in segs if t <= (s["start"] + s["end"]) / 2 < fim_nucleo]
        antes = [s for s in segs if t - contexto_seg <= (s["start"] + s["end"]) / 2 < t]
        depois = [s for s in segs if fim_nucleo <= (s["start"] + s["end"]) / 2 < fim_nucleo + contexto_seg]
        if nucleo:
            yield antes, nucleo, depois
        t = fim_nucleo


def _linhas(antes: list[dict], nucleo: list[dict], depois: list[dict], ids: dict) -> list[str]:
    saida = []
    for s in antes:
        saida.append(f"(contexto, fora do intervalo) [{ids[id(s)]}] {s['text']}")
    for s in nucleo:
        saida.append(f"[{ids[id(s)]}] {s['text']}")
    for s in depois:
        saida.append(f"(contexto, fora do intervalo) [{ids[id(s)]}] {s['text']}")
    return saida


def segmentar(segs: list[dict], segmentador: Segmentador, duracao_min: float,
             contexto_seg: float) -> list[dict]:
    """Devolve blocos candidatos: {inicio, fim, gancho, pedaco}, com tempos reais da transcrição."""
    ids = {id(s): f"s{i:04d}" for i, s in enumerate(segs)}
    por_id = {f"s{i:04d}": s for i, s in enumerate(segs)}
    candidatos: list[dict] = []
    pedacos = list(_pedacos(segs, duracao_min, contexto_seg))
    for i, (antes, nucleo, depois) in enumerate(pedacos, 1):
        linhas = _linhas(antes, nucleo, depois, ids)
        log.info("   pedaço %d/%d: %d segmentos (+%d de contexto)", i, len(pedacos),
                 len(nucleo), len(antes) + len(depois))
        try:
            brutos = segmentador.segmentar_pedaco(linhas)
        except Exception as exc:  # noqa: BLE001 — um pedaço falhar não derruba o resto
            log.error("   pedaço %d falhou: %s", i, exc)
            continue
        nucleo_ids = {ids[id(s)] for s in nucleo}
        for b in brutos:
            ini_id, fim_id = b.get("inicio_id"), b.get("fim_id")
            if ini_id not in por_id or fim_id not in por_id:
                log.warning("   bloco com ID inexistente ignorado: %s", b)
                continue
            ini_s, fim_s = por_id[ini_id], por_id[fim_id]
            if fim_s["end"] <= ini_s["start"]:
                continue
            # Só aceita blocos cujo NÚCLEO deste pedaço está dentro dele — evita duplicar
            # o mesmo bloco em dois pedaços vizinhos por causa do contexto compartilhado.
            if ini_id not in nucleo_ids and fim_id not in nucleo_ids:
                continue
            candidatos.append({
                "inicio": round(ini_s["start"], 3),
                "fim": round(fim_s["end"], 3),
                "gancho": str(b.get("gancho") or "").strip(),
                "pedaco": i,
            })
    return candidatos
