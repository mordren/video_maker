"""Cliente do JEV (TypeSafe) pela Decisions API do OpenRouter.

O JEV não gera texto: responde perguntas tipadas sobre um `state` com
probabilidades calibradas.
- noul   -> probabilidade de "sim", de 0 a 1
- choice -> a opção mais provável + a distribuição completa
- score  -> posição esperada numa escala de níveis 0..n-1 (pode ser fracionária)

Uma única chamada `decide()` roda várias perguntas em paralelo sobre o mesmo
`state` sem custo extra de latência (é assim que a API foi desenhada) — por
isso `qualificar()` faz viral + ritmo + ajuste de limites numa chamada só, em
vez de três.
"""

from __future__ import annotations

import re

from openrouter import APIError, post

ENDPOINT = "/alpha/decisions"

_CRITERIO_VIRAL = [
    "1 - Nenhum potencial: morno, confuso ou sem assunto",
    "2 - Fraco: tem assunto, mas sem gancho nem emoção",
    "3 - Médio: ideia clara, interessa a quem já acompanha",
    "4 - Forte: gancho claro, emoção ou polêmica, gera comentário",
    "5 - Muito forte: prende nos primeiros segundos e pede compartilhamento",
]


class JEV:
    def __init__(self, cfg: dict):
        self.modelo = cfg["modelo"]
        self.timeout = cfg.get("timeout", 30)
        self.tentativas = cfg.get("tentativas", 3)

    def decide(self, state, questions: dict) -> dict:
        resp = post(ENDPOINT, {"model": self.modelo, "state": state, "questions": questions},
                    self.timeout, self.tentativas)
        answers = resp.get("answers") or (resp.get("data") or {}).get("answers")
        if not isinstance(answers, dict) or not set(questions) <= set(answers):
            raise APIError(f"resposta do JEV sem as respostas esperadas: {str(resp)[:300]}")
        return answers

    # Etapa 4 (qualificação dos candidatos que vieram da segmentação por LLM) ----
    def qualificar(self, segmentos: list[dict], n_opcoes: int,
                  gancho: str = "", motivo_editor: str = "") -> dict:
        """Score viral, ritmo e (se precisar) um ajuste fino dos limites — tudo numa chamada.

        Os candidatos já vêm da segmentação semântica (DeepSeek), então já
        devem começar/terminar perto do lugar certo; isto é só um afinamento
        de borda, por isso as opções de início/fim são os primeiros/últimos
        `n_opcoes` segmentos do bloco, não o vídeo inteiro.

        `gancho`/`motivo_editor` são o título e o comentário que o DeepSeek já
        escreveu ao escolher este trecho (etapa 3) — mandados aqui para o JEV
        avaliar com o mesmo contexto que a triagem editorial usou, em vez de
        julgar um texto pelado do zero. Testado sem isso: o score do JEV saiu
        sem nenhuma correlação com a ordem de força que o próprio DeepSeek já
        dá aos candidatos (~0,0 nos dois vídeos testados) — o mais provável é
        que o JEV estivesse simplesmente re-julgando com menos informação.
        """
        state = {
            "gancho_do_editor": gancho or "(nenhum)",
            "motivo_do_editor": motivo_editor or "(nenhum)",
            "segmentos": [
                {"id": f"s{i}", "inicio": round(s["start"], 2), "fim": round(s["end"], 2), "texto": s["text"]}
                for i, s in enumerate(segmentos)],
        }
        n = len(segmentos)
        ini_idx = list(range(min(n_opcoes, n)))
        fim_idx = list(range(max(0, n - n_opcoes), n))
        ini_opts = {f"s{i}": ("manter o começo atual: " if i == 0 else "começar em: ") + _resumo(segmentos[i]["text"])
                    for i in ini_idx}
        fim_opts = {f"s{i}": ("manter o fim atual: " if i == n - 1 else "terminar em: ") + _resumo(segmentos[i]["text"])
                    for i in fim_idx}
        a = self.decide(state, {
            "viral": {
                "type": "score",
                "instructions": (
                    "Avalie o potencial viral deste trecho de vídeo como corte para redes "
                    "sociais. `gancho_do_editor` e `motivo_do_editor` são a justificativa de "
                    "uma primeira triagem — use como contexto do que se esperava encontrar, "
                    "mas julgue pelo texto real dos segmentos: se o motivo não se sustenta "
                    "na fala, dê uma nota baixa mesmo assim. Considere: força do gancho "
                    "inicial, clareza da ideia, densidade de informação, presença de emoção "
                    "ou surpresa, possibilidade de gerar comentário ou compartilhamento."),
                "criteria": _CRITERIO_VIRAL,
            },
            "ritmo": {
                "type": "noul",
                "instructions": ("Este trecho de vídeo mantém ritmo consistente do início ao "
                                 "fim, sem partes arrastadas ou repetitivas?"),
                "criteria": {"true": "Ritmo firme o tempo todo",
                             "false": "Tem partes arrastadas, repetitivas ou enrolação"},
            },
            "precisa_melhora": {
                "type": "noul",
                "instructions": ("Este trecho precisa de ajuste fino nos limites (cortar um "
                                 "pouco o começo, cortar um pouco o fim, ou ambos) para ficar "
                                 "mais forte como corte?"),
                "criteria": {"true": "Tem sobra de preâmbulo ou rabo fraco que deveria sair",
                             "false": "Os limites atuais já estão bons"},
            },
            "inicio": {
                "type": "choice",
                "instructions": ("Em qual segmento o corte deveria começar para ficar mais forte "
                                 "e ainda fazer sentido sozinho?"),
                "criteria": ini_opts,
            },
            "fim": {
                "type": "choice",
                "instructions": ("Em qual segmento o corte deveria terminar para fechar a ideia "
                                 "sem deixar rabo fraco?"),
                "criteria": fim_opts,
            },
        })
        return {
            "score_viral": float(a["viral"]["score"]) + 1.0,
            "ritmo": float(a["ritmo"]["noul"]),
            "precisa_melhora": float(a["precisa_melhora"]["noul"]),
            "inicio_idx": _idx(a["inicio"]["choice"], 0),
            "fim_idx": _idx(a["fim"]["choice"], n - 1),
        }


    # Gancho de 2,5s (Fase 2, pipeline_cortes.py) ---------------------------
    def escolher_gancho(self, candidatos: list[dict], gancho_do_editor: str = "",
                        motivo_editor: str = "") -> dict:
        """Escolhe, entre micro-trechos de ~2-3s do bloco, o melhor para tocar
        sozinho no início do clipe (antes do corte principal), como prévia do
        que vem a seguir — tem que prender atenção mesmo fora de contexto.
        """
        opts = {f"c{i}": _resumo(c["texto"], 200) for i, c in enumerate(candidatos)}
        state = {
            "gancho_do_editor": gancho_do_editor or "(nenhum)",
            "motivo_do_editor": motivo_editor or "(nenhum)",
            "candidatos": opts,
        }
        a = self.decide(state, {
            "melhor": {
                "type": "choice",
                "instructions": (
                    "Este trecho vai tocar sozinho, ISOLADO DO RESTO, nos primeiros 2-3 "
                    "segundos do vídeo — antes mesmo do corte principal começar — para "
                    "prender quem está passando o dedo na tela. `gancho_do_editor` e "
                    "`motivo_do_editor` dizem o que faz este bloco forte; escolha o "
                    "candidato que melhor entrega essa força sozinho, sem precisar do "
                    "resto do contexto para fazer sentido ou impactar: a frase de efeito, "
                    "o dado chocante, a acusação direta — não um preâmbulo ou uma "
                    "transição."),
                "criteria": opts,
            },
        })
        idx = _idx(a["melhor"]["choice"], 0)
        return {"indice": idx, **candidatos[idx]}


def _resumo(texto: str, limite: int = 140) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= limite else texto[:limite - 1] + "…"


def _idx(choice, padrao: int) -> int:
    """Extrai o número de uma chave tipo 's3' ou 'c12' devolvida pelo `choice`."""
    m = re.search(r"\d+", str(choice))
    return int(m.group()) if m else padrao
