"""Envio do vídeo pronto para o Publicador (a fila de postagem no YouTube que roda na rede local).

Equivale a:  curl -F "canal=garras" -F "arquivos=@final.mp4" http://<publicador>/api/videos
O arquivo vai com o nome do título da história; se o Publicador devolver o id do item, o título também é
gravado nele (POST /api/itens/{id} {"titulo": ...}). A descrição fica na configuração de cada canal lá.
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


def enviar(projeto: dict, canal: dict) -> dict:
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
            r = httpx.post(f"{base}/api/videos", data={"canal": canal_pub},
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
