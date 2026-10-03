NOME = "pan"
TIPO = "movimento"
DESCRICAO = "Desliza a câmera de um lado ao outro da imagem (direção alterna por cena). Rende mais no formato longo."


def filtro(ctx) -> str:
    n = max(ctx["frames"] - 1, 1)
    z = 1.15 if ctx["w"] < ctx["h"] else 1.12
    progresso = f"on/{n}" if ctx.get("n", 1) % 2 else f"(1-on/{n})"
    return (f"zoompan=z='{z}':x='(iw-iw/zoom)*{progresso}':y='ih/2-(ih/zoom/2)'"
            f":d={ctx['frames']}:s={ctx['w']}x{ctx['h']}:fps={ctx['fps']}")
