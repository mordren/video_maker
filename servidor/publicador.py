"""Publicador: fila que publica os vídeos no YouTube sozinha, num servidor local.

Roda num Debian da rede (http://192.168.31.130:8080) e faz uma coisa só:
guardar uma lista de vídeos no HD do servidor e ir publicando um por vez, com
um intervalo aleatório entre eles (por padrão entre 2h30 e 3h30), para os
envios não saírem sempre no mesmo ritmo. Essa espera é *por canal*: cada um
tem o seu relógio, então dois canais não dividem o mesmo ritmo.

Cada vídeo já sai com a privacidade escolhida assim que o envio termina — sem
agendamento. O envio por navegador (`youtube_browser_upload.py`) já espera o
upload e as verificações do YouTube (direitos autorais, diretrizes da
comunidade) terminarem antes de publicar, então não precisa da etapa a mais
de subir privado e o YouTube abrir sozinho depois — isso só fazia sentido na
versão antiga pela API oficial.

A interface é o navegador: subir os arquivos, montar a lista, escolher o canal
de cada vídeo, reordenar, excluir e ligar/desligar os envios. Não depende do
programa de desktop — só reaproveita o `youtube_browser_upload.py`, que
controla um navegador de verdade (Playwright, com display virtual via Xvfb —
veja publicador.service) autenticado por cookies, do mesmo jeito que o
`tiktok_upload.py` já faz com o TikTok. Antes o envio era pela API oficial do
YouTube; trocado porque a API tem cota apertada (~6 vídeos/dia) e, pela
experiência medida neste projeto, os vídeos enviados por ela quase não eram
entregues.

Tudo o que é estado mora na pasta de dados (PUBLICADOR_DATA, por padrão
/srv/publicador), fora da pasta do programa:

    videos/                              os arquivos que subiram pelo navegador
    fila.json                            a lista, a ordem e o próximo horário de cada canal
    config.json                          privacidade, intervalo e descrição por canal
    publicador.log                       histórico do que aconteceu
    youtube_browser_cookies_<canal>.txt  login do canal (cookies exportados do navegador)

Um envio por vez, de propósito: o laço é uma thread só e toda escrita no
fila.json passa pela mesma trava — dois uploads simultâneos na mesma conta é
justamente o tipo de padrão que se quer evitar aqui.
"""

from __future__ import annotations

import json
import os
import random
import re
import smtplib
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, make_response, render_template, request, send_file

DATA_DIR = Path(os.environ.get("PUBLICADOR_DATA") or "/srv/publicador")
VIDEOS_DIR = DATA_DIR / "videos"
FILA_PATH = DATA_DIR / "fila.json"
CONFIG_PATH = DATA_DIR / "config.json"
LOG_PATH = DATA_DIR / "publicador.log"

# O youtube_browser_upload procura youtube_browser_cookies_<canal>.txt na
# pasta apontada por VIDEOMAKER_DIR; aqui as credenciais ficam junto dos
# dados, não junto do programa (que é instalado em /opt e pode ser substituído).
os.environ.setdefault("VIDEOMAKER_DIR", str(DATA_DIR))

import tiktok_upload  # noqa: E402  (idem; opcional — só pesa se algum canal usar)
import youtube_browser_upload  # noqa: E402  (precisa do VIDEOMAKER_DIR já definido)

# E-mail de aviso (opcional): sem PUBLICADOR_EMAIL_PARA definido, o serviço
# roda normalmente e só o histórico da página registra os erros. As
# credenciais de SMTP ficam fora do código de propósito — veja o LEIA-ME
# (EnvironmentFile aponta para <DATA_DIR>/publicador.env, um arquivo que a
# instalação nunca sobrescreve).
EMAIL_PARA = os.environ.get("PUBLICADOR_EMAIL_PARA", "").strip()
EMAIL_SMTP_HOST = os.environ.get("PUBLICADOR_EMAIL_SMTP_HOST", "smtp.gmail.com").strip()
EMAIL_SMTP_PORT = int(os.environ.get("PUBLICADOR_EMAIL_SMTP_PORT") or 587)
EMAIL_SMTP_USER = os.environ.get("PUBLICADOR_EMAIL_SMTP_USER", "").strip()
EMAIL_SMTP_SENHA = os.environ.get("PUBLICADOR_EMAIL_SMTP_SENHA", "")
EMAIL_DE = os.environ.get("PUBLICADOR_EMAIL_DE", "").strip() or EMAIL_SMTP_USER
URL_PUBLICADOR = os.environ.get("PUBLICADOR_URL", "http://192.168.31.130:8080").strip()

EXTENSOES = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
TAMANHO_MAXIMO = 12 * 1024 * 1024 * 1024        # 12 GB por requisição
INTERVALO_DO_LACO = 15                          # segundos entre conferências
ESPERA_SEM_REDE = 5                             # minutos até tentar de novo sem internet
MAX_TENTATIVAS_REDE = 12                        # ~3h insistindo antes de desistir
ESPERA_APOS_RECUSA = 30                         # minutos até tentar de novo após recusa
MAX_TENTATIVAS_RECUSA = 5                       # 30, 60, 90… ~7h antes de desistir

# Quedas de rede no servidor (DNS fora do ar, link caído, Wi-Fi oscilando) não
# são culpa do vídeo: o envio é refeito sozinho em vez de marcar "falhou" e
# ficar esperando alguém clicar. Os nomes são das classes que requests,
# urllib3, httplib2 e google-auth levantavam nesses casos (versão antiga, API
# oficial); o envio por navegador (Playwright/Chromium) levanta outra coisa,
# coberta pelas marcas de texto abaixo.
ERROS_DE_REDE = {
    "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "TimeoutError",
    "TransportError", "NewConnectionError", "NameResolutionError", "MaxRetryError",
    "ServerNotFoundError", "ProtocolError", "SSLError", "gaierror", "timeout",
    "RemoteDisconnected", "IncompleteRead", "ConnectionResetError", "BrokenPipeError",
}
MARCAS_DE_REDE = ("name resolution", "max retries exceeded", "connection aborted",
                  "connection refused", "temporarily unavailable", "timed out",
                  "network is unreachable",
                  # Erros de rede do próprio Chromium (net::ERR_*) — os dois
                  # primeiros são os que já apareceram de verdade neste
                  # servidor antes do ajuste de IPv6/DNS (veja o LEIA-ME).
                  "err_network_changed", "err_name_not_resolved",
                  "err_internet_disconnected", "err_connection_reset",
                  "err_connection_refused", "err_connection_timed_out",
                  "err_address_unreachable", "net::err_")

# "proximos" guarda um horário por canal: cada canal tem o seu próprio ritmo,
# senão dois canais dividiriam a mesma espera e cada um sairia na metade do ritmo.
FILA_PADRAO: dict = {"rodando": False, "proximos": {}, "itens": []}

# Cada canal tem a sua própria privacidade, intervalo, sorteio e descrição —
# um canal de política e um de humor, por exemplo, não têm por que publicar
# do mesmo jeito. config.json guarda isso em config["canais"][<canal>].
CANAL_CONFIG_PADRAO: dict = {
    "privacidade": "public",
    "intervalo_min": 150,                       # 2h30
    "intervalo_max": 210,                       # 3h30
    "ordem_aleatoria": False,
    "descricao": "",
    "tiktok_conta": "",                         # apelido da conta do TikTok, se este
                                                 # canal também publica lá (vazio = não)
    "tiktok_legenda": "",                       # legenda/hashtags padrão do TikTok
}
CONFIG_PADRAO: dict = {"canais": {}}            # canal -> CANAL_CONFIG_PADRAO

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAXIMO

# Protege fila.json e config.json de duas requisições ao mesmo tempo; o envio
# em si roda fora da trava (leva minutos), marcado como "enviando" na fila.
_TRAVA = threading.RLock()
_ENVIANDO: str | None = None                    # id do item em envio, se houver


# ──────────────────────────────────────────────────────────────────
#  Estado em disco
# ──────────────────────────────────────────────────────────────────

def _ler_json(caminho: Path, padrao: dict) -> dict:
    """Lê um .json de estado; se estiver faltando ou corrompido, usa o padrão.

    O padrão é copiado inteiro: sem isso, a lista de itens devolvida na
    primeira leitura (quando o arquivo ainda não existe) seria a própria lista
    dentro de FILA_PADRAO, e tudo o que entrasse na fila ficaria grudado nela.
    """
    dados = {}
    if caminho.exists():
        try:
            lido = json.loads(caminho.read_text(encoding="utf-8"))
            if isinstance(lido, dict):
                dados = lido
        except (json.JSONDecodeError, OSError):
            dados = {}
    return {**deepcopy(padrao), **dados}


def _gravar_json(caminho: Path, dados: dict) -> None:
    """Grava primeiro num .tmp e só então substitui — nunca deixa meio arquivo."""
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_suffix(caminho.suffix + ".tmp")
    temporario.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    temporario.replace(caminho)


def carregar_fila() -> dict:
    fila = _ler_json(FILA_PATH, FILA_PADRAO)
    # Fila gravada pela versão antiga, que tinha um horário só para todos. Esse
    # horário era a espera de quem acabou de publicar, então só o canal do
    # último envio o herda; os outros nunca esperaram nada e ficam livres.
    antigo = fila.pop("proximo_envio", None)
    if antigo and not fila.get("proximos"):
        ultimo = max((item for item in fila["itens"] if item.get("enviado_em")),
                     key=lambda item: item["enviado_em"], default=None)
        fila["proximos"] = {ultimo["canal"]: antigo} if ultimo and ultimo.get("canal") else {}
    return fila


def salvar_fila(fila: dict) -> None:
    _gravar_json(FILA_PATH, fila)


def carregar_config() -> dict:
    return _migrar_config_antiga(_ler_json(CONFIG_PATH, CONFIG_PADRAO))


def _migrar_config_antiga(config: dict) -> dict:
    """Versão antiga: privacidade/intervalo/atraso/sorteio eram globais e só a
    descrição já era por canal. Agora tudo mora em config["canais"][<canal>].

    "atraso_publicacao" (agendamento) não existe mais — a versão da API
    usava para subir privado e deixar o YouTube abrir sozinho depois; o
    envio por navegador já espera as verificações terminarem antes de
    publicar, então não precisa dessa etapa a mais. Uma config antiga que
    ainda tenha essa chave só a perde nesta migração, sem erro.
    """
    chaves_antigas = ("privacidade", "intervalo_min", "intervalo_max",
                       "atraso_publicacao", "ordem_aleatoria", "descricoes")
    if not any(chave in config for chave in chaves_antigas):
        return config
    globais = {
        "privacidade": config.get("privacidade", CANAL_CONFIG_PADRAO["privacidade"]),
        "intervalo_min": config.get("intervalo_min", CANAL_CONFIG_PADRAO["intervalo_min"]),
        "intervalo_max": config.get("intervalo_max", CANAL_CONFIG_PADRAO["intervalo_max"]),
        "ordem_aleatoria": bool(config.get("ordem_aleatoria", False)),
    }
    descricoes = config.get("descricoes") or {}
    canais_cfg = dict(config.get("canais") or {})
    nomes = set(descricoes) | {c["nome"] for c in canais()}
    for nome in nomes:
        if nome not in canais_cfg:
            canais_cfg[nome] = {**globais, "descricao": descricoes.get(nome, "")}
    novo = {"canais": canais_cfg}
    salvar_config(novo)
    registrar("🔧 Configuração de envio migrada: agora cada canal tem a sua própria.")
    return novo


def _config_do_canal(config: dict, canal: str) -> dict:
    """Configuração de envio de um canal, com os padrões para o que falta."""
    salvo = (config.get("canais") or {}).get(canal) or {}
    return {**CANAL_CONFIG_PADRAO, **salvo}


def salvar_config(config: dict) -> None:
    _gravar_json(CONFIG_PATH, config)


def registrar(texto: str) -> None:
    """Anota uma linha no log, com hora, e corta o arquivo quando ele cresce."""
    linha = f"{datetime.now():%d/%m %H:%M:%S}  {texto}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as arquivo:
        arquivo.write(linha + "\n")
    try:
        linhas = LOG_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    if len(linhas) > 4000:
        LOG_PATH.write_text("\n".join(linhas[-1000:]) + "\n", encoding="utf-8")


def ultimas_linhas_do_log(quantas: int = 80) -> list[str]:
    if not LOG_PATH.exists():
        return []
    try:
        return LOG_PATH.read_text(encoding="utf-8").splitlines()[-quantas:]
    except OSError:
        return []


def avisar_por_email(assunto: str, corpo: str) -> None:
    """Manda um e-mail de aviso, se PUBLICADOR_EMAIL_PARA estiver configurado.

    Só é chamado nos casos que precisam de alguém olhar (um envio que
    esgotou as tentativas, ou o serviço acabou de subir — sinal indireto de
    que ele tinha caído). Sem e-mail configurado, não faz nada: o histórico
    continua sendo a fonte de verdade. Falha ao mandar o e-mail nunca
    derruba o que estava acontecendo — só fica registrada no histórico.
    """
    if not (EMAIL_PARA and EMAIL_SMTP_HOST and EMAIL_SMTP_USER and EMAIL_SMTP_SENHA):
        return
    mensagem = EmailMessage()
    mensagem["Subject"] = assunto
    mensagem["From"] = EMAIL_DE
    mensagem["To"] = EMAIL_PARA
    mensagem.set_content(corpo)
    try:
        # Porta 465 é SSL implícito desde a conexão (Hostinger, entre outros);
        # qualquer outra porta (587 no Gmail) é texto puro que vira TLS depois
        # do STARTTLS. Os dois protocolos não se misturam — usar o errado dá
        # erro de handshake, não um erro claro de senha.
        if EMAIL_SMTP_PORT == 465:
            with smtplib.SMTP_SSL(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=20) as smtp:
                smtp.login(EMAIL_SMTP_USER, EMAIL_SMTP_SENHA)
                smtp.send_message(mensagem)
        else:
            with smtplib.SMTP(EMAIL_SMTP_HOST, EMAIL_SMTP_PORT, timeout=20) as smtp:
                smtp.starttls()
                smtp.login(EMAIL_SMTP_USER, EMAIL_SMTP_SENHA)
                smtp.send_message(mensagem)
    except Exception as erro:                            # noqa: BLE001
        registrar(f"⚠️ Não deu para mandar o e-mail de aviso: {erro}")


# ──────────────────────────────────────────────────────────────────
#  Canais
# ──────────────────────────────────────────────────────────────────

def canais() -> list[dict]:
    """Canais configurados, um por youtube_browser_cookies_<canal>.txt na pasta de dados.

    Diferente do login por OAuth (client_secret + token), aqui não existe um
    estado intermediário de "configurado mas não autorizado" — o cookies.txt
    importado já é a credencial inteira, então "existe o arquivo" e
    "conectado" são a mesma coisa.
    """
    return [{"nome": nome, "conectado": True}
            for nome in youtube_browser_upload.list_accounts()]


def canal_padrao() -> str:
    """O canal a usar quando ninguém disse qual — só se houver um só conectado.

    Com dois ou mais não dá para adivinhar, e chutar aqui seria publicar no
    canal errado: o vídeo entra sem canal e a tela pede para escolher.
    """
    conectados = [c["nome"] for c in canais() if c["conectado"]]
    return conectados[0] if len(conectados) == 1 else ""


# ──────────────────────────────────────────────────────────────────
#  Arquivos de vídeo
# ──────────────────────────────────────────────────────────────────

def _nome_seguro(nome: str) -> str:
    """Tira caminho e caracteres problemáticos do nome que veio do navegador."""
    nome = Path(nome.replace("\\", "/")).name
    nome = re.sub(r'[\x00-\x1f<>:"/|?*]+', "_", nome).strip(" .")
    return nome[:140] or "video.mp4"


def _nome_livre(nome: str) -> str:
    """Se já existe um arquivo com esse nome, acrescenta (2), (3)… ao final."""
    destino = VIDEOS_DIR / nome
    if not destino.exists():
        return nome
    base, extensao = destino.stem, destino.suffix
    for contador in range(2, 1000):
        tentativa = f"{base} ({contador}){extensao}"
        if not (VIDEOS_DIR / tentativa).exists():
            return tentativa
    return f"{base} {int(time.time())}{extensao}"


def caminho_do_video(arquivo: str) -> Path:
    """Caminho absoluto de um vídeo da fila, sempre dentro de videos/."""
    return VIDEOS_DIR / Path(arquivo).name


def _apagar_video(arquivo: str, fila: dict, ignorando: str = "") -> None:
    """Apaga o arquivo do HD, se nenhum outro item da fila ainda usar ele."""
    em_uso = any(
        item["arquivo"] == arquivo and item["id"] != ignorando
        for item in fila["itens"])
    if em_uso:
        return
    try:
        caminho_do_video(arquivo).unlink(missing_ok=True)
    except OSError as erro:
        registrar(f"⚠️ não deu para apagar {arquivo}: {erro}")


# ──────────────────────────────────────────────────────────────────
#  Fila: escolha do próximo e envio
# ──────────────────────────────────────────────────────────────────

def _novo_id() -> str:
    return f"{datetime.now():%Y%m%d%H%M%S}-{uuid4().hex[:6]}"


def _pendentes(fila: dict) -> list[dict]:
    return [item for item in fila["itens"] if item.get("status") == "aguardando"]


def _adotar_canal_nos_orfaos() -> dict:
    """Dá um canal para os itens que entraram na fila antes de haver algum.

    Sem isso eles ficariam parados para sempre: entraram com o canal vazio (a
    fila estava sem credencial na hora do upload) e o envio pula quem não tem
    canal conectado. Item com canal preenchido nunca é mexido aqui — trocar o
    canal de um vídeo sozinho seria publicar no lugar errado.

    Só vale quando existe um canal conectado e um só: com dois ou mais não há
    como adivinhar qual, e a tela mostra "(escolha o canal)" para o usuário
    resolver na mão.
    """
    with _TRAVA:
        fila = carregar_fila()
        padrao = canal_padrao()
        if not padrao:
            return fila
        ajustados = [item for item in fila["itens"]
                     if not item.get("canal") and item.get("status") == "aguardando"]
        for item in ajustados:
            item["canal"] = padrao
        if ajustados:
            salvar_fila(fila)
            registrar(f"🔧 {len(ajustados)} vídeo(s) sem canal foram apontados para '{padrao}'.")
        return fila


def _escolher_proximo(fila: dict, config: dict) -> dict | None:
    """O próximo a subir: o primeiro da fila de cada canal pronto, ou um
    sorteado dentre os vídeos do canal se ele estiver com o sorteio ligado.

    Só entra na conta quem tem canal conectado — um item apontando para um
    canal sem token fica esperando na fila até o token chegar, sem travar os
    outros. Se dois canais ficam prontos ao mesmo tempo, sai o que está mais
    acima na lista.
    """
    conectados = {c["nome"] for c in canais() if c["conectado"]}
    prontos = [item for item in _pendentes(fila)
               if item.get("canal") in conectados and _canal_liberado(fila, item["canal"])]
    if not prontos:
        return None
    por_canal: dict[str, list[dict]] = {}
    for item in prontos:
        por_canal.setdefault(item["canal"], []).append(item)
    escolhidos = []
    for canal, itens_do_canal in por_canal.items():
        sorteia = _config_do_canal(config, canal).get("ordem_aleatoria")
        escolhidos.append(random.choice(itens_do_canal) if sorteia else itens_do_canal[0])
    ordem = {item["id"]: indice for indice, item in enumerate(fila["itens"])}
    escolhidos.sort(key=lambda item: ordem[item["id"]])
    return escolhidos[0]


def _erro_de_rede(erro: BaseException) -> bool:
    """O envio morreu por falta de internet, e não porque o YouTube recusou?

    Olha a cadeia inteira da exceção: a de fora costuma ser um TransportError
    do google-auth embrulhando o erro real do requests lá embaixo.
    """
    atual: BaseException | None = erro
    vistos = 0
    while atual is not None and vistos < 10:
        if type(atual).__name__ in ERROS_DE_REDE or isinstance(atual, OSError):
            return True
        atual = atual.__cause__ or atual.__context__
        vistos += 1
    texto = str(erro).lower()
    return any(marca in texto for marca in MARCAS_DE_REDE)


def _sortear_intervalo(config: dict) -> timedelta:
    """Quanto esperar até o próximo envio: um sorteio dentro da faixa escolhida."""
    minimo = int(config.get("intervalo_min") or 150)
    maximo = int(config.get("intervalo_max") or 210)
    if maximo < minimo:
        minimo, maximo = maximo, minimo
    return timedelta(minutes=random.uniform(minimo, maximo))


def _marcar_proximo(canal: str, quando: datetime) -> None:
    """Guarda quando este canal pode mandar o próximo — os outros não esperam."""
    with _TRAVA:
        fila = carregar_fila()
        proximos = fila.get("proximos") or {}
        proximos[canal] = quando.isoformat(timespec="seconds")
        fila["proximos"] = proximos
        salvar_fila(fila)


def _canal_liberado(fila: dict, canal: str) -> bool:
    """O canal já cumpriu a espera desde o último envio dele?"""
    quando = (fila.get("proximos") or {}).get(canal)
    if not quando:
        return True
    try:
        return datetime.now() >= datetime.fromisoformat(quando)
    except ValueError:
        return True                                      # horário torto: libera


def _mandar_para_o_fim(item_id: str) -> None:
    """Põe o item no fim da lista, para ele não travar a vez dos outros."""
    with _TRAVA:
        fila = carregar_fila()
        item = next((i for i in fila["itens"] if i["id"] == item_id), None)
        if item is None:
            return
        fila["itens"] = [i for i in fila["itens"] if i["id"] != item_id] + [item]
        salvar_fila(fila)


def _atualizar_item(item_id: str, **campos) -> None:
    with _TRAVA:
        fila = carregar_fila()
        for item in fila["itens"]:
            if item["id"] == item_id:
                item.update(campos)
                break
        salvar_fila(fila)


def _legenda_tiktok(titulo: str, cfg_canal: dict) -> str:
    """Título do vídeo + a legenda/hashtags padrão do canal, cada um numa linha."""
    legenda = (cfg_canal.get("tiktok_legenda") or "").strip()
    return f"{titulo}\n\n{legenda}" if legenda else titulo


def _talvez_publicar_no_tiktok(
        item_id: str, item: dict, cfg_canal: dict, caminho: Path, titulo: str) -> None:
    """Publica o mesmo vídeo no TikTok, depois de o YouTube já ter dado certo.

    Só é chamada quando o YouTube publicou de verdade — nunca numa tentativa
    que falhou ou que vai ser refeita automaticamente (rede, recusa): tentar
    TikTok nesses casos publicava lá um vídeo cujo YouTube podia nunca sair,
    e ainda marcava este vídeo como "já teve sua vez no TikTok" pra sempre,
    pulando a tentativa de verdade quando o YouTube enfim funcionasse (era um
    bug real, medido em 24/09/2026).

    Sai **depois** do YouTube, e não junto: os dois sobem o mesmo arquivo pela
    mesma internet, e em paralelo disputam a banda de subida — o YouTube, que
    manda o vídeo inteiro de uma vez, sufoca o TikTok a ponto de a página dele
    nem abrir (timeout). É a mesma razão de o serviço nunca mandar dois vídeos
    ao mesmo tempo. Falha no TikTok fica só no histórico — nunca derruba o
    envio do vídeo ao YouTube, que já tinha dado certo antes desta função
    rodar.
    """
    conta = (cfg_canal.get("tiktok_conta") or "").strip()
    # Qualquer estado já anotado (indo, foi, ou falhou) significa que este vídeo
    # já teve a sua vez no TikTok. Repetir depois de uma falha é arriscado: a
    # falha costuma acontecer *depois* de o arquivo já ter subido, e a segunda
    # tentativa publicaria o mesmo vídeo de novo.
    if not conta or item.get("tiktok_status"):
        return
    if not tiktok_upload.is_authorized(conta):
        registrar(f"⚠️ TikTok: conta '{conta}' sem cookies importados, pulando “{titulo}”.")
        _atualizar_item(item_id, tiktok_status="falhou",
                        tiktok_mensagem=f"Conta '{conta}' sem cookies importados.")
        avisar_por_email(
            f"⚠️ Publicador: TikTok sem cookies (conta {conta})",
            f"A conta '{conta}' do TikTok está sem cookies importados — "
            f"\"{titulo}\" (e os próximos vídeos deste canal) não vão pro "
            f"TikTok até isso ser corrigido.\n\n{URL_PUBLICADOR}")
        return
    legenda = _legenda_tiktok(titulo, cfg_canal)
    _atualizar_item(item_id, tiktok_status="enviando", tiktok_mensagem="")
    registrar(f"📤 Enviando “{titulo}” também para o TikTok (conta {conta})…")
    try:
        mensagem = tiktok_upload.upload_video(caminho, legenda, conta)
    except Exception as erro:                            # noqa: BLE001
        registrar(f"⚠️ TikTok recusou “{titulo}”: {erro}")
        _atualizar_item(item_id, tiktok_status="falhou", tiktok_mensagem=str(erro)[:500])
        avisar_por_email(
            f"⚠️ Publicador: TikTok recusou \"{titulo}\" ({conta})",
            f"O envio ao TikTok de \"{titulo}\" (conta {conta}) falhou e não "
            f"tenta de novo sozinho.\n\nErro: {erro}\n\n{URL_PUBLICADOR}")
        return
    registrar(f"✅ TikTok: {mensagem}")
    _atualizar_item(item_id, tiktok_status="enviado", tiktok_mensagem=mensagem)


def _enviar_item(item_id: str) -> None:
    """Sobe um item já marcado como "enviando" e agenda o próximo envio.

    Roda numa thread própria (pode levar minutos). Erro aqui nunca derruba o
    serviço nem faz o vídeo sumir da lista: sem internet ele espera e tenta de
    novo no mesmo lugar; recusado pelo YouTube, vai para o fim da fila e tenta
    de novo mais tarde. Só vira "falhou" de vez — esperando um clique em
    "Tentar de novo" — depois de esgotar as tentativas.
    """
    global _ENVIANDO
    youtube_ok = False
    try:
        with _TRAVA:
            fila = carregar_fila()
            config = carregar_config()
            item = next((i for i in fila["itens"] if i["id"] == item_id), None)
        if item is None:
            return

        caminho = caminho_do_video(item["arquivo"])
        titulo = (item.get("titulo") or caminho.stem)[:100]
        canal = item.get("canal") or ""
        cfg_canal = _config_do_canal(config, canal)
        descricao = cfg_canal.get("descricao", "")

        if not caminho.exists():
            registrar(f"⚠️ {item['arquivo']} não está mais no HD do servidor.")
            _atualizar_item(item_id, status="falhou", tentativas=0,
                            mensagem="O arquivo não está mais na pasta de vídeos.")
            _marcar_proximo(canal, datetime.now())
            return

        # Sem agendamento: o envio por navegador já espera o upload e as
        # verificações do YouTube terminarem antes de publicar (veja
        # youtube_browser_upload.py), então o vídeo já sai valendo com esta
        # privacidade assim que o envio termina — sem precisar da etapa a
        # mais de subir privado e o YouTube abrir sozinho depois (isso era
        # só da versão pela API oficial).
        privacidade = cfg_canal.get("privacidade", "public")

        registrar(f"📤 Enviando “{titulo}” no canal {canal}…")

        def progresso(mensagem: str) -> None:
            registrar(f"   {titulo}: {mensagem}")

        try:
            # headless=False: o serviço roda sob Xvfb (display virtual — veja
            # publicador.service), mesma ideia já usada pelo tiktok_upload.py
            # aqui dentro. headless=True existe, mas o YouTube detecta e
            # bloqueia automação com mais facilidade nesse modo.
            url = youtube_browser_upload.upload_video(
                caminho, titulo, canal, descricao, privacidade,
                publish_at=None, headless=False, log=progresso)
        except Exception as erro:                        # noqa: BLE001
            tentativas = int(item.get("tentativas") or 0) + 1
            if _erro_de_rede(erro) and tentativas <= MAX_TENTATIVAS_REDE:
                # Espera crescente: 5, 10, 15… até 30 minutos, para uma queda
                # longa não virar uma sequência de tentativas inúteis.
                espera = min(ESPERA_SEM_REDE * tentativas, 30)
                registrar(f"🌐 Servidor sem internet ao enviar “{titulo}” "
                          f"(tentativa {tentativas}); tenta de novo em {espera} min.")
                _atualizar_item(
                    item_id, status="aguardando", tentativas=tentativas,
                    mensagem=f"Servidor sem internet; nova tentativa em {espera} min.")
                _marcar_proximo(canal, datetime.now() + timedelta(minutes=espera))
                return
            if tentativas <= MAX_TENTATIVAS_RECUSA:
                # Recusa do YouTube (cota estourada, erro momentâneo): o vídeo
                # volta para o fim da fila em vez de ficar parado esperando um
                # clique. Vai para o fim, e não para o mesmo lugar, para não
                # segurar os outros vídeos do canal enquanto espera.
                espera = min(ESPERA_APOS_RECUSA * tentativas, 240)
                registrar(f"↩️ “{titulo}” voltou para o fim da fila "
                          f"(tentativa {tentativas}/{MAX_TENTATIVAS_RECUSA}, "
                          f"nova chance em {espera} min): {erro}")
                _atualizar_item(
                    item_id, status="aguardando", tentativas=tentativas,
                    mensagem=f"Tentativa {tentativas}/{MAX_TENTATIVAS_RECUSA} em "
                             f"{espera} min. O YouTube recusou: {str(erro)[:300]}")
                _mandar_para_o_fim(item_id)
                _marcar_proximo(canal, datetime.now() + timedelta(minutes=espera))
                return
            registrar(f"⚠️ Falhou “{titulo}” e desisti depois de "
                      f"{MAX_TENTATIVAS_RECUSA} tentativas: {erro}")
            _atualizar_item(item_id, status="falhou", mensagem=str(erro)[:500], tentativas=0)
            _marcar_proximo(canal, datetime.now() + timedelta(minutes=ESPERA_APOS_RECUSA))
            avisar_por_email(
                f"⚠️ Publicador: falha ao enviar \"{titulo}\" ({canal})",
                f"O vídeo \"{titulo}\" (canal {canal}) falhou depois de "
                f"{MAX_TENTATIVAS_RECUSA} tentativas e está parado, esperando "
                f"você clicar em \"Tentar de novo\".\n\nErro: {erro}\n\n{URL_PUBLICADOR}")
            return

        registrar(f"✅ Publicado “{titulo}”: {url}")
        _atualizar_item(item_id, status="enviado", url=url, mensagem="", tentativas=0,
                        enviado_em=datetime.now().isoformat(timespec="seconds"))
        proximo = datetime.now() + _sortear_intervalo(_config_do_canal(carregar_config(), canal))
        _marcar_proximo(canal, proximo)
        if any(i.get("canal") == canal for i in _pendentes(carregar_fila())):
            registrar(f"⏳ Próximo envio do canal {canal} por volta de {proximo:%d/%m %H:%M}.")
        # O TikTok deste vídeo sai só agora, com o YouTube já confirmado —
        # nunca antes disso. Tentar TikTok num envio que falhou (rede,
        # recusa, ou que ainda vai ser refeito automaticamente) publicava lá
        # um vídeo cujo YouTube podia nunca sair, e ainda marcava
        # tiktok_status como "feito" pra sempre, pulando a tentativa de
        # verdade quando o YouTube enfim funcionasse (medido em 24/09/2026).
        youtube_ok = True
    finally:
        # Aqui dentro, antes de liberar o _ENVIANDO: enquanto o TikTok sobe,
        # nenhum outro vídeo da fila começa a subir por cima dele.
        if youtube_ok:
            try:
                _talvez_publicar_no_tiktok(item_id, item, cfg_canal, caminho, titulo)
            except Exception as erro:                    # noqa: BLE001
                registrar(f"⚠️ Erro inesperado no envio ao TikTok: {erro}")
        with _TRAVA:
            _ENVIANDO = None


def _iniciar_envio(item_id: str) -> bool:
    """Marca o item como "enviando" e dispara a thread. False se já tem um em curso."""
    global _ENVIANDO
    with _TRAVA:
        if _ENVIANDO:
            return False
        fila = carregar_fila()
        item = next((i for i in fila["itens"] if i["id"] == item_id), None)
        if item is None:
            return False
        item["status"] = "enviando"
        item["mensagem"] = ""
        salvar_fila(fila)
        _ENVIANDO = item_id
    threading.Thread(target=_enviar_item, args=(item_id,), daemon=True).start()
    return True


def _passo_do_laco() -> None:
    """Uma conferência da fila: chegou a hora? tem quem enviar? então envia."""
    if _ENVIANDO:
        return
    fila = _adotar_canal_nos_orfaos()
    with _TRAVA:
        if _ENVIANDO:
            return
        if not fila.get("rodando"):
            return
        config = carregar_config()
        proximo = _escolher_proximo(fila, config)
        if proximo is None:
            return
        item_id = proximo["id"]
    _iniciar_envio(item_id)


def _laco_de_envio() -> None:
    while True:
        try:
            _passo_do_laco()
        except Exception as erro:                        # noqa: BLE001
            registrar(f"⚠️ Erro no laço de envio: {erro}")
        time.sleep(INTERVALO_DO_LACO)


# ──────────────────────────────────────────────────────────────────
#  Interface web
# ──────────────────────────────────────────────────────────────────

@app.get("/")
def pagina():
    resposta = make_response(render_template("index.html"))
    # Sem isto, o navegador pode continuar mostrando a página de antes de uma
    # atualização do servidor — e as correções parecem não ter chegado.
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.get("/api/estado")
def estado():
    fila = _adotar_canal_nos_orfaos()
    config = carregar_config()
    canais_lista = canais()
    # Cada canal, mesmo sem configuração salva ainda, entra com os padrões —
    # a tela não precisa saber a diferença entre "nunca configurado" e "salvo".
    canais_cfg = {c["nome"]: _config_do_canal(config, c["nome"]) for c in canais_lista}
    return jsonify({
        "agora": datetime.now().isoformat(timespec="seconds"),
        "rodando": bool(fila.get("rodando")),
        "proximos": fila.get("proximos") or {},
        "enviando": _ENVIANDO,
        "itens": fila.get("itens", []),
        "canais": canais_lista,
        "config": {"canais": canais_cfg},
        "tiktok_contas": tiktok_upload.list_accounts(),
        "log": ultimas_linhas_do_log(),
        "pasta_videos": str(VIDEOS_DIR),
    })


@app.post("/api/videos")
def subir_videos():
    """Recebe os arquivos do navegador, grava no HD e põe no fim da lista."""
    canal = (request.form.get("canal") or canal_padrao()).strip()
    arquivos = [a for a in request.files.getlist("arquivos") if a and a.filename]
    if not arquivos:
        return jsonify({"erro": "Nenhum arquivo escolhido."}), 400

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    adicionados, recusados = [], []
    # Gravar no HD pode levar um tempo num vídeo grande; a trava da fila só é
    # tomada depois, para a página continuar respondendo durante o upload.
    for enviado in arquivos:
        nome = _nome_seguro(enviado.filename)
        if Path(nome).suffix.lower() not in EXTENSOES:
            recusados.append(nome)
            continue
        nome = _nome_livre(nome)
        enviado.save(VIDEOS_DIR / nome)
        adicionados.append(nome)

    with _TRAVA:
        fila = carregar_fila()
        for nome in adicionados:
            fila["itens"].append({
                "id": _novo_id(),
                "arquivo": nome,
                "titulo": Path(nome).stem,
                "canal": canal,
                "status": "aguardando",
                "mensagem": "",
                "url": "",
                "enviado_em": "",
                "tentativas": 0,
                "tiktok_status": "",
                "tiktok_mensagem": "",
            })
        salvar_fila(fila)
    for nome in adicionados:
        registrar(f"➕ {nome} entrou na fila (canal {canal or 'nenhum'}).")
    resposta = {"adicionados": adicionados}
    if recusados:
        resposta["erro"] = "Formato não aceito: " + ", ".join(recusados)
    return jsonify(resposta)


@app.post("/api/itens/<item_id>")
def editar_item(item_id: str):
    """Troca título ou canal de um item da lista."""
    dados = request.get_json(silent=True) or {}
    with _TRAVA:
        fila = carregar_fila()
        item = next((i for i in fila["itens"] if i["id"] == item_id), None)
        if item is None:
            return jsonify({"erro": "Item não está mais na fila."}), 404
        if "titulo" in dados:
            item["titulo"] = str(dados["titulo"]).strip()[:100]
        if "canal" in dados:
            item["canal"] = str(dados["canal"]).strip()
        salvar_fila(fila)
    return jsonify({"ok": True})


@app.post("/api/itens/<item_id>/mover")
def mover_item(item_id: str):
    """Sobe ou desce um item — a ordem da lista é a ordem de publicação."""
    direcao = int((request.get_json(silent=True) or {}).get("direcao", 0))
    with _TRAVA:
        fila = carregar_fila()
        itens = fila["itens"]
        posicao = next((i for i, item in enumerate(itens) if item["id"] == item_id), None)
        if posicao is None:
            return jsonify({"erro": "Item não está mais na fila."}), 404
        destino = posicao + (1 if direcao > 0 else -1)
        if 0 <= destino < len(itens):
            itens[posicao], itens[destino] = itens[destino], itens[posicao]
            salvar_fila(fila)
    return jsonify({"ok": True})


@app.post("/api/itens/<item_id>/remover")
def remover_item(item_id: str):
    """Tira da lista e apaga o arquivo do HD do servidor."""
    if _ENVIANDO == item_id:
        return jsonify({"erro": "Este vídeo está sendo enviado agora."}), 409
    with _TRAVA:
        fila = carregar_fila()
        item = next((i for i in fila["itens"] if i["id"] == item_id), None)
        if item is None:
            return jsonify({"erro": "Item não está mais na fila."}), 404
        fila["itens"] = [i for i in fila["itens"] if i["id"] != item_id]
        _apagar_video(item["arquivo"], fila, ignorando=item_id)
        salvar_fila(fila)
    registrar(f"➖ {item['arquivo']} saiu da fila e foi apagado do servidor.")
    return jsonify({"ok": True})


@app.post("/api/itens/<item_id>/enviar-agora")
def enviar_agora(item_id: str):
    """Sobe este vídeo já, sem esperar o intervalo (e a contagem reinicia depois)."""
    with _TRAVA:
        fila = carregar_fila()
        item = next((i for i in fila["itens"] if i["id"] == item_id), None)
        if item is None:
            return jsonify({"erro": "Item não está mais na fila."}), 404
        canal = item.get("canal") or ""
        if not canal:
            return jsonify({"erro": "Escolha o canal deste vídeo antes de enviar."}), 400
        if not youtube_browser_upload.is_authorized(canal):
            return jsonify({"erro": f"O canal '{canal}' não está conectado."}), 400
    if not _iniciar_envio(item_id):
        return jsonify({"erro": "Já há um envio em andamento."}), 409
    return jsonify({"ok": True})


@app.post("/api/itens/<item_id>/repetir")
def repetir_item(item_id: str):
    """Devolve um item que falhou para a fila, como se tivesse acabado de entrar."""
    _atualizar_item(item_id, status="aguardando", mensagem="", tentativas=0)
    return jsonify({"ok": True})


@app.post("/api/limpar-enviados")
def limpar_enviados():
    """Tira os já publicados da lista e apaga os arquivos deles do HD."""
    with _TRAVA:
        fila = carregar_fila()
        enviados = [i for i in fila["itens"] if i.get("status") == "enviado"]
        fila["itens"] = [i for i in fila["itens"] if i.get("status") != "enviado"]
        for item in enviados:
            _apagar_video(item["arquivo"], fila)
        salvar_fila(fila)
    if enviados:
        registrar(f"🧹 {len(enviados)} vídeo(s) já publicado(s) foram apagados do servidor.")
    return jsonify({"removidos": len(enviados)})


@app.post("/api/rodando")
def ligar_desligar():
    """Liga ou desliga a fila. Ligando com a lista parada, o primeiro sai já."""
    ligar = bool((request.get_json(silent=True) or {}).get("rodando"))
    with _TRAVA:
        fila = carregar_fila()
        fila["rodando"] = ligar
        salvar_fila(fila)
    registrar("▶ Fila ligada." if ligar else "⏸ Fila desligada.")
    return jsonify({"ok": True})


@app.get("/tiktok-falha.png")
def tela_da_falha_do_tiktok():
    """Retrato da tela do TikTok no instante da última falha.

    O servidor não tem tela para abrir a imagem, e é justamente ela que diz se
    o envio caiu numa tela de login, num captcha ou num botão que mudou de
    lugar — então ela é servida aqui para abrir no navegador.
    """
    caminho = DATA_DIR / tiktok_upload.FALHA_PNG
    if not caminho.exists():
        return jsonify({"erro": "Nenhuma falha do TikTok registrada ainda."}), 404
    return send_file(caminho, mimetype="image/png", max_age=0)


@app.get("/youtube-falha.png")
def tela_da_falha_do_youtube():
    """Retrato da tela do Studio no instante da última falha de envio ao YouTube.

    Mesma ideia da rota acima, para o navegador do YouTube: diz se o envio
    caiu numa tela de login (cookies vencidos), num botão que mudou de lugar,
    ou num aviso de verificação pendente.
    """
    caminho = DATA_DIR / youtube_browser_upload.FALHA_PNG
    if not caminho.exists():
        return jsonify({"erro": "Nenhuma falha do YouTube registrada ainda."}), 404
    return send_file(caminho, mimetype="image/png", max_age=0)


@app.post("/api/testar-tiktok")
def testar_tiktok():
    """Manda só para o TikTok, sem tocar no YouTube — pra testar sem gastar a
    cota da API. Reaproveita um vídeo que já está no servidor (o primeiro da
    fila do canal) com a conta e a legenda configuradas para ele; roda na
    hora e devolve o resultado, sem mexer na fila nem no estado do item.
    """
    canal = str((request.get_json(silent=True) or {}).get("canal") or "").strip()
    if not canal:
        return jsonify({"erro": "Diga de qual canal é o teste."}), 400
    cfg_canal = _config_do_canal(carregar_config(), canal)
    conta = (cfg_canal.get("tiktok_conta") or "").strip()
    if not conta:
        return jsonify({"erro": f"O canal '{canal}' não tem conta do TikTok vinculada."}), 400
    if not tiktok_upload.is_authorized(conta):
        return jsonify({"erro": f"A conta '{conta}' não tem cookies importados."}), 400
    item = next((i for i in carregar_fila()["itens"] if i.get("canal") == canal), None)
    if item is None:
        return jsonify({"erro": f"Nenhum vídeo do canal '{canal}' no servidor para testar."}), 400
    caminho = caminho_do_video(item["arquivo"])
    if not caminho.exists():
        return jsonify({"erro": f"{item['arquivo']} não está mais no HD do servidor."}), 400
    titulo = (item.get("titulo") or caminho.stem)[:100]
    legenda = _legenda_tiktok(titulo, cfg_canal)
    registrar(f"🧪 Teste avulso do TikTok: “{titulo}” (conta {conta})…")

    resultado: dict = {}

    def alvo() -> None:
        try:
            resultado["mensagem"] = tiktok_upload.upload_video(caminho, legenda, conta)
        except Exception as erro:                    # noqa: BLE001
            resultado["erro"] = str(erro)[:500]

    # Roda numa thread nova, nunca reaproveitada: o driver do Playwright deixa
    # estado preso na thread onde rodou, e o waitress reaproveita threads
    # entre requisições — rodar direto na thread da requisição faz a
    # tentativa seguinte trombar com "Sync API inside the asyncio loop".
    thread = threading.Thread(target=alvo, daemon=True, name="teste-tiktok")
    thread.start()
    thread.join()

    if "erro" in resultado:
        registrar(f"⚠️ Teste do TikTok falhou: {resultado['erro']}")
        return jsonify({"erro": resultado["erro"]}), 400
    registrar(f"✅ Teste do TikTok: {resultado['mensagem']}")
    return jsonify({"mensagem": resultado["mensagem"]})


@app.post("/api/adiar")
def adiar():
    """Sorteia de novo o próximo envio de um canal, contando a partir de agora."""
    canal = str((request.get_json(silent=True) or {}).get("canal") or "").strip()
    if not canal:
        return jsonify({"erro": "Diga de qual canal é o horário a adiar."}), 400
    quando = datetime.now() + _sortear_intervalo(_config_do_canal(carregar_config(), canal))
    _marcar_proximo(canal, quando)
    registrar(f"⏳ Canal {canal} adiado para {quando:%d/%m %H:%M}.")
    return jsonify({"proximos": (carregar_fila().get("proximos") or {})})


@app.post("/api/config")
def salvar_configuracoes():
    """Privacidade, faixa de intervalo, ordem e descrição de UM canal."""
    dados = request.get_json(silent=True) or {}
    canal = str(dados.get("canal") or "").strip()
    if not canal:
        return jsonify({"erro": "Diga de qual canal é essa configuração."}), 400
    config = carregar_config()
    canais_cfg = config.setdefault("canais", {})
    atual = _config_do_canal(config, canal)
    if dados.get("privacidade") in ("public", "unlisted", "private"):
        atual["privacidade"] = dados["privacidade"]
    for campo in ("intervalo_min", "intervalo_max"):
        if campo in dados:
            try:
                atual[campo] = max(1, min(2880, int(dados[campo])))
            except (TypeError, ValueError):
                pass
    if atual["intervalo_max"] < atual["intervalo_min"]:        # trocados na digitação
        atual["intervalo_min"], atual["intervalo_max"] = (
            atual["intervalo_max"], atual["intervalo_min"])
    if "ordem_aleatoria" in dados:
        atual["ordem_aleatoria"] = bool(dados["ordem_aleatoria"])
    if "descricao" in dados:
        atual["descricao"] = str(dados["descricao"])[:4900]
    if "tiktok_conta" in dados:
        atual["tiktok_conta"] = str(dados["tiktok_conta"]).strip()
    if "tiktok_legenda" in dados:
        atual["tiktok_legenda"] = str(dados["tiktok_legenda"])[:2200]
    canais_cfg[canal] = atual
    salvar_config(config)
    return jsonify({"config": {"canais": {canal: atual}}})


_PADRAO_COOKIES_YOUTUBE = re.compile(r"youtube_browser_cookies_(.+)\.txt")
_PADRAO_COOKIES_TIKTOK = re.compile(r"tiktok_cookies_(.+)\.txt")


@app.post("/api/credenciais")
def subir_credenciais():
    """Recebe os youtube_browser_cookies_<canal>.txt e tiktok_cookies_<conta>.txt.

    Nenhum dos dois tem login programático — os dois são cookies de uma sessão
    já logada, exportados do navegador (studio.youtube.com e tiktok.com,
    respectivamente) com uma extensão do tipo "Get cookies.txt".
    """
    arquivos = [a for a in request.files.getlist("arquivos") if a and a.filename]
    if not arquivos:
        return jsonify({"erro": "Nenhum arquivo escolhido."}), 400
    aceitos, recusados = [], []
    for enviado in arquivos:
        nome = _nome_seguro(enviado.filename)
        conteudo = enviado.read()
        if not (_PADRAO_COOKIES_YOUTUBE.fullmatch(nome) or _PADRAO_COOKIES_TIKTOK.fullmatch(nome)):
            recusados.append(nome)
            continue
        (DATA_DIR / nome).write_bytes(conteudo)
        (DATA_DIR / nome).chmod(0o600)
        aceitos.append(nome)
        registrar(f"🔑 {nome} recebido.")
    resposta = {"aceitos": aceitos}
    if recusados:
        resposta["erro"] = (
            "Ignorados (o nome tem que ser youtube_browser_cookies_<canal>.txt "
            "ou tiktok_cookies_<conta>.txt): " + ", ".join(recusados))
    return jsonify(resposta)


@app.errorhandler(413)
def arquivo_grande_demais(_erro):
    return jsonify({"erro": "Arquivo maior que o limite de 12 GB por envio."}), 413


# O laço roda no próprio processo do servidor — a instalação usa um processo
# só (waitress com threads), então existe exatamente um laço de envio.
DATA_DIR.mkdir(parents=True, exist_ok=True)
VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
threading.Thread(target=_laco_de_envio, daemon=True, name="fila").start()

registrar("🟢 Publicador iniciado.")
# Sinal indireto de queda: se a máquina/serviço tiver caído, este é o
# primeiro código a rodar quando ele volta (boot, ou o systemd reiniciando
# sozinho com Restart=always) — não existe como avisar *durante* uma queda
# de verdade (nada roda numa máquina desligada), só depois que ela volta.
# Roda numa thread à parte para o SMTP lento/fora do ar não atrasar o
# servidor web subir (o waitress só começa a escutar depois deste módulo
# terminar de carregar).
threading.Thread(
    target=avisar_por_email,
    args=("🟢 Publicador iniciado", f"O serviço acabou de subir (ou reiniciar) em {URL_PUBLICADOR}."),
    daemon=True, name="email-inicio").start()


if __name__ == "__main__":
    # Só para testar na mão; em produção quem serve é o waitress (veja o
    # publicador.service), que não recarrega nem duplica o laço.
    app.run(host="0.0.0.0", port=int(os.environ.get("PUBLICADOR_PORTA", 8080)))
