"""Cliente do JEV (TypeSafe) pela Decisions API do OpenRouter.

O JEV não gera texto: responde perguntas tipadas sobre um `state` com
probabilidades calibradas.
- noul   -> probabilidade de "sim", de 0 a 1
- choice -> a opção mais provável + a distribuição completa
- score  -> posição esperada numa escala de níveis 0..n-1 (pode ser fracionária)

Por isso a etapa 6 não pede timestamps ao JEV: oferece como opções os
segmentos do começo e do fim do bloco e deixa ele escolher onde cortar.
"""

from __future__ import annotations

from openrouter import APIError, post

ENDPOINT = "/alpha/decisions"


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

    # Etapa 4 --------------------------------------------------------------
    def coerente(self, texto: str) -> float:
        """Pré-filtro: o trecho TEM um assunto/ideia aproveitável?

        Isto é uma janela de tempo fixo, cortada às cegas — quase nunca começa
        ou termina exatamente no lugar certo. Por isso a pergunta é sobre o
        CONTEÚDO (dá pra reconhecer um assunto no meio do trecho?), não sobre
        os cortes: ajustar os limites é trabalho da etapa 6 (JEV) e da 8 (LLM),
        que só rodam para quem passar daqui. Pedir "começo e fim perfeitos"
        aqui reprovava quase tudo, mesmo material bom.
        """
        a = self.decide(texto, {"coerente": {
            "type": "noul",
            "instructions": (
                "O texto a seguir é a transcrição de um trecho de vídeo falado, recortado "
                "por tempo fixo — o começo e o fim exatos ainda não foram ajustados, então "
                "IGNORE se a primeira ou a última frase parecem cortadas no meio. A pergunta "
                "é sobre o miolo do trecho: existe, em algum ponto dele, um assunto ou ideia "
                "reconhecível e que se sustenta sozinho (uma opinião, um fato, uma história, "
                "uma resposta), ou o trecho inteiro é só transição, saudação, sumário de "
                "pauta ou barulho sem conteúdo?"),
            "criteria": {
                "true": "Tem um assunto/ideia reconhecível em algum trecho, aproveitável como corte",
                "false": "É só transição, saudação, chamada ou não tem assunto nenhum",
            },
        }})
        return float(a["coerente"]["noul"])

    # Etapa 5 --------------------------------------------------------------
    def viral(self, texto: str) -> float:
        """Nota de 1 a 5 (média ponderada pelas probabilidades, pode ser fracionária)."""
        a = self.decide(texto, {"viral": {
            "type": "score",
            "instructions": (
                "Avalie o potencial viral deste trecho de vídeo como corte para redes "
                "sociais. Considere: força do gancho inicial, clareza da ideia, densidade "
                "de informação, presença de emoção ou surpresa, possibilidade de gerar "
                "comentário ou compartilhamento."),
            "criteria": [
                "1 - Nenhum potencial: morno, confuso ou sem assunto",
                "2 - Fraco: tem assunto, mas sem gancho nem emoção",
                "3 - Médio: ideia clara, interessa a quem já acompanha",
                "4 - Forte: gancho claro, emoção ou polêmica, gera comentário",
                "5 - Muito forte: prende nos primeiros segundos e pede compartilhamento",
            ],
        }})
        return float(a["viral"]["score"]) + 1.0

    # Etapa 6 --------------------------------------------------------------
    def qualidade(self, segmentos: list[dict], n_opcoes: int) -> dict:
        """Ritmo, necessidade de ajuste e em qual segmento começar/terminar.

        `segmentos` são os do bloco, em ordem. As opções de início são os
        primeiros `n_opcoes`; as de fim, os últimos `n_opcoes`. O índice
        devolvido é a posição dentro de `segmentos`.
        """
        state = {"segmentos": [
            {"id": f"s{i}", "inicio": round(s["start"], 2), "fim": round(s["end"], 2), "texto": s["text"]}
            for i, s in enumerate(segmentos)]}
        n = len(segmentos)
        ini_idx = list(range(min(n_opcoes, n)))
        fim_idx = list(range(max(0, n - n_opcoes), n))
        ini_opts = {f"s{i}": ("manter o começo atual: " if i == 0 else "começar em: ") + _resumo(segmentos[i]["text"])
                    for i in ini_idx}
        fim_opts = {f"s{i}": ("manter o fim atual: " if i == n - 1 else "terminar em: ") + _resumo(segmentos[i]["text"])
                    for i in fim_idx}
        a = self.decide(state, {
            "ritmo": {
                "type": "noul",
                "instructions": ("Este trecho de vídeo mantém ritmo consistente do início ao "
                                 "fim, sem partes arrastadas ou repetitivas?"),
                "criteria": {"true": "Ritmo firme o tempo todo",
                             "false": "Tem partes arrastadas, repetitivas ou enrolação"},
            },
            "precisa_melhora": {
                "type": "noul",
                "instructions": ("Este trecho precisa de ajuste nos limites (cortar o começo, "
                                 "cortar o fim, ou ambos) para ficar mais forte como corte?"),
                "criteria": {"true": "Tem preâmbulo ou rabo fraco que deveria sair",
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
            "ritmo": float(a["ritmo"]["noul"]),
            "precisa_melhora": float(a["precisa_melhora"]["noul"]),
            "inicio_idx": _idx(a["inicio"]["choice"], 0),
            "fim_idx": _idx(a["fim"]["choice"], n - 1),
        }


def _resumo(texto: str, limite: int = 140) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= limite else texto[:limite - 1] + "…"


def _idx(choice, padrao: int) -> int:
    try:
        return int(str(choice).lstrip("s"))
    except ValueError:
        return padrao
