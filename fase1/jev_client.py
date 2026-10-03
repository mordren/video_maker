"""Cliente do JEV (TypeSafe) pela Decisions API do OpenRouter.

O JEV não gera texto: responde perguntas tipadas sobre um `state` com
probabilidades calibradas.
- noul   -> probabilidade de "sim", de 0 a 1
- choice -> a opção mais provável + a distribuição completa
- score  -> posição esperada numa escala de níveis 0..n-1 (pode ser fracionária)

Uma única chamada `decide()` roda várias perguntas em paralelo sobre o mesmo
`state` sem custo extra de latência (é assim que a API foi desenhada) — por
isso `qualificar()` faz viral + ritmo + abertura + ajuste de limites numa chamada só, em
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
        """Score viral, ritmo, abertura e (se precisar) um ajuste fino dos limites — tudo numa chamada.

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
            "abertura": {
                "type": "noul",
                "instructions": ("Imagine alguém rolando o feed de shorts, sem contexto nenhum. "
                                 "As primeiras frases deste trecho (os primeiros 5 a 10 segundos) "
                                 "fazem essa pessoa parar para assistir?"),
                "criteria": {"true": ("Começa com pergunta direta, confronto, acusação, frase de "
                                      "efeito ou afirmação forte"),
                             "false": ("Começa com preâmbulo, cumprimento, explicação técnica ou "
                                       "abstrata, ou respondendo a algo que não aparece")},
            },
            "tipo_abertura": {
                "type": "choice",
                "instructions": ("Como começa este trecho (as primeiras frases, os primeiros 5 a 10 "
                                 "segundos)? Escolha o tipo que melhor descreve a abertura."),
                "criteria": TIPOS_ABERTURA,
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
            "abertura": float(a["abertura"]["noul"]),
            "tipo_abertura": _tipo_abertura(a.get("tipo_abertura", {}).get("choice")),
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

    # Proposta no Estúdio (estudio.py, sem custo extra: só texto já escrito) --
    def sugerir_formato(self, gancho: str, comentario: str) -> dict:
        """Sugere o formato do 9:16 pelo texto que a Fase 1 já escreveu
        (gancho + comentário do editor) — sem decodificar vídeo nem áudio, por
        isso cabe rodar num corte por proposta, antes de qualquer acabamento.

        Três formatos que o Estúdio sabe produzir hoje: `dinamico` (crop que
        segue quem fala), `transparente` (16:9 sobre fundo desfocado) e
        `imagens` (foto do assunto em cima, corte embaixo — a foto vem do
        Wikidata pelo nome que o DeepSeek extrai do gancho/comentário só
        quando o corte é de fato produzido nesse formato, não aqui).
        Tela dividida (2 pessoas alternando) ainda não tem renderização — ver
        [[video-maker-microedicao-piloto]]. Isto é só a pré-seleção do
        dropdown da proposta: o usuário decide."""
        state = {"gancho": gancho or "(nenhum)", "comentario_do_editor": comentario or "(nenhum)"}
        a = self.decide(state, {
            "formato": {
                "type": "choice",
                "instructions": (
                    "Este corte vai virar um vídeo vertical (9:16) para redes sociais. "
                    "Pelo gancho e pelo comentário do editor, escolha o formato mais "
                    "adequado entre os três."),
                "criteria": {
                    "crop": ("A reação, a emoção ou a expressão de quem fala é parte do que "
                             "prende: depoimento emocional, discussão, entrevista, embate, "
                             "confissão. A pessoa em quadro muda pouco ou nada durante o "
                             "corte — não importa se é uma pessoa só ou uma conversa entre "
                             "poucas, o que importa é valer a pena VER o rosto de quem fala."),
                    "transparente": ("QUANTO MAIS PESSOAS falando ou aparecendo, melhor esta "
                                     "opção: grade/mosaico de chamada de vídeo com vários "
                                     "quadrinhos, debate com muita gente alternando — a câmera "
                                     "dinâmica não consegue seguir todo mundo. OU o interesse "
                                     "está só na ideia/dado falado, sem um rosto específico que "
                                     "valha a pena seguir (leitura de número, estatística, "
                                     "notícia, explicação fria de um fato)."),
                    "imagens": ("O corte inteiro fala de UM assunto específico e nomeável — "
                                "uma pessoa, organização, partido, lugar ou evento — que dá "
                                "para ilustrar com foto/logo dele, e ver o rosto de quem fala "
                                "não é o ponto principal (é um comentário, uma crítica, uma "
                                "explicação SOBRE esse assunto, não uma reação pessoal de quem "
                                "fala). Não muda de assunto no meio do corte."),
                },
            },
        })
        escolha = str(a["formato"]["choice"]).lower()
        if "imagens" in escolha:
            formato = "imagens"
        elif "transparente" in escolha:
            formato = "transparente"
        else:
            formato = "dinamico"
        return {"formato_sugerido": formato}

    # Corte inicial do vídeo longo (estudio/video_longo.py) -----------------
    def melhor_previa(self, candidatos: list[dict], tema: str = "", comentario: str = "") -> dict:
        """Escolhe, entre janelas de ~15 s do trecho longo, a melhor parte para
        servir de prévia (o que aparece na proposta antes de produzir os
        15-20 min): tem que prender e se entender sozinha."""
        opts = {f"c{i}": _resumo(c["texto"], 320) for i, c in enumerate(candidatos)}
        state = {"tema_do_video": tema or "(nenhum)", "motivo_do_editor": comentario or "(nenhum)",
                 "candidatos": opts}
        a = self.decide(state, {
            "melhor": {
                "type": "choice",
                "instructions": (
                    "Um vídeo longo de 15-20 minutos vai ganhar uma prévia de ~15 segundos, "
                    "sem legenda, para quem escolhe se vale produzi-lo. Escolha o candidato "
                    "que mostra a MELHOR PARTE do vídeo: a fala mais forte (confronto, "
                    "revelação, tirada, afirmação marcante) e que se entende sozinha, sem o "
                    "resto do contexto. Evite cumprimento, preâmbulo, leitura de comentário "
                    "ou frase que começa respondendo a algo que não aparece."),
                "criteria": opts,
            },
        })
        idx = _idx(a["melhor"]["choice"], 0)
        return {"indice": idx, **candidatos[idx]}

    def tem_contexto(self, texto: str, gancho_do_editor: str = "", motivo_editor: str = "") -> dict:
        """Confere, depois de escolhido, se o texto ISOLADO do gancho (transcrito
        de novo só a partir do áudio recortado, sem o resto do bloco) ainda
        faz sentido sozinho — a escolha em `escolher_gancho` usa o texto vindo
        da transcrição do bloco inteiro, que pode diferir um pouco do que o
        Whisper entende sem esse contexto ao redor."""
        state = {
            "gancho_do_editor": gancho_do_editor or "(nenhum)",
            "motivo_do_editor": motivo_editor or "(nenhum)",
            "texto_isolado": texto,
        }
        a = self.decide(state, {
            "tem_contexto": {
                "type": "noul",
                "instructions": (
                    "`texto_isolado` é a transcrição de um trecho de ~3-7s que vai tocar "
                    "SOZINHO, sem nada antes, como abertura de um corte para redes sociais. "
                    "Alguém que não viu mais nada do vídeo entenderia do que se trata e "
                    "sentiria vontade de continuar assistindo? Responda false para um "
                    "preâmbulo vago, uma frase de transição ('é o seguinte', 'vamos lá') ou "
                    "algo que só faz sentido com o que vem antes ou depois."),
                "criteria": {"true": "Faz sentido e prende sozinho, mesmo sem mais contexto",
                             "false": "Vago, incompleto ou só faz sentido com contexto externo"},
            },
        })
        return {"tem_contexto": float(a["tem_contexto"]["noul"])}


def _resumo(texto: str, limite: int = 140) -> str:
    texto = " ".join(texto.split())
    return texto if len(texto) <= limite else texto[:limite - 1] + "…"


# Tipo da abertura (pergunta "tipo_abertura" do qualificar). Os três primeiros
# são os que os dados do info mostraram segurando mais, e monólogo técnico e
# abstrato os que perdem mais (EDICAO_CANAL.md §1 — pista de só 10 vídeos; o
# tipo fica gravado para a aba Desempenho conferir). Não entra na nota final.
TIPOS_ABERTURA = {
    "pergunta_jornalista": "Abre com uma pergunta direta (de jornalista, entrevistador ou a quem assiste)",
    "confronto": "Abre com tensão, acusação, embate ou alguém sendo rebatido",
    "urgente": "Abre com notícia quente: 'urgente', 'acabou de acontecer', 'agora'",
    "frase_de_efeito": "Abre com uma afirmação forte ou frase de efeito que se sustenta sozinha",
    "monologo_tecnico": "Abre explicando algo técnico, dado ou processo, sem tensão",
    "abstrato": "Abre com contexto genérico, preâmbulo ou cumprimento",
    "nenhum": "Não dá para dizer como abre, ou começa respondendo algo que não aparece",
}


def _tipo_abertura(choice) -> str:
    """A chave de TIPOS_ABERTURA que o JEV devolveu ("" se veio outra coisa)."""
    c = str(choice or "").strip()
    return c if c in TIPOS_ABERTURA else ""


def _idx(choice, padrao: int) -> int:
    """Extrai o número de uma chave tipo 's3' ou 'c12' devolvida pelo `choice`."""
    m = re.search(r"\d+", str(choice))
    return int(m.group()) if m else padrao
