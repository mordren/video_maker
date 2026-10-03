"""Etapa 2b: humanizer-br dirigido sobre a narração aprovada pelo Jev, ainda em texto corrido.

As regras do humanizer já vão no prompt de escrita (historia.sistema_escrita). Aqui não há reescrita inteira, que
muda fatos e perde as pistas da virada: um verificador em Python aponta os trechos com cara de IA e só eles voltam
ao humanizer (sem achado, nenhuma chamada).
- verificador em Python (travessão, hífen duplo, vocabulário da seção 12 da skill, comparação decorativa):
  até 2 voltas dirigidas;
- tamanho fora do limite: só avisa;
- rechecagem do Jev só desfaz em caso grave: coerência abaixo de 0,6 (volta tudo) ou gancho mais de 2 pontos
  abaixo do corte (volta só a primeira frase).
"""
import json
import re
from functools import lru_cache

from . import canais, clientes, config, db, historia

SCHEMA_REVISAO = {
    "type": "object", "additionalProperties": False, "required": ["titulo", "descricao_youtube", "narracao"],
    "properties": {"titulo": {"type": "string"}, "descricao_youtube": {"type": "string"},
                   "narracao": {"type": "string"}},
}

# Limites de tolerância do humanizer (COERENCIA_MINIMA, GANCHO_FOLGA, VOLTAS_VERIFICADOR): config.py, aba Configurações.

_DASHES = re.compile(r"[—–]| -- |--")


def ler_skill() -> str:
    try:
        return config.HUMANIZER_SKILL.read_text(encoding="utf-8")
    except OSError:
        return ""


@lru_cache(maxsize=4)
def _vocab_de(texto: str) -> tuple[str, ...]:
    m = re.search(r"### 12\..*?\*\*Procure por:\*\*(.*?)\n", texto, re.S)
    if not m:
        return tuple()
    itens = []
    for parte in re.split(r"[;,]", m.group(1)):
        parte = parte.strip().strip(".").strip()
        parte = re.sub(r"\s*\(([^)]*)\)", r" \1", parte).strip()  # "navegar (por temas)" -> "navegar por temas"
        if parte:
            itens.append(parte.lower())
    return tuple(itens)


def vocabulario_ia() -> list[str]:
    return list(_vocab_de(ler_skill()))


def _regex_termo(t: str) -> re.Pattern:
    if " " in t:
        return re.compile(r"\b" + re.escape(t) + r"\b", re.I)
    raiz = t[:-1] if len(t) > 5 and t[-1] in "aeor" else t
    return re.compile(r"\b" + re.escape(raiz) + r"\w{0,3}\b", re.I)


_COMPARACAO = re.compile(r"\bcomo (?:quem|se)\b", re.I)
# "Não é medo. É o chão." / "não é X, é Y": o contraste encenado (padrão 1 do humanizer-br).
_NAO_X_E_Y = re.compile(r"\bnão (?:é|era|foi)\b[^.!?;]{0,60}[.,;]\s*(?:é|era|foi)\b", re.I)
_COMPARACAO_NO_FIM = re.compile(r",\s*como (?:quem|se)\b[^,.!?]*[.!?]?$", re.I)


def verificar(texto: str) -> list[str]:
    """Trechos com travessão, hífen duplo, vocabulário da lista ou comparação decorativa. Custo zero."""
    achados = []
    frases = re.split(r"(?<=[.!?])\s+", texto)
    termos = [(t, _regex_termo(t)) for t in vocabulario_ia()]
    comparacoes = 0
    for i, f in enumerate(frases):
        dupla = f"{f} {frases[i + 1]}" if i + 1 < len(frases) else f
        if _NAO_X_E_Y.search(f) or (re.match(r"\s*não (?:é|era|foi)\b", f, re.I) and _NAO_X_E_Y.search(dupla)):
            achados.append(f"contraste \"não é X, é Y\" em: \"{dupla.strip() if _NAO_X_E_Y.search(dupla) else f.strip()}\"")
        if _COMPARACAO.search(f):
            comparacoes += 1
            if _COMPARACAO_NO_FIM.search(f.strip()) or comparacoes > 1:
                achados.append(f"comparação decorativa (\"como quem\", \"como se\") em: \"{f.strip()}\"")
        if _DASHES.search(f):
            achados.append(f"travessão ou hífen duplo em: \"{f.strip()}\"")
        for t, rx in termos:
            if rx.search(f):
                achados.append(f"palavra da lista de IA ({t}) em: \"{f.strip()}\"")
    return achados


def _sistema(canal: dict, palavras_total: int) -> str:
    return ler_skill() + f"""

---
MODO EMBUTIDO. Você está revisando a narração de um vídeo do canal "{canal['nome']}". Junto com o texto vem uma
lista de trechos que um verificador marcou com cara de IA. Reescreva só esses trechos, para soarem escritos e falados
por uma pessoa; todo o resto do texto fica exatamente igual. É ficção; os fatos da história são os do texto recebido.

O que precisa ficar:
- os mesmos acontecimentos, na mesma ordem, com o mesmo final;
- a primeira frase continua sendo um gancho forte, que prende nos primeiros segundos;
- tamanho perto de {palavras_total} palavras (mais ou menos 10%);
- texto para ser ouvido: frases que alguém falaria contando a história em voz alta;
- nenhum travessão e nenhum hífen duplo.
As imagens serão escolhidas depois, a partir do seu texto; não se preocupe com cenas.
Devolva só o JSON com titulo, descricao_youtube e narracao."""


def _texto_publico(h: dict) -> str:
    return " ".join([h.get("titulo", ""), h.get("descricao_youtube", ""), h.get("narracao", "")])


def _aplicar(h: dict, resp: dict) -> dict | None:
    narr = re.sub(r"\s+", " ", resp.get("narracao") or "").strip()
    if not narr:
        return None
    return historia.normalizar_narrativa({"titulo": resp.get("titulo") or h["titulo"],
                                          "descricao_youtube": resp.get("descricao_youtube") or h.get("descricao_youtube", ""),
                                          "narracao": narr})


def humanizar(projeto: dict, canal: dict, h: dict) -> tuple[dict, dict]:
    """Recebe e devolve a narrativa em texto corrido (antes da decupagem). Devolve (história, relatório)."""
    rel = {"aplicado": False, "revertidas": [], "avisos": [], "achados_antes": [], "achados_depois": [], "motivo": ""}
    cfg = canal["config"]
    if not cfg.get("humanizer", True):
        rel["motivo"] = "humanizer desligado no canal"
        return h, rel
    if not canal["idioma"].lower().startswith("pt"):
        rel["motivo"] = "o humanizer-br só vale para português"
        return h, rel
    if not ler_skill():
        rel["motivo"] = f"SKILL.md não encontrado em {config.HUMANIZER_SKILL}"
        db.evento(projeto["id"], rel["motivo"], "aviso")
        return h, rel

    pid = projeto["id"]
    rel["achados_antes"] = verificar(_texto_publico(h))
    if not rel["achados_antes"]:
        rel["motivo"] = "nenhum trecho com cara de IA; texto mantido"
        return h, rel
    # O gancho também pode ser corrigido aqui; a rechecagem do Jev devolve o original se ele piorar.
    fixo = projeto.pop("gancho_fixo", None)
    sistema = _sistema(canal, historia.contar_palavras(h["narracao"]))
    novo, achados = h, rel["achados_antes"]
    for volta in range(config.VOLTAS_VERIFICADOR):
        if not achados:
            break
        db.evento(pid, f"Verificador achou {len(achados)} trecho(s) com cara de IA; volta {volta + 1} ao humanizer.")
        atual = {k: novo.get(k, "") for k in ("titulo", "descricao_youtube", "narracao")}
        msgs = [{"role": "system", "content": sistema},
                {"role": "user", "content": json.dumps(atual, ensure_ascii=False, separators=(",", ":")) +
                 "\n\nReescreva só estes trechos; o resto do texto fica exatamente igual. Devolva o JSON completo:\n" +
                 "\n".join(f"- {x}" for x in achados)}]
        resp, _ = clientes.chat("humanizer", msgs, SCHEMA_REVISAO, projeto_id=pid, etapa="revisao",
                                temperatura=0.5, contexto={"entrada": atual})
        novo2 = _aplicar(novo, resp)
        if novo2 is None:
            break
        novo = novo2
        achados = verificar(_texto_publico(novo))
    if fixo:
        projeto["gancho_fixo"] = fixo
    if novo is h:
        rel["motivo"] = "a passada veio vazia; mantida a versão aprovada"
        db.evento(pid, rel["motivo"], "aviso")
        return h, rel
    rel["achados_depois"] = verificar(_texto_publico(novo))

    problemas, _ = historia.checar_narrativa(novo, canal)
    if problemas:
        rel["avisos"].append("fora do tamanho, aceito mesmo assim: " + "; ".join(problemas))
        db.evento(pid, rel["avisos"][-1], "aviso")

    # Rechecagem do Jev: só desfaz em caso grave.
    base = canais.perguntas_do_canal(cfg)
    perguntas = {"gancho": base["gancho"], "coerencia": base["coerencia"]}
    fixo = projeto.pop("gancho_fixo", None)
    aval = historia.avaliar(projeto, canal, novo, perguntas, etapa="revisao")
    if fixo:
        projeto["gancho_fixo"] = fixo
    rel["rechecagem"] = aval
    coer = aval["perguntas"]["coerencia"]["valor"]
    if coer < config.COERENCIA_MINIMA:
        rel["motivo"] = f"a coerência caiu para {coer:.2f} depois da passada; mantida a versão aprovada"
        db.evento(pid, rel["motivo"], "aviso")
        return h, rel
    gancho = aval["perguntas"]["gancho"]
    if gancho["valor"] < base["gancho"]["corte"] - config.GANCHO_FOLGA and novo["gancho"] != h["gancho"]:
        fs = historia.frases(novo["narracao"])
        novo = historia.normalizar_narrativa({**novo, "narracao": " ".join([h["gancho"]] + fs[1:])})
        rel["revertidas"].append({"n": 1, "motivo": f"a nota do gancho caiu para {gancho['valor']:.1f}; "
                                                     "voltou a primeira frase original"})
    rel["aplicado"] = True
    rel["motivo"] = f"aplicado nos trechos apontados{'; gancho original mantido' if rel['revertidas'] else ''}"
    return novo, rel
