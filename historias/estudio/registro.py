"""Arquivos de log.

- dados/logs/estudio.log: tudo, de todos os vídeos, em ordem (troca de arquivo a cada 5 MB, guarda 10).
- projetos/<projeto>/registro.log: só daquele vídeo.

Cada linha: data | nível | #projeto | tipo | mensagem. Os tipos ajudam a filtrar:
EVENTO (o registro da tela), API (cada chamada paga, com tokens, custo e tempo), JEV (as notas de cada versão),
CUSTO, ERRO (com o traceback completo).
"""
import json
import logging
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import config

PASTA_LOGS = config.DADOS / "logs"
PASTA_LOGS.mkdir(parents=True, exist_ok=True)
ARQUIVO = PASTA_LOGS / "estudio.log"

_logger = logging.getLogger("estudio")
if not _logger.handlers:
    _logger.setLevel(logging.INFO)
    _h = RotatingFileHandler(ARQUIVO, maxBytes=5_000_000, backupCount=10, encoding="utf-8")
    _h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    _logger.addHandler(_h)
    _logger.propagate = False

_lock = threading.Lock()
_pastas: dict[int, Path] = {}
_NIVEIS = {"info": logging.INFO, "aviso": logging.WARNING, "erro": logging.ERROR}


def _pasta_projeto(projeto_id) -> Path | None:
    if not projeto_id:
        return None
    if projeto_id not in _pastas:
        from . import db
        p = db.um("SELECT pasta FROM projetos WHERE id = ?", (projeto_id,))
        if not p or not p["pasta"]:
            return None
        _pastas[projeto_id] = config.BASE / p["pasta"]
    return _pastas[projeto_id]


def escrever(projeto_id, tipo: str, msg: str, nivel: str = "info", dados: dict | None = None):
    """Uma linha no log geral e no log do projeto. 'dados' vai em JSON no fim da linha."""
    linha = f"#{projeto_id or '-'} | {tipo:<6} | {msg}"
    if dados:
        linha += " | " + json.dumps(dados, ensure_ascii=False, default=str)
    lvl = _NIVEIS.get(nivel, logging.INFO)
    _logger.log(lvl, linha)
    pasta = _pasta_projeto(projeto_id)
    if pasta and pasta.exists():
        from datetime import datetime
        with _lock, open(pasta / "registro.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} | {logging.getLevelName(lvl):<5} | {linha}\n")


def esquecer(projeto_id):
    _pastas.pop(projeto_id, None)
