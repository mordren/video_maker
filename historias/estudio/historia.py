"""Etapas 1 e 2: escrever a história, avaliar com o Jev, reescrever até passar e só depois decupar em cenas.

Fluxo:
  1. ganchos: o modelo escreve 5, o Jev dá nota a cada um; se o melhor não passa, gera outros 5 vendo os anteriores.
  2. narrativa: a história inteira, em texto corrido, começando pelo gancho. Nada de cenas nesta etapa.
  3. laço: checagens em Python (grátis) -> Jev -> reescrita só do que falhou.
     Para em: tudo aprovado | teto de US$ do laço | 5 rodadas | 2 rodadas seguidas sem melhora.
  (o humanizer roda aqui, sobre o texto corrido: ver revisao.py)
  4. decupagem: o texto final é cortado em trechos (frases); o modelo agrupa trechos consecutivos em cenas
     conforme a história pede (umas vezes 7, outras 11) e escreve o que cada imagem mostra. Como ele só
     agrupa números de trechos, o texto aprovado não muda.
  5. conferência: o Jev checa se cada imagem mostra o que está sendo narrado; só as que falharem são refeitas.
"""
import difflib
import json
import math
import re
import unicodedata

from . import canais, clientes, config, custos, db
from .custos import OrcamentoEstourado

# O Jev aceita no máximo 10 níveis. Seis níveis descritos, convertidos para 0-10 pelo valor esperado
# das probabilidades (nível 0 = 0, nível 5 = 10), então notas intermediárias como 7,4 continuam possíveis.
ESCALA = ["péssimo: falha por completo", "fraco: funciona pouco", "mediano: funciona, sem destaque",
          "bom: funciona bem", "forte: acima da média do gênero", "excepcional: difícil de melhorar"]
MAX_NIVEIS_JEV = 10

SCHEMA_GANCHOS = {
    "type": "object", "additionalProperties": False, "required": ["modo", "ganchos"],
    "properties": {"modo": {"type": "string"}, "ganchos": {"type": "array", "items": {"type": "string"}}},
}

SCHEMA_NARRATIVA = {
    "type": "object", "additionalProperties": False, "required": ["titulo", "descricao_youtube", "narracao"],
    "properties": {"titulo": {"type": "string"}, "descricao_youtube": {"type": "string"},
                   "narracao": {"type": "string"}},
}

SCHEMA_DECUPAGEM = {
    "type": "object", "additionalProperties": False, "required": ["personagens", "ambientes", "cenas"],
    "properties": {
        "personagens": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "nome", "descricao_fixa"],
            "properties": {"id": {"type": "string"}, "nome": {"type": "string"},
                           "descricao_fixa": {"type": "string"}}}},
        "ambientes": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["id", "descricao_fixa"],
            "properties": {"id": {"type": "string"}, "descricao_fixa": {"type": "string"}}}},
        "cenas": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["de", "ate", "prompt_imagem", "pessoas", "ambiente", "tensao", "destaque"],
            "properties": {"de": {"type": "integer"}, "ate": {"type": "integer"},
                           "prompt_imagem": {"type": "string"},
                           "pessoas": {"type": "array", "items": {
                               "type": "object", "additionalProperties": False, "required": ["id", "onde", "faz"],
                               "properties": {"id": {"type": "string"}, "onde": {"type": "string"},
                                              "faz": {"type": "string"}}}},
                           "ambiente": {"type": "string"}, "tensao": {"type": "integer"},
                           "destaque": {"type": "string"}}}},
    },
}

SCHEMA_PROMPTS = {
    "type": "object", "additionalProperties": False, "required": ["cenas"],
    "properties": {"cenas": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["n", "prompt_imagem", "pessoas", "destaque"],
        "properties": {"n": {"type": "integer"}, "prompt_imagem": {"type": "string"},
                       "pessoas": {"type": "array", "items": {
                               "type": "object", "additionalProperties": False, "required": ["id", "onde", "faz"],
                               "properties": {"id": {"type": "string"}, "onde": {"type": "string"},
                                              "faz": {"type": "string"}}}},
                       "destaque": {"type": "string"}}}}},
}

# Folgas de tamanho e estimativas de custo (FOLGA_PALAVRAS, FOLGA_PALAVRAS_CENA, EST_TEXTO, EST_JEV): config.py,
# ajustáveis na aba Configurações.


# ---------------------------------------------------------------- utilidades

def contar_palavras(txt: str) -> int:
    return len(re.findall(r"[\wÀ-ÿ]+(?:['’-][\wÀ-ÿ]+)*", txt or ""))


def limites(canal: dict) -> dict:
    cfg = canal["config"]
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    wps = float(cfg.get("palavras_por_segundo") or 2.75)
    pmin = int(cfg.get("palavras_min") or fmt["palavras_min"])
    pmax = int(cfg.get("palavras_max") or fmt["palavras_max"])
    # duração da narração em segundos, quando preenchida, vale mais que as palavras
    if cfg.get("duracao_min_s"):
        pmin = round(float(cfg["duracao_min_s"]) * wps)
    if cfg.get("duracao_max_s"):
        pmax = round(float(cfg["duracao_max_s"]) * wps)
    pmax = max(pmax, pmin)
    cmin = int(cfg.get("imagens_min") or fmt["min_cenas"])
    cmax = max(int(cfg.get("imagens_max") or fmt["max_imagens"]), cmin)
    # poucas imagens para muita narração: cada cena precisa caber mais tempo (o gancho é curto, por isso cmax-1)
    max_cena_s = max(fmt["max_cena_s"], math.ceil(pmax / wps / max(cmax - 1, 1)))
    return {
        "palavras_min": pmin,
        "palavras_max": pmax,
        "cenas_min": cmin,
        "cenas_max": cmax,
        "max_cena_s": max_cena_s,
        "palavras_por_cena": int(math.floor(max_cena_s * wps)),
        "wps": wps,
        "max_personagens": int(cfg.get("max_personagens") or 2),
        "max_ambientes": int(cfg.get("max_ambientes") or 6),
    }


def frases(texto: str) -> list[str]:
    texto = re.sub(r"\s+", " ", texto or "").strip()
    return [f.strip() for f in re.split(r"(?<=[.!?…])\s+(?=[\"“'(]?[A-ZÀ-Ý0-9])", texto) if f.strip()]


def trechos(texto: str, max_palavras: int) -> list[str]:
    """Corta a narração em trechos que cabem numa imagem: uma frase por trecho; frase longa demais é
    dividida nas vírgulas (e, sem vírgula, ao meio). O texto dos trechos juntos é o texto original."""
    saida = []
    for f in frases(texto):
        if contar_palavras(f) <= max_palavras:
            saida.append(f)
            continue
        pedacos, atual = [], ""
        for parte in re.split(r"(?<=[,;:])\s+", f):
            candidato = f"{atual} {parte}".strip()
            if atual and contar_palavras(candidato) > max_palavras:
                pedacos.append(atual)
                atual = parte
            else:
                atual = candidato
        if atual:
            pedacos.append(atual)
        for p in pedacos:
            palavras = p.split(" ")
            while contar_palavras(" ".join(palavras)) > max_palavras and len(palavras) > 1:
                meio = len(palavras) // 2
                saida.append(" ".join(palavras[:meio]))
                palavras = palavras[meio:]
            saida.append(" ".join(palavras))
    return saida


def narracao_completa(h: dict) -> str:
    """Depois da decupagem, as cenas são a fonte (podem ter sido editadas na tela); antes, o texto corrido."""
    if h.get("cenas"):
        return " ".join(c["narracao"] for c in h["cenas"])
    return re.sub(r"\s+", " ", h.get("narracao", "")).strip()


MAX_REFERENCIAS = 2  # imagens de referência escolhidas à mão por cena
MAX_PESSOAS_CENA = 2  # com mais gente na mesma imagem o gerador mistura os rostos


def pessoas_cena(c: dict) -> list[dict]:
    """Quem aparece na imagem: [{id, onde, faz}]. Projetos antigos guardavam só o id, em "personagem"."""
    if isinstance(c.get("pessoas"), list):
        return [p for p in c["pessoas"] if isinstance(p, dict) and p.get("id")]
    ids = c.get("personagens") if isinstance(c.get("personagens"), list) else [c.get("personagem")]
    return [{"id": i, "onde": "", "faz": ""} for i in ids if i]


def personagens_cena(c: dict) -> list[str]:
    return [p["id"] for p in pessoas_cena(c)]


def descrever_pessoa(ficha: dict, p: dict, ref: int | None = None) -> str:
    """Uma linha por pessoa, no lugar dela: "In the driver's seat: <aparência da ficha>, <o que faz>".
    A aparência vem sempre da ficha, então a imagem e a referência enviada falam da mesma pessoa."""
    aparencia = ficha["descricao_fixa"].strip().rstrip(".")
    if ref:
        aparencia += f" (same face, hair and clothes as reference image {ref})"
    onde, faz = (p.get("onde") or "").strip().rstrip(":,."), (p.get("faz") or "").strip().rstrip(".")
    if not onde and not faz:  # cena antiga, sem posição: o prompt_imagem já cita a pessoa pelo nome
        return f"{ficha.get('nome') or ficha['id']} is {aparencia}"
    texto = f"{onde}: {aparencia}" if onde else aparencia
    texto = texto + (f", {faz}" if faz else "")
    return texto[0].upper() + texto[1:]


def texto_imagem(c: dict) -> str:
    """O que a imagem mostra, para comparar cenas: lugar + posição e ação de cada pessoa."""
    return " ".join([c.get("prompt_imagem") or ""] + [f"{p['onde']} {p['faz']}" for p in pessoas_cena(c)]).lower()


def ambientes_recorrentes(h: dict) -> set[str]:
    usos: dict[str, int] = {}
    for c in h.get("cenas", []):
        if c.get("ambiente") and not c.get("mesma_imagem_de"):  # cópia não gera imagem, não pede placa
            usos[c["ambiente"]] = usos.get(c["ambiente"], 0) + 1
    return {a for a, n in usos.items() if n >= 2}


def _impor_gancho(narracao: str, gancho: str) -> str:
    """A narração começa pelo gancho escolhido. Se o modelo parafraseou a primeira frase, volta o gancho."""
    narracao = re.sub(r"\s+", " ", narracao or "").strip()
    if not gancho or narracao.startswith(gancho):
        return narracao
    fs = frases(narracao)
    # O modelo às vezes parafraseia o gancho ou o quebra em duas ou três frases: troca o trecho mais parecido.
    melhor, n = 0.0, 0
    for k in range(1, min(3, len(fs)) + 1):
        r = difflib.SequenceMatcher(None, " ".join(fs[:k]).lower(), gancho.lower()).ratio()
        if r > melhor:
            melhor, n = r, k
    if melhor >= 0.6:
        return " ".join([gancho] + fs[n:])
    return f"{gancho} {narracao}"


def normalizar_narrativa(h: dict, gancho: str | None = None) -> dict:
    h = {"titulo": (h.get("titulo") or "").strip(), "descricao_youtube": (h.get("descricao_youtube") or "").strip(),
         "narracao": _impor_gancho(h.get("narracao", ""), gancho) if gancho else
         re.sub(r"\s+", " ", h.get("narracao", "")).strip()}
    fs = frases(h["narracao"])
    h["gancho"] = fs[0] if fs else ""
    return h


def normalizar_cenas(h: dict) -> dict:
    """Arruma numeração e ids das cenas e mantém narracao/gancho em sincronia com elas."""
    ids_p = {p["id"] for p in h.get("personagens", [])}
    ids_a = {a["id"] for a in h.get("ambientes", [])}
    for i, c in enumerate(h.get("cenas", []), 1):
        c["n"] = i
        c["narracao"] = re.sub(r"\s+", " ", c.get("narracao", "")).strip()
        c["tensao"] = max(1, min(5, int(c.get("tensao") or 2)))
        c["destaque"] = str(c.get("destaque") or "").strip()
        pessoas = []
        for p in pessoas_cena(c):
            if p["id"] in ids_p and p["id"] not in [x["id"] for x in pessoas]:
                pessoas.append({"id": p["id"], "onde": str(p.get("onde") or "").strip(),
                                "faz": str(p.get("faz") or "").strip()})
        c["pessoas"] = pessoas[:MAX_PESSOAS_CENA]
        c.pop("personagem", None)
        c.pop("personagens", None)
        if c.get("ambiente") not in ids_a:
            c["ambiente"] = ""
        validas = {f"personagem:{i}" for i in ids_p} | {f"ambiente:{i}" for i in ids_a}
        refs = []
        for r in c.get("referencias") or []:
            if r in validas and r not in refs:
                refs.append(r)
        c["referencias"] = refs[:MAX_REFERENCIAS]
    if h.get("cenas"):
        h["narracao"] = narracao_completa(h)
        fs = frases(h["narracao"])
        h["gancho"] = fs[0] if fs else h["cenas"][0]["narracao"]
    return h


def checar_narrativa(h: dict, canal: dict) -> tuple[list[str], dict]:
    """Checagens em Python sobre o texto corrido, custo zero."""
    lim = limites(canal)
    total = contar_palavras(h.get("narracao", ""))
    metricas = {"palavras": total, "duracao_estimada_s": round(total / lim["wps"], 1),
                "frases": len(frases(h.get("narracao", "")))}
    problemas = []
    if not total:
        return ["a narração veio vazia"], metricas
    # Folga antes de mandar reescrever: errar por poucas palavras não vale uma reescrita inteira.
    minimo = int(lim["palavras_min"] * (1 - config.FOLGA_PALAVRAS))
    maximo = int(round(lim["palavras_max"] * (1 + config.FOLGA_PALAVRAS)))
    if total < minimo:
        problemas.append(f"a narração tem {total} palavras; precisa de {lim['palavras_min']} a {lim['palavras_max']} "
                         f"(faltam {lim['palavras_min'] - total}; acrescente detalhes concretos no meio da história, "
                         "sem mexer no gancho nem no final)")
    if total > maximo:
        problemas.append(f"a narração tem {total} palavras; precisa de {lim['palavras_min']} a {lim['palavras_max']} "
                         f"(corte {total - lim['palavras_max']}; tire repetições e explicações, não o gancho nem o final)")
    return problemas, metricas


def checar(h: dict, canal: dict) -> tuple[list[str], dict]:
    """Checagens depois da decupagem (cenas e campos visuais)."""
    lim = limites(canal)
    problemas = []
    cenas = h.get("cenas") or []
    total = contar_palavras(narracao_completa(h))
    metricas = {"palavras": total, "cenas": len(cenas), "duracao_estimada_s": round(total / lim["wps"], 1),
                "palavras_por_cena": [contar_palavras(c["narracao"]) for c in cenas]}
    if not cenas:
        return ["a história não tem cenas"], metricas
    if len(cenas) < lim["cenas_min"] or len(cenas) > lim["cenas_max"]:
        problemas.append(f"a história tem {len(cenas)} cenas; precisa de {lim['cenas_min']} a {lim['cenas_max']}")
    for c, n in zip(cenas, metricas["palavras_por_cena"]):
        if n > lim["palavras_por_cena"] + config.FOLGA_PALAVRAS_CENA:
            problemas.append(f"a cena {c['n']} tem {n} palavras (~{n / lim['wps']:.1f}s); o máximo é "
                             f"{lim['palavras_por_cena']} palavras ({lim['max_cena_s']:.0f}s)")
        if not c.get("prompt_imagem", "").strip():
            problemas.append(f"a cena {c['n']} está sem prompt de imagem")
    if len(h.get("personagens") or []) > lim["max_personagens"]:
        problemas.append(f"há {len(h['personagens'])} personagens com ficha; o máximo é {lim['max_personagens']}")
    if len(h.get("ambientes") or []) > lim["max_ambientes"]:
        problemas.append(f"há {len(h['ambientes'])} ambientes; o máximo é {lim['max_ambientes']}")
    return problemas, metricas


def texto_para_juiz(h: dict, canal: dict, assunto: str) -> str:
    linhas = [f"Canal: {canal['nome']}. {canal['descricao']}", f"Assunto pedido: {assunto}",
              f"Título: {h.get('titulo', '')}"]
    if h.get("cenas"):
        linhas += ["", "Personagens (descrição visual fixa):"]
        linhas += [f"- {p['id']} ({p.get('nome', '')}): {p['descricao_fixa']}" for p in h.get("personagens", [])] or ["- nenhum"]
        linhas += ["", "Ambientes (descrição visual fixa):"]
        linhas += [f"- {a['id']}: {a['descricao_fixa']}" for a in h.get("ambientes", [])] or ["- nenhum"]
        linhas += ["", "Cenas: o trecho narrado e a imagem que aparece na tela enquanto ele é lido:"]
        for c in h["cenas"]:
            fichas = {p["id"]: p for p in h.get("personagens", [])}
            quem = [descrever_pessoa(fichas[p["id"]], p) for p in pessoas_cena(c) if p["id"] in fichas]
            linhas.append(f"[{c['n']}] Narração: {c['narracao']}\n    Imagem: {c['prompt_imagem']}"
                          f"{' ' + '. '.join(quem) + '.' if quem else ''}")
    linhas += ["", "Narração completa, como será ouvida:", narracao_completa(h)]
    return "\n".join(linhas)


def perguntas_jev(perguntas: dict) -> dict:
    out = {}
    for k, p in perguntas.items():
        if p["tipo"] == "score":
            criterios = p.get("criterios")
            if not criterios or not (2 <= len(criterios) <= MAX_NIVEIS_JEV):
                criterios = ESCALA
            out[k] = {"type": "score", "instructions": p["pergunta"], "criteria": criterios}
        else:
            q = {"type": "noul", "instructions": p["pergunta"]}
            if p.get("criterios"):
                q["criteria"] = p["criterios"]
            out[k] = q
    return out


def interpretar(respostas: dict, perguntas: dict) -> dict:
    """Converte as respostas do Jev em notas 0-10 e compara com as notas de corte."""
    por = {}
    for k, p in perguntas.items():
        r = respostas.get(k) or {}
        if p["tipo"] == "score":
            valor = clientes.nota_score(r, len(p.get("criterios") or ESCALA)) if r else 0.0
            nota10 = valor
        else:
            valor = float(r.get("noul", 0.0))
            nota10 = valor * 10
        por[k] = {"tipo": p["tipo"], "pergunta": p["pergunta"], "valor": round(valor, 3), "corte": p["corte"],
                  "passou": valor >= p["corte"], "trava": bool(p.get("trava")), "nota10": round(nota10, 2),
                  "confianca": r.get("confidence")}
    notas = [v["nota10"] for v in por.values()]
    return {
        "perguntas": por,
        "nota_geral": round(sum(notas) / len(notas), 2) if notas else 0.0,
        "passou": all(v["passou"] for v in por.values()),
        "trava_falhou": any(v["trava"] and not v["passou"] for v in por.values()),
        "falhas": [k for k, v in por.items() if not v["passou"]],
    }


# Perguntas que dependem das imagens: ficam para depois da decupagem.
PERGUNTAS_VISUAIS = {"prompts_batem"}


def perguntas_narrativa(canal: dict) -> dict:
    return {k: v for k, v in canais.perguntas_do_canal(canal["config"]).items() if k not in PERGUNTAS_VISUAIS}


def _media_custo(projeto_id, servico_modelo: str, padrao: float, etapa: str = "historia") -> float:
    r = db.um("SELECT AVG(valor_usd) AS m FROM custos WHERE projeto_id = ? AND modelo = ? AND etapa = ?",
              (projeto_id, servico_modelo, etapa))
    return float(r["m"]) if r and r["m"] else padrao


def teto_laco(canal: dict) -> float:
    """Teto do laço da história: o do canal, se definido; senão o do .env (LACO_TETO_USD)."""
    return float(canal["config"].get("teto_laco_usd") or config.LACO_TETO_USD)


def _checar_teto(projeto: dict, estimativa):
    # Conta só o que esta execução do laço gastou: tentativas anteriores do mesmo projeto não comem o teto novo.
    custos.verificar(projeto["id"], "historia", estimativa, projeto.get("_teto_laco", config.LACO_TETO_USD),
                     "Laço da história", base=projeto.get("_base_laco", 0.0))





# ---------------------------------------------------------------- prompts

def _usa_destaque(canal: dict) -> bool:
    from . import visual
    return visual.usa_destaque(canal)


def _vocabulario_proibido() -> list[str]:
    from . import revisao
    return revisao.vocabulario_ia()


def bloco_biblioteca(biblioteca: list[dict]) -> str:
    if not biblioteca:
        return ""
    return ("\n\nPersonagens e ambientes que já existem na biblioteca do canal (já têm imagem de referência). "
            "Reutilize o mesmo id e a mesma descricao_fixa só se a história tiver esse mesmo personagem ou lugar; "
            "senão crie um id novo, diferente destes:\n" +
            "\n".join(f"- {b['tipo']} {b['chave']}: {b['descricao_fixa']}" for b in biblioteca))


def sistema_escrita(canal: dict) -> str:
    """Igual em todas as chamadas de escrita do canal (ganchos, escrita, reescrita): o provedor guarda esse
    prefixo em cache e cobra bem menos por ele a partir da segunda chamada."""
    cfg = canal["config"]
    lim = limites(canal)
    regras = "\n".join(f"- {r}" for r in cfg.get("regras_escrita", []))
    estrutura = " -> ".join(cfg.get("estrutura", []))
    estilo_texto = ""
    if cfg.get("humanizer", True):
        vocab = ", ".join(_vocabulario_proibido()[:40])
        # Os padrões do humanizer-br que aparecem em ficção narrada, já na escrita: a história sai limpa na primeira
        # vez, em vez de passar por uma reescrita inteira depois (que muda fatos e perde as pistas da virada).
        estilo_texto = f"""
Escrita que não soa como texto gerado por IA (padrões do humanizer-br; cada um denuncia a máquina):
- "não X, mas Y", "não é só X, é Y", "mais do que X, é Y", ou o contraste partido em duas frases: afirme direto.
- frase de efeito solta que só repete o que veio antes; fileira de fragmentos curtos ("Silêncio. Escuro. Frio.");
  frase curta só quando traz um fato novo.
- aforismo de profundidade falsa ("no fundo", "a verdadeira questão", "o que realmente importa"): diga o fato.
- tríade forçada: três adjetivos, três exemplos ou três fatos curtos seguidos só para soar completo.
- várias frases seguidas começando pelo mesmo sujeito ("Ele... Ele... Ele..."): junte ou comece pela ação.
- travessão (— ou –) e hífen duplo: proibidos; use vírgula, ponto ou dois-pontos.
- gerúndio pendurado no fim da frase ("..., fazendo com que", "..., deixando", "..., revelando").
- comparação decorativa ("como quem...", "como se...") pendurada no fim da frase só para enfeitar; no máximo uma
  no texto inteiro, e só quando mostra algo que o ouvinte não veria de outro jeito.
- adjetivo vago de medo ("aterrorizante", "sinistro", "inexplicável", "arrepiante") no lugar do fato que assusta.
- palavras que a IA usa muito mais que gente: {vocab}.
- negação em par ("Não chamou meu nome. Não precisou.", "não teve mão, não teve força"): diga o que aconteceu.
- varie o comprimento das frases, como alguém contando em voz alta."""
    # Vem de uma história reescrita à mão: a versão da IA tinha "água de um poço que secou faz anos", morte por
    # afogamento em pé e um final-charada ("Eu não tenho pregos novos"); a versão boa dizia o motivo, mostrava a
    # família procurando e fechava amarrando tudo. As regras ficam genéricas para não puxar todo vídeo para poço e faca.
    sentido = """
Sentido antes de efeito:
- Cada detalhe estranho precisa caber na lógica da história e servir a ela. Nada de imagem que soa bonita mas não
  fecha (água escorrendo de um poço que já secou, alguém se afogar em pé). Se o ouvinte perguntaria "como assim?",
  corte. O impossível é um só: a regra sobrenatural da história, e o resto obedece ao mundo real.
- Diga o porquê das coisas com palavras simples: por que o narrador fez o que fez, por que achou que daria certo.
- Mostre o efeito no mundo ao redor: quem percebe, quem reage, o que o narrador esconde dos outros.
- O estranho nasce do que o narrador fez ou decidiu e se liga a isso por um elo concreto (um objeto, um lugar,
  um gesto que já apareceu antes).
- Tempo marcado como se fala ("ontem de manhã", "de madrugada"), sem lista de dias da semana.
- Sentimento dito direto quando importa ("tenho certeza", "não sei se vou escapar"), sem metáfora.
- O final amarra a história: o que foi aberto se resolve ou se explica, menos UMA ponta, que pode ficar aberta.
  Essa ponta nasce do que já foi contado. Nada de mistério novo na última frase, nem final-charada que o ouvinte
  precisa decifrar (um objeto que "não devia existir", uma frase solta que pede explicação).
- Quem narra em 1ª pessoa não conta nada depois de morrer. Se morre, a voz dele para antes, e o desfecho pode vir
  numa última frase de fato visto de fora."""
    return f"""Você é o roteirista do canal "{canal['nome']}". {canal['descricao']}

Você escreve a narração de um vídeo curto: um texto corrido, contado de uma vez, que precisa funcionar só no
áudio. Não divida em cenas nem pense em imagens agora; isso é feito depois, a partir do seu texto.
Idioma da narração, do título e da descrição: {canal['idioma']}.

Regras do canal:
{regras}

Estrutura da história: {estrutura}

Tamanho (conferido por programa; fora dele a história volta): entre {lim['palavras_min']} e {lim['palavras_max']} palavras,
cerca de {lim['palavras_min'] / lim['wps']:.0f} a {lim['palavras_max'] / lim['wps']:.0f} segundos de narração.
{sentido}
{estilo_texto}

Campos:
- narracao: a história inteira, texto corrido, pronto para ser lido em voz alta. Sem títulos, marcações ou rubricas.
  Termine na última frase da história: convite para seguir, curtir ou se inscrever o programa acrescenta depois.
- titulo: título do vídeo.
- descricao_youtube: 2 ou 3 frases para a descrição do vídeo, sem hashtags.

Responda só com o JSON."""


def sistema_decupagem(canal: dict) -> str:
    lim = limites(canal)
    orient = "vertical 9:16" if canal["formato"] == "short" else "horizontal 16:9"
    destaque = ('UM objeto pequeno e concreto que aparece na imagem e ficará em vermelho vivo, o resto em preto e '
                'branco (em inglês, ex.: "the ribbon on the backpack"). Escolha o que mais pesa na cena; não repita '
                'o mesmo objeto em cenas seguidas.') if _usa_destaque(canal) else 'deixe ""'
    est = canal["config"].get("estilo") or {}
    estilo = " / ".join(x for x in (est.get("nome"), est.get("prefixo"), est.get("sufixo")) if x) or "não definido"
    return f"""Você é o diretor de arte de um vídeo narrado do canal "{canal['nome']}". {canal['descricao']}
A narração já está pronta e aprovada; ela vem dividida em trechos numerados. Seu trabalho é decidir as imagens.

Como dividir:
- Agrupe trechos consecutivos em cenas. Cada cena é uma imagem na tela enquanto os trechos dela são lidos.
- Quem decide o número de cenas é a história: troque de imagem quando surge algo novo para ver (um lugar,
  um objeto, uma ação, uma reação). Trechos que continuam a mesma imagem ficam juntos.
- Entre {lim['cenas_min']} e {lim['cenas_max']} cenas. Nenhuma cena passa de {lim['max_cena_s']:.0f} segundos
  (cerca de {lim['palavras_por_cena']} palavras); a primeira cena é o gancho e deve ser curta.
- Use todos os trechos, na ordem, sem pular nem repetir: a cena 1 começa no trecho 1 e a última termina no último.
- Você não escreve nem muda o texto; só indica "de" e "ate" (números dos trechos, inclusive).

Como as imagens são feitas (escreva pensando nisto):
- O gerador é da família FLUX, da Black Forest Labs (no ComfyUI, o FLUX.1 Kontext, que trabalha a partir de
  imagens de referência). Primeiro o programa gera, só com texto, uma FICHA de cada personagem (retrato de corpo
  até os joelhos, de frente, fundo cinza liso, a partir da descricao_fixa) e uma PLACA de cada lugar que se
  repete (plano geral do lugar vazio, a partir da descricao_fixa do ambiente).
- Cada cena é gerada com essas imagens como referência, mais o texto que o programa monta nesta ordem:
  estilo do canal + prompt_imagem + "o lugar é o da foto de referência, visto do ângulo desta cena" + uma linha
  por pessoa ("onde: descricao_fixa, faz") + "cada pessoa mantém rosto, cabelo e roupa do retrato".
  Então não repita estilo, aparência das pessoas nem a descrição do lugar no prompt_imagem: isso já entra.
- O gerador não sabe o que é "imagem 1" ou "a referência": ele liga cada pessoa ao retrato dela pela descrição.
  Por isso as descricao_fixa precisam distinguir as pessoas à primeira vista.
- O gerador desenha tudo o que é citado, inclusive o que vem depois de "no" ou "without" ("no blood" põe sangue).
  Escreva só o que aparece: em vez de "no people", "the empty corridor"; em vez de "no light", "pitch dark".
- Ele só vê: som, cheiro, frio, pensamento e lembrança não viram imagem. Mostre a pista visível (o telefone com a
  tela acesa, a porta entreaberta, a respiração condensando no ar).
- Estilo visual do canal (o programa põe; não repita, mas também não contradiga, ex.: "photo" num estilo de
  quadrinho): {estilo}

Campos visuais: escreva prompt_imagem, descricao_fixa e destaque SEMPRE EM INGLÊS, mesmo com a narração em
português (vão para um gerador de imagens que entende mal português). Só os ids podem ficar em português.
- personagens[]: no máximo {lim['max_personagens']}, só quem aparece em alguma imagem. id e nome: um nome de
  verdade, nunca um papel. Quem conta a história em 1ª pessoa também é um personagem visual: se a história não dá
  nome a essa pessoa, invente um curto (ex.: Marcos, Helena); "narrador", "motorista", "homem", "mulher" e
  "protagonista" não valem. Só o fantasma ou a criatura pode ter título em vez de nome (ex.: Mulher de Branco).
  descricao_fixa: aparência completa e fixa, vale para todas as cenas e vira o retrato da ficha. Uma expressão
  que começa pelo tipo de pessoa (a man, a woman, an old man, a girl...) e segue com idade, corpo, rosto, cabelo
  (cor, comprimento, corte) e roupa com cores, 20 a 40 palavras, sem ponto final. Só o que a pessoa É e VESTE:
  objeto na mão (faca, lanterna), sangue, ferida nova ou expressão vão em cenas[].pessoas[].faz, senão aparecem
  em todas as cenas. Duas pessoas da mesma história diferem em pelo menos duas marcas fortes (cor do cabelo,
  idade, cor da roupa principal).
- ambientes[]: um para cada lugar diferente por onde a história passa (quarto, escada, corredor, cozinha, quintal,
  poço...), até {lim['max_ambientes']}. descricao_fixa: o lugar vazio com os elementos fixos que se repetem
  (arquitetura, móveis, materiais e cores, a luz típica), 20 a 40 palavras, sem gente e sem o acontecimento de
  nenhuma cena (a porta que se abre sozinha vai no prompt_imagem da cena, não aqui).
  Use os lugares que a narração cita ou sugere: se a pessoa desce a escada, a escada vira imagem; se joga uma chave
  no quintal, o quintal aparece. Variar os lugares prende mais do que repetir sempre os mesmos dois.
  Se duas cenas seguidas mostrariam a mesma coisa, junte as duas numa cena só. Só abra uma cena nova quando a
  narração pedir algo novo para ver; cada imagem deve ser diferente das outras.
- cenas[].prompt_imagem: composição {orient}, 25 a 60 palavras, frases curtas e concretas, nesta ordem:
  1. plano e ângulo de câmera primeiro (extreme close-up, close-up, medium shot, wide shot; eye level, low angle,
     high angle, over the shoulder, from the doorway...). A placa já mostra o lugar em plano geral, então cada
     cena escolhe o seu enquadramento, e cenas seguidas no mesmo lugar mudam de plano ou de ângulo;
  2. o que está acontecendo e o objeto principal, com cor, material e posição na imagem (left, right, center,
     foreground, background);
  3. as partes do lugar que aparecem neste enquadramento;
  4. a luz: de onde vem, cor e força (a single bare bulb overhead, cold moonlight through the window).
  Mostre o que está sendo narrado naquele momento. Em terror, prefira o detalhe concreto (o objeto fora do lugar,
  a marca na parede, a sombra no vão da porta) ao monstro inteiro. Texto escrito dentro da imagem só quando for
  essencial, e entre aspas, exatamente como deve sair. Sem metáfora, sem adjetivo vago ("eerie", "creepy",
  "mysterious"): diga o que se vê. As pessoas NÃO entram no prompt_imagem, nem por papel ("the driver",
  "the woman"), nem por pronome: elas vão em pessoas[], e o programa monta uma linha para cada uma.
- cenas[].pessoas: quem aparece na imagem, ou []. Prefira UMA pessoa por imagem; duas só quando a narração
  pede as duas juntas; nunca mais de {MAX_PESSOAS_CENA}. Toda pessoa que aparece tem ficha em personagens[].
  Para cada uma, em inglês:
  - id: o id da ficha;
  - onde: a posição exata dela nesta imagem (lado, primeiro plano ou fundo, em que assento ou junto de quê, de
    corpo inteiro, da cintura para cima ou só o rosto). Com duas pessoas, uma de cada lado;
  - faz: pose, ação e expressão naquele momento, concretas (o que as mãos fazem e seguram, para onde olha, o que
    o rosto mostra, o que a roupa ou o corpo têm de estranho agora). Não repita a aparência da ficha: o programa
    junta. Uma pose diferente do retrato (de pé, de frente) deixa a cena viva.
  Vulto, sombra ou silhueta sem rosto não é pessoa: vai no prompt_imagem.
- cenas[].ambiente: o id do lugar ONDE ESTA IMAGEM acontece, conferido com o prompt_imagem (se a imagem mostra a
  cama, é o quarto, mesmo que a narração cite a cozinha). A imagem de referência desse lugar vai junto para o
  gerador, então um lugar errado estraga a cena. "" se não for nenhum dos ambientes.
- cenas[].tensao: 1 (calmo) a 5 (pico).
- cenas[].destaque: {destaque}

Responda só com o JSON."""


def _json_narrativa(h: dict) -> str:
    return json.dumps({k: h.get(k, "") for k in ("titulo", "descricao_youtube", "narracao")},
                      ensure_ascii=False, separators=(",", ":"))


# ---------------------------------------------------------------- ganchos

def _modo(canal: dict, nome: str | None) -> dict | None:
    for m in canal["config"].get("modos_narrativos") or []:
        if nome and m["nome"].lower() == nome.strip().lower():
            return m
    return None


def _ultimo_modo(projeto: dict) -> str | None:
    r = db.um("SELECT modo FROM projetos WHERE canal_id = ? AND id < ? AND modo IS NOT NULL ORDER BY id DESC LIMIT 1",
              (projeto["canal_id"], projeto["id"]))
    return r["modo"] if r else None


def dica_modo(projeto: dict, canal: dict) -> str:
    """Modo narrativo escolhido junto com os ganchos; o roteirista segue esse modo na história e nas reescritas."""
    m = _modo(canal, projeto.get("modo"))
    return f"\n\nModo narrativo deste vídeo: {m['nome']} ({m['regra']}). Siga-o do começo ao fim." if m else ""


def mensagens_gancho(assunto: str, canal: dict, ultimo: str | None = None, historico: str = "",
                     evitar_modos: tuple = ()) -> list[dict]:
    """Pedido dos 5 ganchos (e do modo narrativo). Separado para a tela do canal mostrar o prompt exato.
    evitar_modos: modos de tentativas reprovadas na trava; saem da lista enquanto sobrar algum."""
    modos = canal["config"].get("modos_narrativos") or []
    evitar = {x.lower() for x in evitar_modos}
    modos = [m for m in modos if m["nome"].lower() not in evitar] or modos
    bloco_modo, campo_modo = "", "\"modo\": \"\""
    if modos:
        bloco_modo = ("\n\nPrimeiro escolha o modo narrativo que melhor serve ao assunto"
                      f"{' (evite repetir ' + ultimo + ', usado no vídeo anterior, se outro servir)' if ultimo else ''}; "
                      "os 5 ganchos são escritos nesse modo:\n" + "\n".join(f"- {m['nome']}: {m['regra']}" for m in modos))
        campo_modo = "\"modo\": nome exato do modo escolhido"
    return [
        {"role": "system", "content": sistema_escrita(canal)},
        {"role": "user", "content": f"Assunto: {assunto}{bloco_modo}\n\nEscreva 5 ganchos diferentes para abrir essa "
         "história. O gancho é a primeira frase da narração, entre 120 e 150 caracteres: já carrega a premissa (quem, "
         "onde e o que há de impossível) e faz quem está rolando o feed parar nos 3 primeiros segundos, sem revelar o "
         f"final. Cada um com uma abordagem diferente.{historico}\n\nResponda com {{{campo_modo}, \"ganchos\": [5 textos]}}."},
    ]


def _pedido_outra_vibe(reprovadas: list[dict]) -> str:
    """Tentativas inteiras reprovadas na trava: a próxima começa de outro jeito, não remenda a anterior."""
    if not reprovadas:
        return ""
    linhas = [f"- {'modo ' + r['modo'] + ', ' if r.get('modo') else ''}gancho \"{r['gancho']}\" "
              f"(reprovada em: {'; '.join(r['falhas'].values()) or 'trava do juiz'})" for r in reprovadas]
    return ("\n\nEste assunto já foi escrito e a história foi reprovada pelo juiz. Recomece com outra vibe: outro modo "
            "narrativo, outro ponto de vista, outro tom e outra situação de abertura, sem reaproveitar estes começos, "
            "e cuide do que reprovou:\n" + "\n".join(linhas))


def escolher_gancho(projeto: dict, canal: dict, reprovadas: list[dict] | None = None) -> tuple[str, float, list]:
    """5 ganchos por rodada (com o modo narrativo), notas do Jev, até passar do corte ou acabar as rodadas.
    O melhor fica fixo e define o modo da história. reprovadas: tentativas anteriores que falharam na trava."""
    perguntas = canais.perguntas_do_canal(canal["config"])
    base = perguntas["gancho"]
    corte = base["corte"]
    reprovadas = reprovadas or []
    ultimo = _ultimo_modo(projeto)
    evitar = tuple(r["modo"] for r in reprovadas if r.get("modo"))
    vibe = _pedido_outra_vibe(reprovadas)
    anteriores: list[dict] = []
    melhor = ("", -1.0, None)
    for rodada in range(1, config.GANCHO_MAX_RODADAS + 1):
        _checar_teto(projeto, _media_custo(projeto["id"], config.MODELO_TEXTO, config.EST_TEXTO) +
                     _media_custo(projeto["id"], config.MODELO_JUIZ, config.EST_JEV))
        hist = vibe
        if anteriores:
            hist += "\n\nGanchos anteriores e a nota que tiveram (0-10, corte " + f"{corte}" + "). Não repita; faça melhor:\n" + \
                    "\n".join(f"- ({a['nota']:.1f}) {a['texto']}" for a in anteriores)
        msgs = mensagens_gancho(projeto["assunto"], canal, ultimo, hist, evitar)
        resp, _ = clientes.chat("ganchos", msgs, SCHEMA_GANCHOS, projeto_id=projeto["id"], etapa="historia",
                                temperatura=1.0, max_tokens=1200, contexto={"assunto": projeto["assunto"]})
        ganchos = [g.strip() for g in resp.get("ganchos", []) if g.strip()][:5]
        if not ganchos:
            continue
        m = _modo(canal, str(resp.get("modo") or "").strip(" .*"))
        state = f"Canal: {canal['nome']}. {canal['descricao']}\nAssunto do vídeo: {projeto['assunto']}\n" \
                f"Formato: vídeo curto vertical, narração em {canal['idioma']}. O gancho é a primeira frase que o narrador lê."
        qs = {f"g{i}": {"type": "score", "instructions": f"{base['pergunta']}\nGancho: \"{g}\"", "criteria": ESCALA}
              for i, g in enumerate(ganchos, 1)}
        respostas, _ = clientes.decidir(state, qs, projeto_id=projeto["id"], etapa="historia",
                                        contexto={"tipo": "ganchos"})
        notas = [(g, clientes.nota_score(respostas.get(f"g{i}", {}), len(ESCALA))) for i, g in enumerate(ganchos, 1)]
        for g, n in notas:
            anteriores.append({"texto": g, "nota": n})
        top = max(notas, key=lambda x: x[1])
        db.evento(projeto["id"], f"Ganchos rodada {rodada}{' (' + m['nome'] + ')' if m else ''}: melhor nota "
                                 f"{top[1]:.1f} (corte {corte}) · \"{top[0]}\"")
        if top[1] > melhor[1]:
            melhor = (top[0], top[1], m["nome"] if m else None)
        if top[1] >= corte:
            break
    if not melhor[0]:
        raise clientes.ErroAPI("O modelo não devolveu ganchos")
    projeto["modo"] = melhor[2]
    db.executar("UPDATE projetos SET modo = ? WHERE id = ?", (projeto["modo"], projeto["id"]))
    return melhor[0], melhor[1], anteriores


# ---------------------------------------------------------------- escrita e reescrita (texto corrido)

def _pedido_continuacao(canal: dict, gancho: str) -> str:
    """Com gancho fixo, o modelo escreve só o que vem depois dele: pedir a frase de volta faz o modelo reescrevê-la
    com outras palavras, e a história sai com a abertura repetida."""
    lim, g = limites(canal), contar_palavras(gancho)
    return (f"Gancho, já escrito, lido primeiro pelo narrador: \"{gancho}\"\n"
            "Ele não entra na sua resposta. No campo narracao, escreva só o que vem depois dele, continuando do ponto "
            "em que ele para, sem repetir nem resumir o que ele já disse. Com o gancho, a história precisa ter entre "
            f"{lim['palavras_min']} e {lim['palavras_max']} palavras; a sua parte, entre {lim['palavras_min'] - g} e "
            f"{lim['palavras_max'] - g} palavras.")


def _com_gancho(h: dict, gancho: str) -> dict:
    return normalizar_narrativa({**h, "narracao": f"{gancho} {h.get('narracao', '')}"}, gancho)


def _sem_gancho(h: dict, gancho: str) -> dict:
    narr = h["narracao"]
    return {**h, "narracao": narr[len(gancho):].strip() if narr.startswith(gancho) else narr}


def escrever(projeto: dict, canal: dict, gancho: str, fraquezas: list[str] | None = None) -> dict:
    """Escreve a história inteira do zero. fraquezas: onde as versões descartadas falharam no Jev (só o critério,
    não o texto), para esta sair diferente em vez de remendar a anterior."""
    _checar_teto(projeto, _media_custo(projeto["id"], config.MODELO_TEXTO, config.EST_TEXTO))
    fixo = projeto.get("gancho_fixo")
    pedido = (_pedido_continuacao(canal, gancho) if fixo else
              f"A narração começa exatamente com este gancho, não mude: \"{gancho}\"")
    antes = ""
    if fraquezas:
        antes = ("\n\nOutras versões desta história já foram escritas e descartadas. Escreva uma nova, com outro "
                 "desenvolvimento, e cuide em especial do que elas não cumpriram:\n" + "\n".join(f"- {f}" for f in fraquezas))
    msgs = [
        {"role": "system", "content": sistema_escrita(canal)},
        {"role": "user", "content": f"Assunto: {projeto['assunto']}\n\n{pedido}{dica_modo(projeto, canal)}{antes}\n\n"
                                    "Escreva a história em JSON."},
    ]
    h, _ = clientes.chat("narrativa", msgs, SCHEMA_NARRATIVA, projeto_id=projeto["id"], etapa="historia",
                         temperatura=0.9, contexto={"assunto": projeto["assunto"], "gancho": gancho})
    return _com_gancho(h, gancho) if fixo else normalizar_narrativa(h, gancho)


def reescrever(projeto: dict, canal: dict, h: dict, problemas: list[str], avaliacao: dict | None) -> dict:
    _checar_teto(projeto, _media_custo(projeto["id"], config.MODELO_TEXTO, config.EST_TEXTO))
    itens = [f"- {p}" for p in problemas]
    gancho_livre = False
    if avaliacao:
        for k in avaliacao["falhas"]:
            v = avaliacao["perguntas"][k]
            nota = f"{v['valor']:.1f}/10" if v["tipo"] == "score" else f"{v['valor']:.2f} de probabilidade"
            corte = f"{v['corte']}/10" if v["tipo"] == "score" else f"{v['corte']}"
            itens.append(f"- \"{v['pergunta']}\" recebeu {nota}; precisa de {corte}")
            gancho_livre = gancho_livre or k == "gancho"
    fixo = projeto.get("gancho_fixo")
    gancho_livre = gancho_livre and not fixo
    gancho = fixo or h["gancho"]
    trava = "" if gancho_livre else f"\nMantenha o começo exatamente igual: \"{gancho}\"."
    versao = h
    if fixo:
        trava = "\n" + _pedido_continuacao(canal, fixo)
        versao = _sem_gancho(h, fixo)
    trava += dica_modo(projeto, canal)
    msgs = [
        {"role": "system", "content": sistema_escrita(canal)},
        {"role": "user", "content": f"Assunto: {projeto['assunto']}\n\nVersão atual"
                                    f"{' (só a parte depois do gancho)' if fixo else ''}:\n{_json_narrativa(versao)}\n\n"
                                    f"O que falhou na avaliação:\n" + "\n".join(itens) +
                                    "\n\nReescreva só o necessário para corrigir esses pontos e mantenha o que já está bom."
                                    f"{trava}\nDevolva a história completa em JSON."},
    ]
    nova, _ = clientes.chat("reescrita", msgs, SCHEMA_NARRATIVA, projeto_id=projeto["id"], etapa="historia",
                            contexto={"historia": h, "problemas": problemas})
    if fixo:
        return _com_gancho(nova, fixo)
    return normalizar_narrativa(nova, None if gancho_livre else gancho)


def avaliar(projeto: dict, canal: dict, h: dict, perguntas: dict | None = None, etapa: str = "historia") -> dict:
    perguntas = perguntas or perguntas_narrativa(canal)
    if projeto.get("gancho_fixo"):
        perguntas = {k: v for k, v in perguntas.items() if k != "gancho"}
    if etapa == "historia":
        _checar_teto(projeto, _media_custo(projeto["id"], config.MODELO_JUIZ, config.EST_JEV))
    state = texto_para_juiz(h, canal, projeto["assunto"])
    respostas, _ = clientes.decidir(state, perguntas_jev(perguntas), projeto_id=projeto["id"], etapa=etapa,
                                    contexto={"tipo": "historia", "historia": h})
    return interpretar(respostas, perguntas)


# ---------------------------------------------------------------- decupagem (texto pronto -> cenas)

def _validar_grupos(grupos: list[dict], n_trechos: int) -> list[str]:
    erros = []
    esperado = 1
    for i, g in enumerate(grupos, 1):
        de, ate = int(g.get("de", 0)), int(g.get("ate", 0))
        if de != esperado:
            erros.append(f"a cena {i} começa no trecho {de}, mas deveria começar no {esperado}")
        if ate < de:
            erros.append(f"a cena {i} termina ({ate}) antes de começar ({de})")
        esperado = max(ate, de) + 1
    if esperado != n_trechos + 1:
        erros.append(f"as cenas terminam no trecho {esperado - 1}, mas o último é o {n_trechos}")
    return erros


def _consertar_grupos(grupos: list[dict], n_trechos: int) -> list[dict]:
    """Último recurso: ordena, fecha buracos e sobreposições para cobrir 1..n na ordem."""
    grupos = sorted([g for g in grupos if int(g.get("ate", 0)) >= 1 and int(g.get("de", 0)) <= n_trechos],
                    key=lambda g: int(g.get("de", 0)))
    saida, proximo = [], 1
    for g in grupos:
        ate = min(max(int(g["ate"]), proximo), n_trechos)
        if ate < proximo:
            continue
        saida.append({**g, "de": proximo, "ate": ate})
        proximo = ate + 1
    if saida and proximo <= n_trechos:
        saida[-1]["ate"] = n_trechos
    return saida


def _dividir_longas(cenas: list[dict], trs: list[str], lim: dict) -> list[dict]:
    """Cena acima do tempo máximo com mais de um trecho vira duas (a segunda repete a imagem, com zoom diferente)."""
    saida = []
    for c in cenas:
        partes = [(c["de"], c["ate"])]
        while True:
            novas = []
            for de, ate in partes:
                palavras = contar_palavras(" ".join(trs[de - 1:ate]))
                if palavras > lim["palavras_por_cena"] + config.FOLGA_PALAVRAS_CENA and ate > de:
                    meio = (de + ate) // 2
                    novas += [(de, meio), (meio + 1, ate)]
                else:
                    novas.append((de, ate))
            if novas == partes:
                break
            partes = novas
        for de, ate in partes:
            saida.append({**c, "de": de, "ate": ate})
    return saida


def _parecidos(a: dict, b: dict) -> bool:
    return difflib.SequenceMatcher(None, texto_imagem(a), texto_imagem(b)).ratio() >= 0.8


def _juntar(a: dict, b: dict) -> dict:
    """Cena a absorve os trechos da b (fica a imagem da a; a tensão é a maior das duas)."""
    return {**a, "ate": b["ate"], "tensao": max(a.get("tensao", 2), b.get("tensao", 2))}


def _juntar_cenas(grupos: list[dict], trs: list[str], lim: dict) -> list[dict]:
    """Junta cenas seguidas que mostram a mesma coisa (se couberem no tempo de uma cena) e, se ainda passar
    do máximo de imagens, junta os pares vizinhos mais curtos até caber. A cena 1 (gancho) fica sozinha."""
    def palavras(g):
        return contar_palavras(" ".join(trs[g["de"] - 1:g["ate"]]))
    cabe = lim["palavras_por_cena"] + config.FOLGA_PALAVRAS_CENA
    saida = []
    for g in grupos:
        if len(saida) > 1 and _parecidos(saida[-1], g) and palavras(saida[-1]) + palavras(g) <= cabe:
            saida[-1] = _juntar(saida[-1], g)
        else:
            saida.append(g)
    while len(saida) > lim["cenas_max"] and len(saida) > 2:
        i = min(range(1, len(saida) - 1), key=lambda k: palavras(saida[k]) + palavras(saida[k + 1]))
        saida[i:i + 2] = [_juntar(saida[i], saida[i + 1])]
    return saida


PAPEIS = {"narrador", "narradora", "narrator", "protagonista", "protagonist", "motorista", "driver", "homem", "mulher",
          "man", "woman", "personagem", "character", "eu", "ele", "ela", "pessoa", "person", "vitima", "victim"}


def papel_como_nome(p: dict) -> bool:
    """O nome (ou o id) é só um papel na história; o gerador de imagens não saberia quem é."""
    for campo in (p.get("nome"), p.get("id")):
        chave = re.sub(r"[^a-z]+", " ", unicodedata.normalize("NFKD", str(campo or "").lower())
                       .encode("ascii", "ignore").decode()).strip()
        if chave in PAPEIS or chave.split(" ")[0] in ("narrador", "narradora", "narrator"):
            return True
    return False


def _erros_pessoas(resp: dict) -> list[str]:
    ids = {p["id"] for p in resp.get("personagens") or []}
    erros = [f"o personagem '{p.get('nome') or p['id']}' tem um papel no lugar do nome; dê um nome próprio "
             f"(id e nome), como Marcos ou Helena" for p in resp.get("personagens") or [] if papel_como_nome(p)]
    for i, c in enumerate(resp.get("cenas") or [], 1):
        pessoas = pessoas_cena(c)
        if len(pessoas) > MAX_PESSOAS_CENA:
            erros.append(f"a cena {i} tem {len(pessoas)} pessoas; o máximo é {MAX_PESSOAS_CENA}")
        sem_ficha = [p["id"] for p in pessoas if p["id"] not in ids]
        if sem_ficha:
            erros.append(f"a cena {i} tem pessoa sem ficha em personagens[]: {', '.join(sem_ficha)}")
        incompletas = [p["id"] for p in pessoas if not (p.get("onde") or "").strip() or not (p.get("faz") or "").strip()]
        if incompletas:
            erros.append(f"na cena {i}, falta onde ou faz de: {', '.join(incompletas)}")
    return erros


def decupar(projeto: dict, canal: dict, h: dict, biblioteca: list[dict] | None = None) -> dict:
    """Divide a narração aprovada em cenas e escreve os campos visuais. O texto não muda."""
    pid = projeto["id"]
    lim = limites(canal)
    trs = trechos(h["narracao"], lim["palavras_por_cena"])
    lista = "\n".join(f"[{i}] ({contar_palavras(t) / lim['wps']:.1f}s) {t}" for i, t in enumerate(trs, 1))
    msgs = [
        {"role": "system", "content": sistema_decupagem(canal)},
        {"role": "user", "content": f"Título: {h.get('titulo', '')}\n\nNarração em {len(trs)} trechos:\n{lista}"
                                    f"{bloco_biblioteca(biblioteca or [])}\n\nDivida em cenas e responda em JSON."},
    ]
    resp, _ = clientes.chat("decupagem", msgs, SCHEMA_DECUPAGEM, projeto_id=pid, etapa="decupagem", temperatura=0.6,
                            contexto={"trechos": trs})
    erros = _validar_grupos(resp.get("cenas") or [], len(trs)) + _erros_pessoas(resp)
    if erros:
        db.evento(pid, f"Decupagem com erro ({'; '.join(erros[:3])}); pedindo de novo.")
        msgs += [{"role": "assistant", "content": json.dumps(resp, ensure_ascii=False)},
                 {"role": "user", "content": "Corrija: " + "; ".join(erros) + f". Use todos os {len(trs)} trechos, na "
                                             "ordem, sem pular nem repetir. Personagens com nome próprio, nunca papel. JSON completo."}]
        resp, _ = clientes.chat("decupagem", msgs, SCHEMA_DECUPAGEM, projeto_id=pid, etapa="decupagem",
                                temperatura=0.3, contexto={"trechos": trs})
        if _validar_grupos(resp.get("cenas") or [], len(trs)):
            db.evento(pid, "A decupagem ainda veio com buracos; o programa ajustou os limites das cenas.", "aviso")
    grupos = _dividir_longas(_consertar_grupos(resp.get("cenas") or [], len(trs)), trs, lim)
    grupos = _juntar_cenas(grupos, trs, lim)
    if not grupos:
        raise clientes.ErroAPI("A decupagem não devolveu cenas")
    novo = {**h, "personagens": resp.get("personagens") or [], "ambientes": resp.get("ambientes") or [],
            "cenas": [{"narracao": " ".join(trs[g["de"] - 1:g["ate"]]), "prompt_imagem": g.get("prompt_imagem", ""),
                       "pessoas": pessoas_cena(g), "ambiente": g.get("ambiente", ""),
                       "tensao": g.get("tensao", 2), "destaque": g.get("destaque", ""),
                       "trechos": [g["de"], g["ate"]]} for g in grupos]}
    novo = normalizar_cenas(novo)
    db.evento(pid, f"Decupagem: {len(trs)} trechos em {len(novo['cenas'])} cenas.")
    return conferir_imagens(projeto, canal, novo)


def conferir_imagens(projeto: dict, canal: dict, h: dict) -> dict:
    """O Jev confere cena por cena se a imagem mostra o que está sendo narrado; só as que falham são refeitas
    (uma vez). O texto não muda."""
    pid = projeto["id"]
    base = canais.perguntas_do_canal(canal["config"])["prompts_batem"]
    perguntas = {f"cena_{c['n']}": {"tipo": "noul", "corte": base["corte"], "trava": False,
                                    "pergunta": f"A imagem da cena {c['n']} mostra o que está sendo narrado no trecho "
                                                f"da cena {c['n']}?"} for c in h["cenas"]}
    state = texto_para_juiz(h, canal, projeto["assunto"])
    jev = perguntas_jev(perguntas)
    # Em qual lugar cada imagem acontece (escolha entre os ambientes): corrige o id quando o modelo erra,
    # porque a placa de referência do lugar vai junto para o gerador.
    lugares = {a["id"]: a["descricao_fixa"] for a in h.get("ambientes", [])}
    if len(lugares) >= 2:
        opcoes = {**lugares, "nenhum": "nenhum destes lugares (close de objeto, tela, rosto ou outro lugar)"}
        for c in h["cenas"]:
            jev[f"lugar_{c['n']}"] = {"type": "choice", "criteria": opcoes,
                                      "instructions": f"Em qual lugar acontece a imagem da cena {c['n']}? "
                                                      f"Imagem: {c['prompt_imagem']}"}
    respostas, _ = clientes.decidir(state, jev, projeto_id=pid, etapa="decupagem", contexto={"tipo": "imagens"})
    aval = interpretar(respostas, perguntas)
    trocas = []
    for c in h["cenas"]:
        r = respostas.get(f"lugar_{c['n']}") or {}
        escolha = r.get("choice")
        if escolha and (r.get("confidence") or 0) >= 0.6:
            novo_lugar = "" if escolha == "nenhum" else escolha
            if novo_lugar != c.get("ambiente", "") and (novo_lugar in lugares or novo_lugar == ""):
                trocas.append(f"cena {c['n']}: {c.get('ambiente') or '-'} -> {novo_lugar or '-'}")
                c["ambiente"] = novo_lugar
    if trocas:
        db.evento(pid, "Lugar corrigido pelo Jev: " + "; ".join(trocas) + ".")
    ruins = {int(k.split("_")[1]): "não mostra o que está sendo narrado" for k in aval["falhas"]}
    # Cenas seguidas que mostram a mesma coisa (checagem em Python, custo zero): em vez de gerar outra imagem,
    # a cena reaproveita a da anterior com outro movimento de câmera (ver efeitos/detalhe.py). Mais barato e
    # a imagem não "muda de cara" no meio do momento.
    reaproveitadas = []
    for ant, c in zip(h["cenas"], h["cenas"][1:]):
        c.pop("mesma_imagem_de", None)
        # No máximo duas cenas por imagem: se a anterior já é cópia, esta ganha imagem própria.
        if _parecidos(ant, c) and c["n"] not in ruins and ant["n"] not in ruins and not ant.get("mesma_imagem_de"):
            c["mesma_imagem_de"] = ant["n"]
            c["pessoas"], c["ambiente"] = [dict(p) for p in pessoas_cena(ant)], ant.get("ambiente", "")
            c["prompt_imagem"], c["destaque"] = ant["prompt_imagem"], ant.get("destaque", "")
            reaproveitadas.append(f"{c['n']} (da {c['mesma_imagem_de']})")
        elif c.get("destaque") and c["destaque"].lower() == (ant.get("destaque") or "").lower():
            ruins.setdefault(c["n"], f"repete o destaque vermelho da cena {ant['n']}; escolha outro objeto")
    if reaproveitadas:
        db.evento(pid, "Mesma imagem com outro movimento de câmera: cenas " + ", ".join(reaproveitadas) + ".")
    h["conferencia_imagens"] = {k: v["valor"] for k, v in aval["perguntas"].items()}
    if not ruins:
        db.evento(pid, "Todas as imagens batem com a narração.")
        return h
    db.evento(pid, "Imagens para refazer: " + "; ".join(f"cena {n} ({m})" for n, m in sorted(ruins.items())) + ".")
    alvo = [{"n": c["n"], "narracao": c["narracao"], "prompt_imagem_atual": c["prompt_imagem"],
             "destaque_atual": c.get("destaque", ""), "ambiente": c.get("ambiente", ""),
             "pessoas_atuais": pessoas_cena(c),
             "problema": ruins[c["n"]]}
            for c in h["cenas"] if c["n"] in ruins]
    msgs = [{"role": "system", "content": sistema_decupagem(canal)},
            {"role": "user", "content": "Refaça o prompt_imagem (e o destaque) destas cenas, corrigindo o problema de cada "
                                        "uma, em inglês, mantendo o lugar indicado e as mesmas pessoas (ajuste onde e faz se "
                                        "for preciso):\n" +
                                        json.dumps(alvo, ensure_ascii=False) +
                                        "\nResponda com {\"cenas\": [{\"n\", \"prompt_imagem\", \"pessoas\", \"destaque\"}]}."}]
    resp, _ = clientes.chat("prompts_cena", msgs, SCHEMA_PROMPTS, projeto_id=pid, etapa="decupagem", temperatura=0.5,
                            contexto={"alvo": alvo})
    novos = {c["n"]: c for c in resp.get("cenas") or []}
    for c in h["cenas"]:
        if c["n"] in novos and novos[c["n"]].get("prompt_imagem"):
            c["prompt_imagem"] = novos[c["n"]]["prompt_imagem"]
            c["destaque"] = novos[c["n"]].get("destaque", c.get("destaque", ""))
            # Só aceita as pessoas novas se forem as mesmas da cena (muda posição e ação, não quem está).
            novas = [p for p in pessoas_cena(novos[c["n"]]) if p.get("onde") and p.get("faz")]
            if sorted(p["id"] for p in novas) == sorted(personagens_cena(c)):
                c["pessoas"] = novas
    normalizar_cenas(h)
    return h


# ---------------------------------------------------------------- versões

def salvar_versao(projeto: dict, h: dict, origem: str, checagem: dict, avaliacao: dict | None) -> int:
    n = (db.um("SELECT COALESCE(MAX(versao), 0) AS v FROM historias WHERE projeto_id = ?", (projeto["id"],))["v"] or 0) + 1
    passou = None if avaliacao is None else int(avaliacao["passou"] and not checagem["problemas"])
    if checagem["problemas"]:
        passou = 0
    db.executar("INSERT INTO historias (projeto_id, versao, origem, historia_json, checagem_json, avaliacao_json, "
                "nota_geral, passou, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (projeto["id"], n, origem, json.dumps(h, ensure_ascii=False), json.dumps(checagem, ensure_ascii=False),
                 json.dumps(avaliacao, ensure_ascii=False) if avaliacao else None,
                 avaliacao["nota_geral"] if avaliacao else None, passou, db.agora()))
    pasta = config.BASE / projeto["pasta"]
    (pasta / f"historia_v{n}.json").write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
    if avaliacao:
        with open(pasta / "avaliacoes.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"versao": n, "origem": origem, "nota_geral": avaliacao["nota_geral"],
                                "passou": avaliacao["passou"],
                                "notas": {k: v["valor"] for k, v in avaliacao["perguntas"].items()}},
                               ensure_ascii=False) + "\n")
    from . import registro
    registro.escrever(projeto["id"], "JEV", f"versão {n} ({origem}) · passou: {bool(passou)} · "
                                            f"nota geral: {avaliacao['nota_geral'] if avaliacao else '-'}",
                      dados={"metricas": checagem.get("metricas"), "problemas": checagem.get("problemas"),
                             "notas": {k: {"valor": v["valor"], "corte": v["corte"], "passou": v["passou"]}
                                       for k, v in (avaliacao or {}).get("perguntas", {}).items()},
                             "narracao": narracao_completa(h)})
    return n


def falhas_trava(aval: dict | None) -> dict[str, str]:
    """Perguntas com trava que falharam: {chave: pergunta}."""
    return {k: v["pergunta"] for k, v in ((aval or {}).get("perguntas") or {}).items() if v["trava"] and not v["passou"]}


def laco(projeto: dict, canal: dict, reprovadas: list[dict] | None = None) -> dict:
    """Roda o laço sobre o texto corrido. Devolve {'historia', 'avaliacao', 'ressalva', 'versao', 'gancho'}.
    reprovadas: tentativas anteriores reprovadas na trava ({modo, gancho, falhas}); esta começa com outra vibe."""
    pid = projeto["id"]
    projeto["_base_laco"] = custos.total(pid, "historia")
    projeto["_teto_laco"] = teto_laco(canal)
    if projeto["_base_laco"] > 0:
        db.evento(pid, f"Recomeçando o laço: US$ {projeto['_base_laco']:.4f} de tentativas anteriores não contam "
                       f"no teto de US$ {projeto['_teto_laco']:.3f} desta execução.")
    ressalva = None
    melhor = None  # (chave de comparação, historia, avaliacao, versao, problemas)
    # O que reprovou nas tentativas anteriores já entra como ponto de atenção da primeira escrita.
    fraquezas: dict[str, str] = {k: q for r in (reprovadas or []) for k, q in r["falhas"].items()}
    n = config.LACO_MAX_HISTORIAS
    gancho = None
    try:
        # O gancho escolhido pelo Jev fica fixo; cada tentativa escreve o resto do zero.
        gancho, _nota_g, _ = escolher_gancho(projeto, canal, reprovadas)
        projeto["gancho_fixo"] = gancho
        for tentativa in range(1, n + 1):
            h = escrever(projeto, canal, gancho, list(fraquezas.values()))
            problemas, metricas = checar_narrativa(h, canal)
            if problemas:
                # Tamanho é mecânico: um ajuste só no comprimento, sem reescrever a história.
                h = reescrever(projeto, canal, h, problemas, None)
                problemas, metricas = checar_narrativa(h, canal)
            aval = avaliar(projeto, canal, h)
            v = salvar_versao(projeto, h, "escrita", {"problemas": problemas, "metricas": metricas}, aval)
            falhas = ", ".join(aval["falhas"]) or "nenhuma"
            db.evento(pid, f"História {tentativa}/{n} (versão {v}): nota geral {aval['nota_geral']:.2f} · falhas: {falhas}"
                           f"{' · fora do tamanho: ' + '; '.join(problemas) if problemas else ''}")
            # Ordem: passa na segurança, cabe no tamanho, nota geral.
            chave = (not aval["trava_falhou"], not problemas, aval["nota_geral"])
            if melhor is None or chave > melhor[0]:
                melhor = (chave, h, aval, v, problemas)
            if aval["passou"] and not problemas:
                return {"historia": h, "avaliacao": aval, "ressalva": None, "versao": v, "gancho": gancho}
            for k in aval["falhas"]:
                q = aval["perguntas"][k]
                fraquezas[k] = q["pergunta"]
        ressalva = f"nenhuma das {n} histórias passou em tudo; ficou a de maior nota"
    except OrcamentoEstourado as e:
        ressalva = str(e)
        db.evento(pid, ressalva, "aviso")

    if melhor:
        _, h, aval, v, problemas = melhor
        if aval["trava_falhou"]:
            ressalva = f"REPROVADA na trava ({', '.join(falhas_trava(aval))}). {ressalva or ''}".strip()
        elif problemas:
            ressalva = f"{ressalva}; fora do tamanho: {'; '.join(problemas)}"
        return {"historia": h, "avaliacao": aval, "ressalva": ressalva or "não passou em todas as notas", "versao": v,
                "gancho": gancho}
    raise clientes.ErroAPI(ressalva or "o laço terminou sem nenhuma versão")


def biblioteca_para_decupagem(canal: dict) -> list[dict]:
    """A biblioteca só é oferecida quando o canal reaproveita, e só com itens do estilo atual."""
    if not canal["config"].get("reaproveitar_biblioteca"):
        return []
    from . import visual
    return db.todos("SELECT tipo, chave, descricao_fixa FROM biblioteca WHERE canal_id = ? AND estilo = ?",
                    (canal["id"], visual.assinatura_estilo(canal)))
