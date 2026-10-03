NOME = "flash"
TIPO = "sobreposicao"
DESCRICAO = "Dois ou três quadros claros no começo da cena, seguidos de um instante escuro. Para revelação."


def filtro(ctx) -> str:
    return "eq=brightness='if(lt(t,0.1),0.5,if(lt(t,0.2),-0.25,0))':eval=frame"
