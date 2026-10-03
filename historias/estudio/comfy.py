"""ComfyUI (no Colab, via túnel) como motor de imagem, com a ficha/placa como referência.

O workflow vem de um JSON exportado no Comfy com "Save (API format)" (pasta estudio/comfy; COMFY_WORKFLOW no .env
escolhe qual). Dois tipos, reconhecidos pelos nós do arquivo:
- Qwen-Image-Edit (TextEncodeQwenImageEditPlus): cada referência vira image1, image2... nos dois encoders.
- Flux Kontext (ReferenceLatent): cada referência vira uma ReferenceLatent encadeada.
Os nós são achados pelo tipo, então dá para reexportar o arquivo à vontade (modelo, passos, LoRA). O código só põe
as referências, o prompt, a seed e um latente vazio do tamanho da cena.
"""
import copy
import hashlib
import io
import json
import math
import re
import threading
import time
import uuid
from pathlib import Path

import httpx
from PIL import Image

from . import config


class ComfyFora(Exception):
    """Servidor desligado ou túnel caído: dá para cair na Pollinations."""


class ErroComfy(Exception):
    """O Comfy respondeu, mas recusou ou quebrou o workflow (erro de configuração, não de conexão)."""


# O ngrok grátis mostra uma página de aviso sem este cabeçalho; o cloudflared ignora.
_CABECALHOS = {"ngrok-skip-browser-warning": "1"}
_CLIENTE = uuid.uuid4().hex
# Nós da referência no molde: saem todos e são recriados, um conjunto por imagem enviada.
_NOS_REFERENCIA = {"LoadImage", "FluxKontextImageScale", "ImageScaleToTotalPixels", "VAEEncode", "ReferenceLatent"}
_QWEN = "TextEncodeQwenImageEditPlus"
MAX_REFS_QWEN = 3


def _req(metodo: str, caminho: str, timeout: float = 60, **kw) -> httpx.Response:
    base = config.comfy_url()
    if not base:
        raise ComfyFora("COMFY_URL não configurada no .env")
    try:
        r = httpx.request(metodo, base + caminho, headers=_CABECALHOS, timeout=timeout, **kw)
    except (httpx.TimeoutException, httpx.TransportError) as e:
        raise ComfyFora(f"Comfy sem resposta em {base}: {e}") from e
    if r.status_code in (502, 503, 504, 530):  # túnel de pé, Colab desligado
        raise ComfyFora(f"Comfy fora do ar (HTTP {r.status_code})")
    return r


def no_ar() -> bool:
    try:
        return _req("GET", "/system_stats", timeout=10).status_code == 200
    except ComfyFora:
        return False


def _enviar_ref(caminho: Path) -> str:
    """Sobe a ficha/placa para a pasta input do Comfy. O nome é o hash do conteúdo: a mesma ficha não duplica."""
    dados = Path(caminho).read_bytes()
    nome = f"estudio_{hashlib.sha1(dados).hexdigest()[:16]}{Path(caminho).suffix or '.jpg'}"
    r = _req("POST", "/upload/image", timeout=120, files={"image": (nome, dados, "image/jpeg")},
             data={"overwrite": "true"})
    if r.status_code != 200:
        raise ErroComfy(f"Upload da referência falhou HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    return f"{j['subfolder']}/{j['name']}" if j.get("subfolder") else j["name"]


def _tamanho(largura: int, altura: int) -> tuple[int, int]:
    """Mesma proporção da cena, com a área perto de COMFY_MEGAPIXELS (múltiplos de 16). A ampliação vem depois."""
    fator = min(1.0, math.sqrt(config.COMFY_MEGAPIXELS * 1_000_000 / (largura * altura)))
    return int(round(largura * fator / 16) * 16), int(round(altura * fator / 16) * 16)


def _modelo() -> dict:
    caminho = config.comfy_workflow()
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ErroComfy(f"Workflow do Comfy ilegível ({caminho}): {e}") from e


def _um(wf: dict, tipo: str) -> str:
    ids = [k for k, n in wf.items() if n.get("class_type") == tipo]
    if not ids:
        raise ErroComfy(f"O workflow {config.comfy_workflow().name} não tem nó {tipo}")
    return ids[0]


def eh_qwen(wf: dict | None = None) -> bool:
    return any(n.get("class_type") == _QWEN for n in (wf or _modelo()).values())


def modelo() -> str:
    """Quem lê o prompt no workflow configurado: "qwen" ou "kontext"."""
    return "qwen" if eh_qwen() else "kontext"


def montar_workflow(prompt: str, largura: int, altura: int, seed: int, refs: list[str]) -> tuple[dict, str]:
    """Devolve (workflow pronto, id do nó SaveImage)."""
    wf = copy.deepcopy(_modelo())
    amostrador = wf[_um(wf, "KSampler")]["inputs"]
    qwen = eh_qwen(wf)
    # Escala das referências no Kontext: a do molde. FluxKontextImageScale leva cada uma a ~1 MP (o tamanho do
    # treino, mais lento); ImageScaleToTotalPixels usa COMFY_MEGAPIXELS_REF.
    escala_kontext = any(n.get("class_type") == "FluxKontextImageScale" for n in wf.values())
    for k in [k for k, n in wf.items() if n.get("class_type") in _NOS_REFERENCIA]:
        del wf[k]
    proximo = max(int(k) for k in wf if k.isdigit()) + 1
    if qwen:
        proximo = _refs_qwen(wf, amostrador, prompt, refs, proximo)
    else:
        proximo = _refs_kontext(wf, amostrador, prompt, refs, proximo, escala_kontext)

    # Latente vazio do tamanho da cena: a saída não herda o formato da referência (ficha 2:3, cena 9:16).
    vazios = [k for k, n in wf.items() if n.get("class_type") == "EmptySD3LatentImage"]
    latente = vazios[0] if vazios else str(proximo)
    wf[latente] = {**wf.get(latente, {}), "class_type": "EmptySD3LatentImage",
                   "inputs": {"width": largura, "height": altura, "batch_size": 1}}
    amostrador.update({"latent_image": [latente, 0], "seed": int(seed), "denoise": 1.0})
    return wf, _um(wf, "SaveImage")


_NEGATIVA = re.compile("(?:no|without) ", re.IGNORECASE)


def _sem_negativas(prompt: str) -> str:
    """Tira os trechos "no red eyes", "no text", "no people". O Qwen não entende negação: citar a coisa a põe na
    imagem (olhos vermelhos apareceram por causa de "no red eyes"), e com cfg 1 o encoder negativo não tem efeito."""
    saida = []
    for parte in re.split(r"(?<=[.,]) ", prompt):
        if _NEGATIVA.match(parte):
            if parte.endswith(".") and saida and saida[-1].endswith(","):  # fim de frase: a anterior fecha com ponto
                saida[-1] = saida[-1][:-1] + "."
            continue
        saida.append(parte)
    return re.sub(r"[ ,]+$", "", " ".join(saida)).strip()


# "Setting: the same place as picture N (same layout, ...): <descrição>": para o Qwen a placa já mostra o lugar, e
# "same layout" faz a cena herdar o enquadramento dela (armários e porta nos mesmos cantos, pose ignorada).
_LUGAR = re.compile(r"Setting: the same place as reference image (\d+) \([^)]*\): [^.]*\.?")


def _picture(prompt: str) -> str:
    """O encoder do Qwen numera as imagens como "Picture 1", "Picture 2": o prompt passa a falar igual."""
    prompt = _LUGAR.sub(lambda m: f"The room looks like picture {m.group(1)} (same objects and materials), but the "
                                  f"camera angle and framing are new, as described above.", prompt)
    prompt = prompt.replace("Reference image", "Picture").replace("reference image", "picture")
    return _sem_negativas(prompt)


def _refs_qwen(wf, amostrador, prompt, refs, proximo) -> int:
    """Qwen-Image-Edit: as referências entram como image1..3 no encoder positivo e no negativo."""
    if len(refs) > MAX_REFS_QWEN:
        raise ErroComfy(f"O Qwen-Image-Edit aceita no máximo {MAX_REFS_QWEN} referências (vieram {len(refs)})")
    positivo, negativo = amostrador["positive"][0], amostrador["negative"][0]
    if wf[positivo].get("class_type") != _QWEN:
        raise ErroComfy(f"No workflow do Qwen, o positive do KSampler precisa vir direto do {_QWEN}")
    wf[positivo]["inputs"]["prompt"] = _picture(prompt)
    imagens = []
    for nome in refs:
        carregar, escala = str(proximo), str(proximo + 1)
        proximo += 2
        wf[carregar] = {"class_type": "LoadImage", "inputs": {"image": nome}}
        wf[escala] = {"class_type": "ImageScaleToTotalPixels",
                      "inputs": {"image": [carregar, 0], "upscale_method": "lanczos",
                                 "megapixels": config.COMFY_MEGAPIXELS_REF, "resolution_steps": 1}}
        imagens.append([escala, 0])
    for no in {positivo, negativo}:
        if wf[no].get("class_type") != _QWEN:
            continue
        for i in range(1, MAX_REFS_QWEN + 1):
            wf[no]["inputs"].pop(f"image{i}", None)
        for i, img in enumerate(imagens, 1):
            wf[no]["inputs"][f"image{i}"] = img
    return proximo


def _refs_kontext(wf, amostrador, prompt, refs, proximo, escala_kontext=False) -> int:
    """Flux Kontext: uma ReferenceLatent por referência, encadeadas; depois o método de multi-referência (se o
    molde tiver) e o FluxGuidance. O Kontext não numera as imagens: o prompt fala de cada uma pelo que ela mostra."""
    vae = wf[_um(wf, "VAEDecode")]["inputs"]["vae"]
    texto = _um(wf, "CLIPTextEncode")
    wf[texto]["inputs"]["text"] = prompt
    cond = [texto, 0]
    for nome in refs:
        ids = [str(proximo + i) for i in range(4)]
        proximo += 4
        wf[ids[0]] = {"class_type": "LoadImage", "inputs": {"image": nome}}
        wf[ids[1]] = ({"class_type": "FluxKontextImageScale", "inputs": {"image": [ids[0], 0]}} if escala_kontext else
                      {"class_type": "ImageScaleToTotalPixels",
                       "inputs": {"image": [ids[0], 0], "upscale_method": "lanczos",
                                  "megapixels": config.COMFY_MEGAPIXELS_REF, "resolution_steps": 1}})
        wf[ids[2]] = {"class_type": "VAEEncode", "inputs": {"pixels": [ids[1], 0], "vae": vae}}
        wf[ids[3]] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": cond, "latent": [ids[2], 0]}}
        cond = [ids[3], 0]
    for tipo in ("FluxKontextMultiReferenceLatentMethod", "FluxGuidance"):
        nos = [k for k, n in wf.items() if n.get("class_type") == tipo]
        if nos:
            wf[nos[0]]["inputs"]["conditioning"] = cond
            cond = [nos[0], 0]
    amostrador["positive"] = cond
    return proximo


def _ajustar(dados: bytes, largura: int, altura: int) -> bytes:
    im = Image.open(io.BytesIO(dados)).convert("RGB")
    if im.size != (largura, altura):
        im = im.resize((largura, altura), Image.LANCZOS)
    saida = io.BytesIO()
    im.save(saida, "JPEG", quality=95)
    return saida.getvalue()


def _calado(texto: str, progresso: bool = False):
    pass


def _nome_no(n: dict) -> str:
    titulo = (n.get("_meta") or {}).get("title")
    return f"{n['class_type']} ({titulo})" if titulo else n["class_type"]


def _resumo(wf: dict, gw: int, gh: int, largura: int, altura: int, seed: int, refs: int) -> str:
    """Uma linha com o que vai para o Comfy: modelo, tamanho, passos, guidance e como entram as referências."""
    def valor(tipo, campo):
        return next((n["inputs"].get(campo) for n in wf.values() if n["class_type"] == tipo), None)
    modelo = valor("UnetLoaderGGUF", "unet_name") or valor("UNETLoader", "unet_name") or "?"
    partes = [f"workflow {config.comfy_workflow().name} ({len(wf)} nós)", f"modelo {modelo}",
              f"saída {gw}x{gh} (vira {largura}x{altura} depois)", f"seed {seed}",
              f"{valor('KSampler', 'steps')} passos {valor('KSampler', 'sampler_name')}/{valor('KSampler', 'scheduler')}"]
    if valor("FluxGuidance", "guidance") is not None:
        partes.append(f"guidance {valor('FluxGuidance', 'guidance')}")
    if refs:
        mp = valor("ImageScaleToTotalPixels", "megapixels")
        metodo = valor("FluxKontextMultiReferenceLatentMethod", "reference_latents_method")
        partes.append(f"{refs} referência(s) a {mp} MP" if mp else f"{refs} referência(s) a ~1 MP (FluxKontextImageScale)")
        if metodo:
            partes.append(f"multi-referência {metodo}")
    else:
        partes.append("sem referência (só texto)")
    return ", ".join(partes)


def _erro_validacao(texto: str) -> str:
    """O 400 do Comfy em linhas legíveis: nó, tipo e o que falta ou está errado."""
    try:
        j = json.loads(texto)
    except ValueError:
        return texto[:500]
    linhas = [j.get("error", {}).get("message", "")]
    for no, info in (j.get("node_errors") or {}).items():
        for e in info.get("errors", []):
            linhas.append(f"nó {no} ({info.get('class_type')}): {e.get('message')} {e.get('details', '')}".strip())
    return "; ".join(l for l in linhas if l)[:1500]


def _ao_vivo(ids: dict, wf: dict, aviso, parar: threading.Event):
    """Escuta o websocket do Comfy (o mesmo da interface dele): começo, cada nó executado e o passo da amostragem.
    Se não conectar (túnel sem websocket, biblioteca ausente), o acompanhamento segue só pelo /queue e /history."""
    try:
        from websockets.sync.client import connect
    except ImportError:
        aviso("sem acompanhamento passo a passo (biblioteca websockets ausente)")
        return
    url = re.sub(r"^http", "ws", config.comfy_url()) + f"/ws?clientId={_CLIENTE}"
    try:
        with connect(url, additional_headers=_CABECALHOS, open_timeout=15, max_size=None) as ws:
            ids["ws"] = True
            while not parar.is_set():
                try:
                    msg = ws.recv(timeout=1)
                except TimeoutError:
                    continue
                if not isinstance(msg, str):
                    continue  # prévia da imagem em binário
                ev = json.loads(msg)
                d = ev.get("data") or {}
                if ids.get("pid") and d.get("prompt_id") not in (None, ids["pid"]):
                    continue  # outra geração do mesmo estúdio
                tipo = ev.get("type")
                if tipo == "execution_start":
                    aviso("Comfy começou a executar")
                elif tipo == "execution_cached" and d.get("nodes"):
                    aviso(f"{len(d['nodes'])} nó(s) reaproveitados do cache (modelo e encoders já carregados)")
                elif tipo == "executing" and d.get("node") and d["node"] in wf:
                    aviso(f"executando nó {d['node']}: {_nome_no(wf[d['node']])}")
                elif tipo == "progress":
                    aviso(f"amostragem: passo {d.get('value')}/{d.get('max')}", True)
    except Exception as e:  # noqa: BLE001 - o acompanhamento ao vivo é opcional
        if not parar.is_set():
            aviso(f"acompanhamento ao vivo caiu ({type(e).__name__}: {e}); seguindo pelo histórico")


def _posicao_fila(pid: str) -> str | None:
    try:
        q = _req("GET", "/queue", timeout=20).json()
    except (ComfyFora, ValueError):
        return None
    if any(item[1] == pid for item in q.get("queue_running", [])):
        return "rodando"
    pendentes = sorted(q.get("queue_pending", []), key=lambda item: item[0])
    for i, item in enumerate(pendentes):
        if item[1] == pid:
            return f"na fila, {i + len(q.get('queue_running', []))} na frente"
    return None


def gerar(prompt: str, largura: int, altura: int, seed: int, refs: list[Path], aviso=_calado) -> bytes:
    """Gera uma imagem e devolve JPEG já no tamanho pedido. aviso(texto, progresso=False) recebe cada etapa
    (a aba Teste Comfy mostra na tela); progresso=True marca o passo da amostragem, que muda a cada segundo."""
    inicio = time.time()
    nomes = []
    for p in refs:
        with Image.open(p) as im:
            dims = f"{im.width}x{im.height}"
        nomes.append(_enviar_ref(p))
        aviso(f"referência enviada: {Path(p).name} ({dims}, {Path(p).stat().st_size // 1024} KB) como {nomes[-1]}")
    gw, gh = _tamanho(largura, altura)
    wf, saida = montar_workflow(prompt, gw, gh, seed, nomes)
    aviso(_resumo(wf, gw, gh, largura, altura, seed, len(nomes)))

    ids, parar = {}, threading.Event()
    escuta = threading.Thread(target=_ao_vivo, args=(ids, wf, aviso, parar), daemon=True)
    escuta.start()
    try:
        r = _req("POST", "/prompt", json={"prompt": wf, "client_id": _CLIENTE})
        if r.status_code != 200:
            raise ErroComfy(f"Comfy recusou o workflow HTTP {r.status_code}: {_erro_validacao(r.text)}")
        pid = ids["pid"] = r.json()["prompt_id"]
        aviso(f"workflow aceito pelo Comfy (prompt {pid[:8]})")

        limite = time.time() + config.COMFY_TIMEOUT_S
        fila = None
        while time.time() < limite:
            time.sleep(2)
            hist = _req("GET", f"/history/{pid}").json().get(pid)
            if not hist:  # ainda na fila ou gerando
                agora = _posicao_fila(pid)
                if agora and agora != fila and not (agora == "rodando" and ids.get("ws")):
                    aviso(agora)
                fila = agora or fila
                continue
            status = hist.get("status") or {}
            if status.get("status_str") == "error":
                erros = [m[1] for m in status.get("messages", []) if m and m[0] == "execution_error"]
                if erros:
                    e = erros[0]
                    detalhe = (f"nó {e.get('node_id')} ({e.get('node_type')}): {e.get('exception_type', '')} "
                               f"{e.get('exception_message', '')}")
                else:
                    detalhe = str(status)[:300]
                raise ErroComfy(f"Comfy falhou ao gerar: {detalhe.strip()}")
            imagens = (hist.get("outputs", {}).get(saida) or {}).get("images") or []
            if imagens:
                im = imagens[0]
                aviso(f"imagem pronta no Comfy em {time.time() - inicio:.0f}s, baixando {im['filename']}")
                r = _req("GET", "/view", timeout=120, params={"filename": im["filename"],
                                                              "subfolder": im.get("subfolder", ""),
                                                              "type": im.get("type", "output")})
                if r.status_code != 200:
                    raise ErroComfy(f"Não consegui baixar a imagem do Comfy: HTTP {r.status_code}")
                aviso(f"baixada ({len(r.content) // 1024} KB); total {time.time() - inicio:.0f}s")
                return _ajustar(r.content, largura, altura)
            if status.get("completed"):
                raise ErroComfy("Comfy terminou sem devolver imagem (o SaveImage está ligado?)")
        raise ErroComfy(f"Comfy passou de {config.COMFY_TIMEOUT_S}s sem terminar a imagem")
    finally:
        parar.set()


def baixar_saidas(destino: Path, prefixo: str = "") -> list[Path]:
    """Baixa as imagens que o Comfy salvou (SaveImage), as mesmas da pasta output do Colab, pelo histórico dele.
    Só vem o que ainda está no histórico da sessão; o que já foi baixado não é baixado de novo."""
    destino = Path(destino)
    destino.mkdir(parents=True, exist_ok=True)
    r = _req("GET", "/history", timeout=120)
    if r.status_code != 200:
        raise ErroComfy(f"Não consegui ler o histórico do Comfy: HTTP {r.status_code}")
    baixadas = []
    for item in r.json().values():
        for saida in (item.get("outputs") or {}).values():
            for im in saida.get("images") or []:
                if im.get("type", "output") != "output" or not im["filename"].startswith(prefixo):
                    continue
                arquivo = destino / im["filename"]
                if arquivo.exists():
                    continue
                dados = _req("GET", "/view", timeout=120, params={"filename": im["filename"],
                                                                  "subfolder": im.get("subfolder", ""),
                                                                  "type": "output"})
                if dados.status_code != 200:
                    raise ErroComfy(f"Não consegui baixar {im['filename']}: HTTP {dados.status_code}")
                arquivo.write_bytes(dados.content)
                baixadas.append(arquivo)
    return baixadas
