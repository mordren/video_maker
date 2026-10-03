"""Estúdio: a produção dos cortes pelo navegador, rodando na máquina da GPU.

Cola o link do YouTube (ou manda o arquivo) e o serviço propõe os cortes,
um trabalho por vez:

    1. baixa o vídeo (yt-dlp, com a legenda do YouTube quando houver)
    2. Fase 1 — escolhe os melhores trechos (fase1/pipeline.py: transcrição +
       DeepSeek segmenta + JEV qualifica) ou, no lote CSV, usa os tempos do CSV

Cada bloco vira uma PROPOSTA — mas só um recorte BRUTO (ffmpeg -c copy, sem
Whisper, sem silêncio, sem abertura: quase instantâneo), em 16:9, como no
vídeo original. Só os que forem escolhidos em "Propostas" seguem adiante —
tudo que vem depois é caro (Whisper de novo, JEV escolhendo o gancho da
abertura, crop, legenda) e produzir isso em candidatos que o usuário nem
escolhe era processamento jogado fora:

    3. Fase 2 (fase1/pipeline_cortes.py) — SÓ no corte escolhido: corte
       mecânico (silêncio, recomeço, loudness) + abertura em preto e branco +
       transição; fica guardado, então "refazer no outro formato" não roda
       de novo essa parte, só o acabamento
    4. acabamento (finalizar.py), no formato escolhido por corte: crop 9:16
       que segue quem fala (LR-ASD), transparente (16:9 sobre fundo
       desfocado) ou imagens (fotos do assunto no topo, trocando ao longo
       do corte — imagens.py —, e o corte embaixo); legenda, GC, marca d'água, censura, trilha e capa

Vídeo longo: depois da Fase 1, uma consulta à parte ao DeepSeek
(fase1/trechos_longos.py) procura 1-3 trechos corridos de 15-20 min, com o que
tirar de dentro (superchat fora do tema, problema técnico, recado do canal) e
o título. Cada um vira uma proposta "📺 vídeo longo" (prévia = o trecho bruto);
produzir (video_longo.py) tira esses pedaços e o silêncio > 4 s e põe o vídeo
16:9 dentro da moldura do canal (moldura.py) — sem crop, legenda nem GC.

Os produzidos ficam em "Revisar": assiste, ajusta o título e o canal, e
manda para a fila do Publicador (outro serviço, na mesma máquina) — ou
descarta, ou refaz em outro formato. Nada é publicado sem passar pela revisão.

A mesma API que a página usa serve para automatizar depois (ex.: um bot do
Telegram mandando links): POST /api/trabalhos com JSON {"url", "canal", "perfil"}.

Estado em ESTUDIO_DATA (padrão C:\\VideoMaker\\estudio):
    trabalhos.json          a lista de trabalhos e dos cortes de cada um
    config.json             canal -> perfil visual, endereço do Publicador, pastas
    estudio.log             log único de todos os trabalhos (gira em 50 MB)
    trabalhos/<id>/         entrada/, fase1/, cortes/, final/<corte>/

Situação de cada corte: proposta -> produzindo -> revisar -> na_fila (ou
descartado). O 16:9 da proposta (cortes/<corte>.mp4) fica guardado até o
corte ir para a fila, para dar para refazer em outro formato sem cortar de novo.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import yaml
from flask import Flask, jsonify, make_response, render_template, request, send_from_directory

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
FASE1 = RAIZ / "fase1"
sys.path.insert(0, str(AQUI))

import finalizar  # noqa: E402  (também põe RAIZ e fase1 no sys.path)
import pipeline_cortes  # noqa: E402  (Fase 2, chamada por bloco só na produção)
import ai_srt  # noqa: E402
import fila  # noqa: E402  (fila de trabalho/corte no Redis — sobrevive a reinício)
import video_longo  # noqa: E402  (produção do vídeo longo, com a moldura do canal)
import canais_publicacao  # noqa: E402  (perfil de cada canal do Publicador)
import config_cortador  # noqa: E402  (aba Configurações: o que muda por cima dos YAML e constantes)
import custos_cortador  # noqa: E402  (aba Custos)
import historias_ponte  # noqa: E402  (as abas do Estúdio de Histórias na mesma página)
import cta_cortador  # noqa: E402  (CTA em vídeo do canal: anexado só no envio do short; no longo, na produção)
from utils import parse_csv_moments, yt_dlp_path  # noqa: E402

DATA_DIR = Path(os.environ.get("ESTUDIO_DATA") or r"C:\VideoMaker\estudio")
TRABALHOS_DIR = DATA_DIR / "trabalhos"
ESTADO_PATH = DATA_DIR / "trabalhos.json"
CONFIG_PATH = DATA_DIR / "config.json"
# yt_dlp_path() já resolve certo em qualquer SO: bundled .exe no Windows,
# senão o "yt-dlp" instalado no venv (pip) ou no PATH do sistema.
YTDLP = yt_dlp_path() or "yt-dlp"
EXTENSOES = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
TAMANHO_MAXIMO = 12 * 1024 * 1024 * 1024
FORMATOS_VALIDOS = ("dinamico", "transparente", "imagens")
NOMES_FORMATO = {"dinamico": "crop que segue quem fala", "transparente": "transparente",
                 "imagens": "fotos do assunto + corte", "longo": "vídeo longo (16:9 com moldura)"}
LEGENDA_PADRAO = "new"   # finalizar.LEGENDAS: "new" (glow + karaokê com caixa) ou "old" (SRT amarelo)

CONFIG_PADRAO = {
    "publicador_url": "http://127.0.0.1:8080",
    "pasta_transicoes": str(DATA_DIR / "transicoes"),
    "canal_perfil": {},             # canal do Publicador -> perfil visual (logo/cores)
}

# ──────────────────────────────────────────────────────────────────
#  Pouca RAM: a máquina tem 32 GB, mas às vezes o BIOS sobe só com 8 GB
#  (3 pentes misturados). Com 8 GB o crop que segue quem fala (LR-ASD,
#  ~3 GB por minuto de vídeo) derruba a máquina ou cai no crop antigo —
#  então o Estúdio avisa por e-mail e só produz transparente e fotos.
# ──────────────────────────────────────────────────────────────────

RAM_MINIMA_GB = config_cortador.valor_salvo(DATA_DIR, "estudio.RAM_MINIMA_GB", 16)
# as mesmas variáveis PUBLICADOR_EMAIL_* do Publicador (servidor/LEIA-ME.md)
EMAIL_ENV = Path(os.environ.get("ESTUDIO_EMAIL_ENV") or (DATA_DIR.parent / "publicador" / "publicador.env"))


def _ram_total_gb() -> float | None:
    try:
        for linha in open("/proc/meminfo", encoding="ascii"):
            if linha.startswith("MemTotal:"):
                return int(linha.split()[1]) / 1024 / 1024
    except OSError:
        pass
    return None    # Windows: não confere


RAM_GB = float(os.environ["ESTUDIO_RAM_GB"]) if os.environ.get("ESTUDIO_RAM_GB") else _ram_total_gb()  # env: só p/ testar
POUCA_RAM = RAM_GB is not None and RAM_GB < RAM_MINIMA_GB
AVISO_POUCA_RAM = (f"O servidor está com só {RAM_GB:.0f} GB de RAM (o normal são 32 GB — o BIOS "
                   "subiu sem todos os pentes). Crop que segue quem fala DESLIGADO: só produz "
                   "transparente e fotos do assunto. Reinicie/ajuste o BIOS para voltar ao normal."
                   if POUCA_RAM else "")
_ultimo_email_cancelamento = 0.0


def avisar_por_email(assunto: str, corpo: str) -> None:
    """E-mail de aviso pelo mesmo SMTP do Publicador (PUBLICADOR_EMAIL_* em
    EMAIL_ENV ou no ambiente). Sem configuração, só registra no log. Roda
    numa thread: SMTP lento nunca segura a produção."""
    def mandar():
        import smtplib
        from email.message import EmailMessage
        cfg = dict(os.environ)
        if EMAIL_ENV.exists():
            for linha in EMAIL_ENV.read_text(encoding="utf-8-sig").splitlines():
                if "=" in linha and not linha.strip().startswith("#"):
                    k, v = linha.split("=", 1)
                    cfg.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        para = cfg.get("PUBLICADOR_EMAIL_PARA", "").strip()
        host = cfg.get("PUBLICADOR_EMAIL_SMTP_HOST", "smtp.gmail.com").strip()
        porta = int(cfg.get("PUBLICADOR_EMAIL_SMTP_PORT") or 587)
        usuario = cfg.get("PUBLICADOR_EMAIL_SMTP_USER", "").strip()
        senha = cfg.get("PUBLICADOR_EMAIL_SMTP_SENHA", "")
        if not (para and usuario and senha):
            log.warning("e-mail de aviso NÃO enviado (sem PUBLICADOR_EMAIL_* em %s): %s", EMAIL_ENV, assunto)
            return
        msg = EmailMessage()
        msg["Subject"], msg["From"], msg["To"] = assunto, cfg.get("PUBLICADOR_EMAIL_DE", "").strip() or usuario, para
        msg.set_content(corpo)
        try:
            if porta == 465:
                with smtplib.SMTP_SSL(host, porta, timeout=20) as smtp:
                    smtp.login(usuario, senha)
                    smtp.send_message(msg)
            else:
                with smtplib.SMTP(host, porta, timeout=20) as smtp:
                    smtp.starttls()
                    smtp.login(usuario, senha)
                    smtp.send_message(msg)
            log.info("e-mail de aviso enviado para %s: %s", para, assunto)
        except Exception as exc:  # noqa: BLE001
            log.warning("e-mail de aviso falhou (%s): %s", exc, assunto)
    threading.Thread(target=mandar, daemon=True, name="email").start()


def _cancelar_por_pouca_ram(tid: str, cid: str, c: dict) -> None:
    """Corte pedido em crop com a máquina em 8 GB: volta para onde estava,
    com o motivo, e avisa por e-mail (no máximo um por hora)."""
    global _ultimo_email_cancelamento
    motivo = (f"cancelado: servidor com {RAM_GB:.0f} GB de RAM — crop desligado; "
              "produza em transparente ou fotos do assunto")
    _atualizar_corte(tid, cid, status="revisar" if c.get("arquivo") else "proposta", mensagem=motivo)
    _registrar(tid, f"⛔ {cid}: {motivo}")
    if time.time() - _ultimo_email_cancelamento > 3600:
        _ultimo_email_cancelamento = time.time()
        avisar_por_email(f"⚠️ Estúdio: corte cancelado — servidor com {RAM_GB:.0f} GB de RAM",
                         f"O corte {cid} (trabalho {tid}) foi pedido em 'crop que segue quem fala' e foi "
                         f"cancelado.\n\n{AVISO_POUCA_RAM}\n\n(Enquanto a RAM estiver baixa, este aviso "
                         "vem no máximo uma vez por hora.)")


# static/ é a interface do Estúdio de Histórias (historias_ponte.py)
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAXIMO
_TRAVA = threading.RLock()
_cancelar: set[str] = set()        # ids de trabalhos com cancelamento pedido
_processo: subprocess.Popen | None = None
log = logging.getLogger("estudio")


# ──────────────────────────────────────────────────────────────────
#  Estado em disco
# ──────────────────────────────────────────────────────────────────

def _ler_json(caminho: Path, padrao):
    """Lê o estado. Só devolve o padrão se o arquivo NÃO EXISTE.

    No Windows, ler no mesmo instante em que o arquivo é trocado (o .tmp
    substituindo o original) dá "arquivo em uso" — tratar isso como "vazio"
    fazia a API responder "corte não existe" (medido em 25/09/2026) e, pior,
    quem gravasse em cima dessa leitura apagaria todos os trabalhos. Então
    tenta de novo, e se não conseguir, falha em vez de fingir que está vazio.
    """
    for tentativa in range(20):
        try:
            return json.loads(caminho.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return deepcopy(padrao)
        except (OSError, ValueError):
            if tentativa == 19:
                raise
            time.sleep(0.05)


def _gravar_json(caminho: Path, dados) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    for tentativa in range(20):
        try:
            tmp.replace(caminho)
            return
        except PermissionError:  # alguém lendo o arquivo neste instante (Windows)
            if tentativa == 19:
                raise
            time.sleep(0.05)


def carregar_trabalhos() -> list[dict]:
    # A trava (reentrante) também na leitura: dentro deste processo, ler nunca
    # cruza com a troca do arquivo por uma gravação.
    with _TRAVA:
        return _ler_json(ESTADO_PATH, [])


def salvar_trabalhos(trabalhos: list[dict]) -> None:
    with _TRAVA:
        _gravar_json(ESTADO_PATH, trabalhos)


def carregar_config() -> dict:
    return {**deepcopy(CONFIG_PADRAO), **_ler_json(CONFIG_PATH, {})}


def salvar_config(cfg: dict) -> None:
    _gravar_json(CONFIG_PATH, cfg)


def _atualizar(tid: str, **campos) -> dict | None:
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        for t in trabalhos:
            if t["id"] == tid:
                t.update(campos)
                salvar_trabalhos(trabalhos)
                return t
    return None


def _atualizar_corte(tid: str, cid: str, **campos) -> dict | None:
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        for t in trabalhos:
            if t["id"] == tid:
                for c in t.get("cortes", []):
                    if c["id"] == cid:
                        c.update(campos)
                        salvar_trabalhos(trabalhos)
                        return c
    return None


def _pasta(tid: str) -> Path:
    return TRABALHOS_DIR / Path(tid).name


LOG_PATH = DATA_DIR / "estudio.log"


def _registrar(tid: str, texto: str) -> None:
    log.info(texto, extra={"trabalho": tid})


class _LogDoTrabalho(logging.Filter):
    """Marca cada linha com o trabalho em andamento (o worker é uma thread só,
    então há no máximo um), inclusive o logging dos módulos (finalizar, crop)."""
    atual: str | None = None

    def filter(self, registro: logging.LogRecord) -> bool:
        if not getattr(registro, "trabalho", None):
            registro.trabalho = self.atual or "-"
        return True


_log_centralizado = logging.handlers.RotatingFileHandler(
    LOG_PATH, maxBytes=50*1024*1024, backupCount=5, encoding="utf-8")
_log_centralizado.addFilter(_LogDoTrabalho())
_log_centralizado.setFormatter(logging.Formatter(
    "%(asctime)s [%(trabalho)s] %(levelname)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
for nome in ("estudio", "fase1"):
    logging.getLogger(nome).addHandler(_log_centralizado)
    logging.getLogger(nome).setLevel(logging.INFO)


# ──────────────────────────────────────────────────────────────────
#  Etapas
# ──────────────────────────────────────────────────────────────────

class Cancelado(Exception):
    pass


def _checar_cancelamento(tid: str) -> None:
    if tid in _cancelar:
        raise Cancelado()


def _matar_grupo(p: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(p.pid, signal.SIGKILL)
        else:
            subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    except (ProcessLookupError, OSError):
        pass


def _vigiar_cancelamento(tid: str, p: subprocess.Popen) -> None:
    """Confere o cancelamento a cada segundo, mesmo se a etapa estiver calada
    (render, Whisper) — antes só era visto quando saía uma linha no log."""
    while p.poll() is None:
        if tid in _cancelar:
            _matar_grupo(p)
            return
        time.sleep(1)


def _rodar(tid: str, cmd: list[str]) -> None:
    """Roda um comando, copiando a saída para o log do trabalho; cancela se pedido."""
    global _processo
    _registrar(tid, "$ " + " ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd)))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    # Grupo de processos próprio: cancelar mata a etapa E os ffmpeg/Whisper que
    # ela abriu — só p.kill() deixava esses netos rodando órfãos.
    grupo = {"start_new_session": True} if os.name == "posix" else         {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                          text=True, encoding="utf-8", errors="replace", cwd=str(FASE1), **grupo) as p:
        _processo = p
        vigia = threading.Thread(target=_vigiar_cancelamento, args=(tid, p), daemon=True)
        vigia.start()
        try:
            for linha in p.stdout:
                linha = linha.rstrip()
                if linha and "%|" not in linha:        # pula barras de progresso
                    _registrar(tid, "   " + linha[:500])
            p.wait()
            if tid in _cancelar:
                raise Cancelado()
        finally:
            _processo = None
    if p.returncode != 0:
        programa = Path(cmd[1] if Path(cmd[0]).stem.lower().startswith("python") else cmd[0]).name
        raise RuntimeError(f"{programa} terminou com erro (código {p.returncode}) — veja o log")


def _baixar(tid: str, url: str, destino: Path) -> Path:
    destino.mkdir(parents=True, exist_ok=True)
    # Sem um PO Token, o YouTube só liberava 360p (formato 18) para qualquer
    # cliente — o plugin bgutil-ytdlp-pot-provider (instalado no venv) gera um
    # na hora, via um script Deno descartável (servidor/bgutil-ytdlp-pot-
    # provider), sem precisar de navegador nem de um serviço fixo rodando.
    # --impersonate: fingerprint de navegador de verdade via curl_cffi.
    # --progress-delta: uma linha de progresso a cada 20 s, não a cada pedaço.
    # A legenda sai numa chamada à parte, depois (ver _baixar_legenda).
    cmd = [str(YTDLP), "--no-playlist", "--match-filter", "!is_live", "--progress-delta", "20",
           "--retries", "10", "--fragment-retries", "10", "--extractor-retries", "3",
           "--impersonate", "chrome",
           # AV1 por último: o OpenCV do crop dinâmico não decodifica AV1
           # (0 quadros lidos, o crop quebra e o corte é perdido).
           "-N", "8", "-f", "bv*[height<=1080][vcodec!^=av01]+ba/bv*[height<=1080]+ba/b",
           "--merge-output-format", "mp4",
           "-P", str(destino), "-o", _NOME_DOWNLOAD, url]
    # Live recém-encerrada: o YouTube ainda não processou o vídeo (0 fragmentos,
    # "downloaded file is empty") e só o áudio vem. Espera e tenta de novo.
    for tentativa, espera in enumerate(_ESPERAS_DOWNLOAD):
        _checar_cancelamento(tid)
        try:
            _rodar(tid, cmd)
            break
        except RuntimeError:
            if tentativa == len(_ESPERAS_DOWNLOAD) - 1:
                raise
            _registrar(tid, f"   download falhou (live ainda processando ou rede); nova tentativa em {espera // 60} min")
            time.sleep(espera)
    videos = [p for p in destino.iterdir() if p.suffix.lower() in EXTENSOES]
    if not videos:
        raise RuntimeError("o download terminou mas nenhum vídeo apareceu na pasta")
    video = max(videos, key=lambda p: p.stat().st_size)
    _baixar_legenda(tid, url, destino, video)
    return video


# Mesmo nome do vídeo e da legenda: a Fase 1 acha o SRT pelo nome-base do
# vídeo (fase1/transcricao.py:candidatos_srt).
_NOME_DOWNLOAD = "%(title).150B.%(ext)s"
_ESPERAS_DOWNLOAD = (300, 600, 900, 0)   # espera após cada falha; a última tentativa levanta o erro
_ESPERAS_LEGENDA = (0, 30)  # tentativas pelo yt-dlp (espera antes de cada uma, em s)


def _baixar_legenda(tid: str, url: str, destino: Path, video: Path) -> None:
    """Legenda do próprio YouTube ao lado do vídeo — a Fase 1 usa ela e pula o
    Whisper do vídeo inteiro (o usuário prefere a do YouTube). Nunca derruba o
    trabalho: sem legenda, a Fase 1 transcreve.

    Em 29/09/2026 o endereço de legendas (timedtext) passou a dar 429 para o
    IP de casa. O que se sabe (issues do yt-dlp #13831/#11059 e o guia de PO
    Token): a faixa "pt" de uma live em português é uma TRADUÇÃO automática
    (a original é "pt-orig"), e tradução é o que o YouTube mais limita; e o
    cliente padrão pede a legenda sem o PO token de legenda. Por isso aqui
    vai só a original, pelo cliente "web" (o bgutil gera o token "subs"),
    com uma segunda tentativa espaçada. Se ainda assim não vier, lê o painel
    "Transcrição" da página (estudio/legenda_youtube.py), que usa outro
    caminho do YouTube e funcionou do mesmo IP bloqueado.
    """
    def legendas() -> list[Path]:
        return [p for p in destino.iterdir()
                if p.suffix.lower() == ".srt" and p.name.startswith(video.stem + ".")]

    cmd = [str(YTDLP), "--no-playlist", "--skip-download", "--ignore-no-formats-error",
           "--impersonate", "chrome", "--extractor-args", "youtube:player_client=web",
           "--write-subs", "--write-auto-subs", "--sub-langs", "pt-orig,pt-BR",
           "--sub-format", "ttml/best", "--convert-subs", "srt",
           "-P", str(destino), "-o", _NOME_DOWNLOAD, url]
    for espera in _ESPERAS_LEGENDA:
        if espera:
            _registrar(tid, f"   legenda do YouTube não veio; nova tentativa em {espera} s")
            time.sleep(espera)
        _checar_cancelamento(tid)
        try:
            _rodar(tid, cmd)
        except RuntimeError:
            pass                                          # o 429 já saiu no log
        if legendas():
            return
    _registrar(tid, "   legenda pelo yt-dlp indisponível — lendo a transcrição da página do vídeo")
    _checar_cancelamento(tid)
    try:
        _rodar(tid, [sys.executable, str(Path(__file__).with_name("legenda_youtube.py")), url,
                     str(destino / f"{video.stem}.pt.srt")])
    except RuntimeError:
        pass
    if not legendas():
        _registrar(tid, "   (legenda do YouTube indisponível — segue sem ela, o Whisper cobre)")


def _blocos_do_csv(video: Path, csv_path: Path, destino: Path) -> Path:
    momentos = parse_csv_moments(csv_path)
    if not momentos:
        raise RuntimeError("o CSV não tem nenhuma linha válida (colunas inicio, fim, titulo)")
    blocos = [{"id": f"corte_{i:02d}", "rank": i, "inicio": m["start_s"], "fim": m["end_s"],
               "duracao": m["end_s"] - m["start_s"], "gancho": m.get("label", ""),
               "subtitulo": m.get("subtitulo", ""), "musica": m.get("musica", ""),
               "comentario": m.get("comentario", "")}
              for i, m in enumerate(momentos, 1)]
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / "blocos_finais.json"
    _gravar_json(caminho, {"video": str(video), "origem": "csv", "blocos": blocos})
    return caminho


def _etapa(tid: str | None, etapa: str = "") -> None:
    """Trabalho e etapa no ambiente: fase1/custos_api.py anota cada chamada paga
    com eles (aba Custos), inclusive nos subprocessos da Fase 1. Só o laço de
    trabalho chama APIs pagas, e ele é uma thread só."""
    if tid:
        os.environ["ESTUDIO_TRABALHO"], os.environ["ESTUDIO_ETAPA"] = tid, etapa
    else:
        os.environ.pop("ESTUDIO_TRABALHO", None)
        os.environ.pop("ESTUDIO_ETAPA", None)


def _config_fase1(tid: str) -> Path:
    """fase1/config.yaml com o que foi mudado na aba Configurações por cima,
    numa cópia na pasta do trabalho (o do repositório continua sendo o padrão)."""
    caminho = _pasta(tid) / "config_fase1.yaml"
    caminho.write_text(yaml.safe_dump(config_cortador.mesclar_yaml("config.yaml", "fase1"), allow_unicode=True,
                                      sort_keys=False), encoding="utf-8")
    return caminho


def _config_cortes(tid: str) -> Path:
    """config_cortes.yaml do fase1, com a pasta de sons de transição desta
    máquina e sem o crop: a proposta sai em 16:9, o formato vem depois."""
    cfg = config_cortador.mesclar_yaml("config_cortes.yaml", "cortes")
    cfg["transicao"]["pasta_sons"] = carregar_config()["pasta_transicoes"]
    cfg.setdefault("crop", {})["ativo"] = False
    caminho = _pasta(tid) / "config_cortes.yaml"
    caminho.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return caminho


def _jev_e_config(tid: str):
    """O config_cortes.yaml deste trabalho (pasta de sons de transição certa,
    crop desligado — mesma função de antes, só que agora também serve pra Fase
    2 rodar na produção, não só pra proposta) + uma instância do JEV, ou None
    sem chave configurada — quem chama decide se segue sem ela (mesma postura
    de revisar_legenda em finalizar.py)."""
    cfg = yaml.safe_load(_config_cortes(tid).read_text(encoding="utf-8"))
    from jev_client import JEV
    from openrouter import APIError, api_key
    try:
        api_key()
    except APIError:
        return None, cfg
    return JEV(cfg["jev"]), cfg


def _sugerir_formato(jev, bloco: dict) -> str:
    """Pré-seleção do dropdown da proposta: crop, transparente ou imagens,
    pelo texto que a Fase 1 já escreveu (gancho + comentário) — sem
    decodificar vídeo nem áudio de novo, então cabe rodar aqui, antes de
    qualquer produção."""
    if jev is None:
        return "dinamico"
    try:
        return jev.sugerir_formato(bloco.get("gancho", ""), bloco.get("comentario", ""))["formato_sugerido"]
    except Exception as exc:  # noqa: BLE001 — sem sugestão, o padrão é crop
        log.warning("   sugestão de formato (JEV) falhou: %s", exc)
        return "dinamico"


def processar_trabalho(t: dict) -> None:
    tid = t["id"]
    pasta = _pasta(tid)
    py = sys.executable

    # 1. vídeo de entrada
    entrada = pasta / "entrada"
    video = next((p for p in entrada.glob("*") if p.suffix.lower() in EXTENSOES), None) \
        if entrada.exists() else None
    if video is None:
        if not t.get("url"):
            raise RuntimeError("trabalho sem vídeo e sem link")
        _atualizar(tid, etapa="baixando o vídeo")
        video = _baixar(tid, t["url"], entrada)
    _atualizar(tid, titulo_video=video.stem)
    _checar_cancelamento(tid)

    # 2. trechos: Fase 1 (automático) ou CSV
    if t["tipo"] == "csv":
        blocos_path = _blocos_do_csv(video, entrada / t["csv"], pasta / "fase1")
    else:
        _atualizar(tid, etapa="escolhendo os melhores trechos (Fase 1)")
        _etapa(tid, "fase1")
        _rodar(tid, [py, str(FASE1 / "pipeline.py"), str(video), "--workspace", str(pasta / "fase1"),
                     "--config", str(_config_fase1(tid))])
        blocos_path = pasta / "fase1" / "blocos_finais.json"
    blocos = json.loads(blocos_path.read_text(encoding="utf-8")).get("blocos") or []
    _checar_cancelamento(tid)

    # 2b. vídeo longo (15-20 min): consulta à parte ao DeepSeek, na mesma
    # transcrição. Falhar aqui não derruba os shorts.
    longos: list[dict] = []
    if t["tipo"] != "csv":
        _atualizar(tid, etapa="procurando trechos para vídeo longo")
        _etapa(tid, "longo")
        try:
            _rodar(tid, [py, str(FASE1 / "trechos_longos.py"), str(video), "--workspace", str(pasta / "fase1"),
                         "--titulo-live", video.stem, "--config", str(_config_fase1(tid))])
            longos = json.loads((pasta / "fase1" / "longos.json").read_text(encoding="utf-8")).get("blocos") or []
        except (RuntimeError, OSError, ValueError) as exc:
            _registrar(tid, f"   vídeo longo: busca falhou ({exc}) — seguem só os shorts")
        _checar_cancelamento(tid)
    if not blocos and not longos:
        _atualizar(tid, etapa="", mensagem="Nenhum trecho forte o bastante neste vídeo.")
        return
    _registrar(tid, f"{len(blocos)} trecho(s) para cortar" + (f" + {len(longos)} vídeo(s) longo(s)" if longos else ""))
    for b in longos:
        b["duracao"] = b["fim"] - b["inicio"]    # o recorte bruto leva o trecho inteiro, com os buracos

    # 3. corte BRUTO (ffmpeg -c copy, sem Whisper/silêncio/abertura — quase
    # instantâneo), só dos trechos que ainda não têm corte pronto (retomada
    # depois de cair no meio). A Fase 2 de verdade (mecânica + abertura) só
    # roda depois, no que for escolhido pra produzir (ver _preparar_fase2).
    feitos = {c["id"] for c in t.get("cortes", [])}
    pendentes = [b for b in blocos + longos if b["id"] not in feitos
                 and not (pasta / "cortes" / f"{b['id']}.mp4").exists()]
    if pendentes:
        _atualizar(tid, etapa=f"recortando {len(pendentes)} trecho(s)")
        destino_cortes = pasta / "cortes"
        destino_cortes.mkdir(parents=True, exist_ok=True)
        for bloco in pendentes:
            _checar_cancelamento(tid)
            pipeline_cortes.recorta_video(video, bloco["inicio"], bloco["duracao"] + 0.5,
                                          destino_cortes / f"{bloco['id']}.mp4")

    # 4. cada corte vira uma proposta; o resto (Fase 2 + acabamento) só quando for escolhido
    _etapa(tid, "proposta")
    jev, _ = _jev_e_config(tid)
    novos = []
    for bloco in blocos:
        clipe = _fonte(tid, bloco["id"])
        if bloco["id"] in feitos or not clipe.exists():
            continue
        novos.append({"id": bloco["id"], "status": "proposta", "canal": t["canal"],
                      "titulo_publicacao": (bloco.get("gancho") or bloco["id"])[:100],
                      "gancho": bloco.get("gancho", ""), "comentario": bloco.get("comentario", ""),
                      "subtitulo_sugerido": bloco.get("subtitulo", ""), "musica_sugerida": bloco.get("musica", ""),
                      "csv": t["tipo"] == "csv", "nota": bloco.get("nota_final"),
                      "tipo_abertura": bloco.get("tipo_abertura", ""),
                      "duracao": round(finalizar.duracao_de(clipe), 1),
                      "formato_sugerido": _sugerir_formato(jev, bloco)})
    for bloco in longos:
        if bloco["id"] in feitos or not _fonte(tid, bloco["id"]).exists():
            continue
        # Proposta do vídeo longo: a prévia é um corte inicial de ~15 s com a
        # melhor parte (escolhida pelo JEV), sem legenda; os tempos ficam no da
        # live, para a produção cortar dela.
        previa = _previa_longo(tid, bloco, video, jev)
        novos.append({"id": bloco["id"], "tipo": "longo", "status": "proposta", "canal": t["canal"],
                      "previa_inicio": previa[0] if previa else None,
                      "previa_fim": previa[1] if previa else None,
                      "gancho_fim": previa[2] if previa else None,
                      "titulo_publicacao": bloco.get("titulo") or bloco.get("tema", "")[:100],
                      "gancho": bloco.get("titulo", ""), "titulos": bloco.get("titulos", []),
                      "motivo_titulo": bloco.get("motivo_titulo", ""), "tema": bloco.get("tema", ""),
                      "comentario": bloco.get("comentario", ""), "remover": bloco.get("remover", []),
                      "inicio": bloco["inicio"], "fim": bloco["fim"],
                      "duracao": bloco.get("duracao_estimada"), "formato_sugerido": "longo"})
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        for tt in trabalhos:
            if tt["id"] == tid:
                tt.setdefault("cortes", []).extend(novos)
        salvar_trabalhos(trabalhos)
    _registrar(tid, f"{len(novos)} proposta(s) prontas para escolher")


def _previa_longo_arquivo(tid: str, cid: str) -> Path:
    return _pasta(tid) / "cortes" / f"{Path(cid).name}_previa.mp4"


def _previa_longo(tid: str, bloco: dict, live: Path, jev) -> tuple[float, float, float] | None:
    """Corte inicial do vídeo longo: ~15 s com a melhor parte do trecho, que o
    JEV escolhe entre janelas candidatas (sem a legenda). Sem JEV ou se ele
    falhar, fica a primeira janela. Qualquer erro aqui só deixa a proposta sem
    esse corte (a prévia cai no trecho bruto)."""
    destino = _previa_longo_arquivo(tid, bloco["id"])
    try:
        segs = json.loads((_pasta(tid) / "fase1" / "transcricao.json").read_text(encoding="utf-8"))["segmentos"]
        janelas = video_longo.janelas_previa(segs, bloco["inicio"], bloco["fim"], bloco.get("remover", []))
        if not janelas:
            return None
        escolhida = janelas[0]
        if jev is not None and len(janelas) > 1:
            try:
                escolhida = jev.melhor_previa(janelas, bloco.get("tema", ""), bloco.get("comentario", ""))
            except Exception as exc:  # noqa: BLE001
                log.warning("   prévia do longo: JEV falhou (%s); fica a primeira janela", exc)
        video_longo.cortar_previa(live, escolhida["inicio"], escolhida["fim"], destino)
        _registrar(tid, f"   prévia de {bloco['id']}: {escolhida['inicio']:.0f}s–{escolhida['fim']:.0f}s da live "
                        f"(melhor de {len(janelas)} janelas)")
        return escolhida["inicio"], escolhida["fim"], escolhida["gancho_fim"]
    except Exception as exc:  # noqa: BLE001
        destino.unlink(missing_ok=True)
        _registrar(tid, f"   prévia de {bloco['id']} falhou ({exc}) — fica o trecho bruto")
        return None


def _fonte(tid: str, cid: str) -> Path:
    """O corte BRUTO em 16:9 (recorte simples, sem Fase 2) — o que a proposta
    mostra em /previa e de onde sai a Fase 2 quando o usuário produz (ver
    _preparar_fase2). Cortes antigos (antes das propostas) guardavam essa
    cópia em backup/."""
    novo = _pasta(tid) / "cortes" / f"{Path(cid).name}.mp4"
    antigo = _pasta(tid) / "backup" / f"{Path(cid).name}.mp4"
    return antigo if (antigo.exists() and not novo.exists()) else novo


def _fase2_pronto(tid: str, cid: str) -> Path:
    return _pasta(tid) / "produzido" / f"{Path(cid).name}.mp4"


def _preparar_fase2(tid: str, cid: str, c: dict) -> Path:
    """A Fase 2 de verdade (silêncio, recomeço, abertura em preto e branco,
    transição — fase1/pipeline_cortes.py) rodava em TODO bloco antes mesmo da
    proposta existir; agora só roda aqui, no corte bruto (_fonte) do que foi
    escolhido pra produzir. Fica guardado: um "refazer no outro formato" não
    roda essa parte de novo, só o acabamento (crop/legenda) muda."""
    pronto = _fase2_pronto(tid, cid)
    if pronto.exists():
        return pronto
    jev, cfg = _jev_e_config(tid)
    pronto.parent.mkdir(parents=True, exist_ok=True)
    bloco = {"id": cid, "gancho": c.get("gancho", ""), "comentario": c.get("comentario", "")}
    pipeline_cortes.processa_bloco(bloco, None, pronto.parent, cfg, jev, bruto_pronto=_fonte(tid, cid))
    return pronto


def _fim_gancho(tid: str, cid: str) -> float:
    """Duração da abertura (o gancho) que a Fase 2 pôs no começo do clipe; 0
    se ela não teve abertura."""
    rel = _fase2_pronto(tid, cid).parent / Path(cid).name / "relatorio.json"
    try:
        ab = json.loads(rel.read_text(encoding="utf-8")).get("abertura") or {}
        return max(0.0, float(ab["fim"]) - float(ab["inicio"]))
    except (OSError, ValueError, KeyError, TypeError):
        return 0.0


def _corte(tid: str, cid: str) -> dict | None:
    t = next((t for t in carregar_trabalhos() if t["id"] == tid), None)
    return next((c for c in (t or {}).get("cortes", []) if c["id"] == cid), None)


_produzindo_agora: tuple[str, str] | None = None


def _produzir_pendente() -> bool:
    """Produz UM corte pedido (status "produzindo"), o mais antigo pedido
    primeiro (fila.py, Redis — sobrevive a reinício: reconstruída do
    trabalhos.json ao subir). Tem prioridade sobre trabalho novo: é curto e
    alguém está esperando para revisar. Devolve se fez algo (mesmo que o
    corte tenha sido cancelado/descartado enquanto esperava na fila — nesse
    caso só descarta o item e diz que "fez algo", para não esperar à toa)."""
    global _produzindo_agora
    item = fila.proximo_corte()
    if item is None:
        return False
    tid, cid = item
    with _TRAVA:
        t = next((tt for tt in carregar_trabalhos() if tt["id"] == tid), None)
        c = next((cc for cc in (t or {}).get("cortes", []) if cc["id"] == cid), None)
        if c is None or c.get("status") not in ("produzindo", "refazendo"):
            return True    # cancelado/descartado enquanto esperava na fila
        _produzindo_agora = (tid, cid)
    formato = c.get("formato_pedido") or "dinamico"
    legenda = c.get("legenda_pedida") or LEGENDA_PADRAO
    if POUCA_RAM and formato == "dinamico":
        _cancelar_por_pouca_ram(tid, cid, c)
        _produzindo_agora = None
        return True
    if c["status"] == "refazendo":
        _atualizar_corte(tid, cid, status="produzindo")
    _LogDoTrabalho.atual = tid
    _etapa(tid, "producao")
    _registrar(tid, f"🎬 {cid}: produzindo em {NOMES_FORMATO.get(formato, formato)}"
                    + ("" if c.get("tipo") == "longo" else f", legenda {legenda}"))
    try:
        if c.get("tipo") == "longo":
            meta = _produzir_longo(tid, cid, c, t)
            if (_corte(tid, cid) or {}).get("status") == "produzindo":
                _atualizar_corte(tid, cid, status="revisar", formato="longo", versao=c.get("versao", 0) + 1,
                                 mensagem="", **meta)
                _registrar(tid, f"✅ {cid} produzido — em Revisar")
            return True
        ja_pronto = _fase2_pronto(tid, cid).exists()
        clipe = _preparar_fase2(tid, cid, c)
        _registrar(tid, f"   Fase 2 {'(reaproveitada)' if ja_pronto else '(silêncio, abertura, transição)'} ok")
        musicas = ai_srt.load_config().get("music_dir")
        if c.get("arquivo"):
            # refazendo em outro formato: título, manchete e trilha continuam os da revisão
            textos = dict(titulo_sugerido=c.get("titulo", ""), manchete_sugerida=c.get("subtitulo", ""),
                          musica_sugerida=(c.get("trilha") or "").rsplit(".", 1)[0], preferir_sugeridos=True)
        else:
            # CSV: coluna titulo = chapéu do GC, subtitulo = manchete (como no app).
            # Automático: a IA lê a fala; o gancho da Fase 1 é a manchete de reserva.
            csv = bool(c.get("csv"))
            textos = dict(titulo_sugerido=c.get("gancho", "") if csv else "",
                          manchete_sugerida=c.get("subtitulo_sugerido", "") if csv else c.get("gancho", ""),
                          musica_sugerida=c.get("musica_sugerida", ""), preferir_sugeridos=csv)
        canal_corte = c.get("canal") or t.get("canal")
        perfil_nome = (c.get("perfil") or carregar_config().get("canal_perfil", {}).get(canal_corte)
                       or t["perfil"])
        _registrar(tid, f"   visual: {perfil_nome} (canal {canal_corte or '—'})")
        meta = finalizar.finalizar_corte(
            clipe, _pasta(tid) / "final" / cid, finalizar.carregar_perfil(perfil_nome),
            yaml.safe_load((FASE1 / "config_crop.yaml").read_text(encoding="utf-8")),
            contexto=c.get("comentario", ""), gancho=c.get("gancho", ""),
            pasta_trilhas=Path(musicas) if musicas else None,
            formato=formato, permitir_crop=not POUCA_RAM,
            legenda=legenda, fim_gancho=_fim_gancho(tid, cid), **textos)
        if (_corte(tid, cid) or {}).get("status") == "produzindo":    # não foi cancelado no meio
            extra = {}
            if not c.get("arquivo") and not c.get("titulo_editado"):
                extra["titulo_publicacao"] = (meta["subtitulo"] or c.get("gancho") or cid)[:100]
            _atualizar_corte(tid, cid, status="revisar", formato=formato, versao=c.get("versao", 0) + 1,
                             mensagem="", **meta, **extra)
            _registrar(tid, f"✅ {cid} produzido — em Revisar")
    except Exception as exc:  # noqa: BLE001
        _atualizar_corte(tid, cid, status="revisar" if c.get("arquivo") else "proposta",
                         mensagem=f"produção falhou: {exc}"[:300])
        _registrar(tid, f"⚠️ {cid}: produção falhou: {exc}")
    finally:
        _produzindo_agora = None
        _LogDoTrabalho.atual = None
        _etapa(None)
        finalizar.liberar_modelos()
    return True


def _produzir_longo(tid: str, cid: str, c: dict, t: dict) -> dict:
    """Vídeo longo: sai direto da live (corte preciso, sem depender do
    keyframe do recorte bruto); sem ela, do recorte bruto."""
    entrada = _pasta(tid) / "entrada"
    live = next((p for p in entrada.glob("*") if p.suffix.lower() in EXTENSOES), None) if entrada.exists() else None
    if live is not None:
        fonte, inicio, fim, remover = live, c["inicio"], c["fim"], c.get("remover", [])
    else:
        fonte = _fonte(tid, cid)
        inicio, fim = 0.0, finalizar.duracao_de(fonte)
        remover = [{**r, "inicio": r["inicio"] - c["inicio"], "fim": r["fim"] - c["inicio"]}
                   for r in c.get("remover", [])]
    canal_corte = c.get("canal") or t.get("canal")
    perfil_nome = (c.get("perfil") or carregar_config().get("canal_perfil", {}).get(canal_corte) or t["perfil"])
    _registrar(tid, f"   visual: {perfil_nome} (canal {canal_corte or '—'}), moldura do vídeo longo")
    gancho = None       # abertura em P&B: a melhor parte da prévia (tempo da live, como inicio/fim)
    if live is not None and c.get("previa_inicio") is None:
        # proposta de antes do corte inicial: acha a melhor parte agora
        jev, _ = _jev_e_config(tid)
        previa = _previa_longo(tid, {**c, "id": cid}, live, jev)
        if previa:
            c = _atualizar_corte(tid, cid, previa_inicio=previa[0], previa_fim=previa[1], gancho_fim=previa[2]) or c
    if live is not None and c.get("previa_inicio") is not None and c.get("gancho_fim"):
        gancho = (c["previa_inicio"], c["gancho_fim"])
    meta = video_longo.produzir(fonte, inicio, fim, remover, perfil_nome, _pasta(tid) / "final" / cid,
                                c.get("titulo_publicacao") or c.get("gancho") or cid, gancho, canal=canal_corte)
    _registrar(tid, f"   {meta['duracao'] / 60:.1f} min, {meta['remocoes']} remoção(ões), "
                    f"{meta['silencios_cortados']} silêncio(s) longo(s) cortado(s)")
    return meta


def _laco() -> None:
    while True:
        try:
            if _produzir_pendente():
                continue
            tid = fila.proximo_trabalho(timeout=3)      # bloqueia até 3s — nada de sleep(3) sempre
            if tid is None:
                continue
            with _TRAVA:
                proximo = next((t for t in carregar_trabalhos() if t["id"] == tid), None)
                if proximo is None or proximo["status"] != "aguardando":
                    continue    # cancelado/removido enquanto esperava na fila
                _atualizar(tid, status="processando", etapa="começando",
                           iniciado_em=datetime.now().isoformat(timespec="seconds"))
            _LogDoTrabalho.atual = tid
            _registrar(tid, "▶ começou")
            try:
                processar_trabalho(proximo)
                _atualizar(tid, status="pronto", etapa="",
                           terminado_em=datetime.now().isoformat(timespec="seconds"))
                _registrar(tid, "✅ pronto")
            except Cancelado:
                _atualizar(tid, status="cancelado", etapa="")
                _registrar(tid, "⏹ cancelado")
            except Exception as exc:  # noqa: BLE001
                _atualizar(tid, status="erro", etapa="", mensagem=str(exc)[:500])
                _registrar(tid, f"❌ {exc}")
            finally:
                _cancelar.discard(tid)
                _LogDoTrabalho.atual = None
                _etapa(None)
                finalizar.liberar_modelos()
        except Exception as exc:  # noqa: BLE001 — o laço nunca morre
            log.error("laço: %s", exc)
            time.sleep(5)


# ──────────────────────────────────────────────────────────────────
#  Publicador (outro serviço, mesma máquina)
# ──────────────────────────────────────────────────────────────────

def _publicador_estado() -> dict:
    import requests
    try:
        r = requests.get(carregar_config()["publicador_url"] + "/api/estado", timeout=4)
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


def _nome_arquivo(titulo: str) -> str:
    """O Publicador usa o nome do arquivo como título do vídeo no YouTube."""
    nome = re.sub(r'[\x00-\x1f<>"/\\|?*]+', " ", titulo)
    nome = re.sub(r"\s*:\s*", " - ", nome)          # "Teste: x" -> "Teste - x"
    nome = re.sub(r"\s+", " ", nome).strip(" .")
    return (nome[:100] or "corte") + ".mp4"


# ──────────────────────────────────────────────────────────────────
#  Rotas
# ──────────────────────────────────────────────────────────────────

@app.get("/")
def pagina():
    resposta = make_response(render_template("index.html"))
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.get("/api/estado")
def estado():
    pub = _publicador_estado()
    cfg = carregar_config()
    return jsonify({
        "trabalhos": list(reversed(carregar_trabalhos())),
        "perfis": finalizar.perfis(),
        "perfil_padrao": ai_srt.get_current_profile_name(),   # o perfil ativo no app
        "canais": [c["nome"] for c in pub.get("canais", [])],
        "publicador_ok": bool(pub),
        "publicador_porta": cfg["publicador_url"].rsplit(":", 1)[-1],
        # em outra máquina (hoje o .157), o link vai direto; na mesma, pelo endereço que o navegador usou
        "publicador_link": None if re.search(r"//(127\.0\.0\.1|localhost)[:/]", cfg["publicador_url"] + "/")
                           else cfg["publicador_url"].rstrip("/") + "/",
        "canal_perfil": cfg["canal_perfil"],
        "produzindo_agora": list(_produzindo_agora) if _produzindo_agora else None,
        "pouca_ram": AVISO_POUCA_RAM,
        "legenda_padrao": LEGENDA_PADRAO,
        "canais_perfil": canais_publicacao.todos(DATA_DIR),
    })


# ──────────────────────────────────────────────────────────────────
#  Abas da página única: Canais, Custos e Configurações (lado do Cortador)
# ──────────────────────────────────────────────────────────────────

def _erro_valor(exc: ValueError):
    return jsonify({"detail": str(exc), "erro": str(exc)}), 400


@app.get("/api/estudio/canais")
def canais_publicacao_listar():
    """Os canais do Publicador com o perfil de cada um, o visual e a config de
    postagem de lá (intervalo, privacidade, TikTok)."""
    pub = _publicador_estado()
    cfg = carregar_config()
    perfis = canais_publicacao.todos(DATA_DIR)
    conectados = {c["nome"]: c.get("conectado") for c in pub.get("canais", [])}
    publicacao = (pub.get("config") or {}).get("canais") or {}
    marcas = {nome: (p or {}).get("brand_name", "") for nome, p in (ai_srt.load_config().get("profiles") or {}).items()}
    nomes = list(dict.fromkeys([*conectados, *perfis]))
    return jsonify({
        "publicador_ok": bool(pub),
        "canais": [{"nome": n, "no_publicador": n in conectados, "conectado": conectados.get(n),
                    "perfil": perfis.get(n), "visual": cfg["canal_perfil"].get(n, ""),
                    "marca": marcas.get(cfg["canal_perfil"].get(n, ""), ""),
                    "publicacao": publicacao.get(n)} for n in nomes],
        "visuais": [{"nome": p, "marca": marcas.get(p, "")} for p in finalizar.perfis()],
        "formatos": canais_publicacao.FORMATOS,
        "padrao": canais_publicacao.PADRAO,
        "publicador_url": cfg["publicador_url"],
    })


@app.put("/api/estudio/canais/<nome>")
def canais_publicacao_salvar(nome: str):
    dados = request.get_json(silent=True) or {}
    visual = str(dados.pop("visual", "") or "").strip()
    if visual and visual not in finalizar.perfis():
        return jsonify({"detail": f"Visual '{visual}' não existe."}), 400
    try:
        perfil = canais_publicacao.salvar(DATA_DIR, nome, dados)
    except ValueError as exc:
        return _erro_valor(exc)
    if visual:
        with _TRAVA:
            cfg = carregar_config()
            cfg["canal_perfil"][nome] = visual
            salvar_config(cfg)
    return jsonify({"ok": True, "perfil": perfil})


@app.delete("/api/estudio/canais/<nome>")
def canais_publicacao_apagar(nome: str):
    canais_publicacao.apagar(DATA_DIR, nome)
    return jsonify({"ok": True})


def _chave_env(arquivo: Path, nome: str) -> str:
    try:
        for linha in arquivo.read_text(encoding="utf-8-sig").splitlines():
            k, _, v = linha.partition("=")
            if k.strip() == nome:
                return v.strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


@app.get("/api/estudio/custos")
def custos_estudio():
    env_cortes = FASE1 / ".env"
    env_hist = historias_ponte.raiz_dados(DATA_DIR) / ".env"
    chaves = [("openrouter", "Cortador", _chave_env(env_cortes, "OPENROUTER_API_KEY")),
              ("openrouter", "Histórias", _chave_env(env_hist, "OPENROUTER_API_KEY")),
              ("deepseek", "Cortador", _chave_env(env_cortes, "DEEPSEEK_API_KEY")
               or str(ai_srt.load_config().get("api_key") or ""))]
    return jsonify({**custos_cortador.resumo(DATA_DIR, carregar_trabalhos()),
                    "contas": custos_cortador.contas(chaves)})


@app.get("/api/estudio/configuracoes")
def configuracoes_ver():
    return jsonify(config_cortador.estado())


@app.put("/api/estudio/configuracoes")
def configuracoes_salvar():
    try:
        return jsonify(config_cortador.salvar(request.get_json(silent=True)))
    except ValueError as exc:
        return _erro_valor(exc)


@app.post("/api/estudio/configuracoes/restaurar")
def configuracoes_restaurar():
    try:
        return jsonify(config_cortador.restaurar((request.get_json(silent=True) or {}).get("chaves") or []))
    except ValueError as exc:
        return _erro_valor(exc)


@app.post("/api/estudio/configuracoes/testar/<servico>")
def configuracoes_testar(servico: str):
    try:
        return jsonify(config_cortador.testar(servico))
    except ValueError as exc:
        return _erro_valor(exc)


def _novo_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:4]}"


@app.post("/api/trabalhos")
def novo_trabalho():
    """Link ou arquivo de vídeo (+ CSV no lote). Aceita formulário ou JSON."""
    dados = request.get_json(silent=True) or request.form
    url = str(dados.get("url") or "").strip()
    canal = str(dados.get("canal") or "").strip()
    # Sem perfil (agente/bot): o visual lembrado para o canal, senão o padrão do app.
    perfil = (str(dados.get("perfil") or "").strip()
              or carregar_config()["canal_perfil"].get(canal) or ai_srt.get_current_profile_name())
    arquivo = request.files.get("video")
    legenda = request.files.get("legenda")
    csv = request.files.get("csv")
    if not url and not (arquivo and arquivo.filename):
        return jsonify({"erro": "Cole um link do YouTube ou escolha um arquivo de vídeo."}), 400
    if url and not re.match(r"https?://", url):
        return jsonify({"erro": "O link precisa começar com http:// ou https://"}), 400
    if perfil not in finalizar.perfis():
        return jsonify({"erro": f"Perfil visual '{perfil}' não existe."}), 400

    tid = _novo_id()
    entrada = _pasta(tid) / "entrada"
    entrada.mkdir(parents=True, exist_ok=True)
    t = {"id": tid, "criado_em": datetime.now().isoformat(timespec="seconds"), "tipo": "auto",
         "url": url, "perfil": perfil, "canal": canal, "status": "aguardando", "etapa": "",
         "mensagem": "", "cortes": [], "titulo_video": url}
    if arquivo and arquivo.filename:
        nome = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", Path(arquivo.filename).name)
        if Path(nome).suffix.lower() not in EXTENSOES:
            shutil.rmtree(_pasta(tid), ignore_errors=True)
            return jsonify({"erro": "Formato de vídeo não aceito."}), 400
        arquivo.save(entrada / nome)
        t["titulo_video"] = Path(nome).stem
        # Legenda opcional junto do arquivo (sem link, não tem a legenda que o
        # YouTube daria de graça no download) — mesmo nome-base do vídeo, para
        # a Fase 1 achar sozinha (fase1/transcricao.py:candidatos_srt) e pular
        # o Whisper do vídeo inteiro.
        if legenda and legenda.filename and Path(legenda.filename).suffix.lower() == ".srt":
            legenda.save(entrada / f"{Path(nome).stem}.srt")
    if csv and csv.filename:
        csv.save(entrada / "cortes.csv")
        t["tipo"], t["csv"] = "csv", "cortes.csv"
        try:
            n = len(parse_csv_moments(entrada / "cortes.csv"))
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(_pasta(tid), ignore_errors=True)
            return jsonify({"erro": f"CSV inválido: {exc}"}), 400
        if n == 0:
            shutil.rmtree(_pasta(tid), ignore_errors=True)
            return jsonify({"erro": "O CSV não tem nenhuma linha válida."}), 400
    _registrar(tid, f"criado ({t['tipo']}) — {url or t['titulo_video']}")
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        trabalhos.append(t)
        salvar_trabalhos(trabalhos)
        if canal:  # lembra o perfil usado com este canal
            cfg = carregar_config()
            cfg["canal_perfil"][canal] = perfil
            salvar_config(cfg)
    fila.enfileirar_trabalho(tid)
    return jsonify({"id": tid})


def _links_corte(tid: str, c: dict) -> dict:
    base = f"/midia/{tid}/{c['id']}/"
    links = {"previa": f"/previa/{tid}/{c['id']}"} if c.get("status") in ("proposta", "produzindo", "revisar") else {}
    if c.get("arquivo") and c.get("status") == "revisar":
        links["video"] = base + c["arquivo"]
    if c.get("capa") and c.get("status") == "revisar":
        links["capa"] = base + c["capa"]
    return links


def _resumo_trabalho(t: dict) -> dict:
    cortes = t.get("cortes", [])
    contar = lambda *st: sum(1 for c in cortes if c.get("status") in st)  # noqa: E731
    return {k: t.get(k) for k in ("id", "status", "etapa", "mensagem", "titulo_video", "url", "canal", "perfil", "tipo",
                                  "criado_em", "terminado_em")} | {
        "propostas": contar("proposta"), "produzindo": contar("produzindo", "refazendo"),
        "revisar": contar("revisar"), "na_fila": contar("na_fila"), "descartados": contar("descartado")}


@app.get("/api/trabalhos")
def listar_trabalhos():
    """Resumo de cada trabalho, o mais novo primeiro (para um agente/bot: o
    /api/estado traz tudo, inclusive os cortes, e é grande)."""
    return jsonify({"trabalhos": [_resumo_trabalho(t) for t in reversed(carregar_trabalhos())]})


@app.get("/api/trabalhos/<tid>")
def ver_trabalho(tid: str):
    """Um trabalho com os cortes e os links (prévia 16:9, vídeo pronto, capa)."""
    t = next((t for t in carregar_trabalhos() if t["id"] == tid), None)
    if t is None:
        return jsonify({"erro": "Trabalho não existe."}), 404
    campos = ("id", "tipo", "status", "canal", "perfil", "gancho", "comentario", "titulo_publicacao", "duracao", "nota",
              "tipo_abertura", "formato_sugerido", "formato", "formato_pedido", "estilo_legenda", "mensagem",
              "arquivo_publicador", "enviado_em", "titulos", "tema")
    cortes = [{**{k: c.get(k) for k in campos if c.get(k) is not None}, "links": _links_corte(tid, c)}
              for c in t.get("cortes", [])]
    return jsonify({**_resumo_trabalho(t), "cortes": cortes,
                    "produzindo_agora": list(_produzindo_agora) if _produzindo_agora and _produzindo_agora[0] == tid
                    else None})


@app.post("/api/trabalhos/<tid>/cancelar")
def cancelar(tid: str):
    with _TRAVA:
        t = next((t for t in carregar_trabalhos() if t["id"] == tid), None)
        if t is None:
            return jsonify({"erro": "Trabalho não existe."}), 404
        if t["status"] == "aguardando":
            _atualizar(tid, status="cancelado")
        elif t["status"] == "processando":
            _cancelar.add(tid)
    return jsonify({"ok": True})


@app.post("/api/trabalhos/<tid>/repetir")
def repetir(tid: str):
    """Põe de novo na fila (retoma: a Fase 1 reaproveita o que já tinha feito)."""
    t = _atualizar(tid, status="aguardando", etapa="", mensagem="")
    if t:
        fila.enfileirar_trabalho(tid)
    return (jsonify({"ok": True}) if t else (jsonify({"erro": "Trabalho não existe."}), 404))


@app.post("/api/trabalhos/<tid>/remover")
def remover(tid: str):
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        t = next((t for t in trabalhos if t["id"] == tid), None)
        if t is None:
            return jsonify({"erro": "Trabalho não existe."}), 404
        if t["status"] == "processando":
            return jsonify({"erro": "Cancele antes de remover."}), 409
        salvar_trabalhos([x for x in trabalhos if x["id"] != tid])
    shutil.rmtree(_pasta(tid), ignore_errors=True)
    return jsonify({"ok": True})


@app.get("/api/log")
def ver_log():
    """As últimas linhas do estudio.log (todos os trabalhos) — lê só o fim do
    arquivo, que pode chegar a 50 MB antes de girar."""
    if not LOG_PATH.exists():
        return jsonify({"linhas": []})
    inicio = max(0, LOG_PATH.stat().st_size - 256 * 1024)
    with LOG_PATH.open("rb") as f:
        f.seek(inicio)
        linhas = f.read().decode("utf-8", errors="replace").splitlines()[1 if inicio else 0:]
    return jsonify({"linhas": linhas[-500:]})


_LOG_ENVIO = re.compile(r"\[(?P<tid>[\w-]+)\] \w+: 📤 (?P<cid>\S+) foi para a fila do Publicador "
                        r"\((?P<canal>[^)]*)\): (?P<arquivo>.+)$")
_LOG_RANKING = re.compile(r"\[(?P<tid>[\w-]+)\] .*#\d+ (?P<cid>bloco_\d+)\s+\S+\s+viral (?P<viral>[\d.]+)"
                          r"\s+nota (?P<nota>[\d.]+)")


@app.get("/api/notas")
def notas_dos_cortes():
    """As notas do JEV/Fase 1 de cada corte que foi para o Publicador — para a
    aba Desempenho de lá cruzar com as métricas do YouTube. O vínculo com o
    vídeo publicado é o nome do arquivo (o Publicador usa como título).

    Fonte principal: trabalhos.json + fase1/blocos_finais.json (nota completa).
    Complemento: o estudio.log (e os girados), para cortes de trabalhos já
    removidos da lista — ali só sobram a nota final e a viral do ranking.
    """
    def _blocos(tid: str) -> dict:
        caminho = _pasta(tid) / "fase1" / "blocos_finais.json"
        try:
            return {b["id"]: b for b in json.loads(caminho.read_text(encoding="utf-8")).get("blocos") or []}
        except (OSError, ValueError):
            return {}

    def _nota(bloco: dict) -> dict:
        return {"nota_final": bloco.get("nota_final"), "viral": bloco.get("score_viral"),
                "ritmo": bloco.get("qualidade_interna"), "llm": bloco.get("nota_llm"),
                "abertura": bloco.get("abertura"), "tipo_abertura": bloco.get("tipo_abertura"),
                "nota_duracao": bloco.get("nota_duracao"),
                "precisa_melhora": bloco.get("precisa_melhora")}

    cortes: dict[tuple[str, str], dict] = {}
    for t in carregar_trabalhos():
        blocos = _blocos(t["id"])
        for c in t.get("cortes", []):
            if not c.get("arquivo_publicador"):
                continue
            cortes[(t["id"], c["id"])] = {
                "trabalho": t["id"], "corte": c["id"], "canal": c.get("canal"),
                "arquivo": c["arquivo_publicador"], "enviado_em": c.get("enviado_em"),
                "gancho": c.get("gancho"), "formato": c.get("formato"), "csv": c.get("csv"),
                "duracao": c.get("duracao"), "video_origem": t.get("titulo_video"),
                "url_origem": t.get("url"), "fonte": "trabalho",
                **({"nota_final": c.get("nota")} | {k: v for k, v in _nota(blocos.get(c["id"], {})).items()
                                                     if v is not None})}

    ranking: dict[tuple[str, str], dict] = {}
    envios: list[dict] = []
    for caminho in sorted(LOG_PATH.parent.glob(LOG_PATH.name + "*"), reverse=True):
        try:
            texto = caminho.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for linha in texto.splitlines():
            if "📤" in linha:
                m = _LOG_ENVIO.search(linha)
                if m:
                    envios.append({**m.groupdict(), "enviado_em": linha[:19].replace(" ", "T")})
            elif " viral " in linha:
                m = _LOG_RANKING.search(linha)
                if m:
                    ranking[(m["tid"], m["cid"])] = {"viral": float(m["viral"]), "nota_final": float(m["nota"])}
    for e in envios:
        chave = (e["tid"], e["cid"])
        if chave in cortes:
            continue
        bloco = _blocos(e["tid"]).get(e["cid"])
        nota = _nota(bloco) if bloco else ranking.get(chave, {})
        cortes[chave] = {"trabalho": e["tid"], "corte": e["cid"], "canal": e["canal"],
                         "arquivo": e["arquivo"].strip(), "enviado_em": e["enviado_em"],
                         "fonte": "log", **nota}
    return jsonify({"cortes": list(cortes.values())})


@app.get("/previa/<tid>/<cid>")
def previa(tid: str, cid: str):
    """O 16:9 da proposta (Fase 2), para assistir antes de mandar produzir."""
    fonte = _fonte(tid, cid)
    corte = _previa_longo_arquivo(tid, cid)     # vídeo longo: o corte inicial de ~15 s
    if corte.exists():
        fonte = corte
    return send_from_directory(fonte.parent, fonte.name, conditional=True, max_age=0)


@app.get("/midia/<tid>/<cid>/<arquivo>")
def midia(tid: str, cid: str, arquivo: str):
    return send_from_directory(_pasta(tid) / "final" / Path(cid).name, Path(arquivo).name,
                               conditional=True, max_age=0)


@app.post("/api/cortes/<tid>/<cid>")
def editar_corte(tid: str, cid: str):
    dados = request.get_json(silent=True) or {}
    campos = {}
    if "titulo_publicacao" in dados:
        campos["titulo_publicacao"] = str(dados["titulo_publicacao"]).strip()[:100]
        campos["titulo_editado"] = True
    if "canal" in dados:
        campos["canal"] = str(dados["canal"]).strip()
    c = _atualizar_corte(tid, cid, **campos)
    return jsonify({"ok": True}) if c else (jsonify({"erro": "Corte não existe."}), 404)


@app.post("/api/cortes/<tid>/<cid>/descartar")
def descartar(tid: str, cid: str):
    c = _atualizar_corte(tid, cid, status="descartado")
    return jsonify({"ok": True}) if c else (jsonify({"erro": "Corte não existe."}), 404)


@app.post("/api/cortes/<tid>/<cid>/voltar")
def voltar(tid: str, cid: str):
    """Tira do descarte, ou cancela uma produção que ainda não começou."""
    c = _corte(tid, cid)
    if c is None:
        return jsonify({"erro": "Corte não existe."}), 404
    if _produzindo_agora == (tid, cid):
        return jsonify({"erro": "Este corte já está sendo produzido — espere terminar."}), 409
    _atualizar_corte(tid, cid, status="revisar" if c.get("arquivo") else "proposta")
    return jsonify({"ok": True})


@app.post("/api/cortes/<tid>/<cid>/produzir")
def produzir(tid: str, cid: str):
    """Manda produzir (ou refazer) o corte no formato pedido, a partir do 16:9.

    `canal` é opcional: o combobox de Propostas manda o canal escolhido ali
    (pré-selecionado com o canal do trabalho, mas trocável por corte) — assim
    dá para mandar cortes do mesmo trabalho para canais diferentes, em vez de
    todos ficarem presos no canal escolhido lá no início."""
    dados = request.get_json(silent=True) or {}
    formato = dados.get("formato") if dados.get("formato") in FORMATOS_VALIDOS else "dinamico"
    legenda = dados.get("legenda") if dados.get("legenda") in finalizar.LEGENDAS else LEGENDA_PADRAO
    c = _corte(tid, cid)
    if c is None:
        return jsonify({"erro": "Corte não existe."}), 404
    if c.get("tipo") == "longo":
        formato = "longo"      # sem crop nem legenda: só a moldura
    if c.get("status") not in ("proposta", "revisar", "descartado"):
        return jsonify({"erro": "Este corte já está na produção ou na fila."}), 409
    if not _fonte(tid, cid).exists():
        return jsonify({"erro": "O 16:9 deste corte não existe mais para produzir."}), 409
    if POUCA_RAM and formato == "dinamico":
        return jsonify({"erro": AVISO_POUCA_RAM}), 409
    campos = {"status": "produzindo", "formato_pedido": formato, "legenda_pedida": legenda, "mensagem": "",
              "pedido_em": datetime.now().isoformat(timespec="seconds")}
    canal = str(dados.get("canal") or "").strip()
    perfil = str(dados.get("perfil") or "").strip()
    if perfil and perfil not in finalizar.perfis():
        return jsonify({"erro": f"Perfil visual '{perfil}' não existe."}), 400
    if canal:
        campos["canal"] = canal
    if perfil:
        campos["perfil"] = perfil
        if canal:  # lembra o visual usado com este canal
            with _TRAVA:
                cfg = carregar_config()
                cfg["canal_perfil"][canal] = perfil
                salvar_config(cfg)
    _atualizar_corte(tid, cid, **campos)
    fila.enfileirar_corte(tid, cid)
    return jsonify({"ok": True})


@app.post("/api/cortes/<tid>/<cid>/enviar")
def enviar(tid: str, cid: str):
    """Manda o corte para a fila do Publicador (que publica no YouTube/TikTok)."""
    import requests
    t = next((t for t in carregar_trabalhos() if t["id"] == tid), None)
    c = next((c for c in (t or {}).get("cortes", []) if c["id"] == cid), None)
    if c is None:
        return jsonify({"erro": "Corte não existe."}), 404
    if c.get("status") != "revisar":
        return jsonify({"erro": "Este corte não está esperando revisão."}), 409
    if not c.get("canal"):
        return jsonify({"erro": "Escolha o canal antes de enviar."}), 400
    final = _pasta(tid) / "final" / cid / c["arquivo"]
    nome = _nome_arquivo(c.get("titulo_publicacao") or cid)
    # CTA em vídeo do canal: entra só aqui, numa cópia temporária do final.mp4 (o que foi revisado fica intacto,
    # então reenviar nunca duplica). Sem CTA no canal, ou se a anexação falhar, vai o original. O vídeo longo já
    # recebeu o CTA na produção.
    envio, tmp_cta = final, None
    if c.get("tipo") != "longo":
        try:
            tmp_cta = Path(tempfile.mkdtemp(prefix=".envio_cta_", dir=final.parent))
            com_cta, aviso = cta_cortador.anexar_copia(final, c["canal"], tmp_cta)
        except Exception as exc:  # noqa: BLE001 — falha do CTA nunca derruba o envio
            com_cta, aviso = None, f"erro inesperado: {exc}"
        if com_cta is not None:
            envio = com_cta
            _registrar(tid, f"   {cid}: CTA do canal {c['canal']} anexado ao envio ({aviso})")
        elif aviso:
            _registrar(tid, f"⚠️ {cid}: CTA do canal {c['canal']} não anexado ({aviso}); enviando sem ele")
    try:
        with envio.open("rb") as f:
            r = requests.post(carregar_config()["publicador_url"] + "/api/videos",
                              data={"canal": c["canal"]}, files={"arquivos": (nome, f, "video/mp4")},
                              timeout=600)
        resposta = r.json()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"erro": f"Publicador fora do ar? {exc}"}), 502
    finally:
        if tmp_cta is not None:
            shutil.rmtree(tmp_cta, ignore_errors=True)
    if r.status_code != 200 or not resposta.get("adicionados"):
        return jsonify({"erro": resposta.get("erro") or f"Publicador respondeu {r.status_code}"}), 502
    _atualizar_corte(tid, cid, status="na_fila", arquivo_publicador=resposta["adicionados"][0],
                     enviado_em=datetime.now().isoformat(timespec="seconds"), cta_no_envio=envio is not final)
    _fonte(tid, cid).unlink(missing_ok=True)          # bruto: só servia para a Fase 2/refazer
    _previa_longo_arquivo(tid, cid).unlink(missing_ok=True)
    _fase2_pronto(tid, cid).unlink(missing_ok=True)   # 16:9 pronto: idem
    _registrar(tid, f"📤 {cid} foi para a fila do Publicador ({c['canal']}): {resposta['adicionados'][0]}")
    return jsonify({"ok": True, "arquivo": resposta["adicionados"][0]})


@app.errorhandler(413)
def grande_demais(_erro):
    return jsonify({"erro": "Arquivo maior que 12 GB."}), 413


# Um processo só (waitress com threads) → exatamente um laço de trabalho, e a
# GPU nunca é disputada por dois trabalhos.
TRABALHOS_DIR.mkdir(parents=True, exist_ok=True)
historias_ponte.iniciar(app, DATA_DIR)
config_cortador.iniciar(DATA_DIR, EMAIL_ENV, sys.modules[__name__])
canais_publicacao.semear(DATA_DIR)
with _TRAVA:
    _ts = carregar_trabalhos()
    for _t in _ts:
        if _t["status"] == "processando":   # o serviço caiu no meio: retoma
            _t["status"], _t["etapa"] = "aguardando", "retomando depois de reiniciar"
    salvar_trabalhos(_ts)
    fila.reconstruir(_ts)    # fila do Redis do zero, a partir do que ficou pendente
threading.Thread(target=_laco, daemon=True, name="trabalhos").start()
if POUCA_RAM:
    log.warning("⚠️ %s", AVISO_POUCA_RAM)
    avisar_por_email(f"⚠️ Estúdio: servidor com {RAM_GB:.0f} GB de RAM — crop desligado",
                     AVISO_POUCA_RAM + "\n\nCortes pedidos em 'crop que segue quem fala' serão "
                     "cancelados até a RAM voltar; transparente e fotos do assunto seguem normais.")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("ESTUDIO_PORTA", 8090)), threaded=True)
