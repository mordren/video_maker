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

# Cores (identidade visual da bandeira brasileira)
VERDE = "#1B7E3E"
AMARELO = "#FDD835"
BRANCO = "#FFFFFF"

CG_WIDTH = 1080
LT_HEIGHT = 170  # altura do lower-third de fundo branco

_FONT = "C\\:/Windows/Fonts/arialbd.ttf"


# Largura média de um caractere em Arial Bold, como fração do fontsize.
_CHAR_RATIO = 0.62
# Altura de uma linha, como fração do fontsize (inclui espaçamento).
_LINE_RATIO = 1.15


def _fit_fontsize(text: str, max_width: int, max_size: int, min_size: int) -> int:
    """Maior fontsize (Arial bold) em que `text` cabe em `max_width`."""
    if not text:
        return max_size
    ideal = int(max_width / (len(text) * _CHAR_RATIO))
    return max(min_size, min(max_size, ideal))


def _lower_third_chain(title: str, subtitle: str, logo_w: int) -> str:
    """Monta a cadeia de filtros do lower-third de fundo branco.

    Layout: logo à esquerda, faixa de acento verde/amarelo, e à direita o
    subtítulo (verde) sobre o título (preto). O tamanho das fontes é calculado
    a partir do comprimento do texto e o bloco (subtítulo + título) é
    centralizado verticalmente — assim textos curtos e longos ficam sempre
    distribuídos da mesma forma.
    """
    title = title.upper().strip()
    subtitle = subtitle.upper().strip()
    t = escape_drawtext(title)
    sub = escape_drawtext(subtitle)

    text_x = logo_w + 34          # início do texto, depois do logo + acento
    avail = CG_WIDTH - text_x - 30  # largura disponível até a margem direita

    # Fontes calculadas pelo comprimento (subtítulo é sempre menor que o título).
    title_size = _fit_fontsize(title, avail, max_size=54, min_size=24)
    sub_size = _fit_fontsize(subtitle, avail, max_size=32, min_size=20) if subtitle else 0

    # Altura de cada linha e do bloco inteiro, para centralizar na vertical.
    sub_h = int(sub_size * _LINE_RATIO) if subtitle else 0
    title_h = int(title_size * _LINE_RATIO)
    accent_gap = 14 if subtitle else 0   # espaço p/ o detalhe amarelo
    block_h = sub_h + accent_gap + title_h
    top = max(16, (LT_HEIGHT - block_h) // 2)

    parts = [
        # Faixa vertical verde separando o logo do texto
        f"drawbox=x={logo_w + 8}:y=20:w=8:h={LT_HEIGHT - 40}:color={VERDE}@1:t=fill",
        # Faixa vertical amarela fininha colada na verde
        f"drawbox=x={logo_w + 18}:y=20:w=4:h={LT_HEIGHT - 40}:color={AMARELO}@1:t=fill",
    ]
    title_y = top
    if subtitle:
        sub_y = top
        accent_y = sub_y + sub_h + 2
        title_y = accent_y + accent_gap
        parts.append(
            f"drawtext=fontfile='{_FONT}':text='{sub}':x={text_x}:y={sub_y}:"
            f"fontsize={sub_size}:fontcolor={VERDE}"
        )
        # Detalhe amarelo entre subtítulo e título
        parts.append(
            f"drawbox=x={text_x}:y={accent_y}:w=90:h=5:color={AMARELO}@1:t=fill"
        )
    parts.append(
        f"drawtext=fontfile='{_FONT}':text='{t}':x={text_x}:y={title_y}:"
        f"fontsize={title_size}:fontcolor=black"
    )
    return ",".join(parts)


def create_lower_third(title: str, subtitle: str, output_dir: Path,
                       logo_path: Path | None = None) -> Path | None:
    """Gera um PNG do lower-third (fundo branco, logo à esquerda, título+subtítulo).

    Args:
        title: Título principal (linha grande, preta).
        subtitle: Subtítulo/chapéu (linha pequena verde acima do título).
        output_dir: Diretório para salvar o PNG.
        logo_path: Caminho opcional do PNG do logo (fica à esquerda).

    Returns:
        Caminho do PNG gerado, ou None se falhar.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    lt_path = output_dir / "lower_third.png"
    logo_w = 210 if (logo_path and logo_path.exists()) else 40

    chain = _lower_third_chain(title, subtitle, logo_w)

    command = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=white:s={CG_WIDTH}x{LT_HEIGHT}:d=1",
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
