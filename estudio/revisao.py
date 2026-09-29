"""Etapa 2b: passada do humanizer-br sobre a narração aprovada pelo Jev, ainda em texto corrido.

Prioridade: o texto final não pode soar como IA. Como a divisão em cenas só acontece depois, o humanizer pode
mexer à vontade nas frases: só precisa manter os fatos, a ordem dos acontecimentos, o gancho forte e o tamanho.
- verificador em Python (travessão, hífen duplo, vocabulário da seção 12 da skill): até 2 voltas ao humanizer;
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

# Limites de tolerância: o humanizer só é desfeito em caso grave.
COERENCIA_MINIMA = 0.6
GANCHO_FOLGA = 2.0
VOLTAS_VERIFICADOR = 2

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


def verificar(texto: str) -> list[str]:
    """Trechos com travessão, hífen duplo ou vocabulário da lista. Custo zero."""
    achados = []
    frases = re.split(r"(?<=[.!?])\s+", texto)
    termos = [(t, _regex_termo(t)) for t in vocabulario_ia()]
    for f in frases:
        if _DASHES.search(f):
            achados.append(f"travessão ou hífen duplo em: \"{f.strip()}\"")
        for t, rx in termos:
            if rx.search(f):
                achados.append(f"palavra da lista de IA ({t}) em: \"{f.strip()}\"")
    return achados


def _sistema(canal: dict, palavras_total: int) -> str:
    return ler_skill() + f"""

---
MODO EMBUTIDO. Você está revisando a narração de um vídeo do canal "{canal['nome']}". O objetivo principal é
que o texto soe escrito e falado por uma pessoa, não por uma IA. Reescreva de verdade: mude a construção das frases,
o ritmo e as palavras sempre que soarem genéricas. É ficção; os fatos da história são os do texto recebido.

O que precisa ficar:
- os mesmos acontecimentos, na mesma ordem, com o mesmo final;
- a primeira frase continua sendo um gancho forte e curto, que prende nos primeiros segundos;
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
    entrada = {k: h.get(k, "") for k in ("titulo", "descricao_youtube", "narracao")}
    msgs = [{"role": "system", "content": _sistema(canal, historia.contar_palavras(h["narracao"]))},
            {"role": "user", "content": json.dumps(entrada, ensure_ascii=False, separators=(",", ":"))}]
    resp, _ = clientes.chat("humanizer", msgs, SCHEMA_REVISAO, projeto_id=pid, etapa="revisao", temperatura=0.8,
                            contexto={"entrada": entrada})
    novo = _aplicar(h, resp)
    if novo is None:
        rel["motivo"] = "a passada veio vazia; mantida a versão aprovada"
        db.evento(pid, rel["motivo"], "aviso")
        return h, rel

    # Verificador em Python: até 2 voltas apontando os trechos.
    for volta in range(VOLTAS_VERIFICADOR):
        achados = verificar(_texto_publico(novo))
        if not achados:
            break
        db.evento(pid, f"Verificador achou {len(achados)} trecho(s) com cara de IA; volta {volta + 1} ao humanizer.")
        atual = {k: novo.get(k, "") for k in ("titulo", "descricao_youtube", "narracao")}
        msgs2 = msgs + [{"role": "assistant", "content": json.dumps(atual, ensure_ascii=False)},
                        {"role": "user", "content": "Ainda sobraram estes trechos. Reescreva só eles, mantenha o resto e "
                                                    "devolva o JSON completo:\n" + "\n".join(f"- {a}" for a in achados)}]
        resp2, _ = clientes.chat("humanizer", msgs2, SCHEMA_REVISAO, projeto_id=pid, etapa="revisao",
                                 temperatura=0.7, contexto={"entrada": atual})
        novo2 = _aplicar(novo, resp2)
        if novo2 is None:
            break
        novo = novo2
    rel["achados_depois"] = verificar(_texto_publico(novo))

    problemas, _ = historia.checar_narrativa(novo, canal)
    if problemas:
        rel["avisos"].append("fora do tamanho, aceito mesmo assim: " + "; ".join(problemas))
        db.evento(pid, rel["avisos"][-1], "aviso")

    # Rechecagem do Jev: só desfaz em caso grave.
    base = canais.perguntas_do_canal(cfg)
    perguntas = {"gancho": base["gancho"], "coerencia": base["coerencia"]}
    aval = historia.avaliar(projeto, canal, novo, perguntas, etapa="revisao")
    rel["rechecagem"] = aval
    coer = aval["perguntas"]["coerencia"]["valor"]
    if coer < COERENCIA_MINIMA:
        rel["motivo"] = f"a coerência caiu para {coer:.2f} depois da passada; mantida a versão aprovada"
        db.evento(pid, rel["motivo"], "aviso")
        return h, rel
    gancho = aval["perguntas"]["gancho"]
    if gancho["valor"] < base["gancho"]["corte"] - GANCHO_FOLGA and novo["gancho"] != h["gancho"]:
        fs = historia.frases(novo["narracao"])
        novo = historia.normalizar_narrativa({**novo, "narracao": " ".join([h["gancho"]] + fs[1:])})
        rel["revertidas"].append({"n": 1, "motivo": f"a nota do gancho caiu para {gancho['valor']:.1f}; "
                                                     "voltou a primeira frase original"})
    rel["aplicado"] = True
    rel["motivo"] = f"aplicado; {'gancho original mantido' if rel['revertidas'] else 'texto todo humanizado'}"
    return novo, rel
