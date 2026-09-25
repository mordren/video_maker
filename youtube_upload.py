"""Envio dos vídeos da fila do YouTube (aba própria) e controle dessa fila.

Suporta várias contas/canais ao mesmo tempo: cada uma tem seu próprio
client_secret e seu próprio token, identificados por um "apelido" — o nome do
canal, escolhido pelo usuário — embutido no nome do arquivo:

    client_secret_<apelido>.json   (baixado do Google Cloud Console)
    youtube_token_<apelido>.json   (gerado por este módulo, após o login)

Usa OAuth 2.0 (fluxo "installed app"): o usuário autoriza uma vez pelo
navegador por conta, e o token de cada uma fica salvo para as próximas vezes.
Nenhum dos dois tipos de arquivo deve ir para o git — ambos ficam de fora via
.gitignore, porque cada um é uma credencial de acesso a uma conta do usuário.

A fila em si mora em `youtube_queue.txt`, um vídeo por linha (caminho,
apelido da conta e horário de publicação agendado, separados por tab) — é o
"controle simples" que a interface lê e reescreve a cada mudança. A ordem de
envio é a ordem da fila; quando ela deve rodar é controlado por um botão de
ligar/desligar na interface, não por horário de cada vídeo.

Sem Qt, para poder ser chamado de qualquer fluxo sem depender da interface.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]

# Categoria 25 = "Notícias e política", a mais comum para este tipo de corte.
DEFAULT_CATEGORY_ID = "25"

# Quantas quedas de conexão um mesmo envio aguenta antes de desistir. Como o
# upload é resumível, cada uma custa só o pedaço que estava indo — o que
# permite terminar um vídeo mesmo num link que oscila bastante.
MAX_QUEDAS_POR_ENVIO = 30


def _base_dir() -> Path:
    """Pasta onde ficam as credenciais e a fila.

    No Windows é a própria pasta do programa, como sempre. No servidor
    (Debian) o programa fica em /opt e os dados em /srv, então o serviço
    aponta VIDEOMAKER_DIR para a pasta de dados — o resto do módulo não
    precisa saber a diferença.
    """
    pasta = os.environ.get("VIDEOMAKER_DIR")
    return Path(pasta) if pasta else Path(__file__).parent


def _queue_path() -> Path:
    return _base_dir() / "youtube_queue.txt"


_SECRET_PREFIX = "client_secret_"
_SECRET_SUFFIX = ".json"
_TOKEN_PREFIX = "youtube_token_"


class YoutubeUploadError(RuntimeError):
    """Falha ao autenticar ou enviar o vídeo."""


@dataclass
class QueueItem:
    """Um vídeo esperando na fila, na ordem em que deve ser enviado.

    `publish_at`, se marcado, é quando o vídeo deve *ficar público* — o
    agendamento de verdade do YouTube: o vídeo sobe como privado e o próprio
    YouTube libera sozinho na hora marcada. Sem ele, o vídeo fica com a
    privacidade escolhida assim que o upload termina.

    Não há horário de *envio* por vídeo: a fila inteira só roda enquanto o
    botão da interface estiver em "Enviando" — dá tempo de importar e
    configurar vários vídeos antes de qualquer envio sair, e o usuário liga e
    desliga a fila quando quiser.
    """
    path: Path
    account: str = ""
    publish_at: datetime | None = None


def list_accounts() -> list[str]:
    """Apelidos das contas configuradas (um por client_secret_<apelido>.json)."""
    apelidos = []
    for arquivo in sorted(_base_dir().glob(f"{_SECRET_PREFIX}*{_SECRET_SUFFIX}")):
        apelido = arquivo.stem[len(_SECRET_PREFIX):]
        if apelido:
            apelidos.append(apelido)
    return apelidos


def find_client_secret(account: str) -> Path | None:
    """Caminho do client_secret da conta, se o arquivo existir."""
    path = _base_dir() / f"{_SECRET_PREFIX}{account}{_SECRET_SUFFIX}"
    return path if path.exists() else None


def token_path(account: str) -> Path:
    return _base_dir() / f"{_TOKEN_PREFIX}{account}.json"


def is_authorized(account: str) -> bool:
    """Já existe um token salvo para essa conta (login feito ao menos uma vez)?"""
    return bool(account) and token_path(account).exists()


def authorize(account: str) -> None:
    """Abre o navegador para o usuário logar e autorizar essa conta.

    Roda um servidor local (http://localhost) só durante o login, como
    configurado no client_secret de "Aplicativo de desktop". O token
    resultante é salvo para não pedir login de novo.
    """
    client_secret = find_client_secret(account)
    if not client_secret:
        raise YoutubeUploadError(
            f"client_secret_{account}.json não encontrado na pasta do "
            "programa.")
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as error:
        raise YoutubeUploadError(
            "Bibliotecas do Google não instaladas. Rode: pip install "
            "google-auth google-auth-oauthlib google-api-python-client"
        ) from error
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret), SCOPES)
    # Sem isso, o Google pula a tela de escolha quando o navegador já tem uma
    # sessão logada e autoriza direto com ela — o client_secret escolhe só o
    # app, não a conta. "select_account" força a tela toda vez, essencial
    # com várias contas/canais.
    credentials = flow.run_local_server(port=0, authorization_prompt_message="", prompt="select_account")
    token_path(account).write_text(credentials.to_json(), encoding="utf-8")


def _load_credentials(account: str):
    """Credenciais válidas da conta para chamar a API, renovando o token se preciso."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(account)
    if not path.exists():
        raise YoutubeUploadError(
            f"A conta '{account}' ainda não foi conectada. Selecione-a e "
            "clique em 'Conectar conta do YouTube' antes de enviar.")
    credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
        path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def load_queue() -> list[QueueItem]:
    """Lê a fila salva no .txt. Uma linha torta (editada à mão) é ignorada."""
    caminho = _queue_path()
    if not caminho.exists():
        return []
    itens: list[QueueItem] = []
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha:
            continue
        partes = linha.split("\t")
        if not partes[0]:
            continue
        conta = partes[1] if len(partes) > 1 else ""
        publicar_em = None
        if len(partes) > 2 and partes[2]:
            try:
                publicar_em = datetime.fromisoformat(partes[2])
            except ValueError:
                publicar_em = None
        itens.append(QueueItem(Path(partes[0]), conta, publicar_em))
    return itens


def save_queue(items: list[QueueItem]) -> None:
    """Reescreve o .txt inteiro com a fila atual (chamado a cada mudança)."""
    linhas = []
    for item in items:
        publicar = item.publish_at.isoformat(timespec="minutes") if item.publish_at else ""
        linhas.append(f"{item.path}\t{item.account}\t{publicar}")
    caminho = _queue_path()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text("\n".join(linhas), encoding="utf-8")


def upload_video(
    video_path: Path,
    title: str,
    account: str,
    description: str = "",
    tags: list[str] | None = None,
    privacy: str = "private",
    category_id: str = DEFAULT_CATEGORY_ID,
    publish_at: datetime | None = None,
    progress=None,
) -> str:
    """Envia um vídeo pronto para o YouTube, na conta indicada. Devolve a URL.

    `privacy` é "private", "unlisted" ou "public" — vale quando `publish_at`
    não é usado. Com `publish_at`, o YouTube exige o vídeo como privado até a
    hora marcada; ele mesmo libera na hora, então `privacy` é ignorado nesse
    caso. `progress(fraction)` é chamado a cada trecho enviado (0.0 a 1.0),
    se informado — o upload é resumível e feito em pedaços para vídeos
    grandes não travarem a memória.
    """
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    if not account:
        raise YoutubeUploadError("Escolha para qual conta enviar este vídeo.")
    credentials = _load_credentials(account)
    youtube = build("youtube", "v3", credentials=credentials)

    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": False,
    }
    if publish_at:
        # Agendamento nativo: só funciona com o vídeo privado; o YouTube
        # muda sozinho para público na hora marcada. publishAt exige RFC
        # 3339 em UTC — um datetime "naive" (sem fuso) é assumido como
        # horário local do computador e convertido por astimezone().
        if publish_at.tzinfo is None:
            publish_at = publish_at.astimezone()
        status["privacyStatus"] = "private"
        status["publishAt"] = publish_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    body = {
        "snippet": {
            "title": title[:100] or video_path.stem,
            "description": description,
            "tags": tags or [],
            "categoryId": category_id,
        },
        "status": status,
    }
    # Pedaços de 1 MB: numa rede ruim (Wi-Fi, repetidor), um pedaço grande que
    # cai no meio é 4 MB para refazer; um pequeno custa pouco para repetir.
    media = MediaFileUpload(str(video_path), chunksize=1024 * 1024, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    quedas = 0
    while response is None:
        try:
            # num_retries: a própria biblioteca repete o pedaço com espera
            # crescente quando o YouTube responde 5xx ou a conexão tropeça.
            status, response = request.next_chunk(num_retries=5)
        except HttpError as error:
            raise YoutubeUploadError(f"O YouTube recusou o envio: {error}") from error
        except Exception as error:                 # noqa: BLE001 — a rede caiu de vez
            # O envio é resumível: o `request` guarda em que ponto parou, então
            # insistir continua de onde estava em vez de recomeçar o vídeo.
            quedas += 1
            if quedas > MAX_QUEDAS_POR_ENVIO:
                raise YoutubeUploadError(
                    f"A conexão caiu {quedas} vezes durante o envio: {error}") from error
            time.sleep(min(5 * quedas, 60))
            continue
        quedas = 0
        if status and progress:
            progress(status.progress())

    video_id = response.get("id")
    if not video_id:
        raise YoutubeUploadError("O YouTube não devolveu o ID do vídeo enviado.")
    return f"https://youtu.be/{video_id}"
