"""Legenda "new" do Estúdio (a "old" é o SRT amarelo de sempre, em finalizar.py).

  - gancho (a abertura que a Fase 2 põe no começo do corte): frase com glow —
    linha normal em branco + as últimas palavras em destaque amarelo, com halo;
  - resto do corte: karaokê com caixa — frase curta (até 3 palavras / 18
    letras) em branco, e a palavra que está sendo falada vira uma caixa
    amarela.

Por que duas camadas nos dois efeitos:
  - glow: \\blur na mesma linha do texto derrete a letra. Embaixo (\\layer 0)
    vai só o halo, borrado e semitransparente; em cima (\\layer 1) o texto
    nítido, sem blur.
  - caixa: BorderStyle=3 (caixa) não dá pra ligar só numa palavra por tag.
    Embaixo vai a frase inteira em branco; em cima a mesma frase em modo
    caixa, com só a palavra ativa visível (as outras e os espaços com alpha
    100%). A geometria das duas camadas é a mesma, então a caixa cai
    exatamente em cima da palavra e não vaza para o espaço seguinte.

Coordenadas do script em 1080x1920; o libass escala para o tamanho real.
"""

from __future__ import annotations

import re
from pathlib import Path

import censor

LARGURA, ALTURA = 1080, 1920
FONTE = "Montserrat"   # a única em assets/fonts (ExtraBold); o servidor não tem Arial


def _cor(r: int, g: int, b: int) -> str:
    return f"&H00{b:02X}{g:02X}{r:02X}&"


BRANCO = _cor(255, 255, 255)
PRETO = _cor(0, 0, 0)
AMARELO = _cor(255, 221, 0)
INVISIVEL = "&HFF&"
VISIVEL = "&H00&"

# karaokê com caixa
MAX_PALAVRAS = 3
MAX_LETRAS = 18       # cabe numa linha com folga na fonte 64
PAUSA_QUEBRA = 0.40
TAMANHO = 64
FOLGA_CAIXA = 8
SEPARADOR = " \\h"    # espaço largo (e ainda quebrável): a caixa não encosta na vizinha

# glow no gancho
GLOW_PALAVRAS = 5
GLOW_NORMAL = 60
GLOW_DESTAQUE = 92


def _tempo(t: float) -> str:
    t = max(0.0, t)
    cs = round(t * 100)
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _dlg(inicio: float, fim: float, estilo: str, texto: str, camada: int = 0) -> str:
    return f"Dialogue: {camada},{_tempo(inicio)},{_tempo(fim)},{estilo},,0,0,0,,{texto}"


def _limpar(palavras: list[dict], censura: list[str]) -> list[dict]:
    """Tira palavras vazias e aplica a censura (mesma grafia do SRT antigo).
    A censura roda no texto corrido, para pegar também expressões com espaço
    ('filho da puta'). O disfarce troca um caractere por outro (m4tar), sem
    mudar o tamanho, então cada palavra é recortada de volta pela posição."""
    ws = [dict(w, word=w["word"].strip()) for w in palavras if w.get("word", "").strip()]
    if censura and ws:
        texto = " ".join(w["word"] for w in ws)
        novo, n = censor.censor_text(texto, censura)
        if n and len(novo) == len(texto):
            pos = 0
            for w in ws:
                w["word"] = novo[pos:pos + len(w["word"])]
                pos += len(w["word"]) + 1
    return ws


def _frases_curtas(palavras: list[dict]) -> list[list[dict]]:
    frases, atual = [], []
    for i, w in enumerate(palavras):
        atual.append(w)
        prox = palavras[i + 1] if i + 1 < len(palavras) else None
        pontuacao = re.search(r"[.,!?;:]$", w["word"])
        pausa = prox is not None and prox["start"] - w["end"] > PAUSA_QUEBRA
        estoura = prox is not None and len(" ".join(x["word"] for x in atual + [prox])) > MAX_LETRAS
        if len(atual) >= MAX_PALAVRAS or pontuacao or pausa or estoura or prox is None:
            frases.append(atual)
            atual = []
    return frases


def _glow(gancho: list[dict], y: int) -> tuple[list[str], list[str]]:
    estilo = (f"Style: Glow,{FONTE},{GLOW_NORMAL},{BRANCO},{BRANCO},{PRETO},&H00000000,"
              f"1,0,0,0,100,100,0,0,1,3,0,2,60,60,0,1")
    entrada = f"{{\\fad(150,0)\\move(540,{y + 40},540,{y},0,220)}}"
    eventos = []
    grupos = [gancho[i:i + GLOW_PALAVRAS] for i in range(0, len(gancho), GLOW_PALAVRAS)]
    for n, grupo in enumerate(grupos):
        n_destaque = 2 if len(grupo) >= 4 else 1
        normal, destaque = grupo[:-n_destaque], grupo[-n_destaque:]
        texto_normal = " ".join(w["word"] for w in normal)
        texto_destaque = " ".join(w["word"].upper() for w in destaque)
        inicio = grupo[0]["start"]
        fim = grupo[-1]["end"] + 0.15
        if n + 1 < len(grupos):
            fim = min(fim, grupos[n + 1][0]["start"])

        # camada 0: só o halo; a linha normal vai invisível só para manter a
        # mesma quebra de linha da camada nítida
        halo = [f"{{\\fs{GLOW_NORMAL}\\1a{INVISIVEL}\\3a{INVISIVEL}}}{texto_normal}"] if texto_normal else []
        halo.append(f"{{\\fs{GLOW_DESTAQUE}\\bord9\\blur16\\shad0\\1c{AMARELO}\\1a&H55&"
                    f"\\3c{AMARELO}\\3a&H30&}}{texto_destaque}")
        eventos.append(_dlg(inicio, fim, "Glow", entrada + "\\N".join(halo), 0))

        # camada 1: texto nítido, borda branca no destaque
        nitida = [f"{{\\fs{GLOW_NORMAL}\\c{BRANCO}\\bord3\\blur0\\shad0}}{texto_normal}"] if texto_normal else []
        nitida.append(f"{{\\fs{GLOW_DESTAQUE}\\c{AMARELO}\\bord4\\blur0\\3c{BRANCO}\\shad0}}{texto_destaque}")
        eventos.append(_dlg(inicio, fim, "Glow", entrada + "\\N".join(nitida), 1))
    return [estilo], eventos


def _karaoke_caixa(frases: list[list[dict]], y: int) -> tuple[list[str], list[str]]:
    estilos = [
        f"Style: KLeitura,{FONTE},{TAMANHO},{BRANCO},{BRANCO},{PRETO},&H00000000,"
        f"1,0,0,0,100,100,0,0,1,4,0,2,60,60,0,1",
        f"Style: KCaixa,{FONTE},{TAMANHO},{PRETO},{PRETO},{AMARELO},&H00000000,"
        f"1,0,0,0,100,100,0,0,3,{FOLGA_CAIXA},0,2,60,60,0,1",
    ]
    pos = f"{{\\an2\\pos(540,{y})}}"
    eventos = []
    for n, frase in enumerate(frases):
        prox_inicio = frases[n + 1][0]["start"] if n + 1 < len(frases) else frase[-1]["end"] + 0.4
        fim_frase = min(frase[-1]["end"] + 0.3, prox_inicio)
        eventos.append(_dlg(frase[0]["start"], fim_frase, "KLeitura",
                            pos + SEPARADOR.join(w["word"].upper() for w in frase), 0))
        for i, w in enumerate(frase):
            fim_palavra = frase[i + 1]["start"] if i + 1 < len(frase) else fim_frase
            partes = []
            for j, outra in enumerate(frase):
                a = VISIVEL if j == i else INVISIVEL
                # fecha invisível logo depois da palavra: o espaço não entra na caixa
                partes.append(f"{{\\1a{a}\\3a{a}}}{outra['word'].upper()}{{\\1a{INVISIVEL}\\3a{INVISIVEL}}}")
            eventos.append(_dlg(w["start"], fim_palavra, "KCaixa", pos + SEPARADOR.join(partes), 1))
    return estilos, eventos


def gerar(palavras: list[dict], destino: Path, fim_gancho: float, margem_inferior: int,
          censura: list[str] | None = None) -> bool:
    """Grava o .ass em `destino`. `fim_gancho`: segundos do começo do corte
    que são o gancho (0 = sem gancho, tudo em karaokê). `margem_inferior`: em
    pixels (quadro de 1920), onde fica a base da legenda — a mesma conta que a
    legenda antiga faz (acima do GC, ou na emenda do formato "imagens").
    Devolve False se não há nenhuma palavra para escrever."""
    ws = _limpar(palavras, censura or [])
    if not ws:
        return False
    y = ALTURA - margem_inferior
    gancho = [w for w in ws if w["start"] < fim_gancho]
    resto = [w for w in ws if w["start"] >= fim_gancho]
    estilos, eventos = [], []
    if gancho:
        e, ev = _glow(gancho, y)
        estilos += e
        eventos += ev
    if resto:
        e, ev = _karaoke_caixa(_frases_curtas(resto), y)
        estilos += e
        eventos += ev
    destino.write_text(
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {LARGURA}\nPlayResY: {ALTURA}\nWrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(estilos) + "\n\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(eventos) + "\n",
        encoding="utf-8")
    return True
