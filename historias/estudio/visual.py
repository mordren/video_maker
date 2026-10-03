"""Etapa 3: consistência visual e imagens.

Ordem fixa: ficha do personagem -> placa de cada ambiente recorrente -> cenas com as referências que lhes cabem.
Ficha e placa ficam na biblioteca do canal e são reaproveitadas de um vídeo para outro sem custo.
"""
import difflib
import hashlib
import io
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import clientes, comfy, config, custos, db, historia

VALIDADE_URL = timedelta(days=25)  # o media.pollinations.ai guarda por 30 dias


def pasta(projeto) -> Path:
    return config.BASE / projeto["pasta"]


def dims_cena(canal) -> tuple[int, int]:
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    base = int(canal["config"].get("resolucao_base") or 1080)
    w, h = fmt["img_largura"], fmt["img_altura"]
    if base < 1080:
        fator = base / min(w, h)
        w, h = int(round(w * fator / 16) * 16), int(round(h * fator / 16) * 16)
    return w, h


def _orientacao(canal) -> str:
    return "vertical composition" if canal["formato"] == "short" else "horizontal widescreen composition"


def _verificar_orcamento(projeto, canal, preco=None):
    teto = float(canal["config"].get("orcamento_usd") or 0.10)
    if preco is None:
        preco = 0.0 if clientes.usa_comfy() else clientes._preco_imagem(config.MODELO_IMAGEM)
    custos.verificar(projeto["id"], None, preco, teto, "Orçamento do vídeo")


def _salvar(dados: bytes, destino: Path):
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(dados)


def _recortar_bordas(dados: bytes, fracao: float) -> bytes:
    """Tira a moldura de papel que certos estilos (página impressa) desenham e volta ao tamanho original."""
    im = Image.open(io.BytesIO(dados)).convert("RGB")
    w, h = im.size
    dx, dy = round(w * fracao), round(h * fracao)
    saida = io.BytesIO()
    im.crop((dx, dy, w - dx, h - dy)).resize((w, h), Image.LANCZOS).save(saida, "JPEG", quality=95)
    return saida.getvalue()


def _url_valida(item: dict) -> str | None:
    if not item.get("url") or not item.get("url_em"):
        return None
    try:
        if datetime.now() - datetime.fromisoformat(item["url_em"]) > VALIDADE_URL:
            return None
    except ValueError:
        return None
    return item["url"]


def url_referencia(item: dict) -> str:
    """URL pública da ficha/placa; sobe de novo se a antiga estiver perto de expirar."""
    url = _url_valida(item)
    if url:
        return url
    url = clientes.enviar_midia(config.BASE / item["arquivo"])
    db.executar("UPDATE biblioteca SET url = ?, url_em = ? WHERE id = ?", (url, db.agora(), item["id"]))
    item["url"], item["url_em"] = url, db.agora()
    return url


def ref_envio(item: dict):
    """O Comfy recebe o arquivo local da ficha/placa; a Pollinations, a URL pública."""
    return config.BASE / item["arquivo"] if motor_prompt() or item.get("local") else url_referencia(item)


def _parecido(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio() >= 0.8


# ---------------------------------------------------------------- estilo

# Marcador no texto do estilo que recebe o objeto de destaque de cada cena (ex.: [ELEMENTO EM VERMELHO]).
_MARCADOR = re.compile(r"\[[^\]]*(vermelh|destaque|red|highlight)[^\]]*\]", re.I)
SEM_DESTAQUE = "none, keep everything monochrome"


def _limpar(txt: str) -> str:
    return re.sub(r"\s+", " ", txt or "").strip().strip(",").strip()


def assinatura_estilo(canal: dict) -> str:
    est = canal["config"].get("estilo") or {}
    base = _limpar(est.get("prefixo", "")) + "|" + _limpar(est.get("sufixo", ""))
    return hashlib.sha1(base.encode("utf-8")).hexdigest()[:12]


def aplicar_estilo(canal: dict, miolo: list[str], destaque: str | None = None) -> str:
    """prefixo do estilo + conteúdo + orientação + sufixo, com o marcador trocado pelo destaque da cena."""
    est = canal["config"].get("estilo") or {}
    troca = (destaque or "").strip() or SEM_DESTAQUE
    prefixo = _MARCADOR.sub(troca, _limpar(est.get("prefixo", "")))
    if prompt_curto():  # as imagens de referência já carregam o estilo: só as três primeiras marcas dele
        prefixo = ", ".join(prefixo.split(", ")[:3])
    sufixo = _MARCADOR.sub(troca, _limpar(est.get("sufixo", "")))
    partes = [prefixo, _orientacao(canal)] + [_limpar(m).rstrip(".") for m in miolo] + [sufixo]
    return ". ".join(p for p in partes if p)


def usa_destaque(canal: dict) -> bool:
    est = canal["config"].get("estilo") or {}
    return bool(_MARCADOR.search(est.get("prefixo", "") + est.get("sufixo", "")))


# ---------------------------------------------------------------- biblioteca

def _chave_livre(canal, tipo: str, chave: str, projeto_id: int) -> str:
    existe = lambda k: db.um("SELECT id FROM biblioteca WHERE canal_id = ? AND tipo = ? AND chave = ?",
                             (canal["id"], tipo, k))
    if not existe(chave):
        return chave
    candidata, i = f"{chave}-p{projeto_id}", 2
    while existe(candidata):
        candidata, i = f"{chave}-p{projeto_id}-{i}", i + 1
    return candidata


def _entrada_biblioteca(canal, projeto, tipo: str, chave: str, descricao: str) -> tuple[dict | None, str]:
    """Devolve (item reaproveitável, chave a usar). Só reaproveita com a opção ligada no canal, mesmo id,
    descrição parecida e o MESMO estilo visual; senão cria uma entrada nova (a biblioteca só cresce)."""
    if canal["config"].get("reaproveitar_biblioteca"):
        assinatura = assinatura_estilo(canal)
        for item in db.todos("SELECT * FROM biblioteca WHERE canal_id = ? AND tipo = ? AND (chave = ? OR chave LIKE ?) "
                             "AND estilo = ? ORDER BY id DESC", (canal["id"], tipo, chave, f"{chave}-p%", assinatura)):
            if _parecido(item["descricao_fixa"], descricao) and item.get("arquivo") and \
                    (config.BASE / item["arquivo"]).exists() and not _folha_antiga(item):
                return item, item["chave"]
    return None, _chave_livre(canal, tipo, chave, projeto["id"])


def _folha_antiga(item: dict) -> bool:
    """Fichas feitas antes do retrato único (várias vistas numa imagem) não servem mais de referência."""
    if item["tipo"] != "personagem":
        return False
    try:
        meta = json.loads((config.BASE / item["arquivo"]).with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True
    return "reference sheet" in (meta.get("prompt") or "").lower()


def _pasta_bib(canal, tipo, chave) -> Path:
    return config.BIBLIOTECA / canal["slug"] / ("personagens" if tipo == "personagem" else "ambientes") / chave


_thread = threading.local()


def motor_prompt() -> str:
    """Quem vai ler o prompt: "qwen" ou "kontext" (ComfyUI, pelo workflow) ou "" (Pollinations)."""
    forcado = getattr(_thread, "motor", None)
    if forcado is not None:
        return forcado
    return comfy.modelo() if clientes.usa_comfy() else ""


@contextmanager
def prompts_para(motor: str):
    """Monta os prompts para esse motor nesta thread, seja qual for o motor dos vídeos (aba Teste Comfy, exportação
    de workflow). Por thread: um teste rodando não muda os prompts de um vídeo sendo gerado ao mesmo tempo."""
    anterior = getattr(_thread, "motor", None)
    _thread.motor = motor
    try:
        yield
    finally:
        _thread.motor = anterior


def prompt_curto() -> bool:
    """Qwen-Image-Edit no Comfy: prompts enxutos, porque a imagem de referência já carrega os detalhes."""
    return motor_prompt() == "qwen"


def prompt_referencia(canal: dict, tipo: str, descricao: str) -> str:
    if motor_prompt() == "kontext":
        return estilo_kontext(_prompt_referencia(canal, tipo, descricao))
    return _prompt_referencia(canal, tipo, descricao)


# O Kontext lê "comic book panel" e "sensational horror" como uma capa de revista pulp: desenhou título inventado,
# selo de preço, caixa de texto e um monstro na placa do lugar vazio. Por isso o objeto (painel, capa, página) vira
# "ilustração no estilo de", e "sensational" (palavra de chamada de capa) sai. Os "no text", "no people" do estilo
# ficam: sem eles, foi pior.
_TROCAS_KONTEXT = [
    (re.compile(r"\bcomic[- ]book (?:panel|page|cover)s?\b", re.I), "wordless comic-book-style illustration"),
    (re.compile(r"\b(?:magazine|book) cover\b", re.I), "illustration"),
    (re.compile(r"\bposter\b", re.I), "illustration"),
    (re.compile(r"\bsensational\s+", re.I), ""),
]


def estilo_kontext(prompt: str) -> str:
    for padrao, troca in _TROCAS_KONTEXT:
        prompt = padrao.sub(troca, prompt)
    return prompt


def _prompt_referencia(canal: dict, tipo: str, descricao: str) -> str:
    if prompt_curto():
        if tipo == "personagem":
            return aplicar_estilo(canal, [f"Portrait of one person: {descricao}",
                                          "Standing, facing the camera, plain light grey background"])
        return aplicar_estilo(canal, [f"Establishing shot of {descricao}, empty location"])
    if tipo == "personagem":
        # Um retrato só. A antiga folha (frente, lado, costas e três closes) mostrava seis cópias da pessoa, e o
        # gerador repetia gente na cena ou misturava os rostos quando havia duas referências.
        miolo = [f"Portrait of one single person: {descricao}",
                 "Three-quarter body, facing the camera, neutral expression, arms relaxed, full outfit visible",
                 "Plain light grey background, even soft light, nothing else in the image, only this one person"]
    else:
        miolo = [f"Establishing shot of {descricao}", "Empty location, no people, no characters"]
    return aplicar_estilo(canal, miolo)


def _gerar_referencia(projeto: dict, canal: dict, tipo: str, obj: dict, idx: int) -> dict:
    """Ficha (personagem) ou placa (ambiente) nova, salva na biblioteca com a assinatura do estilo."""
    pid = projeto["id"]
    item, chave = _entrada_biblioteca(canal, projeto, tipo, obj["id"], obj["descricao_fixa"])
    rotulo = "Ficha" if tipo == "personagem" else "Placa"
    if item:
        obj["descricao_fixa"] = item["descricao_fixa"]
        obj["bib_chave"] = chave
        db.evento(pid, f"{rotulo} de {obj['id']} reaproveitada da biblioteca (mesmo estilo, custo zero).")
        return item
    _verificar_orcamento(projeto, canal)
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    if tipo == "personagem":
        w, hh, seed, arquivo = fmt["ficha_largura"], fmt["ficha_altura"], projeto["seed_base"] + idx, "ficha"
    else:
        w, hh = dims_cena(canal)
        seed, arquivo = projeto["seed_base"] + 500 + idx, "placa"
    prompt = prompt_referencia(canal, tipo, obj["descricao_fixa"])
    db.evento(pid, f"Gerando {rotulo.lower()} de {obj.get('nome') or obj['id']}...")
    dados, link, _ = clientes.gerar_imagem(prompt, w, hh, seed, projeto_id=pid, etapa=arquivo)
    destino = _pasta_bib(canal, tipo, chave) / f"{arquivo}.jpg"
    _salvar(dados, destino)
    rel = destino.relative_to(config.BASE).as_posix()
    estilo = canal["config"].get("estilo") or {}
    (destino.parent / f"{arquivo}.json").write_text(json.dumps(
        {**obj, "prompt": prompt, "url": link, "estilo": estilo.get("nome"), "projeto": pid},
        ensure_ascii=False, indent=2), encoding="utf-8")
    db.executar("INSERT INTO biblioteca (canal_id, tipo, chave, descricao_fixa, arquivo, url, url_em, criado_em, estilo, "
                "projeto_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (canal["id"], tipo, chave, obj["descricao_fixa"], rel, link, db.agora() if link else None, db.agora(),
                 assinatura_estilo(canal), pid))
    obj["bib_chave"] = chave
    return db.um("SELECT * FROM biblioteca WHERE canal_id = ? AND tipo = ? AND chave = ?", (canal["id"], tipo, chave))


def preparar_referencias(projeto: dict, canal: dict, h: dict) -> dict:
    """Garante ficha de cada personagem e placa de cada ambiente recorrente. Devolve {('personagem', id): item}."""
    refs = {}
    usados = {i for c in h["cenas"] for i in historia.personagens_cena(c)}
    for idx, p in enumerate(h.get("personagens", [])):
        if p["id"] in usados:
            refs[("personagem", p["id"])] = _gerar_referencia(projeto, canal, "personagem", p, idx)
    recorrentes = historia.ambientes_recorrentes(h)
    for idx, a in enumerate(h.get("ambientes", [])):
        if a["id"] in recorrentes:
            refs[("ambiente", a["id"])] = _gerar_referencia(projeto, canal, "ambiente", a, idx)
    return refs


def chaves_referencia(c: dict) -> list[tuple[str, str]]:
    """Referências escolhidas à mão para a cena, na ordem: [('personagem', id), ('ambiente', id)]."""
    saida = []
    for r in c.get("referencias") or []:
        tipo, _, id_ = str(r).partition(":")
        if tipo in ("personagem", "ambiente") and id_ and (tipo, id_) not in saida:
            saida.append((tipo, id_))
    return saida[:historia.MAX_REFERENCIAS]


def montar_prompt(h: dict, c: dict, canal: dict, refs: dict) -> tuple[str, list]:
    """Ordem do prompt: estilo, orientação, referências escolhidas (a primeira antes), lugar e luz, uma linha por
    pessoa no lugar dela, clima e destaque. Sem escolha manual, as referências vêm do ambiente e das pessoas da cena."""
    if prompt_curto():
        return montar_prompt_curto(h, c, canal, refs)
    if motor_prompt() == "kontext":
        return montar_prompt_kontext(h, c, canal, refs)
    fichas = {x["id"]: x for x in h.get("personagens", [])}
    ambientes = {x["id"]: x for x in h.get("ambientes", [])}
    manual = [k for k in chaves_referencia(c) if refs.get(k)]
    urls = [ref_envio(refs[k]) for k in manual]
    miolo = []
    if manual:
        # Cada referência é apresentada pelo número, na ordem escolhida, antes de qualquer outra coisa da cena.
        for i, (tipo, id_) in enumerate(manual, 1):
            obj = (fichas if tipo == "personagem" else ambientes).get(id_, {})
            quem = "the person" if tipo == "personagem" else "the place"
            miolo.append(f"Reference image {i} shows {quem}: {obj.get('descricao_fixa', id_)}")
    miolo.append(c["prompt_imagem"])

    def numero(chave):
        """Número da referência desta chave no envio (None se não há imagem para ela)."""
        item = refs.get(chave)
        if not item:
            return None
        if manual:
            return manual.index(chave) + 1 if chave in manual else None
        urls.append(ref_envio(item))
        return len(urls)

    # Placa escolhida à mão manda no lugar da cena (é para isso que serve a escolha)
    escolhido = next((id_ for tipo, id_ in manual if tipo == "ambiente"), None)
    a = ambientes.get(escolhido or c.get("ambiente"))
    if a:
        n = numero(("ambiente", a["id"]))
        if n:
            # A placa já mostra o lugar: repetir a descrição inteira empurra a fornalha, o teto etc. para dentro de
            # cenas que são um close (uma porta, um gravador) e faz a imagem copiar o enquadramento da placa.
            miolo.append(f"The place is the one in reference image {n} (same objects and materials), but the "
                         f"framing and camera angle are the ones described for this scene")
        else:
            miolo.append(f"Setting: {a['descricao_fixa']}")
    pessoas = [p for p in historia.pessoas_cena(c) if p["id"] in fichas]
    if len(pessoas) > 1:
        miolo.append(f"Exactly {len(pessoas)} people in the image, each clearly distinct")
    linhas, algum_retrato = [], False
    for p in pessoas:
        n = numero(("personagem", p["id"]))
        algum_retrato = algum_retrato or bool(n)
        linhas.append(historia.descrever_pessoa(fichas[p["id"]], p, n))
    if algum_retrato:
        miolo.append("Portrait references are for identity only (face, hair, body, clothes); ignore their pose, "
                     "framing and plain background")
    miolo += linhas
    return aplicar_estilo(canal, miolo, c.get("destaque")), urls


def montar_prompt_curto(h: dict, c: dict, canal: dict, refs: dict) -> tuple[str, list]:
    """Versão enxuta para o Qwen: o que acontece, o lugar e quem está onde, cada um ligado à sua imagem pelo número.
    A aparência (rosto, cabelo, roupa) e o lugar vêm da imagem, então a descrição fixa só entra para quem não tem
    imagem. Mesma ordem de referências do montar_prompt: as escolhidas à mão, senão o lugar e depois as pessoas."""
    fichas = {x["id"]: x for x in h.get("personagens", [])}
    ambientes = {x["id"]: x for x in h.get("ambientes", [])}
    manual = [k for k in chaves_referencia(c) if refs.get(k)]
    chaves = list(manual)
    amb = ambientes.get(next((i for t, i in manual if t == "ambiente"), None) or c.get("ambiente"))
    pessoas = [p for p in historia.pessoas_cena(c) if p["id"] in fichas]
    if not manual:
        if amb and ("ambiente", amb["id"]) in refs:
            chaves.append(("ambiente", amb["id"]))
        chaves += [("personagem", p["id"]) for p in pessoas if ("personagem", p["id"]) in refs]
    n = {k: i for i, k in enumerate(chaves, 1)}
    cena = re.sub(r"^\s*vertical 9:16,?\s*|^\s*horizontal 16:9,?\s*", "", c["prompt_imagem"], flags=re.I)
    miolo = [cena[:1].upper() + cena[1:]]
    if amb:
        i = n.get(("ambiente", amb["id"]))
        miolo.append(f"The room is the one in picture {i}, seen from a new angle" if i else
                     f"Setting: {amb['descricao_fixa']}")
    if len(pessoas) > 1:
        miolo.append(f"Exactly {len(pessoas)} people")
    for p in pessoas:
        f = fichas[p["id"]]
        i = n.get(("personagem", p["id"]))
        acao = ", ".join(x.strip().rstrip(".") for x in (p.get("onde"), p.get("faz")) if x and x.strip())
        quem = f"{f.get('nome') or f['id']} (picture {i})" if i else f"{f.get('nome') or f['id']}, {f['descricao_fixa']}"
        miolo.append(f"{quem}: {acao}" if acao else quem)
    if any(k[0] == "personagem" for k in chaves):
        miolo.append("Keep each person's face, hair and clothes from their picture")
    return aplicar_estilo(canal, miolo, c.get("destaque")), [ref_envio(refs[k]) for k in chaves]


def montar_prompt_kontext(h: dict, c: dict, canal: dict, refs: dict) -> tuple[str, list]:
    """FLUX.1 Kontext: ele não numera as referências ("image 2" não quer dizer nada para ele), então cada imagem é
    citada pelo que mostra: a pessoa pela descrição da ficha (a mesma do retrato), o lugar como "a foto do lugar
    vazio". Pelo guia da BFL: dizer o que se preserva (rosto, cabelo, roupa), nomear quem é quem por descrição e
    não por pronome, e descrever o enquadramento novo para ele não devolver a placa com a pessoa colada.
    Ordem das referências igual à do montar_prompt_curto."""
    fichas = {x["id"]: x for x in h.get("personagens", [])}
    ambientes = {x["id"]: x for x in h.get("ambientes", [])}
    manual = [k for k in chaves_referencia(c) if refs.get(k)]
    chaves = list(manual)
    amb = ambientes.get(next((i for t, i in manual if t == "ambiente"), None) or c.get("ambiente"))
    pessoas = [p for p in historia.pessoas_cena(c) if p["id"] in fichas]
    if not manual:
        if amb and ("ambiente", amb["id"]) in refs:
            chaves.append(("ambiente", amb["id"]))
        chaves += [("personagem", p["id"]) for p in pessoas if ("personagem", p["id"]) in refs]
    cena = re.sub(r"^\s*(vertical 9:16|horizontal 16:9)( composition)?[,.]?\s*", "", c["prompt_imagem"], flags=re.I)
    miolo = [cena[:1].upper() + cena[1:]]
    if amb:
        miolo.append("The location is the place from the reference photo of the empty location, with the same "
                     "architecture, objects, materials and colors, now seen from the camera angle and distance of "
                     "this scene" if ("ambiente", amb["id"]) in chaves else f"Setting: {amb['descricao_fixa']}")
    if len(pessoas) > 1:
        miolo.append(f"Exactly {len(pessoas)} different people, each with their own face")
    miolo += [historia.descrever_pessoa(fichas[p["id"]], p) for p in pessoas]
    retratos = sum(1 for k in chaves if k[0] == "personagem")
    if retratos:
        quem = "Each person keeps" if retratos > 1 else "The person keeps"
        miolo.append(f"{quem} the exact face, facial features, hairstyle, body and clothes from the matching reference "
                     "portrait, in the new pose and position described here, placed inside this scene and lit by its "
                     "light; the plain grey background of the portrait is replaced by this scene")
    prompt = estilo_kontext(aplicar_estilo(canal, miolo, c.get("destaque")))
    return prompt, [ref_envio(refs[k]) for k in chaves]


# A moderação da Pollinations recusa cenas inofensivas por causa do clima do estilo ("sensational horror",
# "disturbing"...). Na segunda tentativa essas palavras viram versões mais brandas; o resto do prompt fica igual.
_BRANDAS = [("sensational horror", "moody"), ("unsettling", "mysterious"), ("dread", "suspense"),
            ("disturbing", "strange"), ("lurid", "vivid"), ("terrified", "worried"), ("terror", "tension"),
            ("blood", "red paint"), ("corpse", "figure")]


def suavizar(prompt: str) -> str:
    for padrao, troca in _BRANDAS:
        prompt = re.sub(padrao, troca, prompt, flags=re.I)
    return prompt


def gerar_cena(projeto: dict, canal: dict, h: dict, c: dict, refs: dict, versao: int = 0) -> Path:
    pid = projeto["id"]
    n = c["n"]
    prompt, urls = montar_prompt(h, c, canal, refs)
    seed = projeto["seed_base"] + n + versao * 1000
    db.executar("INSERT INTO cenas (projeto_id, n, status, seed, versao, prompt_final) VALUES (?, ?, 'gerando', ?, ?, ?) "
                "ON CONFLICT(projeto_id, n) DO UPDATE SET status='gerando', seed=excluded.seed, versao=excluded.versao, "
                "prompt_final=excluded.prompt_final, erro=NULL", (pid, n, seed, versao, prompt))
    try:
        _verificar_orcamento(projeto, canal)
        w, hh = dims_cena(canal)
        try:
            dados, _, _ = clientes.gerar_imagem(prompt, w, hh, seed, urls, projeto_id=pid, etapa="cenas")
        except clientes.ErroModeracao as e:
            db.evento(pid, f"Cena {n}: recusada pela moderação do provedor, tentando de novo com o texto mais brando. "
                           f"Motivo: {str(e)[:400]}", "aviso")
            prompt = suavizar(prompt)
            db.executar("UPDATE cenas SET prompt_final = ? WHERE projeto_id = ? AND n = ?", (prompt, pid, n))
            dados, _, _ = clientes.gerar_imagem(prompt, w, hh, seed, urls, projeto_id=pid, etapa="cenas")
        destino = pasta(projeto) / "cenas" / f"{n:02d}.jpg"
        if destino.exists() and versao > 0:
            destino.replace(destino.with_name(f"{n:02d}_v{versao - 1}.jpg"))
        recorte = float((canal["config"].get("estilo") or {}).get("recorte") or 0)
        _salvar(_recortar_bordas(dados, recorte) if recorte > 0 else dados, destino)
        db.executar("UPDATE cenas SET status = 'ok', arquivo = ? WHERE projeto_id = ? AND n = ?",
                    (destino.relative_to(config.BASE).as_posix(), pid, n))
        return destino
    except Exception as e:
        db.executar("UPDATE cenas SET status = 'erro', erro = ? WHERE projeto_id = ? AND n = ?", (str(e)[:500], pid, n))
        raise


def gerar_todas(projeto: dict, canal: dict, h: dict) -> dict:
    """Gera só as cenas que ainda não têm imagem ok. Devolve o h atualizado (com bib_chave)."""
    aviso = clientes.verificar_preco_imagem()
    if aviso:
        db.evento(projeto["id"], aviso, "aviso")
    if config.motor_imagem() == "comfy" and not config.SIMULACAO:
        if not config.comfy_url():
            db.evento(projeto["id"], "Sem link do ComfyUI; imagens pela API (Pollinations).")
        elif comfy.no_ar():
            clientes.marcar_comfy_no_ar()
            db.evento(projeto["id"], f"Imagens no ComfyUI ({config.comfy_url()}).")
        else:
            clientes.marcar_comfy_fora()
            db.evento(projeto["id"], "O link do ComfyUI não respondeu (Colab desligado ou link velho); "
                                     "as imagens vão pela API (Pollinations).", "aviso")
    refs = preparar_referencias(projeto, canal, h)
    feitas = {r["n"] for r in db.todos("SELECT n FROM cenas WHERE projeto_id = ? AND status = 'ok'", (projeto["id"],))}
    pendentes = [c for c in h["cenas"] if c["n"] not in feitas and not c.get("mesma_imagem_de")]
    copias = sum(1 for c in h["cenas"] if c.get("mesma_imagem_de"))
    db.evento(projeto["id"], f"Gerando {len(pendentes)} cena(s)..."
                             f"{f' ({copias} reaproveitam a imagem anterior com outro movimento)' if copias else ''}")
    erros = []

    def uma(c):
        try:
            gerar_cena(projeto, canal, h, c, refs)
            db.evento(projeto["id"], f"Cena {c['n']} pronta.")
        except custos.OrcamentoEstourado:
            raise
        except Exception as e:
            erros.append((c["n"], str(e)))
            db.evento(projeto["id"], f"Cena {c['n']} falhou: {e}", "erro")

    with ThreadPoolExecutor(max_workers=config.IMAGENS_SIMULTANEAS) as ex:
        list(ex.map(uma, pendentes))
    copiar_reaproveitadas(projeto, h)
    folha_contato(projeto, h)
    if erros:
        db.evento(projeto["id"], f"{len(erros)} cena(s) com erro; use Refazer nelas.", "aviso")
    return h


def copiar_reaproveitadas(projeto: dict, h: dict):
    """Cenas marcadas com mesma_imagem_de recebem uma cópia da imagem da cena de origem (custo zero)."""
    pid = projeto["id"]
    for c in h["cenas"]:
        origem = c.get("mesma_imagem_de")
        if not origem:
            continue
        fonte = db.um("SELECT arquivo, seed FROM cenas WHERE projeto_id = ? AND n = ? AND status = 'ok'", (pid, origem))
        if not fonte or not (config.BASE / fonte["arquivo"]).exists():
            continue
        destino = pasta(projeto) / "cenas" / f"{c['n']:02d}.jpg"
        destino.write_bytes((config.BASE / fonte["arquivo"]).read_bytes())
        db.executar("INSERT INTO cenas (projeto_id, n, status, seed, versao, prompt_final, arquivo) "
                    "VALUES (?, ?, 'ok', ?, 0, ?, ?) ON CONFLICT(projeto_id, n) DO UPDATE SET status='ok', "
                    "seed=excluded.seed, prompt_final=excluded.prompt_final, arquivo=excluded.arquivo, erro=NULL",
                    (pid, c["n"], fonte["seed"], f"mesma imagem da cena {origem}",
                     destino.relative_to(config.BASE).as_posix()))


def refazer(projeto: dict, canal: dict, h: dict, n: int) -> Path:
    c = next(x for x in h["cenas"] if x["n"] == n)
    if c.pop("mesma_imagem_de", None):
        # Pedir para refazer uma cena reaproveitada = ela passa a ter imagem própria.
        db.evento(projeto["id"], f"Cena {n} deixa de reaproveitar a imagem anterior e ganha imagem própria.")
        db.atualizar("projetos", projeto["id"], historia_json=h)
    atual = db.um("SELECT versao FROM cenas WHERE projeto_id = ? AND n = ?", (projeto["id"], n))
    versao = (atual["versao"] + 1) if atual else 0
    # Só usa a ficha/placa criada para ESTE projeto (bib_chave); nunca cai numa entrada antiga com o mesmo id.
    refs = {}
    escolhidas = chaves_referencia(c)
    for tipo, lista in (("personagem", h.get("personagens", [])), ("ambiente", h.get("ambientes", []))):
        for obj in lista:
            if not obj.get("bib_chave"):
                continue
            if tipo == "ambiente" and obj["id"] not in historia.ambientes_recorrentes(h)                     and (tipo, obj["id"]) not in escolhidas:
                continue
            item = db.um("SELECT * FROM biblioteca WHERE canal_id = ? AND tipo = ? AND chave = ?",
                         (canal["id"], tipo, obj["bib_chave"]))
            if item:
                refs[(tipo, obj["id"])] = item
    destino = gerar_cena(projeto, canal, h, c, refs, versao)
    copiar_reaproveitadas(projeto, h)  # quem reaproveita esta cena recebe a imagem nova
    folha_contato(projeto, h)
    return destino


def folha_contato(projeto: dict, h: dict) -> Path | None:
    cenas = db.todos("SELECT n, arquivo FROM cenas WHERE projeto_id = ? AND status = 'ok' ORDER BY n", (projeto["id"],))
    if not cenas:
        return None
    thumbs = []
    for c in cenas:
        im = Image.open(config.BASE / c["arquivo"]).convert("RGB")
        im.thumbnail((360, 360))
        thumbs.append((c["n"], im))
    cw = max(t.width for _, t in thumbs)
    ch = max(t.height for _, t in thumbs)
    cols = 4 if cw < ch else 3
    linhas = (len(thumbs) + cols - 1) // cols
    folha = Image.new("RGB", (cols * (cw + 10) + 10, linhas * (ch + 10) + 10), (15, 15, 18))
    d = ImageDraw.Draw(folha)
    try:
        fonte = ImageFont.truetype("arialbd.ttf", 36)
    except OSError:
        fonte = ImageFont.load_default()
    for i, (n, t) in enumerate(thumbs):
        x, y = 10 + (i % cols) * (cw + 10), 10 + (i // cols) * (ch + 10)
        folha.paste(t, (x, y))
        d.rectangle([x, y, x + 56, y + 46], fill=(0, 0, 0))
        d.text((x + 8, y + 4), str(n), fill=(255, 210, 0), font=fonte)
    destino = pasta(projeto) / "folha_contato.jpg"
    folha.save(destino, "JPEG", quality=85)
    return destino
