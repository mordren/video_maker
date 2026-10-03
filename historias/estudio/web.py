"""Servidor local (FastAPI): interface no navegador + API.

A API externa para gerar vídeo a partir de um assunto é o mesmo POST /api/projetos que a interface usa:
    POST /api/projetos  {"canal": "garras-no-telhado", "assunto": "...", "automatico": true}
Se ESTUDIO_API_TOKEN estiver definido, chamadas de fora do computador precisam do cabeçalho
Authorization: Bearer <token>.
"""
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import canais, config, configuracoes, custos, db, efeitos, gerador, historia, montagem, narracao, pipeline, publicador, registro, relatorio, revisao, teste_comfy, youtube

ESTATICOS = Path(__file__).parent / "static"
INICIO = time.time()
app = FastAPI(title="Estúdio de Histórias em Vídeo")


def codigo_desatualizado() -> bool:
    """True se algum .py do programa mudou depois que o servidor subiu (precisa reiniciar para valer)."""
    arquivos = list(Path(__file__).parent.rglob("*.py")) + [config.CODIGO / "main.py"]
    return any(a.exists() and a.stat().st_mtime > INICIO for a in arquivos)


def autorizar(request: Request):
    if not config.API_TOKEN:
        return
    host = request.client.host if request.client else ""
    if host in ("127.0.0.1", "::1", "localhost"):
        return
    token = request.headers.get("authorization", "").removeprefix("Bearer ").strip() or request.query_params.get("token")
    if token != config.API_TOKEN:
        raise HTTPException(401, "token inválido")


@app.on_event("startup")
def _inicio():
    db.iniciar()
    canais.semear()
    pipeline.iniciar()


@app.exception_handler(ValueError)
async def _erro_valor(_, exc: ValueError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------------------------------------------------------------- páginas e arquivos

@app.get("/")
def index():
    return FileResponse(ESTATICOS / "index.html")


app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")


@app.get("/arquivos/{caminho:path}")
def arquivo(caminho: str):
    alvo = (config.BASE / caminho).resolve()
    raizes = [config.PROJETOS.resolve(), config.BIBLIOTECA.resolve(), config.TRILHAS.resolve(), config.GERADOR_SAIDA.resolve()]
    if not any(alvo.is_relative_to(r) for r in raizes) or not alvo.is_file():
        raise HTTPException(404)
    return FileResponse(alvo, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- status e presets

api = Depends(autorizar)


@app.get("/api/status", dependencies=[api])
def status():
    return {
        "simulacao": config.SIMULACAO,
        "chaves": {"openrouter": bool(config.OPENROUTER_API_KEY), "pollinations": bool(config.POLLINATIONS_API_KEY)},
        "modelos": {"texto": config.MODELO_TEXTO, "juiz": config.MODELO_JUIZ, "imagem": config.MODELO_IMAGEM},
        "ffmpeg": shutil.which(config.FFMPEG) is not None or Path(config.FFMPEG).exists(),
        "humanizer": {"arquivo": str(config.HUMANIZER_SKILL), "existe": config.HUMANIZER_SKILL.exists(),
                      "vocabulario": len(revisao.vocabulario_ia())},
        "teto_laco": config.LACO_TETO_USD,
        "publicador_url": config.PUBLICADOR_URL,
        "desatualizado": codigo_desatualizado(),
        "windows": os.name == "nt",
    }


@app.get("/api/presets", dependencies=[api])
def presets():
    return {"estilos": canais.ESTILOS, "legendas": canais.LEGENDAS, "efeitos": efeitos.listar(),
            "universais": canais.PERGUNTAS_UNIVERSAIS, "padrao": canais.CONFIG_PADRAO,
            "modelos_qwen": canais.MODELOS_QWEN,
            "formatos": {k: v["nome"] for k, v in config.FORMATOS.items()}}


# ---------------------------------------------------------------- configurações globais (aba Configurações)

class RestaurarIn(BaseModel):
    chaves: list[str]


@app.get("/api/configuracoes", dependencies=[api])
def ver_configuracoes():
    """Esquema por grupo com valor atual, padrão, origem (.env, configuracoes.json, ambiente ou padrão) e se vale ao vivo.
    Segredos só voltam como {"definido": bool, "final": "últimos 4"}."""
    return configuracoes.estado()


@app.put("/api/configuracoes", dependencies=[api])
def salvar_configuracoes(dados: dict = Body(...)):
    """Corpo: {chave: valor} só dos campos alterados. Vazio = volta ao padrão (segredo vazio = mantém). Valor inválido: 400
    com o campo, e nada é gravado."""
    return configuracoes.salvar(dados)


@app.post("/api/configuracoes/restaurar", dependencies=[api])
def restaurar_configuracoes(dados: RestaurarIn):
    """Volta ao padrão (tira do .env ou do configuracoes.json). É também o "Apagar" de um segredo."""
    return configuracoes.restaurar(dados.chaves)


@app.post("/api/configuracoes/testar/{servico}", dependencies=[api])
def testar_configuracao(servico: str):
    """Confere, com o que está salvo, se o serviço responde: openrouter (GET /key), comfy ou publicador. Nada que gaste crédito."""
    if servico not in ("openrouter", "comfy", "publicador"):
        raise HTTPException(404, "serviço desconhecido")
    return configuracoes.testar(servico)


# ---------------------------------------------------------------- teste do ComfyUI (aba Teste Comfy)

class TesteComfyIn(BaseModel):
    projeto_id: int
    cena: int = 1
    refazer_fichas: bool = False
    todas: bool = False  # a história inteira: bases e depois todas as cenas, uma imagem por vez


@app.get("/api/teste-comfy", dependencies=[api])
def teste_comfy_estado():
    return teste_comfy.estado()


@app.post("/api/teste-comfy/gerar", dependencies=[api])
def teste_comfy_gerar(dados: TesteComfyIn):
    return teste_comfy.iniciar(dados.projeto_id, dados.cena, dados.refazer_fichas, dados.todas)


@app.get("/api/teste-comfy/progresso", dependencies=[api])
def teste_comfy_progresso():
    return teste_comfy._job


@app.post("/api/teste-comfy/parar", dependencies=[api])
def teste_comfy_parar():
    return teste_comfy.parar()


# ---------------------------------------------------------------- canais

class CanalIn(BaseModel):
    nome: str
    idioma: str = "pt-BR"
    descricao: str = ""
    formato: str = "short"
    config: dict = {}


class SugestaoIn(BaseModel):
    nome: str
    descricao: str
    idioma: str = "pt-BR"
    formato: str = "short"


@app.get("/api/canais", dependencies=[api])
def listar_canais():
    return canais.listar()


@app.get("/api/canais/{cid}", dependencies=[api])
def ver_canal(cid: int):
    c = canais.obter(cid)
    if not c:
        raise HTTPException(404)
    c["biblioteca"] = db.todos("SELECT * FROM biblioteca WHERE canal_id = ? ORDER BY tipo, chave", (cid,))
    return c


@app.post("/api/canais", dependencies=[api])
def criar_canal(dados: CanalIn):
    return {"id": canais.salvar(dados.model_dump())}


@app.put("/api/canais/{cid}", dependencies=[api])
def editar_canal(cid: int, dados: CanalIn):
    if not canais.obter(cid):
        raise HTTPException(404)
    canais.salvar(dados.model_dump(), cid)
    return {"id": cid}


@app.post("/api/canais/previa-prompt", dependencies=[api])
def previa_prompt(dados: CanalIn):
    """Os prompts exatos que o roteirista recebe com a configuração da tela: ganchos e história."""
    d = dados.model_dump()
    canal = {"nome": d["nome"] or "(sem nome)", "idioma": d.get("idioma") or "pt-BR", "descricao": d.get("descricao") or "",
             "formato": d.get("formato") or "short", "config": canais.mesclar(canais.CONFIG_PADRAO, d.get("config") or {})}
    assunto = "<assunto do vídeo>"
    modo = "\n\nModo narrativo deste vídeo: <modo escolhido com os ganchos> (...). Siga-o do começo ao fim." \
        if canal["config"].get("modos_narrativos") else ""
    return {"modelo": config.MODELO_TEXTO, "gancho": historia.mensagens_gancho(assunto, canal),
            "roteirista": [{"role": "system", "content": historia.sistema_escrita(canal)},
                           {"role": "user", "content": f"Assunto: {assunto}\n\n" +
                            historia._pedido_continuacao(canal, "<gancho escolhido pelo Jev>") + modo +
                            "\n\nEscreva a história em JSON."}]}


@app.post("/api/canais/sugerir", dependencies=[api])
def sugerir_canal(dados: SugestaoIn):
    return canais.sugerir(dados.nome, dados.descricao, dados.idioma, dados.formato)


@app.post("/api/canais/{cid}/trilhas", dependencies=[api])
async def enviar_trilha(cid: int, arquivo: UploadFile = File(...)):
    canal = canais.obter(cid)
    if not canal:
        raise HTTPException(404)
    nome = Path(arquivo.filename or "trilha.mp3").name
    if Path(nome).suffix.lower() not in (".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac"):
        raise HTTPException(400, "formato de áudio não suportado")
    destino = config.TRILHAS / canal["slug"] / nome
    destino.parent.mkdir(parents=True, exist_ok=True)
    with open(destino, "wb") as f:
        shutil.copyfileobj(arquivo.file, f)
    rel = destino.relative_to(config.BASE).as_posix()
    if not db.um("SELECT id FROM trilhas WHERE canal_id = ? AND arquivo = ?", (cid, rel)):
        db.executar("INSERT INTO trilhas (canal_id, arquivo, nome, ativa, criado_em) VALUES (?, ?, ?, 1, ?)",
                    (cid, rel, Path(nome).stem, db.agora()))
    return {"ok": True}


class TrilhaIn(BaseModel):
    ativa: bool


@app.patch("/api/trilhas/{tid}", dependencies=[api])
def alternar_trilha(tid: int, dados: TrilhaIn):
    db.executar("UPDATE trilhas SET ativa = ? WHERE id = ?", (int(dados.ativa), tid))
    return {"ok": True}


@app.delete("/api/trilhas/{tid}", dependencies=[api])
def apagar_trilha(tid: int):
    db.executar("DELETE FROM trilhas WHERE id = ?", (tid,))
    return {"ok": True}


@app.get("/api/vozes", dependencies=[api])
def listar_vozes(idioma: str | None = None, provedor: str = "edge", modelo: str | None = None):
    return canais.vozes(idioma, provedor, modelo)


class PreviaIn(BaseModel):
    voz: dict
    texto: str = "O vigia trabalhava naquele prédio havia trinta anos, e nunca tinha sentido medo de nada."


@app.post("/api/vozes/previa", dependencies=[api])
def previa_voz(dados: PreviaIn):
    destino = Path(tempfile.gettempdir()) / f"previa_{uuid.uuid4().hex[:8]}.mp3"
    narracao.previa(dados.voz, dados.texto[:400], destino)
    return FileResponse(destino, media_type="audio/mpeg")


# ---------------------------------------------------------------- projetos

class ProjetoIn(BaseModel):
    canal: str | int
    assunto: str
    automatico: bool = False


@app.get("/api/projetos", dependencies=[api])
def listar_projetos(canal_id: int | None = None):
    sql = ("SELECT p.id, p.assunto, p.status, p.ressalva, p.erro, p.criado_em, p.atualizado_em, p.pasta, p.automatico, "
           "c.nome AS canal, (SELECT COALESCE(SUM(valor_usd),0) FROM custos WHERE projeto_id = p.id) AS custo, "
           "json_extract(p.historia_json, '$.titulo') AS titulo FROM projetos p JOIN canais c ON c.id = p.canal_id")
    if canal_id:
        return db.todos(sql + " WHERE p.canal_id = ? ORDER BY p.id DESC", (canal_id,))
    return db.todos(sql + " ORDER BY p.id DESC")


@app.post("/api/projetos", dependencies=[api])
def criar_projeto(dados: ProjetoIn):
    canal = canais.resolver(dados.canal)
    if not canal:
        raise HTTPException(404, "canal não encontrado (use o id ou o slug)")
    pid = pipeline.criar(canal["id"], dados.assunto, dados.automatico)
    return {"id": pid, "status_url": f"/api/projetos/{pid}"}


def _projeto(pid: int) -> dict:
    p = pipeline.obter(pid)
    if not p:
        raise HTTPException(404)
    return p


@app.get("/api/projetos/{pid}", dependencies=[api])
def ver_projeto(pid: int):
    p = _projeto(pid)
    p.pop("canal_snapshot", None)
    p["cenas"] = db.todos("SELECT * FROM cenas WHERE projeto_id = ? ORDER BY n", (pid,))
    p["versoes"] = []
    for v in db.todos("SELECT * FROM historias WHERE projeto_id = ? ORDER BY versao", (pid,)):
        v["historia"] = db.carregar_json(v.pop("historia_json"))
        v["checagem"] = db.carregar_json(v.pop("checagem_json"))
        v["avaliacao"] = db.carregar_json(v.pop("avaliacao_json"))
        p["versoes"].append(v)
    p["custos"] = custos.resumo(pid)
    pasta = config.BASE / p["pasta"]
    p["arquivos"] = {k: f"/arquivos/{p['pasta']}/{n}" for k, n in
                     (("video", "final.mp4"), ("folha", "folha_contato.jpg"), ("narracao", "narracao.mp3"),
                      ("legendas", "legendas.ass")) if (pasta / n).exists()}
    met = db.um("SELECT dados_json FROM metricas WHERE projeto_id = ?", (pid,))
    p["metricas"] = db.carregar_json(met["dados_json"]) if met else None
    p["publicador"] = db.carregar_json(p.pop("publicador_json", None))
    canal = canais.obter(p["canal_id"])
    p["canal"] = {"id": canal["id"], "nome": canal["nome"], "slug": canal["slug"], "formato": canal["formato"],
                  "trilhas": canal["trilhas"], "publicador_canal": (canal["config"].get("publicador_canal") or "")}
    return p


@app.get("/api/projetos/{pid}/eventos", dependencies=[api])
def eventos(pid: int, depois: int = 0):
    return db.todos("SELECT * FROM eventos WHERE projeto_id = ? AND id > ? ORDER BY id", (pid, depois))


class HistoriaIn(BaseModel):
    historia: dict


@app.put("/api/projetos/{pid}/historia", dependencies=[api])
def editar_historia(pid: int, dados: HistoriaIn):
    p = _projeto(pid)
    if p["status"] in pipeline.EM_ANDAMENTO:
        raise HTTPException(409, "o projeto está em processamento")
    h = dados.historia
    antigas = {c["n"]: c for c in (p["historia"] or {}).get("cenas", [])}
    if len(h.get("cenas", [])) != len(antigas):
        raise HTTPException(400, "a edição não pode mudar o número de cenas")
    h = historia.normalizar_cenas(h)
    db.atualizar("projetos", pid, historia_json=h)
    db.evento(pid, "História editada à mão.")
    return {"ok": True}


class TextoIn(BaseModel):
    titulo: str
    descricao_youtube: str = ""
    narracao: str


@app.put("/api/projetos/{pid}/texto", dependencies=[api])
def editar_texto(pid: int, dados: TextoIn):
    """Parada 1: o texto corrido do narrador. Mudou a narração, as cenas são divididas de novo."""
    p = _projeto(pid)
    if p["status"] in pipeline.EM_ANDAMENTO:
        raise HTTPException(409, "o projeto está em processamento")
    h = p["historia"] or {}
    narracao = " ".join(dados.narracao.split())
    if not narracao:
        raise HTTPException(400, "a narração está vazia")
    if narracao == historia.narracao_completa(h):
        h.update(titulo=dados.titulo.strip(), descricao_youtube=dados.descricao_youtube.strip())
        db.atualizar("projetos", pid, historia_json=h)
        db.evento(pid, "Título e descrição editados à mão.")
        return {"redividindo": False}
    if p["status"] not in ("aguardando_historia", "erro", "interrompido"):
        raise HTTPException(409, "a narração só pode mudar antes das imagens")
    db.evento(pid, "Narração editada à mão. Dividindo em cenas de novo.")
    pipeline.enfileirar(pid, "decupar", narrativa={"titulo": dados.titulo, "descricao_youtube": dados.descricao_youtube,
                                                    "narracao": narracao})
    return {"redividindo": True}


def _acao(pid: int, estados: tuple, acao: str, msg: str, **args):
    p = _projeto(pid)
    if p["status"] not in estados:
        raise HTTPException(409, f"ação indisponível no estado '{p['status']}'")
    db.evento(pid, msg)
    pipeline.enfileirar(pid, acao, **args)
    return {"ok": True}


@app.post("/api/projetos/{pid}/aprovar-historia", dependencies=[api])
def aprovar_historia(pid: int):
    return _acao(pid, ("aguardando_historia",), "imagens", "História aprovada por você. Gerando imagens.")


@app.post("/api/projetos/{pid}/aprovar-imagens", dependencies=[api])
def aprovar_imagens(pid: int):
    p = _projeto(pid)
    faltam = db.um("SELECT COUNT(*) AS n FROM cenas WHERE projeto_id = ? AND status = 'ok'", (pid,))["n"]
    if faltam < len(p["historia"]["cenas"]):
        raise HTTPException(409, "ainda há cenas sem imagem")
    return _acao(pid, ("aguardando_imagens", "pronto", "erro"), "video", "Imagens aprovadas. Narrando e montando.")


@app.post("/api/projetos/{pid}/refazer-historia", dependencies=[api])
def refazer_historia(pid: int):
    return _acao(pid, ("aguardando_historia", "erro", "interrompido"), "historia", "Escrevendo a história de novo.")


@app.post("/api/projetos/{pid}/recomecar", dependencies=[api])
def recomecar(pid: int):
    _projeto(pid)
    try:
        pipeline.recomecar(pid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/projetos/{pid}/refazer-imagens", dependencies=[api])
def refazer_imagens(pid: int):
    _projeto(pid)
    try:
        pipeline.refazer_imagens(pid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.delete("/api/projetos/{pid}", dependencies=[api])
def excluir_projeto(pid: int):
    _projeto(pid)
    try:
        pipeline.excluir(pid)
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


class RefazerIn(BaseModel):
    prompt_imagem: str | None = None
    destaque: str | None = None
    referencias: list[str] | None = None  # ["personagem:id" | "ambiente:id"], no máximo 2, a primeira vale mais


@app.post("/api/projetos/{pid}/cenas/{n}/refazer", dependencies=[api])
def refazer_cena(pid: int, n: int, dados: RefazerIn):
    p = _projeto(pid)
    if p["status"] not in ("aguardando_imagens", "pronto", "erro", "interrompido"):
        raise HTTPException(409, f"ação indisponível no estado '{p['status']}'")
    if dados.prompt_imagem or dados.destaque is not None or dados.referencias is not None:
        for c in p["historia"]["cenas"]:
            if c["n"] == n:
                if dados.referencias is not None:
                    c["referencias"] = dados.referencias
                    historia.normalizar_cenas(p["historia"])
                if dados.prompt_imagem:
                    c["prompt_imagem"] = dados.prompt_imagem.strip()
                if dados.destaque is not None:
                    c["destaque"] = dados.destaque.strip()
        db.atualizar("projetos", pid, historia_json=p["historia"])
    db.evento(pid, f"Refazendo a cena {n}.")
    antes = _estado_arquivo_cena(pid, n)
    _pedidos_refazer[(pid, n)] = {"t": time.time(), "versao": antes["versao"], "sha1": antes["sha1"], "mtime": antes["mtime"]}
    pipeline.enfileirar(pid, "refazer", n=n)
    return {"ok": True, "pedido_em": _pedidos_refazer[(pid, n)]["t"]}


# Diagnóstico do Refazer (não muda a geração): guarda como a imagem estava quando o pedido entrou.
_pedidos_refazer: dict[tuple[int, int], dict] = {}


def _estado_arquivo_cena(pid: int, n: int) -> dict:
    import hashlib
    c = db.um("SELECT status, versao, seed, arquivo, erro FROM cenas WHERE projeto_id = ? AND n = ?", (pid, n))
    saida = {"cena_status": c["status"] if c else None, "versao": c["versao"] if c else None, "seed": c["seed"] if c else None,
             "erro": c["erro"] if c else None, "arquivo": c["arquivo"] if c else None, "mtime": None, "tamanho": None, "sha1": None}
    if c and c["arquivo"] and (config.BASE / c["arquivo"]).exists():
        f = config.BASE / c["arquivo"]
        saida.update(mtime=f.stat().st_mtime, tamanho=f.stat().st_size, sha1=hashlib.sha1(f.read_bytes()).hexdigest()[:12])
    return saida


@app.get("/api/projetos/{pid}/cenas/{n}/status", dependencies=[api])
def status_cena(pid: int, n: int):
    """Estado da (re)geração de uma cena: aguardando | gerando | pronto | erro, mais dados para conferir se a imagem mudou."""
    _projeto(pid)
    atual = _estado_arquivo_cena(pid, n)
    pedido = _pedidos_refazer.get((pid, n))
    mudou = None
    if atual["cena_status"] == "gerando":
        estado = "gerando"
    elif atual["cena_status"] == "erro":
        estado = "erro"
    elif pedido and atual["versao"] == pedido["versao"] and atual["sha1"] == pedido["sha1"]:
        estado = "aguardando"  # pedido aceito, o trabalhador ainda não começou (fila) ou está preparando referências
    else:
        estado = "pronto"
    if pedido and estado in ("pronto", "erro"):
        mudou = atual["sha1"] != pedido["sha1"]
    seg = round(time.time() - pedido["t"], 1) if pedido else None
    if pedido and estado in ("pronto", "erro"):
        _pedidos_refazer.pop((pid, n), None)
    return {"estado": estado, "segundos": seg, "imagem_mudou": mudou, "projeto_status": _projeto(pid)["status"],
            "fila": pipeline._fila.qsize(), **atual}


@app.post("/api/projetos/{pid}/continuar", dependencies=[api])
def continuar(pid: int):
    p = _projeto(pid)
    if p["status"] in pipeline.EM_ANDAMENTO:
        raise HTTPException(409, "já está em processamento")
    pipeline.continuar(pid)
    return {"ok": True}


class TrilhaProjetoIn(BaseModel):
    arquivo: str | None = None


@app.put("/api/projetos/{pid}/trilha", dependencies=[api])
def trilha_projeto(pid: int, dados: TrilhaProjetoIn):
    _projeto(pid)
    db.atualizar("projetos", pid, trilha=dados.arquivo or None)
    return {"ok": True}


class EfeitoTesteIn(BaseModel):
    efeito: str
    projeto_id: int
    n: int = 1
    tensao: int = 3


@app.post("/api/efeitos/teste", dependencies=[api])
def testar_efeito(dados: EfeitoTesteIn):
    p = _projeto(dados.projeto_id)
    pasta = config.BASE / p["pasta"]
    img = pasta / "cenas" / f"{dados.n:02d}.jpg"
    if not img.exists():
        raise HTTPException(404, "a cena não tem imagem")
    canal = canais.obter(p["canal_id"])
    destino = pasta / "testes" / f"{dados.efeito}_{dados.n:02d}.mp4"
    montagem.testar_efeito(dados.efeito, img, destino, canal["formato"], dados.tensao)
    return {"url": f"/arquivos/{p['pasta']}/testes/{destino.name}"}


@app.get("/api/custos", dependencies=[api])
def custos_gerais():
    return {
        "total": db.um("SELECT COALESCE(SUM(valor_usd),0) AS t FROM custos")["t"],
        "por_servico": db.todos("SELECT servico, modelo, etapa, COUNT(*) AS chamadas, SUM(valor_usd) AS valor, "
                                "MIN(real) AS todos_reais FROM custos GROUP BY servico, modelo, etapa ORDER BY valor DESC"),
    }


# ---------------------------------------------------------------- gerador de vídeo (playlists lo-fi 16:9)

class GeradorIn(BaseModel):
    config: dict = {}
    faixas: list[str] = []


def _gerador(fn, *args):
    try:
        return fn(*args)
    except gerador.Ocupado as e:
        raise HTTPException(409, str(e))
    except gerador.ErroGerador as e:
        raise HTTPException(400, str(e))


@app.get("/api/gerador", dependencies=[api])
def gerador_estado():
    return gerador.estado()


@app.put("/api/gerador/config", dependencies=[api])
def gerador_salvar(dados: GeradorIn):
    gerador.salvar(dados.config, dados.faixas)
    return {"ok": True}


@app.get("/api/gerador/fontes", dependencies=[api])
def gerador_fontes():
    return gerador.fontes()


@app.post("/api/gerador/musicas", dependencies=[api])
def gerador_enviar_musicas(arquivos: list[UploadFile] = File(...)):
    """Arquivos arrastados do Explorer: são copiados para a pasta de músicas."""
    return [_gerador(gerador.receber, a.filename, a.file, "musicas") for a in arquivos]


@app.post("/api/gerador/clipes", dependencies=[api])
def gerador_enviar_clipe(arquivo: UploadFile = File(...)):
    return _gerador(gerador.receber, arquivo.filename, arquivo.file, "videos")


@app.get("/api/gerador/miniatura/{nome}", dependencies=[api])
def gerador_miniatura(nome: str):
    return FileResponse(_gerador(gerador.miniatura, nome), headers={"Cache-Control": "max-age=3600"})


@app.post("/api/gerador/abrir/{pasta}", dependencies=[api])
def gerador_abrir(pasta: str):
    _gerador(gerador.abrir_pasta, pasta)
    return {"ok": True}


@app.post("/api/gerador/escolher-pasta/{qual}", dependencies=[api])
def gerador_escolher_pasta(qual: str):
    """Abre a janela de escolha de pasta do Windows (o servidor roda neste computador) e espera o usuário. Não salva:
    a tela põe o caminho no campo e o salvamento automático cuida do resto. {"pasta": null} = cancelou."""
    return {"pasta": _gerador(gerador.escolher, qual)}


@app.get("/api/gerador/saida/{nome}", dependencies=[api])
def gerador_saida(nome: str):
    """Vídeo ou capítulos da pasta de saída escolhida (ela pode ficar fora da pasta do programa)."""
    alvo = gerador.pasta("saida") / Path(nome).name
    if alvo.suffix.lower() not in (".mp4", ".txt") or not alvo.is_file():
        raise HTTPException(404)
    return FileResponse(alvo, headers={"Cache-Control": "no-store"})


@app.post("/api/gerador/previa", dependencies=[api])
def gerador_previa(dados: GeradorIn):
    return _gerador(gerador.iniciar, "previa", dados.config, dados.faixas)


@app.post("/api/gerador/renderizar", dependencies=[api])
def gerador_renderizar(dados: GeradorIn):
    return _gerador(gerador.iniciar, "video", dados.config, dados.faixas)


@app.get("/api/gerador/progresso", dependencies=[api])
def gerador_progresso():
    return gerador.progresso()


@app.post("/api/gerador/cancelar", dependencies=[api])
def gerador_cancelar():
    _gerador(gerador.cancelar)
    return {"ok": True}


@app.get("/api/gerador/previa.mp4", dependencies=[api])
def gerador_previa_video():
    if not gerador.ARQ_PREVIA.exists():
        raise HTTPException(404, "ainda não há prévia")
    return FileResponse(gerador.ARQ_PREVIA, media_type="video/mp4", headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------- logs

@app.get("/api/logs/historico", dependencies=[api])
def log_historico():
    arqs = relatorio.exportar()
    return FileResponse(arqs["historico"], filename=arqs["historico"].name, media_type="text/plain; charset=utf-8")


@app.get("/api/logs/resumo", dependencies=[api])
def log_resumo():
    arqs = relatorio.exportar()
    return FileResponse(arqs["resumo"], filename="resumo_videos.jsonl", media_type="application/x-ndjson")


@app.get("/api/logs/atual", dependencies=[api])
def log_atual():
    if not registro.ARQUIVO.exists():
        raise HTTPException(404, "ainda não há log")
    return FileResponse(registro.ARQUIVO, filename="estudio.log", media_type="text/plain; charset=utf-8")


# ---------------------------------------------------------------- Publicador

@app.post("/api/projetos/{pid}/publicador", dependencies=[api])
def enviar_publicador(pid: int):
    p = _projeto(pid)
    if p["status"] in pipeline.EM_ANDAMENTO:
        raise HTTPException(409, "o projeto está em processamento")
    canal = canais.obter(p["canal_id"])
    try:
        return publicador.enviar(p, canal)
    except publicador.ErroPublicador as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- YouTube

def _yt(fn, *args):
    try:
        return fn(*args)
    except youtube.ErroYouTube as e:
        raise HTTPException(400, str(e))


@app.get("/api/youtube/status", dependencies=[api])
def yt_status():
    return {**youtube.status(), "arquivo_cliente": str(youtube.ARQ_CLIENTE)}


@app.get("/api/youtube/conectar", dependencies=[api])
def yt_conectar(request: Request):
    return RedirectResponse(_yt(youtube.url_login, str(request.base_url)))


@app.get("/api/youtube/retorno", dependencies=[api])
def yt_retorno(request: Request, state: str = "", error: str | None = None):
    if error:
        return RedirectResponse(f"/#/desempenho?erro={error}")
    _yt(youtube.concluir_login, str(request.url), state)
    return RedirectResponse("/#/desempenho")


@app.post("/api/youtube/desconectar", dependencies=[api])
def yt_desconectar():
    youtube.desconectar()
    return {"ok": True}


@app.post("/api/youtube/vincular-auto", dependencies=[api])
def yt_vincular():
    return {"ligados": _yt(youtube.vincular_por_titulo)}


class AtualizarIn(BaseModel):
    projeto_id: int | None = None


@app.post("/api/youtube/atualizar", dependencies=[api])
def yt_atualizar(dados: AtualizarIn):
    return {"resultados": _yt(youtube.atualizar, dados.projeto_id)}


class YoutubeIn(BaseModel):
    url: str | None = None


@app.put("/api/projetos/{pid}/youtube", dependencies=[api])
def yt_ligar(pid: int, dados: YoutubeIn):
    _projeto(pid)
    vid = youtube.extrair_id(dados.url) if dados.url else None
    if dados.url and not vid:
        raise HTTPException(400, "não reconheci o link do YouTube")
    db.executar("UPDATE projetos SET youtube_id = ? WHERE id = ?", (vid, pid))
    if not vid:
        db.executar("DELETE FROM metricas WHERE projeto_id = ?", (pid,))
    db.evento(pid, f"Ligado ao vídeo do YouTube {vid}." if vid else "Desligado do YouTube.")
    return {"youtube_id": vid}


@app.get("/api/desempenho", dependencies=[api])
def desempenho():
    linhas = youtube.painel()
    return {"linhas": linhas, "correlacao_gancho_3s": youtube.correlacao(linhas),
            "correlacao_geral_percentual": youtube.correlacao(linhas, "nota_geral", "media_percentual")}
