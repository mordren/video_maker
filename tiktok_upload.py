"""Envio dos vídeos da fila do TikTok (aba própria) e controle dessa fila.

O TikTok não tem uma API oficial de publicação acessível para uso comum (o
Content Posting API exige aprovação de app e fica limitado a "só você"
enquanto não passa por auditoria). Este módulo usa a biblioteca não-oficial
`tiktok-uploader` (https://github.com/wkaisertexas/tiktok-uploader): ela abre
um navegador de verdade (Playwright) e preenche a própria tela de upload do
tiktok.com, autenticado com os cookies de uma sessão já logada. Não há login
programático aqui dentro — a tela de login do TikTok tem captcha, então quem
loga é o usuário, no navegador, uma vez por conta.

Suporta várias contas ao mesmo tempo, do mesmo jeito que o YouTube: cada uma
tem um apelido escolhido pelo usuário, embutido no nome do arquivo dos
cookies exportados do navegador:

    tiktok_cookies_<apelido>.txt   (exportado do navegador, formato Netscape)

Esse arquivo é uma credencial de acesso à conta — fica de fora do git via
.gitignore, junto com a fila.

A fila em si mora em `tiktok_queue.txt`, no mesmo formato do
`youtube_queue.txt`: um vídeo por linha (caminho, apelido da conta e horário
de publicação agendado, separados por tab).

Sem Qt, para poder ser chamado de qualquer fluxo sem depender da interface.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

# "everyone" = público, "friends" = só seguidores, "only_you" = privado
# (visível só para o autor) — os três valores que a tela de upload do TikTok aceita.
DEFAULT_VISIBILITY = "everyone"


def _base_dir() -> Path:
    """Pasta onde ficam os cookies e a fila — a mesma regra do youtube_upload.py."""
    pasta = os.environ.get("VIDEOMAKER_DIR")
    return Path(pasta) if pasta else Path(__file__).parent


def _queue_path() -> Path:
    return _base_dir() / "tiktok_queue.txt"


_COOKIES_PREFIX = "tiktok_cookies_"
_COOKIES_SUFFIX = ".txt"


class TikTokUploadError(RuntimeError):
    """Falha ao autenticar ou enviar o vídeo."""


@dataclass
class QueueItem:
    """Um vídeo esperando na fila, na ordem em que deve ser enviado.

    `publish_at`, se marcado, vira o agendamento nativo do TikTok (a própria
    tela de upload tem essa opção, de 20 minutos a 10 dias no futuro) — o
    vídeo é enviado agora, mas só aparece publicamente na hora marcada.
    """
    path: Path
    account: str = ""
    publish_at: datetime | None = None


def list_accounts() -> list[str]:
    """Apelidos das contas configuradas (um por tiktok_cookies_<apelido>.txt)."""
    apelidos = []
    for arquivo in sorted(_base_dir().glob(f"{_COOKIES_PREFIX}*{_COOKIES_SUFFIX}")):
        apelido = arquivo.stem[len(_COOKIES_PREFIX):]
        if apelido:
            apelidos.append(apelido)
    return apelidos


def cookies_path(account: str) -> Path:
    return _base_dir() / f"{_COOKIES_PREFIX}{account}{_COOKIES_SUFFIX}"


def is_authorized(account: str) -> bool:
    """Já existem cookies importados para essa conta?"""
    return bool(account) and cookies_path(account).exists()


def import_cookies(account: str, source: Path) -> None:
    """Copia um cookies.txt exportado do navegador para a conta indicada.

    O usuário loga manualmente em tiktok.com no navegador e exporta os
    cookies com uma extensão (ex.: "Get cookies.txt"); esta função só copia
    o arquivo escolhido para o nome que este módulo espera encontrar.
    """
    if not account:
        raise TikTokUploadError("Escolha um apelido para a conta antes de importar os cookies.")
    if not source.exists():
        raise TikTokUploadError(f"Arquivo não encontrado: {source}")
    destino = cookies_path(account)
    destino.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destino)


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


def _dismiss_new_feature_popup(page) -> None:
    """Fecha o aviso "New editing features added" que o TikTok às vezes mostra
    na tela de edição logo depois do upload — ele cobre os botões de
    comentar/duetar/agendar e trava a automação esperando um clique que a
    biblioteca nunca dá, porque ela não conhece esse aviso (só sabe fechar o
    banner de cookies e a tela de "corte automático"). Roda antes dos passos
    que costumam travar por causa dele; se ele não estiver na tela, não faz
    nada (timeout curto, para não atrasar quem não tem o aviso).
    """
    try:
        page.get_by_text(re.compile("got it", re.IGNORECASE)).click(timeout=1500)
        return
    except Exception:
        pass
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass


ARGUMENTOS_DO_NAVEGADOR = [
    # O Chromium resolve DNS por conta própria, sem passar pelo sistema. Num
    # servidor onde esse resolvedor interno não funciona, ele mostra a tela de
    # "sem internet" (ERR_NETWORK_CHANGED, ERR_NAME_NOT_RESOLVED) mesmo com a
    # máquina resolvendo os mesmos domínios normalmente — medido no Debian da
    # rede, onde só esta opção fez o tiktok.com abrir.
    "--disable-features=AsyncDns,DnsOverHttps",
    # Acrescentado enquanto se caçava a instabilidade acima, que no fim era
    # IPv6 quebrado no servidor (veja o LEIA-ME). Pode não ser mais necessário:
    # ficou porque faz parte da combinação que passou a funcionar, e quem isola
    # este serviço é o systemd, não o sandbox do navegador. Para tirar, remova
    # esta linha e teste pelo botão 🧪 Testar TikTok.
    "--no-sandbox",
]

_navegador_ajustado = False


def _usar_dns_do_sistema() -> None:
    """Acrescenta os ajustes acima a toda abertura de navegador do Playwright.

    A tiktok-uploader monta a lista de argumentos do navegador por dentro e não
    deixa acrescentar nada, então o jeito de injetar é interceptar o `launch`
    do próprio Playwright — um ponto só, sem copiar o resto do que a lib faz.
    """
    global _navegador_ajustado
    if _navegador_ajustado:
        return
    from playwright.sync_api import BrowserType

    original = BrowserType.launch

    def launch(self, **kwargs):
        kwargs["args"] = list(kwargs.get("args") or []) + ARGUMENTOS_DO_NAVEGADOR
        return original(self, **kwargs)

    BrowserType.launch = launch
    _navegador_ajustado = True


_biblioteca_corrigida = False


def _preparar_biblioteca() -> None:
    """Ajusta a tiktok-uploader e o navegador antes do primeiro envio.

    A lib não expõe um jeito de rodar código entre os passos do upload, então
    o jeito é substituir (monkeypatch) as funções internas: as que travam por
    causa do aviso de novidades, e a do preenchimento — esta para resgatar o
    motivo real de uma falha, que ela engole. Roda só uma vez por processo; se
    a lib não estiver instalada, não faz nada (o erro de import aparece do
    jeito de sempre, mais na frente).
    """
    global _biblioteca_corrigida
    _usar_dns_do_sistema()
    if _biblioteca_corrigida:
        return
    try:
        import tiktok_uploader.upload as _lib
    except ImportError:
        return

    def _com_dispensa(original):
        def envolvida(page, *args, **kwargs):
            _dismiss_new_feature_popup(page)
            return original(page, *args, **kwargs)
        return envolvida

    # O passo de postar é o que mais sofre com o balão: é o último, e é onde o
    # aviso costuma estar aberto por cima do botão. Os de visibilidade e
    # agendamento só rodam quando se foge do padrão, então sem esta lista
    # completa quase nada dispensava o aviso antes do clique final.
    # São funções internas da lib: se uma sumir numa versão nova, é melhor
    # seguir sem o contorno dela do que derrubar o envio inteiro.
    for nome in ("_set_interactivity", "_set_description", "_set_schedule_video",
                 "_set_visibility", "_post_video"):
        passo = getattr(_lib, nome, None)
        if passo is not None:
            setattr(_lib, nome, _com_dispensa(passo))
    if getattr(_lib, "complete_upload_form", None) is not None:
        _lib.complete_upload_form = _com_diagnostico(_lib.complete_upload_form)
    _biblioteca_corrigida = True


# Motivo real da última falha. A biblioteca captura qualquer erro do envio,
# manda para um logger que ninguém escuta e devolve só "esse vídeo falhou" —
# sem isso, todo problema (login caído, captcha, botão fora do lugar) chega
# aqui com a mesma mensagem genérica, e não dá para saber o que corrigir.
ultimo_detalhe: str = ""

FALHA_PNG = "tiktok_falha.png"
FALHA_HTML = "tiktok_falha.html"


def _com_diagnostico(original):
    """Guarda o erro verdadeiro e um retrato da tela antes da lib engolir tudo."""
    def envolvida(page, *args, **kwargs):
        global ultimo_detalhe
        try:
            return original(page, *args, **kwargs)
        except Exception as erro:
            ultimo_detalhe = f"{type(erro).__name__}: {str(erro).strip()[:400]}"
            # Cada pedaço do diagnóstico é tentado por conta própria: um que
            # falhe não pode levar junto os outros — é tudo o que se tem para
            # entender o erro depois. E nenhum deles atrapalha o envio.
            try:
                ultimo_detalhe += f" [endereço da tela no erro: {page.url}]"
            except Exception:
                pass
            try:
                # O botão de postar fica no fim da página, e a tela do TikTok
                # rola por dentro (o "full_page" sozinho corta justamente essa
                # parte) — então desce até o fim antes de fotografar.
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(500)
            except Exception:
                pass
            try:
                page.screenshot(path=str(_base_dir() / FALHA_PNG), full_page=True)
            except Exception:
                pass
            try:
                (_base_dir() / FALHA_HTML).write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            raise
    return envolvida


def upload_video(
    video_path: Path,
    description: str,
    account: str,
    visibility: str = DEFAULT_VISIBILITY,
    publish_at: datetime | None = None,
    headless: bool = False,
) -> str:
    """Envia um vídeo pronto para o TikTok, na conta indicada.

    Ao contrário da API do YouTube, a tela de upload do TikTok não devolve um
    link do vídeo nem um progresso por pedaço — a biblioteca só confirma se o
    formulário foi enviado com sucesso, então o retorno aqui é uma mensagem,
    não uma URL. `headless=True` roda o navegador escondido, mas o TikTok
    detecta e bloqueia automação com mais facilidade nesse modo — deixe
    desligado a menos que precise mesmo.
    """
    if not account:
        raise TikTokUploadError("Escolha para qual conta enviar este vídeo.")
    cookies = cookies_path(account)
    if not cookies.exists():
        raise TikTokUploadError(
            f"A conta '{account}' ainda não tem cookies importados. Importe o "
            "cookies.txt exportado do navegador antes de enviar.")
    try:
        from tiktok_uploader import upload_video as _upload_video
    except ImportError as error:
        raise TikTokUploadError(
            "Biblioteca não instalada. Rode: pip install tiktok-uploader "
            "e depois: playwright install"
        ) from error
    _preparar_biblioteca()

    global ultimo_detalhe
    ultimo_detalhe = ""                            # não herdar o motivo de uma falha antiga

    schedule = None
    if publish_at:
        # A biblioteca quer um datetime "naive" (sem tzinfo) que ela mesma
        # assume como UTC — com tzinfo definido ela recusa o agendamento
        # ("Not naive datetime"). Por isso convertemos para UTC e então
        # tiramos o fuso, em vez de simplesmente anexar timezone.utc.
        if publish_at.tzinfo is None:
            publish_at = publish_at.astimezone()
        schedule = publish_at.astimezone(timezone.utc).replace(tzinfo=None)

    try:
        resultado = _upload_video(
            str(video_path),
            description=description or "",
            cookies=str(cookies),
            schedule=schedule,
            visibility=visibility,
            headless=headless,
            # Por padrão a biblioteca procura o Google Chrome de verdade
            # instalado no sistema (canal "chrome"), que existe no Windows mas
            # não no servidor Debian. O Chromium do próprio Playwright (que
            # "playwright install chromium" baixa, sem depender de nada do
            # sistema) funciona igual e em qualquer lugar.
            browser="chromium",
        )
    except Exception as error:                     # noqa: BLE001 — biblioteca não documenta um tipo único
        raise TikTokUploadError(f"O TikTok recusou o envio: {error}") from error

    # Versões diferentes da biblioteca devolvem coisas diferentes em caso de
    # sucesso (True, ou uma lista vazia de vídeos que falharam) — cobre as duas.
    sucesso = resultado if isinstance(resultado, bool) else not resultado
    if not sucesso:
        motivo = ultimo_detalhe or ("a tela de upload pode ter mudado, ou os "
                                     "cookies expiraram — tente reimportar o cookies.txt")
        raise TikTokUploadError(f"O envio não foi confirmado pelo TikTok. {motivo}")

    if publish_at:
        return f"Vídeo enviado e agendado para {publish_at.astimezone():%d/%m/%Y %H:%M} na conta '{account}'."
    return f"Vídeo publicado no TikTok (conta '{account}')."
