"""Etapa 3: segmentação semântica via DeepSeek, no lugar da janela fixa.

O prompt é adaptado do `_CUTS_PROMPT` de `ai_srt.py` — o mesmo que o Corta+Legenda já usa
em produção (painel "Cortes IA") e que já mostrou escolher bem os melhores trechos de
vídeos políticos. Duas diferenças da versão do app:

1. Aqui o LLM referencia os limites por ID de segmento (`s0042`), não escreve "m:ss" de
   cabeça — LLM erra número decimal fácil; o tempo real vem sempre da transcrição, nunca do
   texto que o modelo escreve.
2. O app manda a transcrição inteira numa chamada só, e aqui também: o deepseek-chat tem
   contexto de 64K tokens, e mesmo uma live de 1h (~1500 segmentos de SRT) fica bem abaixo
   disso. Só divide em pedaços se a transcrição estourar um teto de segurança — e aí cada
   pedaço ainda é grande (dezenas de minutos), não uma janela de corte.
"""

from __future__ import annotations

import json
import logging
import re

ENDPOINT = "/chat/completions"  # deepseek_client.BASE_URL já termina em /v1
log = logging.getLogger("fase1")

# Estimativa grosseira (chars/4) só para decidir se cabe numa chamada só; a API
# valida de verdade. Deixa boa margem para o contexto de 64K do deepseek-chat.
_TETO_TOKENS_PEDACO = 40_000

_SISTEMA = """Você é um cortador de vídeos políticos. Recebe a transcrição de uma live, \
discurso, entrevista, debate ou podcast, numerada por segmento — uma linha por segmento, \
no formato "[ID] texto" — e escolhe os melhores trechos para virarem shorts/reels: os mais \
fortes, polêmicos e "meme-áveis", que se sustentam sozinhos.

Alguns segmentos no início e no fim vêm marcados "(contexto, fora do intervalo)": servem só \
para você entender o que vem antes/depois deste pedaço da transcrição — nunca escolha um \
corte que comece ou termine só neles.

O que puxar (uma ou mais categorias por corte):
- declaração-tese ou bordão que resume a posição do orador;
- ataque direto e nominal a adversário, instituição ou grupo;
- momento-personagem: fala arrogante, engraçada, provocadora;
- contradição, revelação de estratégia ou bastidor;
- carga emocional (indignação, exaltação, comoção);
- número ou afirmação forte que sozinha rende manchete.

Como montar cada corte:
- DURAÇÃO ENTRE 1MIN E 2MIN30. Isto é obrigatório: um corte com menos de 1min ou mais de \
2min30 não serve e não deve ser incluído. Um short longo demais não funciona.
- Para chegar a essa duração, pegue o RACIOCÍNIO INTEIRO em volta do momento forte, não só \
a frase de efeito. Comece bem antes, quando a pessoa monta o assunto (o gancho, o setup, a \
pergunta), passe pelo desenvolvimento e só termine depois de a ideia fechar. A frase de \
efeito é o clímax do corte, não o corte inteiro.
- Se um momento forte não tiver contexto suficiente em volta para sustentar 1 minuto, NÃO \
o inclua — melhor deixar de fora do que entregar um corte curto.
- Um assunto por corte; comece numa abertura que já prende e termine numa frase que fecha, \
nunca no meio de um raciocínio.
- inicio_id e fim_id: os IDs de segmento exatos da numeração recebida (nunca invente um ID \
que não apareceu). O corte cobre do início do segmento inicial ao fim do segmento final.

Responda APENAS com JSON, exatamente neste formato:
{"cortes": [{"inicio_id": "s0012", "fim_id": "s0045", "titulo": "...", "comentario": "..."}]}

- titulo: o gancho curto (o "título do short"), no máximo 25 caracteres. Priorize a frase \
de efeito ou o bordão, não a descrição do tema. Em CAIXA ALTA.
- comentario: 1 ou 2 frases dizendo por que o trecho vira um bom short. Quando o trecho \
imputa crime a pessoa nomeada, acusa sobre a vida privada ou xinga alguém identificável, \
comece o comentario com "⚠️ " e diga o risco (difamação, possível strike/desmonetização).

QUALIDADE ACIMA DE QUANTIDADE. Não existe número mínimo de cortes. Traga só os trechos que \
realmente se sustentam sozinhos como um bom short — nem que seja UM único corte, ou \
nenhum. É muito melhor um corte forte do que cinco medianos. Se este pedaço da transcrição \
só tem um momento que presta, devolva só ele. Não encha a lista para parecer mais completo.

Ordene os cortes do mais forte para o mais fraco, não em ordem cronológica. Escolha pelo \
potencial de audiência, sem tomar partido nem distorcer o sentido da fala."""


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
            "temperature": 0.4,
            "max_tokens": 4000,
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
        cortes = dados.get("cortes")
        return cortes if isinstance(cortes, list) else []


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
# Pedaços: só existem por limite de contexto do modelo, não por conteúdo. Um
# vídeo comum cabe inteiro numa chamada só; só divide se estourar o teto.
# ---------------------------------------------------------------------------

def _tokens_estimados(linhas: list[str]) -> int:
    return sum(len(l) for l in linhas) // 4


def _pedacos(segs: list[dict], contexto_seg: float):
    """Divide `segs` no menor número de pedaços que cabe no teto de tokens.

    Tenta primeiro o vídeo inteiro numa chamada só (caso comum). Só quando
    isso estoura o teto é que divide — em pedaços iguais, o quanto baste para
    cada um caber, nunca em janelas curtas — com folga de contexto na borda.
    """
    if not segs:
        return
    linhas_totais = [s["text"] for s in segs]
    n_pedacos = max(1, -(-_tokens_estimados(linhas_totais) // _TETO_TOKENS_PEDACO))  # ceil div
    total = segs[-1]["end"]
    inicio_video = segs[0]["start"]
    duracao_pedaco = (total - inicio_video) / n_pedacos
    t = inicio_video
    for _ in range(n_pedacos):
        fim_nucleo = t + duracao_pedaco
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


def segmentar(segs: list[dict], segmentador: Segmentador, contexto_seg: float) -> list[dict]:
    """Devolve blocos candidatos: {inicio, fim, titulo, comentario, pedaco}."""
    ids = {id(s): f"s{i:04d}" for i, s in enumerate(segs)}
    por_id = {f"s{i:04d}": s for i, s in enumerate(segs)}
    candidatos: list[dict] = []
    pedacos = list(_pedacos(segs, contexto_seg))
    log.info("   vídeo inteiro dividido em %d pedaço(s) (limite de contexto do modelo)", len(pedacos))
    for i, (antes, nucleo, depois) in enumerate(pedacos, 1):
        linhas = _linhas(antes, nucleo, depois, ids)
        log.info("   pedaço %d/%d: %d segmentos (+%d de contexto, ~%d tokens)", i, len(pedacos),
                 len(nucleo), len(antes) + len(depois), _tokens_estimados(linhas))
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
                "gancho": str(b.get("titulo") or "").strip(),
                "comentario": str(b.get("comentario") or "").strip(),
                "pedaco": i,
            })
    return candidatos
