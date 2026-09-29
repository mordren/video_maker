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

from . import canais, clientes, config, custos, db
from .custos import OrcamentoEstourado

# O Jev aceita no máximo 10 níveis. Seis níveis descritos, convertidos para 0-10 pelo valor esperado
# das probabilidades (nível 0 = 0, nível 5 = 10), então notas intermediárias como 7,4 continuam possíveis.
ESCALA = ["péssimo: falha por completo", "fraco: funciona pouco", "mediano: funciona, sem destaque",
          "bom: funciona bem", "forte: acima da média do gênero", "excepcional: difícil de melhorar"]
MAX_NIVEIS_JEV = 10

SCHEMA_GANCHOS = {
    "type": "object", "additionalProperties": False, "required": ["ganchos"],
    "properties": {"ganchos": {"type": "array", "items": {"type": "string"}}},
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
            "required": ["de", "ate", "prompt_imagem", "personagem", "ambiente", "tensao", "destaque"],
            "properties": {"de": {"type": "integer"}, "ate": {"type": "integer"},
                           "prompt_imagem": {"type": "string"}, "personagem": {"type": "string"},
                           "ambiente": {"type": "string"}, "tensao": {"type": "integer"},
                           "destaque": {"type": "string"}}}},
    },
}

SCHEMA_PROMPTS = {
    "type": "object", "additionalProperties": False, "required": ["cenas"],
    "properties": {"cenas": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["n", "prompt_imagem", "destaque"],
        "properties": {"n": {"type": "integer"}, "prompt_imagem": {"type": "string"},
                       "destaque": {"type": "string"}}}}},
}

FOLGA_PALAVRAS = 0.05      # 5% para mais ou para menos no total
FOLGA_PALAVRAS_CENA = 2    # palavras a mais por cena
EST_TEXTO = 0.003   # estimativa por chamada de escrita antes de haver medição
EST_JEV = 0.001


# ---------------------------------------------------------------- utilidades

def contar_palavras(txt: str) -> int:
    return len(re.findall(r"[\wÀ-ÿ]+(?:['’-][\wÀ-ÿ]+)*", txt or ""))


def limites(canal: dict) -> dict:
    cfg = canal["config"]
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    wps = float(cfg.get("palavras_por_segundo") or 2.75)
    return {
        "palavras_min": int(cfg.get("palavras_min") or fmt["palavras_min"]),
        "palavras_max": int(cfg.get("palavras_max") or fmt["palavras_max"]),
        "cenas_min": fmt["min_cenas"],
        "cenas_max": fmt["max_imagens"],
        "max_cena_s": fmt["max_cena_s"],
        "palavras_por_cena": int(math.floor(fmt["max_cena_s"] * wps)),
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
    if fs and difflib.SequenceMatcher(None, fs[0].lower(), gancho.lower()).ratio() >= 0.6:
        fs[0] = gancho
        return " ".join(fs)
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
        if c.get("personagem") not in ids_p:
            c["personagem"] = ""
        if c.get("ambiente") not in ids_a:
            c["ambiente"] = ""
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
    minimo = int(lim["palavras_min"] * (1 - FOLGA_PALAVRAS))
    maximo = int(round(lim["palavras_max"] * (1 + FOLGA_PALAVRAS)))
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
        if n > lim["palavras_por_cena"] + FOLGA_PALAVRAS_CENA:
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
            linhas.append(f"[{c['n']}] Narração: {c['narracao']}\n    Imagem: {c['prompt_imagem']}"
                          f"{' (personagem ' + c['personagem'] + ')' if c.get('personagem') else ''}")
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


def _media_custo(projeto_id, servico_modelo: str, padrao: float) -> float:
    r = db.um("SELECT AVG(valor_usd) AS m FROM custos WHERE projeto_id = ? AND modelo = ? AND etapa = 'historia'",
              (projeto_id, servico_modelo))
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
        estilo_texto = f"""
Escrita que não soa como texto gerado por IA:
- nada de "não X, mas Y" nem "não é só X, é Y"; afirme direto
- nada de frase de efeito solta nem fileira de fragmentos ("Silêncio. Escuro. Frio."); frase curta, só com fato novo
- nenhum travessão (— ou –) e nenhum hífen duplo; use vírgula, ponto ou dois-pontos
- nada de tríade forçada (três adjetivos ou três exemplos só para soar completo)
- nada de gerúndio pendurado no fim da frase ("..., fazendo com que", "..., deixando")
- evite estas palavras: {vocab}
- varie o comprimento das frases, como alguém contando em voz alta"""
    return f"""Você é o roteirista do canal "{canal['nome']}". {canal['descricao']}

Você escreve a narração de um vídeo curto: um texto corrido, contado de uma vez, que precisa funcionar só no
áudio. Não divida em cenas nem pense em imagens agora; isso é feito depois, a partir do seu texto.
Idioma da narração, do título e da descrição: {canal['idioma']}.

Regras do canal:
{regras}

Estrutura da história: {estrutura}

Tamanho (conferido por programa; fora dele a história volta): entre {lim['palavras_min']} e {lim['palavras_max']} palavras,
cerca de {lim['palavras_min'] / lim['wps']:.0f} a {lim['palavras_max'] / lim['wps']:.0f} segundos de narração.
{estilo_texto}

Campos:
- narracao: a história inteira, texto corrido, pronto para ser lido em voz alta. Sem títulos, marcações ou rubricas.
- titulo: título do vídeo.
- descricao_youtube: 2 ou 3 frases para a descrição do vídeo, sem hashtags.

Responda só com o JSON."""


def sistema_decupagem(canal: dict) -> str:
    lim = limites(canal)
    orient = "vertical 9:16" if canal["formato"] == "short" else "horizontal 16:9"
    destaque = ('UM objeto pequeno e concreto que aparece na imagem e ficará em vermelho vivo, o resto em preto e '
                'branco (em inglês, ex.: "the ribbon on the backpack"). Escolha o que mais pesa na cena; não repita '
                'o mesmo objeto em cenas seguidas.') if _usa_destaque(canal) else 'deixe ""'
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

Campos visuais: escreva prompt_imagem, descricao_fixa e destaque SEMPRE EM INGLÊS, mesmo com a narração em
português (vão para um gerador de imagens que entende mal português). Só os ids podem ficar em português.
- personagens[]: no máximo {lim['max_personagens']}, só quem aparece em alguma imagem. descricao_fixa: aparência
  completa e fixa (idade, corpo, rosto, cabelo, roupa com cores), vale para todas as cenas.
- ambientes[]: um para cada lugar diferente por onde a história passa (quarto, escada, corredor, cozinha, quintal,
  poço...), até {lim['max_ambientes']}. descricao_fixa: o lugar com os elementos que se repetem.
  Use os lugares que a narração cita ou sugere: se a pessoa desce a escada, a escada vira imagem; se joga uma chave
  no quintal, o quintal aparece. Variar os lugares prende mais do que repetir sempre os mesmos dois.
  Se duas cenas seguidas mostrariam a mesma coisa, repita o prompt_imagem igual: o programa reaproveita a imagem
  com outro movimento de câmera, que sai mais barato e mantém o momento. Só escreva uma imagem nova quando a
  narração pedir algo novo para ver.
- cenas[].prompt_imagem: só o que aparece nesta imagem (ação, enquadramento, luz), composição {orient}.
  Mostre o que está sendo narrado naquele momento. Em terror, prefira o detalhe concreto (o som, o objeto
  fora do lugar, a sombra) ao monstro inteiro. Cite o personagem pelo nome, sem repetir a descrição fixa.
  Texto escrito dentro da imagem só quando for essencial.
- cenas[].personagem: o id de quem aparece na imagem, ou "".
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

def escolher_gancho(projeto: dict, canal: dict) -> tuple[str, float, list]:
    perguntas = canais.perguntas_do_canal(canal["config"])
    base = perguntas["gancho"]
    corte = base["corte"]
    anteriores: list[dict] = []
    melhor = ("", -1.0)
    for rodada in range(1, config.GANCHO_MAX_RODADAS + 1):
        _checar_teto(projeto, _media_custo(projeto["id"], config.MODELO_TEXTO, EST_TEXTO) +
                     _media_custo(projeto["id"], config.MODELO_JUIZ, EST_JEV))
        hist = ""
        if anteriores:
            hist = "\n\nGanchos anteriores e a nota que tiveram (0-10, corte " + f"{corte}" + "). Não repita; faça melhor:\n" + \
                   "\n".join(f"- ({a['nota']:.1f}) {a['texto']}" for a in anteriores)
        msgs = [
            {"role": "system", "content": sistema_escrita(canal)},
            {"role": "user", "content": f"Assunto: {projeto['assunto']}\n\nEscreva 5 ganchos diferentes para abrir essa história. "
             "O gancho é a primeira frase da narração: curta, que faz quem está rolando o feed parar nos 3 primeiros "
             "segundos (siga o tamanho que as regras do canal pedirem). Cada um com uma abordagem diferente "
             f"(situação estranha, pergunta implícita, fato concreto, contradição).{hist}\n\nResponda com {{\"ganchos\": [5 textos]}}."},
        ]
        resp, _ = clientes.chat("ganchos", msgs, SCHEMA_GANCHOS, projeto_id=projeto["id"], etapa="historia",
                                temperatura=1.0, max_tokens=1200, contexto={"assunto": projeto["assunto"]})
        ganchos = [g.strip() for g in resp.get("ganchos", []) if g.strip()][:5]
        if not ganchos:
            continue
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
        db.evento(projeto["id"], f"Ganchos rodada {rodada}: melhor nota {top[1]:.1f} (corte {corte}) · \"{top[0]}\"")
        if top[1] > melhor[1]:
            melhor = top
        if top[1] >= corte:
            break
    if not melhor[0]:
        raise clientes.ErroAPI("O modelo não devolveu ganchos")
    return melhor[0], melhor[1], anteriores


# ---------------------------------------------------------------- escrita e reescrita (texto corrido)

def escrever(projeto: dict, canal: dict, gancho: str) -> dict:
    _checar_teto(projeto, _media_custo(projeto["id"], config.MODELO_TEXTO, EST_TEXTO))
    msgs = [
        {"role": "system", "content": sistema_escrita(canal)},
        {"role": "user", "content": f"Assunto: {projeto['assunto']}\n\nA narração começa exatamente com este gancho, "
                                    f"não mude: \"{gancho}\"\n\nEscreva a história completa em JSON."},
    ]
    h, _ = clientes.chat("narrativa", msgs, SCHEMA_NARRATIVA, projeto_id=projeto["id"], etapa="historia",
                         contexto={"assunto": projeto["assunto"], "gancho": gancho})
    return normalizar_narrativa(h, gancho)


def reescrever(projeto: dict, canal: dict, h: dict, problemas: list[str], avaliacao: dict | None) -> dict:
    _checar_teto(projeto, _media_custo(projeto["id"], config.MODELO_TEXTO, EST_TEXTO))
    itens = [f"- {p}" for p in problemas]
    gancho_livre = False
    if avaliacao:
        for k in avaliacao["falhas"]:
            v = avaliacao["perguntas"][k]
            nota = f"{v['valor']:.1f}/10" if v["tipo"] == "score" else f"{v['valor']:.2f} de probabilidade"
            corte = f"{v['corte']}/10" if v["tipo"] == "score" else f"{v['corte']}"
            itens.append(f"- \"{v['pergunta']}\" recebeu {nota}; precisa de {corte}")
            gancho_livre = gancho_livre or k == "gancho"
    trava = "" if gancho_livre else f"\nMantenha a primeira frase exatamente igual: \"{h['gancho']}\"."
    msgs = [
        {"role": "system", "content": sistema_escrita(canal)},
        {"role": "user", "content": f"Assunto: {projeto['assunto']}\n\nVersão atual:\n{_json_narrativa(h)}\n\n"
                                    f"O que falhou na avaliação:\n" + "\n".join(itens) +
                                    "\n\nReescreva só o necessário para corrigir esses pontos e mantenha o que já está bom."
                                    f"{trava}\nDevolva a história completa em JSON."},
    ]
    nova, _ = clientes.chat("reescrita", msgs, SCHEMA_NARRATIVA, projeto_id=projeto["id"], etapa="historia",
                            contexto={"historia": h, "problemas": problemas})
    return normalizar_narrativa(nova, None if gancho_livre else h["gancho"])


def avaliar(projeto: dict, canal: dict, h: dict, perguntas: dict | None = None, etapa: str = "historia") -> dict:
    perguntas = perguntas or perguntas_narrativa(canal)
    if etapa == "historia":
        _checar_teto(projeto, _media_custo(projeto["id"], config.MODELO_JUIZ, EST_JEV))
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
                if palavras > lim["palavras_por_cena"] + FOLGA_PALAVRAS_CENA and ate > de:
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
    erros = _validar_grupos(resp.get("cenas") or [], len(trs))
    if erros:
        db.evento(pid, f"Decupagem com erro ({'; '.join(erros[:3])}); pedindo de novo.")
        msgs += [{"role": "assistant", "content": json.dumps(resp, ensure_ascii=False)},
                 {"role": "user", "content": "Corrija: " + "; ".join(erros) + f". Use todos os {len(trs)} trechos, na "
                                             "ordem, sem pular nem repetir. JSON completo."}]
        resp, _ = clientes.chat("decupagem", msgs, SCHEMA_DECUPAGEM, projeto_id=pid, etapa="decupagem",
                                temperatura=0.3, contexto={"trechos": trs})
        if _validar_grupos(resp.get("cenas") or [], len(trs)):
            db.evento(pid, "A decupagem ainda veio com buracos; o programa ajustou os limites das cenas.", "aviso")
    grupos = _dividir_longas(_consertar_grupos(resp.get("cenas") or [], len(trs)), trs, lim)
    if not grupos:
        raise clientes.ErroAPI("A decupagem não devolveu cenas")
    novo = {**h, "personagens": resp.get("personagens") or [], "ambientes": resp.get("ambientes") or [],
            "cenas": [{"narracao": " ".join(trs[g["de"] - 1:g["ate"]]), "prompt_imagem": g.get("prompt_imagem", ""),
                       "personagem": g.get("personagem", ""), "ambiente": g.get("ambiente", ""),
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
        parecido = difflib.SequenceMatcher(None, ant["prompt_imagem"].lower(), c["prompt_imagem"].lower()).ratio()
        if parecido >= 0.8 and c["n"] not in ruins and ant["n"] not in ruins:
            c["mesma_imagem_de"] = ant.get("mesma_imagem_de") or ant["n"]
            c["personagem"], c["ambiente"] = ant.get("personagem", ""), ant.get("ambiente", "")
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
             "destaque_atual": c.get("destaque", ""), "ambiente": c.get("ambiente", ""), "problema": ruins[c["n"]]}
            for c in h["cenas"] if c["n"] in ruins]
    msgs = [{"role": "system", "content": sistema_decupagem(canal)},
            {"role": "user", "content": "Refaça o prompt_imagem (e o destaque) destas cenas, corrigindo o problema de cada "
                                        "uma, em inglês, mantendo personagens e o lugar indicado:\n" +
                                        json.dumps(alvo, ensure_ascii=False) +
                                        "\nResponda com {\"cenas\": [{\"n\", \"prompt_imagem\", \"destaque\"}]}."}]
    resp, _ = clientes.chat("prompts_cena", msgs, SCHEMA_PROMPTS, projeto_id=pid, etapa="decupagem", temperatura=0.5,
                            contexto={"alvo": alvo})
    novos = {c["n"]: c for c in resp.get("cenas") or []}
    for c in h["cenas"]:
        if c["n"] in novos and novos[c["n"]].get("prompt_imagem"):
            c["prompt_imagem"] = novos[c["n"]]["prompt_imagem"]
            c["destaque"] = novos[c["n"]].get("destaque", c.get("destaque", ""))
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


def laco(projeto: dict, canal: dict) -> dict:
    """Roda o laço sobre o texto corrido. Devolve {'historia', 'avaliacao', 'ressalva', 'versao'}."""
    pid = projeto["id"]
    projeto["_base_laco"] = custos.total(pid, "historia")
    projeto["_teto_laco"] = teto_laco(canal)
    if projeto["_base_laco"] > 0:
        db.evento(pid, f"Recomeçando o laço: US$ {projeto['_base_laco']:.4f} de tentativas anteriores não contam "
                       f"no teto de US$ {projeto['_teto_laco']:.3f} desta execução.")
    ressalva = None
    melhor = None  # (nota, historia, avaliacao, versao)
    ultima = None
    try:
        gancho, _nota_g, _ = escolher_gancho(projeto, canal)
        h = escrever(projeto, canal, gancho)
        sem_melhora = 0
        for rodada in range(1, config.LACO_MAX_RODADAS + 1):
            problemas, metricas = checar_narrativa(h, canal)
            if problemas:
                v = salvar_versao(projeto, h, "escrita" if rodada == 1 else "reescrita",
                                  {"problemas": problemas, "metricas": metricas}, None)
                ultima = (h, None, v)
                db.evento(pid, f"Versão {v}: fora dos limites ({'; '.join(problemas)}). Volta para reescrita sem chamar o Jev.")
                sem_melhora += 1
                if rodada == config.LACO_MAX_RODADAS or (sem_melhora >= config.LACO_SEM_MELHORA and melhor):
                    ressalva = "o laço acabou com a última versão fora dos limites"
                    break
                h = reescrever(projeto, canal, h, problemas, None)
                continue
            aval = avaliar(projeto, canal, h)
            v = salvar_versao(projeto, h, "escrita" if rodada == 1 else "reescrita",
                              {"problemas": [], "metricas": metricas}, aval)
            ultima = (h, aval, v)
            falhas = ", ".join(aval["falhas"]) or "nenhuma"
            db.evento(pid, f"Versão {v}: nota geral {aval['nota_geral']:.2f} · falhas: {falhas}")
            if aval["passou"]:
                return {"historia": h, "avaliacao": aval, "ressalva": None, "versao": v}
            # Uma versão que falha na trava de segurança nunca substitui outra que passa nela.
            if melhor is None:
                melhorou = True
            elif melhor[2]["trava_falhou"] != aval["trava_falhou"]:
                melhorou = melhor[2]["trava_falhou"]
            else:
                melhorou = aval["nota_geral"] > melhor[0]
            if melhorou:
                sem_melhora = 0
                melhor = (aval["nota_geral"], h, aval, v)
            else:
                sem_melhora += 1
            if sem_melhora >= config.LACO_SEM_MELHORA:
                ressalva = "duas rodadas seguidas sem melhorar a nota"
                break
            if rodada == config.LACO_MAX_RODADAS:
                ressalva = f"{config.LACO_MAX_RODADAS} rodadas sem passar em tudo"
                break
            h = reescrever(projeto, canal, h, [], aval)
    except OrcamentoEstourado as e:
        ressalva = str(e)
        db.evento(pid, ressalva, "aviso")

    if melhor:
        nota, h, aval, v = melhor
        if aval["trava_falhou"]:
            ressalva = f"REPROVADA na pergunta de segurança. {ressalva or ''}".strip()
        return {"historia": h, "avaliacao": aval, "ressalva": ressalva or "não passou em todas as notas", "versao": v}
    if ultima:
        h, aval, v = ultima
        return {"historia": h, "avaliacao": aval, "versao": v,
                "ressalva": f"nenhuma versão ficou dentro dos limites do formato. {ressalva or ''}".strip()}
    raise clientes.ErroAPI(ressalva or "o laço terminou sem nenhuma versão")


def biblioteca_para_decupagem(canal: dict) -> list[dict]:
    """A biblioteca só é oferecida quando o canal reaproveita, e só com itens do estilo atual."""
    if not canal["config"].get("reaproveitar_biblioteca"):
        return []
    from . import visual
    return db.todos("SELECT tipo, chave, descricao_fixa FROM biblioteca WHERE canal_id = ? AND estilo = ?",
                    (canal["id"], visual.assinatura_estilo(canal)))
