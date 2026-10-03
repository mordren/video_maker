"""Aba "Teste Comfy": gera, direto no ComfyUI e com uma história já escrita, a ficha das pessoas, a placa do lugar e a
cena escolhida. Não mexe no projeto (cenas, biblioteca, custos): tudo fica em projetos/<pasta>/teste_comfy/.
As fichas ficam guardadas lá, então testar outra cena com as mesmas pessoas só gera a cena."""
import re
import threading
import time

from . import canais, comfy, config, historia, pipeline, visual

_trava = threading.Lock()
_job: dict = {"estado": "parado", "passos": [], "log": []}
MAX_LOG = 400


def _log(texto: str, passo: dict | None = None):
    """Uma linha no log da aba (com hora) e no journalctl do serviço."""
    hora = time.strftime("%H:%M:%S")
    rotulo = passo["rotulo"] if passo else ""
    _job.setdefault("log", []).append({"hora": hora, "passo": rotulo, "texto": texto})
    del _job["log"][:-MAX_LOG]
    print(f"[teste-comfy] {rotulo + ': ' if rotulo else ''}{texto}", flush=True)


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
            "megapixels": config.COMFY_MEGAPIXELS, "megapixels_ref": config.COMFY_MEGAPIXELS_REF,
            "projetos": projetos(), "job": _job}


def _passo(rotulo: str) -> dict:
    p = {"rotulo": rotulo, "status": "gerando", "arquivo": None, "prompt": "", "segundos": None, "erro": None,
         "inicio": time.time(), "progresso": "começando"}
    _job["passos"].append(p)
    _log("começou", p)
    return p


def _gerar(passo: dict, prompt: str, w: int, hh: int, seed: int, refs: list, destino):
    passo["prompt"] = prompt
    inicio = time.time()

    def aviso(texto: str, progresso: bool = False):
        passo["progresso"] = texto
        m = re.search(r"(\d+)/(\d+)$", texto) if progresso else None
        # O passo da amostragem muda a cada segundo: no log só o 1º, a cada 5 e o último; na tela, todos.
        if not m or int(m[1]) in (1, int(m[2])) or int(m[1]) % 5 == 0:
            _log(texto, passo)

    _log(f"prompt com {len(prompt)} caracteres, {len(refs)} referência(s), seed {seed}", passo)
    try:
        dados = comfy.gerar(prompt, w, hh, seed, refs, aviso)
    except (comfy.ComfyFora, comfy.ErroComfy) as e:
        passo.update(status="erro", erro=str(e), progresso="erro")
        _log(f"ERRO: {e}", passo)
        raise
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(dados)
    passo.update(status="ok", arquivo=_url(destino), segundos=round(time.time() - inicio), progresso="pronta")
    _log(f"salva em {destino.name} ({round(time.time() - inicio)}s)", passo)


def _rodar(projeto: dict, canal: dict, h: dict, cenas: list[dict], refazer_fichas: bool, todas: bool):
    # Prompts no formato do workflow (Kontext ou Qwen), mesmo com o motor dos vídeos na Pollinations.
    with visual.prompts_para(comfy.modelo()):
        _rodar_teste(projeto, canal, h, cenas, refazer_fichas, todas)


class _Parado(Exception):
    pass


def _base(projeto, canal, h, tipo: str, obj: dict, refazer: bool, pasta) -> dict:
    """Ficha (personagem) ou placa (ambiente): gera, ou reaproveita a do teste anterior. Devolve a referência."""
    if _job.get("parar"):
        raise _Parado()
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    nome = "ficha" if tipo == "personagem" else "placa"
    destino = pasta / f"{nome}_{obj['id']}.jpg"
    passo = _passo(f"{nome.capitalize()}: {obj.get('nome') or obj['id']}")
    if destino.exists() and not refazer:
        passo.update(status="ok", arquivo=_url(destino), segundos=0, prompt=f"({nome} guardada do teste anterior)",
                     progresso="reaproveitada")
        _log("reaproveitada do teste anterior (marque Refazer fichas e placas para gerar de novo)", passo)
    elif tipo == "personagem":
        _gerar(passo, visual.prompt_referencia(canal, "personagem", obj["descricao_fixa"]), fmt["ficha_largura"],
               fmt["ficha_altura"], projeto["seed_base"] + h["personagens"].index(obj), [], destino)
    else:
        w, hh = visual.dims_cena(canal)
        _gerar(passo, visual.prompt_referencia(canal, "ambiente", obj["descricao_fixa"]), w, hh,
               projeto["seed_base"] + 500 + h["ambientes"].index(obj), [], destino)
    return {"arquivo": destino.relative_to(config.BASE).as_posix(), "local": True}


def _rodar_teste(projeto: dict, canal: dict, h: dict, cenas: list[dict], refazer_fichas: bool, todas: bool):
    """Mesma ordem do vídeo: primeiro as bases (ficha de cada pessoa, placa de cada lugar), depois cada cena, uma
    imagem por vez, com as bases como referência. Na história inteira as placas seguem a regra do vídeo (só lugar
    que aparece em duas cenas ou mais); no teste de uma cena, o lugar dela sempre ganha placa."""
    pasta = _pasta(projeto)
    fichas = {x["id"]: x for x in h.get("personagens", [])}
    ambientes = {x["id"]: x for x in h.get("ambientes", [])}
    recorrentes = historia.ambientes_recorrentes(h)
    refs, falhas = {}, []
    try:
        pessoas = list(dict.fromkeys(p["id"] for c in cenas
                                     for p in historia.pessoas_cena(c)[:historia.MAX_PESSOAS_CENA] if p["id"] in fichas))
        lugares = list(dict.fromkeys(c["ambiente"] for c in cenas if c.get("ambiente") in ambientes
                                     and (not todas or c["ambiente"] in recorrentes)))
        _log(f"bases: {len(pessoas)} ficha(s) ({', '.join(pessoas) or '-'}) e {len(lugares)} placa(s) "
             f"({', '.join(lugares) or '-'}); depois {len(cenas)} cena(s), uma imagem por vez")
        for i in pessoas:
            refs[("personagem", i)] = _base(projeto, canal, h, "personagem", fichas[i], refazer_fichas, pasta)
        for i in lugares:
            refs[("ambiente", i)] = _base(projeto, canal, h, "ambiente", ambientes[i], refazer_fichas, pasta)

        w, hh = visual.dims_cena(canal)
        for c in cenas:
            if _job.get("parar"):
                raise _Parado()
            prompt, urls = visual.montar_prompt(h, c, canal, refs)
            passo = _passo(f"Cena {c['n']} (com {len(urls)} referência(s))")
            try:
                _gerar(passo, prompt, w, hh, projeto["seed_base"] + c["n"], urls, pasta / f"cena_{c['n']:02d}.jpg")
            except comfy.ErroComfy:
                if not todas:
                    raise
                falhas.append(c["n"])  # uma cena que falha não derruba as outras; o Comfy fora do ar derruba
                _log(f"cena {c['n']} falhou, seguindo para a próxima", passo)
        _job["estado"] = "erro" if falhas else "pronto"
        if falhas:
            _job["erro"] = f"Cenas que falharam: {', '.join(map(str, falhas))} (detalhes no log)."
        fim = f"; falharam as cenas {', '.join(map(str, falhas))}" if falhas else ""
        _log(f"teste terminado em {round(time.time() - _job['inicio'])}s{fim}")
    except _Parado:
        _job.update(estado="erro", erro="Parado a pedido. O que já ficou pronto continua guardado.")
        _log("parado a pedido")
    except (comfy.ComfyFora, comfy.ErroComfy) as e:
        _job.update(estado="erro", erro=str(e))
        _log(f"teste parou com erro: {e}")
    except Exception as e:  # noqa: BLE001 - qualquer falha vai para a tela, não para o log
        _job.update(estado="erro", erro=f"{type(e).__name__}: {e}")
        _log(f"teste parou com erro inesperado: {type(e).__name__}: {e}")
        if _job["passos"] and _job["passos"][-1]["status"] == "gerando":
            _job["passos"][-1].update(status="erro", erro=str(e))


def iniciar(projeto_id: int, n: int, refazer_fichas: bool, todas: bool = False) -> dict:
    """n: a cena do teste; todas=True: a história inteira (as cenas que repetem a imagem da anterior ficam de fora,
    como no vídeo)."""
    projeto = pipeline.obter(projeto_id)
    if not projeto or not projeto.get("historia"):
        raise ValueError("Esse projeto não tem história escrita.")
    h = projeto["historia"]
    if todas:
        cenas = [x for x in h["cenas"] if not x.get("mesma_imagem_de")]
    else:
        cenas = [x for x in h["cenas"] if x["n"] == n]
        if not cenas:
            raise ValueError(f"A história não tem a cena {n}.")
    if not config.comfy_url():
        raise ValueError("Cole o link do Cloudflare e salve antes de gerar.")
    canal = canais.obter(projeto["canal_id"])
    with _trava:
        if _job["estado"] == "gerando":
            raise ValueError("Já tem um teste rodando.")
        _job.clear()
        _job.update(estado="gerando", passos=[], log=[], erro=None, projeto=projeto_id, cena=None if todas else n,
                    inicio=time.time(), parar=False)
        alvo = f"a história inteira ({len(cenas)} cenas)" if todas else f"a cena {n}"
        refazer = ", refazendo fichas e placas" if refazer_fichas else ""
        _log(f"teste de {alvo} do projeto {projeto_id}: Comfy em {config.comfy_url()}, workflow "
             f"{config.comfy_workflow().name}, prompts no formato {comfy.modelo()}{refazer}")
    threading.Thread(target=_rodar, args=(projeto, canal, h, cenas, refazer_fichas, todas), daemon=True).start()
    return _job


def parar() -> dict:
    """Para depois da imagem que está no Comfy agora (a que já começou termina lá)."""
    if _job.get("estado") == "gerando":
        _job["parar"] = True
        _log("pedido para parar: termina a imagem atual e para")
    return _job
