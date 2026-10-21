"""Envio do vídeo pronto para o Publicador (a fila de postagem no YouTube que roda na rede local).

Equivale a:  curl -F "canal=garras" -F "arquivos=@final.mp4" http://<publicador>/api/videos
O arquivo vai com o nome do título da história; se o Publicador devolver o id do item, o título também é
gravado nele (POST /api/itens/{id} {"titulo": ...}). A descrição é a do canal, no Publicador — a não ser
que se mande `legenda` junto do arquivo (campo opcional): aí esse texto vale só para aquele vídeo.
"""
import json
import re

import httpx

from . import config, db, registro


class ErroPublicador(Exception):
    pass


def nome_arquivo(titulo: str) -> str:
    nome = re.sub(r'[\\/:*?"<>|]+', "", titulo or "").strip().rstrip(".")
    return (nome[:90] or "video") + ".mp4"


def _dados_do_envio(canal: str, legenda: str) -> dict:
    """Campos de texto da entrada da API: canal e, se houver, a legenda do vídeo."""
    dados = {"canal": canal}
    legenda = (legenda or "").strip()[:4900]
    if legenda:
        dados["legenda"] = legenda
    return dados


def enviar(projeto: dict, canal: dict, legenda: str = "") -> dict:
    video = config.BASE / projeto["pasta"] / "final.mp4"
    if not video.exists():
        raise ErroPublicador("O vídeo ainda não foi montado")
    canal_pub = (canal["config"].get("publicador_canal") or "").strip()
    if not canal_pub:
        raise ErroPublicador("Defina o \"canal no publicador\" na tela do canal antes de enviar")
    base = config.PUBLICADOR_URL.rstrip("/")
    h = projeto.get("historia") or {}
    titulo = h.get("titulo") or projeto["assunto"]
    try:
        with open(video, "rb") as f:
            r = httpx.post(f"{base}/api/videos", data=_dados_do_envio(canal_pub, legenda),
                           files={"arquivos": (nome_arquivo(titulo), f, "video/mp4")}, timeout=config.PUBLICADOR_TIMEOUT_S)
    except httpx.HTTPError as e:
        raise ErroPublicador(f"Não consegui falar com o publicador em {base}: {e}")
    try:
        dados = r.json()
    except ValueError:
        dados = {"texto": r.text[:300]}
    if r.status_code >= 400 or dados.get("erro"):
        raise ErroPublicador(f"O publicador recusou (HTTP {r.status_code}): {dados.get('erro') or dados}")
    # Garante o título certo no item da fila, quando o publicador informa o id.
    titulos_gravados = []
    for item in dados.get("adicionados") or []:
        item_id = item.get("id") if isinstance(item, dict) else None
        if item_id is not None:
            try:
                httpx.post(f"{base}/api/itens/{item_id}", json={"titulo": titulo}, timeout=30)
                titulos_gravados.append(item_id)
            except httpx.HTTPError:
                pass
    resultado = {"enviado_em": db.agora(), "canal": canal_pub, "url": base, "arquivo": nome_arquivo(titulo),
                 "titulo": titulo, "resposta": dados, "titulo_gravado_em": titulos_gravados}
    db.executar("UPDATE projetos SET publicador_json = ? WHERE id = ?",
                (json.dumps(resultado, ensure_ascii=False), projeto["id"]))
    db.evento(projeto["id"], f"Enviado ao publicador ({canal_pub}): {len(dados.get('adicionados') or [])} vídeo(s) "
                             "na fila.")
    registro.escrever(projeto["id"], "PUBLIC", "enviado ao publicador", dados=resultado)
    return resultado


def canais_disponiveis() -> list[str]:
    """Nomes dos canais que o Publicador conhece (os que têm login lá)."""
    base = config.PUBLICADOR_URL.rstrip("/")
    try:
        r = httpx.get(f"{base}/api/estado", timeout=15)
        r.raise_for_status()
        return [c["nome"] for c in (r.json().get("canais") or []) if c.get("nome")]
    except (httpx.HTTPError, ValueError, KeyError) as e:
        raise ErroPublicador(f"Não consegui falar com o publicador em {base}: {e}")


def enviar_arquivo(video, titulo: str, canal: str, legenda: str = "") -> dict:
    """Manda um mp4 qualquer (ex.: vídeo do Music) para a fila de um canal do Publicador, com o título dado.

    `legenda` é opcional: sem ela, o vídeo usa a descrição/legenda padrão do canal
    no Publicador; com ela, esse texto vale só para este vídeo.
    """
    canal = (canal or "").strip()
    if not canal:
        raise ErroPublicador("Escolha o canal do Publicador")
    titulo = (titulo or "").strip() or video.stem
    base = config.PUBLICADOR_URL.rstrip("/")
    try:
        with open(video, "rb") as f:
            r = httpx.post(f"{base}/api/videos", data=_dados_do_envio(canal, legenda),
                           files={"arquivos": (nome_arquivo(titulo), f, "video/mp4")}, timeout=config.PUBLICADOR_TIMEOUT_S)
    except (httpx.HTTPError, OSError) as e:
        raise ErroPublicador(f"Não consegui enviar ao publicador em {base}: {e}")
    try:
        dados = r.json()
    except ValueError:
        dados = {"texto": r.text[:300]}
    if r.status_code >= 400 or dados.get("erro"):
        raise ErroPublicador(f"O publicador recusou (HTTP {r.status_code}): {dados.get('erro') or dados}")
    for item in dados.get("adicionados") or []:
        item_id = item.get("id") if isinstance(item, dict) else None
        if item_id is not None:
            try:
                httpx.post(f"{base}/api/itens/{item_id}", json={"titulo": titulo}, timeout=30)
            except httpx.HTTPError:
                pass
    return {"canal": canal, "titulo": titulo, "adicionados": len(dados.get("adicionados") or [])}
