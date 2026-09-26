"""Envio de vídeos ao YouTube pela tela real do Studio (Playwright), sem a API.

Por quê: a API de upload do YouTube tem uma cota diária apertada (~6 vídeos/dia)
e — pela experiência medida neste projeto — os vídeos enviados por ela quase não
eram entregues (a maioria ficava em 3-4 views). Este módulo contorna isso do
mesmo jeito que o `tiktok_upload.py` já faz com o TikTok: abre um navegador de
verdade (Playwright/Chromium), autenticado com os cookies de uma sessão já
logada, e preenche a própria tela de upload do `studio.youtube.com`. Como é um
navegador comum, não passa pela API — não há cota de 6/dia nem projeto a auditar.

Custo disso (mesmo trade-off do TikTok): automatizar o Studio contraria os
Termos de Serviço do YouTube, é frágil a mudanças da tela e exige manter o
cookies.txt fresco. Por isso todo passo tira uma "foto" da tela quando falha
(veja FALHA_PNG/FALHA_HTML), para depurar sem adivinhação.

Credenciais e fila seguem o padrão dos outros módulos, por apelido de conta:

    youtube_browser_cookies_<apelido>.txt   (exportado do navegador, Netscape)
    youtube_browser_queue.txt               (fila; caminho/conta/agendamento)

Sem Qt, para poder ser chamado de qualquer fluxo sem depender da interface.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# Visibilidade aceita pela tela do Studio quando NÃO há agendamento.
DEFAULT_VISIBILITY = "private"
_VISIBILITY_RADIO = {
    "public": "PUBLIC",
    "unlisted": "UNLISTED",
    "private": "PRIVATE",
}

# Espera padrão para elementos da tela (ms). O Studio é lento para montar cada
# etapa do diálogo; um teto curto derruba envios que só estavam carregando.
_ESPERA = 60_000

# Teto de espera pro upload do arquivo e pras verificações iniciais (direitos
# autorais / diretrizes da comunidade) do YouTube. Vídeo grande ou fila cheia
# no YouTube podem levar bem mais que os poucos minutos do caso comum — é
# melhor esperar bastante do que desistir cedo e travar a etapa seguinte com
# o envio ou as checagens ainda em andamento.
_ESPERA_ENVIO = 60 * 60_000        # 1 hora
_ESPERA_VERIFICACAO = 60 * 60_000  # 1 hora

FALHA_PNG = "youtube_browser_falha.png"
FALHA_HTML = "youtube_browser_falha.html"


class YoutubeBrowserUploadError(RuntimeError):
    """Falha ao autenticar ou enviar o vídeo pela tela do Studio."""


def _emit(log, msg: str) -> None:
    """Manda uma linha pro log da fila, se alguém estiver ouvindo.

    Usado em cada passo do envio (qual seletor achou o botão, qual texto foi
    digitado, qual link foi capturado etc.) para dar visibilidade total do
    que o bot está fazendo na tela, sem precisar adivinhar pelo print.
    """
    if log:
        log(msg)


# ── Pasta, contas, cookies e fila (mesmo padrão dos outros módulos) ──────────

def _base_dir() -> Path:
    pasta = os.environ.get("VIDEOMAKER_DIR")
    return Path(pasta) if pasta else Path(__file__).parent


def _queue_path() -> Path:
    return _base_dir() / "youtube_browser_queue.txt"


_COOKIES_PREFIX = "youtube_browser_cookies_"
_COOKIES_SUFFIX = ".txt"
_PERFIL_PREFIX = "youtube_browser_perfil_"

# Argumentos do Chromium usados tanto no login manual quanto no envio
# automático — mesma janela, mesmo comportamento.
_ARGS_CHROMIUM = [
    "--disable-blink-features=AutomationControlled",
    # Sem isso, a janela do Chromium abre e fica com a tela toda preta e
    # travada no Windows (medido rodando de dentro do app Qt/VLC, que já
    # desativa decodificação de vídeo por hardware — os dois parecem disputar
    # a mesma GPU). Desliga a aceleração só deste navegador automatizado; não
    # afeta o app em si.
    "--disable-gpu",
    "--disable-gpu-compositing",
    "--disable-software-rasterizer",
    # Mesmo ajuste que o tiktok_upload.py precisou no servidor: o resolvedor
    # de DNS embutido do Chromium falha nessas máquinas (ERR_NETWORK_CHANGED/
    # ERR_NAME_NOT_RESOLVED) mesmo com o sistema resolvendo os mesmos
    # domínios normalmente. Inofensivo — só faz o Chromium usar o DNS do
    # sistema.
    "--disable-features=AsyncDns,DnsOverHttps",
    # Idem: parte da combinação que fez o Chromium rodar estável no servidor
    # (veja o LEIA-ME de servidor/). Quem isola este serviço lá é o systemd,
    # não o sandbox do navegador.
    "--no-sandbox",
]


@dataclass
class QueueItem:
    """Um vídeo esperando na fila, na ordem em que deve ser enviado.

    `publish_at`, se marcado, vira o agendamento nativo do Studio: o vídeo sobe
    como privado e o YouTube o torna público sozinho na hora marcada. Sem ele, a
    visibilidade escolhida vale assim que o processamento termina.
    """
    path: Path
    account: str = ""
    publish_at: datetime | None = None


def list_accounts() -> list[str]:
    """Apelidos das contas: um por cookies.txt importado OU por perfil logado
    manualmente (veja `login_manual`)."""
    apelidos = set()
    for arquivo in _base_dir().glob(f"{_COOKIES_PREFIX}*{_COOKIES_SUFFIX}"):
        apelido = arquivo.stem[len(_COOKIES_PREFIX):]
        if apelido:
            apelidos.add(apelido)
    for pasta in _base_dir().glob(f"{_PERFIL_PREFIX}*"):
        if pasta.is_dir():
            apelidos.add(pasta.name[len(_PERFIL_PREFIX):])
    return sorted(apelidos)


def cookies_path(account: str) -> Path:
    return _base_dir() / f"{_COOKIES_PREFIX}{account}{_COOKIES_SUFFIX}"


def profile_dir(account: str) -> Path:
    """Perfil persistente do Chromium (cookies, localStorage, IndexedDB) desta
    conta — criado por `login_manual`. Preferido sobre `cookies_path`: o
    Google gira cookies de segurança (ex.: __Secure-1PSIDTS) a cada visita
    autenticada, e um cookies.txt é só uma foto congelada que sempre acaba
    ficando pra trás. Com um perfil de verdade sendo reaberto sempre, essas
    trocas acontecem e ficam salvas sozinhas, do jeito que um navegador usado
    de verdade se comporta — sem precisar reexportar nada."""
    return _base_dir() / f"{_PERFIL_PREFIX}{account}"


def is_authorized(account: str) -> bool:
    """Já existem credenciais para essa conta (perfil persistente ou cookies.txt)?"""
    return bool(account) and (profile_dir(account).is_dir() or cookies_path(account).exists())


def import_cookies(account: str, source: Path) -> None:
    """Copia um cookies.txt exportado do navegador para o nome esperado.

    O usuário loga em studio.youtube.com (no canal certo) e exporta os cookies
    com uma extensão (ex.: "Get cookies.txt LOCALLY"); esta função só renomeia/
    copia o arquivo escolhido para o padrão que este módulo procura.
    """
    if not account:
        raise YoutubeBrowserUploadError("Escolha um apelido para a conta antes de importar os cookies.")
    if not source.exists():
        raise YoutubeBrowserUploadError(f"Arquivo não encontrado: {source}")
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


# ── Cookies Netscape → Playwright ────────────────────────────────────────────

def _parse_netscape_cookies(text: str) -> list[dict]:
    """Converte um cookies.txt (formato Netscape) para o formato do Playwright.

    O formato tem 7 campos separados por TAB: domínio, flag de subdomínio,
    caminho, secure, expiração, nome, valor. Linhas de comentário começam com
    '#', exceto o prefixo '#HttpOnly_' que marca um cookie httpOnly de verdade.
    """
    cookies: list[dict] = []
    for line in text.splitlines():
        line = line.rstrip("\n")
        http_only = False
        if line.startswith("#HttpOnly_"):
            http_only = True
            line = line[len("#HttpOnly_"):]
        elif not line.strip() or line.lstrip().startswith("#"):
            continue
        partes = line.split("\t")
        if len(partes) < 7:
            continue
        domain, _include_sub, path, secure, expires, name, value = partes[:7]
        cookie = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "httpOnly": http_only,
            "secure": secure.strip().upper() == "TRUE",
            # O YouTube usa cookies cross-site (SAPISID etc.); "None" preserva o
            # comportamento original melhor que "Lax" para uma sessão logada.
            "sameSite": "None" if secure.strip().upper() == "TRUE" else "Lax",
        }
        try:
            exp = int(float(expires))
            if exp > 0:
                cookie["expires"] = exp
        except ValueError:
            pass
        cookies.append(cookie)
    if not cookies:
        raise YoutubeBrowserUploadError(
            "Nenhum cookie válido no arquivo. Exporte um cookies.txt no formato "
            "Netscape, logado em studio.youtube.com.")
    return cookies


def _cookies_para_netscape(cookies: list[dict]) -> str:
    """Converte cookies do formato do Playwright de volta pro Netscape.

    Usado pra regravar o cookies.txt com os valores mais recentes depois de
    um login confirmado — o Google renova sozinho alguns cookies de sessão
    enquanto o navegador fica aberto (proteção contra roubo/replay de
    cookie), e um cookies.txt nunca atualizado tende a vencer mais cedo,
    principalmente com o bot esperando minutos a mais de uma hora (envio +
    verificações). Espelha exatamente os 7 campos que `_parse_netscape_cookies`
    lê de volta.
    """
    linhas = ["# Netscape HTTP Cookie File",
              "# Regravado automaticamente pelo youtube_browser_upload.py após um login confirmado."]
    for cookie in cookies:
        domain = cookie.get("domain", "")
        http_only = cookie.get("httpOnly", False)
        campo_dominio = f"#HttpOnly_{domain}" if http_only else domain
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        path = cookie.get("path") or "/"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = cookie.get("expires")
        expires_txt = str(int(expires)) if expires and expires > 0 else "0"
        linhas.append("\t".join([
            campo_dominio, include_sub, path, secure, expires_txt,
            cookie.get("name", ""), cookie.get("value", ""),
        ]))
    return "\n".join(linhas) + "\n"


def _salvar_cookies_atualizados(context, cookies_file: Path, log=None) -> None:
    """Regrava o cookies.txt com os cookies atuais da sessão logada.

    Best-effort: se der errado, não atrapalha o envio (os cookies antigos
    continuam válidos até vencerem de verdade) — só não fica mais fresco
    pra próxima vez.
    """
    try:
        atuais = context.cookies()
        cookies_file.write_text(_cookies_para_netscape(atuais), encoding="utf-8")
        _emit(log, "Cookies da sessão salvos de volta no arquivo (login deve durar mais na próxima vez).")
    except Exception:
        pass


def login_manual(account: str) -> None:
    """Abre um Chromium de verdade, com janela, para logar uma vez à mão —
    depois disso o envio (`upload_video`) reaproveita esse mesmo perfil
    persistente sozinho, sem cookies.txt e sem precisar reexportar nada.

    Rode isto direto no desktop da máquina (não por SSH/Xvfb): a tela precisa
    aparecer de verdade para você digitar a senha e passar por qualquer
    verificação em duas etapas.

        python youtube_browser_upload.py --login <apelido-da-conta>
    """
    from playwright.sync_api import sync_playwright

    pasta = profile_dir(account)
    pasta.mkdir(parents=True, exist_ok=True)
    print(f"Abrindo o Chromium para logar a conta '{account}'…")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(pasta), headless=False,
            locale="pt-BR", viewport={"width": 1366, "height": 900},
            args=_ARGS_CHROMIUM)
        page = context.new_page()
        page.goto("https://studio.youtube.com/", wait_until="domcontentloaded")
        print("Faça login normalmente na janela que abriu (email, senha, 2FA se pedir).")
        input("Depois que o YouTube Studio carregar logado, volte aqui e aperte Enter... ")
        context.close()
    print(f"Perfil salvo em {pasta}. A conta '{account}' já pode ser usada nos envios.")


# ── Diagnóstico ──────────────────────────────────────────────────────────────

def _retrato_da_tela(page, etapa: str) -> str:
    """Salva foto + HTML da tela para depurar uma falha. Devolve texto de dica."""
    detalhe = f"etapa: {etapa}"
    try:
        detalhe += f" [endereço: {page.url}]"
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
    return detalhe


# ── Passos da tela do Studio ─────────────────────────────────────────────────

def _dispensar_consentimento(page, log=None) -> None:
    """Fecha a tela de consentimento de cookies do Google, se aparecer."""
    for texto in ("Aceitar tudo", "Accept all", "Rejeitar tudo", "Reject all",
                  "Concordo", "I agree"):
        try:
            page.get_by_role("button", name=re.compile(texto, re.IGNORECASE)).click(timeout=2000)
            _emit(log, f"Fechei o aviso de cookies do Google (botão '{texto}').")
            return
        except Exception:
            continue


def _clicar_criar(page, log=None) -> None:
    """Clica no botão "Criar" do topo do Studio.

    O Studio já trocou o id desse botão pelo menos uma vez (de "#create-icon"
    para uma classe "ytcpAppHeaderCreateIcon", medido em 24/09/2026 — um
    envio real travou 60s esperando o id antigo, que sumiu do HTML). Por
    isso tenta várias formas de achar o mesmo botão, terminando no texto
    (que resiste a mudança de id/classe) em vez de confiar só num seletor.
    """
    seletores = (
        "#create-icon",
        "ytcp-button.ytcpAppHeaderCreateIcon",
        "button[aria-label='Criar']",
        "button[aria-label='Create']",
    )
    for seletor in seletores:
        try:
            page.locator(seletor).first.click(timeout=8000)
            _emit(log, f"Cliquei em 'Criar' (seletor: {seletor}).")
            return
        except Exception:
            continue
    page.get_by_role(
        "button", name=re.compile(r"^\s*(Criar|Create)\s*$", re.IGNORECASE)
    ).first.click(timeout=_ESPERA)
    _emit(log, "Cliquei em 'Criar' (achado pelo texto do botão — nenhum seletor conhecido bateu).")


def _clicar_enviar_videos(page, log=None) -> None:
    """Clica no item "Enviar vídeos" do menu aberto pelo botão Criar."""
    for seletor in ("#text-item-0", "tp-yt-paper-item#text-item-0"):
        try:
            page.locator(seletor).first.click(timeout=8000)
            _emit(log, f"Cliquei em 'Enviar vídeos' (seletor: {seletor}).")
            return
        except Exception:
            continue
    page.get_by_text(
        re.compile(r"Enviar v[ií]deos|Upload videos", re.IGNORECASE)
    ).first.click(timeout=_ESPERA)
    _emit(log, "Cliquei em 'Enviar vídeos' (achado pelo texto do menu — nenhum seletor conhecido bateu).")


def _abrir_upload(page, video_path: Path, log=None) -> None:
    """Clica em Criar → Enviar vídeos e entrega o arquivo ao seletor nativo.

    O clique em "Enviar vídeos" não abre mais o seletor de arquivo direto:
    agora ele só abre a caixa de diálogo com a área de arrastar-e-soltar, que
    tem um botão "Selecionar arquivos" próprio — é esse botão que dispara o
    evento de file chooser (medido em 24/09/2026, depois que o clique direto
    passou a travar 60s esperando um evento que nunca vinha).
    """
    _clicar_criar(page, log=log)
    _clicar_enviar_videos(page, log=log)
    _emit(log, "Aguardando a caixa de seleção de arquivo abrir…")
    with page.expect_file_chooser(timeout=_ESPERA) as fc:
        page.locator("#select-files-button").first.click(timeout=_ESPERA)
        _emit(log, "Cliquei em 'Selecionar arquivos'.")
    fc.value.set_files(str(video_path))
    tamanho_mb = video_path.stat().st_size / 1_048_576
    _emit(log, f"Arquivo entregue ao Studio: {video_path.name} ({tamanho_mb:.1f} MB).")


def _preencher_texto(page, seletor: str, valor: str, log=None, nome_campo: str = "") -> None:
    """Limpa e digita num campo contenteditable do Studio (título/descrição)."""
    campo = page.locator(seletor).first
    campo.wait_for(state="visible", timeout=_ESPERA)
    campo.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    if valor:
        campo.type(valor, delay=8)
    if nome_campo:
        if valor:
            previa = valor[:60] + ("…" if len(valor) > 60 else "")
            _emit(log, f"Preenchi {nome_campo}: \"{previa}\"")
        else:
            _emit(log, f"{nome_campo} deixado em branco (nada pra preencher).")


def _marcar_nao_para_criancas(page, log=None) -> None:
    page.locator('tp-yt-paper-radio-button[name="VIDEO_MADE_FOR_KIDS_NOT_MFK"]').click(timeout=_ESPERA)
    _emit(log, "Marquei 'Não é conteúdo para crianças'.")


# Ícone de "check" (✓) que o Studio mostra em cada linha de verificação
# ("Direitos autorais", "Diretrizes da comunidade") quando ela termina sem
# problema. É o glifo padrão do Material Symbols para "check" — mesmo path
# em qualquer idioma, ao contrário de procurar por texto.
_ICONE_CHECK = (
    'svg path[d="M19.793 5.793 8.5 17.086l-4.293-4.293a1 1 0 10-1.414 '
    '1.414L8.5 19.914 21.207 7.207a1 1 0 10-1.414-1.414Z"]'
)

# Cada verificação (direitos autorais, diretrizes da comunidade) é um
# <ytcp-uploads-check-status> próprio — contar checks na página inteira sem
# esse escopo pegava também um resumo escondido no topo da tela (um ícone
# de aviso com balão, componente diferente) e dava um número inflado que
# nunca batia com "todas prontas" de verdade (medido em 24/09/2026: achou 4
# checks com só 1 das 2 verificações realmente concluída).
_LINHA_VERIFICACAO = "ytcp-uploads-check-status"


def _aguardar_verificacoes_concluidas(page, teto_ms: int, log=None) -> None:
    """Espera todas as verificações iniciais (direitos autorais, diretrizes
    da comunidade) terminarem de verdade — escaneia cada linha de
    verificação individualmente e só libera quando TODAS mostrarem o ícone
    de check, o sinal positivo de que concluíram.

    Esperar só o aviso "N minutos restantes" sumir não é seguro: antes das
    verificações começarem (enquanto o processamento da definição padrão do
    vídeo ainda não terminou), esse aviso também não existe na tela — então
    "sumido" tanto pode significar "ainda nem começou" quanto "já
    terminou". E contar ícones de check na página inteira (sem escopo por
    linha) também não é seguro, pelo motivo explicado acima em
    `_LINHA_VERIFICACAO`. Por isso a espera aqui é: para cada linha de
    verificação encontrada, ela só conta como pronta se o check aparecer
    dentro DELA — e só libera quando não sobrar nenhuma linha pendente.
    """
    inicio = time.monotonic()
    ultimo = None
    while (time.monotonic() - inicio) * 1000 < teto_ms:
        linhas = page.locator(_LINHA_VERIFICACAO)
        try:
            total = linhas.count()
        except Exception:
            total = 0
        if total > 0:
            prontas = 0
            pendentes = []
            for i in range(total):
                linha = linhas.nth(i)
                try:
                    tem_check = linha.locator(_ICONE_CHECK).count() > 0
                except Exception:
                    tem_check = False
                if tem_check:
                    prontas += 1
                else:
                    try:
                        texto = linha.inner_text(timeout=2000).strip().replace("\n", " ")
                    except Exception:
                        texto = "?"
                    pendentes.append(texto)
            if prontas == total:
                _emit(log, f"Verificações concluídas: {prontas}/{total} linha(s) com check.")
                return
            status = f"{prontas}/{total} concluídas — pendente: {' | '.join(pendentes) or '?'}"
        else:
            status = "aguardando a etapa de verificação montar na tela"
        if status != ultimo:
            _emit(log, f"Verificando: {status}")
            ultimo = status
        page.wait_for_timeout(4000)
    raise YoutubeBrowserUploadError(
        "As verificações do YouTube (direitos autorais / diretrizes da "
        "comunidade) não terminaram a tempo.")


_LINK_DO_VIDEO = 'a.ytcp-video-info, .video-url-fadeable a, a[href*="youtu"]'
_PERCENTUAL = re.compile(r"\d+\s*%")


def _aguardar_envio_completo(page, teto_ms: int, log=None) -> None:
    """Espera o upload do arquivo terminar.

    Ao contrário do aviso de verificações (que some da tela quando termina),
    o rótulo "Envio em X% ... Tempo restante: Ys" pode continuar visível —
    só que com outro texto — mesmo depois do upload já ter acabado. Esperar
    só ele sumir da tela prendeu uma espera real até o teto de 1h mesmo com
    o vídeo já processado e o link pronto no painel da direita (medido em
    24/09/2026). Por isso considera terminado assim que o texto deixar de
    ter um percentual OU o link do vídeo já aparecer no painel — o que vier
    primeiro.
    """
    inicio = time.monotonic()
    ultimo = None
    while (time.monotonic() - inicio) * 1000 < teto_ms:
        try:
            if page.locator(_LINK_DO_VIDEO).first.is_visible():
                _emit(log, "Link do vídeo já apareceu no painel — considerando o envio concluído.")
                return
        except Exception:
            pass
        rotulo = page.locator("span.progress-label").first
        try:
            visivel = rotulo.is_visible()
        except Exception:
            visivel = False
        if not visivel:
            _emit(log, "Rótulo de progresso do envio não está mais visível — considerando concluído.")
            return
        try:
            texto = rotulo.inner_text(timeout=2000).strip()
        except Exception:
            _emit(log, "Não consegui ler o rótulo de progresso — considerando o envio concluído.")
            return
        if not _PERCENTUAL.search(texto):
            _emit(log, f"Rótulo de progresso mudou de assunto (\"{texto}\") — considerando o envio concluído.")
            return
        if texto != ultimo:
            _emit(log, f"Enviando: {texto}")
            ultimo = texto
        page.wait_for_timeout(3000)


def _proximo(page, log=None, de: str = "", para: str = "") -> None:
    """Avança uma etapa do diálogo (Detalhes → Elementos → Verificações → Visib.)."""
    botao = page.locator("#next-button")
    botao.wait_for(state="visible", timeout=_ESPERA)
    botao.click()
    page.wait_for_timeout(1200)
    if de and para:
        _emit(log, f"Avancei de '{de}' para '{para}'.")


def _definir_visibilidade(page, visibility: str, log=None) -> None:
    nome = _VISIBILITY_RADIO.get(visibility, "PRIVATE")
    page.locator(f'tp-yt-paper-radio-button[name="{nome}"]').click(timeout=_ESPERA)
    _emit(log, f"Selecionei a visibilidade '{nome}' (pedido: '{visibility}').")


def _agendar(page, quando: datetime, log=None) -> None:
    """Abre a seção de agendamento e define data/hora de publicação.

    Estratégia resiliente: abre o seletor de agendamento (por texto, para não
    depender de id que muda), garante a data e então preenche a hora. Para +1h a
    data quase sempre já é a de hoje, então a hora é o que importa.
    """
    # Abre a seção "Agendar" (fica recolhida por padrão na etapa de visibilidade).
    try:
        page.get_by_text(re.compile(r"^\s*(Agendar|Schedule)\s*$", re.IGNORECASE)).first.click(timeout=8000)
        _emit(log, "Abri a seção 'Agendar' (pelo texto).")
    except Exception:
        # Fallback: alguns layouts usam um radio dedicado de agendamento.
        page.locator('tp-yt-paper-radio-button[name="SCHEDULE"]').click(timeout=8000)
        _emit(log, "Abri a seção 'Agendar' (pelo radio dedicado).")
    page.wait_for_timeout(800)

    # Data: abre o calendário e escolhe o dia (só se for diferente de hoje).
    try:
        page.locator("#datepicker-trigger").click(timeout=8000)
        page.wait_for_timeout(500)
        # O calendário do Studio marca cada dia com aria-label contendo a data;
        # clicar pelo número do dia visível é suficiente para o mês corrente.
        dia = str(quando.day)
        page.locator(
            f'div.ytcp-date-picker[aria-disabled="false"]:has-text("{dia}")'
        ).filter(has_text=re.compile(rf"^\s*{dia}\s*$")).first.click(timeout=5000)
        _emit(log, f"Selecionei o dia {dia} no calendário.")
    except Exception:
        # Se a data já for hoje (caso comum de +1h), o calendário pode nem ser
        # necessário — seguimos para a hora e deixamos a data no padrão.
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        _emit(log, "Calendário não abriu (ou não precisou) — mantendo a data padrão.")

    # Hora: o campo aceita texto no formato local (pt-BR = 24h "HH:MM").
    hora_txt = quando.strftime("%H:%M")
    campo_hora = page.locator(
        '#time-of-day-container input, ytcp-datetime-picker input[aria-label*="ora"], '
        'ytcp-datetime-picker input[aria-label*="ime"]'
    ).first
    campo_hora.wait_for(state="visible", timeout=8000)
    campo_hora.click()
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    campo_hora.type(hora_txt, delay=20)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    _emit(log, f"Preenchi o horário de agendamento: {hora_txt}.")


def _enviar_legenda(page, video_id: str, legenda_path: Path, idioma: str = "pt", log=None) -> None:
    """Sobe um .srt como legenda de verdade do vídeo (tela separada do
    assistente de upload — /video/<id>/translations), não só o texto que já
    vai queimado na imagem.

    Best-effort e isolado do resto do envio: se a tela do Studio mudar aqui,
    o vídeo já publicado continua valendo, só sem essa legenda (quem chama
    trata a exceção). Uma segunda aba evita mexer na aba principal, cujo
    estado (diálogo de confirmação etc.) o resto do fluxo ainda pode usar.
    """
    pagina = page.context.new_page()
    try:
        pagina.set_default_timeout(_ESPERA)
        pagina.goto(f"https://studio.youtube.com/video/{video_id}/translations",
                    wait_until="domcontentloaded", timeout=_ESPERA)
        _emit(log, "Abri a tela de legendas.")

        # Se o idioma já existir na lista (ex.: legenda automática do YouTube),
        # o link de ação é "ADICIONAR"/"Add" na linha dele; senão, primeiro
        # precisa adicionar o idioma pelo botão geral. Os dois casos abrem o
        # mesmo menu de origem da legenda.
        linha_idioma = pagina.get_by_role("row", name=re.compile(idioma, re.IGNORECASE)).first
        try:
            linha_idioma.get_by_role("button", name=re.compile("adicionar|add", re.IGNORECASE)).click(timeout=6000)
        except Exception:
            pagina.get_by_role("button", name=re.compile("adicionar idioma|add language", re.IGNORECASE)).click(timeout=8000)
            pagina.get_by_text(re.compile(f"^{idioma}$", re.IGNORECASE)).first.click(timeout=8000)
            pagina.wait_for_timeout(500)
            linha_idioma = pagina.get_by_role("row", name=re.compile(idioma, re.IGNORECASE)).first
            linha_idioma.get_by_role("button", name=re.compile("adicionar|add", re.IGNORECASE)).click(timeout=6000)

        # Menu com as origens da legenda: upload de arquivo, sincronização
        # automática ou digitar na mão — escolhe "fazer upload de arquivo".
        pagina.get_by_text(re.compile("upload.*arquivo|upload file", re.IGNORECASE)).first.click(timeout=8000)
        # Segunda tela: "com timing" (já tem os tempos, é o nosso caso do
        # .srt) vs "sem timing" (só o texto corrido).
        try:
            pagina.get_by_text(re.compile("com.*tempo|with timing", re.IGNORECASE)).first.click(timeout=5000)
        except Exception:
            pass  # algumas contas pulam direto pro seletor de arquivo

        with pagina.expect_file_chooser(timeout=8000) as escolhedor:
            pagina.get_by_text(re.compile("selecionar arquivo|browse", re.IGNORECASE)).first.click(timeout=8000)
        escolhedor.value.set_files(str(legenda_path))
        _emit(log, f"Arquivo de legenda selecionado: {legenda_path.name}.")

        pagina.get_by_role("button", name=re.compile("^publicar$|^publish$|^salvar$|^save$", re.IGNORECASE)).click(timeout=15_000)
        pagina.wait_for_timeout(2000)
        _emit(log, "Legenda enviada.")
    finally:
        pagina.close()


def _concluir(page, log=None) -> str:
    """Clica em Publicar/Concluir e devolve a URL do vídeo, se aparecer.

    Levanta erro se não conseguir clicar ou se o clique não for confirmado —
    antes essa falha era engolida em silêncio e o envio "dava certo" mesmo
    com o vídeo ainda como rascunho no Studio (medido em 24/09/2026: o botão
    existia na tela, mas o clique não surtia efeito, e ninguém percebia até
    checar o painel de conteúdo do canal). Não dá pra confiar no link do
    vídeo como prova de que publicou — em Shorts ele já aparece no painel da
    direita desde a etapa de Detalhes, bem antes de existir algo publicado.
    """
    clicou = False
    for seletor in ("#done-button", "ytcp-button#done-button"):
        try:
            botao = page.locator(seletor).first
            botao.wait_for(state="visible", timeout=_ESPERA)
            botao.scroll_into_view_if_needed(timeout=8000)
            botao.click(timeout=_ESPERA)
            clicou = True
            _emit(log, f"Cliquei no botão de publicar/concluir (seletor: {seletor}).")
            break
        except Exception:
            continue
    if not clicou:
        try:
            page.get_by_role(
                "button", name=re.compile(r"Publicar|Salvar|Conclu[ií]r|Done|Save|Publish", re.IGNORECASE)
            ).first.click(timeout=_ESPERA)
            clicou = True
            _emit(log, "Cliquei no botão de publicar/concluir (achado pelo texto — nenhum seletor conhecido bateu).")
        except Exception:
            pass
    if not clicou:
        raise YoutubeBrowserUploadError(
            "Não encontrei (ou não consegui clicar em) o botão de publicar/"
            "concluir na etapa de visibilidade — o vídeo pode ter ficado "
            "como rascunho no Studio.")

    # Rede de segurança: se o Studio ainda achar que as verificações não
    # terminaram, ele não avança — mostra um aviso "Publicar mesmo assim? /
    # Voltar" por cima da tela. Isso não deveria mais acontecer (a espera
    # antes deste ponto já confere os dois ícones de check), mas se
    # acontecer mesmo assim, clica em Voltar em vez de forçar a publicação
    # de um vídeo que o próprio YouTube está pedindo pra não publicar ainda.
    _emit(log, "Verificando se apareceu o aviso de verificações pendentes…")
    try:
        page.locator("ytcp-prechecks-warning-dialog").first.wait_for(state="visible", timeout=3000)
    except Exception:
        _emit(log, "Nenhum aviso de verificações pendentes apareceu — seguindo.")
    else:
        _emit(log, "Apareceu o aviso 'Publicar mesmo assim? / Voltar' — clicando em Voltar.")
        try:
            page.locator("#primary-action-button").first.click(timeout=5000)  # "Voltar"
        except Exception:
            pass
        raise YoutubeBrowserUploadError(
            "O Studio avisou que as verificações ainda não tinham terminado "
            "e pediu confirmação pra publicar mesmo assim — voltei sem "
            "publicar. O vídeo deve ter ficado como rascunho, esperando as "
            "verificações; tente enviar de novo mais tarde.")

    # Confirmação visual (melhor esforço, não trava o envio nisso): o botão
    # costuma sumir da tela quando o Studio sai da etapa de visibilidade
    # para a de confirmação. Mas não é confiável como critério de sucesso —
    # medido em 24/09/2026: um envio que publicou de verdade (2/2
    # verificações OK, clique aceito sem erro, sem aviso de pendência) ainda
    # assim não fez o botão sumir dentro do teto, só porque a troca de tela
    # demorou mais que isso. Tratar isso como erro duro marcava envios
    # bem-sucedidos como "Falhou". As duas checagens anteriores (o clique
    # não lançou exceção, e não apareceu o aviso de "verificações
    # pendentes") já são a garantia real; isto aqui é só um log a mais.
    _emit(log, "Aguardando a troca de tela pra confirmar visualmente (não é obrigatório)…")
    try:
        page.locator("#done-button").first.wait_for(state="hidden", timeout=45_000)
        _emit(log, "Botão de publicar sumiu da tela — clique confirmado visualmente.")
    except Exception:
        _emit(log, "A tela não trocou de figura dentro do esperado, mas o clique foi aceito "
                    "sem erro e sem aviso de verificação pendente — considerando publicado. "
                    "Confira no Studio se quiser ter certeza absoluta.")

    # A tela de confirmação também costuma mostrar o link do vídeo; captura
    # se aparecer, mas a ausência dele não é mais o critério de sucesso.
    try:
        link = page.locator(_LINK_DO_VIDEO).first
        link.wait_for(state="visible", timeout=15_000)
        href = link.get_attribute("href")
        if href:
            _emit(log, f"Link do vídeo capturado: {href}")
            return href
    except Exception:
        _emit(log, "Não consegui capturar o link do vídeo na tela final (não impede o envio).")
    return ""


def _fechar_confirmacao(page, log=None) -> None:
    """Fecha o diálogo final de confirmação, se ele tiver um botão de fechar."""
    for texto in ("Fechar", "Close"):
        try:
            page.get_by_role("button", name=re.compile(texto, re.IGNORECASE)).click(timeout=4000)
            _emit(log, f"Fechei o diálogo de confirmação final (botão '{texto}').")
            return
        except Exception:
            continue


# ── Envio ────────────────────────────────────────────────────────────────────

def upload_video(
    video_path: Path,
    title: str,
    account: str,
    description: str = "",
    visibility: str = DEFAULT_VISIBILITY,
    publish_at: datetime | None = None,
    headless: bool = False,
    log=None,
    legenda_path: Path | None = None,
    legenda_idioma: str = "pt",
) -> str:
    """Envia um vídeo pela tela do Studio, na conta indicada. Devolve a URL.

    `visibility` ("private"/"unlisted"/"public") vale quando `publish_at` é None.
    Com `publish_at`, o vídeo é agendado (privado até a hora marcada). `log`, se
    informado, é uma função que recebe mensagens de progresso (str). `headless`
    roda o navegador escondido, mas o YouTube detecta automação com mais
    facilidade assim — deixe desligado a menos que precise mesmo. `legenda_path`
    (opcional, .srt) sobe como legenda de verdade do vídeo (não só texto
    queimado na imagem) — útil quando o vídeo não veio de um download com
    legenda oficial do YouTube. Best-effort: se falhar, o vídeo já publicado
    não é desfeito, só fica sem essa legenda (veja `_enviar_legenda`).
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)

    video_path = Path(video_path)
    if not video_path.exists():
        raise YoutubeBrowserUploadError(f"Vídeo não encontrado: {video_path}")

    # Perfil persistente (login manual, `login_manual`) é preferido: o Google
    # gira cookies de segurança a cada visita autenticada, e isso só se
    # sustenta sozinho com um perfil de verdade sendo reaberto — um
    # cookies.txt estático sempre acaba ficando pra trás e vencendo (veja o
    # comentário em `profile_dir`). cookies.txt continua funcionando para
    # quem ainda não migrou.
    usa_perfil = profile_dir(account).is_dir()
    cookies_file = None
    cookies = None
    if not usa_perfil:
        cookies_file = cookies_path(account)
        if not cookies_file.exists():
            raise YoutubeBrowserUploadError(
                f"A conta '{account}' ainda não tem credenciais. Rode "
                f"'python youtube_browser_upload.py --login {account}' (login manual, "
                "recomendado) ou exporte um cookies.txt logado em studio.youtube.com e importe.")
        cookies = _parse_netscape_cookies(cookies_file.read_text(encoding="utf-8", errors="replace"))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise YoutubeBrowserUploadError(
            "Playwright não instalado. Rode: pip install playwright "
            "e depois: playwright install chromium"
        ) from error

    titulo_final = title[:100] or video_path.stem
    resumo_agendamento = f"agendado para {publish_at:%d/%m/%Y %H:%M}" if publish_at else f"visibilidade '{visibility}'"
    _log(f"Plano do envio: arquivo '{video_path.name}', conta '{account}', "
         f"título '{titulo_final}', {resumo_agendamento}.")
    if cookies is not None:
        _log(f"Cookies carregados: {len(cookies)} do arquivo {cookies_file.name}.")
    else:
        _log(f"Usando o perfil persistente da conta (login manual em {profile_dir(account)}).")

    etapa = "iniciando navegador"
    with sync_playwright() as p:
        if usa_perfil:
            browser = None
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir(account)), headless=headless,
                locale="pt-BR", viewport={"width": 1366, "height": 900},
                args=_ARGS_CHROMIUM)
        else:
            browser = p.chromium.launch(headless=headless, args=_ARGS_CHROMIUM)
            context = browser.new_context(locale="pt-BR", viewport={"width": 1366, "height": 900})
            try:
                context.add_cookies(cookies)
            except Exception as error:
                browser.close()
                raise YoutubeBrowserUploadError(f"Cookies inválidos para o Playwright: {error}") from error
        _log(f"Navegador aberto ({'headless' if headless else 'com janela visível'}).")

        def _fechar() -> None:
            (browser or context).close()

        page = context.new_page()
        page.set_default_timeout(_ESPERA)
        try:
            etapa = "abrindo o Studio"
            _log("Abrindo o YouTube Studio…")
            page.goto("https://studio.youtube.com/", wait_until="domcontentloaded", timeout=_ESPERA)
            _log(f"Página carregada: {page.url}")
            _dispensar_consentimento(page, log=_log)
            # Se a sessão não autenticou, o Studio redireciona para o login.
            if "accounts.google.com" in page.url or "signin" in page.url.lower():
                dica = (f"Rode 'python youtube_browser_upload.py --login {account}' de novo."
                        if usa_perfil else
                        "Reexporte o cookies.txt logado em studio.youtube.com.")
                raise YoutubeBrowserUploadError(f"O Studio pediu login — a sessão expirou. {dica}")
            _log("Login confirmado.")
            if cookies_file is not None:
                _salvar_cookies_atualizados(context, cookies_file, log=_log)

            etapa = "abrindo o formulário de upload"
            _log("Selecionando o arquivo de vídeo…")
            _abrir_upload(page, video_path, log=_log)
            # A URL já mostra o id do vídeo assim que o Studio cria o rascunho
            # (antes mesmo do upload/publicação terminar) — é o mesmo id usado
            # depois pra achar a tela de legendas, sem depender do link final
            # (que às vezes não aparece na tela de confirmação).
            video_id_m = re.search(r"studio\.youtube\.com/video/([^/]+)/", page.url)
            video_id = video_id_m.group(1) if video_id_m else None

            etapa = "preenchendo título"
            _log("Preenchendo título e descrição…")
            _preencher_texto(page, "#title-textarea #textbox", titulo_final, log=_log, nome_campo="o título")
            etapa = "preenchendo descrição"
            if description:
                _preencher_texto(page, "#description-textarea #textbox", description, log=_log, nome_campo="a descrição")
            else:
                _log("Sem descrição padrão configurada — deixando o campo em branco.")

            etapa = "marcando 'não é para crianças'"
            _marcar_nao_para_criancas(page, log=_log)

            etapa = "aguardando o envio do vídeo"
            _log("Aguardando o upload do vídeo terminar…")
            _aguardar_envio_completo(page, _ESPERA_ENVIO, log=_log)

            etapa = "avançando para a verificação inicial"
            _proximo(page, log=_log, de="Detalhes", para="Elementos do vídeo")
            _proximo(page, log=_log, de="Elementos do vídeo", para="Verificação inicial")

            etapa = "aguardando as verificações iniciais"
            _log("Aguardando as verificações do YouTube (direitos autorais e "
                 "diretrizes da comunidade)…")
            _aguardar_verificacoes_concluidas(page, _ESPERA_VERIFICACAO, log=_log)

            etapa = "avançando para a visibilidade"
            _proximo(page, log=_log, de="Verificação inicial", para="Visibilidade")

            if publish_at:
                etapa = "agendando publicação"
                _log(f"Agendando para {publish_at:%d/%m/%Y %H:%M}…")
                _agendar(page, publish_at, log=_log)
            else:
                etapa = "definindo visibilidade"
                _log(f"Definindo visibilidade: {visibility}…")
                _definir_visibilidade(page, visibility, log=_log)

            etapa = "concluindo"
            _log("Confirmando o envio (o processamento continua no YouTube)…")
            url = _concluir(page, log=_log)
            _fechar_confirmacao(page, log=_log)
            page.wait_for_timeout(2000)

            if legenda_path and legenda_path.is_file():
                if video_id:
                    etapa = "enviando a legenda"
                    _log(f"Enviando legenda ({legenda_path.name})…")
                    try:
                        _enviar_legenda(page, video_id, legenda_path, legenda_idioma, log=_log)
                    except Exception as exc:  # noqa: BLE001 — vídeo já publicado; legenda é bônus
                        _log(f"Legenda não foi enviada ({exc}); o vídeo continua publicado normalmente.")
                else:
                    _log("Não consegui identificar o id do vídeo — pulando o envio da legenda.")

            # Salva de novo no final: a espera das verificações pode levar
            # bastante tempo, e o Google pode ter renovado o token de sessão
            # durante esse período — este é o cookie mais fresco que se
            # consegue capturar de toda a execução. Com perfil persistente
            # isso já acontece sozinho ao fechar o contexto.
            if cookies_file is not None:
                _salvar_cookies_atualizados(context, cookies_file, log=_log)
        except YoutubeBrowserUploadError:
            dica = _retrato_da_tela(page, etapa)
            _log(f"Falhou na etapa '{etapa}'. {dica}")
            _fechar()
            raise
        except Exception as error:  # noqa: BLE001 — a tela do Studio pode mudar
            dica = _retrato_da_tela(page, etapa)
            _log(f"Falhou na etapa '{etapa}' com erro inesperado: "
                 f"{type(error).__name__}: {str(error).strip()[:300]}. {dica}")
            _fechar()
            raise YoutubeBrowserUploadError(
                f"Falha na {dica}. Erro: {type(error).__name__}: {str(error).strip()[:300]}. "
                f"Veja {FALHA_PNG}/{FALHA_HTML} na pasta de dados."
            ) from error
        _fechar()
        _log("Navegador fechado.")

    if publish_at:
        return url or f"Agendado para {publish_at:%d/%m/%Y %H:%M} na conta '{account}' (upload enviado)."
    return url or f"Enviado como '{visibility}' na conta '{account}'."


if __name__ == "__main__":
    import sys as _sys
    if len(_sys.argv) == 3 and _sys.argv[1] == "--login":
        login_manual(_sys.argv[2])
    else:
        print("Uso: python youtube_browser_upload.py --login <apelido-da-conta>")
        _sys.exit(1)
