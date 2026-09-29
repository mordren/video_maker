NOME = "detalhe"
TIPO = "movimento"
DESCRICAO = ("Plano fechado num ponto fora do centro, aproximando devagar. Faz a mesma imagem parecer outro plano; "
             "é o que o programa usa quando duas cenas seguidas mostram a mesma coisa.")

# Pontos de foco (fração da largura e da altura); a cena escolhe um pelo número, para não repetir o vizinho.
FOCOS = [(0.35, 0.38), (0.65, 0.40), (0.40, 0.62), (0.62, 0.60)]


def filtro(ctx) -> str:
    n = max(ctx["frames"] - 1, 1)
    fx, fy = FOCOS[ctx.get("n", 1) % len(FOCOS)]
    return (f"zoompan=z='1.35+0.15*on/{n}'"
            f":x='max(0,min(iw-iw/zoom,iw*{fx}-iw/zoom/2))'"
            f":y='max(0,min(ih-ih/zoom,ih*{fy}-ih/zoom/2))'"
            f":d={ctx['frames']}:s={ctx['w']}x{ctx['h']}:fps={ctx['fps']}")
