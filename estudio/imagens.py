"""Formato "imagens": fotos do assunto em cima, o corte 16:9 embaixo — e as
fotos TROCAM ao longo do corte (a mesma foto parada do começo ao fim deixava
o vídeo monótono, e a foto única do Wikidata às vezes vinha pequena demais).

Receita, do texto ao vídeo:

1. Quem aparece: o DeepSeek lê gancho + comentário + a fala do corte (com os
   tempos) e lista até 4 pessoas/organizações/lugares — o 1º é o assunto
   principal — e como cada um é falado ("Moraes", "Xandão").
2. De onde vêm as fotos: Wikimedia Commons, pelas fotos marcadas como
   "retrata" (P180) o verbete do Wikidata. Licença livre, sem chave, e com
   dezenas de fotos em alta de político brasileiro (Agência Brasil, Câmara,
   Senado). Continua o filtro por TIPO do verbete do piloto de micro-edição
   (sem ele "Nazismo" trouxe cartaz nazista).
3. É a pessoa mesmo? (custo zero) Reconhecimento facial local (YuNet + SFace
   do OpenCV, os mesmos modelos do crop antigo, licença Apache/MIT): a foto
   oficial do verbete (P18) é a referência, e cada candidata só entra se tem
   um rosto que bate com ela — grande o bastante para enquadrar sem pixelar.
   Charge, documento e foto de plenário com a pessoa minúscula caem aqui. O
   enquadramento sai centrado nesse rosto.
4. A foto serve? (~US$ 0,00003 por foto) Um modelo de visão barato pelo
   OpenRouter vê todas as candidatas numa chamada só e dá nota pela
   qualidade e pelo tom do corte (sem tentar identificar ninguém — a
   identidade já veio do passo 3). Sem chave ou se falhar, segue sem nota.
5. Quando troca: a cada ~5 s, sempre no começo de uma palavra (de preferência
   depois de uma pausa), com um fade curto; quando a fala cita outra
   pessoa/organização da lista, a foto dela entra naquele segundo.

Cache em disco por verbete (lista de fotos, downloads e rostos), porque os
mesmos políticos voltam corte após corte.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger("estudio")

_UA = "VideoMaker-estudio/1.0 (formato imagens; contato: joaokko@gmail.com)"
WIKIDATA = "https://www.wikidata.org/w/api.php"
COMMONS = "https://commons.wikimedia.org/w/api.php"
CACHE = Path(os.environ.get("ESTUDIO_DATA") or r"C:\VideoMaker\estudio") / "cache_imagens"
CACHE_VALIDADE = 14 * 86400       # lista de fotos de um verbete: refaz a cada 2 semanas

# Bloco da foto no quadro 9:16 (utils.build_clip_filter, modo "imagem")
LARGURA, ALTURA = 1080, 608
ASPECTO = LARGURA / ALTURA

MAX_CANDIDATAS = 60               # fotos baixadas por verbete (as mais novas primeiro)
BAIXAR_LARGURA = 1600             # miniatura do Commons: sobra para um bloco de 1080
ROSTO_MIN = 90                    # lado do rosto (px, na imagem baixada) para entrar
SIMILARIDADE_MIN = 0.42           # SFace, cosseno (limiar oficial 0.363 — aqui mais exigente)
ALTURA_MIN_RECORTE = 380          # recorte menor que isso vira pixel depois de ampliado para 608

MODELO_VISAO = os.environ.get("ESTUDIO_MODELO_VISAO") or "google/gemini-2.5-flash-lite"
NOTA_MIN = 5

TROCA_ALVO = 5.0                  # segundos por foto
TROCA_MIN = 3.0
MENCAO_MIN = 2.0                  # uma citação pode trocar a foto depois de 2 s na tela
FADE = 0.35

_FASE1 = Path(__file__).resolve().parent.parent / "fase1"
_YUNET = _FASE1 / "models" / "face_detection_yunet_2023mar.onnx"
_SFACE = _FASE1 / "models" / "face_recognition_sface_2021dec.onnx"


# ---------------------------------------------------------------------------
# 1. Quem aparece no corte
# ---------------------------------------------------------------------------

def entidades_do_corte(gancho: str, comentario: str, segmentos: list[dict]) -> list[dict]:
    """[{"nome", "tipo": pessoa|organizacao|lugar, "falado_como": [...]}, ...],
    o assunto principal primeiro. Lista vazia se o corte não fala de ninguém
    nomeável (aí o corte sai no formato de reserva)."""
    import deepseek_client
    from llm_client import _json
    fala = "\n".join(f"[{s['start']:.1f}] {s['text'].strip()}" for s in segmentos if s.get("text", "").strip())
    resp = deepseek_client.post("/chat/completions", {
        "model": "deepseek-chat", "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": (
                "Você escolhe as fotos que aparecem em cima de um corte de vídeo (política/notícia). "
                "Liste as PESSOAS, ORGANIZAÇÕES (partido, empresa, órgão, tribunal) e LUGARES "
                "específicos de que o corte fala — no máximo 4, do mais importante ao menos. O "
                "primeiro é o assunto principal do corte inteiro. Não inclua conceitos, ideologias, "
                "crimes, leis, eventos nem grupos genéricos ('o governo', 'os deputados'). Quem só "
                "está falando no vídeo entra apenas se é também o assunto. Para cada um: 'nome' "
                "exatamente como no título do verbete da Wikipédia, 'tipo' e 'falado_como' — as "
                "palavras com que aparece NA FALA (sobrenome, apelido, sigla), para achar o "
                "segundo em que é citado. Responda só JSON: "
                '{"entidades": [{"nome": "...", "tipo": "pessoa|organizacao|lugar", '
                '"falado_como": ["..."]}]} — lista vazia se não há ninguém nomeável.')},
            {"role": "user", "content": (f"Gancho: {gancho or '(nenhum)'}\n"
                                         f"Comentário: {comentario or '(nenhum)'}\n\nFala:\n{fala[:7000]}")},
        ],
    }, 45, 2)
    dados = _json(resp["choices"][0]["message"].get("content") or "{}")
    saida = []
    for e in dados.get("entidades") or []:
        nome = str(e.get("nome") or "").strip()
        if nome and e.get("tipo") in ("pessoa", "organizacao", "lugar"):
            saida.append({"nome": nome, "tipo": e["tipo"],
                          "falado_como": [str(f) for f in (e.get("falado_como") or []) if str(f).strip()]})
    return saida[:4]


# ---------------------------------------------------------------------------
# 2. Fotos do verbete no Commons
# ---------------------------------------------------------------------------

def _get_json(url: str, **params) -> dict:
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers={"User-Agent": _UA})
    for tentativa in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            if tentativa == 2:
                raise
            time.sleep(2 * (tentativa + 1))
    return {}


def _valor(claim: dict):
    return claim.get("mainsnak", {}).get("datavalue", {}).get("value")


def resolver_verbete(nome: str, tipo: str) -> dict | None:
    """Verbete do Wikidata do tipo pedido: pessoa (P31 = Q5), organização (tem
    logo P154 ou sede/fundação) ou lugar (tem coordenadas P625). O tipo vem do
    verbete, não do que a IA achou — o mesmo filtro do piloto de micro-edição."""
    itens = _get_json(WIKIDATA, action="wbsearchentities", search=nome, language="pt",
                      uselang="pt", limit="4", format="json").get("search") or []
    for item in itens:
        claims = _get_json(WIKIDATA, action="wbgetclaims", entity=item["id"], format="json").get("claims", {})
        instancia = {(_valor(c) or {}).get("id") for c in claims.get("P31", [])}
        pessoa = "Q5" in instancia
        org = bool(claims.get("P154") or claims.get("P159") or claims.get("P571")) and not pessoa
        lugar = bool(claims.get("P625")) and not pessoa
        if (tipo == "pessoa" and pessoa) or (tipo == "organizacao" and org) or (tipo == "lugar" and lugar):
            return {"qid": item["id"], "rotulo": item.get("label") or nome, "tipo": tipo,
                    "retrato": _valor(claims["P18"][0]) if claims.get("P18") else None,
                    "logo": _valor(claims["P154"][0]) if claims.get("P154") else None}
    return None


def _info_arquivos(titulos: list[str]) -> list[dict]:
    """imageinfo (miniatura em BAIXAR_LARGURA, autor, licença) de arquivos do Commons."""
    saida = []
    for i in range(0, len(titulos), 40):
        pags = _get_json(COMMONS, action="query", format="json", titles="|".join(titulos[i:i + 40]),
                         prop="imageinfo", iiprop="url|size|mime|extmetadata",
                         iiurlwidth=str(BAIXAR_LARGURA)).get("query", {}).get("pages", {})
        saida += [p for p in pags.values() if p.get("imageinfo")]
    return saida


def _candidata(pagina: dict) -> dict | None:
    info = pagina["imageinfo"][0]
    if info.get("mime") not in ("image/jpeg", "image/png", "image/webp", "image/tiff", "image/svg+xml"):
        return None
    # Sem miniatura (o Commons não reduz PNG/TIFF acima de ~50 MP — mapas,
    # scans) o link é o ORIGINAL: decodificado, um mapa desses passa de 1 GB
    # de RAM. Só entra o que vem reduzido (ou já é pequeno).
    tw, th = info.get("thumbwidth") or info.get("width", 0), info.get("thumbheight") or info.get("height", 0)
    if not info.get("thumburl") or tw * th > 16_000_000 or tw > BAIXAR_LARGURA * 1.2:
        return None
    meta = info.get("extmetadata") or {}
    texto = lambda k: re.sub(r"<[^>]+>", "", (meta.get(k) or {}).get("value", "")).strip()  # noqa: E731
    return {"titulo": pagina["title"], "url": info.get("thumburl") or info["url"],
            "largura": info.get("width", 0), "altura": info.get("height", 0),
            "pagina": info.get("descriptionurl", ""), "autor": texto("Artist")[:120],
            "licenca": texto("LicenseShortName"), "svg": info.get("mime") == "image/svg+xml"}


def fotos_do_verbete(verbete: dict) -> list[dict]:
    """Retrato oficial (P18) + logo (P154) + as fotos que "retratam" o verbete
    no Commons (P180, as mais novas primeiro). Cacheado por verbete."""
    pasta = CACHE / verbete["qid"]
    pasta.mkdir(parents=True, exist_ok=True)
    lista = pasta / "fotos.json"
    if lista.exists() and time.time() - lista.stat().st_mtime < CACHE_VALIDADE:
        return json.loads(lista.read_text(encoding="utf-8"))

    titulos = []
    for fixo in (verbete.get("retrato"), verbete.get("logo")):
        if fixo:
            titulos.append("File:" + fixo)
    buscas = [f"haswbstatement:P180={verbete['qid']} filetype:bitmap"]
    if verbete["tipo"] == "pessoa":
        # Pessoa: também a busca pelo nome (muita foto boa não tem a marcação
        # "retrata" — do Moraes eram só 12). Pode trazer foto de outra pessoa,
        # mas o rosto conferido com o retrato oficial barra.
        buscas.append(f'"{verbete["rotulo"]}" filetype:bitmap')
    for busca in buscas:
        achados = _get_json(COMMONS, action="query", format="json", list="search", srnamespace="6",
                            srsearch=busca, srsort="create_timestamp_desc", srlimit="80")
        titulos += [r["title"] for r in achados.get("query", {}).get("search", [])]
    titulos = list(dict.fromkeys(titulos))
    ordem = {t: i for i, t in enumerate(titulos)}       # a API devolve fora de ordem
    vistos, candidatas = set(), []
    for pagina in sorted(_info_arquivos(titulos), key=lambda p: ordem.get(p["title"], len(ordem))):
        c = _candidata(pagina)
        if c and c["titulo"] not in vistos:
            vistos.add(c["titulo"])
            c["oficial"] = c["titulo"] == "File:" + str(verbete.get("retrato"))
            c["logo"] = c["titulo"] == "File:" + str(verbete.get("logo"))
            candidatas.append(c)
    # a ordem do Commons é por data; o retrato e o logo vão na frente
    candidatas.sort(key=lambda c: (not c["logo"], not c["oficial"]))
    candidatas = _uma_por_evento(candidatas)[:MAX_CANDIDATAS]
    lista.write_text(json.dumps(candidatas, ensure_ascii=False, indent=1), encoding="utf-8")
    return candidatas


def _uma_por_evento(candidatas: list[dict], por_evento: int = 2) -> list[dict]:
    """Agência de notícia sobe 30 fotos do mesmo evento com o mesmo nome e só o
    número mudando ("Reunião de Bancada 07 02 2023 (52675597126).jpg") — no
    máximo `por_evento` de cada, para as fotos não parecerem a mesma."""
    contagem: dict[str, int] = {}
    saida = []
    for c in candidatas:
        chave = re.sub(r"[\d\W_]+", " ", c["titulo"].rsplit(".", 1)[0]).strip().lower()
        if contagem.get(chave, 0) < por_evento or c["oficial"] or c["logo"]:
            contagem[chave] = contagem.get(chave, 0) + 1
            saida.append(c)
    return saida


_DOWNLOADS = threading.Semaphore(3)   # o Commons devolve 429 com muitos downloads juntos


def _baixar(c: dict, pasta: Path) -> Path | None:
    """Só baixa para o cache em disco — decodificar fica para `_ler`, uma foto
    por vez (60 fotos decodificadas juntas, em 4 threads, ajudaram a derrubar
    o servidor de 8 GB num OOM)."""
    arquivo = pasta / (hashlib.md5(c["titulo"].encode()).hexdigest()[:12] + ".img")
    if not arquivo.exists():
        for tentativa in range(3):
            try:
                with _DOWNLOADS:
                    req = urllib.request.Request(c["url"], headers={"User-Agent": _UA})
                    with urllib.request.urlopen(req, timeout=40) as r:
                        arquivo.write_bytes(r.read())
                break
            except urllib.error.HTTPError as exc:
                if exc.code != 429 or tentativa == 2:
                    log.info("   foto não baixou (%s): %s", c["titulo"], exc)
                    return None
                time.sleep(3 * (tentativa + 1))
            except Exception as exc:  # noqa: BLE001
                log.info("   foto não baixou (%s): %s", c["titulo"], exc)
                return None
    if arquivo.stat().st_size > 25_000_000:      # miniatura de verdade não chega nisso
        return None
    return arquivo


def _ler(arquivo: Path) -> np.ndarray | None:
    img = cv2.imdecode(np.fromfile(str(arquivo), dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if img is None or img.size > 16_000_000 * 4:
        return None
    if img.dtype != np.uint8:                    # PNG de 16 bits
        img = (img / 257).astype(np.uint8)
    if img.ndim == 2:
        img = cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    elif img.shape[2] == 4:          # logo com transparência: fundo branco
        alfa = img[:, :, 3:4].astype(np.float32) / 255
        img = (img[:, :, :3] * alfa + 255 * (1 - alfa)).astype(np.uint8)
    return img


# ---------------------------------------------------------------------------
# 3. Rosto: é a pessoa mesmo, e onde enquadrar
# ---------------------------------------------------------------------------

class _Rostos:
    def __init__(self):
        self.detector = cv2.FaceDetectorYN.create(str(_YUNET), "", (320, 320), 0.8)
        self.reconhecedor = cv2.FaceRecognizerSF.create(str(_SFACE), "")

    def rostos(self, img: np.ndarray) -> list[tuple[np.ndarray, np.ndarray]]:
        """[(linha do YuNet em coordenadas da imagem, embedding SFace), ...]"""
        h, w = img.shape[:2]
        escala = min(1.0, 1280 / max(h, w))
        peq = cv2.resize(img, (round(w * escala), round(h * escala))) if escala < 1 else img
        self.detector.setInputSize((peq.shape[1], peq.shape[0]))
        _, achados = self.detector.detect(peq)
        saida = []
        for f in achados if achados is not None else []:
            f = f.copy()
            f[:14] /= escala
            if min(f[2], f[3]) < ROSTO_MIN:
                continue
            alinhado = self.reconhecedor.alignCrop(img, f)
            saida.append((f, self.reconhecedor.feature(alinhado).flatten()))
        return saida


def _cos(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9))


def _referencia(imagens: list[tuple[dict, Path, list]]) -> np.ndarray | None:
    """Rosto de referência: o do retrato oficial (maior rosto dele). Sem
    retrato, o rosto que mais se repete entre as candidatas (em pelo menos 3)."""
    for c, _arq, rostos in imagens:
        if c["oficial"] and rostos:
            return max(rostos, key=lambda r: r[0][2] * r[0][3])[1]
    todos = [r[1] for _c, _i, rostos in imagens for r in rostos]
    melhor, votos = None, 0
    for a in todos:
        v = sum(_cos(a, b) >= SIMILARIDADE_MIN for b in todos)
        if v > votos:
            melhor, votos = a, v
    return melhor if votos >= 3 else None


def _recorte_no_rosto(img: np.ndarray, rosto: np.ndarray, outros: list[np.ndarray]
                      ) -> tuple[int, int, int, int] | str | None:
    """Caixa 16:9 com o rosto ocupando ~42% da altura (entre 25% e 60%) e o
    rosto a ~40% do topo. "inteira" quando a foto é estreita demais para
    isso (retrato em pé, close): vai inteira sobre o fundo desfocado. None se
    qualquer recorte ficaria pixelado, ou se outra pessoa rouba a cena (um
    rosto maior que o dela dentro do recorte)."""
    h, w = img.shape[:2]
    x, y, fw, fh = rosto[:4]
    cx, cy = x + fw / 2, y + fh / 2
    maximo = min(h, w / ASPECTO)
    ch = min(max(fh / 0.42, ALTURA_MIN_RECORTE), fh / 0.25, maximo)
    if ch < ALTURA_MIN_RECORTE or fh / ch > 0.6:
        if h < ALTURA_MIN_RECORTE or fh / h > 0.6:
            return None
        caixa = (0, 0, w, h)
        resultado = "inteira"
    else:
        cw = ch * ASPECTO
        x0 = int(np.clip(cx - cw / 2, 0, w - cw))
        y0 = int(np.clip(cy - ch * 0.40, 0, h - ch))
        caixa = resultado = (x0, y0, int(cw), int(ch))
    bx, by, bw, bh = caixa
    for o in outros:
        ox, oy = o[0] + o[2] / 2, o[1] + o[3] / 2
        if bx <= ox <= bx + bw and by <= oy <= by + bh and o[3] > fh * 0.9:
            return None
    return resultado


def _encaixar(img: np.ndarray, caixa: tuple[int, int, int, int] | None) -> np.ndarray:
    """Imagem -> bloco LARGURAxALTURA. Com caixa: recorte direto (preenche o
    bloco). Sem caixa (logo, foto em pé, foto estreita): a foto inteira no
    meio, sobre ela mesma desfocada — nunca barra preta."""
    if caixa is not None:
        x0, y0, cw, ch = caixa
        return cv2.resize(img[y0:y0 + ch, x0:x0 + cw], (LARGURA, ALTURA), interpolation=cv2.INTER_AREA
                          if ch >= ALTURA else cv2.INTER_CUBIC)
    h, w = img.shape[:2]
    # fundo: a própria foto preenchendo o bloco, desfocada e escurecida
    s = max(LARGURA / w, ALTURA / h)
    fundo = cv2.resize(img, (max(LARGURA, round(w * s)), max(ALTURA, round(h * s))))
    oy, ox = (fundo.shape[0] - ALTURA) // 2, (fundo.shape[1] - LARGURA) // 2
    fundo = fundo[oy:oy + ALTURA, ox:ox + LARGURA]
    fundo = cv2.GaussianBlur(cv2.resize(fundo, (LARGURA // 4, ALTURA // 4)), (0, 0), 6)
    fundo = (cv2.resize(fundo, (LARGURA, ALTURA)) * 0.6).astype(np.uint8)
    s = min(LARGURA * 0.94 / w, ALTURA * 0.94 / h)
    frente = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA if s < 1 else cv2.INTER_CUBIC)
    fy, fx = (ALTURA - frente.shape[0]) // 2, (LARGURA - frente.shape[1]) // 2
    fundo[fy:fy + frente.shape[0], fx:fx + frente.shape[1]] = frente
    return fundo


def _dhash(x: np.ndarray) -> np.ndarray:
    """Hash de diferença 16x9 — duas fotos quase iguais (mesma sequência de
    disparos) diferem em poucos bits."""
    g = cv2.resize(cv2.cvtColor(x, cv2.COLOR_BGR2GRAY), (17, 9), interpolation=cv2.INTER_AREA)
    return (g[:, 1:] > g[:, :-1]).flatten()


def preparar_fotos(verbete: dict, destino: Path, rostos: _Rostos | None, principal: bool = True) -> list[dict]:
    """Candidatas do verbete -> blocos 1080x608 prontos em `destino`, na ordem
    de preferência. Pessoa: só foto com o rosto dela, enquadrada nele.
    Organização/lugar: logo e fotos inteiras (quem filtra é o modelo de visão).
    `principal`: o assunto do corte aparece o tempo todo; os outros só quando
    citados — baixa menos fotos deles (pessoa: o rosto descarta muitas)."""
    from concurrent.futures import ThreadPoolExecutor
    pasta = CACHE / verbete["qid"]
    limite = {("pessoa", True): 60, ("pessoa", False): 25}.get((verbete["tipo"], principal), 20 if principal else 8)
    candidatas = fotos_do_verbete(verbete)[:limite]
    with ThreadPoolExecutor(8) as ex:
        arquivos = list(ex.map(lambda c: _baixar(c, pasta), candidatas))
    baixadas = [(c, a) for c, a in zip(candidatas, arquivos) if a is not None]

    saida, hashes = [], []
    destino.mkdir(parents=True, exist_ok=True)

    def guardar(c: dict, bloco: np.ndarray) -> None:
        h = _dhash(bloco)
        if any(np.count_nonzero(h != o) < 22 for o in hashes):   # quase igual a uma que já entrou
            return
        hashes.append(h)
        arquivo = destino / f"{verbete['qid']}_{len(saida):02d}.jpg"
        cv2.imwrite(str(arquivo), bloco, [cv2.IMWRITE_JPEG_QUALITY, 93])
        saida.append(dict(c, arquivo=str(arquivo), verbete=verbete["rotulo"], qid=verbete["qid"]))

    if verbete["tipo"] == "pessoa":
        if rostos is None:
            return []
        # 1ª passada: rostos de cada foto (cacheados junto com as fotos do verbete)
        arq_rostos = pasta / "rostos.json"
        salvos = json.loads(arq_rostos.read_text(encoding="utf-8")) if arq_rostos.exists() else {}
        analisadas = []
        for c, arquivo in baixadas:
            if c["titulo"] not in salvos:
                img = _ler(arquivo)
                salvos[c["titulo"]] = [] if img is None else \
                    [[r.tolist(), e.tolist()] for r, e in rostos.rostos(img)]
            analisadas.append((c, arquivo, [(np.array(r, np.float32), np.array(e, np.float32))
                                            for r, e in salvos[c["titulo"]]]))
        arq_rostos.write_text(json.dumps(salvos), encoding="utf-8")
        ref = _referencia(analisadas)
        if ref is None:
            log.info("   %s: sem rosto de referência (sem retrato oficial com rosto)", verbete["rotulo"])
            return []
        # 2ª passada: só as fotos com o rosto dela, lidas de novo uma por vez
        for c, arquivo, achados in analisadas:
            pares = [(r, _cos(ref, e)) for r, e in achados]
            pares = [p for p in pares if p[1] >= SIMILARIDADE_MIN]
            if not pares:
                continue
            rosto, sim = max(pares, key=lambda p: p[1] * p[0][2])      # o mais parecido e maior
            img = _ler(arquivo)
            if img is None:
                continue
            caixa = _recorte_no_rosto(img, rosto, [r for r, _e in achados if r is not rosto])
            if caixa is not None:
                guardar(dict(c, similaridade=round(sim, 3)), _encaixar(img, None if caixa == "inteira" else caixa))
    else:
        for c, arquivo in baixadas:
            img = _ler(arquivo)
            if img is None or not (c["logo"] or min(img.shape[:2]) >= ALTURA_MIN_RECORTE):
                continue
            h, w = img.shape[:2]
            caixa = None
            if not c["logo"] and w / h >= ASPECTO * 0.8:        # paisagem: preenche o bloco
                ch = min(h, w / ASPECTO)
                caixa = (int((w - ch * ASPECTO) / 2), int((h - ch) / 2), int(ch * ASPECTO), int(ch))
            guardar(c, _encaixar(img, caixa))
    return saida


# ---------------------------------------------------------------------------
# 4. Modelo de visão: a foto serve para este corte?
# ---------------------------------------------------------------------------

def avaliar_fotos(fotos: list[dict], verbete: dict, gancho: str, comentario: str) -> list[dict]:
    """Nota 0-10 por foto, todas numa chamada só (miniaturas 512x288). Devolve
    só as aprovadas, da melhor para a pior. Sem chave/erro: devolve como veio."""
    import base64
    import openrouter
    if not fotos:
        return fotos
    lote = fotos[:16]
    conteudo = []
    for i, f in enumerate(lote):
        mini = cv2.resize(cv2.imread(f["arquivo"]), (512, 288), interpolation=cv2.INTER_AREA)
        b64 = base64.b64encode(cv2.imencode(".jpg", mini, [cv2.IMWRITE_JPEG_QUALITY, 80])[1]).decode()
        conteudo += [{"type": "text", "text": f"Foto {i}:"},
                     {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]
    if verbete["tipo"] == "pessoa":
        quem = ("A identidade da pessoa já foi conferida por reconhecimento facial: NÃO tente "
                "identificar ninguém, julgue só a foto.")
    else:
        quem = (f"Estas fotos deveriam mostrar '{verbete['rotulo']}' ({verbete['tipo']}): dê nota "
                "baixa se a foto não deixa isso claro (logotipo, fachada, placa, sede, lugar conhecido).")
    pedido = (
        "Estas fotos vão aparecer em cima de um vídeo curto (Shorts/Reels) de notícia/política, "
        "trocando a cada poucos segundos, em um bloco 16:9.\n"
        f"Gancho do vídeo: {gancho or '(nenhum)'}\nComentário: {comentario or '(nenhum)'}\n{quem}\n"
        "Dê nota 0-10 para cada foto: nítida e bem enquadrada, é FOTOGRAFIA (não documento, "
        "print de tela, gráfico, charge, desenho ou montagem), o assunto aparece bem, e o clima "
        "da foto não contradiz o tom do vídeo (ex.: gargalhando num assunto trágico). Nota 0 "
        "para foto constrangedora, íntima, violenta ou que expõe menor de idade. Responda só "
        'JSON: {"fotos": [{"i": 0, "nota": 7}]}')
    try:
        resp = openrouter.post("/v1/chat/completions", {
            "model": MODELO_VISAO, "temperature": 0, "max_tokens": 400,
            "response_format": {"type": "json_object"}, "usage": {"include": True},
            "messages": [{"role": "user", "content": [{"type": "text", "text": pedido}, *conteudo]}],
        }, 60, 2)
        from llm_client import _json
        notas = {int(n["i"]): float(n["nota"])
                 for n in _json(resp["choices"][0]["message"].get("content") or "{}").get("fotos", [])
                 if isinstance(n, dict) and "i" in n and "nota" in n}
        uso = resp.get("usage") or {}
        log.info("   visão (%s): %d fotos de %s, US$ %.5f — notas %s", MODELO_VISAO, len(lote),
                 verbete["rotulo"], float(uso.get("cost") or 0),
                 " ".join(f"{notas.get(i, '-'):g}" if i in notas else "-" for i in range(len(lote))))
    except Exception as exc:  # noqa: BLE001 — sem nota, segue com o que o rosto aprovou
        log.warning("   visão falhou (%s); seguindo sem nota", exc)
        return fotos
    aprovadas = [dict(f, nota=notas[i]) for i, f in enumerate(lote) if notas.get(i, 0) >= NOTA_MIN]
    return sorted(aprovadas, key=lambda f: -f["nota"])


# ---------------------------------------------------------------------------
# 5. Quando cada foto entra
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    return re.sub(r"[^a-z0-9 ]", "", "".join(ch for ch in s if unicodedata.category(ch) != "Mn")).strip()


def _mencoes(palavras: list[dict], formas: list[str]) -> list[float]:
    """Segundos em que alguma forma ("Moraes", "Alexandre de Moraes") é dita."""
    alvos = [_norm(f).split() for f in formas]
    alvos = [a for a in alvos if a and len("".join(a)) >= 2]      # "PT", "PL" contam
    ditas = [_norm(p.get("word", "")) for p in palavras]
    tempos = []
    for i in range(len(ditas)):
        for alvo in alvos:
            if ditas[i:i + len(alvo)] == alvo:
                tempos.append(float(palavras[i]["start"]))
                break
    return tempos


def _proximo_corte(palavras: list[dict], a: float, b: float, alvo: float) -> float:
    """Começo de palavra entre a e b, perto de `alvo`, de preferência logo
    depois de uma pausa (a troca cai no respiro da fala, não no meio da frase)."""
    opcoes = []
    for i, p in enumerate(palavras):
        t = float(p["start"])
        if a <= t <= b:
            pausa = t - float(palavras[i - 1]["end"]) if i else 1.0
            opcoes.append((abs(t - alvo) - (1.5 if pausa >= 0.25 else 0), t))
    return min(opcoes)[1] if opcoes else alvo


def planejar(dur: float, palavras: list[dict], galerias: list[dict]) -> list[tuple[float, dict]]:
    """[(início em segundos, foto), ...]. `galerias`: [{"fotos": [...],
    "mencoes": [s, ...]}], a 1ª é o assunto principal (a foto "de casa")."""
    principal = galerias[0]
    usos = {id(g): 0 for g in galerias}

    def proxima_foto(g: dict, anterior: dict | None) -> dict:
        fotos = g["fotos"]
        f = fotos[usos[id(g)] % len(fotos)]
        if anterior is not None and f["arquivo"] == anterior["arquivo"] and len(fotos) > 1:
            usos[id(g)] += 1
            f = fotos[usos[id(g)] % len(fotos)]
        usos[id(g)] += 1
        return f

    citacoes = sorted(((t, g) for g in galerias[1:] for t in g["mencoes"]), key=lambda c: c[0])
    plano: list[tuple[float, dict]] = []
    t, atual = 0.0, principal
    while True:
        foto = proxima_foto(atual, plano[-1][1] if plano else None)
        if not plano or foto["arquivo"] != plano[-1][1]["arquivo"]:
            plano.append((t, foto))       # (uma foto só do assunto: não "troca" pela mesma)
        # a próxima troca: uma citação de outro (se vier cedo), senão o ritmo normal
        cit = next(((tc, g) for tc, g in citacoes if t + MENCAO_MIN <= tc - 0.15 < t + TROCA_ALVO + 1.5
                    and g is not atual), None)
        if cit:
            fim, atual = cit[0] - 0.15, cit[1]
        else:
            fim = _proximo_corte(palavras, t + TROCA_MIN, t + TROCA_ALVO + 2, t + TROCA_ALVO)
            atual = principal
        if dur - fim < TROCA_MIN:
            return plano
        t = fim


# ---------------------------------------------------------------------------
# Render e ponta a ponta
# ---------------------------------------------------------------------------

def renderizar(clipe: Path, plano: list[tuple[float, dict]], dur: float, fps: float, destino: Path) -> None:
    """Fotos em cima (com fade entre elas), o corte 16:9 inteiro embaixo — o
    mesmo desenho de utils.build_clip_filter("imagem"), agora com várias."""
    entradas = ["-i", str(clipe)]
    filtros = []
    for k, (inicio, foto) in enumerate(plano):
        fim = plano[k + 1][0] + FADE if k + 1 < len(plano) else dur
        quadros = max(1, round((fim - inicio) * fps))
        # a foto é decodificada uma vez e repetida pelo filtro loop (com
        # "-loop 1" o ffmpeg decodificava o JPEG de novo a cada quadro)
        entradas += ["-i", foto["arquivo"]]
        filtros.append(f"[{k + 1}:v]scale={LARGURA}:{ALTURA},setsar=1,format=yuv420p,"
                       f"loop=loop={quadros - 1}:size=1:start=0,setpts=N/({fps:.5f}*TB),fps={fps:.5f}[f{k}]")
    atual = "f0"
    for k in range(1, len(plano)):
        filtros.append(f"[{atual}][f{k}]xfade=transition=fade:duration={FADE}:offset={plano[k][0]:.3f}[x{k}]")
        atual = f"x{k}"
    filtros += [f"[0:v]scale={LARGURA}:{ALTURA}:force_original_aspect_ratio=decrease,"
                f"pad={LARGURA}:{ALTURA}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,format=yuv420p[bot]",
                f"[{atual}][bot]vstack=inputs=2,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1[base]"]
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *entradas, "-filter_complex", ";".join(filtros),
                        "-map", "[base]", "-map", "0:a?", "-t", f"{dur:.3f}",
                        # intermediário (o acabamento recodifica): ultrafast poupa ~25% do tempo
                        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "17", "-c:a", "copy",
                        str(destino)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou no formato imagens: {r.stderr[-600:]}")


def montar(clipe: Path, destino: Path, pasta: Path, gancho: str, comentario: str,
           segmentos: list[dict], palavras: list[dict], dur: float, fps: float) -> list[dict] | None:
    """Ponta a ponta. Devolve os créditos das fotos usadas (autor, licença,
    página no Commons), ou None sem gravar `destino` se não achou ninguém
    com foto — quem chama cai para o formato de reserva."""
    from concurrent.futures import ThreadPoolExecutor
    t0 = time.time()
    entidades = entidades_do_corte(gancho, comentario, segmentos)
    if not entidades:
        log.warning("   formato imagens: o corte não cita ninguém nomeável")
        return None
    for e in entidades:
        e["mencoes"] = _mencoes(palavras, e["falado_como"] + [e["nome"]])
    # quem não é o assunto principal só aparece quando é dito — se não é dito, nem busca foto
    entidades = [entidades[0]] + [e for e in entidades[1:] if e["mencoes"]]
    log.info("   formato imagens: %s (%.1fs)", ", ".join(
        f"{e['nome']} ({e['tipo']}, {len(e['mencoes'])} citações)" for e in entidades), time.time() - t0)
    def galeria(e: dict) -> dict | None:
        # YuNet/SFace não são thread-safe: um detector por entidade
        rostos = _Rostos() if _YUNET.exists() and _SFACE.exists() else None
        try:
            verbete = resolver_verbete(e["nome"], e["tipo"])
            if verbete is None:
                log.info("   %s: sem verbete de %s no Wikidata", e["nome"], e["tipo"])
                return None
            fotos = preparar_fotos(verbete, pasta / "imagens", rostos, principal=e is entidades[0])
            n_rosto = len(fotos)
            # secundário aparece 1-2 vezes: não precisa avaliar 16 fotos dele
            fotos = avaliar_fotos(fotos[:16 if e is entidades[0] else 5], verbete, gancho, comentario)
        except Exception as exc:  # noqa: BLE001 — uma entidade sem foto não derruba as outras
            log.warning("   %s: fotos falharam: %s", e["nome"], exc)
            return None
        log.info("   %s (%s): %d fotos com o rosto/assunto, %d aprovadas", verbete["rotulo"],
                 verbete["qid"], n_rosto, len(fotos))
        if not fotos:
            return None
        return {"fotos": fotos, "nome": verbete["rotulo"],
                "mencoes": e["mencoes"] or _mencoes(palavras, [verbete["rotulo"]])}

    t0 = time.time()
    with ThreadPoolExecutor(2) as ex:      # 2 por vez: o servidor às vezes fica com 8 GB
        galerias = [g for g in ex.map(galeria, entidades) if g]
    if not galerias:
        log.warning("   formato imagens: nenhuma foto aproveitável")
        return None
    plano = planejar(dur, palavras, galerias)
    log.info("   formato imagens: fotos em %.1fs; %d trocas — %s", time.time() - t0, len(plano),
             ", ".join(f"{t:.1f}s {f['verbete']}" for t, f in plano))
    t0 = time.time()
    renderizar(clipe, plano, dur, fps, destino)
    log.info("   formato imagens: render em %.1fs", time.time() - t0)
    usadas = list({f["arquivo"]: f for _t, f in plano}.values())
    return [{"verbete": f["verbete"], "autor": f.get("autor", ""), "licenca": f.get("licenca", ""),
             "pagina": f.get("pagina", "")} for f in usadas]
