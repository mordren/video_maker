"""Chamadas externas: texto (OpenRouter), juiz Jev (OpenRouter Decisions) e imagem (Pollinations ou ComfyUI).
Toda chamada registra o custo em custos.py. Em modo simulação nada é pago (ver simulacao.py)."""
import json
import re
import time
from pathlib import Path
from urllib.parse import quote

import httpx

from . import comfy, config, custos, registro, simulacao


class ErroAPI(Exception):
    pass


class ErroModeracao(ErroAPI):
    """O provedor recusou o prompt, a referência ou a imagem pela política de conteúdo (HTTP 422)."""


def _cabecalhos_openrouter():
    if not config.OPENROUTER_API_KEY:
        raise ErroAPI("OPENROUTER_API_KEY não configurada no arquivo .env")
    return {
        "Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "http://localhost:8000",
        "X-Title": "Estudio de Historias",
    }


def _cabecalhos_deepseek():
    if not config.DEEPSEEK_API_KEY:
        raise ErroAPI("DEEPSEEK_API_KEY não configurada (Configurações > Chaves e acesso, ou no arquivo .env)")
    return {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}", "Content-Type": "application/json"}


def _cabecalhos_polli():
    if not config.POLLINATIONS_API_KEY:
        raise ErroAPI("POLLINATIONS_API_KEY não configurada no arquivo .env")
    return {"Authorization": f"Bearer {config.POLLINATIONS_API_KEY}"}


def _post(url, corpo, cabecalhos, timeout=None, tentativas=None):
    timeout = timeout or config.API_TIMEOUT_S
    tentativas = tentativas or config.API_TENTATIVAS
    ultimo = None
    for i in range(tentativas):
        try:
            r = httpx.post(url, json=corpo, headers=cabecalhos, timeout=timeout)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            ultimo = e
            time.sleep(2 * (i + 1))
            continue
        if r.status_code >= 500 or r.status_code == 429:
            ultimo = ErroAPI(f"HTTP {r.status_code}: {r.text[:300]}")
            time.sleep(3 * (i + 1))
            continue
        return r
    raise ErroAPI(f"Falha de rede em {url}: {ultimo}")


def extrair_json(txt: str):
    txt = txt.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", txt, re.S)
    if m:
        txt = m.group(1).strip()
    try:
        return json.loads(txt)
    except ValueError:
        ini = min([i for i in (txt.find("{"), txt.find("[")) if i >= 0], default=-1)
        fim = max(txt.rfind("}"), txt.rfind("]"))
        if ini >= 0 and fim > ini:
            return json.loads(txt[ini:fim + 1])
        raise


# ---------------------------------------------------------------- texto

def chat(tarefa: str, mensagens: list[dict], schema: dict | None = None, *, projeto_id=None, etapa="texto",
         temperatura: float = 0.8, max_tokens: int = 6000, contexto: dict | None = None):
    """Devolve (resposta, custo_usd). Com schema, a resposta já vem como dict."""
    if config.SIMULACAO:
        resp = simulacao.chat(tarefa, mensagens, contexto or {})
        custos.registrar(projeto_id, etapa, "simulacao", config.modelo_texto(), 0.0, True, tarefa)
        return resp, 0.0

    pensa = tarefa in config.RACIOCINIO_TAREFAS or "todas" in config.RACIOCINIO_TAREFAS
    oficial = config.PROVEDOR_TEXTO == "deepseek"
    # Os tokens de raciocínio saem do mesmo limite da resposta: sem folga a história viria cortada.
    limite = max_tokens + 16000 if pensa else max_tokens
    if oficial:
        # A API oficial só aceita json_object (sem schema): o schema vai no pedido. O deepseek-reasoner ignora temperature.
        msgs = list(mensagens)
        if schema:
            msgs.append({"role": "system", "content": "Responda só com um objeto JSON válido neste schema: "
                                                       + json.dumps(schema, ensure_ascii=False)})
        corpo = {"model": "deepseek-reasoner" if pensa else "deepseek-chat", "messages": msgs,
                 "max_tokens": min(limite, 60000 if pensa else 8000)}
        if not pensa:
            corpo["temperature"] = temperatura
        if schema:
            corpo["response_format"] = {"type": "json_object"}
        url, cabecalhos = config.DEEPSEEK_URL, _cabecalhos_deepseek()
    else:
        corpo = {
            "model": config.MODELO_TEXTO,
            "messages": mensagens,
            "temperature": temperatura,
            "max_tokens": limite,
            "usage": {"include": True},
            "reasoning": {"enabled": pensa},
        }
        if schema:
            corpo["response_format"] = {"type": "json_schema",
                                        "json_schema": {"name": tarefa, "strict": True, "schema": schema}}
        url, cabecalhos = config.OPENROUTER_URL, _cabecalhos_openrouter()
    inicio = time.time()
    timeout = config.API_TIMEOUT_S * 3 if pensa else None
    r = _post(url, corpo, cabecalhos, timeout=timeout)
    if r.status_code == 400:
        # Alguns provedores recusam json_schema estrito ou o desligamento do raciocínio: tenta o modo simples.
        corpo.pop("reasoning", None)
        if schema:
            corpo["response_format"] = {"type": "json_object"}
        r = _post(url, corpo, cabecalhos, timeout=timeout)
    nome_api = "DeepSeek" if oficial else "OpenRouter"
    if r.status_code != 200:
        raise ErroAPI(f"{nome_api} HTTP {r.status_code}: {r.text[:500]}")
    dados = r.json()
    if "error" in dados:
        raise ErroAPI(f"{nome_api}: {dados['error']}")
    uso = dados.get("usage") or {}
    if oficial:
        # A API oficial não devolve o custo: calcula pelo preço por token (cache e não cache separados).
        acertos = uso.get("prompt_cache_hit_tokens") or 0
        custo = ((uso.get("prompt_tokens", 0) - acertos) * config.PRECO_DEEPSEEK_ENTRADA
                 + acertos * config.PRECO_DEEPSEEK_ENTRADA_CACHE
                 + uso.get("completion_tokens", 0) * config.PRECO_DEEPSEEK_SAIDA)
        real, em_cache = True, acertos
    else:
        custo = uso.get("cost")
        real = custo is not None
        if custo is None:
            custo = (uso.get("prompt_tokens", 0) * config.PRECO_TEXTO_ENTRADA
                     + uso.get("completion_tokens", 0) * config.PRECO_TEXTO_SAIDA)
        em_cache = (uso.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
    raciocinio = (uso.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
    custos.registrar(projeto_id, etapa, "deepseek" if oficial else "openrouter", config.modelo_texto(), custo, real,
                     f"{tarefa} · {uso.get('prompt_tokens', '?')}+{uso.get('completion_tokens', '?')} tokens"
                     f"{f' ({raciocinio} de raciocínio)' if raciocinio else ''}"
                     f"{f' ({em_cache} do cache)' if em_cache else ''}")
    conteudo = dados["choices"][0]["message"].get("content") or ""
    # Guarda a parte variável do pedido (mensagem do usuário) e a resposta, para ver depois o que foi pedido e o que veio.
    registro.escrever(projeto_id, "API", f"texto · {tarefa} · {time.time() - inicio:.1f}s · "
                                         f"fim: {dados['choices'][0].get('finish_reason')}",
                      dados={"pedido": str(mensagens[-1].get("content", ""))[:3000], "resposta": conteudo[:6000]})
    if schema:
        try:
            return extrair_json(conteudo), custo
        except ValueError as e:
            raise ErroAPI(f"Resposta não é JSON válido ({e}): {conteudo[:300]}")
    return conteudo.strip(), custo


# ---------------------------------------------------------------- juiz (Jev)

def decidir(state, perguntas: dict, *, projeto_id=None, etapa="avaliacao", contexto: dict | None = None):
    """Chama o Jev (typesafe/jev-1.13) pela Decisions API do OpenRouter. Devolve (answers, custo)."""
    if config.SIMULACAO:
        resp = simulacao.decidir(state, perguntas, contexto or {})
        custos.registrar(projeto_id, etapa, "simulacao", config.MODELO_JUIZ, 0.0, True, f"{len(perguntas)} perguntas")
        return resp, 0.0

    corpo = {"model": config.MODELO_JUIZ, "state": state, "questions": perguntas}
    inicio = time.time()
    r = _post(config.OPENROUTER_DECISOES_URL, corpo, _cabecalhos_openrouter())
    if r.status_code != 200:
        raise ErroAPI(f"Jev HTTP {r.status_code}: {r.text[:500]}")
    dados = r.json()
    uso = dados.get("usage") or {}
    custo = uso.get("cost")
    real = custo is not None
    if custo is None:
        custo = uso.get("input_tokens", 0) * config.PRECO_JEV_TOKEN_ENTRADA
    custos.registrar(projeto_id, etapa, "openrouter", config.MODELO_JUIZ, custo, real,
                     f"{len(perguntas)} perguntas · {uso.get('input_tokens', '?')} tokens")
    answers = dados.get("answers")
    if not isinstance(answers, dict):
        raise ErroAPI(f"Jev sem 'answers': {str(dados)[:300]}")
    registro.escrever(projeto_id, "API", f"jev · {len(perguntas)} perguntas · {time.time() - inicio:.1f}s",
                      dados={"respostas": answers})
    return answers, custo


def nota_score(resposta: dict, niveis: int | None = None) -> float:
    """Valor esperado de uma resposta score, levado para 0-10. Usa as probabilidades, não só a mais provável."""
    legenda = resposta.get("legend") or {}
    n = len(legenda) if legenda else (niveis or 6)
    probs = resposta.get("probabilities") or {}
    esperado, soma = 0.0, 0.0
    for k, p in probs.items():
        try:
            idx = int(k)
        except (TypeError, ValueError):
            idx = next((int(i) for i, v in legenda.items() if str(v) == str(k)), None)
            if idx is None:
                continue
        esperado += idx * float(p)
        soma += float(p)
    if soma > 0:
        esperado /= soma
    else:
        esperado = float(resposta.get("score", 0))
    return round(esperado / max(n - 1, 1) * 10, 2)


# ---------------------------------------------------------------- imagem (Pollinations)

# Depois de uma queda do Comfy, as próximas imagens vão direto para a Pollinations por um tempo (sem esperar
# o túnel morto a cada cena).
_comfy_fora_ate = 0.0


def usa_comfy() -> bool:
    """ComfyUI só quando o gerador está em "comfy", há um link salvo e o Comfy não caiu há pouco."""
    return config.motor_imagem() == "comfy" and bool(config.comfy_url()) and time.time() >= _comfy_fora_ate


def marcar_comfy_fora():
    """Manda as próximas imagens direto para a Pollinations por COMFY_PAUSA_S, sem esperar o túnel a cada cena."""
    global _comfy_fora_ate
    _comfy_fora_ate = time.time() + config.COMFY_PAUSA_S


def marcar_comfy_no_ar():
    """O link respondeu: desfaz a pausa de uma queda anterior."""
    global _comfy_fora_ate
    _comfy_fora_ate = 0.0


def gerar_imagem(prompt: str, largura: int, altura: int, seed: int, referencias: list | None = None, *,
                 projeto_id=None, etapa="imagens", modelo: str | None = None):
    """Devolve (bytes, url_link, custo). O cabeçalho Link traz a URL pública da imagem, reaproveitável como referência.
    Referências: URL pública (Pollinations) ou Path local (Comfy); um Path que cair na Pollinations é enviado antes."""
    modelo = modelo or config.MODELO_IMAGEM
    referencias = referencias or []
    if config.SIMULACAO:
        dados = simulacao.imagem(prompt, largura, altura, seed, referencias)
        custos.registrar(projeto_id, etapa, "simulacao", modelo, 0.0, True, f"seed {seed}")
        return dados, None, 0.0

    if usa_comfy():
        inicio = time.time()
        try:
            dados = comfy.gerar(prompt, largura, altura, seed, [Path(r) for r in referencias])
        except comfy.ComfyFora as e:
            marcar_comfy_fora()
            registro.escrever(projeto_id, "API", f"comfy fora, usando Pollinations: {e}")
        except comfy.ErroComfy as e:
            raise ErroAPI(str(e)) from e
        else:
            custos.registrar(projeto_id, etapa, "comfy", config.comfy_workflow().stem, 0.0, True,
                             f"{largura}x{altura} seed {seed} refs {len(referencias)} {time.time() - inicio:.0f}s")
            return dados, None, 0.0

    referencias = [enviar_midia(r) if isinstance(r, Path) else r for r in referencias]
    params = {"model": modelo, "width": largura, "height": altura, "seed": seed, "nologo": "true"}
    if referencias:
        params["image"] = "|".join(referencias)
    url = f"{config.POLLI_URL}/image/{quote(prompt, safe='')}"
    ultimo = None
    for i in range(config.API_TENTATIVAS):
        try:
            r = httpx.get(url, params=params, headers=_cabecalhos_polli(), timeout=config.IMAGEM_TIMEOUT_S)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            ultimo = e
            time.sleep(3 * (i + 1))
            continue
        if r.status_code >= 500 or r.status_code == 429:
            ultimo = ErroAPI(f"HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(4 * (i + 1))
            continue
        if r.status_code != 200:
            msg = r.text[:300]
            if r.status_code == 402:
                msg += " (saldo de Pollen insuficiente)"
            if r.status_code == 422 and re.search(r"moderation|content_safety|RAI", r.text, re.I):
                raise ErroModeracao(f"Pollinations HTTP 422: {r.text[:900]}")
            raise ErroAPI(f"Pollinations HTTP {r.status_code}: {msg}")
        link = None
        m = re.search(r"<([^>]+)>", r.headers.get("link", ""))
        if m:
            link = m.group(1)
        custo = _preco_imagem(modelo)
        custos.registrar(projeto_id, etapa, "pollinations", modelo, custo, False,
                         f"{largura}x{altura} seed {seed} refs {len(referencias or [])}")
        return r.content, link, custo
    raise ErroAPI(f"Pollinations sem resposta: {ultimo}")


def enviar_midia(caminho: Path) -> str:
    """Sobe um arquivo para media.pollinations.ai e devolve a URL pública."""
    if config.SIMULACAO:
        return f"simulacao://{Path(caminho).name}"
    with open(caminho, "rb") as f:
        r = httpx.post(f"{config.POLLI_MEDIA_URL}/upload", headers=_cabecalhos_polli(),
                       files={"file": (Path(caminho).name, f, "image/jpeg")}, timeout=120)
    if r.status_code != 200:
        raise ErroAPI(f"Upload falhou HTTP {r.status_code}: {r.text[:300]}")
    j = r.json()
    return j.get("url") or f"{config.POLLI_MEDIA_URL}/{j['id']}"


_precos_imagem: dict[str, float] = {}


def _preco_imagem(modelo: str) -> float:
    if not _precos_imagem:
        verificar_preco_imagem()
    return _precos_imagem.get(modelo, config.PRECO_IMAGEM_PADRAO)


def verificar_preco_imagem() -> str | None:
    """Lê o catálogo e avisa se o preço do modelo de imagem mudou (roteiro: conferir a cada execução)."""
    try:
        r = httpx.get(f"{config.POLLI_URL}/image/models", timeout=20)
        dados = r.json()
        dados = dados if isinstance(dados, list) else dados.get("data", [])
        for m in dados:
            preco = (m.get("pricing") or {}).get("completionImageTokens")
            if preco is not None:
                v = float(preco) * config.POLLEN_USD
                _precos_imagem[m.get("name")] = v
                for a in m.get("aliases") or []:
                    _precos_imagem[a] = v
    except Exception as e:  # catálogo fora do ar não pode travar a produção
        return f"Não consegui ler o catálogo de imagens: {e}"
    atual = _precos_imagem.get(config.MODELO_IMAGEM)
    if atual is not None and abs(atual - config.PRECO_IMAGEM_PADRAO) > 1e-9:
        return (f"Atenção: o preço de {config.MODELO_IMAGEM} mudou para US$ {atual:.4f} por imagem "
                f"(referência: {config.PRECO_IMAGEM_PADRAO}).")
    return None
