"""Thumbnail do vídeo longo: 3 opções 1280x720 para escolher na revisão.

1. Prints: ~24 quadros do trecho da live (fora o que o vídeo longo corta);
   os 8 com rosto maior e mais nítido (YuNet) viram candidatos.
2. Quem fala: uma janela de 12 s em volta de cada candidato passa pelo LR-ASD
   (fase1/falante_ativo.py — o mesmo reconhecimento de boca + voz do crop,
   usado como está, num processo à parte). Fica o print em que a pessoa
   visível está FALANDO, com o rosto dela, e a fala da transcrição nos
   momentos em que ela fala: é dela que sai a frase da thumb.
3. Decupagem (modelo de visão barato, OpenRouter): descreve cada print
   (expressão, gesto, o que tem na tela) e dá nota de thumb.
4. Frases (DeepSeek, OpenRouter): 3 frases curtas e polêmicas, cada uma presa
   a um print e com o trecho da fala DAQUELA pessoa que a sustenta. Frase sem
   apoio na fala de quem aparece é recusada (e o modelo tenta de novo).
5. Composição no código: a imagem NÃO é gerada por IA — o rosto é o da pessoa
   de verdade, recortado do print (segmentação do MediaPipe), e o texto sai
   sempre escrito certo. Três layouts: "destaque" (pessoa à direita, texto à
   esquerda), "contraste" (pessoa à esquerda, fundo vermelho) e "tela cheia".
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import ai_srt
import moldura

log = logging.getLogger("estudio")

LARGURA, ALTURA = 1280, 720
N_AMOSTRAS = 24              # quadros lidos do trecho
N_CANDIDATOS = 8             # vão para o LR-ASD
N_PRINTS = 5                 # vão para a decupagem e as frases
JANELA = 12                  # s em volta de cada candidato, para saber quem fala
ARREDORES = 45               # s de fala para cada lado do print (contexto, pode ter outras vozes)
# 03/10, mesmos 5 prints: DeepSeek US$ 0,00017 · gpt-5.4-nano 0,00075 · Gemini 2.5 Flash-Lite 0,00073 ·
# Gemini 3.1 Flash-Lite 0,0021 · Gemini 3.8 Flash 0,0054 (o Gemini 3 conta ~4x mais tokens por imagem)
MODELO_VISAO = os.environ.get("ESTUDIO_MODELO_THUMB_VISAO") or "deepseek/deepseek-v4.1-flash"
MODELO_FRASE = os.environ.get("ESTUDIO_MODELO_THUMB_FRASE") or "deepseek/deepseek-v4.1-flash"

VERMELHO = (225, 16, 24)
AMARELO = (255, 214, 0)

_FASE1 = Path(__file__).resolve().parent.parent / "fase1"
_YUNET = _FASE1 / "models" / "face_detection_yunet_2023mar.onnx"
_SEGMENTADOR = _FASE1 / "models" / "selfie_multiclass_256x256.tflite"
_URL_SEGMENTADOR = ("https://storage.googleapis.com/mediapipe-models/image_segmenter/"
                    "selfie_multiclass_256x256/float32/latest/selfie_multiclass_256x256.tflite")


# ---------------------------------------------------------------------------
# 1. Candidatos (rosto grande e nítido)
# ---------------------------------------------------------------------------

def _quadro(video: Path, t: float) -> np.ndarray | None:
    r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                        "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    if r.returncode or not r.stdout:
        return None
    return cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_COLOR)


def _rostos(detector, img: np.ndarray) -> list[np.ndarray]:
    """Linhas do YuNet (x, y, w, h, ..., confiança) em coordenadas da imagem."""
    h, w = img.shape[:2]
    escala = min(1.0, 960 / w)
    peq = cv2.resize(img, (round(w * escala), round(h * escala))) if escala < 1 else img
    detector.setInputSize((peq.shape[1], peq.shape[0]))
    _, achados = detector.detect(peq)
    saida = []
    for f in achados if achados is not None else []:
        f = f.copy()
        f[:14] /= escala
        saida.append(f)
    return sorted(saida, key=lambda f: -f[2] * f[3])


def _nota_rosto(img: np.ndarray, caixa) -> float:
    """Rosto grande e nítido = bom para thumb."""
    x, y, w, h = (int(v) for v in caixa[:4])
    cinza = cv2.cvtColor(img[max(0, y):y + h, max(0, x):x + w], cv2.COLOR_BGR2GRAY)
    nitidez = min(1.0, cv2.Laplacian(cinza, cv2.CV_64F).var() / 300) if cinza.size else 0
    area = (w * h) / (img.shape[0] * img.shape[1])
    return float(min(area * 12, 1.0) * (0.4 + 0.6 * nitidez))


def _fora(t: float, remover: list[tuple[float, float]], folga: float) -> bool:
    return any(a - folga <= t <= b + folga for a, b in remover)


def candidatos(fonte: Path, inicio: float, fim: float, remover: list[tuple[float, float]],
               n: int = N_CANDIDATOS) -> list[dict]:
    """[{t, img, rosto, nota}] (tempo de `fonte`), os melhores e afastados entre si."""
    detector = cv2.FaceDetectorYN.create(str(_YUNET), "", (320, 320), 0.7)
    margem = JANELA / 2 + 1
    tempos = [float(t) for t in np.linspace(inicio + margem, fim - margem, N_AMOSTRAS * 2)
              if not _fora(float(t), remover, JANELA / 2)][::2] or [(inicio + fim) / 2]
    achados = []
    for t in tempos:
        img = _quadro(fonte, t)
        if img is None:
            continue
        rostos = _rostos(detector, img)
        achados.append({"t": t, "img": img, "rosto": rostos[0][:4] if rostos else None,
                        "nota": _nota_rosto(img, rostos[0]) * float(rostos[0][14]) if rostos else 0.0})
    if not achados:
        raise RuntimeError("não consegui ler quadros do vídeo")
    distancia = (fim - inicio) / (n * 2.5)
    escolhidos = []
    for c in sorted(achados, key=lambda c: -c["nota"]):
        if all(abs(c["t"] - e["t"]) >= distancia for e in escolhidos):
            escolhidos.append(c)
        if len(escolhidos) == n:
            break
    return sorted(escolhidos, key=lambda c: c["t"])


# ---------------------------------------------------------------------------
# 2. Quem fala em cada candidato (LR-ASD, processo à parte)
# ---------------------------------------------------------------------------

def _montagem(fonte: Path, inicios: list[float], destino: Path) -> None:
    """As janelas de JANELA s em fila, 1280x720 a 25 quadros/s, com áudio."""
    entradas, filtros = [], []
    for i, a in enumerate(inicios):
        entradas += ["-ss", f"{a:.3f}", "-t", f"{JANELA}", "-i", str(fonte)]
        filtros.append(f"[{i}:v]scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,"
                       f"fps=25,setsar=1,trim=end_frame={JANELA * 25},setpts=PTS-STARTPTS[v{i}];"
                       f"[{i}:a]aresample=48000,atrim=end={JANELA},asetpts=PTS-STARTPTS[a{i}]")
    junta = "".join(f"[v{i}][a{i}]" for i in range(len(inicios))) + f"concat=n={len(inicios)}:v=1:a=1[v][a]"
    base = ["ffmpeg", "-y", "-v", "error", *entradas, "-filter_complex", ";".join(filtros) + ";" + junta,
            "-map", "[v]", "-map", "[a]", "-pix_fmt", "yuv420p", "-c:a", "aac"]
    r = subprocess.run(base + ["-c:v", "h264_nvenc", "-cq", "23", str(destino)], capture_output=True, text=True)
    if r.returncode:
        subprocess.run(base + ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", str(destino)],
                       capture_output=True, text=True, check=True)


def _seguidos(quadros: np.ndarray) -> list[tuple[int, int]]:
    """[(primeiro, último)] de cada sequência de quadros consecutivos."""
    if not len(quadros):
        return []
    quebras = np.where(np.diff(quadros) > 1)[0]
    inicios = np.r_[quadros[0], quadros[quebras + 1]]
    fins = np.r_[quadros[quebras], quadros[-1]]
    return list(zip(inicios.tolist(), fins.tolist()))


def quem_fala(fonte: Path, cands: list[dict], pasta: Path) -> list[dict | None]:
    """Para cada candidato: {t, rosto [x, y, w, h], nota, falando [(a, b)]} no
    tempo de `fonte` — o rosto de quem está falando no meio da janela e os
    momentos em que essa mesma pessoa fala nela — ou None (ninguém visível
    falando). Se o LR-ASD não rodar, devolve None para todos."""
    W = cands[0]["img"].shape[1]
    inicios = [max(0.0, c["t"] - JANELA / 2) for c in cands]
    video, saida = pasta / "_quem_fala.mp4", pasta / "_quem_fala.json"
    try:
        _montagem(fonte, inicios, video)
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--lrasd", str(video), str(saida)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(_FASE1),
                           timeout=900, env={**os.environ, "PYTHONPATH": os.pathsep.join(
                               [str(_FASE1.parent), str(_FASE1), os.environ.get("PYTHONPATH", "")])})
        if r.returncode or not saida.exists():
            raise RuntimeError((r.stderr or r.stdout)[-300:])
        an = json.loads(saida.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — sem LR-ASD, segue sem saber quem fala
        log.warning("   thumb: LR-ASD não rodou (%s); sem confirmar quem fala", exc)
        return [None] * len(cands)
    finally:
        video.unlink(missing_ok=True)
        saida.unlink(missing_ok=True)
    escala = W / 1280
    por_janela = JANELA * 25
    resultado = []
    for k, ini in enumerate(inicios):
        q0, q1 = k * por_janela, (k + 1) * por_janela
        meio = (q0 + q1) / 2
        melhor = None
        for t, notas in zip(an["trilhas"], an["notas"]):
            quadros = np.array(t["frame"][:len(notas)])
            notas = np.array(notas)
            centro = (quadros >= meio - 40) & (quadros <= meio + 40)     # ±1,6 s do meio
            if centro.sum() < 10:
                continue
            media = float(notas[centro].mean())
            if media > 0 and (melhor is None or media > melhor[0]):
                melhor = (media, t, quadros, notas)
        if melhor is None:
            resultado.append(None)
            continue
        media, t, quadros, notas = melhor
        suave = np.convolve(notas, np.ones(5) / 5, mode="same")
        centro = np.where((quadros >= meio - 40) & (quadros <= meio + 40))[0]
        j = int(centro[np.argmax(suave[centro])])
        x, y, s = t["x"][j] * escala, t["y"][j] * escala, t["s"][j] * escala
        na_janela = (quadros >= q0) & (quadros < q1) & (notas > 0)
        falando = [(ini + (a - q0) / 25, ini + (b + 1 - q0) / 25) for a, b in _seguidos(quadros[na_janela])]
        resultado.append({"t": ini + (int(quadros[j]) - q0) / 25, "rosto": [x - s, y - s, 2 * s, 2 * s],
                          "nota": media, "falando": falando})
    return resultado


def _lrasd_main(video: str, saida: str) -> int:
    """Modo processo à parte: roda o falante_ativo.analisar e grava as trilhas."""
    sys.path.insert(0, str(_FASE1))
    import falante_ativo
    an = falante_ativo.analisar(Path(video))
    Path(saida).write_text(json.dumps({
        "trilhas": [{"frame": np.asarray(t["frame"]).tolist(), "x": np.asarray(t["x"]).tolist(),
                     "y": np.asarray(t["y"]).tolist(), "s": np.asarray(t["s"]).tolist()} for t in an["trilhas"]],
        "notas": [np.asarray(n).tolist() for n in an["notas"]]}), encoding="utf-8")
    return 0


# ---------------------------------------------------------------------------
# 3-4. Decupagem (visão) e frases (DeepSeek), pelo OpenRouter
# ---------------------------------------------------------------------------

def _jpg_b64(img: np.ndarray, largura: int = 768) -> str:
    h, w = img.shape[:2]
    mini = cv2.resize(img, (largura, round(h * largura / w)), interpolation=cv2.INTER_AREA)
    return base64.b64encode(cv2.imencode(".jpg", mini, [cv2.IMWRITE_JPEG_QUALITY, 82])[1]).decode()


def _chamar(modelo: str, conteudo, max_tokens: int, temperatura: float) -> dict:
    import openrouter
    from llm_client import _json
    resp = openrouter.post("/v1/chat/completions", {
        "model": modelo, "temperature": temperatura, "max_tokens": max_tokens, "reasoning": {"effort": "low"},
        "response_format": {"type": "json_object"}, "usage": {"include": True},
        "messages": [{"role": "user", "content": conteudo}],
    }, 120, 3)
    escolha = resp["choices"][0]
    texto = escolha["message"].get("content") or ""
    log.info("   thumb (%s): US$ %.4f", modelo, float((resp.get("usage") or {}).get("cost") or 0))
    if not texto.strip():   # modelo que raciocina antes: o raciocínio pode comer todos os tokens
        raise RuntimeError(f"{modelo} respondeu vazio ({escolha.get('finish_reason')})")
    try:
        return _json(texto)
    except ValueError as exc:
        raise RuntimeError(f"{modelo} não devolveu JSON ({escolha.get('finish_reason')}): {texto[:300]}") from exc


def decupar(prints: list[dict]) -> list[dict]:
    """Descrição e nota de cada print: [{descricao, nota}] na ordem dos prints."""
    conteudo = [{"type": "text", "text": (
        "Prints de um vídeo de política/notícia, para escolher a thumbnail do YouTube. Para cada print: "
        "descreva em uma frase curta a pessoa em destaque (se houver um quadrado amarelo, é o rosto de quem está "
        "falando), a expressão e o gesto dela e o que mais aparece na tela (logo, gráfico, texto, outra pessoa); "
        "e dê nota 0-10 de quanto serve para thumb (rosto grande e expressão forte — indignado, rindo, surpreso, "
        "apontando — vale mais; olho fechado, boca torta no meio da palavra, de costas ou tela de gráfico vale "
        'menos). NÃO tente dizer quem é a pessoa. Responda só JSON: {"prints": [{"i": 0, "descricao": "...", '
        '"nota": 7}]}')}]
    for i, p in enumerate(prints):
        img = p["img"].copy()
        if p.get("confirmado"):
            x, y, w, h = (int(v) for v in p["rosto"])
            cv2.rectangle(img, (x, y), (x + w, y + h), (0, 255, 255), max(3, img.shape[1] // 300))
        conteudo += [{"type": "text", "text": f"Print {i}:"},
                     {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{_jpg_b64(img)}"}}]
    try:
        dados = _chamar(MODELO_VISAO, conteudo, 8000, 0.2)
        por_i = {int(d["i"]): d for d in dados.get("prints") or [] if isinstance(d, dict) and "i" in d}
    except Exception as exc:  # noqa: BLE001 — sem decupagem as frases saem só da fala
        log.warning("   thumb: decupagem falhou (%s)", exc)
        por_i = {}
    return [{"descricao": str(por_i.get(i, {}).get("descricao") or ""),
             "nota": float(por_i.get(i, {}).get("nota") or 0)} for i in range(len(prints))]


def _palavras(texto: str) -> list[str]:
    texto = unicodedata.normalize("NFD", texto.lower())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    return re.findall(r"[a-z0-9]+", texto)


def _apoiada(base: str, fala: str) -> bool:
    """O trecho citado está mesmo na fala (transcrição: tolera uma palavra ou outra diferente)."""
    b, f = _palavras(base), set(_palavras(fala))
    return len(b) >= 3 and sum(w in f for w in b) / len(b) >= 0.75


def frases(prints: list[dict], contexto: dict) -> list[dict]:
    """3 opções [{frase, destaque, print, base, angulo}], cada uma apoiada na
    fala de quem aparece no print escolhido."""
    blocos = []
    for i, p in enumerate(prints):
        if p.get("confirmado"):
            quem = ("A pessoa em destaque está FALANDO neste momento (confirmado por leitura da boca + voz).\n"
                    f"O que ELA diz aqui: \"{p['fala_pessoa'] or '(trecho curto, sem texto)'}\"")
        else:
            quem = "Não deu para confirmar quem está falando neste print."
        blocos.append(f"### Print {i}\nImagem: {p['descricao'] or '(sem descrição)'} (nota de thumb {p['nota']:g})\n"
                      f"{quem}\nFala ao redor (pode ter outras pessoas falando): \"{p['arredores']}\"")
    confirmados = [i for i, p in enumerate(prints) if p.get("confirmado") and len(_palavras(p["fala_pessoa"])) >= 4]
    if confirmados:
        regra_print = (f"- \"print\" só pode ser um destes: {confirmados} (os prints em que sabemos quem fala); a frase "
                       "tem que dizer o que A PESSOA DESSE PRINT disse ou defende — nunca a fala de outra pessoa "
                       "(entrevistador, convidado, vídeo citado);\n"
                       "- \"base\": copie, palavra por palavra, o trecho de \"O que ELA diz aqui\" desse print que "
                       "de onde saiu a sacada (no mínimo 4 palavras);")
    else:
        regra_print = "- \"base\": copie, palavra por palavra, o trecho da fala que sustenta a frase (no mínimo 4 palavras);"
    pedido = f"""Você escreve o texto de thumbnails de YouTube para um canal brasileiro de política/notícia. O vídeo é um trecho longo (15-20 min) de uma live.

Título do vídeo: {contexto.get('titulo') or '(sem título)'}
Tema: {contexto.get('tema') or '(nenhum)'}
Resumo: {contexto.get('comentario') or '(nenhum)'}

Prints candidatos (transcrição automática, pode errar palavras e nomes):

{chr(10).join(blocos)}

Escreva TRÊS opções de texto para a thumb, cada uma presa a um print e com um ângulo diferente (apelido/rótulo, acusação, revelação, confronto, pergunta provocativa, ironia):
- 2 a 5 palavras, no máximo 26 letras, português do Brasil, MAIÚSCULAS; prefira palavras curtas (a letra fica maior);
- bem polêmica e provocativa, que dê vontade de clicar (lacuna de curiosidade, tensão, conflito);
{regra_print}
- a frase NÃO deve repetir o que foi dito: extrapole e faça uma sacada — apelido, rótulo, veredito, ironia, exagero de manchete. Exemplos do tom: a pessoa conta que o Flávio cancelou o debate para não encarar ela → "FLÁVIO É UM FUJÃO"; reclama que parentes de ministros ganham causas no STF → "STF VIROU NEGÓCIO DE FAMÍLIA"; diz que a emissora tirou ela do debate → "A GLOBO TEM MEDO";
- pelo menos DUAS das três têm que ser sacadas com palavras que NÃO estão no "base"; no máximo uma pode ficar perto do literal;
- a sacada vale se for uma leitura que a própria pessoa assinaria; não vale inventar fato novo, número, crime ou acusação que não saia do que ela falou;
- "leitura": em poucas palavras, por que a sacada é fiel ao que a pessoa disse;
- primeira pessoa ("EU", "ME") só se a pessoa falou de si mesma;
- nomes de pessoas, partidos e lugares escritos do jeito certo (a transcrição erra nomes: corrija pelo contexto, ex.: "Bukelhe" → "Bukele");
- sem palavrão; não repita o título (a thumb complementa o título);
- "destaque": a palavra (ou duas) mais forte da frase, copiada como está na frase;
- prefira prints de nota alta e use prints diferentes nas três opções se houver.

Responda só JSON:
{{"opcoes": [{{"frase": "...", "destaque": "...", "print": 0, "base": "...", "leitura": "...", "angulo": "..."}}]}}"""
    aceitas, recusas = [], []
    for _ in range(2):
        texto = pedido if not recusas else pedido + (
            "\n\nAs opções abaixo foram RECUSADAS porque o \"base\" não está na fala da pessoa do print escolhido "
            "(ou o print não é permitido). Escreva outras:\n" + "\n".join(recusas))
        dados = _chamar(MODELO_FRASE, texto, 10000, 0.8)
        for o in dados.get("opcoes") or []:
            if not isinstance(o, dict) or not str(o.get("frase") or "").strip():
                continue
            try:
                i = int(o.get("print", -1))
            except (TypeError, ValueError):
                i = -1
            frase = " ".join(str(o["frase"]).upper().split())
            base = str(o.get("base") or "")
            valido = i in confirmados if confirmados else 0 <= i < len(prints)
            fala = (prints[i]["fala_pessoa"] if confirmados else prints[i]["arredores"]) if valido else ""
            if not valido or not _apoiada(base, fala):
                recusas.append(f"- {frase} (print {i}, base \"{base}\")")
                log.info("   thumb: frase recusada (sem apoio na fala do print %s): %s", i, frase)
                continue
            if any(a["frase"] == frase for a in aceitas):
                continue
            aceitas.append({"frase": frase, "destaque": " ".join(str(o.get("destaque") or "").upper().split()),
                            "print": i, "base": base, "leitura": str(o.get("leitura") or ""),
                            "angulo": str(o.get("angulo") or "")})
        if len(aceitas) >= 3:
            break
    if not aceitas:
        raise RuntimeError("nenhuma frase com apoio na fala de quem aparece")
    return aceitas[:3]


# ---------------------------------------------------------------------------
# 5. Composição
# ---------------------------------------------------------------------------

def _fonte(tamanho: int) -> ImageFont.FreeTypeFont:
    """Montserrat variável no peso mais pesado que o arquivo tiver."""
    fonte = ImageFont.truetype(str(moldura.FONTE), tamanho)
    for peso in ("Black", "ExtraBold"):
        try:
            fonte.set_variation_by_name(peso)
            break
        except (OSError, ValueError):
            continue
    return fonte


_segmentador = None


def _segmentar(rgb: np.ndarray) -> np.ndarray:
    """Probabilidade de ser pessoa (0-1), do tamanho da imagem."""
    global _segmentador
    import mediapipe as mp
    from crop_dinamico import garantir_modelo
    from mediapipe.tasks.python import BaseOptions, vision
    if _segmentador is None:
        garantir_modelo(_SEGMENTADOR, _URL_SEGMENTADOR)
        _segmentador = vision.ImageSegmenter.create_from_options(vision.ImageSegmenterOptions(
            base_options=BaseOptions(model_asset_path=str(_SEGMENTADOR)),
            output_confidence_masks=True, output_category_mask=False))
    res = _segmentador.segment(mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb)))
    fundo = res.confidence_masks[0].numpy_view().astype(np.float32)
    prob = 1.0 - (fundo[..., 0] if fundo.ndim == 3 else fundo)
    return cv2.resize(prob, (rgb.shape[1], rgb.shape[0]), interpolation=cv2.INTER_LINEAR)


def recortar_pessoa(img: np.ndarray, rosto: np.ndarray) -> tuple[Image.Image, tuple[float, float]] | None:
    """A pessoa do rosto, recortada (RGBA), do topo da cabeça até a borda de
    baixo do print, e onde fica o centro do rosto nela (0-1, 0-1). Só a
    mancha ligada ao rosto: vizinho de bancada sai."""
    H, W = img.shape[:2]
    x, y, w, h = (float(v) for v in rosto[:4])
    cx = x + w / 2
    x1, x2 = int(max(0, cx - 2.4 * w)), int(min(W, cx + 2.4 * w))
    y1 = int(max(0, y - 0.9 * h))
    peca = img[y1:H, x1:x2]
    if peca.shape[0] < 80 or peca.shape[1] < 80:
        return None
    rgb = cv2.cvtColor(peca, cv2.COLOR_BGR2RGB)
    prob = cv2.GaussianBlur(_segmentar(rgb), (0, 0), max(1.5, w / 50))
    binaria = (prob > 0.5).astype(np.uint8)
    n, rotulos = cv2.connectedComponents(binaria)
    if n < 2:
        return None
    # componente que mais cobre o rosto
    fx1, fy1 = int(x - x1), int(y - y1)
    caixa = rotulos[max(0, fy1):fy1 + int(h), max(0, fx1):fx1 + int(w)]
    contagem = np.bincount(caixa.ravel(), minlength=n)
    contagem[0] = 0
    if contagem.max() < 0.25 * w * h:
        return None
    pessoa = cv2.dilate((rotulos == contagem.argmax()).astype(np.uint8), np.ones((9, 9), np.uint8))
    # buraco dentro da pessoa (adesivo, crachá, vão entre braço e microfone) ganharia contorno: preenche
    contornos, _ = cv2.findContours(pessoa, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    cheia = np.zeros_like(pessoa)
    cv2.drawContours(cheia, contornos, -1, 1, thickness=cv2.FILLED)
    miolo = cv2.erode(cheia, np.ones((7, 7), np.uint8)) > 0
    alfa = np.where(miolo, 1.0, np.clip((prob - 0.35) / 0.3, 0, 1) * cheia).astype(np.float32)
    alfa = cv2.GaussianBlur(alfa, (3, 3), 0)
    rgba = np.dstack([rgb, (alfa * 255).astype(np.uint8)])
    im = Image.fromarray(rgba, "RGBA")
    cx1, cy1, cx2, cy2 = im.getchannel("A").point(lambda a: 255 if a > 40 else 0).getbbox() or (0, 0, 0, 0)
    if cx2 - cx1 < 40 or cy2 - cy1 < 40:
        return None
    rosto_rel = ((cx - x1 - cx1) / (cx2 - cx1), (y + h / 2 - y1 - cy1) / (cy2 - cy1))
    return im.crop((cx1, cy1, cx2, cy2)), rosto_rel


def _contorno(pessoa: Image.Image, cor, espessura: int) -> Image.Image:
    """A pessoa com contorno sólido em volta + sombra (o "adesivo" das thumbs)."""
    m = espessura * 2 + 20
    tela = Image.new("RGBA", (pessoa.width + 2 * m, pessoa.height + 2 * m), (0, 0, 0, 0))
    alfa = np.zeros((tela.height, tela.width), np.uint8)
    alfa[m:m + pessoa.height, m:m + pessoa.width] = np.asarray(pessoa.getchannel("A"))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * espessura + 1, 2 * espessura + 1))
    borda = cv2.GaussianBlur(cv2.dilate(np.where(alfa > 110, 255, 0).astype(np.uint8), k), (0, 0), espessura / 2)
    borda = cv2.GaussianBlur(np.where(borda > 127, 255, 0).astype(np.uint8), (0, 0), 1.0)
    grosso = Image.fromarray(borda, "L")
    sombra = grosso.filter(ImageFilter.GaussianBlur(14)).point(lambda a: round(a * 0.75))
    tela.paste(Image.new("RGBA", tela.size, (0, 0, 0, 255)), (0, 0), sombra)
    tela.paste(Image.new("RGBA", tela.size, tuple(cor) + (255,)), (0, 0), grosso)
    tela.alpha_composite(pessoa, (m, m))
    return tela


def _cobrir(img: np.ndarray, centro_x: float | None = None, zoom: float = 1.0, posicao: float = 0.5) -> Image.Image:
    """O print preenchendo 1280x720, com zoom, e centro_x (x do rosto no
    print) caindo em `posicao` da largura, se der."""
    im = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    escala = max(LARGURA / im.width, ALTURA / im.height) * zoom
    im = im.resize((round(im.width * escala), round(im.height * escala)), Image.LANCZOS)
    cx = (centro_x * escala) if centro_x is not None else im.width / 2
    x = int(min(max(0, cx - LARGURA * posicao), im.width - LARGURA))
    y = int(min(max(0, (im.height - ALTURA) / 3), im.height - ALTURA))
    return im.crop((x, y, x + LARGURA, y + ALTURA))


def _gradiente(cor_a, alfa_a: int, cor_b, alfa_b: int, vertical: bool = False) -> Image.Image:
    """De cor_a para cor_b, da esquerda para a direita (ou de cima para baixo)."""
    passo = Image.linear_gradient("L")                 # preto em cima, branco embaixo
    passo = (passo if vertical else passo.rotate(90)).resize((LARGURA, ALTURA))
    a = Image.new("RGBA", (LARGURA, ALTURA), tuple(cor_a) + (alfa_a,))
    b = Image.new("RGBA", (LARGURA, ALTURA), tuple(cor_b) + (alfa_b,))
    return Image.composite(b, a, passo)


def _linhas_possiveis(palavras: list[str], max_linhas: int):
    """Todas as quebras de `palavras` em 1..max_linhas linhas, na ordem."""
    n = len(palavras)
    for k in range(1, min(max_linhas, n) + 1):
        def quebras(inicio, resto):
            if resto == 1:
                yield [palavras[inicio:]]
                return
            for fim in range(inicio + 1, n - resto + 2):
                for depois in quebras(fim, resto - 1):
                    yield [palavras[inicio:fim]] + depois
        yield from quebras(0, k)


def _trechos(linha: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Junta palavras vizinhas de destaque num trecho só (uma caixa para "SE UNIU")."""
    saida = []
    for palavra, dest in linha:
        if saida and dest and saida[-1][1]:
            saida[-1] = (saida[-1][0] + " " + palavra, True)
        else:
            saida.append((palavra, dest))
    return saida


def _bloco_texto(frase: str, destaque: str, largura: int, altura: int, cor_texto, cor_destaque,
                 destaque_em_caixa: bool, max_linhas: int = 3, alinhamento: str = "esq") -> Image.Image:
    """Texto da thumb no maior tamanho que cabe em largura x altura.
    O destaque vai numa caixa colorida (texto escuro) ou só pintado na cor."""
    limpa = lambda p: p.strip(".,!?;:\"'“”…")     # noqa: E731
    alvos = {limpa(d) for d in destaque.split()} - {""}
    palavras = [(p, limpa(p) in alvos) for p in frase.split()]
    traco = lambda t: max(4, t // 11)          # noqa: E731
    espaco_caixa = lambda t: t // 7            # noqa: E731
    medida = ImageDraw.Draw(Image.new("L", (1, 1)))

    def larg_linha(linha, fonte, t):
        trechos = _trechos(linha)
        larg = sum(medida.textlength(x, font=fonte) for x, _ in trechos)
        larg += medida.textlength(" ", font=fonte) * (len(trechos) - 1)
        return larg + sum(2 * espaco_caixa(t) for _, dest in trechos if dest and destaque_em_caixa) + 2 * traco(t)

    melhor = None
    for linhas in _linhas_possiveis(palavras, max_linhas):
        lo, hi = 20, 260
        while lo < hi:                                     # maior tamanho que cabe
            t = (lo + hi + 1) // 2
            fonte = _fonte(t)
            alt = len(linhas) * t * 1.08 + 2 * traco(t)
            if alt <= altura and all(larg_linha(lin, fonte, t) <= largura for lin in linhas):
                lo = t
            else:
                hi = t - 1
        # empate: prefere linhas de tamanho parecido
        desequilibrio = np.std([len(" ".join(p for p, _ in lin)) for lin in linhas])
        nota = lo - desequilibrio * 0.8
        if melhor is None or nota > melhor[0]:
            melhor = (nota, lo, linhas)
    _, t, linhas = melhor
    fonte = _fonte(t)
    passo = round(t * 1.08)
    tr = traco(t)
    bloco = Image.new("RGBA", (largura, len(linhas) * passo + 2 * tr + 30), (0, 0, 0, 0))
    d = ImageDraw.Draw(bloco)
    y = tr
    for linha in linhas:
        lw = larg_linha(linha, fonte, t)
        x = tr + (0 if alinhamento == "esq" else (largura - lw) / 2 if alinhamento == "centro" else largura - lw)
        for texto, dest in _trechos(linha):
            pw = medida.textlength(texto, font=fonte)
            if dest and destaque_em_caixa:
                e = espaco_caixa(t)
                topo, base = y + t * 0.12, y + t * 1.06
                d.rounded_rectangle((x - tr // 2, topo - tr // 2, x + pw + 2 * e + tr // 2, base + tr // 2),
                                    radius=t // 8, fill=(0, 0, 0, 255))
                d.rounded_rectangle((x, topo, x + pw + 2 * e, base), radius=t // 10, fill=tuple(cor_destaque) + (255,))
                d.text((x + e, y), texto, font=fonte, fill=(15, 15, 15, 255))
                x += pw + 2 * e
            else:
                cor = cor_destaque if dest else cor_texto
                d.text((x, y), texto, font=fonte, fill=tuple(cor) + (255,), stroke_width=tr, stroke_fill=(0, 0, 0, 255))
                x += pw
            x += medida.textlength(" ", font=fonte)
        y += passo
    # sombra: uma dura deslocada (o "3D" das thumbs) + uma difusa
    alfa = bloco.getchannel("A")
    final = Image.new("RGBA", (bloco.width + 12, bloco.height + 14), (0, 0, 0, 0))
    difusa = Image.new("L", final.size, 0)
    difusa.paste(alfa.filter(ImageFilter.GaussianBlur(10)).point(lambda a: round(a * 0.8)), (4, 6))
    dura = Image.new("L", final.size, 0)
    dura.paste(alfa, (max(3, t // 22), max(4, t // 16)))
    preto = Image.new("RGBA", final.size, (0, 0, 0, 255))
    final.paste(preto, (0, 0), difusa)
    final.paste(preto, (0, 0), dura)
    final.alpha_composite(bloco)
    caixa = final.getchannel("A").getbbox()
    return final.crop(caixa) if caixa else final


def _colar_pessoa(tela: Image.Image, pessoa: Image.Image, rosto_rel: tuple[float, float],
                  centro_x: float, altura_alvo: float, cor) -> None:
    """Pessoa com o rosto em centro_x, alta como altura_alvo e encostada embaixo."""
    escala = altura_alvo / pessoa.height
    p = pessoa.resize((max(1, round(pessoa.width * escala)), max(1, round(pessoa.height * escala))), Image.LANCZOS)
    adesivo = _contorno(p, cor, 9)
    m = (adesivo.width - p.width) // 2
    x = round(centro_x - rosto_rel[0] * p.width) - m
    y = ALTURA - p.height - m + 30                      # passa um pouco da borda de baixo
    _colar(tela, adesivo, x, y)


def _colar(tela: Image.Image, camada: Image.Image, x: int, y: int) -> None:
    """alpha_composite que aceita camada saindo da tela."""
    x1, y1 = max(0, x), max(0, y)
    x2, y2 = min(tela.width, x + camada.width), min(tela.height, y + camada.height)
    if x2 > x1 and y2 > y1:
        tela.alpha_composite(camada.crop((x1 - x, y1 - y, x2 - x, y2 - y)), (x1, y1))


def compor(layout: str, p: dict, frase: str, destaque: str, dados: dict) -> Image.Image:
    principal, cor_destaque = moldura._cores(dados)
    if max(cor_destaque) - min(cor_destaque) < 60:      # destaque branco/cinza some na thumb
        cor_destaque = AMARELO
    recorte = None
    if layout != "tela_cheia" and p.get("rosto") is not None:
        recorte = recortar_pessoa(p["img"], p["rosto"])
    if recorte is None:
        layout = "tela_cheia"
    else:
        pessoa, rosto_rel = recorte
    cx_rosto = float(p["rosto"][0] + p["rosto"][2] / 2) if p.get("rosto") is not None else None

    if layout == "tela_cheia":
        tela = _cobrir(p["img"], cx_rosto, zoom=1.25, posicao=0.66).convert("RGBA")
        # o rosto vai para o terço direito; embaixo e à esquerda escurece para o texto
        baixo = _gradiente((0, 0, 0), 0, (0, 0, 0), 255, vertical=True)
        baixo.putalpha(baixo.getchannel("A").point(lambda a: 0 if a < 110 else round((a - 110) * 1.6)))
        tela.alpha_composite(baixo)
        tela.alpha_composite(_gradiente((0, 0, 0), 170, (0, 0, 0), 0))
        bloco = _bloco_texto(frase, destaque, 900, 250, (255, 255, 255), AMARELO, True, max_linhas=2)
        tela.alpha_composite(bloco, (44, ALTURA - bloco.height - 34))
        faixa = Image.new("RGBA", (LARGURA, 10), VERMELHO + (255,))
        tela.alpha_composite(faixa, (0, 0))
        return tela.convert("RGB")

    # destaque / contraste: fundo = o próprio print desfocado e tingido
    fundo = _cobrir(p["img"], cx_rosto, zoom=1.3).filter(ImageFilter.GaussianBlur(16)).convert("RGBA")
    if layout == "destaque":
        escuro = moldura._mistura(principal, (5, 5, 8), 0.75)
        fundo.alpha_composite(Image.new("RGBA", fundo.size, (0, 0, 0, 120)))
        fundo.alpha_composite(_gradiente(escuro, 250, escuro, 40))
        _colar_pessoa(fundo, pessoa, rosto_rel, LARGURA * 0.73, ALTURA * 1.02, (255, 255, 255))
        bloco = _bloco_texto(frase, destaque, 640, 470, (255, 255, 255), cor_destaque, True)
        fundo.alpha_composite(bloco, (44, (ALTURA - bloco.height) // 2))
    else:  # contraste
        fundo = Image.fromarray(cv2.cvtColor(cv2.cvtColor(np.asarray(fundo.convert("RGB")), cv2.COLOR_RGB2GRAY),
                                             cv2.COLOR_GRAY2RGB)).convert("RGBA")
        fundo.alpha_composite(_gradiente((20, 0, 0), 120, VERMELHO, 225))
        _colar_pessoa(fundo, pessoa, rosto_rel, LARGURA * 0.27, ALTURA * 1.02, AMARELO)
        bloco = _bloco_texto(frase, destaque, 610, 500, (255, 255, 255), AMARELO, False, alinhamento="dir")
        fundo.alpha_composite(bloco, (LARGURA - bloco.width - 40, (ALTURA - bloco.height) // 2))
    return fundo.convert("RGB")


LAYOUTS = ("destaque", "contraste", "tela_cheia")


def _fala_em(segs: list[dict], intervalos: list[tuple[float, float]]) -> str:
    """Segmentos da transcrição que caem (40% ou mais) dentro dos intervalos."""
    textos = []
    for s in segs:
        dur = max(0.1, s["end"] - s["start"])
        dentro = sum(max(0.0, min(b, s["end"]) - max(a, s["start"])) for a, b in intervalos)
        if dentro / dur >= 0.4 and s["text"].strip():
            textos.append(s["text"].strip())
    return " ".join(textos)


def gerar(fonte: Path, inicio: float, fim: float, remover: list[tuple[float, float]], segs: list[dict],
          deslocamento: float, pasta: Path, contexto: dict, perfil_nome: str) -> list[dict]:
    """Grava thumb_1..3.jpg (1280x720) em `pasta` e devolve
    [{arquivo, frase, destaque, base, layout, t}]. `inicio`/`fim`/`remover`
    estão no tempo de `fonte`; a transcrição `segs`, no tempo da live
    (= tempo de `fonte` + `deslocamento`). A análise fica em thumbs.json."""
    pasta.mkdir(parents=True, exist_ok=True)
    cands = candidatos(fonte, inicio, fim, remover)
    falas = quem_fala(fonte, cands, pasta)
    confirmados, outros = [], []
    for c, f in zip(cands, falas):
        if f is None:
            outros.append({**c, "confirmado": False, "falando": []})
            continue
        img = _quadro(fonte, f["t"])
        img = c["img"] if img is None else img
        confirmados.append({"t": f["t"], "img": img, "rosto": np.array(f["rosto"]), "confirmado": True,
                            "falando": f["falando"], "nota": _nota_rosto(img, f["rosto"])})
    log.info("   thumb: %d candidato(s), %d com quem fala confirmado (LR-ASD)", len(cands), len(confirmados))
    prints = sorted(confirmados, key=lambda p: -p["nota"])[:N_PRINTS]
    if len(prints) < 3:
        prints += sorted(outros, key=lambda p: -p["nota"])[:N_PRINTS - len(prints)]
    prints.sort(key=lambda p: p["t"])
    for p in prints:
        t_live = p["t"] + deslocamento
        p["fala_pessoa"] = _fala_em(segs, [(a + deslocamento, b + deslocamento) for a, b in p["falando"]])
        p["arredores"] = " ".join(s["text"].strip() for s in segs
                                  if t_live - ARREDORES <= s["start"] <= t_live + ARREDORES and s["text"].strip())
    for p, d in zip(prints, decupar(prints)):
        p.update(d)
    opcoes = frases(prints, contexto)
    dados = ai_srt.load_profile(perfil_nome) or {}
    saida = []
    for n, (o, layout) in enumerate(zip(opcoes, LAYOUTS), 1):
        p = prints[o["print"]]
        img = compor(layout, p, o["frase"], o["destaque"], dados)
        arquivo = f"thumb_{n}.jpg"
        img.save(pasta / arquivo, quality=92)
        saida.append({"arquivo": arquivo, "frase": o["frase"], "destaque": o["destaque"], "base": o["base"],
                      "leitura": o.get("leitura", ""),
                      "layout": layout, "t": round(p["t"], 1), "angulo": o.get("angulo", "")})
    for i, p in enumerate(prints):
        cv2.imwrite(str(pasta / f"print_{i}.jpg"), p["img"], [cv2.IMWRITE_JPEG_QUALITY, 85])
    (pasta / "thumbs.json").write_text(json.dumps({
        "modelos": {"visao": MODELO_VISAO, "frase": MODELO_FRASE},
        "prints": [{k: p.get(k) for k in ("t", "confirmado", "descricao", "nota", "fala_pessoa")} for p in prints],
        "opcoes": saida}, ensure_ascii=False, indent=1, default=float), encoding="utf-8")
    return saida


if __name__ == "__main__" and sys.argv[1:2] == ["--lrasd"]:
    sys.exit(_lrasd_main(sys.argv[2], sys.argv[3]))
