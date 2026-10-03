NOME = "cintilar_luz"
TIPO = "sobreposicao"
DESCRICAO = "Varia o brilho como lanterna ou vela. Para cenas noturnas."


def filtro(ctx) -> str:
    return "eq=brightness='0.045*sin(t*11)+0.03*sin(t*23.7)-0.02':eval=frame"
