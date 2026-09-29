NOME = "vinheta_pulso"
TIPO = "sobreposicao"
DESCRICAO = "Bordas escurecem e clareiam no ritmo. Para tensão crescente."


def filtro(ctx) -> str:
    return "vignette=angle='PI/4+0.12*sin(t*2.6)':eval=frame"
