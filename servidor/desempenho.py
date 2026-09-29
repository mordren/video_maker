"""Desempenho: views, retenção e origem do tráfego dos vídeos de cada canal do
Publicador, lidos da API oficial do YouTube (Data v3 + Analytics v2) — só
leitura, nada aqui publica nem mexe no canal.

O envio continua sendo pelo navegador (youtube_browser_upload.py); a API aqui
é só para ler números, e ler gasta pouquíssima cota (uns 5 pontos da Data API
por canal a cada atualização, de 10.000 por dia).

Login (uma vez por canal): o Publicador abre um Chromium na tela da sessão RDP
do servidor com a tela de consentimento do Google; o redirecionamento volta
para http://localhost:<porta> do próprio servidor (cliente OAuth do tipo "app
instalado" — os client_secret_*.json de sempre). Quando o canal já tem o perfil
do navegador logado (o mesmo do envio), o Google nem pede senha: é só escolher
o canal e autorizar.

Arquivos na pasta de dados do Publicador (PUBLICADOR_DATA):

    client_secret_<nome>.json          cliente OAuth do Google Cloud (um por projeto)
    desempenho_token_<canal>.json      autorização de leitura daquele canal
    desempenho_dados_<canal>.json      última leitura (a página mostra isto)
    desempenho_login_<canal>.json      andamento do login pela tela remota

Também roda sozinho, como processo à parte, para o login:

    python desempenho.py --login <canal> <cliente>
"""

from __future__ import annotations

import json
import os
import queue
import re
import sys
import threading
import time
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

# O redirecionamento do login volta em http://localhost; o oauthlib exige
# https fora disso, e o Google às vezes devolve os escopos em outra ordem.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

ESCOPOS = ["https://www.googleapis.com/auth/youtube.readonly",
           "https://www.googleapis.com/auth/yt-analytics.readonly"]

MAX_VIDEOS = 100              # últimos envios lidos por canal
MAX_RETENCOES = 30            # curvas de retenção relidas por atualização
DIAS_RETENCAO = 45            # só relê a curva de vídeos publicados há menos que isto
ATUALIZAR_A_CADA = timedelta(hours=6)
TEMPO_LOGIN_MIN = 15

_TRAVA = threading.Lock()
_ATUALIZANDO: set[str] = set()


class ErroDesempenho(Exception):
    pass


def _base() -> Path:
    return Path(os.environ.get("PUBLICADOR_DATA") or "/srv/publicador")


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _ler(caminho: Path) -> dict | None:
    try:
        return json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _gravar(caminho: Path, dados: dict, privado: bool = False) -> None:
    temporario = caminho.with_suffix(".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
    if privado:
        temporario.chmod(0o600)
    temporario.replace(caminho)


def arquivo_cliente(nome: str) -> Path:
    return _base() / f"client_secret_{nome}.json"


def arquivo_token(canal: str) -> Path:
    return _base() / f"desempenho_token_{canal}.json"


def arquivo_dados(canal: str) -> Path:
    return _base() / f"desempenho_dados_{canal}.json"


def arquivo_login(canal: str) -> Path:
    return _base() / f"desempenho_login_{canal}.json"


# ──────────────────────────────────────────────────────────────────
#  Clientes OAuth e login
# ──────────────────────────────────────────────────────────────────

def clientes() -> list[str]:
    """Nomes dos client_secret_<nome>.json presentes na pasta de dados."""
    return sorted(p.stem[len("client_secret_"):] for p in _base().glob("client_secret_*.json"))


def cliente_sugerido(canal: str) -> str:
    """O cliente mais provável deste canal: mesmo nome, depois prefixo
    (br → br_semfim), senão o primeiro. Só um palpite — a tela deixa trocar."""
    nomes = clientes()
    if canal in nomes:
        return canal
    prefixos = [n for n in nomes if canal.startswith(n) or n.startswith(canal)]
    return (prefixos or nomes or [""])[0]


def status_login(canal: str) -> dict | None:
    return _ler(arquivo_login(canal))


def login_em_andamento(canal: str) -> bool:
    """Há uma janela de login aberta para este canal agora? Enquanto houver,
    o perfil do navegador dele está em uso e o envio desse canal espera."""
    st = status_login(canal)
    if not st or st.get("estado") not in ("abrindo", "aguardando"):
        return False
    try:
        desde = datetime.fromisoformat(st["atualizado_em"])
    except (KeyError, ValueError):
        return False
    return datetime.now() - desde < timedelta(minutes=TEMPO_LOGIN_MIN + 2)


def gravar_status_login(canal: str, estado: str, mensagem: str = "") -> None:
    _gravar(arquivo_login(canal), {"estado": estado, "mensagem": mensagem, "atualizado_em": _agora()})


class _NavegadorDoPublicador(webbrowser.BaseBrowser):
    """Recebe a URL de consentimento do run_local_server em vez de abrir um
    navegador do sistema (o servidor não tem um) — quem abre é o Playwright."""

    def __init__(self, fila_urls: queue.Queue):
        super().__init__("publicador")
        self.fila_urls = fila_urls

    def open(self, url, new=0, autoraise=True):
        self.fila_urls.put(url)
        return True


def login(canal: str, cliente: str) -> None:
    """Abre o consentimento do Google num Chromium visível (DISPLAY da sessão
    RDP, passado pelo Publicador) e grava o token ao final. Roda como processo
    à parte; o andamento vai para desempenho_login_<canal>.json."""
    from google_auth_oauthlib.flow import InstalledAppFlow
    from playwright.sync_api import sync_playwright

    import youtube_browser_upload

    caminho_cliente = arquivo_cliente(cliente)
    if not caminho_cliente.exists():
        gravar_status_login(canal, "falhou", f"Falta o arquivo {caminho_cliente.name} na pasta de dados.")
        return
    gravar_status_login(canal, "abrindo", "Abrindo o navegador na tela remota…")

    fila_urls: queue.Queue = queue.Queue()
    webbrowser.register("publicador", None, _NavegadorDoPublicador(fila_urls))
    resultado: dict = {}

    def _esperar_retorno() -> None:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(str(caminho_cliente), ESCOPOS)
            resultado["cred"] = flow.run_local_server(
                host="localhost", port=0, browser="publicador",
                timeout_seconds=TEMPO_LOGIN_MIN * 60,
                authorization_prompt_message="",
                success_message="Pronto! O Publicador já pode ler as métricas. Esta janela fecha sozinha.",
                access_type="offline", prompt="consent")
        except Exception as erro:                                # noqa: BLE001
            resultado["erro"] = erro

    servidor = threading.Thread(target=_esperar_retorno, daemon=True)
    servidor.start()
    try:
        url = fila_urls.get(timeout=30)
    except queue.Empty:
        gravar_status_login(canal, "falhou", f"Não consegui gerar o link de login: {resultado.get('erro')}")
        return

    # O perfil do envio já está logado no Google dessa conta: com ele o
    # consentimento é só escolher o canal e autorizar. Sem perfil, um
    # temporário (aí o Google pede o login inteiro).
    perfil = youtube_browser_upload.profile_dir(canal)
    if not perfil.is_dir():
        perfil = _base() / f"desempenho_perfil_{canal}"
    try:
        with sync_playwright() as p:
            contexto = p.chromium.launch_persistent_context(
                user_data_dir=str(perfil), headless=False, locale="pt-BR",
                viewport={"width": 1100, "height": 860},
                args=youtube_browser_upload._ARGS_CHROMIUM)
            pagina = contexto.pages[0] if contexto.pages else contexto.new_page()
            pagina.goto(url, wait_until="domcontentloaded")
            gravar_status_login(canal, "aguardando",
                          "Navegador aberto na tela remota: escolha a conta/o canal "
                          f"'{canal}' e clique em Continuar/Permitir.")
            inicio = time.monotonic()
            while servidor.is_alive() and time.monotonic() - inicio < TEMPO_LOGIN_MIN * 60:
                time.sleep(1)
                if not contexto.pages:
                    break                                        # fecharam a janela
            if not servidor.is_alive():
                time.sleep(2)                                    # deixa ver a mensagem de pronto
            contexto.close()
    except Exception as erro:                                    # noqa: BLE001
        gravar_status_login(canal, "falhou", f"{type(erro).__name__}: {str(erro).strip()[:300]}")
        return

    cred = resultado.get("cred")
    if cred is None:
        erro = resultado.get("erro")
        gravar_status_login(canal, "falhou" if erro else "tempo esgotado",
                      f"O Google recusou: {erro}" if erro else
                      "A janela fechou (ou o tempo esgotou) antes de autorizar.")
        return
    _gravar(arquivo_token(canal), json.loads(cred.to_json()), privado=True)
    try:
        info = canal_youtube(canal)
        gravar_status_login(canal, "sucesso", f"Conectado ao canal do YouTube “{info['titulo']}”.")
    except Exception as erro:                                    # noqa: BLE001
        gravar_status_login(canal, "sucesso", f"Autorizado, mas a primeira leitura falhou: {_mensagem(erro)}")


def desconectar(canal: str) -> None:
    arquivo_token(canal).unlink(missing_ok=True)


def conectado(canal: str) -> bool:
    return arquivo_token(canal).exists()


# ──────────────────────────────────────────────────────────────────
#  API
# ──────────────────────────────────────────────────────────────────

def _mensagem(erro: BaseException) -> str:
    """Texto legível de um erro do Google (o HttpError traz o motivo num JSON)."""
    conteudo = getattr(erro, "content", None)
    if conteudo:
        try:
            return json.loads(conteudo)["error"]["message"][:500]
        except (ValueError, KeyError, TypeError):
            pass
    texto = str(erro)
    if "invalid_grant" in texto:
        return ("A autorização expirou ou foi revogada — clique em Conectar de novo. "
                "(Com o app do Google Cloud em modo \"Teste\", ela vale só 7 dias.)")
    return texto[:500]


def _credenciais(canal: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not conectado(canal):
        raise ErroDesempenho("Canal não conectado à API — clique em Conectar.")
    cred = Credentials.from_authorized_user_file(str(arquivo_token(canal)), ESCOPOS)
    if not cred.valid:
        if not cred.refresh_token:
            raise ErroDesempenho("A autorização expirou — clique em Conectar de novo.")
        try:
            cred.refresh(Request())
        except Exception as erro:                                # noqa: BLE001
            raise ErroDesempenho(_mensagem(erro)) from erro
        _gravar(arquivo_token(canal), json.loads(cred.to_json()), privado=True)
    return cred


def _servicos(canal: str):
    from googleapiclient.discovery import build

    cred = _credenciais(canal)
    return (build("youtube", "v3", credentials=cred, cache_discovery=False),
            build("youtubeAnalytics", "v2", credentials=cred, cache_discovery=False))


def canal_youtube(canal: str) -> dict:
    yt, _ = _servicos(canal)
    itens = yt.channels().list(part="snippet,statistics,contentDetails", mine=True).execute().get("items") or []
    if not itens:
        raise ErroDesempenho("A conta autorizada não tem canal do YouTube.")
    c = itens[0]
    est = c.get("statistics", {})
    return {"id": c["id"], "titulo": c["snippet"]["title"],
            "criado_em": c["snippet"]["publishedAt"][:10],
            "uploads": c["contentDetails"]["relatedPlaylists"]["uploads"],
            "inscritos": int(est.get("subscriberCount", 0)),
            "views_total": int(est.get("viewCount", 0)),
            "videos_total": int(est.get("videoCount", 0))}


def _segundos_iso(duracao: str) -> float:
    m = re.match(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duracao or "")
    if not m:
        return 0.0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


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


def _linhas(resposta: dict) -> list[dict]:
    nomes = [c["name"] for c in resposta.get("columnHeaders", [])]
    return [dict(zip(nomes, linha)) for linha in resposta.get("rows") or []]


def _consulta_com_engajadas(ya, metricas: str, **kw) -> tuple[list[dict], str | None]:
    """engagedViews só existe em alguns canais/regiões: tenta com, cai para sem."""
    try:
        return _linhas(_consulta(ya, metrics="engagedViews," + metricas, **kw)), None
    except Exception:                                            # noqa: BLE001
        try:
            return _linhas(_consulta(ya, metrics=metricas, **kw)), None
        except Exception as erro:                                # noqa: BLE001
            return [], _mensagem(erro)


def _videos_recentes(yt, playlist: str) -> list[dict]:
    ids, token = [], None
    while len(ids) < MAX_VIDEOS:
        r = yt.playlistItems().list(part="contentDetails", playlistId=playlist,
                                    maxResults=50, pageToken=token).execute()
        ids += [it["contentDetails"]["videoId"] for it in r.get("items", [])]
        token = r.get("nextPageToken")
        if not token:
            break
    videos = []
    for i in range(0, len(ids[:MAX_VIDEOS]), 50):
        r = yt.videos().list(part="snippet,statistics,contentDetails,status",
                             id=",".join(ids[i:i + 50])).execute()
        for v in r.get("items", []):
            est = v.get("statistics", {})
            miniaturas = v["snippet"].get("thumbnails", {})
            videos.append({
                "id": v["id"], "titulo": v["snippet"]["title"],
                "publicado_em": v["snippet"]["publishedAt"],
                "privacidade": v.get("status", {}).get("privacyStatus"),
                "duracao_s": _segundos_iso(v["contentDetails"]["duration"]),
                "miniatura": (miniaturas.get("default") or {}).get("url"),
                "views": int(est.get("viewCount", 0)), "likes": int(est.get("likeCount", 0)),
                "comentarios": int(est.get("commentCount", 0)),
            })
    videos.sort(key=lambda v: v["publicado_em"], reverse=True)
    return videos


def atualizar(canal: str, ids_do_publicador: set[str] | None = None) -> dict:
    """Lê tudo do canal e grava em desempenho_dados_<canal>.json."""
    with _TRAVA:
        if canal in _ATUALIZANDO:
            raise ErroDesempenho("Já está atualizando este canal.")
        _ATUALIZANDO.add(canal)
    try:
        return _atualizar(canal, ids_do_publicador or set())
    finally:
        with _TRAVA:
            _ATUALIZANDO.discard(canal)


def atualizando(canal: str) -> bool:
    return canal in _ATUALIZANDO


def _atualizar(canal: str, ids_do_publicador: set[str]) -> dict:
    try:
        yt, ya = _servicos(canal)
        info = canal_youtube(canal)
        videos = _videos_recentes(yt, info.pop("uploads"))
    except ErroDesempenho:
        raise
    except Exception as erro:                                    # noqa: BLE001
        raise ErroDesempenho(_mensagem(erro)) from erro

    anterior = _ler(arquivo_dados(canal)) or {}
    antigos = {v["id"]: v for v in anterior.get("videos", [])}
    hoje = date.today()
    avisos: list[str] = []

    # Totais do Analytics por vídeo (atrasam 2-3 dias em relação às views da Data API).
    for i in range(0, len(videos), 50):
        lote = videos[i:i + 50]
        linhas, aviso = _consulta_com_engajadas(
            ya, "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,"
                "likes,shares,subscribersGained",
            startDate=info["criado_em"], endDate=hoje.isoformat(), dimensions="video",
            filters="video==" + ",".join(v["id"] for v in lote), sort="-views", maxResults=50)
        if aviso:
            avisos.append(f"Totais por vídeo: {aviso}")
        por_id = {l["video"]: l for l in linhas}
        for v in lote:
            l = por_id.get(v["id"], {})
            v.update({"views_analytics": l.get("views"), "views_engajadas": l.get("engagedViews"),
                      "minutos": l.get("estimatedMinutesWatched"),
                      "media_visualizacao_s": l.get("averageViewDuration"),
                      "media_percentual": l.get("averageViewPercentage"),
                      "compartilhamentos": l.get("shares"),
                      "inscritos_ganhos": l.get("subscribersGained")})

    # Curva de retenção e origem, uma consulta por vídeo: só dos recentes (os
    # antigos quase não mudam — fica a última leitura guardada).
    relidos = 0
    for v in videos:
        velho = antigos.get(v["id"], {})
        for campo in ("retencao_curva", "retencao_3s", "retencao_fim", "origens", "retencao_lida_em"):
            if campo in velho:
                v[campo] = velho[campo]
        publicado = datetime.fromisoformat(v["publicado_em"].replace("Z", "+00:00")).date()
        idade = (hoje - publicado).days
        if idade < 2 or relidos >= MAX_RETENCOES:
            continue
        if idade > DIAS_RETENCAO and v.get("retencao_curva"):
            continue
        relidos += 1
        filtro = f"video=={v['id']}"
        try:
            r = _consulta(ya, startDate=publicado.isoformat(), endDate=hoje.isoformat(),
                          dimensions="elapsedVideoTimeRatio", metrics="audienceWatchRatio",
                          filters=filtro)
            curva = [(float(x), float(y)) for x, y in r.get("rows") or []]
            if curva:
                v["retencao_curva"] = [[round(x, 3), round(y, 4)] for x, y in curva]
                if v["duracao_s"]:
                    v["retencao_3s"] = round(_interpolar(curva, min(3 / v["duracao_s"], 1.0)), 4)
                v["retencao_fim"] = round(curva[-1][1], 4)
            r = _consulta(ya, startDate=publicado.isoformat(), endDate=hoje.isoformat(),
                          dimensions="insightTrafficSourceType", metrics="views", filters=filtro)
            v["origens"] = {k: int(n) for k, n in r.get("rows") or []}
            v["retencao_lida_em"] = _agora()
        except Exception as erro:                                # noqa: BLE001
            avisos.append(f"Retenção de {v['id']}: {_mensagem(erro)}")
            if "quota" in str(erro).lower():
                break

    for v in videos:
        v["via_publicador"] = v["id"] in ids_do_publicador

    # Canal nos últimos 28 dias: views por dia e de onde vieram.
    inicio = (hoje - timedelta(days=28)).isoformat()
    dias, aviso = _consulta_com_engajadas(
        ya, "views,estimatedMinutesWatched,subscribersGained,subscribersLost",
        startDate=inicio, endDate=hoje.isoformat(), dimensions="day", sort="day")
    if aviso:
        avisos.append(f"Views por dia: {aviso}")
    origens = {}
    try:
        r = _consulta(ya, startDate=inicio, endDate=hoje.isoformat(),
                      dimensions="insightTrafficSourceType", metrics="views")
        origens = {k: int(n) for k, n in r.get("rows") or []}
    except Exception as erro:                                    # noqa: BLE001
        avisos.append(f"Origem do tráfego: {_mensagem(erro)}")

    dados = {"canal": canal, "canal_youtube": info, "atualizado_em": _agora(),
             "periodo": {"inicio": inicio, "fim": hoje.isoformat()},
             "dias": [{"dia": d["day"], "views": d.get("views"), "engajadas": d.get("engagedViews"),
                       "minutos": d.get("estimatedMinutesWatched"),
                       "inscritos_ganhos": d.get("subscribersGained"),
                       "inscritos_perdidos": d.get("subscribersLost")} for d in dias],
             "origens": origens, "videos": videos, "avisos": avisos[:10]}
    _gravar(arquivo_dados(canal), dados)
    return dados


def dados(canal: str) -> dict | None:
    return _ler(arquivo_dados(canal))


# ──────────────────────────────────────────────────────────────────
#  Notas do JEV (Estúdio) × vídeos do canal
# ──────────────────────────────────────────────────────────────────

def _normalizar(texto: str) -> str:
    import unicodedata
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", texto).strip()


def anexar_notas(videos: list[dict], notas: list[dict], publicados: list[dict]) -> None:
    """Põe em cada vídeo a nota do corte que o originou (v["jev"]), pelo título.

    O envio pelo navegador não devolve o ID do vídeo, então o elo é o título:
    o Publicador usa o nome do arquivo como título no YouTube (a menos que
    tenha sido editado na fila — aí vale o que ficou em publicados.jsonl).
    Aceita prefixo porque o YouTube corta o título em 100 caracteres.
    """
    titulo_final = {p.get("arquivo"): p.get("titulo") for p in publicados}
    candidatos = []
    for nota in notas:
        if nota.get("nota_final") is None and nota.get("viral") is None:
            continue                     # corte antigo, de antes do log guardar as notas
        arquivo = nota.get("arquivo") or ""
        titulos = {_normalizar(Path(arquivo).stem)}
        if titulo_final.get(arquivo):
            titulos.add(_normalizar(titulo_final[arquivo]))
        candidatos.append(({t for t in titulos if len(t) >= 8}, nota))
    for video in videos:
        video.pop("jev", None)
        tv = _normalizar(video.get("titulo", ""))
        if len(tv) < 8:
            continue
        achados = [nota for titulos, nota in candidatos
                   if any(t == tv or (min(len(t), len(tv)) >= 25 and (tv.startswith(t) or t.startswith(tv)))
                          for t in titulos)]
        if achados:
            # O mesmo título em dois cortes (raro): o que foi enviado por último.
            achados.sort(key=lambda n: n.get("enviado_em") or "", reverse=True)
            video["jev"] = achados[0]


def ponto_de_nota(canal: str, video: dict) -> dict:
    jev = video["jev"]
    return {"canal": canal, "id": video["id"], "titulo": video["titulo"],
            "publicado_em": video["publicado_em"],
            **{k: jev.get(k) for k in ("nota_final", "viral", "ritmo", "llm", "formato")},
            **{k: video.get(k) for k in ("views", "views_engajadas", "retencao_3s",
                                         "media_percentual", "inscritos_ganhos")}}


def resumo(canal: str) -> dict:
    """O que a lista de canais da aba mostra, sem carregar os vídeos todos."""
    d = dados(canal) or {}
    dias = d.get("dias") or []
    return {"nome": canal, "conectado": conectado(canal), "atualizando": atualizando(canal),
            "cliente_sugerido": cliente_sugerido(canal), "login": status_login(canal),
            "canal_youtube": d.get("canal_youtube"), "atualizado_em": d.get("atualizado_em"),
            "views_28d": sum(x.get("views") or 0 for x in dias) if dias else None,
            "inscritos_28d": (sum((x.get("inscritos_ganhos") or 0) - (x.get("inscritos_perdidos") or 0)
                                  for x in dias) if dias else None),
            "erro": d.get("erro")}


def precisa_atualizar(canal: str) -> bool:
    if not conectado(canal) or atualizando(canal):
        return False
    d = dados(canal) or {}
    try:
        ultima = datetime.fromisoformat(d.get("tentado_em") or d.get("atualizado_em") or "")
    except ValueError:
        return True
    return datetime.now() - ultima >= ATUALIZAR_A_CADA


def marcar_falha(canal: str, erro: str) -> None:
    """Guarda o erro junto da última leitura boa (a página continua mostrando
    os números antigos, com o aviso por cima) e adia a próxima tentativa."""
    d = dados(canal) or {"canal": canal}
    d["erro"] = erro
    d["tentado_em"] = _agora()
    _gravar(arquivo_dados(canal), d)


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "--login":
        # Qualquer erro aqui tem que virar "falhou": um "abrindo" esquecido
        # seguraria o envio desse canal até o tempo do login estourar.
        try:
            login(sys.argv[2], sys.argv[3])
        except Exception as erro:                                # noqa: BLE001
            gravar_status_login(sys.argv[2], "falhou", f"{type(erro).__name__}: {str(erro)[:300]}")
    else:
        print("uso: python desempenho.py --login <canal> <cliente>")
