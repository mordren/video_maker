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


def montar_workflow(prompt: str, largura: int, altura: int, seed: int, refs: list[str],
                    literal: bool = False) -> tuple[dict, str]:
    """Devolve (workflow pronto, id do nó SaveImage). literal=True manda o texto exatamente como veio."""
    wf = copy.deepcopy(_modelo())
    amostrador = wf[_um(wf, "KSampler")]["inputs"]
    qwen = eh_qwen(wf)
    for k in [k for k, n in wf.items() if n.get("class_type") in _NOS_REFERENCIA]:
        del wf[k]
    proximo = max(int(k) for k in wf if k.isdigit()) + 1
    if qwen:
        proximo = _refs_qwen(wf, amostrador, prompt if literal else _picture(prompt), refs, proximo)
    else:
        proximo = _refs_kontext(wf, amostrador, prompt, refs, proximo)

    # Latente vazio do tamanho da cena: a saída não herda o formato da referência (ficha 2:3, cena 9:16).
    latente = str(proximo)
    wf[latente] = {"class_type": "EmptySD3LatentImage",
                   "inputs": {"width": largura, "height": altura, "batch_size": 1}}
    amostrador.update({"latent_image": [latente, 0], "seed": int(seed), "denoise": 1.0})
    return wf, _um(wf, "SaveImage")


def _picture(prompt: str) -> str:
    """O encoder do Qwen numera as imagens como "Picture 1", "Picture 2": o prompt passa a falar igual."""
    return prompt.replace("Reference image", "Picture").replace("reference image", "picture")


def _refs_qwen(wf, amostrador, prompt, refs, proximo) -> int:
    """Qwen-Image-Edit: as referências entram como image1..3 no encoder positivo e no negativo."""
    if len(refs) > MAX_REFS_QWEN:
        raise ErroComfy(f"O Qwen-Image-Edit aceita no máximo {MAX_REFS_QWEN} referências (vieram {len(refs)})")
    positivo, negativo = amostrador["positive"][0], amostrador["negative"][0]
    if wf[positivo].get("class_type") != _QWEN:
        raise ErroComfy(f"No workflow do Qwen, o positive do KSampler precisa vir direto do {_QWEN}")
    wf[positivo]["inputs"]["prompt"] = prompt
    imagens = []
    for nome in refs:
        carregar, escala = str(proximo), str(proximo + 1)
        proximo += 2
        wf[carregar] = {"class_type": "LoadImage", "inputs": {"image": nome}}
        wf[escala] = {"class_type": "ImageScaleToTotalPixels",
                      "inputs": {"image": [carregar, 0], "upscale_method": "lanczos", "megapixels": 1.0}}
        imagens.append([escala, 0])
    for no in {positivo, negativo}:
        if wf[no].get("class_type") != _QWEN:
            continue
        for i in range(1, MAX_REFS_QWEN + 1):
            wf[no]["inputs"].pop(f"image{i}", None)
        for i, img in enumerate(imagens, 1):
            wf[no]["inputs"][f"image{i}"] = img
    return proximo


def _refs_kontext(wf, amostrador, prompt, refs, proximo) -> int:
    """Flux Kontext: uma ReferenceLatent por referência, encadeadas antes do FluxGuidance."""
    vae = wf[_um(wf, "VAEDecode")]["inputs"]["vae"]
    texto = _um(wf, "CLIPTextEncode")
    wf[texto]["inputs"]["text"] = prompt
    cond = [texto, 0]
    for nome in refs:
        ids = [str(proximo + i) for i in range(4)]
        proximo += 4
        wf[ids[0]] = {"class_type": "LoadImage", "inputs": {"image": nome}}
        wf[ids[1]] = {"class_type": "FluxKontextImageScale", "inputs": {"image": [ids[0], 0]}}
        wf[ids[2]] = {"class_type": "VAEEncode", "inputs": {"pixels": [ids[1], 0], "vae": vae}}
        wf[ids[3]] = {"class_type": "ReferenceLatent", "inputs": {"conditioning": cond, "latent": [ids[2], 0]}}
        cond = [ids[3], 0]
    guias = [k for k, n in wf.items() if n.get("class_type") == "FluxGuidance"]
    if guias:
        wf[guias[0]]["inputs"]["conditioning"] = cond
    else:
        amostrador["positive"] = cond
    return proximo


def _ajustar(dados: bytes, largura: int, altura: int) -> bytes:
    im = Image.open(io.BytesIO(dados)).convert("RGB")
    if im.size != (largura, altura):
        im = im.resize((largura, altura), Image.LANCZOS)
    saida = io.BytesIO()
    im.save(saida, "JPEG", quality=95)
    return saida.getvalue()


def gerar(prompt: str, largura: int, altura: int, seed: int, refs: list[Path], literal: bool = False) -> bytes:
    """Gera uma imagem e devolve JPEG já no tamanho pedido."""
    nomes = [_enviar_ref(p) for p in refs]
    gw, gh = _tamanho(largura, altura)
    wf, saida = montar_workflow(prompt, gw, gh, seed, nomes, literal)
    r = _req("POST", "/prompt", json={"prompt": wf, "client_id": _CLIENTE})
    if r.status_code != 200:
        raise ErroComfy(f"Comfy recusou o workflow HTTP {r.status_code}: {r.text[:500]}")
    pid = r.json()["prompt_id"]

    limite = time.time() + config.COMFY_TIMEOUT_S
    while time.time() < limite:
        time.sleep(2)
        hist = _req("GET", f"/history/{pid}").json().get(pid)
        if not hist:
            continue  # ainda na fila ou gerando
        status = hist.get("status") or {}
        if status.get("status_str") == "error":
            erros = [m[1] for m in status.get("messages", []) if m and m[0] == "execution_error"]
            detalhe = erros[0].get("exception_message", "") if erros else str(status)[:300]
            raise ErroComfy(f"Comfy falhou ao gerar: {detalhe.strip()}")
        imagens = (hist.get("outputs", {}).get(saida) or {}).get("images") or []
        if imagens:
            im = imagens[0]
            r = _req("GET", "/view", timeout=120, params={"filename": im["filename"],
                                                          "subfolder": im.get("subfolder", ""),
                                                          "type": im.get("type", "output")})
            if r.status_code != 200:
                raise ErroComfy(f"Não consegui baixar a imagem do Comfy: HTTP {r.status_code}")
            return _ajustar(r.content, largura, altura)
        if status.get("completed"):
            raise ErroComfy("Comfy terminou sem devolver imagem (o SaveImage está ligado?)")
    raise ErroComfy(f"Comfy passou de {config.COMFY_TIMEOUT_S}s sem terminar a imagem")
