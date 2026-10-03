NOME = "zoom_out"
TIPO = "movimento"
DESCRICAO = "Afasta devagar, do zoom máximo até 1,00."


def filtro(ctx) -> str:
    n = max(ctx["frames"] - 1, 1)
    zmax = 1.18 if ctx.get("tensao", 2) >= 4 else 1.10
    return (f"zoompan=z='{zmax:.3f}-{zmax - 1:.3f}*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={ctx['frames']}:s={ctx['w']}x{ctx['h']}:fps={ctx['fps']}")
