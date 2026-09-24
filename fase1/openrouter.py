"""Chamada HTTP ao OpenRouter, usada pelo JEV e pelo LLM.

Sem dependências externas (urllib), como o ai_srt.py do app. A chave vem de
OPENROUTER_API_KEY, lida do ambiente ou do arquivo .env desta pasta.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_URL = "https://openrouter.ai/api"
ENV_PATH = Path(__file__).resolve().parent / ".env"

log = logging.getLogger("fase1")


class APIError(RuntimeError):
    pass


def load_env(path: Path = ENV_PATH) -> None:
    """Carrega CHAVE=valor do .env sem sobrescrever o que já está no ambiente."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def api_key() -> str:
    load_env()
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key or key.startswith("sk-or-v1-COLOQUE"):
        raise APIError(f"OPENROUTER_API_KEY não definida (preencha {ENV_PATH}).")
    return key


def post(path: str, payload: dict, timeout: float, tentativas: int) -> dict:
    """POST com novas tentativas em erro de rede, 429 e 5xx. Erro 4xx falha na hora."""
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "X-Title": "Corta+Legenda Fase 1",
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
