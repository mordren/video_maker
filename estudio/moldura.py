"""Moldura do vídeo longo: o vídeo encolhe para 90% e fica dentro de uma
moldura com as cores e o logo do perfil (canal).

Numa live todo canto já está ocupado (logo do programa, QR code, contador,
faixa de rodapé), então carimbar o logo por cima sempre tampa alguma coisa.
Em vez disso o vídeo ganha uma borda na cor de destaque, o fundo em volta
leva o tom do canal, o logo entra como selo no canto de baixo e o rodapé
mostra o nome do canal.

A moldura é um PNG 1920x1080 com um "buraco" transparente de cantos
arredondados onde o vídeo aparece — o ffmpeg só põe o vídeo em (X, Y) e
cola o PNG por cima (ver `filtro`).
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

import ai_srt

RAIZ = Path(__file__).resolve().parent.parent
FONTE = RAIZ / "assets" / "fonts" / "Montserrat-ExtraBold.ttf"

LARGURA, ALTURA = 1920, 1080
VIDEO_W, VIDEO_H = 1728, 972                 # 90% do quadro, ainda 16:9
X, Y = (LARGURA - VIDEO_W) // 2, 24          # o que sobra embaixo é o rodapé (84 px)
RAIO = 22
BORDA = 5


def _rgb(cor: str, padrao: str) -> tuple[int, int, int]:
    cor = (cor or padrao).strip().lstrip("#")
    try:
        return tuple(int(cor[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError:
        return _rgb(padrao, "#000000")


def _mistura(a, b, t: float):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _luz(c) -> float:
    return (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255


def _cores(dados: dict) -> tuple[tuple, tuple]:
    """(principal, destaque). No perfil, "green" é a cor forte do canal e
    "yellow" a de destaque; se as duas forem claras demais para um fundo
    escuro (ou iguais), cai no banner."""
    principal = _rgb(dados.get("brand_color_green"), "#1B7E3E")
    destaque = _rgb(dados.get("brand_color_yellow"), "#FDD835")
    if principal == destaque:
        destaque = _rgb(dados.get("brand_color_banner"), "#FFFFFF")
    return principal, destaque


def _fundo(principal) -> Image.Image:
    """Escuro com o tom do canal: gradiente diagonal + listras finas."""
    escuro = _mistura(principal, (8, 8, 10), 0.86)
    claro = _mistura(principal, (8, 8, 10), 0.62)
    # diagonal: média de um gradiente horizontal e um vertical
    horiz = Image.linear_gradient("L").rotate(90).resize((LARGURA, ALTURA))
    vert = Image.linear_gradient("L").resize((LARGURA, ALTURA))
    grad = ImageChops.add(horiz, vert, scale=2)
    fundo = Image.composite(Image.new("RGB", (LARGURA, ALTURA), claro),
                            Image.new("RGB", (LARGURA, ALTURA), escuro), grad)
    listras = Image.new("L", (LARGURA, ALTURA), 0)
    d = ImageDraw.Draw(listras)
    for x in range(-ALTURA, LARGURA, 26):
        d.line([(x, ALTURA), (x + ALTURA, 0)], fill=22, width=5)
    return Image.composite(Image.new("RGB", (LARGURA, ALTURA), _mistura(claro, (255, 255, 255), 0.25)),
                           fundo, listras)


def _fonte(tamanho: int) -> ImageFont.FreeTypeFont:
    """O arquivo é a Montserrat variável: sem escolher o peso, sai a Thin."""
    fonte = ImageFont.truetype(str(FONTE), tamanho)
    try:
        fonte.set_variation_by_name("ExtraBold")
    except (OSError, ValueError):
        pass
    return fonte


def _logo(caminho: str | None, altura: int) -> Image.Image | None:
    if not caminho or not Path(caminho).exists():
        return None
    im = Image.open(caminho).convert("RGBA")
    caixa = im.getchannel("A").point(lambda a: 255 if a > 24 else 0).getbbox()
    if caixa:
        im = im.crop(caixa)
    return im.resize((round(im.width * altura / im.height), altura), Image.LANCZOS)


def _sombra(camada: Image.Image, raio: int = 12, opacidade: float = 0.6) -> Image.Image:
    alfa = camada.getchannel("A").filter(ImageFilter.GaussianBlur(raio))
    alfa = alfa.point(lambda a: round(a * opacidade))
    sombra = Image.new("RGBA", camada.size, (0, 0, 0, 0))
    sombra.putalpha(alfa)
    return sombra


def gerar(perfil_nome: str, destino: Path) -> Path:
    """Desenha a moldura do perfil em `destino` (PNG RGBA 1920x1080)."""
    dados = ai_srt.load_profile(perfil_nome) or {}
    principal, destaque = _cores(dados)
    nome = str(dados.get("brand_name") or perfil_nome).strip().upper()

    tela = _fundo(principal).convert("RGBA")

    # sombra do vídeo sobre o fundo (fica por baixo da borda)
    caixa = (X, Y, X + VIDEO_W, Y + VIDEO_H)
    sombra = Image.new("L", (LARGURA, ALTURA), 0)
    ImageDraw.Draw(sombra).rounded_rectangle((X + 4, Y + 10, X + VIDEO_W + 4, Y + VIDEO_H + 10), RAIO, fill=190)
    tela = Image.composite(Image.new("RGBA", tela.size, (0, 0, 0, 255)), tela,
                           sombra.filter(ImageFilter.GaussianBlur(16)))

    # buraco do vídeo (cantos arredondados) + borda na cor de destaque
    buraco = Image.new("L", (LARGURA, ALTURA), 0)
    ImageDraw.Draw(buraco).rounded_rectangle(caixa, RAIO, fill=255)
    tela.putalpha(ImageChops.invert(buraco))
    ImageDraw.Draw(tela).rounded_rectangle((X - BORDA, Y - BORDA, X + VIDEO_W + BORDA - 1, Y + VIDEO_H + BORDA - 1),
                                           RAIO + BORDA, outline=destaque + (255,), width=BORDA)

    # rodapé: faixa fina principal → destaque colada na borda de baixo
    d = ImageDraw.Draw(tela)
    faixa_y = ALTURA - 8
    for x in range(LARGURA):
        d.line([(x, faixa_y), (x, ALTURA)], fill=_mistura(principal, destaque, x / LARGURA) + (255,))

    # selo do logo, mordendo o canto de baixo à esquerda
    logo = _logo(dados.get("brand_logo"), 150)
    texto_x = X + 30
    if logo is not None:
        selo = Image.new("RGBA", tela.size, (0, 0, 0, 0))
        lx, ly = X - 30, ALTURA - logo.height - 6
        selo.alpha_composite(logo, (lx, ly))
        tela.alpha_composite(_sombra(selo, 10, 0.7))
        tela.alpha_composite(selo)
        texto_x = lx + logo.width + 22

    # nome do canal + etiqueta "INSCREVA-SE" à direita
    base_y = Y + VIDEO_H + BORDA + (ALTURA - 8 - (Y + VIDEO_H + BORDA)) // 2
    fonte = _fonte(38)
    d.text((texto_x, base_y), nome, font=fonte, fill=(255, 255, 255, 255), anchor="lm")
    fim_nome = texto_x + d.textlength(nome, font=fonte)
    d.rectangle((texto_x, base_y + 22, texto_x + min(120, fim_nome - texto_x), base_y + 26), fill=destaque + (255,))

    fonte_p = _fonte(24)
    rotulo = "▶  INSCREVA-SE"
    try:
        larg = d.textlength(rotulo, font=fonte_p)
    except Exception:  # noqa: BLE001 — fonte sem o triângulo
        rotulo = "INSCREVA-SE"
        larg = d.textlength(rotulo, font=fonte_p)
    px2 = X + VIDEO_W - 6
    px1 = px2 - larg - 44
    texto_pilula = (20, 20, 20) if _luz(destaque) > 0.55 else (255, 255, 255)
    d.rounded_rectangle((px1, base_y - 22, px2, base_y + 22), 22, fill=destaque + (255,))
    d.text(((px1 + px2) / 2, base_y), rotulo, font=fonte_p, fill=texto_pilula + (255,), anchor="mm")

    destino.parent.mkdir(parents=True, exist_ok=True)
    tela.save(destino)
    return destino


def filtro(entrada_video: str = "0:v", entrada_moldura: str = "1:v", saida: str = "v") -> str:
    """filter_complex que põe o vídeo no buraco da moldura."""
    return (f"[{entrada_video}]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease,"
            f"pad={VIDEO_W}:{VIDEO_H}:(ow-iw)/2:(oh-ih)/2,setsar=1,"
            f"pad={LARGURA}:{ALTURA}:{X}:{Y}:black[_vm];"
            f"[_vm][{entrada_moldura}]overlay=0:0:format=auto,format=yuv420p[{saida}]")
