"""Métricas do YouTube: views, tempo assistido e curva de retenção de cada vídeo publicado.

Configuração (uma vez): crie um cliente OAuth "Aplicativo da Web" no Google Cloud com as APIs
"YouTube Data API v3" e "YouTube Analytics API" ativadas, URI de redirecionamento
http://localhost:8000/api/youtube/retorno, e salve o JSON baixado como dados/youtube_cliente.json.
Depois é só clicar em "Conectar YouTube" na página Desempenho.

A API de Analytics tem 2 a 3 dias de atraso; o número de views da Data API é quase em tempo real.
"""
import json
import os
import re
from datetime import date, datetime

from . import config, db, registro

# O login acontece em http://localhost; o oauthlib exige https fora disso.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

ESCOPOS = ["https://www.googleapis.com/auth/youtube.readonly",
           "https://www.googleapis.com/auth/yt-analytics.readonly"]
ARQ_CLIENTE = config.DADOS / "youtube_cliente.json"
ARQ_TOKEN = config.DADOS / "youtube_token.json"
_fluxos: dict = {}  # state -> Flow (precisa ser o mesmo objeto no retorno por causa do PKCE)


class ErroYouTube(Exception):
    pass


# ---------------------------------------------------------------- login

def status() -> dict:
    st = {"cliente_configurado": ARQ_CLIENTE.exists(), "conectado": False, "canal": None}
    if ARQ_TOKEN.exists():
        try:
            yt = _servico("youtube", "v3")
            r = yt.channels().list(part="snippet", mine=True).execute()
            itens = r.get("items") or []
            st["conectado"] = True
            if itens:
                st["canal"] = {"id": itens[0]["id"], "titulo": itens[0]["snippet"]["title"]}
        except Exception as e:
            st["erro"] = str(e)[:300]
    return st


def url_login(base_url: str) -> str:
    if not ARQ_CLIENTE.exists():
        raise ErroYouTube(f"Falta o arquivo {ARQ_CLIENTE}. Veja as instruções na página Desempenho.")
    from google_auth_oauthlib.flow import Flow
    flow = Flow.from_client_secrets_file(str(ARQ_CLIENTE), scopes=ESCOPOS,
                                         redirect_uri=base_url.rstrip("/") + "/api/youtube/retorno")
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    _fluxos[state] = flow
    return url


def concluir_login(url_retorno: str, state: str):
    flow = _fluxos.pop(state, None)
    if not flow:
        raise ErroYouTube("Login expirado ou aberto em outra janela. Clique em Conectar YouTube de novo.")
    flow.fetch_token(authorization_response=url_retorno)
    ARQ_TOKEN.write_text(flow.credentials.to_json(), encoding="utf-8")
    registro.escrever(None, "EVENTO", "YouTube conectado")


def desconectar():
    ARQ_TOKEN.unlink(missing_ok=True)


def _credenciais():
    if not ARQ_TOKEN.exists():
        raise ErroYouTube("YouTube não conectado. Clique em Conectar YouTube na página Desempenho.")
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    cred = Credentials.from_authorized_user_file(str(ARQ_TOKEN), ESCOPOS)
    if not cred.valid:
        if cred.expired and cred.refresh_token:
            try:
                cred.refresh(Request())
            except Exception as e:
                raise ErroYouTube(f"O login do YouTube expirou ({e}). Conecte de novo.")
            ARQ_TOKEN.write_text(cred.to_json(), encoding="utf-8")
        else:
            raise ErroYouTube("O login do YouTube expirou. Conecte de novo.")
    return cred


def _servico(nome: str, versao: str):
    from googleapiclient.discovery import build
    return build(nome, versao, credentials=_credenciais(), cache_discovery=False)


# ---------------------------------------------------------------- vínculo vídeo <-> projeto

def extrair_id(texto: str) -> str | None:
    texto = (texto or "").strip()
    for rx in (r"(?:v=|/shorts/|youtu\.be/|/embed/|/live/)([A-Za-z0-9_-]{11})", r"^([A-Za-z0-9_-]{11})$"):
        m = re.search(rx, texto)
        if m:
            return m.group(1)
    return None


def _normalizar(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"#\w+", "", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def uploads(limite: int = 200) -> list[dict]:
    yt = _servico("youtube", "v3")
    canal = yt.channels().list(part="contentDetails", mine=True).execute()["items"][0]
    lista = canal["contentDetails"]["relatedPlaylists"]["uploads"]
    videos, token = [], None
    while len(videos) < limite:
        r = yt.playlistItems().list(part="snippet,contentDetails", playlistId=lista, maxResults=50,
                                    pageToken=token).execute()
        for it in r.get("items", []):
            videos.append({"id": it["contentDetails"]["videoId"], "titulo": it["snippet"]["title"],
                           "publicado_em": it["contentDetails"].get("videoPublishedAt")})
        token = r.get("nextPageToken")
        if not token:
            break
    return videos


def vincular_por_titulo() -> list[dict]:
    """Liga projetos sem vídeo aos uploads do canal cujo título bate com o título da história."""
    vids = uploads()
    ligados = {p["youtube_id"] for p in db.todos("SELECT youtube_id FROM projetos WHERE youtube_id IS NOT NULL")}
    feitos = []
    for p in db.todos("SELECT id, historia_json FROM projetos WHERE youtube_id IS NULL"):
        titulo = _normalizar((db.carregar_json(p["historia_json"], {}) or {}).get("titulo", ""))
        if len(titulo) < 4:
            continue
        for v in vids:
            tv = _normalizar(v["titulo"])
            if v["id"] not in ligados and (tv == titulo or tv.startswith(titulo) or titulo.startswith(tv)):
                db.executar("UPDATE projetos SET youtube_id = ? WHERE id = ?", (v["id"], p["id"]))
                ligados.add(v["id"])
                feitos.append({"projeto": p["id"], "youtube_id": v["id"], "titulo": v["titulo"]})
                registro.escrever(p["id"], "EVENTO", f"Ligado ao YouTube pelo título: {v['titulo']} ({v['id']})")
                break
    return feitos


# ---------------------------------------------------------------- métricas

def _segundos_iso(d: str) -> float:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0.0
    dd, h, mi, s = (int(x or 0) for x in m.groups())
    return dd * 86400 + h * 3600 + mi * 60 + s


def _interpolar(curva: list[tuple[float, float]], x: float) -> float | None:
    if not curva:
        return None
    if x <= curva[0][0]:
        return curva[0][1]
    for (x0, y0), (x1, y1) in zip(curva, curva[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / ((x1 - x0) or 1)
    return curva[-1][1]


def _consulta(ya, **kw) -> dict:
    return ya.reports().query(ids="channel==MINE", **kw).execute()


def metricas_video(video_id: str) -> dict:
    yt = _servico("youtube", "v3")
    ya = _servico("youtubeAnalytics", "v2")
    itens = yt.videos().list(part="snippet,statistics,contentDetails", id=video_id).execute().get("items") or []
    if not itens:
        raise ErroYouTube(f"Vídeo {video_id} não encontrado no seu canal")
    v = itens[0]
    est = v.get("statistics", {})
    publicado = v["snippet"]["publishedAt"][:10]
    hoje = date.today().isoformat()
    dados = {
        "titulo": v["snippet"]["title"], "publicado_em": publicado,
        "duracao_s": _segundos_iso(v["contentDetails"]["duration"]),
        "views": int(est.get("viewCount", 0)), "likes": int(est.get("likeCount", 0)),
        "comentarios": int(est.get("commentCount", 0)), "atualizado_em": db.agora(),
    }
    filtro = f"video=={video_id}"
    # Totais do Analytics (atrasam 2-3 dias). engagedViews só existe em canais/regiões novos; cai fora se falhar.
    for metricas in ("views,engagedViews,likes,shares,averageViewDuration,averageViewPercentage,subscribersGained",
                     "views,likes,shares,averageViewDuration,averageViewPercentage,subscribersGained"):
        try:
            r = _consulta(ya, startDate=publicado, endDate=hoje, metrics=metricas, filters=filtro)
            if r.get("rows"):
                linha = dict(zip([c["name"] for c in r["columnHeaders"]], r["rows"][0]))
                dados.update({"views_analytics": linha.get("views"), "views_engajadas": linha.get("engagedViews"),
                              "compartilhamentos": linha.get("shares"),
                              "media_visualizacao_s": linha.get("averageViewDuration"),
                              "media_percentual": linha.get("averageViewPercentage"),
                              "inscritos_ganhos": linha.get("subscribersGained")})
            break
        except Exception as e:
            dados["aviso_totais"] = str(e)[:200]
    # Curva de retenção: 100 pontos de 0 a 1 da duração; audienceWatchRatio passa de 1 quando o Short repete.
    try:
        r = _consulta(ya, startDate=publicado, endDate=hoje, dimensions="elapsedVideoTimeRatio",
                      metrics="audienceWatchRatio,relativeRetentionPerformance", filters=filtro)
        curva = [(float(x), float(y)) for x, y, *_ in r.get("rows") or []]
        dados["retencao_curva"] = [[round(x, 3), round(y, 4)] for x, y in curva]
        if curva and dados["duracao_s"]:
            dados["retencao_3s"] = round(_interpolar(curva, min(3 / dados["duracao_s"], 1.0)), 4)
            dados["retencao_fim"] = round(curva[-1][1], 4)
            dados["retencao_relativa_media"] = round(sum(float(x[2]) for x in r["rows"]) / len(r["rows"]), 4) \
                if r["rows"] and len(r["rows"][0]) > 2 else None
    except Exception as e:
        dados["aviso_retencao"] = str(e)[:200]
    # De onde vieram as views (feed de Shorts, busca, externo...).
    try:
        r = _consulta(ya, startDate=publicado, endDate=hoje, dimensions="insightTrafficSourceType", metrics="views",
                      filters=filtro)
        dados["origens"] = {k: int(n) for k, n in r.get("rows") or []}
    except Exception as e:
        dados["aviso_origens"] = str(e)[:200]
    return dados


def atualizar(projeto_id: int | None = None) -> list[dict]:
    sql = "SELECT id, youtube_id FROM projetos WHERE youtube_id IS NOT NULL"
    params = ()
    if projeto_id:
        sql += " AND id = ?"
        params = (projeto_id,)
    resultados = []
    for p in db.todos(sql, params):
        try:
            dados = metricas_video(p["youtube_id"])
            db.executar("INSERT INTO metricas (projeto_id, youtube_id, atualizado_em, dados_json) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(projeto_id) DO UPDATE SET youtube_id = excluded.youtube_id, "
                        "atualizado_em = excluded.atualizado_em, dados_json = excluded.dados_json",
                        (p["id"], p["youtube_id"], db.agora(), json.dumps(dados, ensure_ascii=False)))
            resumo = {k: dados.get(k) for k in ("views", "media_percentual", "retencao_3s", "retencao_fim")}
            registro.escrever(p["id"], "YOUTUB", f"métricas de {p['youtube_id']}", dados=resumo)
            resultados.append({"projeto": p["id"], "ok": True, **resumo})
        except Exception as e:
            registro.escrever(p["id"], "YOUTUB", f"falha ao ler {p['youtube_id']}: {e}", "aviso")
            resultados.append({"projeto": p["id"], "ok": False, "erro": str(e)[:300]})
    return resultados


def painel() -> list[dict]:
    """Uma linha por projeto, com as notas do Jev e as métricas lado a lado."""
    linhas = []
    for p in db.todos("SELECT p.id, p.assunto, p.status, p.youtube_id, p.historia_json, p.avaliacao_json, "
                      "c.nome AS canal, (SELECT COALESCE(SUM(valor_usd),0) FROM custos WHERE projeto_id = p.id) AS custo "
                      "FROM projetos p JOIN canais c ON c.id = p.canal_id ORDER BY p.id DESC"):
        h = db.carregar_json(p.pop("historia_json"), {}) or {}
        aval = db.carregar_json(p.pop("avaliacao_json"), {}) or {}
        m = db.um("SELECT dados_json, atualizado_em FROM metricas WHERE projeto_id = ?", (p["id"],))
        dados = db.carregar_json(m["dados_json"], {}) if m else {}
        perg = aval.get("perguntas") or {}
        linhas.append({**p, "titulo": h.get("titulo"), "gancho": h.get("gancho"),
                       "nota_gancho": (perg.get("gancho") or {}).get("valor"), "nota_geral": aval.get("nota_geral"),
                       "metricas": dados or None})
    return linhas


def correlacao(linhas: list[dict], x: str = "nota_gancho", y: str = "retencao_3s") -> dict | None:
    pares = [(l[x], (l["metricas"] or {}).get(y)) for l in linhas if l.get(x) is not None and (l["metricas"] or {}).get(y) is not None]
    if len(pares) < 5:
        return {"n": len(pares), "r": None}
    n = len(pares)
    mx = sum(a for a, _ in pares) / n
    my = sum(b for _, b in pares) / n
    cov = sum((a - mx) * (b - my) for a, b in pares)
    vx = sum((a - mx) ** 2 for a, _ in pares) ** 0.5
    vy = sum((b - my) ** 2 for _, b in pares) ** 0.5
    return {"n": n, "r": round(cov / (vx * vy), 3) if vx and vy else None}
