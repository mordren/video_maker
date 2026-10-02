"""Registro de cada gasto e aplicação dos tetos."""
from . import db


class OrcamentoEstourado(Exception):
    pass


def registrar(projeto_id, etapa: str, servico: str, modelo: str, valor_usd: float, real: bool, detalhe: str = ""):
    db.executar(
        "INSERT INTO custos (projeto_id, etapa, servico, modelo, valor_usd, real, detalhe, criado_em) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (projeto_id, etapa, servico, modelo, float(valor_usd or 0), 1 if real else 0, detalhe, db.agora()),
    )
    from . import registro
    registro.escrever(projeto_id, "CUSTO", f"{etapa} · {servico} · {modelo} · US$ {float(valor_usd or 0):.6f} "
                                           f"({'real' if real else 'estimado'}) · {detalhe}")


def total(projeto_id, etapa: str | None = None) -> float:
    if etapa:
        r = db.um("SELECT COALESCE(SUM(valor_usd), 0) AS t FROM custos WHERE projeto_id = ? AND etapa = ?",
                  (projeto_id, etapa))
    else:
        r = db.um("SELECT COALESCE(SUM(valor_usd), 0) AS t FROM custos WHERE projeto_id = ?", (projeto_id,))
    return float(r["t"])


def verificar(projeto_id, etapa: str | None, estimativa: float, teto: float, rotulo: str, base: float = 0.0):
    """base: quanto já tinha sido gasto antes desta execução (não conta contra o teto dela)."""
    gasto = total(projeto_id, etapa) - base
    if gasto + estimativa > teto + 1e-9:
        raise OrcamentoEstourado(
            f"{rotulo}: gasto de US$ {gasto:.4f} + próxima chamada (~{estimativa:.4f}) passa do teto de US$ {teto:.4f}")


def resumo(projeto_id) -> dict:
    linhas = db.todos("SELECT etapa, servico, modelo, SUM(valor_usd) AS valor, COUNT(*) AS chamadas, "
                      "MIN(real) AS todos_reais FROM custos WHERE projeto_id = ? GROUP BY etapa, servico, modelo "
                      "ORDER BY MIN(id)", (projeto_id,))
    return {"total": total(projeto_id), "linhas": linhas}
