"""Exporta uma história do estúdio como UM workflow do Comfy para abrir e rodar na interface.

Segue o mesmo roteiro do gerador: ficha de cada personagem, placa de cada ambiente recorrente e uma imagem por cena,
com os mesmos prompts (visual.montar_prompt), as mesmas referências e as mesmas seeds. Serve para testar o Qwen no
Colab sem passar pelo estúdio; em produção quem faz isso é o comfy.gerar, uma imagem por vez.
"""
import json
from pathlib import Path

from . import canais, comfy, config, db, historia, visual

_FIXOS = {"UnetLoaderGGUF", "CLIPLoader", "VAELoader", "LoraLoaderModelOnly", "ModelSamplingAuraFlow", "CFGNorm"}


def _ultima_versao(pid: int) -> dict:
    r = db.um("SELECT historia_json FROM historias WHERE projeto_id = ? ORDER BY versao DESC LIMIT 1", (pid,))
    if not r:
        raise ValueError(f"O projeto {pid} não tem história salva")
    return json.loads(r["historia_json"])


_FIXOS_KONTEXT = {"UnetLoaderGGUF", "DualCLIPLoader", "VAELoader", "LoraLoaderModelOnly"}


class _Grafo:
    """Qwen-Image-Edit: as referências entram como image1..3 no encoder; o LoRA Lightning faz em 4 passos."""
    molde, fixos = "qwen_edit.json", _FIXOS

    def __init__(self):
        molde = json.loads((config.COMFY_PASTA / self.molde).read_text(encoding="utf-8"))
        self.wf = {k: n for k, n in molde.items() if n["class_type"] in self.fixos}
        self._n = 100
        self.iniciar()

    def iniciar(self):
        self.clip, self.vae = comfy._um(self.wf, "CLIPLoader"), comfy._um(self.wf, "VAELoader")
        self.modelo = comfy._um(self.wf, "CFGNorm")

    def _id(self) -> str:
        self._n += 1
        return str(self._n)

    def _no(self, tipo, entradas, titulo=None) -> str:
        i = self._id()
        self.wf[i] = {"class_type": tipo, "inputs": entradas, **({"_meta": {"title": titulo}} if titulo else {})}
        return i

    def imagem(self, titulo, prompt, largura, altura, seed, refs: list[str], prefixo) -> str:
        """Uma geração completa. refs são ids de nós que dão uma imagem já escalada. Devolve esse nó escalado."""
        def codificar(texto, t=None):
            e = {"clip": [self.clip, 0], "vae": [self.vae, 0], "prompt": texto}
            e.update({f"image{i}": [r, 0] for i, r in enumerate(refs, 1)})
            return self._no("TextEncodeQwenImageEditPlus", e, t)
        pos, neg = codificar(comfy._picture(prompt), titulo), codificar("")
        gw, gh = comfy._tamanho(largura, altura)
        vazio = self._no("EmptySD3LatentImage", {"width": gw, "height": gh, "batch_size": 1})
        amostra = self._no("KSampler", {"model": [self.modelo, 0], "seed": int(seed), "steps": 4, "cfg": 1.0,
                                        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                                        "positive": [pos, 0], "negative": [neg, 0], "latent_image": [vazio, 0]})
        dec = self._no("VAEDecode", {"samples": [amostra, 0], "vae": [self.vae, 0]})
        self._no("SaveImage", {"images": [dec, 0], "filename_prefix": prefixo})
        return self._no("ImageScaleToTotalPixels", {"image": [dec, 0], "upscale_method": "lanczos",
                                                     "megapixels": config.COMFY_MEGAPIXELS_REF, "resolution_steps": 1})


class _GrafoKontext(_Grafo):
    """Flux Kontext: cada referência vira uma ReferenceLatent encadeada; passos e guidance vêm do kontext.json."""
    molde, fixos = "kontext.json", _FIXOS_KONTEXT

    def iniciar(self):
        self.clip, self.vae = comfy._um(self.wf, "DualCLIPLoader"), comfy._um(self.wf, "VAELoader")
        self.modelo = comfy._um(self.wf, "LoraLoaderModelOnly") if any(
            n["class_type"] == "LoraLoaderModelOnly" for n in self.wf.values()) else comfy._um(self.wf, "UnetLoaderGGUF")
        molde = json.loads((config.COMFY_PASTA / self.molde).read_text(encoding="utf-8"))
        amostrador = molde[comfy._um(molde, "KSampler")]["inputs"]
        guia = [n for n in molde.values() if n["class_type"] == "FluxGuidance"]
        metodo = [n for n in molde.values() if n["class_type"] == "FluxKontextMultiReferenceLatentMethod"]
        self.passos = int(amostrador.get("steps", 20))
        self.guidance = float(guia[0]["inputs"]["guidance"]) if guia else 2.5
        self.metodo = metodo[0]["inputs"]["reference_latents_method"] if metodo else None

    def imagem(self, titulo, prompt, largura, altura, seed, refs: list[str], prefixo) -> str:
        texto = self._no("CLIPTextEncode", {"clip": [self.clip, 0], "text": prompt}, titulo)
        cond = [texto, 0]
        for r in refs:
            cod = self._no("VAEEncode", {"pixels": [r, 0], "vae": [self.vae, 0]})
            cond = [self._no("ReferenceLatent", {"conditioning": cond, "latent": [cod, 0]}), 0]
        if refs and self.metodo:
            cond = [self._no("FluxKontextMultiReferenceLatentMethod",
                             {"conditioning": cond, "reference_latents_method": self.metodo}), 0]
        guia = self._no("FluxGuidance", {"conditioning": cond, "guidance": self.guidance})
        zero = self._no("ConditioningZeroOut", {"conditioning": [texto, 0]})
        gw, gh = comfy._tamanho(largura, altura)
        vazio = self._no("EmptySD3LatentImage", {"width": gw, "height": gh, "batch_size": 1})
        amostra = self._no("KSampler", {"model": [self.modelo, 0], "seed": int(seed), "steps": self.passos, "cfg": 1.0,
                                        "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0,
                                        "positive": [guia, 0], "negative": [zero, 0], "latent_image": [vazio, 0]})
        dec = self._no("VAEDecode", {"samples": [amostra, 0], "vae": [self.vae, 0]})
        self._no("SaveImage", {"images": [dec, 0], "filename_prefix": prefixo})
        return self._no("ImageScaleToTotalPixels", {"image": [dec, 0], "upscale_method": "lanczos",
                                                     "megapixels": config.COMFY_MEGAPIXELS_REF, "resolution_steps": 1})


def exportar(pid: int, motor: str = "qwen") -> tuple[dict, str]:
    """Devolve (workflow, resumo). Lê a última versão da história do projeto. motor: qwen ou kontext."""
    projeto = db.um("SELECT * FROM projetos WHERE id = ?", (pid,))
    if not projeto:
        raise ValueError(f"Projeto {pid} não existe")
    canal, h = canais.obter(projeto["canal_id"]), _ultima_versao(pid)
    # montar_prompt devolve, no lugar da URL/arquivo, a chave de cada referência: a ordem é a do "Picture N".
    # Os prompts saem no formato do motor (enxuto no Qwen, por descrição no Kontext), como em produção.
    original = visual.ref_envio
    visual.ref_envio = lambda item: item["chave"]
    try:
        with visual.prompts_para(motor):
            return _montar(pid, projeto, canal, h, _GrafoKontext() if motor == "kontext" else _Grafo(), motor)
    finally:
        visual.ref_envio = original


def _montar(pid: int, projeto: dict, canal: dict, h: dict, g, motor: str) -> tuple[dict, str]:
    base = projeto["seed_base"]
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    refs, linhas = {}, []

    usados = {i for c in h["cenas"] for i in historia.personagens_cena(c)}
    for idx, p in enumerate(h.get("personagens", [])):
        if p["id"] in usados:
            refs[("personagem", p["id"])] = g.imagem(
                f"FICHA {p['id']}", visual.prompt_referencia(canal, "personagem", p["descricao_fixa"]),
                fmt["ficha_largura"], fmt["ficha_altura"], base + idx, [], f"hist{pid}_ficha_{p['id']}")
            linhas.append(f"ficha {p['id']}")
    w, hh = visual.dims_cena(canal)
    recorrentes = historia.ambientes_recorrentes(h)
    for idx, a in enumerate(h.get("ambientes", [])):
        if a["id"] in recorrentes:
            refs[("ambiente", a["id"])] = g.imagem(
                f"PLACA {a['id']}", visual.prompt_referencia(canal, "ambiente", a["descricao_fixa"]),
                w, hh, base + 500 + idx, [], f"hist{pid}_placa_{a['id']}")
            linhas.append(f"placa {a['id']}")

    for c in h["cenas"]:
        if c.get("mesma_imagem_de"):
            continue
        itens = {k: {"chave": k} for k in refs}
        prompt, chaves = visual.montar_prompt(h, c, canal, itens)
        if motor == "qwen" and len(chaves) > comfy.MAX_REFS_QWEN:
            raise ValueError(f"Cena {c['n']} pede {len(chaves)} referências; o Qwen aceita {comfy.MAX_REFS_QWEN}")
        g.imagem(f"CENA {c['n']}", prompt, w, hh, base + c["n"], [refs[k] for k in chaves],
                 f"hist{pid}_cena_{c['n']:02d}")
        linhas.append(f"cena {c['n']} ({', '.join(f'{t}:{i}' for t, i in chaves) or 'sem referência'})")
    return g.wf, chr(10).join(linhas)


def salvar(pid: int, destino: Path | None = None, motor: str = "qwen") -> tuple[Path, str]:
    wf, resumo = exportar(pid, motor)
    sufixo = "" if motor == "qwen" else f"_{motor}"
    destino = destino or Path("testes_comfy") / f"historia_{pid}{sufixo}.json"
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_text(json.dumps(wf, ensure_ascii=False, indent=1), encoding="utf-8")
    return destino, resumo
