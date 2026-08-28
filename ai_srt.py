"""Revisão das legendas com IA (API do DeepSeek).

O Whisper erra bastante em português — acentuação, pontuação, concordância e,
principalmente, nomes próprios. Este módulo manda **só as falas** para o modelo
e recolhe as falas corrigidas; os tempos nunca saem daqui. Quem remonta o SRT é
`utils.review_srt_with_ai`, sempre a partir dos tempos originais, de modo que
uma resposta torta do modelo possa estragar o texto, mas nunca dessincronizar a
legenda do vídeo.

Sem dependências externas (usa urllib) e sem Qt, para poder ser testado solto.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

API_BASE = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

# Quantos blocos de legenda vão por requisição. Blocos demais numa tacada só
# aumentam a chance de o modelo devolver uma quantidade diferente de linhas.
CHUNK = 40

_PROMPT = """Você revisa legendas em português do Brasil geradas por reconhecimento \
de fala (Whisper). Corrija ortografia, acentuação, pontuação, concordância e nomes \
próprios mal transcritos.

Regras:
- Devolva exatamente a mesma quantidade de linhas que recebeu, na mesma ordem.
- Nunca junte nem separe linhas: cada linha é um bloco de legenda com tempo próprio.
- Não traduza, não resuma e não reescreva o estilo. Corrija só o que está errado.
- Mantenha o jeito falado (gírias, repetições, frases cortadas no meio). Você \
corrige a grafia, não a fala.
- Se a linha já estiver certa, devolva ela igual.
- Responda apenas com JSON no formato {"linhas": ["...", "..."]}"""

_REVIEW_TITLE_PROMPT = """Você trabalha a legenda de um vídeo político curto do Brasil \
— em geral a fala ou a opinião de um político sobre algum tema. Faça as duas coisas \
abaixo e devolva TUDO num único JSON.

1) Revise a legenda: corrija ortografia, acentuação, pontuação, concordância e nomes \
próprios mal transcritos pelo reconhecimento de fala. Mantenha o jeito falado (gírias, \
repetições, frases cortadas no meio). Não traduza, não resuma, não reescreva o estilo.
2) Crie um título e um subtítulo para o vídeo, com base só no que é dito na fala.

Responda apenas com JSON, exatamente neste formato:
{"titulo": "...", "subtitulo": "...", "linhas": ["...", "..."]}

- titulo: chamativo mas fiel à fala; no máximo ~60 caracteres; sem ponto final.
- subtitulo: um chapéu curto de tema/categoria; no máximo ~30 caracteres; em CAIXA ALTA \
(ex.: ECONOMIA, ELEIÇÕES 2026, STF, SEGURANÇA).
- linhas: exatamente a mesma quantidade de linhas que você recebeu, na mesma ordem; \
nunca junte nem separe linhas; se uma já estiver certa, devolva-a igual.
- Não invente fatos que não estão na fala."""


class AiError(RuntimeError):
    """Falha ao falar com a API (chave, rede, cota, modelo inexistente...)."""


# ──────────────────────────────────────────────────────────────
#  Configuração (chave da API e modelo)
# ──────────────────────────────────────────────────────────────

def config_path() -> Path:
    """Arquivo de configuração, fora da pasta do projeto.

    A pasta do projeto fica no OneDrive; guardar a chave da API ali a mandaria
    para a nuvem junto. Por isso vai no diretório local do usuário.
    """
    base = os.getenv("LOCALAPPDATA") or os.getenv("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "CortaLegenda" / "config.json"


def load_config() -> dict:
    """Lê a configuração salva; devolve dict vazio se não houver nenhuma."""
    path = config_path()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    # Antes isto usava a API da xAI. Chave e modelo de lá não servem aqui e só
    # dariam um erro confuso na primeira chamada, então saem de campo — mas a
    # chave antiga fica guardada de lado, para não sumir sem o usuário mandar.
    if str(config.get("api_key", "")).startswith("xai-"):
        config["api_key_xai"] = config["api_key"]
        config["api_key"] = ""
    # Volta ao padrão (deepseek-chat) quem tiver ficado com um modelo antigo da
    # xAI ou com um variante que "raciocina" (flash/reasoner) — esses geram muito
    # token à toa nesta tarefa e foram o que estourou a conta.
    model = str(config.get("model", "")).lower()
    if model.startswith("grok") or "flash" in model or "reasoner" in model:
        config["model"] = ""
    return config


def save_config(config: dict) -> bool:
    """Grava a configuração, preservando o que já estava no arquivo.

    A interface só conhece alguns campos; o resto do arquivo é mantido como
    está para não apagar nada que ela não saiba escrever de volta.
    """
    path = config_path()
    merged = load_config() | config
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(merged, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except OSError:
        return False


def api_key_from_env() -> str:
    """Chave da variável de ambiente, para quem prefere não salvar em arquivo."""
    return (os.getenv("DEEPSEEK_API_KEY") or "").strip()


# ──────────────────────────────────────────────────────────────
#  Chamadas à API
# ──────────────────────────────────────────────────────────────

def _request(path: str, api_key: str, payload: dict | None = None,
             timeout: int = 180) -> dict:
    """POST (ou GET, se payload for None) em JSON, com o erro da API repassado."""
    request = urllib.request.Request(
        API_BASE + path,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {api_key}"},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:400]
        try:  # a API devolve {"error": {"message": ...}} ou {"error": "..."}
            parsed = json.loads(detail).get("error")
            detail = parsed.get("message", detail) if isinstance(parsed, dict) else str(parsed)
        except ValueError:
            pass
        raise AiError(f"HTTP {error.code}: {detail}") from error
    except urllib.error.URLError as error:
        raise AiError(f"Sem conexão com a API: {error.reason}") from error
    except (TimeoutError, OSError) as error:
        raise AiError(f"Falha de rede: {error}") from error


def list_models(api_key: str) -> list[str]:
    """Modelos disponíveis para essa chave, para preencher a lista na interface."""
    if not api_key:
        raise AiError("Informe a chave da API.")
    data = _request("/models", api_key, timeout=30).get("data") or []
    return sorted(str(m.get("id")) for m in data if m.get("id"))


def _unwrap_json(content: str) -> dict:
    """Lê o JSON da resposta, tolerando cerca de ```json e texto em volta."""
    text = content.strip()
    if text.startswith("```"):
        text = text.split("```")[1] if "```" in text[3:] else text[3:]
        text = text.removeprefix("json").strip()
    try:
        return json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                pass
    raise AiError("A IA respondeu num formato inesperado.")


def _correct_chunk(lines: list[str], api_key: str, model: str,
                   context: str) -> tuple[list[str], dict]:
    """Corrige um lote de linhas. Devolve (linhas, uso de tokens).

    Devolve as originais se a resposta não bater. `uso` é o bloco `usage` da
    resposta (prompt/completion tokens), para a interface poder mostrar quanto
    custou de verdade.
    """
    prompt = _PROMPT
    if context:
        prompt += f"\n\nContexto do vídeo (ajuda com nomes próprios): {context}"
    user = json.dumps({"linhas": lines}, ensure_ascii=False)
    # Teto de saída: a resposta é do tamanho da entrada (mesmas linhas,
    # corrigidas). Sem este limite, o modelo pode disparar milhares de tokens
    # à toa — foi o que estourou a conta. Dou folga generosa (para não cortar
    # uma resposta legítima, o que viraria erro de JSON) e um teto rígido.
    max_tokens = min(4096, max(512, len(user) // 2 + 512))
    payload = {
        "model": model,
        "temperature": 0.2,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
    }
    data = _request("/chat/completions", api_key, payload)
    usage = data.get("usage") or {}
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as error:
        raise AiError("Resposta da API sem conteúdo.") from error

    fixed = _unwrap_json(content).get("linhas")
    # Só aceita se vier a mesma quantidade de linhas: uma resposta com mais ou
    # menos blocos desalinharia a legenda do áudio. Na dúvida, fica o original.
    if not isinstance(fixed, list) or len(fixed) != len(lines):
        return lines, usage
    return [str(new).strip() or old for new, old in zip(fixed, lines)], usage


def correct_lines(lines: list[str], api_key: str, model: str = DEFAULT_MODEL,
                  context: str = "", progress=None) -> tuple[list[str], dict]:
    """Corrige as falas em lotes, preservando a quantidade e a ordem.

    Devolve (linhas corrigidas, uso somado de tokens). `progress` é chamado com
    (linhas_prontas, total) a cada lote, para a interface mostrar andamento.
    """
    if not api_key:
        raise AiError("Informe a chave da API do DeepSeek.")
    if not lines:
        return [], {}
    result: list[str] = []
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for start in range(0, len(lines), CHUNK):
        batch = lines[start:start + CHUNK]
        fixed, usage = _correct_chunk(batch, api_key, model or DEFAULT_MODEL, context)
        result.extend(fixed)
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
        if progress:
            progress(len(result), len(lines))
    return result, totals


def review_lines_with_title(lines: list[str], api_key: str, model: str = DEFAULT_MODEL,
                            context: str = "") -> tuple[str, str, list[str], dict]:
    """Numa só requisição: corrige as falas e cria título e subtítulo.

    Devolve (título, subtítulo, linhas corrigidas, uso de tokens). A resposta é
    um único JSON com as três chaves. Se a chave `linhas` não vier com a mesma
    quantidade, ficam as originais (o título/subtítulo ainda são aproveitados).
    """
    if not api_key:
        raise AiError("Informe a chave da API do DeepSeek.")
    if not lines:
        return "", "", [], {}
    user = json.dumps({"linhas": lines}, ensure_ascii=False)
    # Saída ≈ entrada (mesmas linhas) + um punhado para título/subtítulo.
    max_tokens = min(4096, max(512, len(user) // 2 + 512))
    prompt = _REVIEW_TITLE_PROMPT
    if context:
        prompt += f"\n\nDica de contexto (tema/pessoa): {context}"
    payload = {
        "model": model or DEFAULT_MODEL,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user},
        ],
    }
    data = _request("/chat/completions", api_key, payload)
    usage = data.get("usage") or {}
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as error:
        raise AiError("Resposta da API sem conteúdo.") from error
    obj = _unwrap_json(content)
    titulo = str(obj.get("titulo") or obj.get("title") or "").strip()
    subtitulo = str(obj.get("subtitulo") or obj.get("subtitle") or "").strip()
    fixed = obj.get("linhas")
    if not isinstance(fixed, list) or len(fixed) != len(lines):
        fixed = lines
    else:
        fixed = [str(new).strip() or old for new, old in zip(fixed, lines)]
    return titulo, subtitulo, fixed, usage
