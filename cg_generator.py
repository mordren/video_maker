"""Gerador de CG (Graphics) estilo Informativo Nacional para sobreposição em vídeos.

Design baseado nas cores da bandeira brasileira: verde, amarelo, azul e branco.
"""

from pathlib import Path
import subprocess
import tempfile


def generate_cg_svg(title: str, width: int = 1080, height: int = 240) -> str:
    """Gera SVG com design de jornal 'Informativo Nacional'.

    Args:
        title: Título do trecho (ex: "ZEMA JÁ PERDEU")
        width: Largura da imagem (padrão 1080px)
        height: Altura da imagem (padrão 240px - para ficar no rodapé do vídeo 9:16)

    Returns:
        String com SVG renderizado
    """
    # Cores baseadas na identidade visual (bandeira brasileira)
    verde = "#1B7E3E"      # Verde da bandeira
    amarelo = "#FDD835"    # Amarelo da bandeira
    azul = "#002776"       # Azul da bandeira
    branco = "#FFFFFF"     # Branco
    cinza_escuro = "#1a1a2a"

    # Altura das secções
    altura_barra_topo = 12
    altura_titulo = 80
    altura_infos = 90

    svg = f"""<?xml version="1.0" encoding="UTF-8"?>
<svg width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">
  <!-- Fundo principal (azul escuro) -->
  <rect width="{width}" height="{height}" fill="{cinza_escuro}"/>

  <!-- Barra verde superior (identidade visual) -->
  <rect width="{width}" height="{altura_barra_topo}" fill="{verde}"/>

  <!-- Barra verde na lateral esquerda -->
  <rect width="12" height="{height}" fill="{verde}"/>

  <!-- Barra amarela como divisor -->
  <rect y="{altura_barra_topo + 90}" width="{width}" height="4" fill="{amarelo}"/>

  <!-- Seção do nome do jornal -->
  <!-- "INFORMATIVO" em branco grande -->
  <text x="40" y="55" font-family="Arial, sans-serif" font-size="36" font-weight="900"
        fill="{branco}" letter-spacing="3">INFORMATIVO</text>

  <!-- "NACIONAL" em amarelo -->
  <text x="40" y="95" font-family="Arial, sans-serif" font-size="36" font-weight="900"
        fill="{amarelo}" letter-spacing="3">NACIONAL</text>

  <!-- Separador visual -->
  <line x1="40" y1="110" x2="{width - 40}" y2="110" stroke="{amarelo}" stroke-width="2"/>

  <!-- Título do trecho (conteúdo principal) -->
  <text x="40" y="165" font-family="Arial, sans-serif" font-size="34" font-weight="bold"
        fill="{branco}">{title}</text>

  <!-- Barra inferior azul (identidade visual) -->
  <rect y="{height - 8}" width="{width}" height="8" fill="{azul}"/>

  <!-- Pequeno badge/símbolo no canto superior direito (decorativo) -->
  <rect x="{width - 80}" y="20" width="60" height="60" fill="none" stroke="{amarelo}" stroke-width="2"/>
</svg>"""

    return svg


def svg_to_png(svg_content: str, output_path: Path) -> bool:
    """Converte SVG para PNG usando FFmpeg.

    Args:
        svg_content: Conteúdo SVG como string
        output_path: Caminho para salvar PNG

    Returns:
        True se sucesso, False caso contrário
    """
    with tempfile.NamedTemporaryFile(mode='w', suffix='.svg', delete=False, encoding='utf-8') as f:
        f.write(svg_content)
        svg_path = Path(f.name)

    try:
        # Usa FFmpeg para converter SVG para PNG
        cmd = [
            "ffmpeg", "-y",
            "-i", str(svg_path),
            "-vf", "scale=1080:240",
            str(output_path)
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False
    finally:
        svg_path.unlink(missing_ok=True)


def create_cg_overlay(title: str, output_dir: Path) -> Path | None:
    """Cria arquivo PNG com CG do Informativo Nacional.

    Args:
        title: Título do trecho (será convertido para UPPERCASE)
        output_dir: Diretório para salvar PNG

    Returns:
        Caminho do PNG ou None se falhar
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    cg_path = output_dir / "cg_overlay.png"

    # Garante que o título está em UPPERCASE
    title = title.upper().strip()

    svg = generate_cg_svg(title)
    if svg_to_png(svg, cg_path):
        return cg_path

    return None
