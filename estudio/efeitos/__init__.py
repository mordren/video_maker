"""Biblioteca de efeitos. Cada efeito é um arquivo desta pasta com:

    NOME = "zoom_in"
    TIPO = "movimento"      # movimento: define a câmera (zoompan); sobreposicao: filtro por cima
    DESCRICAO = "..."
    def filtro(ctx) -> str  # trecho de filtro do FFmpeg

ctx traz: w, h (saída), fps, frames, dur, tensao, n (número da cena).
O filtro de movimento recebe a imagem já ampliada para 2w x 2h (o zoom não treme).
Criar um efeito novo = criar um arquivo novo aqui; ele aparece sozinho na lista.
"""
import importlib
import pkgutil

_registro: dict = {}


def registro() -> dict:
    if not _registro:
        for m in pkgutil.iter_modules(__path__):
            mod = importlib.import_module(f"{__name__}.{m.name}")
            if hasattr(mod, "NOME") and hasattr(mod, "filtro"):
                _registro[mod.NOME] = mod
    return _registro


def listar() -> list[dict]:
    return [{"nome": k, "tipo": m.TIPO, "descricao": m.DESCRICAO} for k, m in sorted(registro().items())]


def movimento(nome: str, ctx: dict) -> str:
    mod = registro().get(nome)
    if not mod or mod.TIPO != "movimento":
        mod = registro()["zoom_in"]
    return mod.filtro(ctx)


def sobreposicao(nome: str | None, ctx: dict) -> str | None:
    mod = registro().get(nome or "")
    if not mod or mod.TIPO != "sobreposicao":
        return None
    return mod.filtro(ctx)


def atribuir(h: dict, canal: dict, formato: dict):
    """Escolhe movimento e sobreposição de cada cena pela regra do canal. Não mexe no que já foi escolhido à mão."""
    ef = canal["config"].get("efeitos") or {}
    permitidos = set(ef.get("permitidos") or registro().keys())
    por_tensao = ef.get("por_tensao") or {}
    extras = ef.get("extras_por_tensao") or {}
    anterior = None
    por_n = {c["n"]: c for c in h.get("cenas", [])}
    for c in h.get("cenas", []):
        t = str(c.get("tensao", 2))
        origem = por_n.get(c.get("mesma_imagem_de"))
        if origem and not c.get("efeito"):
            # Mesma imagem da cena anterior: o movimento tem de ser outro, para parecer outro plano.
            # (vale mesmo que o canal não liste o "detalhe" entre os permitidos: é enquadramento, não enfeite)
            c["efeito"] = "detalhe" if origem.get("efeito") != "detalhe" else "pan"
        if not c.get("efeito"):
            mov = por_tensao.get(t, formato.get("movimento_padrao", "zoom_in"))
            if mov not in permitidos or mov not in registro():
                mov = formato.get("movimento_padrao", "zoom_in")
            # O zoom alterna entre aproximar e afastar nas cenas calmas.
            if mov == anterior and mov in ("zoom_in", "zoom_out") and int(t) < 4:
                mov = "zoom_out" if mov == "zoom_in" else "zoom_in"
            c["efeito"] = mov
        if "extra" not in c:
            extra = extras.get(t)
            c["extra"] = extra if extra in permitidos and extra in registro() else ""
        anterior = c["efeito"]
    h["transicao"] = h.get("transicao") or ef.get("transicao", "escurecer")
    return h
