NOME = "tremor"
TIPO = "movimento"
DESCRICAO = "Tremida curta da imagem com leve aproximação. Para o susto."


def filtro(ctx) -> str:
    n = max(ctx["frames"] - 1, 1)
    a = 10 * (ctx["w"] / 1080)  # amplitude em pixels da imagem ampliada
    return (f"zoompan=z='1.10+0.06*on/{n}'"
            f":x='iw/2-(iw/zoom/2)+{a:.1f}*sin(on*1.9)*cos(on*0.7)'"
            f":y='ih/2-(ih/zoom/2)+{a:.1f}*cos(on*2.3)*sin(on*0.5)'"
            f":d={ctx['frames']}:s={ctx['w']}x{ctx['h']}:fps={ctx['fps']}")
