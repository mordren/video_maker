"""Gerador do lower-third 'Informativo Nacional' para sobreposição em vídeos.

Estilo: fundo branco, logo à esquerda, subtítulo (verde) sobre título (preto),
com acentos verde/amarelo. Montado com filtros nativos do FFmpeg
(drawbox/drawtext), sem depender de Pillow nem de suporte a SVG.

Altura do lower-third exportada como LT_HEIGHT — o app usa esse valor para
posicionar o overlay no rodapé e afastar as legendas (MarginV).
"""

from pathlib import Path
import subprocess

from utils import escape_drawtext

# Cores padrão (identidade visual da bandeira brasileira). São só os padrões:
# a aba Configuração pode sobrescrever cada uma, e o app passa as escolhidas em
# `colors` para create_lower_third.
VERDE = "#1B7E3E"
AMARELO = "#FDD835"
BRANCO = "#FFFFFF"
PRETO = "#000000"

# Chaves de cor do lower-third e seus padrões. O FFmpeg aceita "#RRGGBB" tanto no
# fundo (color=c=) quanto no drawtext (fontcolor=), então guardamos hex direto.
DEFAULT_COLORS = {
    "banner": BRANCO,     # fundo da tarja
    "green": VERDE,       # acento vertical + chapéu (título pequeno)
    "yellow": AMARELO,    # detalhe de acento
    "headline": PRETO,    # manchete grande (subtítulo)
}


def merge_colors(colors: dict | None) -> dict:
    """Completa o dict de cores com os padrões, ignorando valores vazios."""
    merged = dict(DEFAULT_COLORS)
    for key, value in (colors or {}).items():
        if key in merged and value:
            merged[key] = value
    return merged

CG_WIDTH = 1080
LT_HEIGHT = 160  # altura do lower-third de fundo branco
# Distância entre a base do lower-third e a borda inferior do vídeo. O YouTube
# (Shorts/Reels) cobre a faixa de baixo com a própria interface — nome do canal,
# descrição, botões — então o CG precisa ficar acima dessa zona.
LT_BOTTOM_MARGIN = 416

_FONT = "C\\:/Windows/Fonts/arialbd.ttf"


# Altura de uma linha, como fração do fontsize (inclui espaçamento).
_LINE_RATIO = 1.15

# Largura de cada glifo da Arial Bold, em milésimos do fontsize. São as métricas
# da Helvetica-Bold, com que a Arial Bold é compatível. Medir caractere a
# caractere importa: "MANHÃ" e "III" têm 5 letras, mas a primeira ocupa quase o
# triplo da largura da segunda — um "ratio" médio por caractere erra feio nos
# dois sentidos (texto cortado num caso, fonte pequena à toa no outro).
_GLYPH_W = {
    " ": 278, "!": 333, '"': 474, "#": 556, "$": 556, "%": 889, "&": 722,
    "'": 238, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
    ".": 278, "/": 278, ":": 333, ";": 333, "<": 584, "=": 584, ">": 584,
    "?": 611, "@": 975, "[": 333, "\\": 278, "]": 333, "^": 584, "_": 556,
    "`": 333, "{": 389, "|": 280, "}": 389, "~": 584,
    "A": 722, "B": 722, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
    "H": 722, "I": 278, "J": 556, "K": 722, "L": 611, "M": 833, "N": 722,
    "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
    "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
    "a": 556, "b": 611, "c": 556, "d": 611, "e": 556, "f": 333, "g": 611,
    "h": 611, "i": 278, "j": 278, "k": 556, "l": 278, "m": 889, "n": 611,
    "o": 611, "p": 611, "q": 611, "r": 389, "s": 556, "t": 333, "u": 611,
    "v": 556, "w": 778, "x": 556, "y": 556, "z": 500,
}
for _digit in "0123456789":
    _GLYPH_W[_digit] = 556
# Acentuadas ocupam a mesma largura da letra-base.
for _accented, _base in (("ÁÀÂÃÄ", "A"), ("ÉÈÊË", "E"), ("ÍÌÎÏ", "I"),
                         ("ÓÒÔÕÖ", "O"), ("ÚÙÛÜ", "U"), ("Ç", "C"), ("Ñ", "N"),
                         ("áàâãä", "a"), ("éèêë", "e"), ("íìîï", "i"),
                         ("óòôõö", "o"), ("úùûü", "u"), ("ç", "c"), ("ñ", "n")):
    for _char in _accented:
        _GLYPH_W[_char] = _GLYPH_W[_base]

_FALLBACK_W = 611   # largura de segurança p/ caractere fora da tabela
_MIN_SIZE = 12      # abaixo disso não dá para ler no vídeo


def _text_width(text: str, size: int) -> float:
    """Largura do texto em pixels, na Arial Bold, para um dado fontsize."""
    return sum(_GLYPH_W.get(c, _FALLBACK_W) for c in text) * size / 1000


def _wrap_lines(text: str, n: int) -> list[str]:
    """Quebra `text` em até `n` linhas equilibradas pela largura medida."""
    words = text.split()
    if n <= 1 or len(words) <= 1:
        return [text] if text else []
    target = _text_width(text, 1000) / n   # largura alvo por linha
    space = _GLYPH_W[" "]
    lines: list[str] = []
    cur: list[str] = []
    cur_w = 0.0
    for word in words:
        word_w = _text_width(word, 1000)
        # Fecha a linha quando passar do alvo, deixando as demais palavras
        # para as próximas linhas (nunca mais que n linhas).
        if cur and cur_w + space + word_w > target and len(lines) < n - 1:
            lines.append(" ".join(cur))
            cur, cur_w = [word], word_w
        else:
            cur_w += (space + word_w) if cur else word_w
            cur.append(word)
    if cur:
        lines.append(" ".join(cur))
    return lines


def _fit_headline(text: str, avail_w: int, avail_h: float,
                  max_size: int = 40, max_lines: int = 2) -> tuple[list[str], int]:
    """Quebra e fonte da manchete que caibam na largura *e* na altura dadas.

    Para cada número de linhas testa os dois limites — o que a largura permite
    e o que a altura permite — e fica com o arranjo que rende a maior fonte.
    Como o tamanho sai de uma medição, e não de um piso arbitrário, o texto
    nunca é cortado na borda do CG.
    """
    if not text:
        return [], 0
    best_lines, best_size = [text], 0
    for n in range(1, max_lines + 1):
        lines = _wrap_lines(text, n)
        widest = max(_text_width(line, 1000) for line in lines)
        by_width = int(avail_w * 1000 / widest) if widest else max_size
        by_height = int(avail_h / (len(lines) * _LINE_RATIO))
        size = min(max_size, by_width, by_height)
        if size > best_size:
            best_lines, best_size = lines, size
    return best_lines, max(best_size, _MIN_SIZE)


def _fit_kicker(text: str, avail_w: int, max_size: int = 22) -> tuple[list[str], int]:
    """Fonte do chapéu (uma linha só, encolhe até caber na largura)."""
    if not text:
        return [], 0
    width = _text_width(text, 1000)
    size = min(max_size, int(avail_w * 1000 / width)) if width else max_size
    return [text], max(size, _MIN_SIZE)


def _lower_third_chain(titulo: str, subtitulo: str, logo_w: int,
                       colors: dict | None = None) -> str:
    """Monta a cadeia de filtros do lower-third de fundo branco.

    Layout: logo à esquerda, faixa de acento verde/amarelo, e à direita o
    título (chapéu verde, pequeno) em cima e o subtítulo (preto, grande)
    embaixo — a manchete em destaque é o subtítulo, como num GC de telejornal.

    As duas fontes saem de uma medição real do texto (largura glifo a glifo e
    altura do bloco), então nada é cortado nem sobra espaço vazio à toa. O
    subtítulo quebra em duas linhas quando isso rende uma fonte maior. O bloco
    inteiro é centralizado na vertical.
    """
    c = merge_colors(colors)
    verde, amarelo, preto = c["green"], c["yellow"], c["headline"]
    titulo = titulo.upper().strip()
    subtitulo = subtitulo.upper().strip()

    text_x = logo_w + 34            # início do texto, depois do logo + acento
    avail = CG_WIDTH - text_x - 30  # largura disponível até a margem direita
    avail_h = LT_HEIGHT - 24        # respiro em cima e embaixo

    # Chapéu (título): linha verde pequena, no topo.
    kicker_lines, kicker_size = _fit_kicker(titulo, avail)
    kicker_h = int(kicker_size * _LINE_RATIO) if kicker_lines else 0
    accent_gap = 14 if kicker_lines else 0   # espaço p/ o detalhe amarelo

    # Manchete (subtítulo): linha preta grande, embaixo, no espaço que sobrou.
    head_lines, head_size = _fit_headline(subtitulo, avail, avail_h - kicker_h - accent_gap)
    head_line_h = int(head_size * _LINE_RATIO) if head_lines else 0

    block_h = kicker_h + accent_gap + head_line_h * len(head_lines)
    top = max(10, (LT_HEIGHT - block_h) // 2)

    parts = [
        # Faixa vertical verde separando o logo do texto
        f"drawbox=x={logo_w + 8}:y=20:w=8:h={LT_HEIGHT - 40}:color={verde}@1:t=fill",
        # Faixa vertical amarela fininha colada na verde
        f"drawbox=x={logo_w + 18}:y=20:w=4:h={LT_HEIGHT - 40}:color={amarelo}@1:t=fill",
    ]
    head_y0 = top
    # Chapéu (verde, menor) no topo, com o detalhe amarelo abaixo dele.
    if kicker_lines:
        parts.append(
            f"drawtext=fontfile='{_FONT}':text='{escape_drawtext(kicker_lines[0])}':"
            f"x={text_x}:y={top}:fontsize={kicker_size}:fontcolor={verde}"
        )
        accent_y = top + kicker_h + 2
        head_y0 = accent_y + accent_gap
        parts.append(
            f"drawbox=x={text_x}:y={accent_y}:w=90:h=5:color={amarelo}@1:t=fill"
        )
    # Manchete (grande) embaixo, na cor da manchete.
    for i, line in enumerate(head_lines):
        parts.append(
            f"drawtext=fontfile='{_FONT}':text='{escape_drawtext(line)}':"
            f"x={text_x}:y={head_y0 + i * head_line_h}:"
            f"fontsize={head_size}:fontcolor={preto}"
        )
    return ",".join(parts)


def create_lower_third(titulo: str, subtitulo: str, output_dir: Path,
                       logo_path: Path | None = None,
                       colors: dict | None = None) -> Path | None:
    """Gera um PNG do lower-third (tarja, logo à esquerda, título+subtítulo).

    Args:
        titulo: Chapéu curto (linha pequena, no topo).
        subtitulo: Manchete em destaque (linha grande, embaixo).
        output_dir: Diretório para salvar o PNG.
        logo_path: Caminho opcional do PNG do logo (fica à esquerda).
        colors: cores da tarja (banner/green/yellow/headline); usa os padrões da
            bandeira quando None ou parcial.

    Returns:
        Caminho do PNG gerado, ou None se falhar.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    lt_path = output_dir / "lower_third.png"
    logo_w = 210 if (logo_path and logo_path.exists()) else 40

    c = merge_colors(colors)
    chain = _lower_third_chain(titulo, subtitulo, logo_w, c)

    command = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={c['banner']}:s={CG_WIDTH}x{LT_HEIGHT}:d=1",
    ]

    if logo_path and logo_path.exists():
        command += ["-i", str(logo_path)]
        logo_h = LT_HEIGHT - 30
        filter_complex = (
            f"[0:v]{chain}[bg];"
            f"[1:v]scale={logo_w - 20}:{logo_h}:force_original_aspect_ratio=decrease[logo];"
            f"[bg][logo]overlay=x=20:y=(H-h)/2[out]"
        )
        command += [
            "-filter_complex", filter_complex, "-map", "[out]",
            "-frames:v", "1", str(lt_path),
        ]
    else:
        command += ["-vf", chain, "-frames:v", "1", str(lt_path)]

    try:
        result = subprocess.run(command, capture_output=True, timeout=15)
        if result.returncode == 0 and lt_path.exists():
            return lt_path
    except Exception:
        pass
    return None
