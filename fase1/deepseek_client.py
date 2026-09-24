r"""Cliente DeepSeek direto (não OpenRouter), reutilizando a chave do app.

A chave vem de DEEPSEEK_API_KEY (env) ou do arquivo de config do app:
  Windows: ~\AppData\Local\CortaLegenda\config.json
  Linux/Mac: ~/.config/CortaLegenda/config.json
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://api.deepseek.com/v1"

log = logging.getLogger("fase1")


class APIError(RuntimeError):
    pass


def _config_path() -> Path:
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "CortaLegenda" / "config.json"


def api_key() -> str:
    """Lê a chave do DeepSeek do env ou do arquivo de config do app."""
    chave = (os.getenv("DEEPSEEK_API_KEY") or "").strip()
    if chave:
        return chave
    try:
        cfg = json.loads(_config_path().read_text(encoding="utf-8"))
        chave = cfg.get("api_key", "").strip()
    except (OSError, ValueError):
        chave = ""
    if not chave or chave.startswith("sk-"):  # sk- é formato de outras APIs
        raise APIError(
            f"Chave do DeepSeek não encontrada.\n"
            f"  1. Defina DEEPSEEK_API_KEY no ambiente, ou\n"
            f"  2. Rode o Corta+Legenda e configure no painel 'Legenda com IA'")
    return chave


def post(path: str, payload: dict, timeout: float, tentativas: int) -> dict:
    """POST com novas tentativas em erro de rede, 429 e 5xx. Erro 4xx falha na hora."""
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
    }
    ultimo = ""
    for tentativa in range(1, tentativas + 1):
        req = urllib.request.Request(BASE_URL + path, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detalhe = exc.read().decode("utf-8", errors="replace")[:500]
            ultimo = f"HTTP {exc.code}: {detalhe}"
            if exc.code != 429 and exc.code < 500:
                raise APIError(ultimo) from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            ultimo = f"{type(exc).__name__}: {exc}"
        if tentativa < tentativas:
            espera = 2 ** tentativa
            log.warning("  %s falhou (%s); nova tentativa em %ss", path, ultimo, espera)
            time.sleep(espera)
    raise APIError(ultimo)
