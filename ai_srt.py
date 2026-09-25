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

# Pedido extra, colado nos dois prompts: além de corrigir, a IA aponta onde há
# palavra sensível. Isso resolve o furo do casamento por texto — o Whisper erra
# justamente nessas palavras, por serem incomuns, e escreve algo que não existe
# ("escuprida" no lugar de "estupro"). Quem lê a frase inteira entende; uma
# lista de palavras, não.
_SENSITIVE_RULE = """

Aponte também onde há palavrão ou conteúdo que costuma derrubar o alcance nas \
redes (violência, crime, sexo, drogas, automutilação, termos pejorativos). Use a \
chave "sensiveis", com uma entrada por ocorrência:
{"linha": 3, "trecho": "estupro", "motivo": "violência sexual"}
- linha: o número da linha em que aparece; a primeira linha é 1.
- trecho: a palavra exatamente como ela ficou na SUA linha corrigida.
- motivo: duas ou três palavras dizendo por quê.
- O reconhecimento de fala erra muito nessas palavras justamente por serem \
incomuns. Se a linha tiver algo sem sentido que claramente era uma delas \
(por exemplo "escuprida" onde se disse "estupro"), corrija na linha e aponte \
aqui assim mesmo.
- Se não houver nenhuma, devolva "sensiveis": []."""

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
- Responda apenas com JSON no formato {"linhas": ["...", "..."], "sensiveis": [...]}""" + _SENSITIVE_RULE

_REVIEW_TITLE_PROMPT = """Você trabalha a legenda de um vídeo político curto do Brasil \
— em geral a fala ou a opinião de um político sobre algum tema. Faça as duas coisas \
abaixo e devolva TUDO num único JSON.

1) Revise a legenda: corrija ortografia, acentuação, pontuação, concordância e nomes \
próprios mal transcritos pelo reconhecimento de fala. Mantenha o jeito falado (gírias, \
repetições, frases cortadas no meio). Não traduza, não resuma, não reescreva o estilo.
2) Crie um título e um subtítulo para o vídeo, com base só no que é dito na fala.

Responda apenas com JSON, exatamente neste formato:
{"titulo": "...", "subtitulo": "...", "musica": "...", "linhas": ["...", "..."], "sensiveis": [...]}

- titulo: um chapéu curto de tema/categoria; no máximo ~30 caracteres; em CAIXA ALTA \
(ex.: ECONOMIA, ELEIÇÕES 2026, STF, SEGURANÇA). É a linha pequena, no topo.
- subtitulo: a manchete em destaque, chamativa mas fiel à fala; no máximo ~60 \
caracteres; sem ponto final. É a linha grande, embaixo.
- linhas: exatamente a mesma quantidade de linhas que você recebeu, na mesma ordem; \
nunca junte nem separe linhas; se uma já estiver certa, devolva-a igual.
- musica: o clima da trilha de fundo, escolhido da lista que vem no fim deste texto. Responda com o rótulo exatamente como aparece na lista, sem inventar outro. Pense no tom da fala: um bate-boca pede confronto, uma denúncia pede algo grave, uma reflexão pede algo contido. Na dúvida, prefira a opção mais neutra.
- Não invente fatos que não estão na fala.""" + _SENSITIVE_RULE


# Catálogo de trilhas, colado no fim do prompt. Os rótulos saem dos nomes dos
# arquivos da pasta, então a lista muda sozinha quando o usuário acrescenta ou
# tira uma música — o prompt não precisa saber quais são.
_MUSIC_LIST_HEADER = '\n\nTrilhas disponíveis (escolha uma para "musica"):\n- '
_NO_MUSIC_NOTE = '\n\nNão há trilhas disponíveis: devolva "musica": "".'


def _parse_sensitive(obj: dict, total_lines: int, offset: int = 0) -> list[dict]:
    """Lê a chave `sensiveis` da resposta, descartando o que vier torto.

    A resposta do modelo é texto livre: qualquer campo pode faltar ou vir com o
    tipo errado. Só passam entradas com uma linha existente e um trecho não
    vazio; `offset` desloca a numeração quando as linhas foram enviadas em lotes.
    """
    items = obj.get("sensiveis")
    if not isinstance(items, list):
        return []
    found: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            line = int(item.get("linha"))
        except (TypeError, ValueError):
            continue
        trecho = str(item.get("trecho") or "").strip()
        if not trecho or not 1 <= line <= total_lines:
            continue
        found.append({
            "linha": line + offset,
            "trecho": trecho,
            "motivo": str(item.get("motivo") or "").strip(),
        })
    return found


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


def get_current_profile_name() -> str:
    """Retorna o nome do perfil ativo (ou vazio se nenhum)."""
    cfg = load_config()
    return str(cfg.get("current_profile", "")).strip()


def get_profiles_dict() -> tuple[dict, str]:
    """Retorna (perfis_dict, nome_do_perfil_atual).

    A estrutura em config.json é:
    {
      "current_profile": "info_nacional",
      "profiles": {
        "info_nacional": { brand_logo, brand_name, ... },
        ...
      }
    }
    """
    cfg = load_config()
    profiles = cfg.get("profiles", {})
    if not isinstance(profiles, dict):
        profiles = {}
    current = get_current_profile_name()
    return profiles, current


def load_profile(name: str) -> dict:
    """Carrega os dados de um perfil específico (chaves brand_*).

    Retorna um dict vazio se o perfil não existir.
    """
    profiles, _ = get_profiles_dict()
    profile_data = profiles.get(name, {})
    return dict(profile_data) if isinstance(profile_data, dict) else {}


def save_profile(name: str, data: dict, set_as_current: bool = False) -> bool:
    """Salva ou atualiza um perfil com os dados fornecidos.

    Args:
        name: nome do perfil (ex.: "info_nacional")
        data: dict com chaves brand_* a guardar
        set_as_current: se True, torna este o perfil ativo

    Returns:
        True se bem-sucedido.
    """
    profiles, current = get_profiles_dict()
    # Guarda o perfil
    profiles[name] = dict(data)
    # Salva tudo de volta
    to_save = {
        "profiles": profiles,
        "current_profile": name if set_as_current else current,
    }
    return save_config(to_save)


def delete_profile(name: str) -> bool:
    """Deleta um perfil. Se for o atual, torna o primeiro da lista como padrão.

    Returns:
        False se o perfil não existir ou se for o último.
    """
    profiles, current = get_profiles_dict()
    if name not in profiles:
        return False
    if len(profiles) <= 1:
        return False  # não deixa deletar o único perfil
    del profiles[name]
    # Se era o atual, muda para o primeiro disponível
    new_current = current if current in profiles else next(iter(profiles), "")
    to_save = {
        "profiles": profiles,
        "current_profile": new_current,
    }
    return save_config(to_save)


def set_current_profile(name: str) -> bool:
    """Define qual perfil fica ativo.

    Returns:
        False se o perfil não existir.
    """
    profiles, _ = get_profiles_dict()
    if name not in profiles:
        return False
    return save_config({"current_profile": name})


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


def ask(prompt: str, api_key: str, model: str = DEFAULT_MODEL,
        max_tokens: int = 1400) -> tuple[str, dict]:
    """Uma pergunta solta ao modelo; devolve (resposta em texto, uso de tokens).

    Diferente do resto do módulo, aqui a resposta é texto livre, não JSON — é
    usada para redigir a legenda do Reels, que vai inteira para um arquivo.
    O teto de saída existe para uma resposta desgovernada não virar conta alta.
    """
    if not api_key:
        raise AiError("Informe a chave da API do DeepSeek.")
    payload = {
        "model": model or DEFAULT_MODEL,
        "temperature": 0.8,          # texto de marketing pede mais soltura
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = _request("/chat/completions", api_key, payload)
    usage = data.get("usage") or {}
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as error:
        raise AiError("Resposta da API sem conteúdo.") from error
    return str(content).strip(), usage


_CUTS_PROMPT = """Você é um cortador de vídeos políticos. Recebe a transcrição de \
uma live, discurso, entrevista, debate ou podcast (com marcação de tempo) e escolhe \
os melhores trechos para virarem shorts/reels — os mais fortes, polêmicos e \
"meme-áveis", que se sustentam sozinhos.

O que puxar (uma ou mais categorias por corte):
- declaração-tese ou bordão que resume a posição do orador;
- ataque direto e nominal a adversário, instituição ou grupo;
- momento-personagem: fala arrogante, engraçada, provocadora;
- contradição, revelação de estratégia ou bastidor;
- carga emocional (indignação, exaltação, comoção);
- número ou afirmação forte que sozinha rende manchete.

Como montar cada corte:
- DURAÇÃO ENTRE 1MIN E 2MIN30. Isto é obrigatório: um corte com menos de 1min \
ou mais de 2min30 não serve e não deve ser incluído. Um short longo demais não \
funciona.
- Para chegar a essa duração, pegue o RACIOCÍNIO INTEIRO em volta do momento forte, \
não só a frase de efeito. Comece bem antes, quando a pessoa monta o assunto (o \
gancho, o setup, a pergunta), passe pelo desenvolvimento e só termine depois de a \
ideia fechar. A frase de efeito é o clímax do corte, não o corte inteiro.
- Se um momento forte não tiver contexto suficiente em volta para sustentar 1 \
minuto, NÃO o inclua — melhor deixar de fora do que entregar um corte curto.
- Um assunto por corte; comece numa abertura que já prende e termine numa frase que \
fecha, nunca no meio de um raciocínio.

Responda APENAS com JSON, exatamente neste formato:
{"cortes": [{"inicio": "m:ss", "fim": "m:ss", "titulo": "...", "subtitulo": "...", \
"musica": "...", "comentario": "..."}]}

- inicio, fim: os tempos do trecho, no formato m:ss (ou h:mm:ss acima de 1h), \
tirados da marcação da transcrição. Dê ~1s de folga antes e depois para não cortar \
a fala no talo.
- titulo: o gancho curto (o "título do short"), no máximo 25 caracteres. Priorize a \
frase de efeito ou o bordão, não a descrição do tema. Em CAIXA ALTA.
- subtitulo: a manchete em destaque, chamativa e fiel à fala, até ~60 caracteres, \
sem ponto final.
- musica: o clima da trilha de fundo deste corte, escolhido da lista que vem no fim \
deste texto. Responda com o rótulo exatamente como aparece na lista, sem inventar \
outro. Pense no tom do trecho: um bate-boca pede confronto, uma denúncia pede algo \
grave, uma reflexão pede algo contido. Na dúvida, prefira a opção mais neutra. Se não \
houver lista de trilhas, devolva "musica": "".
- comentario: 1 ou 2 frases dizendo por que o trecho vira um bom short e a duração \
estimada. Quando o trecho imputa crime a pessoa nomeada, acusa sobre a vida privada \
ou xinga alguém identificável, comece o comentario com "⚠️ " e diga o risco \
(difamação, possível strike/desmonetização).

QUALIDADE ACIMA DE QUANTIDADE. Não existe número mínimo de cortes. Traga só os \
trechos que realmente se sustentam sozinhos como um bom short — nem que seja UM \
único corte, ou nenhum. É muito melhor um corte forte do que cinco medianos. Se o \
vídeo só tem um momento que presta, devolva só ele. Não encha a lista para parecer \
mais completo.

Ordene os cortes do mais forte para o mais fraco, não em ordem cronológica. Escolha \
pelo potencial de audiência, sem tomar partido nem distorcer o sentido da fala."""


def suggest_cuts(transcript: str, api_key: str, model: str = DEFAULT_MODEL,
                 musicas: list[str] | None = None) -> tuple[list[dict], dict]:
    """Escolhe cortes a partir da transcrição com tempos. Devolve (cortes, uso).

    Cada corte é um dict com inicio, fim, titulo, subtitulo, musica e comentario
    — como veio da IA, em texto. Quem converte os tempos e monta os "moments" é a
    interface, para manter este módulo sem dependência do resto do app.

    `musicas` são os rótulos de clima disponíveis (vindos dos nomes dos arquivos
    da pasta de trilhas). Quando há lista, ela é colada no fim do prompt e a IA
    escolhe uma por corte; sem lista, o campo `musica` volta vazio.
    """
    if not api_key:
        raise AiError("Informe a chave da API do DeepSeek.")
    if not transcript.strip():
        return [], {}
    system = _CUTS_PROMPT
    if musicas:
        system += _MUSIC_LIST_HEADER + "\n- ".join(musicas)
    else:
        system += _NO_MUSIC_NOTE
    payload = {
        "model": model or DEFAULT_MODEL,
        "temperature": 0.4,
        # A saída tem vários cortes; um teto generoso evita cortar a lista no
        # meio, mas ainda segura uma resposta desgovernada.
        "max_tokens": 4000,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": transcript},
        ],
    }
    data = _request("/chat/completions", api_key, payload)
    usage = data.get("usage") or {}
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError) as error:
        raise AiError("Resposta da API sem conteúdo.") from error
    obj = _unwrap_json(content)
    cortes = obj.get("cortes")
    if not isinstance(cortes, list):
        raise AiError("A IA não devolveu a lista de cortes.")
    limpos = [c for c in cortes if isinstance(c, dict) and c.get("inicio") and c.get("fim")]
    # A trilha só vale se for uma das oferecidas; qualquer invenção vira "".
    validos = {m.lower() for m in (musicas or [])}
    for c in limpos:
        musica = str(c.get("musica") or "").strip()
        c["musica"] = musica if musica.lower() in validos else ""
    return limpos, usage


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

    obj = _unwrap_json(content)
    sensiveis = _parse_sensitive(obj, len(lines))
    fixed = obj.get("linhas")
    # Só aceita se vier a mesma quantidade de linhas: uma resposta com mais ou
    # menos blocos desalinharia a legenda do áudio. Na dúvida, fica o original.
    if not isinstance(fixed, list) or len(fixed) != len(lines):
        return lines, sensiveis, usage
    return ([str(new).strip() or old for new, old in zip(fixed, lines)],
            sensiveis, usage)


def correct_lines(lines: list[str], api_key: str, model: str = DEFAULT_MODEL,
                  context: str = "", progress=None) -> tuple[list[str], list[dict], dict]:
    """Corrige as falas em lotes, preservando a quantidade e a ordem.

    Devolve (linhas corrigidas, trechos sensíveis, uso somado de tokens).
    `progress` é chamado com (linhas_prontas, total) a cada lote, para a
    interface mostrar andamento.
    """
    if not api_key:
        raise AiError("Informe a chave da API do DeepSeek.")
    if not lines:
        return [], [], {}
    result: list[str] = []
    sensiveis: list[dict] = []
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for start in range(0, len(lines), CHUNK):
        batch = lines[start:start + CHUNK]
        fixed, achados, usage = _correct_chunk(
            batch, api_key, model or DEFAULT_MODEL, context)
        result.extend(fixed)
        # A IA numera dentro do lote; aqui vira o número da linha no SRT todo.
        sensiveis.extend(dict(item, linha=item["linha"] + start) for item in achados)
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
        if progress:
            progress(len(result), len(lines))
    return result, sensiveis, totals


def review_lines_with_title(lines: list[str], api_key: str, model: str = DEFAULT_MODEL,
                            context: str = "", musicas: list[str] | None = None
                            ) -> tuple[str, str, str, list[str], list[dict], dict]:
    """Numa só requisição: corrige as falas, cria título/subtítulo, sinaliza risco
    e escolhe a trilha.

    Devolve (título, subtítulo, trilha escolhida, linhas corrigidas, trechos
    sensíveis, uso de tokens). A resposta é um único JSON. Se a chave `linhas`
    não vier com a mesma quantidade, ficam as originais (o resto é aproveitado).

    `musicas` são os rótulos de clima disponíveis; sem eles a chave `musica` não
    é nem pedida, porque não haveria de onde escolher.
    """
    if not api_key:
        raise AiError("Informe a chave da API do DeepSeek.")
    if not lines:
        return "", "", "", [], [], {}
    user = json.dumps({"linhas": lines}, ensure_ascii=False)
    # Saída ≈ entrada (mesmas linhas) + um punhado para título/subtítulo.
    max_tokens = min(4096, max(512, len(user) // 2 + 512))
    prompt = _REVIEW_TITLE_PROMPT
    if musicas:
        prompt += _MUSIC_LIST_HEADER + "\n- ".join(musicas)
    else:
        prompt += _NO_MUSIC_NOTE
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
    sensiveis = _parse_sensitive(obj, len(lines))
    # Só vale um rótulo que exista de fato: a IA às vezes inventa um clima que
    # não está na pasta, e aí é melhor ficar sem trilha do que com a errada.
    musica = str(obj.get("musica") or "").strip()
    if musicas and musica.lower() not in {m.lower() for m in musicas}:
        musica = ""
    fixed = obj.get("linhas")
    if not isinstance(fixed, list) or len(fixed) != len(lines):
        fixed = lines
    else:
        fixed = [str(new).strip() or old for new, old in zip(fixed, lines)]
    return titulo, subtitulo, musica, fixed, sensiveis, usage
