"""Micro-edição por emoção (PILOTO v2): punch-in suave e imagem em cartão.

Entra o clipe da Fase 2 já vertical (abertura P&B + corte principal) e sai o
mesmo clipe, com o mesmo áudio e o mesmo tempo — só a imagem muda. Por isso a
legenda, a censura e a trilha do acabamento (estudio/finalizar.py) continuam
valendo sem ajuste nenhum, e este passo entra entre a Fase 2 e o acabamento.

Quem decide o quê:
- O DeepSeek lê a fala (com tempo) e diz O QUE cada momento é e qual efeito
  combina: punch-in numa palavra forte, ou imagem de algo concreto citado.
- A voz (modelo de emoção audeering — "arousal", o quanto a pessoa está
  exaltada; na falta dele, o volume) mede a intensidade de cada trecho.
- Regras duras em Python, que a IA não quebra (base: B-Script/CHI 2019 e
  Wals et al. 2026): no máximo ~8s sem mudança, no mínimo 3s entre mudanças,
  uma mudança por vez, nada colado numa troca de câmera, nada na abertura.

v2, depois da primeira rodada ("imagens desconexas, zoom estranho"):
- A imagem entra NA palavra que a cita (se a palavra não for achada na fala,
  a imagem cai fora) e aparece como cartão com o nome embaixo, sobre o vídeo —
  quem fala continua na tela. Só vem do Wikidata (imagem principal do
  verbete); sem verbete com foto, sem imagem.
- Zoom sem degrau seco: só aproximação suave (curva em S), feita com o filtro
  `perspective` — posição em fração de pixel, não treme como o `zoompan`.
- Cache em disco de cada etapa (análise, plano da IA, imagens): refazer o
  vídeo depois de mexer num parâmetro ou no plano.json leva segundos.

Uso:
    python microedicao.py clipe.mp4 relatorio.json palavras.json saida.mp4 [--replanejar] [--previa]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

AQUI = Path(__file__).resolve().parent
if str(AQUI) not in sys.path:
    sys.path.insert(0, str(AQUI))

import crop_dinamico  # noqa: E402
import deepseek_client  # noqa: E402
from llm_client import _json  # noqa: E402

log = logging.getLogger("microedicao")

FONTE = AQUI.parent / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"

# Ritmo (segundos)
MAX_SEM_MUDANCA = 8.0
MIN_ENTRE_MUDANCAS = 3.0
LONGE_DE_TROCA_DE_CAMERA = 0.6
# Zoom (fator; 1.0 = sem zoom)
ZOOM_PUNCH = 1.10           # punch-in numa palavra forte
ZOOM_SUAVE = 1.05           # aproximação de preenchimento (teto de 8s)
SUBIDA = 0.35               # duração da aproximação (curva em S)
SEGURA = 1.0                # quanto segura depois do fim da palavra
DESCIDA = 0.8               # volta suave ao normal
FOCO_Y = 0.36               # altura do rosto no quadro vertical (0 = topo)
# Imagem em cartão
IMAGEM_DUR = 2.4
IMAGEM_RESERVA = IMAGEM_DUR
CARTAO_LARGURA, CARTAO_ALTURA_MAX, CARTAO_Y = 860, 680, 240
ENTRADA = 0.18              # fade + subida do cartão

_UA = "VideoMaker-microedicao/0.2 (piloto; contato: joaokko@gmail.com)"


# ---------------------------------------------------------------------------
# Onde já existe "corte" no clipe
# ---------------------------------------------------------------------------

def emendas_no_clipe(relatorio: dict, dur_clipe: float) -> tuple[float, list[float]]:
    """(início do corte principal no clipe, [tempos das emendas no clipe]).

    O relatório da Fase 2 guarda os cortes no tempo do bloco bruto; no clipe,
    tudo anda para trás o que foi removido antes, e para frente o que a
    abertura (+ transição) ocupa no começo."""
    bordas = relatorio.get("apara_bordas") or {"inicio": 0.0, "fim": relatorio["duracao_antes"]}
    inicio_bruto = bordas["inicio"]
    # duracao_depois já inclui a abertura; o principal é o bloco aparado
    # menos o que foi cortado, e a abertura é o que sobra antes dele.
    principal = (bordas["fim"] - bordas["inicio"]) - sum(c["fim"] - c["inicio"]
                                                          for c in relatorio.get("cortes", []))
    inicio_principal = max(0.0, dur_clipe - principal) if relatorio.get("abertura") else 0.0
    emendas, removido = [], 0.0
    for c in sorted(relatorio.get("cortes", []), key=lambda c: c["inicio"]):
        t = inicio_principal + (c["inicio"] - inicio_bruto) - removido
        emendas.append(round(t, 3))
        removido += c["fim"] - c["inicio"]
    return inicio_principal, emendas


# ---------------------------------------------------------------------------
# Emoção da voz
# ---------------------------------------------------------------------------

def curva_de_emocao(video: Path, pasta: Path, passo: float = 0.5) -> tuple[list[float], str]:
    """Agitação da voz (0-1) a cada `passo` segundos. Modelo audeering
    (arousal) se estiver instalado; senão, volume RMS normalizado."""
    import numpy as np
    import soundfile as sf
    wav = pasta / "voz16k.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-ac", "1",
                    "-ar", "16000", str(wav)], check=True)
    sinal, sr = sf.read(str(wav), dtype="float32")
    n = int(len(sinal) / sr / passo)
    janela = int(sr * 1.0)      # 1s de contexto de cada lado
    try:
        import torch
        from transformers import Wav2Vec2Processor
        from transformers.models.wav2vec2.modeling_wav2vec2 import (
            Wav2Vec2Model, Wav2Vec2PreTrainedModel)

        class _Regressao(torch.nn.Module):
            def __init__(self, config):
                super().__init__()
                self.dense = torch.nn.Linear(config.hidden_size, config.hidden_size)
                self.dropout = torch.nn.Dropout(config.final_dropout)
                self.out_proj = torch.nn.Linear(config.hidden_size, config.num_labels)

            def forward(self, x):
                return self.out_proj(self.dropout(torch.tanh(self.dense(self.dropout(x)))))

        class _Emocao(Wav2Vec2PreTrainedModel):
            def __init__(self, config):
                super().__init__(config)
                self.wav2vec2 = Wav2Vec2Model(config)
                self.classifier = _Regressao(config)
                self.post_init()

            def forward(self, x):
                return self.classifier(self.wav2vec2(x)[0].mean(dim=1))

        nome = "audeering/wav2vec2-large-robust-12-ft-emotion-msp-dim"
        proc = Wav2Vec2Processor.from_pretrained(nome)
        disp = "cuda" if torch.cuda.is_available() else "cpu"
        modelo = _Emocao.from_pretrained(nome).to(disp).eval()
        valores = []
        with torch.no_grad():
            for i in range(n):
                c = int((i + 0.5) * passo * sr)
                trecho = sinal[max(0, c - janela): c + janela]
                x = proc(trecho, sampling_rate=sr, return_tensors="pt").input_values.to(disp)
                valores.append(float(modelo(x)[0][0]))   # ordem: arousal, dominance, valence
        fonte = "audeering arousal"
    except Exception as exc:  # noqa: BLE001 — sem o modelo, o volume serve de aproximação
        log.warning("   modelo de emoção indisponível (%s); usando volume", exc)
        valores = []
        for i in range(n):
            c = int((i + 0.5) * passo * sr)
            trecho = sinal[max(0, c - janela // 2): c + janela // 2]
            valores.append(20 * math.log10(float(np.sqrt(np.mean(trecho ** 2))) + 1e-6))
        fonte = "volume"
    wav.unlink(missing_ok=True)
    lo, hi = min(valores), max(valores)
    return [round((v - lo) / (hi - lo + 1e-9), 3) for v in valores], fonte


def emocao_em(curva: list[float], t: float, passo: float = 0.5) -> float:
    i = min(len(curva) - 1, max(0, int(t / passo)))
    return curva[i] if curva else 0.0


# ---------------------------------------------------------------------------
# Plano da IA
# ---------------------------------------------------------------------------

_SISTEMA = """Você é editor de vídeos curtos verticais (Shorts/Reels/TikTok) de notícia e comentário político em português.
Recebe a fala de um corte com os tempos (segundos) e a agitação da voz (0 a 1) por trecho.
Planeje a MICRO-EDIÇÃO: poucas mudanças visuais, cada uma LIGADA ao que está sendo dito naquele segundo.

Tipos (uma por vez, nunca empilhar):
- "punch": aproximação suave de câmera numa PALAVRA forte (acusação, número chocante, frase de efeito,
  virada). Diga a palavra exata como está na fala. Use em indignação, ataque, clímax, revelação.
- "imagem": um cartão com a foto de algo citado aparece por ~2s enquanto a voz continua. SÓ quando a
  fala diz o NOME de algo concreto que tem verbete com foto na Wikipédia: pessoa pública, instituição,
  empresa, prédio, lugar. O cartão entra exatamente na palavra, então "palavra" tem que ser o nome
  como foi dito na fala. Nunca para ideia abstrata, nunca para algo só insinuado.
  "busca" = título do verbete na Wikipédia, só o nome (ex.: "Luiz Inácio Lula da Silva",
  "Ministério da Fazenda", "Palácio do Planalto"). "rotulo" = como mostrar o nome no cartão (curto).

Ritmo: uma mudança a cada 4-8 segundos. Prefira menos e certeiras a muitas e aleatórias.
Não planeje nada antes de INICIO_PRINCIPAL (é a abertura, já tem efeito próprio).

Responda SÓ JSON:
{"eventos": [{"t": 12.3, "tipo": "punch|imagem", "palavra": "palavra exata dita em t",
  "emocao": "indignação|ironia|explicação|clímax|revelação|...", "intensidade": 1-3,
  "busca": "só para imagem", "rotulo": "só para imagem", "motivo": "curto"}]}"""


_SISTEMA_IMAGENS = """Você é editor de vídeos curtos verticais (Shorts/Reels/TikTok) em português.
Recebe a fala de um corte com os tempos (segundos). Escolha IMAGENS para ilustrar o que está sendo dito:
um cartão com uma foto aparece por ~2s, exatamente quando a palavra é dita, enquanto a voz continua.

O que vale ilustrar (a foto vem do verbete na Wikipédia/Wikidata — foto, ou o logo):
- pessoa pública, partido, movimento, instituição, empresa, programa, lugar citados pelo NOME.
Nunca objeto genérico (faca, celular, dinheiro), nunca ideia abstrata, nunca algo só insinuado.

"palavra" = a palavra EXATA como foi dita na fala (o cartão entra nela).
"busca" = título do verbete na Wikipédia, só o nome (ex.: "Arthur do Val", "Movimento Brasil Livre",
"Rede Globo"). "rotulo" = texto curto mostrado no cartão.

Ritmo: uma imagem a cada ~6-12 segundos; prefira as mais ligadas à história do corte.
Não planeje nada antes de INICIO_PRINCIPAL (é a abertura).

Responda SÓ JSON:
{"eventos": [{"t": 12.3, "tipo": "imagem", "palavra": "...", "busca": "...", "rotulo": "...",
  "emocao": "...", "intensidade": 1-3, "motivo": "curto"}]}"""


def planejar(palavras: list[dict], curva: list[float], inicio_principal: float, dur: float,
             modelo: str = "deepseek-chat", so_imagens: bool = False) -> list[dict]:
    frases, atual = [], []
    for p in palavras:
        atual.append(p)
        if p["word"].endswith((".", "?", "!")) or len(atual) >= 14:
            frases.append(atual)
            atual = []
    if atual:
        frases.append(atual)
    linhas = [f"INICIO_PRINCIPAL = {inicio_principal:.2f}   DURACAO = {dur:.2f}", "", "FALA:"]
    for f in frases:
        ag = max(emocao_em(curva, p["start"]) for p in f)
        linhas.append(f"[{f[0]['start']:.2f}-{f[-1]['end']:.2f}] (agitação {ag:.2f}) "
                      + " ".join(p["word"] for p in f))
    resp = deepseek_client.post("/chat/completions", {
        "model": modelo, "temperature": 0.2, "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": _SISTEMA_IMAGENS if so_imagens else _SISTEMA},
                     {"role": "user", "content": "\n".join(linhas)}],
    }, 120, 3)
    return _json(resp["choices"][0]["message"].get("content") or "").get("eventos") or []


# ---------------------------------------------------------------------------
# Regras duras
# ---------------------------------------------------------------------------

def _norm(s: str) -> str:
    return re.sub(r"[^\wà-ú]", "", s.lower())


def _achar_palavra(palavras: list[dict], t: float, alvos: list[str], janela: float = 4.0) -> dict | None:
    """A palavra da fala, perto de t, que casa com algum dos alvos (tolera
    grafia do Whisper: basta começar igual nas 5 primeiras letras)."""
    chaves = {_norm(a)[:5] for alvo in alvos for a in alvo.split() if len(_norm(a)) >= 3}
    iguais = [p for p in palavras if abs(p["start"] - t) <= janela and _norm(p["word"])[:5] in chaves]
    return min(iguais, key=lambda p: abs(p["start"] - t)) if iguais else None


def _inicio_de_frase(palavras: list[dict], a: float, b: float, curva: list[float]) -> float | None:
    """Entre a e b, o começo de frase (palavra depois de pausa) com a voz mais exaltada."""
    cand = [p for i, p in enumerate(palavras)
            if a <= p["start"] <= b and (i == 0 or p["start"] - palavras[i - 1]["end"] >= 0.15)]
    if not cand:
        return None
    return max(cand, key=lambda p: emocao_em(curva, p["start"]))["start"]


def _ocupa(e: dict) -> float:
    return IMAGEM_RESERVA if e["tipo"] == "imagem" else 0.0


def aplicar_regras(eventos: list[dict], palavras: list[dict], curva: list[float],
                   inicio_principal: float, dur: float, trocas: list[float],
                   zoom: bool = True) -> list[dict]:
    """`zoom=False`: só as imagens (sem punch-in nem aproximação de preenchimento)."""
    livre_de = inicio_principal + 0.8
    if not zoom:
        eventos = [e for e in eventos if e.get("tipo") == "imagem"]
    ok = []
    for e in eventos:
        tipo = e.get("tipo")
        try:
            t = float(e.get("t"))
        except (TypeError, ValueError):
            continue
        if tipo not in ("punch", "imagem") or t < livre_de or t > dur - 1.5:
            continue
        alvos = [e.get("palavra") or ""] + ([e.get("busca") or "", e.get("rotulo") or ""] if tipo == "imagem" else [])
        p = _achar_palavra(palavras, t, [a for a in alvos if a])
        if p is None:
            log.info("   descartado (%s): %r não foi dito perto de %.1fs", tipo, e.get("palavra"), t)
            continue
        if tipo == "imagem" and not e.get("busca"):
            continue
        ok.append({"tipo": tipo, "t": p["start"], "fim_palavra": p["end"], "palavra": p["word"],
                   "busca": e.get("busca", ""), "rotulo": e.get("rotulo") or e.get("busca", ""),
                   "emocao": e.get("emocao", ""), "intensidade": int(e.get("intensidade") or 2),
                   "motivo": e.get("motivo", ""), "origem": "ia",
                   "agitacao": emocao_em(curva, p["start"])})

    # zoom longe de troca de câmera (o cartão fica por cima, a troca não atrapalha);
    # uma mudança por vez, com folga mínima
    ok = [e for e in ok if e["tipo"] == "imagem"
          or all(abs(e["t"] - c) >= LONGE_DE_TROCA_DE_CAMERA for c in trocas)]
    ok.sort(key=lambda e: (-(e["tipo"] == "imagem"), -e["intensidade"], -e["agitacao"]))
    escolhidos: list[dict] = []
    for e in ok:
        if all(e["t"] + _ocupa(e) + MIN_ENTRE_MUDANCAS <= o["t"] or
               o["t"] + _ocupa(o) + MIN_ENTRE_MUDANCAS <= e["t"] for o in escolhidos):
            escolhidos.append(e)

    # buracos maiores que o teto: aproximação suave no começo de frase mais
    # exaltado do buraco (troca de câmera já conta como mudança)
    marcos = sorted([livre_de, dur] + trocas + [e["t"] for e in escolhidos]
                    + [e["t"] + _ocupa(e) for e in escolhidos if _ocupa(e)])
    extras = []
    for a, b in zip(marcos, marcos[1:] if zoom else []):
        while b - a > MAX_SEM_MUDANCA:
            t = _inicio_de_frase(palavras, a + MIN_ENTRE_MUDANCAS, min(b, a + MAX_SEM_MUDANCA) - 1.0, curva)
            if t is None:
                t = a + MAX_SEM_MUDANCA - 1.0
            extras.append({"tipo": "suave", "t": round(t, 3), "fim_palavra": t + 0.4, "origem": "regra 8s",
                           "emocao": "", "intensidade": 1, "motivo": "teto de 8s sem mudança",
                           "agitacao": emocao_em(curva, t)})
            a = t
    return sorted(escolhidos + extras, key=lambda e: e["t"])


# ---------------------------------------------------------------------------
# Imagens: só a imagem principal (P18) do verbete no Wikidata
# ---------------------------------------------------------------------------

def _get_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def buscar_imagem(busca: str, pasta: Path) -> dict | None:
    """Retrato/foto "oficial" escolhida pela comunidade para o verbete — bem
    mais seguro do que uma busca livre no Commons (que trouxe o Lula ao lado
    de outro político na primeira rodada). Cache por termo de busca."""
    nome = "imagem_" + hashlib.md5(busca.lower().encode()).hexdigest()[:10]
    meta_cache = pasta / f"{nome}.json"
    if meta_cache.exists():
        dados = json.loads(meta_cache.read_text(encoding="utf-8"))
        return dados or None
    achado = None
    try:
        itens = _get_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
            {"action": "wbsearchentities", "search": busca, "language": "pt", "uselang": "pt",
             "limit": "3", "format": "json"})).get("search") or []
        for item in itens:
            claims = _get_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(
                {"action": "wbgetclaims", "entity": item["id"], "format": "json"})).get("claims", {})
            # Só coisa "fotografável" pelo tipo do verbete, não pelo que a IA
            # achou: pessoa (P31 = Q5), organização com logo (P154) ou lugar
            # com coordenadas (P625). Ideologia, crime, conceito, obra de arte
            # ficam de fora — na 1ª rodada "Nazismo" trouxe um cartaz nazista
            # e "Calúnia" um quadro do Botticelli.
            tipos = {c["mainsnak"].get("datavalue", {}).get("value", {}).get("id")
                     for c in claims.get("P31", [])}
            if "Q5" in tipos:
                fotos = claims.get("P18") or []
            elif claims.get("P154"):
                fotos = claims["P154"]                  # organização: o logo identifica melhor
            elif claims.get("P625"):
                fotos = claims.get("P18") or []
            else:
                log.info("   sem imagem para %r: verbete %s não é pessoa, organização nem lugar",
                         busca, item["id"])
                continue
            if not fotos:
                continue
            arquivo = fotos[0]["mainsnak"]["datavalue"]["value"]
            pags = _get_json("https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode(
                {"action": "query", "format": "json", "titles": f"File:{arquivo}", "prop": "imageinfo",
                 "iiprop": "url|extmetadata|mime", "iiurlwidth": "1080"}))["query"]["pages"]
            info = next(iter(pags.values()))["imageinfo"][0]
            # logo costuma ser SVG: o Commons entrega a miniatura já em PNG
            if info.get("mime") not in ("image/jpeg", "image/png") and not (
                    info.get("mime") == "image/svg+xml" and info.get("thumburl")):
                continue
            destino = pasta / f"{nome}.png"
            req = urllib.request.Request(info.get("thumburl") or info["url"], headers={"User-Agent": _UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                destino.write_bytes(r.read())
            meta = info.get("extmetadata") or {}
            achado = {"arquivo": str(destino), "wikidata": item["id"], "verbete": item.get("label", ""),
                      "pagina": info.get("descriptionurl", ""),
                      "autor": re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", "")).strip()[:120],
                      "licenca": (meta.get("LicenseShortName") or {}).get("value", "")}
            break
    except Exception as exc:  # noqa: BLE001
        log.warning("   Wikidata falhou para %r: %s", busca, exc)
        return None
    meta_cache.write_text(json.dumps(achado or {}, ensure_ascii=False), encoding="utf-8")
    return achado


def _cartao(img: Path, rotulo: str, destino: Path) -> Path:
    """PNG do cartão: foto com borda branca e o nome numa tarja embaixo."""
    esc = rotulo.replace("\\", "\\\\").replace("'", "’").replace(":", "\\:").replace("%", "\\%")
    fonte = str(FONTE).replace("\\", "/").replace(":", "\\:")
    filtro = (f"scale={CARTAO_LARGURA}:{CARTAO_ALTURA_MAX}:force_original_aspect_ratio=decrease,"
              "pad=iw+16:ih+16:8:8:white,"
              f"pad=iw:ih+74:0:0:white,"
              f"drawtext=fontfile='{fonte}':text='{esc}':fontsize=40:fontcolor=black:"
              "x=(w-text_w)/2:y=h-58")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(img), "-vf", filtro,
                    "-frames:v", "1", str(destino)], check=True)
    return destino


# ---------------------------------------------------------------------------
# Render
# ---------------------------------------------------------------------------

def segmentos_de_zoom(eventos: list[dict], dur: float) -> list[tuple[float, float, float, float]]:
    """(t0, t1, z0, z1) com curva em S dentro de cada um; fora deles, 1.0."""
    segs = []
    for e in eventos:
        if e["tipo"] not in ("punch", "suave"):
            continue
        alvo = ZOOM_SUAVE if e["tipo"] == "suave" else 1 + (ZOOM_PUNCH - 1) * (0.7 + 0.1 * min(3, e["intensidade"]))
        t0 = max(0.0, e["t"] - 0.1)             # começa um pouco antes: chega junto da palavra
        topo = t0 + SUBIDA
        solta = max(topo, e.get("fim_palavra", e["t"] + 0.3)) + SEGURA
        segs += [(t0, topo, 1.0, alvo), (topo, solta, alvo, alvo), (solta, min(dur, solta + DESCIDA), alvo, 1.0)]
    return [(round(a, 3), round(b, 3), round(z0, 4), round(z1, 4)) for a, b, z0, z1 in segs if b > a]


def _expr_zoom(segs, fps: float) -> str:
    t = f"(in/{fps})"
    partes = []
    for t0, t1, z0, z1 in segs:
        if z0 == z1:
            z = f"{z0 - 1:.4f}"
        else:
            u = f"(({t}-{t0})/{t1 - t0:.3f})"
            z = f"({z0 - 1:.4f}+({z1 - z0:.4f})*(3*{u}*{u}-2*{u}*{u}*{u}))"
        partes.append(f"gte({t},{t0})*lt({t},{t1})*{z}")
    return "(1+" + ("+".join(partes) or "0") + ")"


def _filtro_zoom(segs, fps: float) -> str:
    """Zoom com o `perspective`: mapeia os 4 cantos de uma janela menor
    (centrada no rosto) para o quadro inteiro, com interpolação — posição
    em fração de pixel, sem o tremido do zoompan."""
    if not segs:
        return "null"
    z = _expr_zoom(segs, fps)
    esq, dir_ = f"W/2-W/2/{z}", f"W/2+W/2/{z}"
    cima, baixo = f"H*{FOCO_Y}-H*{FOCO_Y}/{z}", f"H*{FOCO_Y}+H*{1 - FOCO_Y:.2f}/{z}"
    return (f"perspective=x0='{esq}':y0='{cima}':x1='{dir_}':y1='{cima}':"
            f"x2='{esq}':y2='{baixo}':x3='{dir_}':y3='{baixo}':interpolation=cubic:eval=frame")


def renderizar(clipe: Path, eventos: list[dict], destino: Path, pasta: Path, previa: bool = False) -> None:
    _larg, _alt, fps, dur = crop_dinamico.resolucao_de(clipe)
    cadeia = f"[0:v]{_filtro_zoom(segmentos_de_zoom(eventos, dur), fps)},setsar=1[v0]"
    entradas, atual, n = ["-i", str(clipe)], "v0", 1
    for e in eventos:
        if e["tipo"] != "imagem" or not e.get("cartao"):
            continue
        a, d = e["t"], e["duracao"]
        entradas += ["-loop", "1", "-t", f"{d:.3f}", "-i", e["cartao"]]
        cadeia += (f";[{n}:v]format=rgba,fade=in:st=0:d={ENTRADA}:alpha=1,"
                   f"fade=out:st={d - ENTRADA:.3f}:d={ENTRADA}:alpha=1,setpts=PTS-STARTPTS+{a}/TB[c{n}];"
                   f"[{atual}][c{n}]overlay=x=(W-w)/2:"
                   f"y='{CARTAO_Y}+40*max(0,1-(t-{a})/{ENTRADA})':eof_action=pass:"
                   f"enable='between(t,{a},{a + d:.3f})'[v{n}]")
        atual, n = f"v{n}", n + 1
    saida = "scale=540:960," if previa else ""
    cadeia += f";[{atual}]{saida}format=yuv420p[outv]"
    (pasta / "filtro.txt").write_text(cadeia, encoding="utf-8")
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *entradas,
                        "-/filter_complex", str(pasta / "filtro.txt"),
                        "-map", "[outv]", "-map", "0:a?", "-c:v", "libx264",
                        "-preset", "ultrafast" if previa else "medium", "-crf", "23" if previa else "18",
                        "-c:a", "copy", "-t", f"{dur:.3f}", str(destino)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou na micro-edição: {r.stderr[-1500:]}")


# ---------------------------------------------------------------------------
# Ponta a ponta (com cache de cada etapa)
# ---------------------------------------------------------------------------

def _cache(caminho: Path, gerar):
    if caminho.exists():
        return json.loads(caminho.read_text(encoding="utf-8"))
    dados = gerar()
    caminho.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    return dados


def processar(clipe: Path, relatorio: dict, palavras: list[dict], destino: Path, pasta: Path,
              replanejar: bool = False, previa: bool = False, so_render: bool = False,
              zoom: bool = True) -> dict:
    """`pasta` guarda o cache: analise.json (emendas, trocas de câmera, voz),
    plano_ia.json (resposta do DeepSeek), imagens, e plano.json (o que foi
    aplicado). `replanejar` pede outro plano à IA; `so_render` usa o
    plano.json como está (para testar ajustes feitos à mão nele)."""
    pasta.mkdir(parents=True, exist_ok=True)

    def analisar():
        _l, _a, _fps, dur = crop_dinamico.resolucao_de(clipe)
        inicio_principal, emendas = emendas_no_clipe(relatorio, dur)
        curva, fonte = curva_de_emocao(clipe, pasta)
        return {"duracao": dur, "inicio_principal": inicio_principal, "emendas": emendas,
                "trocas_de_camera": crop_dinamico.cortes_de_cena(clipe),
                "fonte_emocao": fonte, "curva_emocao": curva}

    an = _cache(pasta / "analise.json", analisar)
    log.info("   principal começa em %.2fs; %d troca(s) de câmera; emoção: %s",
             an["inicio_principal"], len(an["trocas_de_camera"]), an["fonte_emocao"])

    if so_render:
        eventos = json.loads((pasta / "plano.json").read_text(encoding="utf-8"))["eventos"]
    else:
        if replanejar:
            (pasta / "plano_ia.json").unlink(missing_ok=True)
        bruto = _cache(pasta / "plano_ia.json", lambda: planejar(
            palavras, an["curva_emocao"], an["inicio_principal"], an["duracao"], so_imagens=not zoom))
        eventos = aplicar_regras(bruto, palavras, an["curva_emocao"], an["inicio_principal"],
                                 an["duracao"], an["trocas_de_camera"], zoom=zoom)
        for i, e in enumerate(eventos):
            if e["tipo"] != "imagem":
                continue
            img = buscar_imagem(e["busca"], pasta)
            if img is None:
                e.update(tipo="descartado", motivo=f"sem foto no Wikidata para {e['busca']!r}")
                continue
            proxima = next((o["t"] for o in eventos if o["t"] > e["t"]), an["duracao"])
            e["duracao"] = round(max(1.5, min(IMAGEM_DUR, proxima - e["t"] - 0.5)), 2)
            e["imagem"] = img
        eventos = [e for e in eventos if e["tipo"] != "descartado"]

    for i, e in enumerate(eventos):
        if e["tipo"] == "imagem":
            e["cartao"] = str(_cartao(Path(e["imagem"]["arquivo"]), e.get("rotulo") or e["busca"],
                                      pasta / f"cartao_{i:02d}.png"))
    renderizar(clipe, eventos, destino, pasta, previa)
    plano = {"eventos": eventos}
    (pasta / "plano.json").write_text(json.dumps(plano, ensure_ascii=False, indent=1), encoding="utf-8")
    return plano


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("clipe", type=Path)
    ap.add_argument("relatorio", type=Path, help="relatorio.json do bloco (Fase 2)")
    ap.add_argument("palavras", type=Path, help="JSON com [{word,start,end}] no tempo do clipe")
    ap.add_argument("saida", type=Path)
    ap.add_argument("--replanejar", action="store_true", help="pede outro plano à IA")
    ap.add_argument("--so-render", action="store_true", help="refaz só o vídeo a partir do plano.json")
    ap.add_argument("--previa", action="store_true", help="540x960, rápido")
    ap.add_argument("--sem-zoom", action="store_true", help="só as imagens")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    plano = processar(args.clipe, json.loads(args.relatorio.read_text(encoding="utf-8")),
                      json.loads(args.palavras.read_text(encoding="utf-8")), args.saida,
                      args.saida.parent / (args.saida.stem + "_plano"),
                      replanejar=args.replanejar, previa=args.previa, so_render=args.so_render,
                      zoom=not args.sem_zoom)
    for e in plano["eventos"]:
        log.info("   %6.2fs %-7s %-11s %s", e["t"], e["tipo"], e.get("emocao", ""),
                 e.get("palavra") or e.get("motivo", ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
