"""Registro de cada chamada paga do Cortador, para a aba Custos do Estúdio.

Uma linha JSON por chamada em $ESTUDIO_DATA/custos.jsonl: quando, trabalho,
etapa, serviço, modelo, tokens (ou segundos de áudio) e o valor em US$. Vale o
custo devolvido pela API quando ela devolve (OpenRouter: usage.cost); senão é
estimado pelos preços abaixo, que a aba Configurações pode trocar
($ESTUDIO_DATA/config_cortador.json, seção "precos"). Sem ESTUDIO_DATA (linha
de comando no notebook) não registra nada, e um erro aqui nunca derruba a
chamada que estava sendo registrada.

O trabalho e a etapa vêm do ambiente (ESTUDIO_TRABALHO, ESTUDIO_ETAPA), que o
Estúdio põe antes de cada etapa — vale também para os subprocessos (Fase 1).
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

# US$ por token (DeepSeek, OpenRouter) ou por minuto de áudio (Whisper).
PRECOS_PADRAO = {
    "deepseek_entrada": 0.28e-6,        # deepseek-chat, entrada sem cache
    "deepseek_entrada_cache": 0.028e-6,  # entrada com cache
    "deepseek_saida": 0.42e-6,
    "jev_entrada": 4.431e-8,            # Decisions API (JEV), quando a resposta não traz o custo
    "openrouter_entrada": 0.3e-6,        # chat no OpenRouter sem usage.cost
    "openrouter_saida": 1.2e-6,
    "whisper_minuto": 0.0004,            # whisper-large-v3-turbo (DeepInfra/Groq pelo OpenRouter)
}

_trava = threading.Lock()


def _arquivo() -> Path | None:
    dados = os.environ.get("ESTUDIO_DATA")
    return Path(dados) / "custos.jsonl" if dados else None


def precos() -> dict:
    p = dict(PRECOS_PADRAO)
    dados = os.environ.get("ESTUDIO_DATA")
    if dados:
        try:
            extra = json.loads((Path(dados) / "config_cortador.json").read_text(encoding="utf-8")).get("precos") or {}
            p.update({k: float(v) for k, v in extra.items() if k in p})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return p


def _gravar(linha: dict) -> None:
    arq = _arquivo()
    if arq is None:
        return
    linha = {"em": time.strftime("%Y-%m-%dT%H:%M:%S"), "trabalho": os.environ.get("ESTUDIO_TRABALHO") or "",
             "etapa": os.environ.get("ESTUDIO_ETAPA") or "", **linha}
    texto = json.dumps(linha, ensure_ascii=False) + "\n"
    with _trava:
        with open(arq, "a", encoding="utf-8") as f:   # uma linha curta por write: sem misturar com outro processo
            f.write(texto)


def registrar_openrouter(caminho: str, payload: dict, resposta: dict) -> None:
    try:
        uso = (resposta.get("usage") if isinstance(resposta, dict) else None) or {}
        modelo = str(payload.get("model") or "")
        custo = uso.get("cost")
        real = isinstance(custo, (int, float))
        p = precos()
        if "transcriptions" in caminho:
            dados = ((payload.get("input_audio") or {}).get("data") or "")
            segundos = len(dados) * 3 / 4 / 32000          # WAV 16 kHz mono 16 bits
            if not real:
                custo = segundos / 60 * p["whisper_minuto"]
            _gravar({"servico": "openrouter", "tipo": "whisper", "modelo": modelo, "segundos": round(segundos, 1),
                     "valor_usd": float(custo), "real": real})
            return
        entrada = int(uso.get("prompt_tokens") or uso.get("input_tokens") or 0)
        saida = int(uso.get("completion_tokens") or uso.get("output_tokens") or 0)
        if "decisions" in caminho:
            tipo = "jev"
            if not entrada:
                entrada = len(json.dumps(payload, ensure_ascii=False)) // 4
            if not real:
                custo = entrada * p["jev_entrada"]
        else:
            tipo = "chat"
            if not real:
                custo = entrada * p["openrouter_entrada"] + saida * p["openrouter_saida"]
        _gravar({"servico": "openrouter", "tipo": tipo, "modelo": modelo, "tokens_entrada": entrada,
                 "tokens_saida": saida, "valor_usd": float(custo or 0), "real": real})
    except Exception:  # noqa: BLE001 — registrar nunca derruba a chamada
        pass


def registrar_deepseek(payload: dict, resposta: dict) -> None:
    try:
        uso = (resposta or {}).get("usage") or {}
        p = precos()
        cache = int(uso.get("prompt_cache_hit_tokens") or 0)
        entrada = int(uso.get("prompt_tokens") or 0)
        sem_cache = int(uso.get("prompt_cache_miss_tokens") or max(0, entrada - cache))
        saida = int(uso.get("completion_tokens") or 0)
        custo = sem_cache * p["deepseek_entrada"] + cache * p["deepseek_entrada_cache"] + saida * p["deepseek_saida"]
        _gravar({"servico": "deepseek", "tipo": "chat", "modelo": str(payload.get("model") or "deepseek-chat"),
                 "tokens_entrada": entrada, "tokens_cache": cache, "tokens_saida": saida,
                 "valor_usd": custo, "real": False})
    except Exception:  # noqa: BLE001
        pass
