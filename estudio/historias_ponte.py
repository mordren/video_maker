"""Ponte entre a página única do Estúdio (porta 8090) e o serviço de Histórias.

O Estúdio de Histórias (historias/, FastAPI) roda à parte, só para esta
máquina (127.0.0.1:8091), e as abas Histórias, Music, Canais, Custos e
Configurações da página do Estúdio usam a API dele sem abrir outra porta:
toda rota que o Flask não conhece é repassada para lá, com o mesmo método,
cabeçalhos e corpo, e a resposta volta em streaming.

Os arquivos grandes (vídeos e imagens dos projetos, a saída do Music) e a
interface (static/) saem direto do disco, pelo send_from_directory do Flask:
no waitress isso não prende uma das poucas threads do Estúdio enquanto o
vídeo é baixado, e o Range (pular no vídeo) funciona como nos cortes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import requests
from flask import Blueprint, Response, abort, request, send_from_directory, stream_with_context

AQUI = Path(__file__).resolve().parent
CODIGO = AQUI.parent / "historias"
ESTATICOS = CODIGO / "estudio" / "static"
URL = (os.environ.get("ESTUDIO_HISTORIAS_URL") or "http://127.0.0.1:8091").rstrip("/")


def raiz_dados(data_dir: Path) -> Path:
    """Onde o serviço de Histórias guarda banco, projetos e Music (ESTUDIO_RAIZ
    dele). Padrão: ao lado dos dados do Estúdio (~/VideoMaker/historias)."""
    return Path(os.environ.get("ESTUDIO_HISTORIAS_RAIZ") or (data_dir.parent / "historias"))


bp = Blueprint("historias", __name__)
_sessao = requests.Session()
_RAIZ: Path | None = None

# Cabeçalhos que valem só para um salto da conexão (RFC 9110 §7.6.1) e os que
# o requests/waitress recalculam.
_SALTO = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
          "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
_CORPO_EM_MEMORIA = 8 * 1024 * 1024


def iniciar(app, data_dir: Path) -> None:
    global _RAIZ
    _RAIZ = raiz_dados(data_dir)
    app.register_blueprint(bp)


@bp.get("/static/<path:arquivo>")
def estaticos(arquivo: str):
    r = send_from_directory(ESTATICOS, arquivo, max_age=0)
    r.headers["Cache-Control"] = "no-cache"
    return r


def _servir(raiz: Path, relativo: str):
    alvo = (raiz / relativo).resolve()
    if not alvo.is_relative_to(raiz.resolve()) or not alvo.is_file():
        abort(404)
    r = send_from_directory(alvo.parent, alvo.name, conditional=True, max_age=0)
    r.headers["Cache-Control"] = "no-store"
    return r


@bp.get("/arquivos/<path:caminho>")
def arquivos(caminho: str):
    """Mesmas pastas que o /arquivos do serviço de Histórias libera."""
    for pasta in ("projetos", "biblioteca", "trilhas", "saida"):
        if caminho.split("/", 1)[0] == pasta:
            return _servir(_RAIZ, caminho)
    abort(404)


@bp.get("/api/gerador/saida/<nome>")
def saida_music(nome: str):
    """A pasta de saída do Music pode ter sido trocada na tela (gerador.json)."""
    pasta = _RAIZ / "saida"
    try:
        escolhida = (json.loads((_RAIZ / "dados" / "gerador.json").read_text(encoding="utf-8"))
                     .get("config", {}).get("pastas", {}).get("saida"))
        if escolhida:
            pasta = Path(escolhida) if Path(escolhida).is_absolute() else _RAIZ / escolhida
    except (OSError, ValueError, AttributeError):
        pass
    if Path(nome).suffix.lower() not in (".mp4", ".txt"):
        abort(404)
    return _servir(pasta, Path(nome).name)


def _corpo():
    if request.content_length is not None and request.content_length <= _CORPO_EM_MEMORIA:
        return request.get_data()
    if request.content_length is None and "chunked" not in request.headers.get("Transfer-Encoding", ""):
        return None

    def pedacos():   # upload grande (música, clipe): vai em pedaços, sem guardar na memória
        while True:
            pedaco = request.stream.read(1024 * 1024)
            if not pedaco:
                return
            yield pedaco
    return pedacos()


def repassar(caminho: str):
    cabecalhos = {k: v for k, v in request.headers.items() if k.lower() not in _SALTO}
    # O serviço monta endereços absolutos (retorno do login do YouTube) com o
    # endereço que o navegador usou, não com 127.0.0.1.
    cabecalhos["Host"] = request.host
    cabecalhos["X-Forwarded-For"] = request.remote_addr or ""
    corpo = _corpo()
    if corpo is not None and not isinstance(corpo, bytes):
        cabecalhos.pop("Content-Length", None)
    try:
        r = _sessao.request(request.method, f"{URL}/{caminho}", params=list(request.args.items(multi=True)),
                            headers=cabecalhos,
                            data=corpo, stream=True, allow_redirects=False, timeout=(5, 900))
    except requests.ConnectionError:
        return Response(json.dumps({"detail": "O serviço de Histórias não está no ar (videomaker-historias). "
                                              "O Cortador continua funcionando."}),
                        status=502, mimetype="application/json")
    except requests.Timeout:
        return Response(json.dumps({"detail": "O serviço de Histórias demorou demais para responder."}),
                        status=504, mimetype="application/json")
    volta = [(k, v) for k, v in r.raw.headers.items() if k.lower() not in _SALTO]
    if r.headers.get("Content-Length") and not r.headers.get("Content-Encoding"):
        volta.append(("Content-Length", r.headers["Content-Length"]))

    def corpo_resposta():
        try:
            yield from r.raw.stream(64 * 1024, decode_content=False)
        finally:
            r.close()
    return Response(stream_with_context(corpo_resposta()), status=r.status_code, headers=volta,
                    direct_passthrough=True)


_METODOS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
# Por último na ordem do Flask (regra só com conversor): as rotas do Estúdio vencem.
bp.add_url_rule("/<path:caminho>", "repassar", repassar, methods=_METODOS)
