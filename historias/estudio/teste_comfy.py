"""Aba "Teste Comfy": gera, direto no ComfyUI e com uma história já escrita, a ficha das pessoas, a placa do lugar e a
cena escolhida. Não mexe no projeto (cenas, biblioteca, custos): tudo fica em projetos/<pasta>/teste_comfy/.
As fichas ficam guardadas lá, então testar outra cena com as mesmas pessoas só gera a cena."""
import threading
import time

from . import canais, comfy, config, historia, pipeline, visual

_trava = threading.Lock()
_job: dict = {"estado": "parado", "passos": []}


def _pasta(projeto: dict):
    return config.BASE / projeto["pasta"] / "teste_comfy"


def _url(caminho) -> str:
    return "/arquivos/" + caminho.relative_to(config.BASE).as_posix() + f"?t={int(time.time())}"


def projetos() -> list[dict]:
    from . import db
    saida = []
    for p in db.todos("SELECT id, assunto, historia_json FROM projetos WHERE historia_json IS NOT NULL ORDER BY id DESC"):
        h = db.carregar_json(p["historia_json"], {})
        if not h.get("cenas"):
            continue
        saida.append({"id": p["id"], "titulo": h.get("titulo") or p["assunto"],
                      "cenas": [{"n": c["n"], "resumo": (c.get("prompt_imagem") or "")[:90]} for c in h["cenas"]
                                if not c.get("mesma_imagem_de")]})
    return saida


def estado() -> dict:
    return {"url": config.comfy_url(), "workflow": config.comfy_workflow().stem,
            "megapixels": config.COMFY_MEGAPIXELS, "projetos": projetos(), "job": _job}


def _passo(rotulo: str) -> dict:
    p = {"rotulo": rotulo, "status": "gerando", "arquivo": None, "prompt": "", "segundos": None, "erro": None}
    _job["passos"].append(p)
    return p


def _gerar(passo: dict, prompt: str, w: int, hh: int, seed: int, refs: list, destino):
    passo["prompt"] = prompt
    inicio = time.time()
    try:
        dados = comfy.gerar(prompt, w, hh, seed, refs)
    except (comfy.ComfyFora, comfy.ErroComfy) as e:
        passo.update(status="erro", erro=str(e))
        raise
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(dados)
    passo.update(status="ok", arquivo=_url(destino), segundos=round(time.time() - inicio))


def _rodar(projeto: dict, canal: dict, h: dict, c: dict, refazer_fichas: bool):
    pasta = _pasta(projeto)
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    fichas = {x["id"]: x for x in h.get("personagens", [])}
    ambientes = {x["id"]: x for x in h.get("ambientes", [])}
    refs = {}
    try:
        for i, p in enumerate(historia.pessoas_cena(c)[:historia.MAX_PESSOAS_CENA]):
            obj = fichas.get(p["id"])
            if not obj:
                continue
            destino = pasta / f"ficha_{obj['id']}.jpg"
            passo = _passo(f"Ficha: {obj.get('nome') or obj['id']}")
            if destino.exists() and not refazer_fichas:
                passo.update(status="ok", arquivo=_url(destino), segundos=0, prompt="(ficha guardada do teste anterior)")
            else:
                idx = h["personagens"].index(obj)
                _gerar(passo, visual.prompt_referencia(canal, "personagem", obj["descricao_fixa"]),
                       fmt["ficha_largura"], fmt["ficha_altura"], projeto["seed_base"] + idx, [], destino)
            refs[("personagem", obj["id"])] = {"arquivo": destino.relative_to(config.BASE).as_posix(), "local": True}
        amb = ambientes.get(c.get("ambiente"))
        if amb:
            destino = pasta / f"placa_{amb['id']}.jpg"
            passo = _passo(f"Placa: {amb.get('nome') or amb['id']}")
            if destino.exists() and not refazer_fichas:
                passo.update(status="ok", arquivo=_url(destino), segundos=0, prompt="(placa guardada do teste anterior)")
            else:
                idx = h["ambientes"].index(amb)
                w, hh = visual.dims_cena(canal)
                _gerar(passo, visual.prompt_referencia(canal, "ambiente", amb["descricao_fixa"]), w, hh,
                       projeto["seed_base"] + 500 + idx, [], destino)
            refs[("ambiente", amb["id"])] = {"arquivo": destino.relative_to(config.BASE).as_posix(), "local": True}

        prompt, urls = visual.montar_prompt(h, c, canal, refs)
        w, hh = visual.dims_cena(canal)
        passo = _passo(f"Cena {c['n']} (com {len(urls)} referência(s))")
        _gerar(passo, prompt, w, hh, projeto["seed_base"] + c["n"], urls, pasta / f"cena_{c['n']:02d}.jpg")
        _job["estado"] = "pronto"
    except (comfy.ComfyFora, comfy.ErroComfy) as e:
        _job.update(estado="erro", erro=str(e))
    except Exception as e:  # noqa: BLE001 - qualquer falha vai para a tela, não para o log
        _job.update(estado="erro", erro=f"{type(e).__name__}: {e}")
        if _job["passos"] and _job["passos"][-1]["status"] == "gerando":
            _job["passos"][-1].update(status="erro", erro=str(e))


def iniciar(projeto_id: int, n: int, refazer_fichas: bool) -> dict:
    projeto = pipeline.obter(projeto_id)
    if not projeto or not projeto.get("historia"):
        raise ValueError("Esse projeto não tem história escrita.")
    h = projeto["historia"]
    c = next((x for x in h["cenas"] if x["n"] == n), None)
    if not c:
        raise ValueError(f"A história não tem a cena {n}.")
    if not config.comfy_url():
        raise ValueError("Cole o link do Cloudflare e salve antes de gerar.")
    canal = canais.obter(projeto["canal_id"])
    with _trava:
        if _job["estado"] == "gerando":
            raise ValueError("Já tem um teste rodando.")
        _job.clear()
        _job.update(estado="gerando", passos=[], erro=None, projeto=projeto_id, cena=n)
    threading.Thread(target=_rodar, args=(projeto, canal, h, c, refazer_fichas), daemon=True).start()
    return _job
