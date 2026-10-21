"""Envio de vídeos ao Kwai pelo app Android, pilotado por ADB (celular no USB).

O Kwai não tem API pública de publicação — nem app registrado, nem endpoint
aberto. Este módulo faz o que um dedo faria: empurra o arquivo pro celular,
força o Android a indexá-lo na galeria, abre o app do Kwai e toca nos botões
até publicar. Sem Qt e sem dependência de terceiros: só o `adb` do
platform-tools, que já existe tanto no Windows quanto no Arch.

Por que por ADB e não por navegador (como o TikTok e o YouTube): o Kwai
publica pelo app — a versão web não tem o fluxo de envio do criador. E por que
por *toque* e não por coordenada fixa: coordenada quebra na primeira troca de
tela ou de versão do app. Aqui cada passo lê a árvore de elementos do Android
(`uiautomator dump`), procura o botão por texto/descrição/id e só então toca no
centro dele. Quando não acha, falha dizendo exatamente o que estava na tela —
não toca no escuro.

Uso pela linha de comando (é assim que se testa numa máquina nova):

    python kwai_upload.py --dispositivo          # lista os celulares no USB
    python kwai_upload.py --diagnostico          # confere tudo o que o envio precisa, de uma vez
    python kwai_upload.py --tela                 # despeja a tela atual (para achar seletores)
    python kwai_upload.py --mapear               # walkthrough interativo: abre o app e você decide cada toque
    python kwai_upload.py video.mp4 "Título" --legenda "#hashtags"
    python kwai_upload.py video.mp4 --seco       # faz tudo, menos tocar em Publicar

Uso pelo publicador (o mesmo contrato do tiktok_upload.py):

    import kwai_upload
    if kwai_upload.disponivel():                  # tem adb e um celular autorizado?
        mensagem = kwai_upload.enviar_para_kwai(caminho, legenda)

Onde roda: a máquina que tem o celular no cabo. Hoje é o Windows do Estúdio;
amanhã é o servidor Arch com um celular fixo no USB. Nada aqui depende de ser
Windows ou Linux — só de o `adb` enxergar o aparelho.

Configuração por variável de ambiente (todas opcionais):

    KWAI_ADB          caminho do binário adb (se não estiver no PATH)
    KWAI_SERIAL       serial do aparelho, quando há mais de um no USB
    KWAI_PACOTE       pacote do app (padrão: detecta sozinho)
    KWAI_ESPERA_MS    quanto esperar cada tela, em ms (padrão 60000)
    KWAI_ENVIO_MS     quanto esperar o envio subir, em ms (padrão 900000)
    KWAI_MANTER       "1" guarda o vídeo no celular depois de publicar
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────────
#  Ajustes e catálogo de seletores
# ──────────────────────────────────────────────────────────────────

ESPERA_MS = int(os.environ.get("KWAI_ESPERA_MS") or 60_000)
ENVIO_MS = int(os.environ.get("KWAI_ENVIO_MS") or 900_000)
MANTER_NO_CELULAR = os.environ.get("KWAI_MANTER") == "1"
PASTA_NO_CELULAR = "/sdcard/DCIM/Camera"

# Pacotes possíveis do Kwai, na ordem em que se procura. O internacional é
# com.kwai.video; com.smile.gifmaker é o Kuaishou chinês (some no Brasil, mas
# aparece em aparelho importado) e com.kwai.videoeditor é o editor separado.
PACOTES_KWAI = ("com.kwai.video", "com.kwai.videoeditor", "com.smile.gifmaker")

# Cada passo da tela é uma lista de candidatos: texto visível, descrição de
# acessibilidade e pedaço de id. Basta um bater. São listas generosas de
# propósito — o app muda de versão e de idioma, e três candidatos a mais
# custam menos que um bot que quebra numa atualização. A ordem importa: o
# primeiro que aparecer na tela vence.
SELETORES = {
    # Botão "+" da barra de baixo, na tela inicial do app.
    "botao_publicar_inicio": {
        "descs": ["Publicar", "Publicar vídeo", "Postar", "Criar", "Criar vídeo",
                  "Adicionar", "Nova publicação", "Publish", "Post", "Create", "发布"],
        "ids": ["publish", "plus", "add", "create", "camera", "post"],
        "textos": ["+"],
    },
    # Aba do seletor de mídia que mostra os vídeos.
    "aba_videos": {
        "textos": ["Vídeos", "Videos", "Galeria", "Tudo", "Todos", "Vídeo", "Gallery", "All"],
        "descs": ["Vídeos", "Videos", "Galeria"],
    },
    # Um item qualquer da grade de vídeos do celular (o último empurrado é o
    # primeiro da grade, então basta tocar no primeiro que aparecer).
    "item_de_video": {
        "descs": ["Vídeo", "Video", "vídeo", "video", "MP4"],
        "ids": ["video_item", "thumb", "grid_item", "media_item", "cell"],
    },
    # Seta de "seguir" do editor (galeria → editor → legenda).
    "avancar": {
        "textos": ["Avançar", "Próximo", "Próxima", "Continuar", "Seguinte", "Next", "下一步"],
        "descs": ["Avançar", "Próximo", "Próxima", "Continuar", "Next", "下一步"],
        "ids": ["next", "arrow", "avancar", "forward", "confirm"],
    },
    # Campo de texto da legenda na tela de publicação.
    "campo_legenda": {
        "descs": ["Diga algo", "Adicione uma descrição", "Escreva uma legenda",
                  "Digite algo", "Descrição", "Legenda", "Description", "Caption", "Say something"],
        "ids": ["desc", "caption", "input", "edit", "comment"],
        "classes": ["android.widget.EditText"],
    },
    # Botão final.
    "publicar": {
        "textos": ["Publicar", "Postar", "Publicar agora", "Postar agora", "Enviar", "Publish", "Post"],
        "descs": ["Publicar", "Postar", "Publish", "Post"],
        "ids": ["publish", "post_btn", "btn_post", "done", "submit"],
    },
    # Sinais de que o app está deslogado (para falhar explicando, e não tocar
    # no escuro até estourar o tempo).
    "tela_de_login": {
        "textos": ["Entrar", "Fazer login", "Login", "Continuar com", "Entrar com",
                   "Log in", "Sign in", "Sign up"],
    },
}


class KwaiError(RuntimeError):
    """Falha no envio — sempre com uma frase que diga o que fazer a seguir."""


def _log_padrao(mensagem: str) -> None:
    print(mensagem, flush=True)


# ──────────────────────────────────────────────────────────────────
#  ADB
# ──────────────────────────────────────────────────────────────────

def _achar_adb() -> str:
    """Caminho do `adb`: variável de ambiente, PATH, e os cantos onde ele costuma
    ficar quando foi instalado por winget/SDK (Windows) ou pelos pacotes do Arch.
    """
    if os.environ.get("KWAI_ADB"):
        return os.environ["KWAI_ADB"]
    no_path = shutil.which("adb")
    if no_path:
        return no_path
    candidatos = [
        Path.home() / "AppData/Local/Microsoft/WinGet/Packages",
        Path("C:/platform-tools"),
        Path.home() / "Android/Sdk/platform-tools",
        Path("/usr/bin"),
        Path("/usr/local/bin"),
    ]
    for pasta in candidatos:
        if not pasta.exists():
            continue
        if pasta.name == "platform-tools":
            exe = pasta / ("adb.exe" if os.name == "nt" else "adb")
            if exe.exists():
                return str(exe)
        else:
            for exe in pasta.rglob("platform-tools/adb.exe" if os.name == "nt" else "platform-tools/adb"):
                return str(exe)
    raise KwaiError(
        "adb não encontrado. Instale o platform-tools (Android SDK) ou aponte o "
        "caminho dele em KWAI_ADB.")


class Adb:
    """Uma sessão de adb presa a um aparelho (ou à escolha automática, quando
    só há um no USB).
    """

    def __init__(self, serial: str | None = None, log=_log_padrao):
        self.bin = _achar_adb()
        self.log = log
        self.serial = serial or os.environ.get("KWAI_SERIAL") or None

    # -- baixo nível -------------------------------------------------

    def _base(self) -> list[str]:
        return [self.bin] + (["-s", self.serial] if self.serial else [])

    def rodar(self, *args: str, timeout: float | None = 120,
              binario: bool = False) -> str | bytes:
        try:
            r = subprocess.run(self._base() + list(args), capture_output=True,
                               timeout=timeout)
        except FileNotFoundError as erro:
            raise KwaiError(f"não consegui executar o adb: {erro}") from erro
        except subprocess.TimeoutExpired as erro:
            raise KwaiError(f"o adb demorou demais para responder ({args[0]}).") from erro
        if binario:
            return r.stdout
        saida = r.stdout.decode("utf-8", "replace")
        erro = r.stderr.decode("utf-8", "replace")
        if r.returncode != 0:
            raise KwaiError(f"adb {' '.join(args[:2])} falhou: {(erro or saida).strip()[:400]}")
        return saida

    def shell(self, comando: str, timeout: float | None = 120) -> str:
        """Roda um comando no Android (uma string só, com as aspas já pensadas
        para o shell do aparelho — veja `_aspas`).
        """
        try:
            return str(self.rodar("shell", comando, timeout=timeout))
        except KwaiError as erro:
            raise KwaiError(_traduzir_erro(str(erro))) from erro

    # -- aparelho ---------------------------------------------------

    def resolver(self) -> str:
        """Escolhe o aparelho. Um só: usa ele. Vários: exige KWAI_SERIAL."""
        saida = str(self.rodar("devices", "-l"))
        prontos, outros = [], []
        for linha in saida.splitlines()[1:]:
            partes = linha.split()
            if len(partes) < 2:
                continue
            serial, estado = partes[0], partes[1]
            (prontos if estado == "device" else outros).append((serial, estado))
        if self.serial:
            if any(s == self.serial for s, _ in prontos):
                return self.serial
            estados = ", ".join(f"{s} ({e})" for s, e in prontos + outros) or "nenhum aparelho"
            raise KwaiError(f"KWAI_SERIAL={self.serial} não está autorizado. No USB agora: {estados}.")
        if not prontos and outros:
            serial, estado = outros[0]
            dica = ("autorize a depuração USB na tela do celular" if estado == "unauthorized"
                    else "reconecte o cabo")
            raise KwaiError(f"o celular {serial} está no USB mas não liberado ({estado}) — {dica}.")
        if not prontos:
            raise KwaiError("nenhum celular no USB. Ligue a Depuração USB e confirme "
                            "a autorização na tela do aparelho.")
        if len(prontos) > 1:
            raise KwaiError("há mais de um celular no USB: escolha um com KWAI_SERIAL "
                            f"({', '.join(s for s, _ in prontos)}).")
        self.serial = prontos[0][0]
        return self.serial

    def acordar(self) -> None:
        self.shell("input keyevent KEYCODE_WAKEUP")
        time.sleep(0.6)

    def bloqueado(self) -> bool:
        """True se a tela ainda está no bloqueio — aí não há toque que resolva:
        senha/PIN não se digita por adb (e nem se deveria).
        """
        try:
            d = self.shell("dumpsys window policy")
        except KwaiError:
            return False
        for chave in ("mShowingLockscreen=true", "isStatusBarKeyguard=true",
                      "mDreamingLockscreen=true", "mKeyguardShowing=true"):
            if chave in d:
                return True
        return False

    def pacote_instalado(self) -> str | None:
        try:
            lista = self.shell("pm list packages")
        except KwaiError:
            return None
        instalados = {linha.split(":", 1)[1].strip() for linha in lista.splitlines() if ":" in linha}
        for pacote in ([os.environ["KWAI_PACOTE"]] if os.environ.get("KWAI_PACOTE") else []) + list(PACOTES_KWAI):
            if pacote in instalados:
                return pacote
        return None

    def em_primeiro_plano(self) -> str:
        for comando in ("dumpsys window displays", "dumpsys activity activities"):
            try:
                d = self.shell(comando)
            except KwaiError:
                continue
            for chave in ("mCurrentFocus", "mResumedActivity", "topResumedActivity"):
                for linha in d.splitlines():
                    if chave in linha and "/" in linha:
                        achado = re.search(r"([A-Za-z0-9_.]+)/", linha)
                        if achado:
                            return achado.group(1)
        return ""

    def abrir_app(self, pacote: str) -> None:
        self.shell(f"monkey -p {pacote} -c android.intent.category.LAUNCHER 1")

    def fechar_app(self, pacote: str) -> None:
        self.shell(f"am force-stop {pacote}")


def _aspas(texto: str) -> str:
    """Deixa o texto seguro para o shell do Android.

    O `adb shell` manda o resto dos argumentos como uma linha de comando só,
    interpretada pelo shell do aparelho. Sem isto, qualquer legenda com espaço
    vira três argumentos e o Kwai recebe só a primeira palavra — e um `#` de
    hashtag sem aspas ainda corta o resto da linha.
    """
    return "'" + texto.replace("'", "'\\''") + "'"


def _traduzir_erro(mensagem: str) -> str:
    """Troca o erro cru do Android pela frase que diz o que fazer.

    O caso que importa é o do aparelho da Xiaomi: com a "Depuração USB" ligada
    mas sem a "Depuração USB (configurações de segurança)", o system_server
    recusa todo toque simulado com um SecurityException de INJECT_EVENTS — e o
    sintoma é um bot que lê a tela direitinho e não mexe nada, que é o pior de
    diagnosticar sem essa dica.
    """
    if "INJECT_EVENTS" in mensagem or "Injecting input events" in mensagem:
        return ("este celular não está aceitando toque simulado pelo USB. É o aparelho "
                "da Xiaomi sem a \"Depuração USB (configurações de segurança)\": ligue "
                "essa opção em Configurações → Configurações adicionais → Opções do "
                "desenvolvedor e reconecte o cabo.")
    return mensagem


# ──────────────────────────────────────────────────────────────────
#  Leitura e toque na tela
# ──────────────────────────────────────────────────────────────────

_BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")


class Tela:
    """A árvore de elementos do Android agora, com busca por texto e toque.

    O `uiautomator dump` é lento (1–2 s por leitura), então a tela é lida de
    novo a cada tentativa, e não uma vez só: entre um toque e o próximo o app
    já trocou de página.
    """

    def __init__(self, adb: Adb):
        self.adb = adb
        self.raiz: ET.Element | None = None
        self.pais: dict[ET.Element, ET.Element] = {}
        self.tamanho = self._tamanho()

    def _tamanho(self) -> tuple[int, int]:
        try:
            d = self.adb.shell("wm size")
        except KwaiError:
            return (1080, 2400)
        achado = re.findall(r"(\d+)x(\d+)", d)
        return (int(achado[-1][0]), int(achado[-1][1])) if achado else (1080, 2400)

    def ler(self) -> ET.Element:
        """Lê a tela. Tenta pelo /dev/tty (mais rápido) e cai para arquivo —
        alguns aparelhos não deixam o uiautomator escrever no tty.
        """
        texto = ""
        try:
            texto = self.adb.shell("uiautomator dump /dev/tty")
        except KwaiError:
            texto = ""
        if "<hierarchy" not in texto:
            self.adb.shell("uiautomator dump /sdcard/kwai_dump.xml")
            texto = self.adb.shell("cat /sdcard/kwai_dump.xml")
        inicio, fim = texto.find("<hierarchy"), texto.rfind("</hierarchy>")
        if inicio < 0 or fim < 0:
            raise KwaiError("não consegui ler a tela do celular (uiautomator não devolveu nada).")
        try:
            self.raiz = ET.fromstring(texto[inicio:fim + len("</hierarchy>")])
        except ET.ParseError as erro:
            raise KwaiError(f"a tela do celular veio em formato inesperado: {erro}") from erro
        self.pais = {filho: pai for pai in self.raiz.iter() for filho in pai}
        return self.raiz

    # -- consulta ---------------------------------------------------

    def _texto_de(self, e: ET.Element) -> str:
        return " ‖ ".join(x for x in ((e.get("text") or "").strip(),
                                      (e.get("content-desc") or "").strip()) if x)

    def elementos(self) -> list[ET.Element]:
        return list((self.raiz if self.raiz is not None else self.ler()).iter())

    def textos_visiveis(self) -> list[str]:
        vistos = []
        for e in self.elementos():
            t = self._texto_de(e)
            if t and t not in vistos:
                vistos.append(t)
        return vistos

    def achar(self, *, textos=(), descs=(), ids=(), classes=(), contem=True,
              clicavel=False) -> ET.Element | None:
        """Procura o primeiro elemento que combine com os candidatos.

        `contem=True` compara por pedaço (o id real é sempre prefixado pelo
        pacote e a versão do app muda o resto), e a comparação ignora acento e
        maiúscula — "Publicar" tem que achar "publicar" e "Publicar vídeo".
        """
        def chave(s: str) -> str:
            sem_acento = unicodedata.normalize("NFKD", s or "")
            return "".join(c for c in sem_acento if not unicodedata.combining(c)).lower()

        def bate(valor: str, candidatos) -> bool:
            v = chave(valor)
            return any(chave(c) in v if contem else chave(c) == v for c in candidatos)

        for e in self.elementos():
            if textos and not bate(e.get("text") or "", textos):
                continue
            if descs and not bate(e.get("content-desc") or "", descs):
                continue
            if ids and not any(c.lower() in (e.get("resource-id") or "").lower() for c in ids):
                continue
            if classes and not any(c.lower() in (e.get("class") or "").lower() for c in classes):
                continue
            if clicavel and e.get("enabled") == "false":
                continue
            return e
        return None

    def esperar(self, *, segundos: float | None = None, log=None, o_que="a tela do Kwai",
                **busca) -> ET.Element | None:
        """Relê a tela até o elemento aparecer ou o tempo acabar."""
        limite = time.time() + (segundos if segundos is not None else ESPERA_MS / 1000)
        while True:
            self.ler()
            if self.achar(**busca) is not None:
                # Um respiro: o elemento pode ter aparecido com o app ainda
                # animando a transição, e o toque cai no vazio.
                time.sleep(0.8)
                self.ler()
                if self.achar(**busca) is not None:
                    return self.achar(**busca)
            if log:
                log(f"   aguardando {o_que}…")
            if time.time() >= limite:
                return None
            time.sleep(1.5)

    # -- toque ------------------------------------------------------

    def centro(self, e: ET.Element) -> tuple[int, int]:
        achado = _BOUNDS.search(e.get("bounds") or "")
        if not achado:
            raise KwaiError("o elemento não tem posição na tela (bounds vazio).")
        x1, y1, x2, y2 = (int(v) for v in achado.groups())
        return (x1 + x2) // 2, (y1 + y2) // 2

    def clicavel_de(self, e: ET.Element) -> ET.Element:
        """O nó com o texto quase nunca é o que recebe o toque — o clique está
        no pai (o item da lista, o botão em volta). Sobe até achar.
        """
        atual = e
        for _ in range(6):
            if atual.get("clickable") == "true":
                return atual
            pai = self.pais.get(atual)
            if pai is None:
                break
            atual = pai
        return e

    def tocar(self, e: ET.Element, log=None, nome: str = "") -> tuple[int, int]:
        alvo = self.clicavel_de(e)
        x, y = self.centro(alvo)
        if log:
            log(f"   toquei em {nome or self._texto_de(alvo) or 'elemento'} ({x},{y})")
        self.adb.shell(f"input tap {x} {y}")
        time.sleep(1.2)
        return x, y

    def despejar(self, destino: Path | None = None) -> Path | None:
        """Salva o XML da tela — é o que se olha quando um passo não acha o
        botão (e o que vai pro log de falha).
        """
        if destino is None:
            return None
        try:
            self.ler()
            destino.write_text(ET.tostring(self.raiz, encoding="unicode"),
                               encoding="utf-8")
            return destino
        except (KwaiError, OSError):
            return None

    def imprimir(self, log=_log_padrao) -> None:
        """Lista o que está na tela agora: texto, id, descrição e posição.

        É a ferramenta de mapeamento: quando um passo do fluxo não achar o
        botão, é isto que se lê para descobrir como o app chama aquele botão
        nesta versão.
        """
        self.ler()
        log(f"Tela {self.tamanho[0]}x{self.tamanho[1]} — {datetime.now():%H:%M:%S}")
        for i, e in enumerate(self.elementos()):
            if e.get("class") == "android.view.View":
                continue
            x, y = self.centro(e) if _BOUNDS.search(e.get("bounds") or "") else (0, 0)
            texto = self._texto_de(e) or ""
            if not texto and not e.get("resource-id"):
                continue
            marcas = []
            if e.get("clickable") == "true":
                marcas.append("clicável")
            if e.get("scrollable") == "true":
                marcas.append("rolável")
            if e.get("enabled") == "false":
                marcas.append("desabilitado")
            log(f"  [{i:>3}] {texto or '(sem texto)':<40} "
                f"{e.get('resource-id') or '':<50} {x:>5},{y:<5} {' '.join(marcas)}")


# ──────────────────────────────────────────────────────────────────
#  Cópia do vídeo e índice da galeria
# ──────────────────────────────────────────────────────────────────

def _nome_no_celular(caminho: Path) -> str:
    """Nome único do arquivo no celular.

    Único porque o app abre a galeria pela ordem de gravação: o arquivo
    recém-chegado tem que ser o primeiro da grade, e um nome repetido faria o
    Android reaproveitar a linha antiga do índice.
    """
    limpo = re.sub(r"[^A-Za-z0-9._-]+", "_", caminho.stem)[:40].strip("_")
    return f"kwai_{datetime.now():%y%m%d_%H%M%S}_{limpo or 'video'}_{uuid.uuid4().hex[:6]}.mp4"


def _linha_na_galeria(adb: Adb, nome: str) -> tuple[str, str] | None:
    """A linha deste arquivo no MediaStore — (id, caminho) — ou None se não há.

    Sem cláusula `--where` de propósito: o adb no Windows come as aspas de um
    argumento com espaços, e a cláusula chega quebrada no aparelho ("Unsupported
    argument: LIKE" — medido neste POCO). Já o `--projection` é separado por
    dois-pontos, não por vírgula: `_id,_data` é lido como uma coluna só, que
    não existe. Trazer as duas colunas e filtrar aqui custa uma lista de
    algumas centenas de linhas e não tem como dar errado.
    """
    saida = adb.shell("content query --uri content://media/external/video/media "
                      "--projection _id:_data")
    for linha in saida.splitlines():
        if nome not in linha:
            continue
        achado = re.search(r"_id=(\d+)\s+_data=(\S+)", linha)
        if achado:
            return achado.group(1), achado.group(2)
    return None


def _garantir_indice(adb: Adb, remoto: str, real: str, log) -> bool:
    """Confere que a galeria do Android enxerga o arquivo que foi empurrado.

    No HyperOS/MIUI isto é conferência e não truque: medido neste POCO M6 Pro
    5G (Android 14), o arquivo aparece no MediaStore **imediatamente depois do
    push** — o sistema indexa sozinho. Só quando isso não acontece (outro
    aparelho, outro fabricante) é que o aviso deprecado
    `MEDIA_SCANNER_SCAN_FILE` entra, tentado no caminho real
    (/storage/emulated/0/...) e depois no atalho (/sdcard/...), e por último
    abrir o vídeo no player do sistema, que costuma indexar só de abrir. Cada
    tentativa é verificada no MediaStore — nenhuma é adivinhação.
    """
    if _linha_na_galeria(adb, remoto.rsplit("/", 1)[-1]):
        return True
    for caminho in (real, remoto):
        log(f"   a galeria não indexou sozinha; avisando pelo caminho {caminho}…")
        for extra in ("", " --receiver-include-background"):
            try:
                adb.shell("am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE"
                          f"{extra} -d file://{caminho}")
            except KwaiError:
                pass
        for _ in range(10):
            if _linha_na_galeria(adb, remoto.rsplit("/", 1)[-1]):
                return True
            time.sleep(1)
    log("   abrindo o vídeo no player do sistema, que costuma indexar de abrir.")
    try:
        adb.shell(f"am start -a android.intent.action.VIEW -d file://{real} -t video/*")
        time.sleep(6)
        adb.shell("input keyevent KEYCODE_BACK")
    except KwaiError:
        pass
    return _linha_na_galeria(adb, remoto.rsplit("/", 1)[-1]) is not None


def _copiar(adb: Adb, caminho: Path, log) -> str:
    """Empurra o vídeo pro celular e devolve o caminho remoto. Confere que o
    arquivo chegou inteiro comparando o tamanho — um push cortado no meio
    abriria no Kwai como um vídeo pela metade.
    """
    nome = _nome_no_celular(caminho)
    remoto = f"{PASTA_NO_CELULAR}/{nome}"
    tamanho_local = caminho.stat().st_size
    log(f"   copiando {caminho.name} ({tamanho_local / 1048576:.1f} MB) para o celular…")
    adb.shell(f'mkdir -p {PASTA_NO_CELULAR}')
    adb.rodar("push", str(caminho), remoto, timeout=None)
    remoto_tamanho = adb.shell(f"stat -c %s {remoto}").strip()
    if remoto_tamanho.isdigit() and int(remoto_tamanho) != tamanho_local:
        raise KwaiError(f"a cópia para o celular ficou incompleta "
                        f"({remoto_tamanho} de {tamanho_local} bytes).")
    return remoto


def _apagar_do_celular(adb: Adb, remoto: str, nome: str, log) -> None:
    """Tira o vídeo do celular depois de publicado.

    Só apaga o arquivo: medido neste aparelho, o próprio provedor de mídia
    remove a linha da galeria ao ver o arquivo fora do disco. O `content
    delete` por id é a rede de segurança, para o aparelho em que isso não
    acontecer.
    """
    try:
        adb.shell(f"rm -f {remoto}")
    except KwaiError as erro:
        log(f"   não consegui apagar o vídeo do celular: {erro}")
        return
    for _ in range(3):
        linha = _linha_na_galeria(adb, nome)
        if linha is None:
            log("   vídeo apagado do celular.")
            return
        try:
            adb.shell(f"content delete --uri content://media/external/video/media/{linha[0]}")
        except KwaiError:
            pass
        time.sleep(2)
    log("   o vídeo saiu do celular; a linha na galeria deve sumir sozinha.")


# ──────────────────────────────────────────────────────────────────
#  Texto na legenda
# ──────────────────────────────────────────────────────────────────

def _teclado_adb(adb: Adb) -> str | None:
    """IME do ADBKeyboard, se estiver instalado.

    O `input text` só digita ASCII: acento e emoji simplesmente não saem. O
    ADBKeyboard é um teclado de verdade (um apk pequeno, open source) que
    recebe o texto por broadcast e digita qualquer coisa. Se ele estiver no
    aparelho, é ele que digita; se não estiver, o código digita pelo `input
    text` e avisa o que vai perder.
    """
    try:
        imes = adb.shell("ime list -s")
    except KwaiError:
        return None
    for linha in imes.splitlines():
        if "adbkeyboard" in linha.lower():
            return linha.strip()
    return None


def _digitar(adb: Adb, texto: str, log) -> None:
    if not texto:
        return
    ime = _teclado_adb(adb)
    if ime:
        anterior = adb.shell("settings get secure default_input_method").strip()
        try:
            adb.shell(f"ime set {ime}")
            time.sleep(1.5)
            adb.shell(f"am broadcast -a ADB_INPUT_TEXT --es msg {_aspas(texto)}")
            time.sleep(1.2)
        finally:
            if anterior and anterior != "null":
                try:
                    adb.shell(f"ime set {anterior}")
                except KwaiError:
                    pass
        return
    sem_ascii = [c for c in texto if ord(c) > 127]
    if sem_ascii:
        log(f"   ⚠️ sem o ADBKeyboard no celular, acentos e emoji não são digitados "
            f"({''.join(sorted(set(sem_ascii)))[:12]}…) — a legenda sai sem eles.")
    # O `input text` não aceita `%` solto nem quebras de linha.
    limpo = texto.replace("%", "").replace("\n", " ").strip()
    for parte in [limpo[i:i + 300] for i in range(0, len(limpo), 300)]:
        adb.shell(f"input text {_aspas(parte)}")
        time.sleep(0.8)


# ──────────────────────────────────────────────────────────────────
#  O fluxo, de ponta a ponta
# ──────────────────────────────────────────────────────────────────

def _primeiro_item_da_grade(tela: Tela) -> ET.Element | None:
    """O item mais acima e mais à esquerda da grade de vídeos.

    A grade do Kwai mostra o vídeo mais recente primeiro, que é justamente o
    que acabou de chegar — então "o primeiro" é a resposta certa, e é uma
    regra que não depende de id nem de texto do app.
    """
    candidatos = []
    for e in tela.elementos():
        desc = (e.get("content-desc") or "").lower()
        rid = (e.get("resource-id") or "").lower()
        classes = (e.get("class") or "").lower()
        if not (e.get("clickable") == "true" or "video" in desc or "video" in rid):
            continue
        if "video" not in desc and "video" not in rid and "image" not in classes:
            continue
        achado = _BOUNDS.search(e.get("bounds") or "")
        if not achado:
            continue
        x1, y1, _, _ = (int(v) for v in achado.groups())
        if y1 < tela.tamanho[1] * 0.12:          # ignora a barra de status
            continue
        candidatos.append((y1, x1, e))
    if not candidatos:
        return None
    return sorted(candidatos, key=lambda c: (c[0], c[1]))[0][2]


def enviar_para_kwai(caminho: Path | str, legenda: str = "", *,
                     serial: str | None = None, publicar: bool = True,
                     manter_no_celular: bool | None = None,
                     log=_log_padrao) -> str:
    """Publica um vídeo no Kwai e devolve uma frase de confirmação.

    `publicar=False` faz o fluxo inteiro e para na tela de publicação, com a
    legenda já escrita, para alguém conferir e tocar em Publicar — é o modo
    de teste seguro enquanto os seletores ainda não foram confirmados no
    aparelho de verdade.

    Levanta `KwaiError` com o motivo e, nos casos de tela inesperada, o
    caminho do XML da tela (que é o que se lê para descobrir o seletor novo).
    """
    caminho = Path(caminho)
    if not caminho.exists():
        raise KwaiError(f"o vídeo não está mais no disco: {caminho}")
    manter = MANTER_NO_CELULAR if manter_no_celular is None else manter_no_celular

    adb = Adb(serial, log=log)
    serial_usado = adb.resolver()
    log(f"📱 Kwai: celular {serial_usado}.")

    pacote = adb.pacote_instalado()
    if not pacote:
        raise KwaiError("o app do Kwai não está instalado neste celular "
                        f"(procurei por {', '.join(PACOTES_KWAI)}).")

    log("   acordando o celular…")
    adb.acordar()
    if adb.bloqueado():
        raise KwaiError("o celular está com a tela bloqueada: desbloqueie e rode de novo "
                        "(não dá para digitar a senha por adb).")
    adb.fechar_app(pacote)

    remoto = _copiar(adb, caminho, log)
    nome = remoto.rsplit("/", 1)[-1]
    real = remoto.replace("/sdcard", "/storage/emulated/0")
    if not _garantir_indice(adb, remoto, real, log):
        raise KwaiError("o vídeo chegou ao celular mas a galeria não o indexou — sem isso "
                        "o Kwai nem o enxerga. Verifique se o celular ficou desbloqueado "
                        "durante a cópia e tente de novo.")
    log("   vídeo na galeria do celular.")

    tela = Tela(adb)
    try:
        log("   abrindo o Kwai…")
        adb.abrir_app(pacote)
        time.sleep(8)
        if adb.em_primeiro_plano() != pacote:
            time.sleep(8)
        tela.ler()
        if tela.achar(**SELETORES["tela_de_login"]):
            raise KwaiError("o Kwai está pedindo login neste celular — entre na conta uma vez, "
                            "no aparelho, e rode de novo.")

        # 1) "+" na tela inicial
        botao = tela.esperar(log=log, o_que="o botão de publicar do Kwai",
                             **SELETORES["botao_publicar_inicio"])
        if botao is None:
            raise KwaiError("não achei o botão \"+\" do Kwai na tela inicial. "
                            "Rode `python kwai_upload.py --tela` com o app aberto e veja o que aparece.")
        tela.tocar(botao, log=log, nome="botão publicar")

        # 2) a grade de mídia
        tela.esperar(log=log, o_que="a galeria de vídeos", segundos=ESPERA_MS / 1000,
                     descs=SELETORES["item_de_video"]["descs"][:2])
        if tela.achar(**SELETORES["aba_videos"]) is not None:
            tela.tocar(tela.achar(**SELETORES["aba_videos"]), log=log, nome="aba de vídeos")
        item = tela.esperar(log=log, o_que="o vídeo recém-copiado na grade",
                            **SELETORES["item_de_video"])
        if item is None:
            item = _primeiro_item_da_grade(tela)
        if item is None:
            raise KwaiError("a galeria do Kwai abriu mas não achei os itens de vídeo nela. "
                            "Rode `--tela` nessa tela para ver como ela vem neste aparelho.")
        tela.tocar(item, log=log, nome="vídeo mais recente")

        # 3) editor → legenda
        avancar = tela.esperar(log=log, o_que="a seta de avançar do editor",
                               **SELETORES["avancar"])
        if avancar is None:
            raise KwaiError("o vídeo abriu no editor mas não achei a seta de avançar.")
        tela.tocar(avancar, log=log, nome="avançar")

        # 4) legenda
        campo = tela.esperar(log=log, o_que="o campo da legenda", **SELETORES["campo_legenda"])
        if campo is None:
            raise KwaiError("não achei o campo de legenda na tela de publicação.")
        tela.tocar(campo, log=log, nome="campo da legenda")
        _digitar(adb, legenda, log)
        adb.shell("input keyevent KEYCODE_BACK")   # fecha o teclado, senão ele cobre o botão
        time.sleep(1.5)

        # 5) publicar
        botao_final = None
        if publicar:
            botao_final = tela.esperar(log=log, o_que="o botão de publicar",
                                       **SELETORES["publicar"])
            if botao_final is None:
                raise KwaiError("cheguei na tela de publicação mas não achei o botão "
                                "\"Publicar\" — o vídeo está lá, pronto, esperando você.")
            tela.tocar(botao_final, log=log, nome="Publicar")
            if not _esperar_envio(adb, pacote, log):
                raise KwaiError("toquei em Publicar mas o Kwai não voltou para o feed em "
                                f"{ENVIO_MS // 60000} min — confira no celular se o vídeo saiu.")
        else:
            log("   modo ensaio: parei aqui, com a legenda escrita. Publique você.")
        return (f"Kwai: publicado no celular {serial_usado}"
                if publicar else "Kwai: pronto para publicar (modo ensaio)")
    except KwaiError as erro:
        alvo = Path("kwai_falha.xml")
        if tela.despejar(alvo):
            log(f"   (tela salva em {alvo} para conferência)")
        raise
    finally:
        if publicar and not manter:
            _apagar_do_celular(adb, remoto, nome, log)


def _esperar_envio(adb: Adb, pacote: str, log) -> bool:
    """Espera o envio terminar de verdade.

    O sinal de sucesso é o app sair da tela de publicação e voltar ao feed —
    melhor que procurar um "postado!" que muda de texto e de tempo em cada
    versão. Cada leitura também registra em que ponto o envio está, para o
    histórico do publicador dizer alguma coisa enquanto espera.
    """
    limite = time.time() + ENVIO_MS / 1000
    ultima = ""
    while time.time() < limite:
        time.sleep(5)
        try:
            tela = Tela(adb)
            tela.ler()
        except KwaiError:
            continue
        avisos = [t for t in tela.textos_visiveis()
                  if any(p in t.lower() for p in ("enviando", "publicando", "processando",
                                                  "%", "upload", "aguarde"))]
        if avisos and avisos[0] != ultima:
            ultima = avisos[0]
            log(f"   Kwai: {ultima}")
        if adb.em_primeiro_plano() == pacote:
            if tela.achar(textos=SELETORES["publicar"]["textos"]) is None:
                return True
        else:
            return True
    return False


def disponivel() -> tuple[bool, str]:
    """O publicador pergunta isto antes de tentar: tem adb e um celular pronto?

    Devolve (pode_enviar, explicação) — a explicação vai pro histórico, para
    uma falha dizer o que consertar em vez de só "não deu".
    """
    try:
        adb = Adb()
        adb.resolver()
    except KwaiError as erro:
        return False, str(erro)
    pacote = adb.pacote_instalado()
    if not pacote:
        return False, "o Kwai não está instalado no celular conectado."
    return True, f"celular {adb.serial} com o Kwai instalado."


# ──────────────────────────────────────────────────────────────────
#  Ferramentas de linha de comando
# ──────────────────────────────────────────────────────────────────

def diagnosticar(log=_log_padrao) -> bool:
    """Confere de uma vez tudo o que um envio precisa, e diz o que falta.

    Existe porque cada obstáculo deste fluxo só aparece no meio de uma
    publicação, depois de já ter copiado centenas de MB — e cada um tem uma
    causa diferente e distante do sintoma: o celular no USB mas não autorizado,
    o app deslogado, a tela bloqueada, o toque recusado pela Xiaomi, o
    ADBKeyboard ausente (que não impede publicar, mas come os acentos). Melhor
    ouvir tudo de uma vez, com o celular na mão, do que descobrir um por um.

    Devolve True se o aparelho está pronto para publicar (o ADBKeyboard não
    entra nessa conta: a falta dele só custa acento na legenda).
    """
    pronto = True

    def falha(mensagem: str) -> None:
        nonlocal pronto
        pronto = False
        log(f"❌ {mensagem}")

    try:
        adb = Adb(log=log)
        log(f"✅ adb: {adb.bin}")
    except KwaiError as erro:
        log(f"❌ {erro}")
        return False

    try:
        log(f"✅ celular: {adb.resolver()}")
    except KwaiError as erro:
        falha(str(erro))
        return False

    try:
        modelo = adb.shell("getprop ro.product.model").strip()
        versao = adb.shell("getprop ro.build.version.release").strip()
        fabricante = adb.shell("getprop ro.product.manufacturer").strip()
        log(f"✅ aparelho: {fabricante} {modelo} (Android {versao})")
        if fabricante.lower() in ("xiaomi", "redmi", "poco"):
            log("   ℹ️ aparelho da Xiaomi: o toque simulado depende da "
                "\"Depuração USB (configurações de segurança)\".")
    except KwaiError as erro:
        log(f"⚠️ não consegui ler o modelo: {erro}")

    try:
        tela = Tela(adb)
        log(f"✅ tela: {tela.tamanho[0]}x{tela.tamanho[1]}")
    except KwaiError as erro:
        log(f"⚠️ não consegui medir a tela: {erro}")

    adb.acordar()
    if adb.bloqueado():
        falha("a tela está bloqueada: o envio não anda com o celular bloqueado "
              "(e a senha não se digita por adb).")
    else:
        log("✅ tela desbloqueada.")

    # A sonda do toque é um keyevent KEYCODE_UNKNOWN: nenhum app faz nada com
    # ele, mas passa exatamente pela mesma checagem de permissão que qualquer
    # toque — é a diferença entre saber agora e descobrir no meio do envio.
    try:
        adb.shell("input keyevent 0")
        log("✅ toque simulado: liberado neste aparelho.")
    except KwaiError as erro:
        falha(f"toque simulado: {erro}")

    pacote = adb.pacote_instalado()
    if pacote:
        log(f"✅ app do Kwai: {pacote}")
    else:
        falha(f"o app do Kwai não está instalado (procurei por {', '.join(PACOTES_KWAI)}).")

    if _teclado_adb(adb):
        log("✅ ADBKeyboard: instalado — a legenda sai com acento e emoji.")
    else:
        log("⚠️ ADBKeyboard: ausente — o `input text` do Android só digita ASCII, "
            "então a legenda sai sem acento e sem emoji. Instalar o apk "
            "(https://github.com/senzhk/ADBKeyBoard) com `adb install` resolve; "
            "não impede publicar.")

    log("— pronto para publicar." if pronto else "— ainda não dá para publicar: veja o ❌ acima.")
    return pronto


def _listar_dispositivos() -> int:
    try:
        adb = Adb()
        print(str(adb.rodar("devices", "-l")), end="")
        ok, explicacao = disponivel()
        print(("\n✅ " if ok else "\n⚠️ ") + explicacao)
        return 0 if ok else 1
    except KwaiError as erro:
        print(f"❌ {erro}")
        return 1


def _mapear(serial: str | None) -> int:
    """Walkthrough interativo: lê a tela, numera os elementos, e você diz o
    número a tocar. Serve para descobrir o fluxo real de um app que muda de
    versão — cada toque aqui é um passo que depois vira seletor no código.
    """
    adb = Adb(serial)
    adb.resolver()
    pacote = adb.pacote_instalado()
    if not pacote:
        print("❌ o Kwai não está instalado no celular.")
        return 1
    adb.acordar()
    print(f"Mapeando {pacote} no celular {adb.serial}.")
    print("Comandos: <número> toca no elemento | r relê a tela | x salva o XML | "
          "a abre o app | q sai")
    tela = Tela(adb)
    while True:
        tela.imprimir()
        try:
            comando = input("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if comando in ("q", ""):
            return 0
        if comando == "r":
            continue
        if comando == "x":
            destino = Path(f"kwai_tela_{datetime.now():%H%M%S}.xml").resolve()
            print(f"salvo em {tela.despejar(destino)}")
            continue
        if comando == "a":
            adb.abrir_app(pacote)
            time.sleep(8)
            continue
        if not comando.isdigit():
            print("não entendi.")
            continue
        try:
            tela.tocar(tela.elementos()[int(comando)], nome="(escolhido)")
        except (IndexError, KwaiError) as erro:
            print(f"não deu: {erro}")


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # noqa: PLW2901  (acento no console do Windows)
    except (AttributeError, OSError):
        pass

    p = argparse.ArgumentParser(description="Publica um vídeo no Kwai pelo app, via ADB.")
    p.add_argument("video", nargs="?", help="arquivo de vídeo a publicar")
    p.add_argument("titulo", nargs="?", default="", help="título/legenda do vídeo")
    p.add_argument("--legenda", default="", help="legenda/hashtags (padrão: o título)")
    p.add_argument("--serial", default=None, help="serial do celular, se houver mais de um")
    p.add_argument("--dispositivo", action="store_true", help="lista os celulares no USB e sai")
    p.add_argument("--diagnostico", action="store_true",
                   help="confere tudo o que o envio precisa e diz o que falta")
    p.add_argument("--tela", action="store_true", help="mostra o que está na tela do celular agora")
    p.add_argument("--mapear", action="store_true", help="mapeia o app passo a passo, na mão")
    p.add_argument("--seco", action="store_true",
                   help="faz tudo menos tocar em Publicar (modo ensaio)")
    p.add_argument("--manter", action="store_true", help="não apaga o vídeo do celular no fim")
    args = p.parse_args(argv)

    if args.dispositivo:
        return _listar_dispositivos()
    if args.diagnostico:
        return 0 if diagnosticar() else 1
    if args.tela:
        try:
            tela = Tela(Adb(args.serial))
            tela.adb.resolver()
            tela.imprimir()
            return 0
        except KwaiError as erro:
            print(f"❌ {erro}")
            return 1
    if args.mapear:
        return _mapear(args.serial)
    if not args.video:
        p.print_help()
        return 2

    legenda = args.legenda or args.titulo
    try:
        mensagem = enviar_para_kwai(
            args.video, legenda, serial=args.serial, publicar=not args.seco,
            manter_no_celular=args.manter or None,
            log=lambda m: print(m, flush=True))
    except KwaiError as erro:
        print(f"❌ {erro}")
        return 1
    print(f"✅ {mensagem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
