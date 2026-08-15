"""Gerador de CG (Graphics) estilo Informativo Nacional para sobreposição em vídeos.

Design baseado nas cores da bandeira brasileira: verde, amarelo, azul e branco.
O CG é montado diretamente com filtros nativos do FFmpeg (drawbox/drawtext),
sem precisar de bibliotecas externas (Pillow) nem de suporte a SVG.
"""

from pathlib import Path
import subprocess

from utils import escape_drawtext

# Cores (identidade visual da bandeira brasileira)
VERDE = "#1B7E3E"
AMARELO = "#FDD835"
AZUL = "#002776"
BRANCO = "#FFFFFF"
FUNDO = "#141428"

CG_WIDTH = 1080
CG_HEIGHT = 240

_FONT = "C\\:/Windows/Fonts/arialbd.ttf"


def _cg_filter_chain(title: str) -> str:
    """Monta a cadeia de filtros que desenha o CG sobre uma base color."""
    t = escape_drawtext(title.upper().strip())
    return (
        # Barra verde superior
        f"drawbox=x=0:y=0:w={CG_WIDTH}:h=12:color={VERDE}@1:t=fill,"
        # Barra verde lateral esquerda
        f"drawbox=x=0:y=0:w=12:h={CG_HEIGHT}:color={VERDE}@1:t=fill,"
        # Barra azul inferior
        f"drawbox=x=0:y={CG_HEIGHT - 8}:w={CG_WIDTH}:h=8:color={AZUL}@1:t=fill,"
        # Divisor amarelo entre cabeçalho e título
        f"drawbox=x=40:y=118:w={CG_WIDTH - 80}:h=3:color={AMARELO}@1:t=fill,"
        # "INFORMATIVO" (branco)
        f"drawtext=fontfile='{_FONT}':text='INFORMATIVO':x=40:y=28:"
        f"fontsize=40:fontcolor={BRANCO}:borderw=1:bordercolor=black,"
        # "NACIONAL" (amarelo)
        f"drawtext=fontfile='{_FONT}':text='NACIONAL':x=40:y=72:"
        f"fontsize=40:fontcolor={AMARELO}:borderw=1:bordercolor=black,"
        # Título do trecho (branco)
        f"drawtext=fontfile='{_FONT}':text='{t}':x=40:y=150:"
        f"fontsize=44:fontcolor={BRANCO}:borderw=2:bordercolor=black"
    )


def create_cg_overlay(title: str, output_dir: Path, icon_path: Path | None = None) -> Path | None:
    """Gera um PNG do CG 'Informativo Nacional' com o título e o ícone opcional.

    Args:
        title: Título do trecho (será convertido para UPPERCASE)
        output_dir: Diretório para salvar o PNG
        icon_path: Caminho opcional do PNG do ícone (canto superior direito)

    Returns:
        Caminho do PNG gerado, ou None se falhar.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cg_path = output_dir / "cg_overlay.png"

    chain = _cg_filter_chain(title)

    command = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={FUNDO}:s={CG_WIDTH}x{CG_HEIGHT}:d=1",
    ]

    if icon_path and icon_path.exists():
        # Ícone entra como segunda entrada e é sobreposto à direita.
        command += ["-i", str(icon_path)]
        icon_h = CG_HEIGHT - 40
        filter_complex = (
            f"[0:v]{chain}[cg];"
            f"[1:v]scale=-1:{icon_h}[icon];"
            f"[cg][icon]overlay=x=W-w-30:y=(H-h)/2[out]"
        )
        command += [
            "-filter_complex", filter_complex, "-map", "[out]",
            "-frames:v", "1", str(cg_path),
        ]
    else:
        command += ["-vf", chain, "-frames:v", "1", str(cg_path)]

    try:
        result = subprocess.run(command, capture_output=True, timeout=15)
        if result.returncode == 0 and cg_path.exists():
            return cg_path
    except Exception:
        pass
    return None
