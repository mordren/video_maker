NOME = "zoom_in"
TIPO = "movimento"
DESCRICAO = "Aproxima devagar (1,00 a 1,10; até 1,18 nas cenas de tensão 4 e 5)."


def zoom_max(ctx) -> float:
    return 1.18 if ctx.get("tensao", 2) >= 4 else 1.10


def filtro(ctx) -> str:
    n = max(ctx["frames"] - 1, 1)
    z = zoom_max(ctx) - 1
    return (f"zoompan=z='1+{z:.3f}*on/{n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={ctx['frames']}:s={ctx['w']}x{ctx['h']}:fps={ctx['fps']}")
