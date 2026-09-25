"""Corta+Legenda — editor de vídeo offline baseado em FFmpeg e Whisper."""

from __future__ import annotations

import os
import sys

os.environ["QT_DISABLE_HW_VIDEO_DECODING"] = "1"
os.environ["QT_FFMPEG_NO_HWACCEL"] = "1"
os.environ["QMEDIAPLAYER_USE_HW"] = "0"

import shutil
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

import vlc
from PySide6.QtCore import QDateTime, QEvent, QObject, QProcess, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QDateTimeEdit, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from utils import (
    APP_NAME, DOWNLOAD_DIR, FONT_DIR, OUTPUT_DIR,
    PROJECT_DIR, YTDLP_BUNDLED, YTDLP_SYSTEM,
    as_time, build_clip_filter, build_srt_for_clip, clean_srt_file, command_exists,
    escape_drawtext, filter_path, find_video_subtitle, format_title_for_video,
    build_reels_prompt, cuts_to_moments, looks_like_auto_caption,
    review_srt_with_ai,
    strip_markdown, transcript_with_timestamps,
    thumbnail_path, write_reels_prompt, write_reels_text,
    parse_csv_moments, parse_srt_segments, parse_time_string, segments_to_srt,
    shorten_srt_captions, srt_has_content, whisper_path, yt_dlp_path,
    TimestampInput, log_change, log_video,
)

import ai_srt
import censor
import trilhas
import tiktok_upload
import youtube_upload
import youtube_browser_upload
from cg_generator import (
    LT_BOTTOM_MARGIN, LT_HEIGHT, DEFAULT_COLORS, create_lower_third,
)

# Teto de duração dos cortes sugeridos pela IA. O prompt pede de 1min a 2min30; o
# piso de 1min fica só no prompt (a IA decide), mas o teto é reforçado por software
# porque um short longo demais não funciona. A folga de ~2s evita descartar um
# corte que a IA arredondou de leve para fora da faixa.
MAX_CUT_SECONDS = 152

# Padrões da identidade visual. São só o ponto de partida: a aba Configuração
# sobrescreve cada um e grava em config.json (chaves brand_*). O logo aponta para
# o caminho antigo do canal quando ele existe, senão fica vazio.
DEFAULT_WATERMARK = "@RENANSANTOSMBL"   # marca d'água no topo; "" desliga
DEFAULT_WM_COLOR = "#FFFF00"            # amarelo (equivale ao antigo "yellow")
DEFAULT_BRAND_NAME = "Informativo Nacional"
_LEGACY_LOGO = Path("G:/My Drive/Canais/Informativo Nacional/icone informativo.png")

# MarginV do libass é em unidades do script ASS (PlayResY ≈ 288 num .srt), não
# em pixels: 1 unidade ≈ 6,67 px num vídeo 9:16 (1920 px de altura). Converte a
# faixa ocupada pelo lower-third + uma folga para essas unidades, de modo que a
# legenda pare acima do CG em vez de flutuar no meio da tela.
#
# A folga precisa ser generosa: MarginV posiciona a *caixa* da linha, que é mais
# alta que os glifos (descida + contorno de 2,5), então uma folga pequena ainda
# deixa a legenda encostando no CG.
LT_CAPTION_GAP = 140
LT_CAPTION_MARGIN_V = round((LT_HEIGHT + LT_BOTTOM_MARGIN + LT_CAPTION_GAP) * 288 / 1920)


class WheelGuard(QObject):
    """Impede que combo/spin mudem de valor quando a roda do mouse passa por
    cima sem eles estarem em foco.

    Por padrão o Qt deixa esses controles capturarem a roda mesmo sem foco, o
    que rouba o scroll da página e ainda altera o valor. Aqui, se o controle
    não está em foco, a roda é repassada para a área rolável (a página desce) e
    o controle fica quieto. Para mexer no valor, basta clicar nele antes.
    """

    def eventFilter(self, obj, event) -> bool:
        if event.type() == QEvent.Type.Wheel and not obj.hasFocus():
            area = obj.parent()
            while area is not None and not isinstance(area, QAbstractScrollArea):
                area = area.parent()
            if area is not None:
                QApplication.sendEvent(area.viewport(), event)
            return True                      # não deixa o controle processar
        return False


class SrtReviewWorker(QThread):
    """Revisa um SRT com a IA fora da thread da interface.

    A chamada à API leva de alguns segundos a mais de um minuto; feita direto no
    clique, congelaria a janela. Cada fluxo (edição, CSV, live) conecta `done` e
    segue de onde parou quando a revisão termina.
    """

    progress = Signal(int, int)          # linhas prontas, total
    done = Signal(bool, str, str, str)   # ok?, mensagem, título, subtítulo
    flagged = Signal(list)               # trechos sensíveis apontados pela IA
    music = Signal(str)                  # clima da trilha escolhido pela IA

    def __init__(self, path: Path, api_key: str, model: str, context: str = "",
                 with_title: bool = False, musicas: list[str] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._path, self._api_key = path, api_key
        self._model, self._context = model, context
        self._with_title = with_title
        self._musicas = musicas or []

    def run(self) -> None:
        try:
            changed, total, usage, titulo, subtitulo, sensiveis, musica = review_srt_with_ai(
                self._path, self._api_key, self._model, self._context,
                progress=lambda ready, all_: self.progress.emit(ready, all_),
                with_title=self._with_title, musicas=self._musicas,
            )
        except ai_srt.AiError as error:
            self.done.emit(False, f"Revisão com IA falhou: {error}", "", "")
        except Exception as error:                     # noqa: BLE001
            self.done.emit(False, f"Revisão com IA falhou: {error}", "", "")
        else:
            self.flagged.emit(sensiveis)
            self.music.emit(musica)
            enviados = int(usage.get("prompt_tokens") or 0)
            recebidos = int(usage.get("completion_tokens") or 0)
            tok = ""
            if enviados or recebidos:
                tok = f" — {enviados} tokens enviados, {recebidos} recebidos"
            self.done.emit(
                True, f"Legenda revisada pela IA: {changed}/{total} blocos alterados{tok}.",
                titulo, subtitulo)


class CutsSuggestWorker(QThread):
    """Pede ao DeepSeek a lista de cortes de um vídeo, fora da thread da interface.

    Recebe a transcrição inteira (com tempos) e devolve os cortes que a IA
    escolheu. A chamada pode levar bastante numa live longa, daí a thread.
    """

    done = Signal(bool, str, list)       # ok?, mensagem, cortes

    def __init__(self, transcript: str, api_key: str, model: str,
                 musicas: list[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self._transcript, self._api_key, self._model = transcript, api_key, model
        self._musicas = musicas or []

    def run(self) -> None:
        try:
            cortes, usage = ai_srt.suggest_cuts(
                self._transcript, self._api_key, self._model, self._musicas)
        except ai_srt.AiError as error:
            self.done.emit(False, f"A IA não conseguiu escolher os cortes: {error}", [])
            return
        except Exception as error:                     # noqa: BLE001
            self.done.emit(False, f"A IA não conseguiu escolher os cortes: {error}", [])
            return
        enviados = int(usage.get("prompt_tokens") or 0)
        recebidos = int(usage.get("completion_tokens") or 0)
        self.done.emit(
            True,
            f"IA sugeriu {len(cortes)} corte(s) "
            f"({enviados} tokens enviados, {recebidos} recebidos).",
            cortes)


class ReelsCaptionWorker(QThread):
    """Pede ao DeepSeek a legenda de Instagram do corte, fora da thread da interface.

    Roda depois que o vídeo já está pronto, então uma falha aqui não estraga a
    exportação — no pior caso o .txt sai com o prompt, para o usuário colar
    numa IA por conta própria.
    """

    done = Signal(bool, str, object)     # ok?, mensagem, caminho do .txt

    def __init__(self, prompt: str, dest: Path, api_key: str, model: str,
                 parent=None) -> None:
        super().__init__(parent)
        self._prompt, self._dest = prompt, dest
        self._api_key, self._model = api_key, model

    def run(self) -> None:
        try:
            texto, usage = ai_srt.ask(self._prompt, self._api_key, self._model)
        except Exception as error:                     # noqa: BLE001
            caminho = write_reels_text(self._dest, self._prompt)
            self.done.emit(
                False,
                f"Não deu para gerar a legenda do Reels ({error}). "
                "O .txt saiu com o prompt, para você colar numa IA.",
                caminho)
            return
        caminho = write_reels_text(self._dest, strip_markdown(texto))
        recebidos = int(usage.get("completion_tokens") or 0)
        enviados = int(usage.get("prompt_tokens") or 0)
        self.done.emit(
            True,
            f"Legenda do Reels gerada pela IA ({enviados} tokens enviados, "
            f"{recebidos} recebidos).",
            caminho)


class YoutubeUploadWorker(QThread):
    """Envia o vídeo exportado ao YouTube, fora da thread da interface.

    O upload é resumível e pode levar minutos num vídeo grande; feito direto
    no clique, travaria a janela até terminar.
    """

    progress = Signal(float)             # fração enviada (0.0 a 1.0)
    done = Signal(bool, str)             # ok?, mensagem

    def __init__(self, video_path: Path, title: str, account: str, description: str,
                 tags: list[str], privacy: str, publish_at: datetime | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._video_path, self._title, self._account = video_path, title, account
        self._description, self._tags, self._privacy = description, tags, privacy
        self._publish_at = publish_at

    def run(self) -> None:
        try:
            url = youtube_upload.upload_video(
                self._video_path, self._title, self._account, self._description,
                self._tags, self._privacy, publish_at=self._publish_at,
                progress=lambda frac: self.progress.emit(frac))
        except youtube_upload.YoutubeUploadError as error:
            self.done.emit(False, f"Envio ao YouTube falhou: {error}")
        except Exception as error:                     # noqa: BLE001
            self.done.emit(False, f"Envio ao YouTube falhou: {error}")
        else:
            if self._publish_at:
                self.done.emit(
                    True,
                    f"Vídeo enviado e agendado para {self._publish_at:%d/%m/%Y %H:%M}: {url}")
            else:
                self.done.emit(True, f"Vídeo publicado no YouTube: {url}")


class TikTokUploadWorker(QThread):
    """Envia o vídeo exportado ao TikTok, fora da thread da interface.

    Ao contrário do YouTube, o envio aqui é feito controlando um navegador de
    verdade (biblioteca não-oficial `tiktok-uploader`) e não avisa o
    progresso aos pedaços — só quando termina, com sucesso ou não. Como abre
    uma janela de navegador, feito direto no clique travaria a interface até
    o fim do preenchimento da tela de upload.
    """

    done = Signal(bool, str)             # ok?, mensagem

    def __init__(self, video_path: Path, description: str, account: str,
                 visibility: str, publish_at: datetime | None = None,
                 headless: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._video_path, self._description, self._account = video_path, description, account
        self._visibility = visibility
        self._publish_at = publish_at
        self._headless = headless

    def run(self) -> None:
        try:
            mensagem = tiktok_upload.upload_video(
                self._video_path, self._description, self._account,
                self._visibility, publish_at=self._publish_at, headless=self._headless)
        except tiktok_upload.TikTokUploadError as error:
            self.done.emit(False, f"Envio ao TikTok falhou: {error}")
        except Exception as error:                  # noqa: BLE001
            self.done.emit(False, f"Envio ao TikTok falhou: {error}")
        else:
            self.done.emit(True, mensagem)


class YoutubeBrowserUploadWorker(QThread):
    """Envia o vídeo ao YouTube pela tela do Studio, fora da thread da interface.

    Mesmo esquema do TikTokUploadWorker: controla um navegador de verdade
    (Playwright), então feito direto no clique travaria a interface até o
    fim do preenchimento da tela de upload. Ao contrário do TikTok, este
    fluxo avisa o progresso passo a passo (via `log`), porque o Studio tem
    várias etapas (detalhes, verificações, visibilidade) que levam tempo.
    """

    log = Signal(str)                    # mensagem de progresso
    done = Signal(bool, str)             # ok?, mensagem

    def __init__(self, video_path: Path, title: str, description: str, account: str,
                 visibility: str, publish_at: datetime | None = None,
                 headless: bool = False, parent=None) -> None:
        super().__init__(parent)
        self._video_path, self._title, self._description = video_path, title, description
        self._account, self._visibility = account, visibility
        self._publish_at = publish_at
        self._headless = headless

    def run(self) -> None:
        try:
            mensagem = youtube_browser_upload.upload_video(
                self._video_path, self._title, self._account, self._description,
                self._visibility, publish_at=self._publish_at, headless=self._headless,
                log=lambda msg: self.log.emit(msg))
        except youtube_browser_upload.YoutubeBrowserUploadError as error:
            self.done.emit(False, f"Envio ao YouTube (navegador) falhou: {error}")
        except Exception as error:                  # noqa: BLE001
            self.done.emit(False, f"Envio ao YouTube (navegador) falhou: {error}")
        else:
            self.done.emit(True, mensagem)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 780)
        self.video_path: Path | None = None
        self.fixed_image_path: Path | None = None
        self.caption_path: Path | None = None
        # Identidade visual (logo, nome, marca d'água e cores da tarja). Vem do
        # config.json e é editável na aba Configuração. Precisa carregar antes de
        # montar as abas, que já usam o logo e o nome nos rótulos.
        self._load_branding()
        self.duration = 0.0
        self._reels_queue: list[tuple] = []      # legendas de Reels a pedir à IA
        self._reels_worker: ReelsCaptionWorker | None = None
        self._ai_flagged: list[dict] = []        # trechos sensíveis da última revisão
        self._music_track: Path | None = None    # trilha escolhida pela IA
        self.work_dir = Path(tempfile.mkdtemp(prefix="corta_legenda_"))
        self.process: QProcess | None = None
        self._thumbnail_procs: set[QProcess] = set()   # mantém os QProcess de thumb vivos até terminar
        self.playback_speed = 1.0
        self._vlc_seeking = False

        self.vlc_instance = vlc.Instance("--avcodec-hw=none")  # Desabilita hardware decoding
        self.vlc_player = self.vlc_instance.media_player_new()
        self.vlc_media: vlc.Media | None = None
        # Player só da trilha: separado dos de vídeo, para ouvir a música sem
        # mexer no que estiver tocando na pré-visualização.
        self._music_player = self.vlc_instance.media_player_new()
        self.csv_vlc_player = self.vlc_instance.media_player_new()
        self.csv_vlc_media: vlc.Media | None = None
        self._csv_selected_index = -1
        self._csv_duration = 0.0

        # Estado da aba Live
        self.live_vlc_player = self.vlc_instance.media_player_new()
        self.live_vlc_media: vlc.Media | None = None
        self._live_duration = 0.0
        self._live_dir: Path | None = None
        self._live_process: QProcess | None = None
        self._live_audio_process: QProcess | None = None
        self._live_recording = False
        self._live_busy = False
        self._live_fixed_image_path: Path | None = None
        self._live_cookies_file: Path | None = None
        self._live_cut_process: QProcess | None = None
        self._live_cut_count = 0
        self._live_pending: dict = {}
        self._live_needs_remux = False
        self._live_remux_running = False
        self._live_remux_goal: object = 0
        self._live_remux_count = 0
        self._live_remux_process: QProcess | None = None

        # Fila de publicação no YouTube (aba própria) — carregada do .txt
        # salvo da última vez, para os vídeos importados sobreviverem entre
        # sessões.
        self._youtube_queue: list[youtube_upload.QueueItem] = youtube_upload.load_queue()
        self._youtube_queue_worker: YoutubeUploadWorker | None = None
        self._youtube_queue_busy = False
        # Começa parada de propósito: sem isso, vídeos importados com
        # "Enviar em" = agora sairiam sozinhos em segundos, antes de dar
        # tempo de escolher canal, privacidade ou agendar a publicação.
        self._youtube_queue_running = False

        # Fila de publicação no TikTok (aba própria) — mesma ideia da fila do
        # YouTube, só que via biblioteca não-oficial (navegador automatizado).
        self._tiktok_queue: list[tiktok_upload.QueueItem] = tiktok_upload.load_queue()
        self._tiktok_queue_worker: TikTokUploadWorker | None = None
        self._tiktok_queue_busy = False
        self._tiktok_queue_running = False

        # Fila de publicação no YouTube pela tela do Studio (aba própria) —
        # mesma ideia da fila do TikTok: navegador automatizado com cookies,
        # em vez da API oficial (cota apertada e vídeos que quase não eram
        # entregues, na experiência medida neste projeto).
        self._youtube_browser_queue: list[youtube_browser_upload.QueueItem] = youtube_browser_upload.load_queue()
        self._youtube_browser_queue_worker: YoutubeBrowserUploadWorker | None = None
        self._youtube_browser_queue_busy = False
        self._youtube_browser_queue_running = False

        self.build_ui()

        # Timer para atualizar timeline e duração (VLC não tem sinais Qt)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_vlc)
        self._poll_timer.timeout.connect(self._csv_poll_vlc)
        self._poll_timer.timeout.connect(self._live_poll_vlc)
        self._poll_timer.start()

        # Atualiza o status da live (até onde vídeo e áudio foram baixados)
        self._live_status_timer = QTimer(self)
        self._live_status_timer.setInterval(10000)
        self._live_status_timer.timeout.connect(self._live_update_status)
        self._live_status_timer.start()

        # Confere a fila do YouTube a cada 30s e dispara o envio de quem já
        # chegou no horário marcado.
        self._youtube_queue_timer = QTimer(self)
        self._youtube_queue_timer.setInterval(30000)
        self._youtube_queue_timer.timeout.connect(self._check_youtube_queue_schedule)
        self._youtube_queue_timer.start()

        # Mesma lógica para a fila do TikTok.
        self._tiktok_queue_timer = QTimer(self)
        self._tiktok_queue_timer.setInterval(30000)
        self._tiktok_queue_timer.timeout.connect(self._check_tiktok_queue_schedule)
        self._tiktok_queue_timer.start()

        # Mesma lógica para a fila do YouTube via navegador (Studio).
        self._youtube_browser_queue_timer = QTimer(self)
        self._youtube_browser_queue_timer.setInterval(30000)
        self._youtube_browser_queue_timer.timeout.connect(self._check_youtube_browser_queue_schedule)
        self._youtube_browser_queue_timer.start()

    def _running_processes(self) -> list[QProcess]:
        """Todo QProcess que a janela pode ter em execução neste momento."""
        procs = [
            self.process, getattr(self, "_csv_process", None), getattr(self, "_csv_cuts_proc", None),
            self._live_process, self._live_audio_process, self._live_cut_process,
        ]
        procs.extend(self._thumbnail_procs)
        return [p for p in procs if p is not None]

    def closeEvent(self, event) -> None:
        """Evita deixar FFmpeg/yt-dlp órfão ao fechar a janela no meio de um corte.

        Sem isso, fechar durante um lote (CSV, Live ou até a geração da capa)
        deixa o QProcess ser destruído com o processo ainda rodando; quando o
        `finished` chega tarde, tenta atualizar widgets que a janela já
        apagou e vira RuntimeError ("already deleted") no console.
        """
        running = [p for p in self._running_processes() if p.state() != QProcess.ProcessState.NotRunning]
        if running:
            resposta = QMessageBox.question(
                self, APP_NAME,
                "Ainda tem processamento em andamento (corte, download ou legenda).\n\n"
                "Fechar agora interrompe tudo o que está rodando. Fechar mesmo assim?")
            if resposta != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for proc in running:
                try:
                    proc.finished.disconnect()
                except (TypeError, RuntimeError):
                    pass
                proc.kill()
                proc.waitForFinished(2000)
        event.accept()

    def build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(0, 0, 0, 0)
        self.setCentralWidget(root)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("mainTabs")
        root_layout.addWidget(self.tabs)

        # ── Aba 1: Edição ──────────────────────────────────────────
        tab_edit = QWidget()
        edit_layout = QHBoxLayout(tab_edit)
        edit_layout.setContentsMargins(22, 22, 22, 22)
        edit_layout.setSpacing(20)

        # --- preview column ---
        preview_column = QVBoxLayout()
        title = QLabel("Corta+Legenda")
        title.setObjectName("title")
        subtitle = QLabel("Edite e legende vídeos sem enviar nada para a internet.")
        subtitle.setObjectName("muted")
        preview_column.addWidget(title)
        preview_column.addWidget(subtitle)

        self.video_widget = QFrame()
        self.video_widget.setMinimumSize(600, 390)
        self.video_widget.setStyleSheet("background: #10131a; border-radius: 12px;")
        self.video_widget.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video_widget.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        preview_column.addWidget(self.video_widget, 1)
        self.video_widget.windowHandle()

        self.timeline = QSlider(Qt.Orientation.Horizontal)
        self.timeline.setRange(0, 0)
        self.timeline.setObjectName("timeline")
        self.timeline.sliderMoved.connect(lambda v: self._vlc_seek(v))
        self.timeline.sliderPressed.connect(lambda: self._vlc_seek(self.timeline.value()))
        preview_column.addWidget(self.timeline)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.play_button = QPushButton("▶")
        self.play_button.setObjectName("controlButton")
        self.play_button.setFixedSize(42, 42)
        self.play_button.setToolTip("Reproduzir / Pausar")
        self.play_button.clicked.connect(self.toggle_play)

        self.skip_back = QPushButton("⏪")
        self.skip_back.setObjectName("controlButton")
        self.skip_back.setFixedSize(42, 42)
        self.skip_back.setToolTip("Voltar 5s")
        self.skip_back.clicked.connect(lambda: self.vlc_player.set_time(max(0, self.vlc_player.get_time() - 5000)))

        self.skip_fwd = QPushButton("⏩")
        self.skip_fwd.setObjectName("controlButton")
        self.skip_fwd.setFixedSize(42, 42)
        self.skip_fwd.setToolTip("Avançar 5s")
        self.skip_fwd.clicked.connect(lambda: self.vlc_player.set_time(self.vlc_player.get_time() + 5000))

        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setObjectName("timeDisplay")
        self.time_label.setMinimumWidth(130)

        controls.addWidget(self.play_button)
        controls.addWidget(self.skip_back)
        controls.addWidget(self.skip_fwd)
        controls.addWidget(self.time_label)
        controls.addStretch()

        speed_label = QLabel("Velocidade")
        speed_label.setObjectName("muted")
        controls.addWidget(speed_label)

        self.speed_1x = QPushButton("1×")
        self.speed_1x.setObjectName("speedActive")
        self.speed_1x.setFixedSize(38, 28)
        self.speed_1x.setToolTip("Velocidade normal")
        self.speed_1x.clicked.connect(lambda: self.set_speed(1.0))

        self.speed_1_5x = QPushButton("1.5×")
        self.speed_1_5x.setObjectName("speedButton")
        self.speed_1_5x.setFixedSize(44, 28)
        self.speed_1_5x.setToolTip("Velocidade 1.5×")
        self.speed_1_5x.clicked.connect(lambda: self.set_speed(1.5))

        self.speed_2x = QPushButton("2×")
        self.speed_2x.setObjectName("speedButton")
        self.speed_2x.setFixedSize(38, 28)
        self.speed_2x.setToolTip("Velocidade 2×")
        self.speed_2x.clicked.connect(lambda: self.set_speed(2.0))

        controls.addWidget(self.speed_1x)
        controls.addWidget(self.speed_1_5x)
        controls.addWidget(self.speed_2x)
        preview_column.addLayout(controls)

        # Log em baixo do vídeo
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(300)
        self.log.setMaximumHeight(120)
        log_label = QLabel("Log de exportação")
        log_label.setObjectName("muted")
        preview_column.addWidget(log_label)
        preview_column.addWidget(self.progress)
        preview_column.addWidget(self.log)
        edit_layout.addLayout(preview_column, 3)

        # --- panel coluna direita ---
        panel = QVBoxLayout()
        panel.setSpacing(14)
        source_box = QGroupBox("Vídeo de origem")
        source_layout = QVBoxLayout(source_box)
        source_layout.setSpacing(9)
        local_row = QHBoxLayout()
        upload = QPushButton("Escolher arquivo")
        upload.clicked.connect(self.pick_video)
        self.file_label = QLabel("Nenhum vídeo selecionado")
        self.file_label.setWordWrap(True)
        self.file_label.setObjectName("selectedFile")
        local_row.addWidget(upload)
        local_row.addWidget(self.file_label, 1)
        source_layout.addLayout(local_row)

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("sourceSeparator")
        source_layout.addWidget(separator)

        url_label = QLabel("Ou cole uma URL para baixar")
        url_label.setObjectName("sourceLabel")
        source_layout.addWidget(url_label)
        self.url_input = QLineEdit()
        self.url_input.setObjectName("urlInput")
        self.url_input.setClearButtonEnabled(True)
        self.url_input.setPlaceholderText("https://...")
        self.url_input.returnPressed.connect(self.download_video)
        self.download_button = QPushButton("Baixar com yt-dlp")
        self.download_button.setObjectName("downloadButton")
        self.download_button.clicked.connect(self.download_video)
        acceleration_row = QHBoxLayout()
        acceleration_label = QLabel("Conexões simultâneas")
        self.fragment_count = QSpinBox()
        self.fragment_count.setRange(1, 16)
        self.fragment_count.setValue(8)
        self.fragment_count.setSuffix(" conexões")
        self.fragment_count.setToolTip("Baixa fragmentos de vídeos HLS/DASH em paralelo.")
        acceleration_row.addWidget(acceleration_label)
        acceleration_row.addStretch()
        acceleration_row.addWidget(self.fragment_count)
        download_hint = QLabel("Mais conexões podem acelerar vídeos segmentados.")
        download_hint.setObjectName("muted")
        self.download_subs = QCheckBox("Baixar também a transcrição/legenda do vídeo (pt-br)")
        self.download_subs.setChecked(True)
        self.download_subs.setToolTip(
            "Baixa as legendas do próprio vídeo (oficiais ou automáticas do YouTube) "
            "em pt-br, convertidas para .srt ao lado do vídeo."
        )

        time_range_label = QLabel("Intervalo de tempo")
        time_range_label.setObjectName("sourceLabel")
        self.download_time_mode = QComboBox()
        self.download_time_mode.addItem("Tudo", "all")
        self.download_time_mode.addItem("Personalizado", "custom")
        self.download_time_mode.setCurrentIndex(0)
        self.download_time_mode.currentIndexChanged.connect(self._toggle_download_time_inputs)

        self.download_time_row = QWidget()
        download_time_layout = QVBoxLayout(self.download_time_row); download_time_layout.setContentsMargins(0, 0, 0, 0)
        download_time_layout.setSpacing(8)

        start_time_layout = QHBoxLayout()
        start_time_layout.setContentsMargins(0, 0, 0, 0)
        start_time_label = QLabel("Início")
        self.download_start_time = self.time_input()
        start_time_layout.addWidget(start_time_label)
        start_time_layout.addWidget(self.download_start_time, 1)
        download_time_layout.addLayout(start_time_layout)

        end_time_layout = QHBoxLayout()
        end_time_layout.setContentsMargins(0, 0, 0, 0)
        end_time_label = QLabel("Fim")
        self.download_end_time = self.time_input()
        end_time_layout.addWidget(end_time_label)
        end_time_layout.addWidget(self.download_end_time, 1)
        download_time_layout.addLayout(end_time_layout)

        source_layout.addWidget(self.url_input)
        source_layout.addLayout(acceleration_row)
        source_layout.addWidget(self.download_button)
        source_layout.addWidget(time_range_label)
        source_layout.addWidget(self.download_time_mode)
        source_layout.addWidget(self.download_time_row)
        self.download_time_row.setVisible(False)
        source_layout.addWidget(self.download_subs)
        source_layout.addWidget(download_hint)
        panel.addWidget(source_box)

        cut_box = QGroupBox("1. Recorte")
        cut_form = QFormLayout(cut_box)
        self.start_input = self.time_input()
        self.end_input = self.time_input()
        self.start_input.secondsChanged.connect(self.sync_cut_range)
        self.end_input.secondsChanged.connect(self.sync_cut_range)
        # Inputs com botões "Marcar" para pegar tempo atual
        start_row = QWidget()
        start_layout = QHBoxLayout(start_row); start_layout.setContentsMargins(0, 0, 0, 0)
        mark_start = QPushButton("📍 Marcar")
        mark_start.setMaximumWidth(90)
        mark_start.setToolTip("Usa a posição atual do vídeo")
        mark_start.clicked.connect(lambda: self.start_input.setValue(self.vlc_player.get_time() / 1000))
        start_layout.addWidget(self.start_input, 1)
        start_layout.addWidget(mark_start)

        end_row = QWidget()
        end_layout = QHBoxLayout(end_row); end_layout.setContentsMargins(0, 0, 0, 0)
        mark_end = QPushButton("📍 Marcar")
        mark_end.setMaximumWidth(90)
        mark_end.setToolTip("Usa a posição atual do vídeo")
        mark_end.clicked.connect(lambda: self.end_input.setValue(self.vlc_player.get_time() / 1000))
        end_layout.addWidget(self.end_input, 1)
        end_layout.addWidget(mark_end)

        cut_form.addRow("Início", start_row)
        cut_form.addRow("Fim", end_row)
        panel.addWidget(cut_box)

        format_box = QGroupBox("2. Formato")
        format_form = QFormLayout(format_box)
        self.ratio = QComboBox()
        self.ratio.addItem("Original", "original")
        self.ratio.addItem("Vertical 9:16 — preencher", "vertical_crop")
        self.ratio.addItem("Vertical 9:16 — fundo desfocado", "vertical_blur")
        self.ratio.addItem("Vertical 9:16 — imagem fixa", "vertical_image")
        self.ratio.setCurrentIndex(1)
        self.ratio.currentIndexChanged.connect(self._toggle_fixed_image_row)
        format_form.addRow("Saída", self.ratio)

        self.fixed_image_row = QWidget()
        fixed_layout = QHBoxLayout(self.fixed_image_row); fixed_layout.setContentsMargins(0, 0, 0, 0)
        self.fixed_image_label = QLabel("Nenhuma imagem")
        self.fixed_image_label.setWordWrap(True)
        fixed_button = QPushButton("Escolher")
        fixed_button.clicked.connect(self.pick_fixed_image)
        fixed_layout.addWidget(self.fixed_image_label, 1); fixed_layout.addWidget(fixed_button)
        format_form.addRow("Imagem (topo)", self.fixed_image_row)
        self.fixed_image_row.setVisible(False)
        panel.addWidget(format_box)

        # Ordem lógica: primeiro as legendas (3), depois a IA que as revisa e
        # sugere o texto (4), e por fim os campos de texto que a IA preenche (5).
        caption_box = QGroupBox("3. Legendas")
        caption_layout = QVBoxLayout(caption_box)
        self.caption_status = QLabel("As legendas serão geradas somente para o recorte.")
        self.caption_status.setWordWrap(True)
        caption_style = QLabel("Estilo: Montserrat amarela, contorno preto e blocos curtos.")
        caption_style.setObjectName("muted")
        self.caption_button = QPushButton("Gerar legendas do recorte")
        self.caption_button.clicked.connect(self.generate_captions)
        caption_layout.addWidget(self.caption_status)
        caption_layout.addWidget(caption_style)

        # O modelo decide a qualidade da transcrição — e, por tabela, o que a
        # revisão com IA e a censura conseguem fazer, já que ambas só enxergam
        # o texto que o Whisper escreveu.
        model_row = QFormLayout()
        self.whisper_model = QComboBox()
        for label, value in (("small — recomendado", "small"),
                             ("base — rápido, erra bastante", "base"),
                             ("medium — melhor, bem mais lento", "medium")):
            self.whisper_model.addItem(label, value)
        saved_model = ai_srt.load_config().get("whisper_model", "small")
        index = self.whisper_model.findData(saved_model)
        self.whisper_model.setCurrentIndex(index if index >= 0 else 0)
        self.whisper_model.currentIndexChanged.connect(
            lambda: ai_srt.save_config({"whisper_model": self.whisper_model.currentData()}))
        model_row.addRow("Modelo", self.whisper_model)
        caption_layout.addLayout(model_row)

        caption_layout.addWidget(self.caption_button)
        panel.addWidget(caption_box)

        panel.addWidget(self._build_ai_box())

        overlay_box = QGroupBox("5. Texto")
        overlay_form = QFormLayout(overlay_box)
        # Título = chapéu pequeno no topo do GC; Subtítulo = manchete grande embaixo.
        self.text_input = QLineEdit("POLÍTICA")
        self.text_input.setPlaceholderText("Chapéu/tema, no topo (ex.: ECONOMIA)")
        self.subtitle_input = QLineEdit("")
        self.subtitle_input.setPlaceholderText("Manchete em destaque, embaixo (linha grande)")
        overlay_form.addRow("Título", self.text_input)
        overlay_form.addRow("Subtítulo", self.subtitle_input)
        self.use_cg = QCheckBox(f"Usar tarja '{self.brand_name}' (rodapé)")
        self.use_cg.setChecked(bool(self.cg_icon_path))
        self.use_cg.setEnabled(bool(self.cg_icon_path))
        overlay_form.addRow(self.use_cg)
        panel.addWidget(overlay_box)

        panel.addWidget(self._build_censor_box())
        panel.addWidget(self._build_music_box())

        self.export_button = QPushButton("Exportar vídeo")
        self.export_button.setObjectName("primary")
        self.export_button.clicked.connect(self.export_video)
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self.cancel_edit)
        self.cancel_button.setEnabled(False)
        panel.addWidget(self.export_button)
        panel.addWidget(self.cancel_button)
        panel.addStretch()
        panel_content = QWidget()
        panel_content.setObjectName("settingsPanel")
        panel_content.setLayout(panel)
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setFrameShape(QFrame.Shape.NoFrame)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        settings_scroll.setMinimumWidth(390)
        settings_scroll.setWidget(panel_content)
        edit_layout.addWidget(settings_scroll, 2)

        self.tabs.addTab(tab_edit, "🎬 Edição")

        # ── Aba 2: CSV Lotes ───────────────────────────────────────
        self._build_csv_tab()

        # ── Aba 3: Live ────────────────────────────────────────────
        self._build_live_tab()

        # ── Aba 4: YouTube (fila de publicação, API oficial) ─────────
        self._build_youtube_tab()

        # ── Aba 5: YouTube via navegador (fila de publicação, Studio) ─
        self._build_youtube_browser_tab()

        # ── Aba 6: TikTok (fila de publicação) ───────────────────────
        self._build_tiktok_tab()

        # ── Aba 6: Configuração (identidade visual) ──────────────────
        self._build_config_tab()

        # A roda do mouse rola a página, não altera combos/spins de passagem.
        self._wheel_guard = WheelGuard(self)
        for kind in (QComboBox, QAbstractSpinBox):
            for widget in self.findChildren(kind):
                widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                widget.installEventFilter(self._wheel_guard)

        self.apply_style()

    def apply_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background: #0f1119; color: #e1e4ed; }
            QWidget#settingsPanel { background: #0f1119; }
            QScrollArea { background: transparent; border: none; }
            QScrollBar:vertical { background: transparent; width: 10px; margin: 4px 0; }
            QScrollBar::handle:vertical { background: #2d3140; border-radius: 5px; min-height: 34px; }
            QScrollBar::handle:vertical:hover { background: #4a5070; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QLabel { color: #cdd2e0; }
            QLabel#title { font-size: 28px; font-weight: 700; color: #e8ecf4; }
            QLabel#muted { color: #8890a8; }
            QLabel#timeDisplay { color: #c8cedc; font-size: 13px; font-weight: 600;
                background: #1a1d2c; border-radius: 8px; padding: 6px 14px; margin: 0; }
            QGroupBox { font-weight: 700; border: 1px solid #232738; border-radius: 10px; margin-top: 11px;
                padding: 10px; background: #141723; color: #dce1f0; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px; }
            QPushButton { padding: 9px 12px; border: 1px solid #2d3140; border-radius: 7px;
                background: #1a1d2c; color: #cdd2e0; }
            QPushButton:hover { background: #252a3e; }
            QPushButton#primary { background: #6366f1; color: white; border: none; font-weight: 700; padding: 13px 16px; }
            QPushButton#primary:hover { background: #4f46e5; }
            QPushButton#cancelButton { background: #2a1a1f; color: #f0a0a8; border: 1px solid #7f2f3a; font-weight: 600; }
            QPushButton#cancelButton:hover { background: #7f2f3a; color: white; }
            QPushButton#cancelButton:disabled { background: #1a1d2c; color: #555a6a; border: 1px solid #2d3140; }
            QPushButton#downloadButton { background: #3b82f6; color: white; border: none; font-weight: 600; }
            QPushButton#downloadButton:hover { background: #2563eb; }
            QPushButton#controlButton { background: #1a1d2c; border: 1px solid #2d3140; border-radius: 8px;
                font-size: 16px; padding: 0; color: #cdd2e0; }
            QPushButton#controlButton:hover { background: #2d3140; }
            QPushButton#speedButton { background: transparent; border: 1px solid #2d3140; border-radius: 6px;
                font-size: 12px; font-weight: 600; padding: 2px; color: #8890a8; }
            QPushButton#speedButton:hover { background: #252a3e; color: #c8cedc; }
            QPushButton#speedActive { background: #6366f1; border: 1px solid #6366f1; border-radius: 6px;
                font-size: 12px; font-weight: 700; padding: 2px; color: white; }
            QSlider#timeline::groove:horizontal { background: #1e2235; height: 6px; border-radius: 3px; }
            QSlider#timeline::handle:horizontal { background: #6366f1; width: 16px; height: 16px;
                margin: -5px 0; border-radius: 8px; }
            QSlider#timeline::sub-page:horizontal { background: #6366f1; border-radius: 3px; }
            QLabel#sourceLabel { color: #a5adc4; font-weight: 600; }
            QLabel#selectedFile { color: #bcc3d4; background: #1a1d2c; border-radius: 6px; padding: 8px; }
            QFrame#sourceSeparator { color: #232738; }
            QLineEdit#urlInput { background: #1a1d2c; min-height: 26px; color: #cdd2e0;
                border: 1px solid #2d3140; border-radius: 6px; padding: 6px; }
            QLineEdit, QComboBox, QSpinBox, QPlainTextEdit { border: 1px solid #2d3140; border-radius: 6px;
                padding: 6px; background: #1a1d2c; color: #cdd2e0; }
            QComboBox QAbstractItemView { background: #1a1d2c; color: #cdd2e0;
                border: 1px solid #2d3140; outline: none;
                selection-background-color: #6366f1; selection-color: white; }
            QComboBox::drop-down { border: none; width: 22px; }
            QCheckBox { color: #cdd2e0; spacing: 8px; }
            QCheckBox::indicator { width: 16px; height: 16px; border: 1px solid #2d3140;
                border-radius: 4px; background: #1a1d2c; }
            QCheckBox::indicator:checked { background: #6366f1; border-color: #6366f1; }
            QMessageBox { background: #141723; }
            QMessageBox QLabel { color: #e1e4ed; }
            QProgressBar { border: none; border-radius: 4px; background: #1e2235; height: 7px; }
            QProgressBar::chunk { background: #6366f1; border-radius: 4px; }
            QTabWidget::pane { border: 1px solid #232738; border-radius: 10px; background: #0f1119; }
            QTabBar::tab { padding: 8px 18px; border: 1px solid #232738; border-radius: 8px;
                margin-right: 4px; background: #1a1d2c; color: #8890a8; font-weight: 600; }
            QTabBar::tab:selected { background: #6366f1; color: white; border-color: #6366f1; }
            QTabBar::tab:hover:!selected { background: #252a3e; color: #c8cedc; }
            QTableWidget { background: #1a1d2c; border: 1px solid #2d3140; border-radius: 6px; gridline-color: #2d3140; }
            QTableWidget::item { color: #cdd2e0; }
            QHeaderView::section { background: #141723; color: #a5adc4; padding: 4px 8px;
                border: 1px solid #232738; font-weight: 700; }
        """)

    def time_input(self) -> TimestampInput:
        return TimestampInput()

    def pick_video(self) -> None:
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        filename, _ = QFileDialog.getOpenFileName(self, "Selecionar vídeo", str(DOWNLOAD_DIR), "Vídeos (*.mp4 *.mov *.mkv *.avi *.webm)")
        if not filename:
            return
        self.video_path = Path(filename)
        self.caption_path = None
        self.file_label.setText(self.video_path.name)
        self.csv_video_label.setText(self.video_path.name)
        self.log.clear()
        self.caption_status.setText("As legendas serão geradas somente para o recorte.")

        self.vlc_player.stop()
        self.vlc_media = self.vlc_instance.media_new(str(self.video_path))
        self.vlc_player.set_media(self.vlc_media)
        if self.video_widget.winId():
            self.vlc_player.set_hwnd(int(self.video_widget.winId()))
        self.play_button.setText("▶")
        self.vlc_media.parse_with_options(vlc.MediaParseFlag.local, -1)

    def pick_fixed_image(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Selecionar imagem do topo", "", "Imagens (*.png *.jpg *.jpeg *.webp)")
        if filename:
            self.fixed_image_path = Path(filename)
            self.fixed_image_label.setText(self.fixed_image_path.name)

    def _toggle_download_time_inputs(self) -> None:
        show = self.download_time_mode.currentData() == "custom"
        self.download_time_row.setVisible(show)

    def _toggle_fixed_image_row(self) -> None:
        self.fixed_image_row.setVisible(self.ratio.currentData() == "vertical_image")

    def _poll_vlc(self) -> None:
        """Atualiza timeline e label de tempo via VLC (polling 100ms)."""
        if self.vlc_player.get_media() is None:
            return
        # Duração
        length = self.vlc_player.get_length()
        if length > 0 and int(length) != int(self.duration * 1000):
            self.duration = length / 1000
            self.timeline.setRange(0, length)
            for spin in (self.start_input, self.end_input):
                spin.setMaximum(self.duration)
            self.end_input.blockSignals(True)
            self.end_input.setValue(self.duration)
            self.end_input.blockSignals(False)
        # Posição
        pos = self.vlc_player.get_time()
        if not self.timeline.isSliderDown():
            self.timeline.setValue(pos)
        self.time_label.setText(f"{as_time(pos / 1000)} / {as_time(self.duration)}")

    def _vlc_seek(self, ms: int) -> None:
        self.vlc_player.set_time(ms)

    def toggle_play(self) -> None:
        if self.vlc_player.is_playing():
            self.vlc_player.pause()
            self.play_button.setText("▶")
        else:
            self.vlc_player.play()
            self.play_button.setText("❚❚")

    def set_speed(self, rate: float) -> None:
        self.playback_speed = rate
        self.vlc_player.set_rate(rate)
        for btn in (self.speed_1x, self.speed_1_5x, self.speed_2x):
            btn.setObjectName("speedActive" if btn.text() == f"{rate}×" else "speedButton")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def sync_cut_range(self) -> None:
        if self.end_input.value() <= self.start_input.value():
            self.end_input.setValue(min(self.duration, self.start_input.value() + 0.25))
        self.caption_path = None
        self.caption_status.setText("Recorte alterado. Gere as legendas novamente.")

    def validate_video(self) -> bool:
        if not self.video_path:
            QMessageBox.warning(self, APP_NAME, "Selecione um vídeo primeiro.")
            return False
        if not command_exists("ffmpeg"):
            QMessageBox.critical(self, APP_NAME, "FFmpeg não foi encontrado no computador. Consulte o README para instalar.")
            return False
        return True

    def set_busy(self, busy: bool, caption: bool = False) -> None:
        self.caption_button.setDisabled(busy)
        self.export_button.setDisabled(busy)
        self.download_button.setDisabled(busy)
        self.cancel_button.setEnabled(busy)
        self.progress.setRange(0, 0 if busy else 1)
        if not busy: self.progress.setValue(0)

    def cancel_edit(self) -> None:
        """Cancela o processo atual da aba Edição (legenda, download ou export)."""
        # Impede novas tentativas do download e evita a mensagem de erro final.
        self._dl_attempt = getattr(self, "_dl_max_attempts", 0)
        self._cancelled = True
        proc = self.process
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            try:
                proc.finished.disconnect()
            except (TypeError, RuntimeError):
                pass
            proc.kill()
            proc.waitForFinished(2000)
        self.set_busy(False)
        self.log.appendPlainText("\n⛔ Operação cancelada.")

    def run_process(self, command: list[str], success_message: str, caption: bool = False) -> None:
        self.set_busy(True, caption)
        self.log.appendPlainText("\n> " + " ".join(command[:3]) + " …")
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.append_process_output)
        self.process.finished.connect(lambda code, status: self.process_finished(code, status, success_message, caption))
        self.process.start(command[0], command[1:])

    def run_download(self, command: list[str], success_message: str, max_attempts: int = 6) -> None:
        """Executa o yt-dlp reextraindo URLs a cada falha.

        As URLs de mídia do YouTube têm binding de sessão e às vezes já nascem
        "mortas" (HTTP 403). Como o --retries do yt-dlp bate na mesma URL, não
        resolve; reexecutar o comando inteiro reextrai URLs frescas. O .part é
        retomado entre as tentativas, então nada é rebaixado à toa.

        Tenta até 6 vezes com delay de 2s entre elas. A partir da tentativa 3,
        muda a estratégia de player (default+web_safari → web_embedded).
        """
        self._dl_command = command
        self._dl_success_msg = success_message
        self._dl_attempt = 0
        self._dl_max_attempts = max_attempts
        self._start_download_attempt()

    def _start_download_attempt(self) -> None:
        self._dl_attempt += 1
        if self._dl_attempt == 1:
            self.log.appendPlainText("\n> yt-dlp …")
            self._execute_download_command()
        else:
            self.log.appendPlainText(
                f"\n↻ Tentativa {self._dl_attempt}/{self._dl_max_attempts} "
                "(aguardando 2s + reextraindo URLs)…"
            )
            # Aguarda 2s sem bloquear a thread principal (usa timer assíncrono)
            timer = QTimer(self)
            timer.setSingleShot(True)
            timer.timeout.connect(self._execute_download_command)
            timer.start(2000)
            self._dl_retry_timer = timer

    def _execute_download_command(self) -> None:
        """Executa o comando de download (chamado imediatamente ou após delay)."""
        self.set_busy(True)
        # A partir da tentativa 3, muda a estratégia de player (tenta web_embedded)
        command = self._dl_command.copy()
        if self._dl_attempt >= 3:
            try:
                idx = command.index("--extractor-args")
                # Troca "mweb,tv_simply" por "web_embedded" (clientes diferentes)
                command[idx + 1] = "youtube:player_client=web_embedded"
                self.log.appendPlainText("   → Tentando com outro player (web_embedded)…")
            except (ValueError, IndexError):
                pass
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.append_process_output)
        self.process.finished.connect(self._download_finished)
        self.process.start(command[0], command[1:])

    def _download_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        if code == 0 and status == QProcess.ExitStatus.NormalExit:
            self.set_busy(False)
            if getattr(self, "_dl_wants_subs", False):
                self._clean_downloaded_subs()
            QMessageBox.information(self, APP_NAME, self._dl_success_msg)
            return
        if self._dl_attempt < self._dl_max_attempts:
            self.log.appendPlainText("   ⚠️ Falhou (URL expirada/403). Reextraindo e tentando de novo…")
            self._start_download_attempt()
            return
        self.set_busy(False)
        QMessageBox.critical(
            self, APP_NAME,
            "O download falhou após várias tentativas. O YouTube pode estar "
            "bloqueando este vídeo agora — tente novamente em alguns minutos.")

    def _clean_downloaded_subs(self) -> None:
        """Tira as repetições da legenda rolante do YouTube nos .srt baixados.

        O yt-dlp grava a legenda automática crua, em que cada frase reaparece em
        vários blocos (e vêm micro-blocos de 10ms). Aberto num player/editor,
        parece "bugado". Limpamos só os .srt recém-gravados por este download
        (mtime dos últimos 10 min), preservando o fraseado — nada é picado.
        """
        try:
            recentes = [p for p in DOWNLOAD_DIR.glob("*.srt")
                        if time.time() - p.stat().st_mtime < 600]
        except OSError:
            return
        total = 0
        for srt in recentes:
            try:
                total += clean_srt_file(srt)
            except OSError:
                continue
        if total:
            self.log.appendPlainText(
                f"🧹 Legenda limpa: {total} bloco(s) repetido(s) removido(s) "
                "da legenda automática do YouTube.")

    def append_process_output(self) -> None:
        data = bytes(self.process.readAllStandardOutput())
        try:
            output = data.decode("utf-8")
        except UnicodeDecodeError:
            output = data.decode("cp1252", errors="replace")
        if output.strip():
            self.log.appendPlainText(output.strip())

    def process_finished(self, code: int, status: QProcess.ExitStatus, message: str, caption: bool) -> None:
        self.set_busy(False)
        if code == 0 and status == QProcess.ExitStatus.NormalExit:
            if caption:
                srt_files = sorted(self.work_dir.glob("*.srt"), key=lambda p: p.stat().st_mtime)
                if srt_files and srt_has_content(srt_files[-1]):
                    self.caption_path = srt_files[-1]

                    def ready(note: str = "") -> None:
                        # Encurtar depois da revisão: a IA trabalha melhor com as
                        # frases inteiras do Whisper do que com blocos picados.
                        shorten_srt_captions(self.caption_path)
                        self.caption_status.setText(
                            f"Legendas prontas: {self.caption_path.name}{note}")

                    skip = self.ai_review_skip_reason(self.caption_path)
                    if skip:
                        self.log.appendPlainText("⚠️ " + skip)
                    if self.ai_review_enabled() and not skip:
                        self.caption_status.setText("Revisando as legendas com IA…")

                        def on_reviewed(ok, msg, titulo, subtitulo):
                            self.log.appendPlainText(("✅ " if ok else "⚠️ ") + msg)
                            if ok:
                                self._apply_ai_title(titulo, subtitulo)
                            ready(" (revisada pela IA)" if ok else "")

                        started = self._start_ai_review(
                            self.caption_path, self.text_input.text().strip(),
                            on_reviewed, with_title=True,
                        )
                        if not started:
                            ready()
                    else:
                        ready()
                else:
                    self.caption_path = None
                    self.caption_status.setText("Nenhuma fala detectada no trecho; sem legendas.")
            else:
                # Exportação de vídeo: salva a transcrição (.srt pt-br) ao lado.
                srt_src = getattr(self, "_srt_to_export", None)
                srt_dst = getattr(self, "_srt_export_target", None)
                # Se não há legenda preparada, tenta encontrar do vídeo original
                if not srt_src and self.video_path:
                    existing = find_video_subtitle(self.video_path)
                    if existing and srt_has_content(existing):
                        srt_src = existing
                # Copia a legenda ao lado do vídeo exportado
                if srt_src and srt_dst:
                    try:
                        shutil.copyfile(srt_src, srt_dst)
                        message += f"\n\nTranscrição salva em:\n{srt_dst}"
                    except OSError:
                        pass
                    # Legenda de Instagram (.txt ao lado do vídeo). A IA responde
                    # em segundo plano; o arquivo aparece alguns segundos depois.
                    self.deliver_reels_caption(srt_dst, srt_dst, self.log.appendPlainText)
                musica_track = getattr(self, "_music_track", None)
                log_video(
                    self.text_input.text().strip(),
                    trilhas.label_for(musica_track) if musica_track else "",
                    self.subtitle_input.text().strip(),
                )
                post = getattr(self, "_post_frame", None)
                if post:
                    message += f"\n\nFrame de post (também é a capa do vídeo):\n{post}"
                self._post_frame = None
                self._srt_to_export = None
            QMessageBox.information(self, APP_NAME, message)
        else:
            QMessageBox.critical(self, APP_NAME, "O processamento falhou. Veja o log abaixo e confirme se FFmpeg/Whisper estão instalados.")

    def generate_captions(self) -> None:
        if not self.validate_video(): return
        start, length = self.cut_values()
        # 1) Reaproveita a legenda do vídeo (ex.: baixada pelo yt-dlp), sem Whisper.
        #    Mas não quando ela é auto-gerada (YouTube): essas vêm com ">>",
        #    repetições e micro-blocos que nem a limpeza nem a IA deixam boas.
        #    Aí vale mais transcrever de novo, se o Whisper estiver disponível.
        existing = find_video_subtitle(self.video_path)
        if existing:
            source = parse_srt_segments(existing)
            auto = looks_like_auto_caption(source)
            if auto and whisper_path():
                self.log.appendPlainText(
                    f"ℹ️ {existing.name} parece uma legenda automática (marcadores "
                    ">>, repetições). Transcrevendo com Whisper para sair limpa.")
            else:
                out = self.work_dir / "trecho_para_legendar.srt"
                if segments_to_srt(source, start, start + length, out):
                    removidos = shorten_srt_captions(out)
                    self.caption_path = out
                    if removidos:
                        self.log.appendPlainText(
                            f"🧹 Limpeza da legenda: {removidos} bloco(s) "
                            "repetido(s) ou curto(s) removido(s).")
                    aviso = ("\n\n⚠️ Era uma legenda automática (>>, repetições). "
                             "Limpei o que deu, mas para ficar impecável instale o "
                             "Whisper — aí ela é transcrita de novo.") if auto else ""
                    self.caption_status.setText(f"Legenda do vídeo reaproveitada: {existing.name}")
                    QMessageBox.information(self, APP_NAME, f"Legenda do vídeo reaproveitada ({existing.name}).{aviso}\nSerá aplicada na exportação.")
                    return
                self.log.appendPlainText(f"ℹ️ {existing.name} não cobre este trecho; usando Whisper.")
        # 2) Sem legenda pronta: transcreve com Whisper.
        whisper = whisper_path()
        if not whisper:
            QMessageBox.warning(self, APP_NAME, "Whisper não foi encontrado. Instale as dependências do README para usar legendas offline.")
            return
        audio_path = self.work_dir / "trecho_para_legendar.wav"
        try:
            subprocess.run(["ffmpeg", "-y", "-ss", str(start), "-t", str(length), "-i", str(self.video_path), "-vn", "-ac", "1", "-ar", "16000", str(audio_path)], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            QMessageBox.critical(self, APP_NAME, "Não foi possível extrair o áudio do trecho.")
            return
        command = self.whisper_command(whisper, audio_path, self.work_dir)
        self.run_process(command, "Legendas geradas. Elas serão aplicadas na exportação.", caption=True)

    # ──────────────────────────────────────────────────────────────
    #  Revisão da legenda com IA (DeepSeek) — vale para os 3 tipos de corte
    # ──────────────────────────────────────────────────────────────

    def _build_ai_box(self) -> QGroupBox:
        """Caixa de configuração da revisão com IA, na aba Edição.

        A configuração é única e vale para os três fluxos (edição, CSV e live).
        """
        config = ai_srt.load_config()
        box = QGroupBox("4. Revisão da legenda com IA (DeepSeek)")
        layout = QVBoxLayout(box)

        self.ai_enabled = QCheckBox("Revisar as legendas com IA antes de queimar no vídeo")
        self.ai_enabled.setChecked(bool(config.get("ai_enabled")))
        self.ai_enabled.toggled.connect(self._save_ai_config)
        layout.addWidget(self.ai_enabled)

        # Uma requisição a mais por corte, depois que o vídeo já está pronto.
        self.ai_reels = QCheckBox(
            "Gerar a legenda do Instagram com IA e salvar o .txt ao lado do vídeo")
        self.ai_reels.setChecked(bool(config.get("ai_reels", True)))
        self.ai_reels.toggled.connect(self._save_ai_config)
        layout.addWidget(self.ai_reels)

        form = QFormLayout()
        self.ai_key = QLineEdit(config.get("api_key", ""))
        self.ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_key.setPlaceholderText("sk-… (ou defina DEEPSEEK_API_KEY no sistema)")
        self.ai_key.editingFinished.connect(self._save_ai_config)
        form.addRow("Chave da API", self.ai_key)

        self.ai_model = QComboBox()
        self.ai_model.setEditable(True)
        self.ai_model.addItem(config.get("model") or ai_srt.DEFAULT_MODEL)
        self.ai_model.currentTextChanged.connect(lambda _: self._save_ai_config())
        model_row = QHBoxLayout()
        model_row.addWidget(self.ai_model, 1)
        fetch = QPushButton("Buscar")
        fetch.setToolTip("Lista os modelos que a sua chave pode usar.")
        fetch.clicked.connect(self._fetch_ai_models)
        model_row.addWidget(fetch)
        model_widget = QWidget(); model_widget.setLayout(model_row)
        form.addRow("Modelo", model_widget)
        layout.addLayout(form)

        note = QLabel("O texto das legendas é enviado para a API do DeepSeek. Os tempos "
                      "nunca saem daqui — só as falas vão, e o SRT é remontado "
                      "com os tempos originais. Na Edição, a mesma revisão devolve "
                      "também o título e o subtítulo (seção 5). Use 'deepseek-chat' "
                      "(barato); o log mostra os tokens de cada revisão.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.ai_button = QPushButton("Revisar a legenda e sugerir título com IA")
        self.ai_button.clicked.connect(self._review_current_caption)
        layout.addWidget(self.ai_button)
        return box

    def _save_ai_config(self) -> None:
        ai_srt.save_config({
            "ai_enabled": self.ai_enabled.isChecked(),
            "ai_reels": self.ai_reels.isChecked(),
            "api_key": self.ai_key.text().strip(),
            "model": self.ai_model.currentText().strip(),
        })

    def _ai_key(self) -> str:
        """Chave digitada na interface; se vazia, a do ambiente."""
        return self.ai_key.text().strip() or ai_srt.api_key_from_env()

    def ai_review_enabled(self) -> bool:
        """A revisão automática está ligada e configurada?"""
        return bool(self.ai_enabled.isChecked() and self._ai_key())

    def ai_review_skip_reason(self, path: Path) -> str:
        """Por que a revisão automática não vai rodar? '' se for rodar.

        Evita o pior caso: o usuário marca a revisão, mas ela é pulada em
        silêncio (sem chave, ou sem legenda), e ele acha que a IA rodou.
        """
        if not self.ai_enabled.isChecked():
            return ""                       # o usuário não pediu revisão
        if not self._ai_key():
            return ("revisão com IA ligada, mas sem chave da API do DeepSeek — "
                    "informe a chave na seção 5 (ou desmarque a revisão).")
        if not srt_has_content(path):
            return "revisão com IA ligada, mas não há legenda para revisar."
        return ""

    def _fetch_ai_models(self) -> None:
        try:
            models = ai_srt.list_models(self._ai_key())
        except ai_srt.AiError as error:
            QMessageBox.warning(self, APP_NAME, f"Não deu para listar os modelos:\n\n{error}")
            return
        if not models:
            QMessageBox.information(self, APP_NAME, "A API não devolveu nenhum modelo.")
            return
        current = self.ai_model.currentText().strip()
        self.ai_model.clear()
        self.ai_model.addItems(models)
        self.ai_model.setCurrentText(current if current in models else models[0])

    def _start_ai_review(self, path: Path, context: str, on_done,
                         with_title: bool = False) -> bool:
        """Dispara a revisão em segundo plano. False se não deu para começar.

        Com `with_title` (só na Edição), a mesma requisição também devolve título
        e subtítulo, entregues no callback `on_done(ok, mensagem, titulo, subtitulo)`.
        """
        if not srt_has_content(path):
            return False
        key = self._ai_key()
        if not key:
            return False
        self._ai_flagged = []              # achados da revisão anterior não valem mais
        # Só faz sentido pedir trilha quando há título a pedir também (Edição):
        # é lá que existe o botão de ouvir e a aprovação.
        climas = [rotulo for rotulo, _ in trilhas.list_tracks(self.music_dir())] if with_title else []
        worker = SrtReviewWorker(
            path, key, self.ai_model.currentText().strip(), context, with_title,
            climas, self)
        worker.flagged.connect(self._on_ai_flagged)
        worker.music.connect(self._on_ai_music)
        worker.done.connect(on_done)
        worker.finished.connect(worker.deleteLater)
        self._ai_worker = worker           # segura a referência enquanto roda
        worker.start()
        return True

    def _apply_ai_title(self, titulo: str, subtitulo: str) -> None:
        """Preenche os campos Título/Subtítulo (seção 5) com o que a IA devolveu."""
        if titulo:
            self.text_input.setText(titulo)
        if subtitulo:
            self.subtitle_input.setText(subtitulo)

    def _review_current_caption(self) -> None:
        """Botão da aba Edição: revisa a legenda e sugere título, sob demanda."""
        if not srt_has_content(self.caption_path):
            QMessageBox.warning(self, APP_NAME, "Gere as legendas do recorte antes de revisar.")
            return
        if not self._ai_key():
            QMessageBox.warning(self, APP_NAME,
                                "Informe a chave da API do DeepSeek (ou defina DEEPSEEK_API_KEY).")
            return
        self.ai_button.setEnabled(False)
        self.ai_button.setText("Revisando com IA…")

        def finished(ok: bool, message: str, titulo: str, subtitulo: str) -> None:
            self.ai_button.setEnabled(True)
            self.ai_button.setText("Revisar a legenda e sugerir título com IA")
            self.log.appendPlainText(("✅ " if ok else "⚠️ ") + message)
            self.caption_status.setText(message)
            if ok:
                self._apply_ai_title(titulo, subtitulo)
                extra = f"\n\nTítulo: {titulo or '(vazio)'}\nSubtítulo: {subtitulo or '(vazio)'}"
                QMessageBox.information(self, APP_NAME, message + extra)
            else:
                QMessageBox.warning(self, APP_NAME, message)

        self._start_ai_review(
            self.caption_path, self.text_input.text().strip(), finished, with_title=True)

    # ──────────────────────────────────────────────────────────────
    #  Censura de palavrões / palavras bloqueáveis — vale para os 3 fluxos
    # ──────────────────────────────────────────────────────────────

    def _build_censor_box(self) -> QGroupBox:
        """Lista de palavras a censurar, na aba Edição.

        Como a caixa da IA, a configuração é única e vale para os três fluxos
        (edição, CSV e live).
        """
        config = ai_srt.load_config()
        box = QGroupBox("6. Palavrões e palavras bloqueadas")
        layout = QVBoxLayout(box)

        self.censor_enabled = QCheckBox("Censurar as palavras da lista abaixo")
        self.censor_enabled.setChecked(bool(config.get("censor_enabled")))
        layout.addWidget(self.censor_enabled)

        self.censor_mute = QCheckBox("Silenciar no áudio (só o instante da palavra)")
        self.censor_mute.setChecked(bool(config.get("censor_mute", True)))
        self.censor_caption = QCheckBox("Disfarçar na legenda (matar → m4tar)")
        self.censor_caption.setChecked(bool(config.get("censor_caption", True)))
        layout.addWidget(self.censor_mute)
        layout.addWidget(self.censor_caption)

        saved = config.get("censor_words")
        self.censor_words = QPlainTextEdit(
            saved if isinstance(saved, str) else ", ".join(censor.DEFAULT_WORDS))
        self.censor_words.setMaximumHeight(90)
        self.censor_words.setPlaceholderText("uma por linha, ou separadas por vírgula")
        layout.addWidget(self.censor_words)

        hint = QLabel(
            "Uma por linha ou separadas por vírgula. Termine com * para pegar as "
            "variações (put* → puta, putas, putaria). Só funciona onde há legenda: "
            "é ela que diz em que segundo a palavra foi dita.")
        hint.setObjectName("muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        restore = QPushButton("Restaurar lista padrão")
        restore.clicked.connect(
            lambda: self.censor_words.setPlainText(", ".join(censor.DEFAULT_WORDS)))
        layout.addWidget(restore)

        # Salva sozinho, mas sem gravar o arquivo a cada tecla digitada.
        self._censor_save_timer = QTimer(self)
        self._censor_save_timer.setSingleShot(True)
        self._censor_save_timer.setInterval(600)
        self._censor_save_timer.timeout.connect(self._save_censor_config)
        self.censor_words.textChanged.connect(self._censor_save_timer.start)
        for check in (self.censor_enabled, self.censor_mute, self.censor_caption):
            check.toggled.connect(self._save_censor_config)
        return box

    def _save_censor_config(self) -> None:
        ai_srt.save_config({
            "censor_enabled": self.censor_enabled.isChecked(),
            "censor_mute": self.censor_mute.isChecked(),
            "censor_caption": self.censor_caption.isChecked(),
            "censor_words": self.censor_words.toPlainText(),
        })

    def deliver_reels_caption(self, srt_path: Path | None, dest: Path, log) -> None:
        """Entrega o .txt de legenda do Reels ao lado do vídeo.

        Com chave da API e a opção ligada, o prompt vai para o DeepSeek e o
        arquivo sai com a legenda pronta para postar. Sem isso, sai com o
        próprio prompt — o comportamento antigo, para colar numa IA à mão.
        """
        prompt = build_reels_prompt(srt_path)
        if not prompt:
            return
        if not (self.ai_reels.isChecked() and self._ai_key()):
            caminho = write_reels_text(dest, prompt)
            if caminho:
                log(f"📝 Prompt de legenda salvo em {caminho.name} "
                    "(ligue a legenda com IA na seção 4 para vir pronta).")
            return
        self._reels_queue.append((prompt, dest, log))
        self._next_reels_caption()

    def _next_reels_caption(self) -> None:
        """Roda a fila de legendas do Reels, uma por vez.

        Um lote de CSV enfileira uma por corte; dispará-las juntas seria um
        punhado de requisições simultâneas à mesma API, sem ganho nenhum.
        """
        if self._reels_worker is not None or not self._reels_queue:
            return
        prompt, dest, log = self._reels_queue.pop(0)
        log("🤖 Gerando a legenda do Reels com IA…")
        worker = ReelsCaptionWorker(
            prompt, dest, self._ai_key(), self.ai_model.currentText().strip(), self)

        def finished(ok: bool, message: str, caminho) -> None:
            log(("✅ " if ok else "⚠️ ") + message)
            if caminho:
                log(f"   → {caminho.name}")
            self._reels_worker = None
            self._next_reels_caption()

        worker.done.connect(finished)
        worker.finished.connect(worker.deleteLater)
        self._reels_worker = worker
        worker.start()

    def _build_music_box(self) -> QGroupBox:
        """Trilha de fundo: a IA escolhe, você ouve e aprova.

        A pasta é o catálogo — o nome do arquivo vira o rótulo do clima e é
        isso que a IA recebe para escolher. Acrescentar música é só soltar o
        mp3 na pasta.
        """
        config = ai_srt.load_config()
        box = QGroupBox("7. Trilha de fundo")
        layout = QVBoxLayout(box)

        self.music_enabled = QCheckBox("Misturar a trilha escolhida na exportação")
        self.music_enabled.setChecked(bool(config.get("music_enabled")))
        self.music_enabled.toggled.connect(self._save_music_config)
        layout.addWidget(self.music_enabled)

        pasta = QHBoxLayout()
        self.music_dir_input = QLineEdit(
            str(config.get("music_dir") or trilhas.DEFAULT_MUSIC_DIR))
        self.music_dir_input.editingFinished.connect(self._save_music_config)
        escolher = QPushButton("Pasta…")
        escolher.clicked.connect(self._pick_music_dir)
        pasta.addWidget(QLabel("Pasta"))
        pasta.addWidget(self.music_dir_input, 1)
        pasta.addWidget(escolher)
        layout.addLayout(pasta)

        self.music_status = QLabel("A IA escolhe a trilha ao revisar a legenda.")
        self.music_status.setWordWrap(True)
        layout.addWidget(self.music_status)

        linha = QHBoxLayout()
        self.music_choice = QComboBox()
        self.music_choice.currentIndexChanged.connect(self._on_music_picked)
        self.music_play_btn = QPushButton("▶ Ouvir")
        self.music_play_btn.clicked.connect(self._toggle_music_preview)
        self.music_play_btn.setEnabled(False)
        linha.addWidget(self.music_choice, 1)
        linha.addWidget(self.music_play_btn)
        layout.addLayout(linha)

        self._reload_music_choices()
        return box

    def _save_music_config(self) -> None:
        ai_srt.save_config({
            "music_enabled": self.music_enabled.isChecked(),
            "music_dir": self.music_dir_input.text().strip(),
        })

    def music_dir(self) -> Path:
        """Pasta das trilhas, do campo da interface."""
        texto = self.music_dir_input.text().strip() if hasattr(self, "music_dir_input") else ""
        return Path(texto) if texto else trilhas.DEFAULT_MUSIC_DIR

    def _pick_music_dir(self) -> None:
        pasta = QFileDialog.getExistingDirectory(
            self, "Pasta das trilhas", str(self.music_dir()))
        if pasta:
            self.music_dir_input.setText(pasta)
            self._save_music_config()
            self._reload_music_choices()

    def _reload_music_choices(self) -> None:
        """Recarrega a lista de trilhas a partir da pasta."""
        faixas = trilhas.list_tracks(self.music_dir())
        self.music_choice.blockSignals(True)
        self.music_choice.clear()
        self.music_choice.addItem("(sem trilha)", None)
        for rotulo, caminho in faixas:
            self.music_choice.addItem(rotulo, str(caminho))
        self.music_choice.blockSignals(False)
        if not faixas:
            self.music_status.setText(
                f"Nenhuma trilha em {self.music_dir()} — solte arquivos .mp3 lá.")

    def _on_music_picked(self) -> None:
        """Troca manual no combo: vale sobre o que a IA escolheu."""
        dado = self.music_choice.currentData()
        self._stop_music_preview()
        self._music_track = Path(dado) if dado else None
        self.music_play_btn.setEnabled(self._music_track is not None)

    def _on_ai_music(self, clima: str) -> None:
        """Aplica a trilha que a IA escolheu, deixando-a pronta para ouvir."""
        if not clima:
            self.music_status.setText("A IA não escolheu trilha para este corte.")
            return
        caminho = trilhas.find_track(clima, self.music_dir())
        if not caminho:
            self.music_status.setText(
                f"A IA pediu '{clima}', mas não há essa trilha na pasta.")
            return
        indice = self.music_choice.findData(str(caminho))
        if indice >= 0:
            self.music_choice.setCurrentIndex(indice)     # dispara _on_music_picked
        self.music_status.setText(f"🎵 A IA escolheu: {clima} ({caminho.name})")
        self.log.appendPlainText(f"🎵 Trilha escolhida pela IA: {clima} → {caminho.name}")

    def _toggle_music_preview(self) -> None:
        """Toca ou para a trilha selecionada, só para você aprovar."""
        if self._music_player.is_playing():
            self._stop_music_preview()
            return
        if not self._music_track or not self._music_track.exists():
            return
        self._music_player.set_media(self.vlc_instance.media_new(str(self._music_track)))
        self._music_player.play()
        self.music_play_btn.setText("■ Parar")

    def _stop_music_preview(self) -> None:
        self._music_player.stop()
        self.music_play_btn.setText("▶ Ouvir")

    def _on_ai_flagged(self, sensiveis: list) -> None:
        """Mostra onde a IA viu conteúdo sensível e guarda para a censura.

        É o que a lista de palavras não alcança: o reconhecimento de fala erra
        justamente nessas palavras (escreveu "escuprida" onde se disse
        "estupro"), e só quem lê a frase inteira percebe.
        """
        self._ai_flagged = sensiveis or []
        if not self._ai_flagged:
            return
        self.log.appendPlainText(f"🚩 A IA apontou {len(self._ai_flagged)} trecho(s) sensível(is):")
        for item in self._ai_flagged:
            motivo = f" — {item['motivo']}" if item.get("motivo") else ""
            self.log.appendPlainText(
                f"    {as_time(item.get('inicio', 0.0))}  “{item['trecho']}”{motivo}")

    def censor_word_list(self) -> list[str]:
        """Palavras a censurar; vazio quando a censura está desligada.

        Junta a lista escrita à mão com o que a IA apontou na revisão — assim a
        censura pega também o que o usuário não previu.
        """
        if not self.censor_enabled.isChecked():
            return []
        words = censor.parse_words(self.censor_words.toPlainText())
        for item in getattr(self, "_ai_flagged", []):
            trecho = str(item.get("trecho", "")).strip()
            if trecho and trecho.lower() not in (w.lower() for w in words):
                words.append(trecho)
        return words

    def censor_wants_mute(self) -> bool:
        """O Whisper precisa marcar palavra a palavra nesta exportação?"""
        return bool(self.censor_word_list()) and self.censor_mute.isChecked()

    def whisper_command(self, binary: str, wav: Path, out_dir: Path) -> list[str]:
        """Comando do Whisper para o trecho, já ajustado à censura.

        Marcar palavra por palavra (`--word_timestamps`) deixa a transcrição
        mais lenta, então só entra quando a censura vai silenciar o áudio — é o
        único caso em que precisamos do segundo exato de cada palavra. O formato
        'all' é o que também grava o .json, onde ficam esses tempos.
        """
        command = [binary, str(wav), "--model", self.whisper_model.currentData(),
                   "--language", "Portuguese",
                   "--task", "transcribe", "--output_dir", str(out_dir)]
        if self.censor_wants_mute():
            return command + ["--word_timestamps", "True", "--output_format", "all"]
        return command + ["--output_format", "srt"]

    def _csv_music_track(self, moment: dict) -> Path | None:
        """Trilha do corte de CSV: a que a IA escolheu, se a mixagem estiver ligada.

        Reaproveita a config da aba Edição (checkbox "Misturar trilha" e a pasta
        de trilhas). O rótulo vem do campo `musica` do momento (escolhido pela IA
        ao gerar os cortes, ou pela coluna 'musica' de um CSV feito à mão).
        """
        if not (hasattr(self, "music_enabled") and self.music_enabled.isChecked()):
            return None
        clima = str(moment.get("musica") or "").strip()
        if not clima:
            return None
        return trilhas.find_track(clima, self.music_dir())

    def music_export_track(self) -> Path | None:
        """Trilha a misturar na exportação, ou None se a opção estiver desligada."""
        if not (hasattr(self, "music_enabled") and self.music_enabled.isChecked()):
            return None
        faixa = self._music_track
        return faixa if faixa and faixa.exists() else None

    def _audio_chain(self, video_chain: str, speech: str, censor_af: str,
                     music_in: int | None, duration: float) -> tuple[str, str]:
        """Junta censura e trilha numa cadeia só, e diz o que mapear.

        Sem trilha, a censura continua indo pelo caminho simples (`-af`), que
        já estava testado. Com trilha, tudo tem de viver dentro do
        filter_complex, porque a mixagem precisa da fala como sidechain.
        Devolve (cadeia de filtros, rótulo a mapear no áudio).
        """
        if music_in is None:
            return video_chain, "0:a?"
        fala = speech
        chain = video_chain
        if censor_af:
            chain += f";[{speech}]{censor_af}[sp]"
            fala = "sp"
        chain += ";" + trilhas.mix_chain(fala, music_in, "aout", duration=duration)
        return chain, "[aout]"

    @staticmethod
    def _audio_censor_args(audio_filter: str) -> list[str]:
        """Argumentos do FFmpeg que silenciam as palavras, ou nada a acrescentar."""
        return ["-af", audio_filter] if audio_filter else []

    def apply_censorship(self, srt_path: Path | None, log) -> str:
        """Censura a legenda e devolve o filtro de áudio (-af) do trecho.

        Roda na hora de exportar, não quando a legenda é gerada: assim vale
        também para quem ligou a censura depois de transcrever, e a lista em
        vigor é sempre a que está na tela.

        A ordem importa: os tempos saem primeiro, porque logo depois o texto do
        SRT muda ('porra' vira 'p0rra') e deixaria de casar com a lista.
        Devolve "" quando não há nada a silenciar.
        """
        words = self.censor_word_list()
        if not words:
            return ""
        if not srt_has_content(srt_path):
            log("⚠️ Censura ligada, mas não há legenda neste corte — "
                "sem transcrição não dá para saber onde estão as palavras.")
            return ""
        spans = censor.mute_spans(srt_path, words) if self.censor_mute.isChecked() else []
        trocadas = censor.censor_srt(srt_path, words) if self.censor_caption.isChecked() else 0
        if spans:
            log(f"🔇 {len(spans)} trecho(s) silenciado(s) no áudio.")
        if trocadas:
            log(f"✏️ {trocadas} palavra(s) disfarçada(s) na legenda.")
        if not spans and not trocadas:
            # Quase sempre é a transcrição: o Whisper ouviu outra coisa, e a
            # censura só enxerga o que está escrito na legenda.
            log("ℹ️ Nenhuma palavra da lista apareceu na legenda deste corte. "
                "Confira se o Whisper transcreveu certo — só dá para censurar "
                "o que ele escreveu.")
        return censor.mute_filter(spans)

    def _render_thumbnail_async(self, command: list[str], thumb_path: Path, callback) -> None:
        """Gera o frame de post (thumbnail) sem travar a janela.

        Antes rodava via `subprocess.run` direto na thread da interface: um
        corte problemático (ou um arquivo de trilha ainda baixando do
        OneDrive) travava a janela inteira até o timeout de 60s — em lote, uma
        vez por corte, sem nada aparecer no log enquanto isso. Aqui o FFmpeg
        roda num QProcess, como o resto do app, e `callback(Path | None)` é
        chamado ao terminar (ou expirar), com o frame gerado ou None.
        """
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        timer = QTimer(self)
        timer.setSingleShot(True)
        state = {"done": False}

        def finish(result: Path | None) -> None:
            if state["done"]:
                return
            state["done"] = True
            timer.stop()
            self._thumbnail_procs.discard(proc)
            callback(result)

        def on_finished(code: int, status: QProcess.ExitStatus) -> None:
            ok = code == 0 and status == QProcess.ExitStatus.NormalExit and thumb_path.exists()
            finish(thumb_path if ok else None)

        def on_timeout() -> None:
            if proc.state() != QProcess.ProcessState.NotRunning:
                proc.kill()
                proc.waitForFinished(2000)
            finish(None)

        proc.finished.connect(on_finished)
        timer.timeout.connect(on_timeout)
        self._thumbnail_procs.add(proc)
        timer.start(60000)
        proc.start(command[0], command[1:])

    def download_video(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, APP_NAME, "Cole a URL de um vídeo para baixar.")
            return
        if "/live/" in url:
            QMessageBox.warning(
                self, APP_NAME,
                "Essa URL é de uma transmissão ao vivo.\n\n"
                "O download comum grava a live em tempo real e nunca termina "
                "enquanto ela estiver no ar. Use a aba 🔴 Live para gravar e cortar lives.")
            return
        downloader = yt_dlp_path()
        if not downloader:
            QMessageBox.critical(self, APP_NAME, "yt-dlp não foi encontrado. Consulte o README para instalá-lo.")
            return
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        # --match-filter "!is_live": recusa lives que não usem /live/ na URL —
        # nelas o download roda em tempo real (1x) e parece "travado".
        # --extractor-args player_client: em 25/09/2026 o "default" (que cai no
        #   android_vr) passou a dar HTTP 403 no vídeo e o web_safari não achava
        #   formato; "mweb,tv_simply" baixou normal. Os dois precisam de um
        #   runtime de JavaScript (deno) no PATH para o desafio "n" do YouTube.
        # --*-retries: reenfileira automaticamente falhas transitórias (403/429).
        command = [str(downloader), "--no-playlist", "--match-filter", "!is_live",
                   "--extractor-args", "youtube:player_client=mweb,tv_simply",
                   "--retries", "10", "--fragment-retries", "10", "--extractor-retries", "3",
                   "-N", str(self.fragment_count.value()), "-f", "bv*+ba/b",
                   "--merge-output-format", "mp4", "-P", str(DOWNLOAD_DIR),
                   "-o", "%(title).200B.%(ext)s"]

        # Baixa só a seção desejada (não tudo depois corta)
        if self.download_time_mode.currentData() == "custom":
            start_sec = self.download_start_time.value()
            end_sec = self.download_end_time.value()
            if end_sec <= start_sec:
                QMessageBox.warning(self, APP_NAME, "Em 'Personalizado', o Fim precisa ser maior que o Início.")
                return
            start_time = as_time(start_sec)
            end_time = as_time(end_sec)
            command += ["--download-sections", f"*{start_time}-{end_time}"]
            # --download-sections só corta de verdade com o downloader nativo;
            # em HLS (comum em vídeos que já foram live) o yt-dlp precisa usar
            # o ffmpeg como downloader, e o ffmpeg por padrão não imprime nada
            # até terminar — parece travado numa live longa. "-stats" força ele
            # a mostrar time=/size=/speed= periodicamente no log.
            command += ["--downloader-args", "ffmpeg:-stats"]
            self.log.appendPlainText(
                "\nBaixando só o trecho selecionado. Se o vídeo original for longo "
                "(ex.: gravação de uma live), o ffmpeg pode demorar sem mostrar % "
                "— acompanhe pelo tamanho crescente do arquivo em "
                f"{DOWNLOAD_DIR}."
            )

        self._dl_wants_subs = self.download_subs.isChecked()
        if self.download_subs.isChecked():
            # Legendas do próprio vídeo (oficiais + automáticas do YouTube) em
            # pt-br, convertidas para .srt ao lado do vídeo. A lista de idiomas
            # é enxuta de propósito: incluir "pt.*" puxa dezenas de traduções
            # automáticas e dispara HTTP 429 (Too Many Requests).
            # --sub-format ttml: o formato nativo do YouTube é WebVTT, e a
            # conversão vtt->srt do yt-dlp é um bug conhecido para legenda
            # automática (deixa tags "<c>", "position:63%" etc. dentro do
            # texto e blocos repetidos da legenda rolante). Pedindo TTML —
            # que não tem essa sintaxe de cue inline — o srt final sai bem
            # mais limpo; "/best" é o fallback se o YouTube não oferecer TTML
            # para aquele idioma.
            command += ["--write-subs", "--write-auto-subs",
                        "--sub-langs", "pt-BR,pt,pt-orig",
                        "--sub-format", "ttml/best",
                        "--convert-subs", "srt",
                        "--no-abort-on-error"]
        command.append(url)
        self.run_download(command, f"Download concluído em:\n{DOWNLOAD_DIR}\n\nAgora selecione o vídeo para editá-lo.")

    def cut_values(self) -> tuple[float, float]:
        start = self.start_input.value()
        return start, max(0.1, self.end_input.value() - start)

    def export_video(self) -> None:
        if not self.validate_video(): return
        mode = self.ratio.currentData()
        if mode == "vertical_image" and not (self.fixed_image_path and self.fixed_image_path.exists()):
            QMessageBox.warning(self, APP_NAME, "Escolha a imagem do topo para o formato 'imagem fixa'.")
            return
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        default = OUTPUT_DIR / f"{self.video_path.stem}_editado.mp4"
        filename, _ = QFileDialog.getSaveFileName(self, "Salvar vídeo final", str(default), "MP4 (*.mp4)")
        if not filename: return
        start, length = self.cut_values()
        # Gera o lower-third (se ativado); ele já traz o logo do canal à esquerda.
        self._cg_path = None
        if self.use_cg.isChecked() and self.cg_icon_path:
            self._cg_path = create_lower_third(
                self.text_input.text(), self.subtitle_input.text(),
                self.work_dir, self.cg_icon_path, colors=self.brand_colors,
            )
        # A imagem fixa é a entrada 1; o lower-third vem depois dela.
        self._image_input_index = 1
        if self._cg_path:
            self._cg_input_index = 1 + (1 if mode == "vertical_image" else 0)
        inputs = ["-i", str(self.video_path)]
        n_inputs = 1
        if mode == "vertical_image":
            inputs += ["-loop", "1", "-i", str(self.fixed_image_path)]; n_inputs += 1
        if self._cg_path:
            inputs += ["-loop", "1", "-i", str(self._cg_path)]; n_inputs += 1

        # 1) Frame de post: mesmo visual (GC), sem legenda escrita.
        thumb = thumbnail_path(Path(filename))
        thumb_command = (
            ["ffmpeg", "-y", "-ss", str(start)] + inputs
            + ["-filter_complex", self.video_filters(captions=False),
               "-map", "[outv]", "-frames:v", "1", "-q:v", "2", str(thumb)]
        )

        def after_thumbnail(post_frame: Path | None) -> None:
            # 2) O frame de post entra como primeiro frame do vídeo exportado.
            self._post_frame = post_frame
            post_input = None
            if post_frame:
                inputs.extend(["-loop", "1", "-i", str(thumb)])
                post_input = n_inputs

            censor_af = self.apply_censorship(self.caption_path, self.log.appendPlainText)
            video_chain = self.video_filters(post_input=post_input)
            # A trilha entra por último; o índice livre é n_inputs, mais um se o
            # frame de post já ocupou essa vaga.
            proxima_entrada = n_inputs + (1 if post_input is not None else 0)
            music_in = proxima_entrada if self.music_export_track() else None
            if music_in is not None:
                inputs.extend(["-i", str(self.music_export_track())])

            command = ["ffmpeg", "-y", "-ss", str(start), "-t", str(length)] + inputs
            chain, amap = self._audio_chain(video_chain, "0:a", censor_af, music_in, length)
            command += ["-filter_complex", chain, "-map", "[outv]", "-map", amap]
            if amap == "0:a?":
                command += self._audio_censor_args(censor_af)
            command += ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", "-shortest", filename]
            # Ao terminar, salva a transcrição (pt-br) ao lado do vídeo, se houver.
            self._srt_to_export = self.caption_path if srt_has_content(self.caption_path) else None
            self._srt_export_target = Path(filename).with_suffix(".srt")
            self.run_process(command, f"Vídeo exportado em:\n{filename}")

        self._render_thumbnail_async(thumb_command, thumb, after_thumbnail)

    def video_filters(self, captions: bool = True, post_input: int | None = None) -> str:
        mode = self.ratio.currentData()
        if mode == "vertical_crop":
            chain, label = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920", "base"
        elif mode == "vertical_blur":
            chain = "[0:v]split=2[bg][fg];[bg]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,boxblur=10:2,scale=1080:1920[blur];[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fit];[blur][fit]overlay=(W-w)/2:(H-h)/2"
            label = "base"
        elif mode == "vertical_image":
            # Imagem fixa (16:9) no topo e o vídeo (16:9) embaixo, empilhados no 9:16.
            img = self._image_input_index
            chain = (f"[{img}:v]scale=1080:608:force_original_aspect_ratio=decrease,"
                     "pad=1080:608:(ow-iw)/2:(oh-ih)/2:black,setsar=1[topimg];"
                     "[0:v]scale=1080:608:force_original_aspect_ratio=decrease,"
                     "pad=1080:608:(ow-iw)/2:(oh-ih)/2:black,setsar=1[botvid];"
                     "[topimg][botvid]vstack=inputs=2[stack];"
                     "[stack]pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black,setsar=1")
            label = "base"
        else:
            chain, label = "[0:v]null", "base"
        chain += f"[{label}]"
        current = label
        use_lt = bool(getattr(self, "_cg_path", None))
        if use_lt:
            # Lower-third "Informativo Nacional", acima da faixa que o YouTube
            # cobre com a própria interface (canal, descrição, botões).
            chain += (f";[{current}][{self._cg_input_index}:v]"
                      f"overlay=x=0:y=main_h-{LT_HEIGHT + LT_BOTTOM_MARGIN}[with_cg]")
            current = "with_cg"
        elif self.text_input.text().strip():
            font = "C\\:/Windows/Fonts/arialbd.ttf"
            text = format_title_for_video(self.text_input.text())
            chain += f";[{current}]drawtext=fontfile='{font}':text='{text}':x=(w-text_w)/2:y=30:fontsize=40:fontcolor=white:borderw=3:bordercolor=black[text]"
            current = "text"
        if captions and srt_has_content(self.caption_path):
            # Com lower-third, sobe a legenda p/ não encostar nele.
            margin_v = LT_CAPTION_MARGIN_V if use_lt else 60
            style = f"FontName=Montserrat,FontSize=18,Bold=-1,PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=0,Alignment=2,MarginV={margin_v}"
            chain += f";[{current}]subtitles=filename='{filter_path(self.caption_path)}':fontsdir='{filter_path(FONT_DIR)}':force_style='{style}'[captioned]"
            current = "captioned"
        chain, current = self._watermark_chain(chain, current)
        chain, current = self._with_post_frame(chain, current, post_input)
        return chain + f";[{current}]format=yuv420p[outv]"

    def _watermark_chain(self, chain: str, current: str, label: str = "wm") -> tuple[str, str]:
        """Carimba a marca d'água no topo, preenchendo a largura.

        Texto e cor vêm da aba Configuração (self.brand_watermark / _wm_color). O
        tamanho da fonte sai de uma medição do texto (métricas do cg_generator),
        para a frase ocupar quase toda a largura do 9:16 sem estourar. Como entra
        antes do frame de post, aparece também na capa do vídeo.
        """
        texto = (self.brand_watermark or "").strip()
        if not texto:
            return chain, current
        from cg_generator import _text_width, _FONT
        size = int((1080 - 60) * 1000 / _text_width(texto, 1000))
        esc = escape_drawtext(texto)
        cor = self.brand_wm_color or DEFAULT_WM_COLOR
        chain += (f";[{current}]drawtext=fontfile='{_FONT}':text='{esc}':"
                  f"x=(w-text_w)/2:y=70:fontsize={size}:fontcolor={cor}@0.8:"
                  f"borderw=2:bordercolor=black@0.4[{label}]")
        return chain, label

    @staticmethod
    def _with_post_frame(chain: str, current: str, post_input: int | None) -> tuple[str, str]:
        """Cobre o frame 0 com o PNG de post, para ele ser a capa do vídeo.

        O PNG é o mesmo quadro sem legenda, então trocar só o frame inicial não
        muda nada visualmente — apenas garante que a capa que as redes pegam do
        primeiro frame saia limpa. Não altera duração nem sincronia do áudio.
        """
        if post_input is None:
            return chain, current
        chain += f";[{current}][{post_input}:v]overlay=0:0:enable='lt(n,1)'[with_post]"
        return chain, "with_post"

    # ──────────────────────────────────────────────────────────────
    #  Identidade visual (logo, nome, marca d'água e cores)
    # ──────────────────────────────────────────────────────────────

    def _load_branding(self) -> None:
        """Carrega a identidade visual do perfil ativo.

        Se nenhum perfil estiver definido, cria o padrão com "info_nacional".
        """
        profile_name = ai_srt.get_current_profile_name()
        if not profile_name:
            profile_name = "info_nacional"
            ai_srt.save_profile(
                profile_name,
                {
                    "brand_logo": str(_LEGACY_LOGO) if _LEGACY_LOGO.exists() else "",
                    "brand_name": DEFAULT_BRAND_NAME,
                    "brand_watermark": DEFAULT_WATERMARK,
                    "brand_wm_color": DEFAULT_WM_COLOR,
                    "brand_color_banner": DEFAULT_COLORS["banner"],
                    "brand_color_green": DEFAULT_COLORS["green"],
                    "brand_color_yellow": DEFAULT_COLORS["yellow"],
                    "brand_color_headline": DEFAULT_COLORS["headline"],
                },
                set_as_current=True
            )
        profile = ai_srt.load_profile(profile_name)
        self._current_profile = profile_name

        logo = str(profile.get("brand_logo") or "").strip()
        if logo and Path(logo).exists():
            self.cg_icon_path: Path | None = Path(logo)
        elif not logo and _LEGACY_LOGO.exists():
            self.cg_icon_path = _LEGACY_LOGO
        else:
            self.cg_icon_path = None
        self.brand_name = str(profile.get("brand_name") or DEFAULT_BRAND_NAME).strip()
        self.brand_watermark = str(profile.get("brand_watermark", DEFAULT_WATERMARK))
        self.brand_wm_color = str(profile.get("brand_wm_color") or DEFAULT_WM_COLOR)
        # Cores da tarja: começa dos padrões e sobrescreve com o que estiver salvo.
        self.brand_colors = dict(DEFAULT_COLORS)
        for key in self.brand_colors:
            saved = str(profile.get(f"brand_color_{key}") or "").strip()
            if saved:
                self.brand_colors[key] = saved

    def _build_config_tab(self) -> None:
        """Aba de identidade visual: logo, nome, marca d'água e cores da tarja."""
        tab = QWidget()
        tab.setStyleSheet("background: #0f1119;")
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(22, 22, 22, 22)
        outer.setSpacing(16)

        intro = QLabel(
            "Configure a identidade que vai em todos os vídeos (Edição, CSV e "
            "Live). Tudo é salvo automaticamente e usado nos próximos cortes.")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        outer.addWidget(intro)

        # ── Seleção e gerenciamento de perfis ──────────────────────
        profile_box = QGroupBox("Perfis")
        profile_form = QFormLayout(profile_box)

        # Dropdown para selecionar perfil
        self.cfg_profile_combo = QComboBox()
        self.cfg_profile_combo.currentTextChanged.connect(self._cfg_load_profile)
        self._cfg_refresh_profile_list()
        profile_form.addRow("Perfil ativo", self.cfg_profile_combo)

        # Campo para nome do novo perfil
        new_profile_row = QWidget()
        new_profile_layout = QHBoxLayout(new_profile_row)
        new_profile_layout.setContentsMargins(0, 0, 0, 0)
        self.cfg_new_profile_name = QLineEdit()
        self.cfg_new_profile_name.setPlaceholderText("ex.: esportes, economia...")
        save_as_btn = QPushButton("Salvar como novo perfil")
        save_as_btn.clicked.connect(self._cfg_save_as_new_profile)
        delete_btn = QPushButton("🗑 Deletar este perfil")
        delete_btn.clicked.connect(self._cfg_delete_current_profile)
        new_profile_layout.addWidget(self.cfg_new_profile_name, 1)
        new_profile_layout.addWidget(save_as_btn)
        new_profile_layout.addWidget(delete_btn)
        profile_form.addRow("Novo perfil", new_profile_row)
        outer.addWidget(profile_box)

        # ── Logo e textos ──────────────────────────────────────────
        id_box = QGroupBox("Logo e textos")
        id_form = QFormLayout(id_box)

        logo_row = QWidget()
        logo_layout = QHBoxLayout(logo_row); logo_layout.setContentsMargins(0, 0, 0, 0)
        self.cfg_logo_label = QLabel(
            self.cg_icon_path.name if self.cg_icon_path else "Nenhum logo escolhido")
        self.cfg_logo_label.setObjectName("muted")
        self.cfg_logo_label.setWordWrap(True)
        logo_btn = QPushButton("Escolher…")
        logo_btn.clicked.connect(self._cfg_pick_logo)
        logo_clear = QPushButton("Remover")
        logo_clear.clicked.connect(self._cfg_clear_logo)
        logo_layout.addWidget(self.cfg_logo_label, 1)
        logo_layout.addWidget(logo_btn)
        logo_layout.addWidget(logo_clear)
        id_form.addRow("Logo (PNG)", logo_row)

        self.cfg_name = QLineEdit(self.brand_name)
        self.cfg_name.setPlaceholderText("ex.: Informativo Nacional")
        self.cfg_name.editingFinished.connect(self._save_branding)
        id_form.addRow("Nome do canal", self.cfg_name)

        self.cfg_watermark = QLineEdit(self.brand_watermark)
        self.cfg_watermark.setPlaceholderText("ex.: @SEUCANAL (vazio = sem marca)")
        self.cfg_watermark.editingFinished.connect(self._save_branding)
        id_form.addRow("Marca d'água", self.cfg_watermark)
        outer.addWidget(id_box)

        # ── Cores ──────────────────────────────────────────────────
        cores_box = QGroupBox("Cores da tarja (lower-third) e marca d'água")
        cores_form = QFormLayout(cores_box)
        self._cfg_color_btns: dict[str, QPushButton] = {}
        # (chave interna, rótulo). "wm" é a cor da marca d'água; as demais são as
        # cores da tarja definidas no cg_generator.
        for key, rotulo in (
            ("banner", "Fundo da tarja"),
            ("green", "Acento / chapéu"),
            ("yellow", "Detalhe (amarelo)"),
            ("headline", "Texto da manchete"),
            ("wm", "Marca d'água"),
        ):
            btn = QPushButton()
            btn.setMinimumHeight(28)
            btn.clicked.connect(lambda _=False, k=key: self._cfg_pick_color(k))
            self._cfg_color_btns[key] = btn
            self._cfg_refresh_color_btn(key)
            cores_form.addRow(rotulo, btn)
        reset_btn = QPushButton("↩ Voltar às cores padrão (bandeira)")
        reset_btn.clicked.connect(self._cfg_reset_colors)
        cores_form.addRow(reset_btn)
        outer.addWidget(cores_box)

        # ── Pré-visualização ───────────────────────────────────────
        prev_box = QGroupBox("Pré-visualização da tarja")
        prev_layout = QVBoxLayout(prev_box)
        self.cfg_preview = QLabel("Clique em “Gerar prévia” para ver a tarja.")
        self.cfg_preview.setObjectName("muted")
        self.cfg_preview.setMinimumHeight(90)
        self.cfg_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        prev_layout.addWidget(self.cfg_preview)
        prev_btn = QPushButton("🖼 Gerar prévia")
        prev_btn.clicked.connect(self._cfg_preview_lower_third)
        prev_layout.addWidget(prev_btn)
        outer.addWidget(prev_box)

        outer.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(tab)
        self.tabs.addTab(scroll, "⚙️ Configuração")

    def _cfg_refresh_profile_list(self) -> None:
        """Recarrega a lista de perfis no combobox."""
        profiles, current = ai_srt.get_profiles_dict()
        self.cfg_profile_combo.blockSignals(True)
        self.cfg_profile_combo.clear()
        for name in sorted(profiles.keys()):
            self.cfg_profile_combo.addItem(name, name)
        # Seleciona o perfil atual
        idx = self.cfg_profile_combo.findData(current)
        if idx >= 0:
            self.cfg_profile_combo.setCurrentIndex(idx)
        self.cfg_profile_combo.blockSignals(False)

    def _cfg_load_profile(self, name: str) -> None:
        """Carrega um perfil e atualiza a aba com seus dados."""
        if not name or name == self._current_profile:
            return
        # Muda o perfil ativo
        if not ai_srt.set_current_profile(name):
            QMessageBox.warning(self, APP_NAME, f"Não foi possível carregar o perfil '{name}'.")
            return
        self._current_profile = name
        self._load_branding()
        # Recarrega os controles da aba
        self.cfg_name.setText(self.brand_name)
        self.cfg_watermark.setText(self.brand_watermark)
        if self.cg_icon_path:
            self.cfg_logo_label.setText(self.cg_icon_path.name)
        else:
            self.cfg_logo_label.setText("Nenhum logo escolhido")
        for key in self._cfg_color_btns:
            self._cfg_refresh_color_btn(key)
        self._refresh_cg_enabled()

    def _cfg_save_as_new_profile(self) -> None:
        """Salva as configs atuais como um novo perfil."""
        new_name = self.cfg_new_profile_name.text().strip().lower()
        if not new_name:
            QMessageBox.warning(self, APP_NAME, "Digite um nome para o novo perfil.")
            return
        # Valida o nome (alfanumérico + espaço/hífen)
        if not all(c.isalnum() or c in " -_" for c in new_name):
            QMessageBox.warning(self, APP_NAME, "O nome do perfil pode ter só letras, números, espaço e hífen.")
            return
        # Verifica se já existe
        profiles, _ = ai_srt.get_profiles_dict()
        if new_name in profiles:
            if QMessageBox.question(
                    self, APP_NAME,
                    f"Um perfil chamado '{new_name}' já existe. Sobrescrever?"
            ) != QMessageBox.StandardButton.Yes:
                return
        # Salva as configs atuais como novo perfil
        self._save_branding()  # atualiza o perfil atual primeiro
        profile_data = ai_srt.load_profile(self._current_profile)
        if ai_srt.save_profile(new_name, profile_data, set_as_current=True):
            self._current_profile = new_name
            self._cfg_refresh_profile_list()
            self.cfg_new_profile_name.clear()
            QMessageBox.information(self, APP_NAME, f"Perfil '{new_name}' criado e ativado.")
        else:
            QMessageBox.critical(self, APP_NAME, "Erro ao salvar o novo perfil.")

    def _cfg_delete_current_profile(self) -> None:
        """Deleta o perfil ativo (se não for o último)."""
        profiles, current = ai_srt.get_profiles_dict()
        if len(profiles) <= 1:
            QMessageBox.warning(self, APP_NAME, "Não dá para deletar o único perfil existente.")
            return
        if QMessageBox.question(
                self, APP_NAME,
                f"Deletar o perfil '{current}'? Esta ação não tem volta."
        ) != QMessageBox.StandardButton.Yes:
            return
        if ai_srt.delete_profile(current):
            profiles, new_current = ai_srt.get_profiles_dict()
            self._current_profile = new_current
            self._load_branding()
            self._cfg_refresh_profile_list()
            # Recarrega os controles
            self.cfg_name.setText(self.brand_name)
            self.cfg_watermark.setText(self.brand_watermark)
            if self.cg_icon_path:
                self.cfg_logo_label.setText(self.cg_icon_path.name)
            else:
                self.cfg_logo_label.setText("Nenhum logo escolhido")
            for key in self._cfg_color_btns:
                self._cfg_refresh_color_btn(key)
            self._refresh_cg_enabled()
            QMessageBox.information(self, APP_NAME, f"Perfil '{current}' deletado.")
        else:
            QMessageBox.critical(self, APP_NAME, "Erro ao deletar o perfil.")

    def _cfg_current_color(self, key: str) -> str:
        """Cor atual de uma chave (tarja ou marca d'água), em hex."""
        if key == "wm":
            return self.brand_wm_color or DEFAULT_WM_COLOR
        return self.brand_colors.get(key, DEFAULT_COLORS.get(key, "#FFFFFF"))

    def _cfg_refresh_color_btn(self, key: str) -> None:
        """Atualiza o texto/fundo do botão de cor para refletir a cor atual."""
        cor = self._cfg_current_color(key)
        btn = self._cfg_color_btns[key]
        btn.setText(cor.upper())
        # Texto legível sobre o fundo colorido do botão.
        legivel = "#000000" if self._cfg_is_light(cor) else "#FFFFFF"
        # Usa setStyleSheet com sintaxe correta para evitar parse errors.
        stylesheet = (
            f"QPushButton {{ background-color: {cor}; color: {legivel}; "
            f"border: 1px solid #2d3140; border-radius: 6px; font-weight: 600; }}"
        )
        btn.setStyleSheet(stylesheet)

    @staticmethod
    def _cfg_is_light(hex_color: str) -> bool:
        """True se a cor for clara (para escolher texto preto sobre ela)."""
        try:
            h = hex_color.lstrip("#")
            r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
        except (ValueError, IndexError):
            return True
        return (0.299 * r + 0.587 * g + 0.114 * b) > 150

    def _cfg_pick_color(self, key: str) -> None:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog
        atual = QColor(self._cfg_current_color(key))
        escolhida = QColorDialog.getColor(atual, self, "Escolha a cor")
        if not escolhida.isValid():
            return
        hexcor = escolhida.name().upper()   # "#RRGGBB"
        if key == "wm":
            self.brand_wm_color = hexcor
        else:
            self.brand_colors[key] = hexcor
        self._cfg_refresh_color_btn(key)
        self._save_branding()

    def _cfg_reset_colors(self) -> None:
        self.brand_colors = dict(DEFAULT_COLORS)
        self.brand_wm_color = DEFAULT_WM_COLOR
        for key in self._cfg_color_btns:
            self._cfg_refresh_color_btn(key)
        self._save_branding()

    def _cfg_pick_logo(self) -> None:
        caminho, _ = QFileDialog.getOpenFileName(
            self, "Escolha o logo do canal", "",
            "Imagens (*.png *.jpg *.jpeg *.webp)")
        if not caminho:
            return
        self.cg_icon_path = Path(caminho)
        self.cfg_logo_label.setText(self.cg_icon_path.name)
        self._refresh_cg_enabled()
        self._save_branding()

    def _cfg_clear_logo(self) -> None:
        self.cg_icon_path = None
        self.cfg_logo_label.setText("Nenhum logo escolhido")
        self._refresh_cg_enabled()
        self._save_branding()

    def _refresh_cg_enabled(self) -> None:
        """Liga/desliga os checkboxes de tarja conforme haja logo escolhido."""
        tem = bool(self.cg_icon_path)
        for attr in ("use_cg", "csv_use_cg"):
            box = getattr(self, attr, None)
            if box is not None:
                box.setEnabled(tem)
                if not tem:
                    box.setChecked(False)

    def _save_branding(self) -> None:
        """Grava a identidade visual no perfil ativo."""
        self.brand_name = self.cfg_name.text().strip() or DEFAULT_BRAND_NAME
        self.brand_watermark = self.cfg_watermark.text()
        profile_data = {
            "brand_logo": str(self.cg_icon_path) if self.cg_icon_path else "",
            "brand_name": self.brand_name,
            "brand_watermark": self.brand_watermark,
            "brand_wm_color": self.brand_wm_color,
            "brand_color_banner": self.brand_colors["banner"],
            "brand_color_green": self.brand_colors["green"],
            "brand_color_yellow": self.brand_colors["yellow"],
            "brand_color_headline": self.brand_colors["headline"],
        }
        ai_srt.save_profile(self._current_profile, profile_data)

    def _cfg_preview_lower_third(self) -> None:
        """Gera uma tarja de exemplo com as cores/logo atuais e mostra na aba."""
        from PySide6.QtGui import QPixmap
        png = create_lower_third(
            self.brand_name.upper(), "MANCHETE DE EXEMPLO EM DESTAQUE",
            self.work_dir, self.cg_icon_path, colors=self.brand_colors)
        if png and png.exists():
            pix = QPixmap(str(png))
            if not pix.isNull():
                self.cfg_preview.setPixmap(
                    pix.scaledToWidth(360, Qt.TransformationMode.SmoothTransformation))
                return
        self.cfg_preview.setText("Não foi possível gerar a prévia (verifique o FFmpeg).")

    # ──────────────────────────────────────────────────────────────
    #  Aba YouTube — fila de publicação
    # ──────────────────────────────────────────────────────────────

    def _build_youtube_tab(self) -> None:
        """Fila de vídeos para publicar no YouTube, com várias contas/canais.

        O controle é um .txt (`youtube_queue.txt`, via youtube_upload.py):
        cada linha é um vídeo, a conta de destino e (opcional) o horário em
        que deve ficar público. A fila roda em ordem enquanto o botão
        "Iniciar envios" estiver ligado — não há horário de envio por vídeo,
        só o botão de ligar/desligar. Ao terminar com sucesso, o vídeo sai da
        lista sozinho — para tentar de novo é só importar de novo.

        As contas vêm dos arquivos client_secret_<apelido>.json na pasta do
        programa — um por canal (ex.: client_secret_info.json,
        client_secret_br.json).
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(10)

        title = QLabel("Fila de publicação no YouTube")
        title.setObjectName("title")
        subtitle = QLabel(
            "Importe os vídeos prontos, escolha o canal de cada um e clique "
            "em Iniciar — a fila envia em ordem enquanto estiver ligada.")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        top_row = QHBoxLayout()
        top_row.setSpacing(14)

        conn_box = QGroupBox("Conta do YouTube")
        conn_form = QFormLayout(conn_box)
        self.yt_account_selector = QComboBox()
        self.yt_account_selector.currentIndexChanged.connect(self._refresh_youtube_queue_status)
        conn_form.addRow("Canal", self.yt_account_selector)
        self.yt_queue_status = QLabel()
        self.yt_queue_status.setWordWrap(True)
        conn_form.addRow(self.yt_queue_status)
        self.yt_queue_connect_btn = QPushButton("Conectar conta do YouTube")
        self.yt_queue_connect_btn.clicked.connect(self._connect_youtube)
        conn_form.addRow(self.yt_queue_connect_btn)
        top_row.addWidget(conn_box, 1)

        config = ai_srt.load_config()
        settings_box = QGroupBox("Configuração de envio")
        settings_form = QFormLayout(settings_box)
        self.yt_queue_privacy = QComboBox()
        self.yt_queue_privacy.addItem("Privado", "private")
        self.yt_queue_privacy.addItem("Não listado", "unlisted")
        self.yt_queue_privacy.addItem("Público", "public")
        saved_privacy = config.get("youtube_privacy", "private")
        index = self.yt_queue_privacy.findData(saved_privacy)
        self.yt_queue_privacy.setCurrentIndex(index if index >= 0 else 0)
        self.yt_queue_privacy.currentIndexChanged.connect(self._save_youtube_queue_config)
        settings_form.addRow("Privacidade", self.yt_queue_privacy)

        # Uma descrição padrão por canal — cada um tem seu próprio texto de
        # "siga o canal" e suas próprias hashtags. Trocar o canal acima troca
        # o texto mostrado aqui (_refresh_youtube_queue_status cuida disso).
        self.yt_queue_description = QPlainTextEdit()
        self.yt_queue_description.setMaximumHeight(60)
        self.yt_queue_description.setPlaceholderText(
            "Descrição padrão deste canal, usada nos vídeos da fila (opcional)")
        self.yt_queue_description.textChanged.connect(self._save_youtube_queue_config)
        settings_form.addRow("Descrição\n(deste canal)", self.yt_queue_description)
        top_row.addWidget(settings_box, 1)
        layout.addLayout(top_row)

        note = QLabel(
            "O título de cada vídeo é o nome do arquivo. Cada canal precisa "
            "de um client_secret_<apelido>.json na pasta do programa e ter "
            "passado pela auditoria do YouTube para publicar como público.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        list_box = QGroupBox("Vídeos na fila")
        list_layout = QVBoxLayout(list_box)

        toggle_row = QHBoxLayout()
        self.yt_queue_toggle_btn = QPushButton("▶ Iniciar envios")
        self.yt_queue_toggle_btn.setObjectName("primary")
        self.yt_queue_toggle_btn.clicked.connect(self._toggle_youtube_queue_running)
        self.yt_queue_running_label = QLabel("Parado — nada será enviado até você clicar em Iniciar.")
        self.yt_queue_running_label.setObjectName("muted")
        toggle_row.addWidget(self.yt_queue_toggle_btn)
        toggle_row.addWidget(self.yt_queue_running_label, 1)
        list_layout.addLayout(toggle_row)

        buttons_row = QHBoxLayout()
        import_btn = QPushButton("Importar vídeos…")
        import_btn.clicked.connect(self._import_youtube_queue_videos)
        remove_btn = QPushButton("Remover selecionado")
        remove_btn.clicked.connect(self._remove_youtube_queue_selected)
        send_now_btn = QPushButton("Enviar selecionado agora")
        send_now_btn.clicked.connect(self._send_selected_youtube_queue_item_now)
        buttons_row.addWidget(import_btn)
        buttons_row.addWidget(remove_btn)
        buttons_row.addWidget(send_now_btn)
        buttons_row.addStretch()
        list_layout.addLayout(buttons_row)

        self.yt_queue_table = QTableWidget(0, 4)
        self.yt_queue_table.setHorizontalHeaderLabels(["Vídeo", "Canal", "Publicar em", "Status"])
        self.yt_queue_table.horizontalHeader().setStretchLastSection(False)
        self.yt_queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.yt_queue_table.setColumnWidth(1, 100)
        self.yt_queue_table.setColumnWidth(2, 230)
        self.yt_queue_table.setColumnWidth(3, 90)
        self.yt_queue_table.verticalHeader().setVisible(False)
        self.yt_queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.yt_queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        # A tabela é o que importa nesta aba — ocupa o espaço que sobrar,
        # bem mais do que o log logo abaixo dela.
        list_layout.addWidget(self.yt_queue_table, 5)

        self.yt_queue_log = QPlainTextEdit()
        self.yt_queue_log.setReadOnly(True)
        self.yt_queue_log.setMaximumHeight(90)
        list_layout.addWidget(self.yt_queue_log)
        layout.addWidget(list_box, 1)

        self.tabs.addTab(tab, "📤 YouTube")
        self._reload_youtube_accounts()
        self._reload_youtube_queue_table()

    def _reload_youtube_accounts(self) -> None:
        """Recarrega os canais a partir dos client_secret_<apelido>.json da pasta."""
        contas = youtube_upload.list_accounts()
        atual = self.yt_account_selector.currentText()
        self.yt_account_selector.blockSignals(True)
        self.yt_account_selector.clear()
        self.yt_account_selector.addItems(contas)
        if atual and atual in contas:
            self.yt_account_selector.setCurrentText(atual)
        self.yt_account_selector.blockSignals(False)
        self._refresh_youtube_queue_status()

    def _youtube_description_for(self, account: str) -> str:
        """Descrição padrão salva para esse canal ('' se nunca foi definida)."""
        descriptions = ai_srt.load_config().get("youtube_descriptions") or {}
        return descriptions.get(account, "") if isinstance(descriptions, dict) else ""

    def _save_youtube_queue_config(self) -> None:
        conta = self.yt_account_selector.currentText()
        descriptions = ai_srt.load_config().get("youtube_descriptions") or {}
        if not isinstance(descriptions, dict):
            descriptions = {}
        if conta:
            descriptions[conta] = self.yt_queue_description.toPlainText()
        ai_srt.save_config({
            "youtube_privacy": self.yt_queue_privacy.currentData(),
            "youtube_descriptions": descriptions,
        })

    def _refresh_youtube_queue_status(self) -> None:
        conta = self.yt_account_selector.currentText()
        # Troca de canal: mostra a descrição salva desse canal, sem disparar
        # o textChanged (que salvaria o texto do canal anterior por engano).
        self.yt_queue_description.blockSignals(True)
        self.yt_queue_description.setPlainText(self._youtube_description_for(conta))
        self.yt_queue_description.blockSignals(False)
        if not youtube_upload.list_accounts():
            self.yt_queue_status.setText(
                "⚠️ Nenhum client_secret_<apelido>.json encontrado na pasta do programa.")
            self.yt_queue_connect_btn.setEnabled(False)
        elif not conta:
            self.yt_queue_status.setText("Escolha um canal.")
            self.yt_queue_connect_btn.setEnabled(False)
        elif youtube_upload.is_authorized(conta):
            self.yt_queue_status.setText(f"✅ Canal '{conta}' conectado.")
            self.yt_queue_connect_btn.setText("Reconectar este canal")
            self.yt_queue_connect_btn.setEnabled(True)
        else:
            self.yt_queue_status.setText(f"Canal '{conta}' ainda não conectado.")
            self.yt_queue_connect_btn.setText("Conectar conta do YouTube")
            self.yt_queue_connect_btn.setEnabled(True)

    def _connect_youtube(self) -> None:
        conta = self.yt_account_selector.currentText()
        if not conta:
            QMessageBox.warning(self, APP_NAME, "Escolha um canal antes de conectar.")
            return
        self.yt_queue_connect_btn.setEnabled(False)
        self.yt_queue_connect_btn.setText("Abrindo o navegador para login…")
        try:
            youtube_upload.authorize(conta)
        except youtube_upload.YoutubeUploadError as error:
            QMessageBox.critical(self, APP_NAME, f"Não deu para conectar:\n\n{error}")
        else:
            QMessageBox.information(self, APP_NAME, f"Canal '{conta}' conectado.")
        self._refresh_youtube_queue_status()

    def _import_youtube_queue_videos(self) -> None:
        conta_padrao = self.yt_account_selector.currentText()
        files, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar vídeos para a fila", str(OUTPUT_DIR),
            "Vídeos (*.mp4 *.mov *.mkv *.avi *.webm)")
        if not files:
            return
        existentes = {str(item.path) for item in self._youtube_queue}
        adicionados = 0
        for arquivo in files:
            if arquivo in existentes:
                continue
            self._youtube_queue.append(youtube_upload.QueueItem(Path(arquivo), conta_padrao))
            adicionados += 1
        if adicionados:
            self._save_youtube_queue()
            self._reload_youtube_queue_table()
            self.yt_queue_log.appendPlainText(f"➕ {adicionados} vídeo(s) adicionado(s) à fila.")

    def _remove_youtube_queue_selected(self) -> None:
        row = self.yt_queue_table.currentRow()
        if row < 0 or row >= len(self._youtube_queue):
            return
        nome = self._youtube_queue[row].path.name
        del self._youtube_queue[row]
        self._save_youtube_queue()
        self._reload_youtube_queue_table()
        self.yt_queue_log.appendPlainText(f"➖ {nome} removido da fila (sem enviar).")

    def _save_youtube_queue(self) -> None:
        youtube_upload.save_queue(self._youtube_queue)

    def _reload_youtube_queue_table(self) -> None:
        contas = youtube_upload.list_accounts()
        self.yt_queue_table.setRowCount(0)
        for item in self._youtube_queue:
            row = self.yt_queue_table.rowCount()
            self.yt_queue_table.insertRow(row)
            self.yt_queue_table.setRowHeight(row, 34)

            nome_item = QTableWidgetItem(item.path.name)
            nome_item.setFlags(nome_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.yt_queue_table.setItem(row, 0, nome_item)

            conta_combo = QComboBox()
            conta_combo.addItems(contas)
            if item.account and item.account in contas:
                conta_combo.setCurrentText(item.account)
            elif contas:
                item.account = contas[0]
            conta_combo.currentTextChanged.connect(
                lambda texto, widget=conta_combo: self._on_youtube_queue_account_changed(widget, texto))
            self.yt_queue_table.setCellWidget(row, 1, conta_combo)

            publish_widget = QWidget()
            publish_layout = QHBoxLayout(publish_widget)
            publish_layout.setContentsMargins(4, 0, 4, 0)
            publish_layout.setSpacing(4)
            publish_check = QCheckBox()
            publish_check.setToolTip(
                "Agendar publicação: o vídeo sobe privado e o YouTube libera "
                "sozinho na data/hora ao lado.")
            publish_datetime = QDateTimeEdit(QDateTime(item.publish_at or datetime.now()))
            publish_datetime.setCalendarPopup(True)
            publish_datetime.setDisplayFormat("dd/MM/yy HH:mm")
            publish_datetime.setMinimumWidth(140)
            publish_check.setChecked(item.publish_at is not None)
            publish_datetime.setEnabled(item.publish_at is not None)
            publish_check.toggled.connect(
                lambda checked, w=publish_datetime, c=publish_check:
                    self._on_youtube_queue_publish_toggled(c, w, checked))
            publish_datetime.dateTimeChanged.connect(
                lambda dt, c=publish_check: self._on_youtube_queue_publish_changed(c, c.isChecked(), dt))
            publish_layout.addWidget(publish_check)
            publish_layout.addWidget(publish_datetime, 1)
            self.yt_queue_table.setCellWidget(row, 2, publish_widget)

            status_item = QTableWidgetItem("Aguardando" if self._youtube_queue_running else "Pausado")
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.yt_queue_table.setItem(row, 3, status_item)
        self._save_youtube_queue()

    def _on_youtube_queue_account_changed(self, widget: QComboBox, texto: str) -> None:
        """Acha a linha pelo próprio widget (índices mudam quando a fila encolhe)."""
        for row in range(self.yt_queue_table.rowCount()):
            if self.yt_queue_table.cellWidget(row, 1) is widget:
                if row < len(self._youtube_queue):
                    self._youtube_queue[row].account = texto
                    self._save_youtube_queue()
                break

    def _on_youtube_queue_publish_toggled(self, checkbox: QCheckBox, widget: QDateTimeEdit, checked: bool) -> None:
        """Liga/desliga o campo de data e propaga a mudança para a fila."""
        widget.setEnabled(checked)
        self._on_youtube_queue_publish_changed(checkbox, checked, widget.dateTime())

    def _on_youtube_queue_publish_changed(self, checkbox: QCheckBox, checked: bool, value: QDateTime) -> None:
        """Acha a linha pelo checkbox (índices mudam quando a fila encolhe).

        `publish_at` é o agendamento de verdade do YouTube: com o checkbox
        marcado, o vídeo sobe privado e o próprio YouTube libera na hora
        marcada; desmarcado, publica com a privacidade escolhida assim que o
        upload terminar.
        """
        for row in range(self.yt_queue_table.rowCount()):
            container = self.yt_queue_table.cellWidget(row, 2)
            if container and container.findChild(QCheckBox) is checkbox:
                if row < len(self._youtube_queue):
                    self._youtube_queue[row].publish_at = value.toPython() if checked else None
                    self._save_youtube_queue()
                break

    def _toggle_youtube_queue_running(self) -> None:
        """Liga/desliga o envio automático da fila inteira.

        Parado, nenhum vídeo sai sozinho — dá tempo de importar e configurar
        vários antes de qualquer coisa ser enviada. "Enviar selecionado
        agora" continua funcionando mesmo parado, para um envio pontual sem
        ligar a fila inteira.
        """
        self._youtube_queue_running = not self._youtube_queue_running
        if self._youtube_queue_running:
            self.yt_queue_toggle_btn.setText("⏸ Parar envios")
            self.yt_queue_running_label.setText(
                "Enviando — a fila processa em ordem enquanto estiver ligada.")
            self._process_youtube_queue()
        else:
            self.yt_queue_toggle_btn.setText("▶ Iniciar envios")
            self.yt_queue_running_label.setText(
                "Parado — nada será enviado até você clicar em Iniciar.")
        for row in range(len(self._youtube_queue)):
            atual = self.yt_queue_table.item(row, 3)
            if atual and atual.text() in ("Enviando…", "Falhou"):
                continue                    # não atropela um envio em curso ou uma falha
            self._set_youtube_queue_row_status(
                row, "Aguardando" if self._youtube_queue_running else "Pausado")

    def _check_youtube_queue_schedule(self) -> None:
        """Chamado pelo timer a cada 30s: mantém a fila andando se ela estiver ligada.

        Cobre o caso de um item ter ficado parado por falta de conta
        conectada — assim que o usuário conectar o canal, este timer retoma
        sem precisar clicar em Iniciar de novo.
        """
        if not self._youtube_queue_running or self._youtube_queue_busy or not self._youtube_queue:
            return
        self._process_youtube_queue()

    def _send_selected_youtube_queue_item_now(self) -> None:
        """Ignora o estado da fila (parada ou não) e envia o selecionado imediatamente."""
        row = self.yt_queue_table.currentRow()
        if row < 0 or row >= len(self._youtube_queue):
            QMessageBox.information(self, APP_NAME, "Selecione um vídeo da fila primeiro.")
            return
        if self._youtube_queue_busy:
            QMessageBox.information(self, APP_NAME, "Já há um envio em andamento.")
            return
        item = self._youtube_queue[row]
        if not youtube_upload.is_authorized(item.account):
            QMessageBox.warning(
                self, APP_NAME, f"Conecte o canal '{item.account}' antes de enviar.")
            return
        self._process_youtube_queue(force_index=row)

    def _process_youtube_queue(self, force_index: int | None = None) -> None:
        """Processa a fila em ordem, um vídeo por vez, pulando quem não tem conta conectada.

        `force_index`, usado só por "Enviar selecionado agora", manda aquele
        item específico mesmo com a fila parada, sem ligá-la — o restante da
        fila só sai se o usuário clicar em Iniciar.
        """
        if self._youtube_queue_busy:
            return
        if not self._youtube_queue_running and force_index is None:
            return
        self._youtube_queue_busy = True
        self._send_next_due_youtube_queue_item(force_index=force_index)

    def _send_next_due_youtube_queue_item(self, force_index: int | None = None) -> None:
        if force_index is not None:
            index = force_index
        else:
            if not self._youtube_queue_running:
                self._youtube_queue_busy = False
                return
            # Pula quem já falhou, senão o timer da fila reenvia o mesmo
            # vídeo a cada 30s pra sempre. "Enviar selecionado agora" ainda
            # tenta de novo, de propósito.
            index = next(
                (i for i, item in enumerate(self._youtube_queue)
                 if youtube_upload.is_authorized(item.account)
                 and (self.yt_queue_table.item(i, 3) is None
                      or self.yt_queue_table.item(i, 3).text() != "Falhou")),
                None)
        if index is None or index >= len(self._youtube_queue):
            self._youtube_queue_busy = False
            return
        item = self._youtube_queue[index]
        if not item.path.exists():
            self.yt_queue_log.appendPlainText(
                f"⚠️ {item.path.name} não existe mais no disco; removendo da fila.")
            del self._youtube_queue[index]
            self._save_youtube_queue()
            self._reload_youtube_queue_table()
            self._send_next_due_youtube_queue_item()
            return
        self._set_youtube_queue_row_status(index, "Enviando…")
        aviso_agendamento = f", agendado para {item.publish_at:%d/%m %H:%M}" if item.publish_at else ""
        self.yt_queue_log.appendPlainText(
            f"📤 Enviando {item.path.name} (canal '{item.account}'{aviso_agendamento})…")
        worker = YoutubeUploadWorker(
            item.path, item.path.stem, item.account, self._youtube_description_for(item.account),
            [], self.yt_queue_privacy.currentData(), item.publish_at, self)
        worker.progress.connect(
            lambda frac, nome=item.path.name: self.yt_queue_log.appendPlainText(
                f"   {nome}: {frac:.0%}"))
        worker.done.connect(lambda ok, msg, idx=index: self._on_youtube_queue_item_done(ok, msg, idx))
        worker.finished.connect(worker.deleteLater)
        self._youtube_queue_worker = worker
        worker.start()

    def _set_youtube_queue_row_status(self, row: int, texto: str) -> None:
        item = self.yt_queue_table.item(row, 3)
        if item:
            item.setText(texto)

    def _on_youtube_queue_item_done(self, ok: bool, message: str, index: int) -> None:
        self.yt_queue_log.appendPlainText(("✅ " if ok else "⚠️ ") + message)
        if ok:
            if index < len(self._youtube_queue):
                del self._youtube_queue[index]
                self._save_youtube_queue()
                self._reload_youtube_queue_table()
            self._send_next_due_youtube_queue_item()
        else:
            # Erro: para a fila aqui para o usuário ver o que houve, em vez de
            # tentar os próximos e empilhar mais falhas.
            self._set_youtube_queue_row_status(index, "Falhou")
            self._youtube_queue_busy = False

    # ──────────────────────────────────────────────────────────────
    #  Aba YouTube via navegador — fila de publicação pela tela do Studio
    # ──────────────────────────────────────────────────────────────

    def _build_youtube_browser_tab(self) -> None:
        """Fila de vídeos para publicar no YouTube pela tela do Studio (sem API).

        Mesmo motivo do TikTok: a API oficial tem cota apertada (~6 vídeos/dia)
        e, na experiência medida neste projeto, os vídeos enviados por ela
        quase não eram entregues. Este fluxo abre um navegador de verdade
        (Playwright/Chromium), autenticado com os cookies de uma sessão já
        logada, e faz exatamente o que uma pessoa faria: abre o Studio, clica
        em Criar → Enviar vídeos, preenche título/descrição, avança pelas
        etapas de verificação e publica (ou agenda) no fim.

        Sem login programático — cada canal precisa de um
        youtube_browser_cookies_<apelido>.txt importado nesta aba, do mesmo
        jeito que a aba TikTok.
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(10)

        title = QLabel("Fila de publicação no YouTube (navegador)")
        title.setObjectName("title")
        subtitle = QLabel(
            "Importe os vídeos prontos, escolha o canal de cada um e clique "
            "em Iniciar — a fila envia em ordem enquanto estiver ligada, "
            "abrindo o Studio de verdade em vez de usar a API.")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        top_row = QHBoxLayout()
        top_row.setSpacing(14)

        conn_box = QGroupBox("Canal do YouTube")
        conn_form = QFormLayout(conn_box)
        self.yb_account_selector = QComboBox()
        self.yb_account_selector.setEditable(True)
        self.yb_account_selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.yb_account_selector.currentTextChanged.connect(self._refresh_youtube_browser_queue_status)
        conn_form.addRow("Apelido", self.yb_account_selector)
        self.yb_queue_status = QLabel()
        self.yb_queue_status.setWordWrap(True)
        conn_form.addRow(self.yb_queue_status)
        self.yb_queue_import_cookies_btn = QPushButton("Importar cookies.txt…")
        self.yb_queue_import_cookies_btn.setToolTip(
            "Logue em studio.youtube.com no navegador (no canal certo), "
            "exporte os cookies com uma extensão (ex.: 'Get cookies.txt') e "
            "escolha o arquivo aqui.")
        self.yb_queue_import_cookies_btn.clicked.connect(self._import_youtube_browser_cookies)
        conn_form.addRow(self.yb_queue_import_cookies_btn)
        top_row.addWidget(conn_box, 1)

        config = ai_srt.load_config()
        settings_box = QGroupBox("Configuração de envio")
        settings_form = QFormLayout(settings_box)
        self.yb_queue_visibility = QComboBox()
        self.yb_queue_visibility.addItem("Privado", "private")
        self.yb_queue_visibility.addItem("Não listado", "unlisted")
        self.yb_queue_visibility.addItem("Público", "public")
        # Sem agendamento nesta aba, o vídeo fica valendo com esta
        # visibilidade assim que o envio termina — por isso o padrão aqui é
        # "Público", diferente da aba YouTube (API), onde "Privado" é mais
        # seguro por poder ficar parado esperando revisão manual.
        saved_visibility = config.get("youtube_browser_visibility", "public")
        index = self.yb_queue_visibility.findData(saved_visibility)
        self.yb_queue_visibility.setCurrentIndex(index if index >= 0 else 2)
        self.yb_queue_visibility.currentIndexChanged.connect(self._save_youtube_browser_queue_config)
        settings_form.addRow("Visibilidade", self.yb_queue_visibility)

        self.yb_queue_headless = QCheckBox("Rodar o navegador escondido (headless)")
        self.yb_queue_headless.setChecked(bool(config.get("youtube_browser_headless", False)))
        self.yb_queue_headless.setToolTip(
            "O YouTube detecta automação com mais facilidade nesse modo. "
            "Deixe desmarcado a menos que precise mesmo.")
        self.yb_queue_headless.toggled.connect(self._save_youtube_browser_queue_config)
        settings_form.addRow(self.yb_queue_headless)

        # Uma descrição padrão por canal, igual ao esquema da aba YouTube (API).
        self.yb_queue_description = QPlainTextEdit()
        self.yb_queue_description.setMaximumHeight(60)
        self.yb_queue_description.setPlaceholderText(
            "Descrição padrão deste canal, usada nos vídeos da fila (opcional)")
        self.yb_queue_description.textChanged.connect(self._save_youtube_browser_queue_config)
        settings_form.addRow("Descrição\n(deste canal)", self.yb_queue_description)
        top_row.addWidget(settings_box, 1)
        layout.addLayout(top_row)

        note = QLabel(
            "Contorna a tela do Studio (studio.youtube.com), não a API — "
            "contraria os Termos de Serviço do YouTube e é frágil a mudanças "
            "da tela, mas não tem cota diária. Sem login programático — cada "
            "canal precisa de um youtube_browser_cookies_<apelido>.txt "
            "importado nesta aba. O título de cada vídeo é o nome do "
            "arquivo. Sem agendamento nesta aba — todo vídeo vai direto com "
            "a visibilidade escolhida acima assim que o envio e as "
            "verificações do YouTube terminarem. Se falhar, confira "
            "youtube_browser_falha.png/.html na pasta do programa.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        list_box = QGroupBox("Vídeos na fila")
        list_layout = QVBoxLayout(list_box)

        toggle_row = QHBoxLayout()
        self.yb_queue_toggle_btn = QPushButton("▶ Iniciar envios")
        self.yb_queue_toggle_btn.setObjectName("primary")
        self.yb_queue_toggle_btn.clicked.connect(self._toggle_youtube_browser_queue_running)
        self.yb_queue_running_label = QLabel("Parado — nada será enviado até você clicar em Iniciar.")
        self.yb_queue_running_label.setObjectName("muted")
        toggle_row.addWidget(self.yb_queue_toggle_btn)
        toggle_row.addWidget(self.yb_queue_running_label, 1)
        list_layout.addLayout(toggle_row)

        buttons_row = QHBoxLayout()
        import_btn = QPushButton("Importar vídeos…")
        import_btn.clicked.connect(self._import_youtube_browser_queue_videos)
        remove_btn = QPushButton("Remover selecionado")
        remove_btn.clicked.connect(self._remove_youtube_browser_queue_selected)
        send_now_btn = QPushButton("Enviar selecionado agora")
        send_now_btn.clicked.connect(self._send_selected_youtube_browser_queue_item_now)
        buttons_row.addWidget(import_btn)
        buttons_row.addWidget(remove_btn)
        buttons_row.addWidget(send_now_btn)
        buttons_row.addStretch()
        list_layout.addLayout(buttons_row)

        self.yb_queue_table = QTableWidget(0, 4)
        self.yb_queue_table.setHorizontalHeaderLabels(["Vídeo", "Canal", "Publicar em", "Status"])
        self.yb_queue_table.horizontalHeader().setStretchLastSection(False)
        self.yb_queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.yb_queue_table.setColumnWidth(1, 100)
        self.yb_queue_table.setColumnWidth(2, 230)
        self.yb_queue_table.setColumnWidth(3, 90)
        self.yb_queue_table.verticalHeader().setVisible(False)
        self.yb_queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.yb_queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        list_layout.addWidget(self.yb_queue_table, 5)

        self.yb_queue_log = QPlainTextEdit()
        self.yb_queue_log.setReadOnly(True)
        self.yb_queue_log.setMaximumHeight(90)
        list_layout.addWidget(self.yb_queue_log)
        layout.addWidget(list_box, 1)

        self.tabs.addTab(tab, "📺 YouTube (navegador)")
        self._reload_youtube_browser_accounts()
        self._reload_youtube_browser_queue_table()

    def _reload_youtube_browser_accounts(self) -> None:
        """Recarrega as contas a partir dos youtube_browser_cookies_<apelido>.txt da pasta."""
        contas = youtube_browser_upload.list_accounts()
        atual = self.yb_account_selector.currentText()
        self.yb_account_selector.blockSignals(True)
        self.yb_account_selector.clear()
        self.yb_account_selector.addItems(contas)
        if atual:
            self.yb_account_selector.setCurrentText(atual)
        self.yb_account_selector.blockSignals(False)
        self._refresh_youtube_browser_queue_status()

    def _youtube_browser_description_for(self, account: str) -> str:
        """Descrição padrão salva para esse canal ('' se nunca foi definida)."""
        descriptions = ai_srt.load_config().get("youtube_browser_descriptions") or {}
        return descriptions.get(account, "") if isinstance(descriptions, dict) else ""

    def _save_youtube_browser_queue_config(self) -> None:
        conta = self.yb_account_selector.currentText().strip()
        descriptions = ai_srt.load_config().get("youtube_browser_descriptions") or {}
        if not isinstance(descriptions, dict):
            descriptions = {}
        if conta:
            descriptions[conta] = self.yb_queue_description.toPlainText()
        ai_srt.save_config({
            "youtube_browser_visibility": self.yb_queue_visibility.currentData(),
            "youtube_browser_headless": self.yb_queue_headless.isChecked(),
            "youtube_browser_descriptions": descriptions,
        })

    def _refresh_youtube_browser_queue_status(self) -> None:
        conta = self.yb_account_selector.currentText().strip()
        self.yb_queue_description.blockSignals(True)
        self.yb_queue_description.setPlainText(self._youtube_browser_description_for(conta))
        self.yb_queue_description.blockSignals(False)
        if not conta:
            self.yb_queue_status.setText("Digite ou escolha um apelido de canal.")
        elif youtube_browser_upload.is_authorized(conta):
            self.yb_queue_status.setText(f"✅ Canal '{conta}' com cookies importados.")
        else:
            self.yb_queue_status.setText(
                f"Canal '{conta}' ainda sem cookies. Importe o cookies.txt "
                "exportado do navegador (já logado em studio.youtube.com).")

    def _import_youtube_browser_cookies(self) -> None:
        conta = self.yb_account_selector.currentText().strip()
        if not conta:
            QMessageBox.warning(self, APP_NAME, "Digite um apelido para o canal antes de importar.")
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Selecionar cookies.txt exportado do navegador", "", "Cookies (*.txt)")
        if not filename:
            return
        try:
            youtube_browser_upload.import_cookies(conta, Path(filename))
        except youtube_browser_upload.YoutubeBrowserUploadError as error:
            QMessageBox.critical(self, APP_NAME, f"Não deu para importar:\n\n{error}")
            return
        QMessageBox.information(self, APP_NAME, f"Cookies importados para o canal '{conta}'.")
        self._reload_youtube_browser_accounts()

    def _import_youtube_browser_queue_videos(self) -> None:
        conta_padrao = self.yb_account_selector.currentText().strip()
        files, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar vídeos para a fila", str(OUTPUT_DIR),
            "Vídeos (*.mp4 *.mov *.mkv *.avi *.webm)")
        if not files:
            return
        existentes = {str(item.path) for item in self._youtube_browser_queue}
        adicionados = 0
        for arquivo in files:
            if arquivo in existentes:
                continue
            self._youtube_browser_queue.append(youtube_browser_upload.QueueItem(Path(arquivo), conta_padrao))
            adicionados += 1
        if adicionados:
            self._save_youtube_browser_queue()
            self._reload_youtube_browser_queue_table()
            self.yb_queue_log.appendPlainText(f"➕ {adicionados} vídeo(s) adicionado(s) à fila.")

    def _remove_youtube_browser_queue_selected(self) -> None:
        row = self.yb_queue_table.currentRow()
        if row < 0 or row >= len(self._youtube_browser_queue):
            return
        nome = self._youtube_browser_queue[row].path.name
        del self._youtube_browser_queue[row]
        self._save_youtube_browser_queue()
        self._reload_youtube_browser_queue_table()
        self.yb_queue_log.appendPlainText(f"➖ {nome} removido da fila (sem enviar).")

    def _save_youtube_browser_queue(self) -> None:
        youtube_browser_upload.save_queue(self._youtube_browser_queue)

    def _reload_youtube_browser_queue_table(self) -> None:
        contas = youtube_browser_upload.list_accounts()
        self.yb_queue_table.setRowCount(0)
        for item in self._youtube_browser_queue:
            row = self.yb_queue_table.rowCount()
            self.yb_queue_table.insertRow(row)
            self.yb_queue_table.setRowHeight(row, 34)

            nome_item = QTableWidgetItem(item.path.name)
            nome_item.setFlags(nome_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.yb_queue_table.setItem(row, 0, nome_item)

            conta_combo = QComboBox()
            conta_combo.setEditable(True)
            conta_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            conta_combo.addItems(contas)
            if item.account:
                conta_combo.setCurrentText(item.account)
            elif contas:
                item.account = contas[0]
                conta_combo.setCurrentText(item.account)
            conta_combo.currentTextChanged.connect(
                lambda texto, widget=conta_combo: self._on_youtube_browser_queue_account_changed(widget, texto))
            self.yb_queue_table.setCellWidget(row, 1, conta_combo)

            # Sem agendamento nesta aba (vai tudo direto, com a visibilidade
            # escolhida no topo) — mesma decisão já tomada na aba TikTok.
            item.publish_at = None
            imediato_label = QTableWidgetItem("Imediato")
            imediato_label.setFlags(imediato_label.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.yb_queue_table.setItem(row, 2, imediato_label)

            status_item = QTableWidgetItem("Aguardando" if self._youtube_browser_queue_running else "Pausado")
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.yb_queue_table.setItem(row, 3, status_item)
        self._save_youtube_browser_queue()

    def _on_youtube_browser_queue_account_changed(self, widget: QComboBox, texto: str) -> None:
        """Acha a linha pelo próprio widget (índices mudam quando a fila encolhe)."""
        for row in range(self.yb_queue_table.rowCount()):
            if self.yb_queue_table.cellWidget(row, 1) is widget:
                if row < len(self._youtube_browser_queue):
                    self._youtube_browser_queue[row].account = texto
                    self._save_youtube_browser_queue()
                break

    def _toggle_youtube_browser_queue_running(self) -> None:
        self._youtube_browser_queue_running = not self._youtube_browser_queue_running
        if self._youtube_browser_queue_running:
            self.yb_queue_toggle_btn.setText("⏸ Parar envios")
            self.yb_queue_running_label.setText(
                "Enviando — a fila processa em ordem enquanto estiver ligada.")
            self._process_youtube_browser_queue()
        else:
            self.yb_queue_toggle_btn.setText("▶ Iniciar envios")
            self.yb_queue_running_label.setText(
                "Parado — nada será enviado até você clicar em Iniciar.")
        for row in range(len(self._youtube_browser_queue)):
            atual = self.yb_queue_table.item(row, 3)
            if atual and atual.text() in ("Enviando…", "Falhou"):
                continue
            self._set_youtube_browser_queue_row_status(
                row, "Aguardando" if self._youtube_browser_queue_running else "Pausado")

    def _check_youtube_browser_queue_schedule(self) -> None:
        if not self._youtube_browser_queue_running or self._youtube_browser_queue_busy or not self._youtube_browser_queue:
            return
        self._process_youtube_browser_queue()

    def _send_selected_youtube_browser_queue_item_now(self) -> None:
        row = self.yb_queue_table.currentRow()
        if row < 0 or row >= len(self._youtube_browser_queue):
            QMessageBox.information(self, APP_NAME, "Selecione um vídeo da fila primeiro.")
            return
        if self._youtube_browser_queue_busy:
            QMessageBox.information(self, APP_NAME, "Já há um envio em andamento.")
            return
        item = self._youtube_browser_queue[row]
        if not youtube_browser_upload.is_authorized(item.account):
            QMessageBox.warning(
                self, APP_NAME, f"Importe os cookies do canal '{item.account}' antes de enviar.")
            return
        self._process_youtube_browser_queue(force_index=row)

    def _process_youtube_browser_queue(self, force_index: int | None = None) -> None:
        if self._youtube_browser_queue_busy:
            return
        if not self._youtube_browser_queue_running and force_index is None:
            return
        self._youtube_browser_queue_busy = True
        self._send_next_due_youtube_browser_queue_item(force_index=force_index)

    def _send_next_due_youtube_browser_queue_item(self, force_index: int | None = None) -> None:
        if force_index is not None:
            index = force_index
        else:
            if not self._youtube_browser_queue_running:
                self._youtube_browser_queue_busy = False
                return
            # Pula quem já falhou: sem isso, o timer da fila reenviava o
            # mesmo vídeo a cada 30s pra sempre (ex.: cookies expiraram no
            # meio do envio), abrindo um upload novo do zero e deixando
            # rascunhos duplicados no Studio a cada tentativa. "Enviar
            # selecionado agora" ainda tenta de novo, de propósito.
            index = next(
                (i for i, item in enumerate(self._youtube_browser_queue)
                 if youtube_browser_upload.is_authorized(item.account)
                 and (self.yb_queue_table.item(i, 3) is None
                      or self.yb_queue_table.item(i, 3).text() != "Falhou")),
                None)
        if index is None or index >= len(self._youtube_browser_queue):
            self._youtube_browser_queue_busy = False
            return
        item = self._youtube_browser_queue[index]
        if not item.path.exists():
            self.yb_queue_log.appendPlainText(
                f"⚠️ {item.path.name} não existe mais no disco; removendo da fila.")
            del self._youtube_browser_queue[index]
            self._save_youtube_browser_queue()
            self._reload_youtube_browser_queue_table()
            self._send_next_due_youtube_browser_queue_item()
            return
        self._set_youtube_browser_queue_row_status(index, "Enviando…")
        aviso_agendamento = f", agendado para {item.publish_at:%d/%m %H:%M}" if item.publish_at else ""
        self.yb_queue_log.appendPlainText(
            f"📤 Enviando {item.path.name} (canal '{item.account}'{aviso_agendamento})…")
        worker = YoutubeBrowserUploadWorker(
            item.path, item.path.stem, self._youtube_browser_description_for(item.account), item.account,
            self.yb_queue_visibility.currentData(), item.publish_at,
            self.yb_queue_headless.isChecked(), self)
        worker.log.connect(lambda msg: self.yb_queue_log.appendPlainText(f"   {msg}"))
        worker.done.connect(lambda ok, msg, idx=index: self._on_youtube_browser_queue_item_done(ok, msg, idx))
        worker.finished.connect(worker.deleteLater)
        self._youtube_browser_queue_worker = worker
        worker.start()

    def _set_youtube_browser_queue_row_status(self, row: int, texto: str) -> None:
        item = self.yb_queue_table.item(row, 3)
        if item:
            item.setText(texto)

    def _on_youtube_browser_queue_item_done(self, ok: bool, message: str, index: int) -> None:
        self.yb_queue_log.appendPlainText(("✅ " if ok else "⚠️ ") + message)
        if ok:
            if index < len(self._youtube_browser_queue):
                del self._youtube_browser_queue[index]
                self._save_youtube_browser_queue()
                self._reload_youtube_browser_queue_table()
            self._send_next_due_youtube_browser_queue_item()
        else:
            self._set_youtube_browser_queue_row_status(index, "Falhou")
            self._youtube_browser_queue_busy = False

    # ──────────────────────────────────────────────────────────────
    #  Aba TikTok — fila de publicação (biblioteca não-oficial)
    # ──────────────────────────────────────────────────────────────

    def _build_tiktok_tab(self) -> None:
        """Fila de vídeos para publicar no TikTok, com várias contas.

        Sem API oficial acessível, o envio usa a biblioteca não-oficial
        `tiktok-uploader` (tiktok_upload.py): ela controla um navegador de
        verdade autenticado com os cookies de uma sessão já logada. Por isso
        não existe aqui um botão "Conectar" que abre login — em vez disso, o
        usuário loga manualmente em tiktok.com no navegador, exporta os
        cookies com uma extensão e importa o arquivo nesta aba.

        Fora esse detalhe de autenticação, a fila funciona exatamente como a
        do YouTube: importa vídeos, escolhe a conta de cada um e roda em
        ordem enquanto o botão "Iniciar envios" estiver ligado.
        """
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(10)

        title = QLabel("Fila de publicação no TikTok")
        title.setObjectName("title")
        subtitle = QLabel(
            "Importe os vídeos prontos, escolha a conta de cada um e clique "
            "em Iniciar — a fila envia em ordem enquanto estiver ligada.")
        subtitle.setObjectName("muted")
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        top_row = QHBoxLayout()
        top_row.setSpacing(14)

        conn_box = QGroupBox("Conta do TikTok")
        conn_form = QFormLayout(conn_box)
        self.tt_account_selector = QComboBox()
        self.tt_account_selector.setEditable(True)
        self.tt_account_selector.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.tt_account_selector.currentTextChanged.connect(self._refresh_tiktok_queue_status)
        conn_form.addRow("Apelido", self.tt_account_selector)
        self.tt_queue_status = QLabel()
        self.tt_queue_status.setWordWrap(True)
        conn_form.addRow(self.tt_queue_status)
        self.tt_queue_import_cookies_btn = QPushButton("Importar cookies.txt…")
        self.tt_queue_import_cookies_btn.setToolTip(
            "Logue em tiktok.com no navegador, exporte os cookies com uma "
            "extensão (ex.: 'Get cookies.txt') e escolha o arquivo aqui.")
        self.tt_queue_import_cookies_btn.clicked.connect(self._import_tiktok_cookies)
        conn_form.addRow(self.tt_queue_import_cookies_btn)
        top_row.addWidget(conn_box, 1)

        config = ai_srt.load_config()
        settings_box = QGroupBox("Configuração de envio")
        settings_form = QFormLayout(settings_box)
        self.tt_queue_visibility = QComboBox()
        self.tt_queue_visibility.addItem("Todos", "everyone")
        self.tt_queue_visibility.addItem("Amigos", "friends")
        self.tt_queue_visibility.addItem("Só eu", "only_you")
        saved_visibility = config.get("tiktok_visibility", tiktok_upload.DEFAULT_VISIBILITY)
        index = self.tt_queue_visibility.findData(saved_visibility)
        self.tt_queue_visibility.setCurrentIndex(index if index >= 0 else 0)
        self.tt_queue_visibility.currentIndexChanged.connect(self._save_tiktok_queue_config)
        settings_form.addRow("Visibilidade", self.tt_queue_visibility)

        self.tt_queue_headless = QCheckBox("Rodar o navegador escondido (headless)")
        self.tt_queue_headless.setChecked(bool(config.get("tiktok_headless", False)))
        self.tt_queue_headless.setToolTip(
            "O TikTok detecta e bloqueia automação com mais facilidade nesse "
            "modo. Deixe desmarcado a menos que precise mesmo.")
        self.tt_queue_headless.toggled.connect(self._save_tiktok_queue_config)
        settings_form.addRow(self.tt_queue_headless)

        # Uma legenda padrão por conta, igual à descrição padrão do YouTube.
        self.tt_queue_description = QPlainTextEdit()
        self.tt_queue_description.setMaximumHeight(60)
        self.tt_queue_description.setPlaceholderText(
            "Legenda padrão desta conta, usada nos vídeos da fila (opcional)")
        self.tt_queue_description.textChanged.connect(self._save_tiktok_queue_config)
        settings_form.addRow("Legenda\n(desta conta)", self.tt_queue_description)
        top_row.addWidget(settings_box, 1)
        layout.addLayout(top_row)

        note = QLabel(
            "Biblioteca não-oficial: exige `pip install tiktok-uploader` e "
            "depois `playwright install` (baixa o navegador usado no envio). "
            "Sem login programático — cada conta precisa de um "
            "tiktok_cookies_<apelido>.txt importado nesta aba. Agendamento "
            "nativo desativado por enquanto (quebrado nesta versão da "
            "biblioteca) — todo vídeo vai publicado na hora.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        list_box = QGroupBox("Vídeos na fila")
        list_layout = QVBoxLayout(list_box)

        toggle_row = QHBoxLayout()
        self.tt_queue_toggle_btn = QPushButton("▶ Iniciar envios")
        self.tt_queue_toggle_btn.setObjectName("primary")
        self.tt_queue_toggle_btn.clicked.connect(self._toggle_tiktok_queue_running)
        self.tt_queue_running_label = QLabel("Parado — nada será enviado até você clicar em Iniciar.")
        self.tt_queue_running_label.setObjectName("muted")
        toggle_row.addWidget(self.tt_queue_toggle_btn)
        toggle_row.addWidget(self.tt_queue_running_label, 1)
        list_layout.addLayout(toggle_row)

        buttons_row = QHBoxLayout()
        import_btn = QPushButton("Importar vídeos…")
        import_btn.clicked.connect(self._import_tiktok_queue_videos)
        remove_btn = QPushButton("Remover selecionado")
        remove_btn.clicked.connect(self._remove_tiktok_queue_selected)
        send_now_btn = QPushButton("Enviar selecionado agora")
        send_now_btn.clicked.connect(self._send_selected_tiktok_queue_item_now)
        buttons_row.addWidget(import_btn)
        buttons_row.addWidget(remove_btn)
        buttons_row.addWidget(send_now_btn)
        buttons_row.addStretch()
        list_layout.addLayout(buttons_row)

        self.tt_queue_table = QTableWidget(0, 4)
        self.tt_queue_table.setHorizontalHeaderLabels(["Vídeo", "Conta", "Publicar em", "Status"])
        self.tt_queue_table.horizontalHeader().setStretchLastSection(False)
        self.tt_queue_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tt_queue_table.setColumnWidth(1, 100)
        self.tt_queue_table.setColumnWidth(2, 230)
        self.tt_queue_table.setColumnWidth(3, 90)
        self.tt_queue_table.verticalHeader().setVisible(False)
        self.tt_queue_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.tt_queue_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        list_layout.addWidget(self.tt_queue_table, 5)

        self.tt_queue_log = QPlainTextEdit()
        self.tt_queue_log.setReadOnly(True)
        self.tt_queue_log.setMaximumHeight(90)
        list_layout.addWidget(self.tt_queue_log)
        layout.addWidget(list_box, 1)

        self.tabs.addTab(tab, "🎵 TikTok")
        self._reload_tiktok_accounts()
        self._reload_tiktok_queue_table()

    def _reload_tiktok_accounts(self) -> None:
        """Recarrega as contas a partir dos tiktok_cookies_<apelido>.txt da pasta."""
        contas = tiktok_upload.list_accounts()
        atual = self.tt_account_selector.currentText()
        self.tt_account_selector.blockSignals(True)
        self.tt_account_selector.clear()
        self.tt_account_selector.addItems(contas)
        if atual:
            self.tt_account_selector.setCurrentText(atual)
        self.tt_account_selector.blockSignals(False)
        self._refresh_tiktok_queue_status()

    def _tiktok_description_for(self, account: str) -> str:
        """Legenda padrão salva para essa conta ('' se nunca foi definida)."""
        descriptions = ai_srt.load_config().get("tiktok_descriptions") or {}
        return descriptions.get(account, "") if isinstance(descriptions, dict) else ""

    def _tiktok_full_description(self, item: tiktok_upload.QueueItem) -> str:
        """Título do vídeo (nome do arquivo) + a legenda padrão da conta (as #).

        O TikTok só tem um campo de texto no post — sem título separado como
        o YouTube —, então o título entra na frente da legenda salva para a
        conta, cada um numa linha.
        """
        titulo = item.path.stem
        legenda = self._tiktok_description_for(item.account)
        return f"{titulo}\n\n{legenda}" if legenda else titulo

    def _save_tiktok_queue_config(self) -> None:
        conta = self.tt_account_selector.currentText().strip()
        descriptions = ai_srt.load_config().get("tiktok_descriptions") or {}
        if not isinstance(descriptions, dict):
            descriptions = {}
        if conta:
            descriptions[conta] = self.tt_queue_description.toPlainText()
        ai_srt.save_config({
            "tiktok_visibility": self.tt_queue_visibility.currentData(),
            "tiktok_headless": self.tt_queue_headless.isChecked(),
            "tiktok_descriptions": descriptions,
        })

    def _refresh_tiktok_queue_status(self) -> None:
        conta = self.tt_account_selector.currentText().strip()
        self.tt_queue_description.blockSignals(True)
        self.tt_queue_description.setPlainText(self._tiktok_description_for(conta))
        self.tt_queue_description.blockSignals(False)
        if not conta:
            self.tt_queue_status.setText("Digite ou escolha um apelido de conta.")
        elif tiktok_upload.is_authorized(conta):
            self.tt_queue_status.setText(f"✅ Conta '{conta}' com cookies importados.")
        else:
            self.tt_queue_status.setText(
                f"Conta '{conta}' ainda sem cookies. Importe o cookies.txt "
                "exportado do navegador (já logado em tiktok.com).")

    def _import_tiktok_cookies(self) -> None:
        conta = self.tt_account_selector.currentText().strip()
        if not conta:
            QMessageBox.warning(self, APP_NAME, "Digite um apelido para a conta antes de importar.")
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Selecionar cookies.txt exportado do navegador", "", "Cookies (*.txt)")
        if not filename:
            return
        try:
            tiktok_upload.import_cookies(conta, Path(filename))
        except tiktok_upload.TikTokUploadError as error:
            QMessageBox.critical(self, APP_NAME, f"Não deu para importar:\n\n{error}")
            return
        QMessageBox.information(self, APP_NAME, f"Cookies importados para a conta '{conta}'.")
        self._reload_tiktok_accounts()

    def _import_tiktok_queue_videos(self) -> None:
        conta_padrao = self.tt_account_selector.currentText().strip()
        files, _ = QFileDialog.getOpenFileNames(
            self, "Selecionar vídeos para a fila", str(OUTPUT_DIR),
            "Vídeos (*.mp4 *.mov *.mkv *.avi *.webm)")
        if not files:
            return
        existentes = {str(item.path) for item in self._tiktok_queue}
        adicionados = 0
        for arquivo in files:
            if arquivo in existentes:
                continue
            self._tiktok_queue.append(tiktok_upload.QueueItem(Path(arquivo), conta_padrao))
            adicionados += 1
        if adicionados:
            self._save_tiktok_queue()
            self._reload_tiktok_queue_table()
            self.tt_queue_log.appendPlainText(f"➕ {adicionados} vídeo(s) adicionado(s) à fila.")

    def _remove_tiktok_queue_selected(self) -> None:
        row = self.tt_queue_table.currentRow()
        if row < 0 or row >= len(self._tiktok_queue):
            return
        nome = self._tiktok_queue[row].path.name
        del self._tiktok_queue[row]
        self._save_tiktok_queue()
        self._reload_tiktok_queue_table()
        self.tt_queue_log.appendPlainText(f"➖ {nome} removido da fila (sem enviar).")

    def _save_tiktok_queue(self) -> None:
        tiktok_upload.save_queue(self._tiktok_queue)

    def _reload_tiktok_queue_table(self) -> None:
        contas = tiktok_upload.list_accounts()
        self.tt_queue_table.setRowCount(0)
        for item in self._tiktok_queue:
            row = self.tt_queue_table.rowCount()
            self.tt_queue_table.insertRow(row)
            self.tt_queue_table.setRowHeight(row, 34)

            nome_item = QTableWidgetItem(item.path.name)
            nome_item.setFlags(nome_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.tt_queue_table.setItem(row, 0, nome_item)

            conta_combo = QComboBox()
            conta_combo.setEditable(True)
            conta_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            conta_combo.addItems(contas)
            if item.account:
                conta_combo.setCurrentText(item.account)
            elif contas:
                item.account = contas[0]
                conta_combo.setCurrentText(item.account)
            conta_combo.currentTextChanged.connect(
                lambda texto, widget=conta_combo: self._on_tiktok_queue_account_changed(widget, texto))
            self.tt_queue_table.setCellWidget(row, 1, conta_combo)

            # Agendamento nativo desativado por enquanto: a biblioteca clica num
            # elemento por um ID gerado pelo próprio TikTok (ex.: "tux-1") que
            # muda de lugar entre uma tela e outra, e o clique trava/expira. Até
            # a biblioteca corrigir isso, todo item vai imediato ao ser enviado.
            item.publish_at = None
            imediato_label = QTableWidgetItem("Imediato")
            imediato_label.setFlags(imediato_label.flags() & ~Qt.ItemFlag.ItemIsEditable)
            imediato_label.setToolTip(
                "Agendamento desativado por enquanto: o agendamento nativo do "
                "TikTok está quebrado nesta versão da biblioteca "
                "tiktok-uploader (trava tentando clicar no botão de agendar).")
            self.tt_queue_table.setItem(row, 2, imediato_label)

            status_item = QTableWidgetItem("Aguardando" if self._tiktok_queue_running else "Pausado")
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.tt_queue_table.setItem(row, 3, status_item)
        self._save_tiktok_queue()

    def _on_tiktok_queue_account_changed(self, widget: QComboBox, texto: str) -> None:
        """Acha a linha pelo próprio widget (índices mudam quando a fila encolhe)."""
        for row in range(self.tt_queue_table.rowCount()):
            if self.tt_queue_table.cellWidget(row, 1) is widget:
                if row < len(self._tiktok_queue):
                    self._tiktok_queue[row].account = texto
                    self._save_tiktok_queue()
                break

    def _toggle_tiktok_queue_running(self) -> None:
        self._tiktok_queue_running = not self._tiktok_queue_running
        if self._tiktok_queue_running:
            self.tt_queue_toggle_btn.setText("⏸ Parar envios")
            self.tt_queue_running_label.setText(
                "Enviando — a fila processa em ordem enquanto estiver ligada.")
            self._process_tiktok_queue()
        else:
            self.tt_queue_toggle_btn.setText("▶ Iniciar envios")
            self.tt_queue_running_label.setText(
                "Parado — nada será enviado até você clicar em Iniciar.")
        for row in range(len(self._tiktok_queue)):
            atual = self.tt_queue_table.item(row, 3)
            if atual and atual.text() in ("Enviando…", "Falhou"):
                continue
            self._set_tiktok_queue_row_status(
                row, "Aguardando" if self._tiktok_queue_running else "Pausado")

    def _check_tiktok_queue_schedule(self) -> None:
        if not self._tiktok_queue_running or self._tiktok_queue_busy or not self._tiktok_queue:
            return
        self._process_tiktok_queue()

    def _send_selected_tiktok_queue_item_now(self) -> None:
        row = self.tt_queue_table.currentRow()
        if row < 0 or row >= len(self._tiktok_queue):
            QMessageBox.information(self, APP_NAME, "Selecione um vídeo da fila primeiro.")
            return
        if self._tiktok_queue_busy:
            QMessageBox.information(self, APP_NAME, "Já há um envio em andamento.")
            return
        item = self._tiktok_queue[row]
        if not tiktok_upload.is_authorized(item.account):
            QMessageBox.warning(
                self, APP_NAME, f"Importe os cookies da conta '{item.account}' antes de enviar.")
            return
        self._process_tiktok_queue(force_index=row)

    def _process_tiktok_queue(self, force_index: int | None = None) -> None:
        if self._tiktok_queue_busy:
            return
        if not self._tiktok_queue_running and force_index is None:
            return
        self._tiktok_queue_busy = True
        self._send_next_due_tiktok_queue_item(force_index=force_index)

    def _send_next_due_tiktok_queue_item(self, force_index: int | None = None) -> None:
        if force_index is not None:
            index = force_index
        else:
            if not self._tiktok_queue_running:
                self._tiktok_queue_busy = False
                return
            # Pula quem já falhou, senão o timer da fila reenvia o mesmo
            # vídeo a cada 30s pra sempre. "Enviar selecionado agora" ainda
            # tenta de novo, de propósito.
            index = next(
                (i for i, item in enumerate(self._tiktok_queue)
                 if tiktok_upload.is_authorized(item.account)
                 and (self.tt_queue_table.item(i, 3) is None
                      or self.tt_queue_table.item(i, 3).text() != "Falhou")),
                None)
        if index is None or index >= len(self._tiktok_queue):
            self._tiktok_queue_busy = False
            return
        item = self._tiktok_queue[index]
        if not item.path.exists():
            self.tt_queue_log.appendPlainText(
                f"⚠️ {item.path.name} não existe mais no disco; removendo da fila.")
            del self._tiktok_queue[index]
            self._save_tiktok_queue()
            self._reload_tiktok_queue_table()
            self._send_next_due_tiktok_queue_item()
            return
        self._set_tiktok_queue_row_status(index, "Enviando…")
        aviso_agendamento = f", agendado para {item.publish_at:%d/%m %H:%M}" if item.publish_at else ""
        self.tt_queue_log.appendPlainText(
            f"📤 Enviando {item.path.name} (conta '{item.account}'{aviso_agendamento})…")
        worker = TikTokUploadWorker(
            item.path, self._tiktok_full_description(item), item.account,
            self.tt_queue_visibility.currentData(), item.publish_at,
            self.tt_queue_headless.isChecked(), self)
        worker.done.connect(lambda ok, msg, idx=index: self._on_tiktok_queue_item_done(ok, msg, idx))
        worker.finished.connect(worker.deleteLater)
        self._tiktok_queue_worker = worker
        worker.start()

    def _set_tiktok_queue_row_status(self, row: int, texto: str) -> None:
        item = self.tt_queue_table.item(row, 3)
        if item:
            item.setText(texto)

    def _on_tiktok_queue_item_done(self, ok: bool, message: str, index: int) -> None:
        self.tt_queue_log.appendPlainText(("✅ " if ok else "⚠️ ") + message)
        if ok:
            if index < len(self._tiktok_queue):
                del self._tiktok_queue[index]
                self._save_tiktok_queue()
                self._reload_tiktok_queue_table()
            self._send_next_due_tiktok_queue_item()
        else:
            self._set_tiktok_queue_row_status(index, "Falhou")
            self._tiktok_queue_busy = False

    # ──────────────────────────────────────────────────────────────
    #  Aba CSV — processamento em lote
    # ──────────────────────────────────────────────────────────────

    def _build_csv_tab(self) -> None:
        """Constrói a interface da aba de processamento CSV."""
        tab_csv = QWidget()
        layout = QHBoxLayout(tab_csv)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(20)

        # Painel CENTRAL: preview VLC + formato
        center_col = QVBoxLayout()
        csv_title = QLabel("Processamento em Lote")
        csv_title.setObjectName("title")
        csv_sub = QLabel("Selecione um trecho abaixo para ver a prévia e escolher o formato.")
        csv_sub.setObjectName("muted")
        center_col.addWidget(csv_title)
        center_col.addWidget(csv_sub)

        self.csv_video_frame = QFrame()
        self.csv_video_frame.setMinimumSize(600, 360)
        self.csv_video_frame.setStyleSheet("background: #10131a; border-radius: 12px;")
        self.csv_video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.csv_video_frame.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        # Label temporário para testar se o frame está visível
        test_label = QLabel("Clique em um trecho para ver a prévia")
        test_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        test_label.setStyleSheet("color: #8890a8; font-size: 12px;")
        frame_layout = QVBoxLayout(self.csv_video_frame)
        frame_layout.addWidget(test_label)
        center_col.addWidget(self.csv_video_frame, 1)
        self.csv_video_frame.windowHandle()

        # Timeline/slider
        self.csv_timeline = QSlider(Qt.Orientation.Horizontal)
        self.csv_timeline.setRange(0, 0)
        self.csv_timeline.setObjectName("timeline")
        self.csv_timeline.sliderMoved.connect(lambda v: self._csv_vlc_seek(v))
        self.csv_timeline.sliderPressed.connect(lambda: self._csv_vlc_seek(self.csv_timeline.value()))
        center_col.addWidget(self.csv_timeline)

        # Controles
        csv_controls = QHBoxLayout()
        csv_controls.setSpacing(10)
        self.csv_play_button = QPushButton("▶")
        self.csv_play_button.setObjectName("controlButton")
        self.csv_play_button.setFixedSize(42, 42)
        self.csv_play_button.clicked.connect(self._csv_toggle_play)
        self.csv_time_label = QLabel("00:00 / 00:00")
        self.csv_time_label.setObjectName("timeDisplay")
        self.csv_time_label.setMinimumWidth(130)
        csv_controls.addWidget(self.csv_play_button)
        csv_controls.addWidget(self.csv_time_label)
        csv_controls.addStretch()
        center_col.addLayout(csv_controls)

        center_panel = QWidget()
        center_panel.setLayout(center_col)
        layout.addWidget(center_panel, 1)

        # Painel DIREITO: lista de trechos + formato
        right_panel = QVBoxLayout()
        right_panel.setSpacing(10)

        # Título
        trechos_label = QLabel("Trechos")
        trechos_label.setObjectName("muted")
        right_panel.addWidget(trechos_label)

        # Lista de trechos (pequena)
        self.csv_moments_list = QTableWidget(0, 1)
        self.csv_moments_list.setHorizontalHeaderLabels([""])
        self.csv_moments_list.horizontalHeader().setVisible(False)
        self.csv_moments_list.itemSelectionChanged.connect(self._csv_on_moment_selected)
        self.csv_moments_list.setMinimumHeight(120)
        right_panel.addWidget(self.csv_moments_list, 0)

        # Tira o trecho selecionado da lista (não mexe no CSV em disco).
        self.csv_remove_moment_btn = QPushButton("🗑 Tirar este corte")
        self.csv_remove_moment_btn.setEnabled(False)
        self.csv_remove_moment_btn.clicked.connect(self._csv_remove_selected_moment)
        right_panel.addWidget(self.csv_remove_moment_btn, 0)

        # Formato do trecho selecionado
        format_select_box = QGroupBox("Formato deste trecho")
        format_select_form = QFormLayout(format_select_box)
        self.csv_clip_format = QComboBox()
        self.csv_clip_format.addItem("(usar padrão)", "")
        self.csv_clip_format.addItem("Estender (9:16 preencher)", "estender")
        self.csv_clip_format.addItem("Transparente (9:16 fundo desfocado)", "transparente")
        self.csv_clip_format.addItem("Imagem fixa (9:16 imagem + corte)", "imagem")
        self.csv_clip_format.addItem("Original / longo (16:9)", "original")
        self.csv_clip_format.currentIndexChanged.connect(self._csv_save_clip_format)
        format_select_form.addRow("Formato", self.csv_clip_format)
        self.csv_clip_format_label = QLabel("Nenhum trecho")
        self.csv_clip_format_label.setObjectName("muted")
        self.csv_clip_format_label.setWordWrap(True)
        format_select_form.addRow(self.csv_clip_format_label)
        self.csv_clip_image_label = QLabel("")
        self.csv_clip_image_label.setObjectName("muted")
        self.csv_clip_image_label.setWordWrap(True)
        format_select_form.addRow(self.csv_clip_image_label)
        image_btn_row = QWidget()
        image_btn_layout = QHBoxLayout(image_btn_row); image_btn_layout.setContentsMargins(0, 0, 0, 0)
        self.csv_clip_image_btn = QPushButton("Escolher imagem")
        self.csv_clip_image_btn.clicked.connect(self._csv_pick_clip_image)
        self.csv_clip_image_clear = QPushButton("Remover")
        self.csv_clip_image_clear.clicked.connect(self._csv_clear_clip_image)
        image_btn_layout.addWidget(self.csv_clip_image_btn, 1)
        image_btn_layout.addWidget(self.csv_clip_image_clear)
        format_select_form.addRow(image_btn_row)
        right_panel.addWidget(format_select_box)

        # Formato padrão para todos
        default_format_box = QGroupBox("Aplicar a todos")
        default_format_form = QFormLayout(default_format_box)
        self.csv_default_format = QComboBox()
        self.csv_default_format.addItem("Estender (9:16 preencher)", "estender")
        self.csv_default_format.addItem("Transparente (9:16 fundo desfocado)", "transparente")
        self.csv_default_format.addItem("Imagem fixa (9:16 imagem + corte)", "imagem")
        self.csv_default_format.addItem("Original / longo (16:9)", "original")
        self.csv_default_format.setCurrentIndex(0)
        default_format_form.addRow("Formato", self.csv_default_format)
        self.csv_default_image_label = QLabel("Nenhuma imagem")
        self.csv_default_image_label.setObjectName("muted")
        self.csv_default_image_label.setWordWrap(True)
        default_image_row = QWidget()
        default_image_layout = QHBoxLayout(default_image_row); default_image_layout.setContentsMargins(0, 0, 0, 0)
        default_image_btn = QPushButton("Escolher imagem")
        default_image_btn.clicked.connect(self._csv_pick_default_image)
        default_image_layout.addWidget(self.csv_default_image_label, 1)
        default_image_layout.addWidget(default_image_btn)
        default_format_form.addRow("Imagem (p/ 'imagem fixa')", default_image_row)
        apply_all_btn = QPushButton("🎬 Aplicar a todos")
        apply_all_btn.clicked.connect(self._csv_apply_format_to_all)
        default_format_form.addRow(apply_all_btn)
        right_panel.addWidget(default_format_box)
        self._csv_default_image: Path | None = None

        # Resto das configurações
        panel = QVBoxLayout()
        panel.setSpacing(14)

        # Vídeo
        video_box = QGroupBox("Vídeo de origem")
        video_layout = QVBoxLayout(video_box)
        video_row = QHBoxLayout()
        csv_upload = QPushButton("Escolher arquivo")
        csv_upload.clicked.connect(self.pick_video)
        self.csv_video_label = QLabel("Nenhum vídeo selecionado")
        self.csv_video_label.setWordWrap(True)
        self.csv_video_label.setObjectName("selectedFile")
        video_row.addWidget(csv_upload)
        video_row.addWidget(self.csv_video_label, 1)
        video_layout.addLayout(video_row)
        panel.addWidget(video_box)

        # CSV
        csv_box = QGroupBox("Ficheiro CSV")
        csv_box_layout = QVBoxLayout(csv_box)
        csv_row = QHBoxLayout()
        csv_pick = QPushButton("Carregar CSV")
        csv_pick.clicked.connect(self._pick_csv)
        self.csv_label = QLabel("Nenhum CSV carregado")
        self.csv_label.setWordWrap(True)
        self.csv_label.setObjectName("selectedFile")
        csv_row.addWidget(csv_pick)
        csv_row.addWidget(self.csv_label, 1)
        csv_box_layout.addLayout(csv_row)
        csv_hint = QLabel(
            "Colunas (com cabeçalho): inicio, fim, titulo, subtitulo, formato, imagem.\n"
            "• subtitulo: chapéu do lower-third (linha verde acima do título)\n"
            "• formato: estender / transparente / imagem / original (vazio = padrão abaixo)\n"
            "• imagem: caminho do PNG/JPG, usado só quando formato=imagem"
        )
        csv_hint.setObjectName("muted")
        csv_hint.setWordWrap(True)
        csv_box_layout.addWidget(csv_hint)
        panel.addWidget(csv_box)

        # Cortes por IA: em vez de um CSV pronto, a IA lê a transcrição do vídeo
        # e devolve os cortes. Preenche a mesma tabela do CSV manual.
        ai_box = QGroupBox("Cortes automáticos com IA (DeepSeek)")
        ai_box_layout = QVBoxLayout(ai_box)
        self.csv_ai_btn = QPushButton("🤖 Gerar cortes do vídeo com IA")
        self.csv_ai_btn.clicked.connect(self._csv_generate_cuts)
        ai_box_layout.addWidget(self.csv_ai_btn)
        ai_hint = QLabel(
            "Usa a legenda .srt ao lado do vídeo; se não houver, transcreve com "
            "Whisper primeiro. A IA escolhe os trechos mais fortes e preenche a "
            "tabela abaixo — revise antes de cortar. Precisa da chave do DeepSeek "
            "(seção 4 da aba Edição).")
        ai_hint.setObjectName("muted")
        ai_hint.setWordWrap(True)
        ai_box_layout.addWidget(ai_hint)
        panel.addWidget(ai_box)

        # Texto
        overlay_box = QGroupBox("Texto")
        overlay_form = QFormLayout(overlay_box)
        text_hint = QLabel("O título de cada corte aparecerá na tela automaticamente")
        text_hint.setObjectName("muted")
        overlay_form.addRow(text_hint)
        self.csv_captions = QCheckBox("Gerar legendas com Whisper (sincronizadas por corte)")
        self.csv_captions.setChecked(True)
        overlay_form.addRow(self.csv_captions)
        self.csv_use_cg = QCheckBox(f"Usar tarja '{self.brand_name}' (rodapé)")
        self.csv_use_cg.setChecked(True if self.cg_icon_path else False)
        self.csv_use_cg.setEnabled(bool(self.cg_icon_path))
        overlay_form.addRow(self.csv_use_cg)
        panel.addWidget(overlay_box)

        # Preview
        preview_box = QGroupBox("Momentos detetados")
        preview_layout = QVBoxLayout(preview_box)
        self.csv_table = QTableWidget(0, 5)
        self.csv_table.setHorizontalHeaderLabels(["Início", "Fim", "Título", "Formato", "Subtítulo"])
        self.csv_table.setMinimumHeight(180)
        self.csv_table.setObjectName("csvTable")
        preview_layout.addWidget(self.csv_table)
        self.csv_row_count = QLabel("0 momentos")
        self.csv_row_count.setObjectName("muted")
        preview_layout.addWidget(self.csv_row_count)
        panel.addWidget(preview_box)

        # Botão processar
        self.csv_process_btn = QPushButton("✂️ Cortar todos os momentos")
        self.csv_process_btn.setObjectName("primary")
        self.csv_process_btn.clicked.connect(self._process_csv_batch)
        panel.addWidget(self.csv_process_btn)

        self.csv_cancel_btn = QPushButton("Cancelar")
        self.csv_cancel_btn.setObjectName("cancelButton")
        self.csv_cancel_btn.clicked.connect(self._cancel_csv_batch)
        self.csv_cancel_btn.setEnabled(False)
        panel.addWidget(self.csv_cancel_btn)

        self.csv_progress = QProgressBar()
        self.csv_progress.setRange(0, 1); self.csv_progress.setValue(0)
        self.csv_progress.setTextVisible(True)
        panel.addWidget(self.csv_progress)

        self.csv_log = QPlainTextEdit()
        self.csv_log.setReadOnly(True)
        self.csv_log.setMaximumBlockCount(200)
        self.csv_log.setMaximumHeight(95)
        panel.addWidget(self.csv_log)
        panel.addStretch()

        panel_content = QWidget()
        panel_content.setObjectName("settingsPanel")
        panel_content.setLayout(panel)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(390)
        scroll.setWidget(panel_content)
        layout.addWidget(scroll, 1)

        # Adiciona painel direito (trechos + formato)
        right_widget = QWidget()
        right_widget.setLayout(right_panel)
        right_widget.setMinimumWidth(320)
        right_widget.setMaximumWidth(380)
        layout.addWidget(right_widget, 0)

        self.tabs.addTab(tab_csv, "📊 CSV Lotes")

        # Armazena momentos carregados
        self._csv_moments: list[dict] = []
        self._csv_batch_index = 0

    def _pick_csv(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Carregar CSV", "", "CSV (*.csv)")
        if not filename:
            return
        csv_path = Path(filename)
        self.csv_label.setText(csv_path.name)
        moments = parse_csv_moments(csv_path)
        self._csv_moments = moments
        self._populate_csv_table(moments)
        self.csv_log.clear()
        self.csv_log.appendPlainText(f"✅ {len(moments)} momentos carregados de {csv_path.name}")

    # ──────────────────────────────────────────────────────────────
    #  Cortes automáticos com IA (a partir da transcrição do vídeo)
    # ──────────────────────────────────────────────────────────────

    def _csv_ai_busy(self, busy: bool) -> None:
        """Trava os botões enquanto a IA escolhe os cortes."""
        self.csv_ai_btn.setEnabled(not busy)
        self.csv_process_btn.setEnabled(not busy)
        self.csv_ai_btn.setText("Gerando cortes…" if busy else "🤖 Gerar cortes do vídeo com IA")

    def _csv_generate_cuts(self) -> None:
        """Ponto de entrada do botão: garante uma transcrição e chama a IA.

        Reaproveita a legenda .srt ao lado do vídeo; sem ela, transcreve o vídeo
        inteiro com Whisper antes de perguntar os cortes à IA.
        """
        if not self.video_path:
            QMessageBox.warning(self, APP_NAME, "Selecione um vídeo primeiro.")
            return
        if not self._ai_key():
            QMessageBox.warning(
                self, APP_NAME,
                "Informe a chave da API do DeepSeek na seção 4 da aba Edição "
                "(ou defina DEEPSEEK_API_KEY).")
            return

        self.csv_log.clear()
        legenda = find_video_subtitle(self.video_path)
        if legenda and srt_has_content(legenda):
            self.csv_log.appendPlainText(f"📄 Usando a legenda do vídeo: {legenda.name}")
            self._csv_run_cuts_ai(legenda)
            return

        whisper = whisper_path()
        if not whisper:
            QMessageBox.warning(
                self, APP_NAME,
                "O vídeo não tem legenda .srt e o Whisper não foi encontrado. "
                "Instale as dependências do README ou coloque um .srt ao lado do vídeo.")
            return

        self._csv_ai_busy(True)
        self.csv_log.appendPlainText("🎙️ Sem legenda ao lado do vídeo; transcrevendo o vídeo inteiro…")
        self._csv_cuts_wav = self.work_dir / "_csv_cuts_full.wav"
        self._csv_cuts_whisper = whisper
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.finished.connect(self._csv_cuts_extracted)
        self._csv_cuts_proc = proc
        proc.start("ffmpeg", ["-y", "-i", str(self.video_path), "-vn", "-ac", "1",
                              "-ar", "16000", str(self._csv_cuts_wav)])

    def _csv_cuts_extracted(self, code: int, status: QProcess.ExitStatus) -> None:
        if code != 0 or not self._csv_cuts_wav.exists():
            self._csv_ai_busy(False)
            QMessageBox.critical(self, APP_NAME, "Não foi possível extrair o áudio do vídeo.")
            return
        self.csv_log.appendPlainText(
            f"🎙️ Transcrevendo com Whisper (modelo {self.whisper_model.currentData()})… "
            "pode demorar em vídeos longos.")
        command = [self._csv_cuts_whisper, str(self._csv_cuts_wav),
                   "--model", self.whisper_model.currentData(), "--language", "Portuguese",
                   "--task", "transcribe", "--output_format", "srt",
                   "--output_dir", str(self.work_dir)]
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.finished.connect(self._csv_cuts_after_whisper)
        self._csv_cuts_proc = proc
        proc.start(command[0], command[1:])

    def _csv_cuts_after_whisper(self, code: int, status: QProcess.ExitStatus) -> None:
        srt = self._csv_cuts_wav.with_suffix(".srt")
        if code != 0 or not srt_has_content(srt):
            self._csv_ai_busy(False)
            QMessageBox.critical(
                self, APP_NAME,
                "O Whisper não gerou uma transcrição utilizável para este vídeo.")
            return
        self.csv_log.appendPlainText("✅ Transcrição pronta; pedindo os cortes à IA…")
        self._csv_run_cuts_ai(srt)

    def _csv_run_cuts_ai(self, srt_path: Path) -> None:
        """Monta a transcrição com tempos e dispara a escolha dos cortes."""
        transcript = transcript_with_timestamps(srt_path)
        if not transcript.strip():
            self._csv_ai_busy(False)
            QMessageBox.warning(self, APP_NAME, "A transcrição do vídeo veio vazia.")
            return
        self._csv_ai_busy(True)
        self.csv_log.appendPlainText("🤖 A IA está escolhendo os cortes…")
        # A IA também escolhe a trilha de cada corte, a partir dos rótulos da
        # pasta de trilhas (a mesma da aba Edição). Sem trilhas na pasta, a lista
        # vai vazia e o campo "musica" volta em branco.
        climas = [rotulo for rotulo, _ in trilhas.list_tracks(self.music_dir())]
        worker = CutsSuggestWorker(
            transcript, self._ai_key(), self.ai_model.currentText().strip(),
            climas, self)
        worker.done.connect(self._csv_apply_cuts)
        worker.finished.connect(worker.deleteLater)
        self._csv_cuts_worker = worker
        worker.start()

    def _csv_apply_cuts(self, ok: bool, message: str, cortes: list) -> None:
        """Recebe os cortes da IA e preenche a tabela do CSV."""
        self._csv_ai_busy(False)
        self.csv_log.appendPlainText(("✅ " if ok else "⚠️ ") + message)
        if not ok:
            QMessageBox.warning(self, APP_NAME, message)
            return
        duracao = self._live_probe_duration(self.video_path)
        moments = cuts_to_moments(cortes, duracao, max_cut_duration=MAX_CUT_SECONDS)
        fora = len(cortes) - len(moments)
        if fora > 0:
            self.csv_log.appendPlainText(
                f"✂️ {fora} corte(s) longo(s) demais (acima de 2min30) descartado(s).")
        if not moments:
            QMessageBox.information(
                self, APP_NAME,
                "A IA não trouxe nenhum corte aproveitável neste vídeo.\n\n"
                "Pode ser que o vídeo só tenha momentos rápidos, sem contexto "
                "para sustentar um short.")
            return
        self._csv_moments = moments
        self.csv_label.setText(f"{len(moments)} cortes sugeridos pela IA")
        self._populate_csv_table(moments)
        for m in moments:
            if m.get("comentario"):
                self.csv_log.appendPlainText(
                    f"   • {m['label']} ({as_time(m['start_s'])}–{as_time(m['end_s'])}): {m['comentario']}")
        QMessageBox.information(
            self, APP_NAME,
            f"{len(moments)} cortes sugeridos e carregados na tabela.\n\n"
            "Revise os trechos e clique em “Cortar todos os momentos” quando quiser exportar.")

    def _populate_csv_table(self, moments: list[dict]) -> None:
        self.csv_table.setRowCount(len(moments))
        self.csv_moments_list.setRowCount(len(moments))
        for i, m in enumerate(moments):
            # Inicializa _format_override se não existir
            if "_format_override" not in m:
                m["_format_override"] = ""
            # Tabela de preview: usa o formato efetivo (override do painel > CSV).
            effective = m.get("_format_override") or m.get("formato") or ""
            fmt = effective or "(padrão)"
            if effective == "imagem":
                img = m.get("image_path")
                if not (img and Path(img).exists()):
                    fmt = "imagem ⚠ sem arquivo"
            self.csv_table.setItem(i, 0, QTableWidgetItem(as_time(m["start_s"])))
            self.csv_table.setItem(i, 1, QTableWidgetItem(as_time(m["end_s"])))
            self.csv_table.setItem(i, 2, QTableWidgetItem(m["label"]))
            self.csv_table.setItem(i, 3, QTableWidgetItem(fmt))
            self.csv_table.setItem(i, 4, QTableWidgetItem(m.get("subtitulo", "")))
            # Lista de momentos (clicável)
            item_text = f"{m['label']}\n{as_time(m['start_s'])} — {as_time(m['end_s'])}"
            if m.get("musica"):
                item_text += f"\n🎵 {m['musica']}"
            self.csv_moments_list.setItem(i, 0, QTableWidgetItem(item_text))
        self.csv_table.resizeColumnsToContents()
        self.csv_moments_list.resizeRowsToContents()
        self.csv_row_count.setText(f"{len(moments)} momentos")

    def _process_csv_batch(self) -> None:
        if not self.video_path:
            QMessageBox.warning(self, APP_NAME, "Selecione um vídeo primeiro.")
            return
        if not self._csv_moments:
            QMessageBox.warning(self, APP_NAME, "Carregue um CSV com momentos primeiro.")
            return
        if not command_exists("ffmpeg"):
            QMessageBox.critical(self, APP_NAME, "FFmpeg não foi encontrado.")
            return

        self._whisper_bin = whisper_path()
        self._csv_captions_on = self.csv_captions.isChecked()
        # Se o vídeo já tem legenda (ex.: baixada pelo yt-dlp), reaproveita-a e
        # dispensa o Whisper. Mas não quando ela é auto-gerada (YouTube): essas
        # "rolam" — a mesma fala reaparece em cues que se sobrepõem, crescendo
        # palavra por palavra — e nem a limpeza deixa boas; cada corte sairia com
        # falas repetidas na legenda. Mesma regra da aba Edição
        # (generate_captions): se for automática e o Whisper estiver disponível,
        # transcreve de novo em vez de reaproveitar.
        raw_srt = find_video_subtitle(self.video_path) if self._csv_captions_on else None
        self._csv_auto_caption = bool(raw_srt) and looks_like_auto_caption(parse_srt_segments(raw_srt))
        self._csv_source_srt = None if (self._csv_auto_caption and self._whisper_bin) else raw_srt
        self._csv_use_whisper = self._csv_captions_on and self._whisper_bin is not None
        if self._csv_captions_on and not self._csv_source_srt and not self._whisper_bin:
            QMessageBox.warning(self, APP_NAME, "Sem legenda pronta e Whisper não encontrado. Os cortes serão gerados sem legendas.")
        elif self._csv_auto_caption and self._csv_source_srt:
            QMessageBox.warning(
                self, APP_NAME,
                f"{raw_srt.name} parece uma legenda automática (repetições, marcadores "
                ">>). Vou limpar o que der, mas pode sair com falas repetidas — para "
                "ficar impecável, instale o Whisper (veja o README).")

        # Salva os cortes numa subpasta com o nome do perfil + nome do vídeo de origem.
        video_name = "".join(c for c in self.video_path.stem if c.isalnum() or c in " _-").strip()[:120] or "Cortes"
        profile_prefix = (self._current_profile or "sem_perfil")[:4]
        safe_dir = f"{profile_prefix} - {video_name}"
        self._csv_output_dir = OUTPUT_DIR / safe_dir
        self._csv_output_dir.mkdir(parents=True, exist_ok=True)
        total = len(self._csv_moments)
        self.csv_progress.setRange(0, total)
        self.csv_progress.setValue(0)
        self.csv_process_btn.setDisabled(True)
        self.csv_cancel_btn.setEnabled(True)
        self.csv_log.clear()
        if self._csv_source_srt:
            legend_note = f"legenda do vídeo: {self._csv_source_srt.name}"
        elif self._csv_use_whisper:
            legend_note = "com legendas Whisper"
        else:
            legend_note = "sem legendas automáticas"
        self.csv_log.appendPlainText(f"Iniciando corte de {total} momentos ({legend_note})...\n")
        if self._csv_auto_caption and not self._csv_source_srt:
            self.csv_log.appendPlainText(
                f"ℹ️ {raw_srt.name} parece uma legenda automática (repetições, marcadores "
                ">>); transcrevendo cada corte com Whisper para sair limpo.\n")

        self._csv_batch_index = 0
        self._csv_clip_srt = None
        self._csv_process_output = ""
        self._csv_total = total
        self._csv_errors: list[str] = []
        self._csv_cancelled = False
        self._process_next_csv()

    def _cancel_csv_batch(self) -> None:
        """Interrompe o lote de cortes em andamento na aba CSV."""
        self._csv_cancelled = True
        proc = getattr(self, "_csv_process", None)
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            try:
                proc.finished.disconnect()
            except (TypeError, RuntimeError):
                pass
            proc.kill()
            proc.waitForFinished(2000)
        # Também mata a geração de thumbnail em andamento, se o cancelamento
        # pegou o lote nessa fase (agora assíncrona, dá para clicar Cancelar ali).
        for thumb_proc in list(self._thumbnail_procs):
            if thumb_proc.state() != QProcess.ProcessState.NotRunning:
                thumb_proc.kill()
                thumb_proc.waitForFinished(2000)
        self.csv_process_btn.setDisabled(False)
        self.csv_cancel_btn.setEnabled(False)
        self.csv_progress.setRange(0, 1)
        self.csv_progress.setValue(0)
        self.csv_log.appendPlainText("\n⛔ Processamento cancelado.")

    def _csv_on_moment_selected(self) -> None:
        rows = self.csv_moments_list.selectionModel().selectedRows()
        if not rows:
            self._csv_selected_index = -1
            self.csv_remove_moment_btn.setEnabled(False)
            self.csv_clip_format_label.setText("Nenhum trecho selecionado")
            self.csv_clip_format.blockSignals(True)
            self.csv_clip_format.setCurrentIndex(0)
            self.csv_clip_format.blockSignals(False)
            self.csv_vlc_player.stop()
            return
        idx = rows[0].row()
        self._csv_selected_index = idx
        self.csv_remove_moment_btn.setEnabled(True)
        m = self._csv_moments[idx]
        # Carrega no VLC
        if self.video_path:
            self.csv_vlc_player.stop()
            hwnd = self.csv_video_frame.winId()
            if not hwnd:
                self.csv_clip_format_label.setText("❌ Erro: frame não tem handle")
                return

            self.csv_vlc_media = self.vlc_instance.media_new(str(self.video_path))
            self.csv_vlc_player.set_media(self.csv_vlc_media)
            self.csv_vlc_player.set_hwnd(int(hwnd))
            self.csv_vlc_player.set_rate(2.0)  # 2x de velocidade

            start_ms = int(m["start_s"] * 1000)
            self.csv_vlc_media.parse_with_options(vlc.MediaParseFlag.local, -1)

            # Toca para renderizar e depois vai pro início
            self.csv_vlc_player.play()
            QTimer.singleShot(600, lambda: self.csv_vlc_player.set_time(start_ms))
        # Atualiza o combobox de formato
        self.csv_clip_format_label.setText(f"{m['label']} ({as_time(m['start_s'])} — {as_time(m['end_s'])})")
        override = m.get("_format_override", "")
        self.csv_clip_format.blockSignals(True)
        idx_combo = self.csv_clip_format.findData(override)
        if idx_combo >= 0:
            self.csv_clip_format.setCurrentIndex(idx_combo)
        else:
            self.csv_clip_format.setCurrentIndex(0)
        self.csv_clip_format.blockSignals(False)

        # Mostra a imagem se formato for "imagem"
        self._csv_update_image_display()

    def _csv_remove_selected_moment(self) -> None:
        """Tira o trecho selecionado da lista de cortes (só na memória)."""
        if self.csv_cancel_btn.isEnabled():
            QMessageBox.warning(
                self, APP_NAME,
                "Não dá para tirar um corte enquanto o lote está sendo processado. "
                "Cancele o processamento primeiro.")
            return
        idx = self._csv_selected_index
        if not (0 <= idx < len(self._csv_moments)):
            return
        m = self._csv_moments[idx]
        if QMessageBox.question(
                self, APP_NAME,
                f"Tirar o corte \"{m['label']}\" "
                f"({as_time(m['start_s'])} — {as_time(m['end_s'])}) da lista?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self.csv_vlc_player.stop()
        del self._csv_moments[idx]
        self._populate_csv_table(self._csv_moments)
        # Seleciona um vizinho para o usuário seguir revisando sem reclicar.
        if self._csv_moments:
            proximo = min(idx, len(self._csv_moments) - 1)
            self.csv_moments_list.selectRow(proximo)
        else:
            self._csv_selected_index = -1
            self.csv_remove_moment_btn.setEnabled(False)
            self.csv_clip_format_label.setText("Nenhum trecho selecionado")

    def _csv_save_clip_format(self) -> None:
        if self._csv_selected_index < 0 or self._csv_selected_index >= len(self._csv_moments):
            return
        m = self._csv_moments[self._csv_selected_index]
        m["_format_override"] = self.csv_clip_format.currentData()
        self._csv_update_image_display()

    def _csv_update_image_display(self) -> None:
        """Mostra a imagem e habilita os botões quando o formato é 'imagem'."""
        has_clip = 0 <= self._csv_selected_index < len(self._csv_moments)
        m = self._csv_moments[self._csv_selected_index] if has_clip else None
        mode = (m.get("_format_override") or m.get("formato") or "") if m else ""
        is_image = mode == "imagem"
        # Os botões só fazem sentido para um trecho no formato "imagem".
        self.csv_clip_image_btn.setEnabled(has_clip and is_image)
        self.csv_clip_image_clear.setEnabled(has_clip and is_image and bool(m and m.get("image_path")))
        if not has_clip or not is_image:
            self.csv_clip_image_label.setText("")
            return
        image = m.get("image_path")
        if image and Path(image).exists():
            self.csv_clip_image_label.setText(f"📷 Imagem: {Path(image).name}")
        elif image:
            self.csv_clip_image_label.setText(f"⚠️ Imagem não encontrada: {Path(image).name}")
        else:
            self.csv_clip_image_label.setText("Nenhuma imagem escolhida para este trecho.")

    def _csv_pick_clip_image(self) -> None:
        """Escolhe a imagem do trecho selecionado (formato 'imagem fixa')."""
        if not (0 <= self._csv_selected_index < len(self._csv_moments)):
            return
        filename, _ = QFileDialog.getOpenFileName(
            self, "Selecionar imagem do topo", "", "Imagens (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not filename:
            return
        m = self._csv_moments[self._csv_selected_index]
        m["image_path"] = Path(filename)
        self._populate_csv_table(self._csv_moments)
        self.csv_moments_list.selectRow(self._csv_selected_index)
        self._csv_update_image_display()

    def _csv_clear_clip_image(self) -> None:
        if not (0 <= self._csv_selected_index < len(self._csv_moments)):
            return
        self._csv_moments[self._csv_selected_index]["image_path"] = None
        self._populate_csv_table(self._csv_moments)
        self.csv_moments_list.selectRow(self._csv_selected_index)
        self._csv_update_image_display()

    def _csv_pick_default_image(self) -> None:
        """Escolhe uma imagem para aplicar a todos os trechos com 'Aplicar a todos'."""
        filename, _ = QFileDialog.getOpenFileName(
            self, "Selecionar imagem do topo", "", "Imagens (*.png *.jpg *.jpeg *.webp *.bmp)")
        if not filename:
            return
        self._csv_default_image = Path(filename)
        self.csv_default_image_label.setText(self._csv_default_image.name)

    def _csv_apply_format_to_all(self) -> None:
        """Aplica o formato (e a imagem, se houver) selecionados a todos os momentos."""
        if not self._csv_moments:
            return
        selected_format = self.csv_default_format.currentData()
        for m in self._csv_moments:
            m["_format_override"] = selected_format
            # Se o formato for "imagem" e houver imagem escolhida, aplica a todos.
            if selected_format == "imagem" and self._csv_default_image:
                m["image_path"] = self._csv_default_image
        self._populate_csv_table(self._csv_moments)
        if self._csv_selected_index >= 0:
            self.csv_moments_list.selectRow(self._csv_selected_index)
        extra = ""
        if selected_format == "imagem" and self._csv_default_image:
            extra = f" (imagem: {self._csv_default_image.name})"
        self.csv_log.appendPlainText(
            f"✅ Formato '{self.csv_default_format.currentText()}'{extra} aplicado a todos os {len(self._csv_moments)} momentos.")
        self._csv_update_image_display()

    def _csv_vlc_seek(self, ms: int) -> None:
        self.csv_vlc_player.set_time(ms)

    def _csv_toggle_play(self) -> None:
        if self.csv_vlc_player.is_playing():
            self.csv_vlc_player.pause()
            self.csv_play_button.setText("▶")
        else:
            self.csv_vlc_player.play()
            self.csv_play_button.setText("❚❚")

    def _csv_poll_vlc(self) -> None:
        """Atualiza timeline e label de tempo do VLC CSV (polling 100ms)."""
        if self.csv_vlc_player.get_media() is None:
            return
        # Duração
        length = self.csv_vlc_player.get_length()
        if length > 0 and int(length) != int(self._csv_duration * 1000):
            self._csv_duration = length / 1000
            self.csv_timeline.setRange(0, length)
        # Posição
        pos = self.csv_vlc_player.get_time()
        if not self.csv_timeline.isSliderDown():
            self.csv_timeline.setValue(pos)
        self.csv_time_label.setText(f"{as_time(pos / 1000)} / {as_time(self._csv_duration)}")

    def _clip_mode(self, moment: dict) -> str:
        """Resolve o formato de um corte: override do usuário ou coluna CSV.

        Se pedir 'imagem' mas não houver arquivo válido, cai em 'estender'.
        """
        # Prioridade: _format_override (escolhido no painel) > formato (coluna CSV) > "estender"
        mode = moment.get("_format_override") or moment.get("formato") or "estender"
        if mode == "imagem":
            image = moment.get("image_path")
            if not (image and Path(image).exists()):
                self.csv_log.appendPlainText(
                    f"   ⚠️ Sem imagem válida para '{moment['label']}'; usando 'estender'."
                )
                return "estender"
        return mode

    def _process_next_csv(self) -> None:
        if getattr(self, "_csv_cancelled", False):
            return
        if self._csv_batch_index >= self._csv_total:
            self._csv_batch_done()
            return

        m = self._csv_moments[self._csv_batch_index]
        self._csv_clip_mode = self._clip_mode(m)
        self.csv_log.appendPlainText(
            f"[{self._csv_batch_index + 1}/{self._csv_total}] {m['label']} ({self._csv_clip_mode})"
        )
        self._csv_clip_srt = None

        # Fase 1a: reaproveita a legenda do vídeo (sem Whisper), se existir.
        if self._csv_captions_on and getattr(self, "_csv_source_srt", None):
            start, end = m["start_s"], m["end_s"]
            out = self.work_dir / f"_csv_clip_{self._csv_batch_index}.srt"
            if segments_to_srt(parse_srt_segments(self._csv_source_srt), start, end, out):
                shorten_srt_captions(out)
                self._csv_clip_srt = out
                self.csv_log.appendPlainText("   📄 Legenda do vídeo reaproveitada (sem Whisper).")
                self._csv_export_clip()
                return
            self.csv_log.appendPlainText("   ℹ️ Legenda do vídeo não cobre este trecho.")

        # Fase 1b: legendas via Whisper (se ligado e disponível).
        if self._csv_use_whisper:
            start, end = m["start_s"], m["end_s"]
            audio_path = self.work_dir / f"_csv_audio_{self._csv_batch_index}.wav"
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-ss", str(start), "-t", str(end - start),
                     "-i", str(self.video_path), "-vn", "-ac", "1", "-ar", "16000", str(audio_path)],
                    check=True, capture_output=True,
                )
            except subprocess.CalledProcessError:
                self.csv_log.appendPlainText("   ⚠️ Sem áudio no trecho; corte sem legenda.")
                self._csv_export_clip()
                return
            self.csv_log.appendPlainText(
                f"   🎙️ Transcrevendo com Whisper (modelo {self.whisper_model.currentData()})…")
            command = self.whisper_command(self._whisper_bin, audio_path, self.work_dir)
            self._csv_process = QProcess(self)
            self._csv_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self._csv_process.finished.connect(
                lambda code, status, idx=self._csv_batch_index: self._csv_after_whisper(code, status, idx)
            )
            self._csv_process.start(command[0], command[1:])
            return

        # Fallback legado: usa o texto da coluna 'legenda' se existir.
        legenda = self._csv_moments[self._csv_batch_index].get("legenda", "")
        if legenda:
            srt_path = self.work_dir / f"_csv_clip_{self._csv_batch_index}.srt"
            build_srt_for_clip(0.0, m["end_s"] - m["start_s"], legenda, srt_path)
            self._csv_clip_srt = srt_path
        self._csv_export_clip()

    def _csv_after_whisper(self, code: int, status: QProcess.ExitStatus, idx: int) -> None:
        srt_path = self.work_dir / f"_csv_audio_{idx}.srt"
        if code == 0 and status == QProcess.ExitStatus.NormalExit and srt_has_content(srt_path):
            def ready() -> None:
                # Encurtar depois da revisão: a IA lida melhor com frases inteiras.
                shorten_srt_captions(srt_path)
                self._csv_clip_srt = srt_path
                self._csv_export_clip()

            skip = self.ai_review_skip_reason(srt_path)
            if skip:
                self.csv_log.appendPlainText("   ⚠️ " + skip)
            if self.ai_review_enabled() and not skip:
                self.csv_log.appendPlainText("   🤖 Revisando a legenda com IA…")
                titulo = self._csv_moments[self._csv_batch_index].get("label", "")
                started = self._start_ai_review(
                    srt_path, titulo,
                    lambda ok, msg, *_: (self.csv_log.appendPlainText(f"   {'✅' if ok else '⚠️'} {msg}"),
                                         ready()),
                )
                if not started:
                    ready()
            else:
                ready()
            return
        self.csv_log.appendPlainText("   ⚠️ Sem fala detectada (ou Whisper falhou); seguindo sem legenda.")
        self._csv_export_clip()

    def _csv_export_clip(self) -> None:
        m = self._csv_moments[self._csv_batch_index]
        start, end, titulo = m["start_s"], m["end_s"], m["label"]
        mode = self._csv_clip_mode
        safe_label = "".join(c for c in titulo if c.isalnum() or c in " _-").strip()[:80]
        output = self._csv_output_dir / f"{safe_label}.mp4"

        has_image = mode == "imagem"

        # Gera o lower-third (se ativado) antes de tudo, para saber se afasta a legenda.
        cg_path = None
        if self.csv_use_cg.isChecked() and self.cg_icon_path:
            cg_path = create_lower_third(titulo, m.get("subtitulo", ""), self.work_dir,
                                         self.cg_icon_path, colors=self.brand_colors)
            if not cg_path:
                self.csv_log.appendPlainText("   ⚠️ Erro ao gerar lower-third; continuando sem overlay.")

        # Calcula o índice de entrada do FFmpeg
        image_input = 1
        cg_input = image_input + (1 if has_image else 0)

        def build_chain(captions: bool, post_input: int | None = None) -> str:
            chain = build_clip_filter(mode, has_image, image_input=image_input)
            current = "base"

            # Aplicar legendas se existir (afastadas do rodapé quando há lower-third)
            if captions and srt_has_content(self._csv_clip_srt):
                margin_v = LT_CAPTION_MARGIN_V if cg_path else 60
                style = ("FontName=Montserrat,FontSize=18,Bold=-1,"
                         "PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,"
                         f"BorderStyle=1,Outline=2.5,Shadow=0,Alignment=2,MarginV={margin_v}")
                chain += (f";[{current}]subtitles=filename='{filter_path(self._csv_clip_srt)}':"
                          f"fontsdir='{filter_path(FONT_DIR)}':force_style='{style}'[captioned]")
                current = "captioned"

            # Lower-third acima da faixa coberta pela interface do YouTube
            if cg_path:
                chain += (f";[{current}][{cg_input}:v]"
                          f"overlay=x=0:y=main_h-{LT_HEIGHT + LT_BOTTOM_MARGIN}[with_cg]")
                current = "with_cg"
            elif titulo.strip():
                # Sem lower-third: título simples com drawtext (mais acima, fonte menor).
                font = "C\\:/Windows/Fonts/arialbd.ttf"
                text = format_title_for_video(titulo)
                chain += f";[{current}]drawtext=fontfile='{font}':text='{text}':x=(w-text_w)/2:y=30:fontsize=40:fontcolor=white:borderw=3:bordercolor=black[text]"
                current = "text"

            chain, current = self._watermark_chain(chain, current)
            chain, current = self._with_post_frame(chain, current, post_input)
            return chain + f";[{current}]format=yuv420p[outv]"

        inputs = ["-i", str(self.video_path)]
        n_inputs = 1
        if has_image:
            inputs += ["-loop", "1", "-i", str(m["image_path"])]; n_inputs += 1
        if cg_path:
            inputs += ["-loop", "1", "-i", str(cg_path)]; n_inputs += 1

        # 1) Frame de post: primeiro frame do corte, com GC e sem legenda escrita.
        thumb = thumbnail_path(output)
        # Se há imagem no CSV mas o mode não é "imagem", precisa incluir na thumb mesmo assim
        thumb_has_image = has_image
        thumb_image_input = 1
        thumb_cg_input = thumb_image_input + (1 if thumb_has_image else 0)
        thumb_inputs = inputs.copy()

        if not has_image and m.get("image_path"):
            thumb_image = m["image_path"]
            if isinstance(thumb_image, (str, Path)) and Path(thumb_image).exists():
                # Monta inputs com a imagem para a thumbnail
                thumb_inputs = ["-i", str(self.video_path), "-loop", "1", "-i", str(thumb_image)]
                thumb_has_image = True
                thumb_image_input = 1
                thumb_cg_input = 2
                if cg_path:
                    thumb_inputs += ["-loop", "1", "-i", str(cg_path)]

        # Constrói o filter_complex para a thumbnail com a imagem correta
        def build_thumb_chain(captions: bool) -> str:
            chain = build_clip_filter(mode if has_image else ("imagem" if thumb_has_image else mode),
                                      thumb_has_image, image_input=thumb_image_input)
            current = "base"
            if captions and srt_has_content(self._csv_clip_srt):
                margin_v = LT_CAPTION_MARGIN_V if cg_path else 60
                style = ("FontName=Montserrat,FontSize=18,Bold=-1,"
                         "PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,"
                         f"BorderStyle=1,Outline=2.5,Shadow=0,Alignment=2,MarginV={margin_v}")
                chain += (f";[{current}]subtitles=filename='{filter_path(self._csv_clip_srt)}':"
                          f"fontsdir='{filter_path(FONT_DIR)}':force_style='{style}'[captioned]")
                current = "captioned"
            if cg_path:
                chain += (f";[{current}][{thumb_cg_input}:v]"
                          f"overlay=x=0:y=main_h-{LT_HEIGHT + LT_BOTTOM_MARGIN}[with_cg]")
                current = "with_cg"
            elif titulo.strip():
                font = "C\\:/Windows/Fonts/arialbd.ttf"
                text = format_title_for_video(titulo)
                chain += f";[{current}]drawtext=fontfile='{font}':text='{text}':x=(w-text_w)/2:y=30:fontsize=40:fontcolor=white:borderw=3:bordercolor=black[text]"
                current = "text"
            chain, current = self._watermark_chain(chain, current)
            return chain + f";[{current}]format=yuv420p[outv]"

        thumb_command = (
            ["ffmpeg", "-y", "-ss", str(start)] + thumb_inputs
            + ["-filter_complex", build_thumb_chain(captions=False),
               "-map", "[outv]", "-frames:v", "1", "-q:v", "2", str(thumb)]
        )

        def after_thumbnail(post_frame: Path | None) -> None:
            if getattr(self, "_csv_cancelled", False):
                return
            self._csv_post_frame = post_frame
            # 2) Esse frame vira a capa: entra por cima do frame 0 do vídeo.
            post_input = None
            if post_frame:
                inputs.extend(["-loop", "1", "-i", str(thumb)])
                post_input = n_inputs

            # Trilha de fundo escolhida pela IA para este corte (respeita a opção
            # "Misturar trilha" da aba Edição). Entra como última entrada; quando há
            # trilha, censura e mixagem vivem juntas no filter_complex.
            censor_af = self.apply_censorship(self._csv_clip_srt, self._csv_log_indent)
            video_chain = build_chain(captions=True, post_input=post_input)
            music_track = self._csv_music_track(m)
            music_in = None
            if music_track and music_track.exists():
                inputs.extend(["-i", str(music_track)])
                music_in = n_inputs + (1 if post_input is not None else 0)
                self._csv_log_indent(f"🎵 Trilha: {m.get('musica')} → {music_track.name}")
            audio_chain, amap = self._audio_chain(video_chain, "0:a", censor_af, music_in, end - start)

            command = ["ffmpeg", "-y", "-ss", str(start), "-t", str(end - start)] + inputs
            command += ["-filter_complex", audio_chain, "-map", "[outv]", "-map", amap]
            if amap == "0:a?":
                command += self._audio_censor_args(censor_af)
            command += [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest",
                str(output),
            ]

            self.csv_log.appendPlainText("   🎬 Convertendo o vídeo…")
            self._csv_process = QProcess(self)
            self._csv_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self._csv_process.readyReadStandardOutput.connect(self._csv_read_process_output)
            self._csv_process.finished.connect(
                lambda code, status: self._csv_on_finished(
                    code, status, output, titulo,
                    m.get("musica", ""), m.get("subtitulo", "")))
            self._csv_process_output = ""
            self._csv_process.start(command[0], command[1:])

        self._render_thumbnail_async(thumb_command, thumb, after_thumbnail)

    def _csv_log_indent(self, message: str) -> None:
        """Log do lote, alinhado com as demais mensagens do corte."""
        self.csv_log.appendPlainText(f"   {message}")

    def _csv_read_process_output(self) -> None:
        """Captura saída do FFmpeg durante o processamento."""
        data = bytes(self._csv_process.readAllStandardOutput())
        try:
            output = data.decode("utf-8")
        except UnicodeDecodeError:
            output = data.decode("cp1252", errors="replace")
        self._csv_process_output += output

    def _csv_on_finished(self, code: int, status: QProcess.ExitStatus, output: Path, label: str,
                          musica: str = "", resumo: str = "") -> None:
        self._csv_batch_index += 1
        self.csv_progress.setValue(self._csv_batch_index)
        if code != 0 or status != QProcess.ExitStatus.NormalExit:
            self._csv_errors.append(label)
            self.csv_log.appendPlainText(f"   ❌ Falha ao cortar: {label}")
            # Mostra as últimas linhas do erro
            error_lines = self._csv_process_output.split("\n")
            error_lines = [l.strip() for l in error_lines if l.strip() and ("error" in l.lower() or "failed" in l.lower())]
            if error_lines:
                for line in error_lines[-3:]:  # Últimas 3 linhas com erro
                    self.csv_log.appendPlainText(f"      {line[:100]}")
        else:
            self.csv_log.appendPlainText(f"   ✅ OK → {output.name}")
            # Salva a transcrição (.srt pt-br) ao lado do vídeo, se houver.
            if srt_has_content(self._csv_clip_srt):
                try:
                    shutil.copyfile(self._csv_clip_srt, output.with_suffix(".srt"))
                except OSError:
                    pass
                # Legenda de Instagram (.txt ao lado do vídeo), gerada pela IA.
                self.deliver_reels_caption(
                    self._csv_clip_srt, output, self._csv_log_indent)
            log_video(label, musica, resumo)
            post = getattr(self, "_csv_post_frame", None)
            if post:
                self.csv_log.appendPlainText(f"   🖼️ Post/capa → {post.name}")
            else:
                self.csv_log.appendPlainText("   ⚠️ Não deu para gerar o frame de post.")
            self._csv_post_frame = None
        self._process_next_csv()

    def _csv_batch_done(self) -> None:
        self.csv_process_btn.setDisabled(False)
        self.csv_cancel_btn.setEnabled(False)
        errors = len(self._csv_errors)
        ok = self._csv_total - errors
        out_dir = getattr(self, "_csv_output_dir", OUTPUT_DIR)
        self.csv_log.appendPlainText(f"\n🎉 Concluído! {ok} cortes salvos em:\n{out_dir}")
        if errors:
            self.csv_log.appendPlainText(f"⚠️ {errors} falhas: {', '.join(self._csv_errors)}")
        QMessageBox.information(self, APP_NAME, f"Processamento concluído!\n\n✅ {ok} vídeos exportados\n❌ {errors} falhas\n\nPasta: {out_dir}")

    # ──────────────────────────────────────────────────────────────
    #  Aba Live — gravação + transcrição em tempo real + cortes
    # ──────────────────────────────────────────────────────────────

    def _build_live_tab(self) -> None:
        tab_live = QWidget()
        layout = QHBoxLayout(tab_live)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(20)

        # ── Coluna esquerda: gravação + preview ──
        left_col = QVBoxLayout()
        live_title = QLabel("Cortes de Live")
        live_title.setObjectName("title")
        live_sub = QLabel("Grava a live desde o início; marque o período, corte e a legenda é gerada na hora.")
        live_sub.setObjectName("muted")
        left_col.addWidget(live_title)
        left_col.addWidget(live_sub)

        url_row = QHBoxLayout()
        self.live_url_input = QLineEdit()
        self.live_url_input.setObjectName("urlInput")
        self.live_url_input.setPlaceholderText("URL da live (YouTube)")
        self.live_url_input.setClearButtonEnabled(True)
        self.live_start_btn = QPushButton("⏺ Gravar live")
        self.live_start_btn.setObjectName("downloadButton")
        self.live_start_btn.clicked.connect(self._live_start_stop)
        url_row.addWidget(self.live_url_input, 1)
        url_row.addWidget(self.live_start_btn)
        left_col.addLayout(url_row)

        # Offset de início (para live com duração anterior)
        offset_row = QHBoxLayout()
        offset_label = QLabel("Começar a partir de (HH:MM:SS):")
        self.live_seek_offset = QLineEdit()
        self.live_seek_offset.setPlaceholderText("ex: 1:30:45 ou deixar em branco")
        self.live_seek_offset.setMaximumWidth(150)
        self.live_seek_offset.setToolTip("Útil para lives com duração anterior — começa do offset especificado")
        offset_row.addWidget(offset_label)
        offset_row.addWidget(self.live_seek_offset)
        offset_row.addStretch()
        left_col.addLayout(offset_row)

        # Cookies do navegador. Numa live com --live-from-start o yt-dlp baixa
        # os fragmentos DASH "manifestless" do YouTube, que hoje exigem um GVS
        # PO Token — que o yt-dlp não gera sozinho. Sem ele os fragmentos dão
        # HTTP 403 e são pulados (buracos na gravação). A saída sem instalar
        # nada é ler os cookies de um navegador logado no YouTube e usar o
        # player_client "tv" (que só vem sem DRM quando há cookies).
        cookies_row = QHBoxLayout()
        cookies_label = QLabel("Cookies do navegador:")
        self.live_cookies_browser = QComboBox()
        self.live_cookies_browser.addItem("Nenhum (pode dar 403)", "")
        for _nome, _valor in (("Chrome", "chrome"), ("Edge", "edge"),
                              ("Firefox", "firefox"), ("Brave", "brave"),
                              ("Opera", "opera"), ("Vivaldi", "vivaldi"),
                              ("Chromium", "chromium")):
            self.live_cookies_browser.addItem(_nome, _valor)
        self.live_cookies_browser.setCurrentIndex(1)  # Chrome por padrão
        self.live_cookies_browser.setToolTip(
            "Lê os cookies de uma sessão já logada no YouTube nesse navegador.\n"
            "No Windows os navegadores Chromium (Chrome, Brave, Edge…) travam o\n"
            "banco de cookies enquanto estão abertos e o yt-dlp não consegue lê-lo\n"
            "(erro 'Could not copy Chrome cookie database'). Nesse caso use o botão\n"
            "'Arquivo cookies.txt…' ao lado, que não sofre com esse bloqueio.")
        # Alternativa à leitura direta do navegador: um cookies.txt exportado
        # (mesma ideia da aba TikTok). É à prova do bloqueio do banco no Windows
        # e, quando definido, tem prioridade sobre o navegador do dropdown.
        self.live_cookies_file_btn = QPushButton("Arquivo cookies.txt…")
        self.live_cookies_file_btn.setToolTip(
            "Use um cookies.txt exportado do navegador (extensão 'Get cookies.txt', "
            "logado no youtube.com). Não sofre com o bloqueio do banco de cookies "
            "no Windows. Tem prioridade sobre o navegador selecionado ao lado.")
        self.live_cookies_file_btn.clicked.connect(self._live_pick_cookies_file)
        self.live_cookies_file_label = QLabel("")
        self.live_cookies_file_label.setObjectName("muted")
        cookies_row.addWidget(cookies_label)
        cookies_row.addWidget(self.live_cookies_browser)
        cookies_row.addWidget(self.live_cookies_file_btn)
        cookies_row.addWidget(self.live_cookies_file_label, 1)
        cookies_row.addStretch()
        left_col.addLayout(cookies_row)

        self.live_status = QLabel("Cole a URL e clique em Gravar.")
        self.live_status.setObjectName("muted")
        left_col.addWidget(self.live_status)

        self.live_video_frame = QFrame()
        self.live_video_frame.setMinimumSize(600, 340)
        self.live_video_frame.setStyleSheet("background: #10131a; border-radius: 12px;")
        self.live_video_frame.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.live_video_frame.setAttribute(Qt.WidgetAttribute.WA_DontCreateNativeAncestors)
        left_col.addWidget(self.live_video_frame, 1)
        self.live_video_frame.windowHandle()

        self.live_timeline = QSlider(Qt.Orientation.Horizontal)
        self.live_timeline.setRange(0, 0)
        self.live_timeline.setObjectName("timeline")
        self.live_timeline.sliderMoved.connect(lambda v: self.live_vlc_player.set_time(v))
        self.live_timeline.sliderPressed.connect(lambda: self.live_vlc_player.set_time(self.live_timeline.value()))
        left_col.addWidget(self.live_timeline)

        controls = QHBoxLayout()
        controls.setSpacing(10)
        self.live_play_button = QPushButton("▶")
        self.live_play_button.setObjectName("controlButton")
        self.live_play_button.setFixedSize(42, 42)
        self.live_play_button.clicked.connect(self._live_toggle_play)
        live_back = QPushButton("⏪")
        live_back.setObjectName("controlButton")
        live_back.setFixedSize(42, 42)
        live_back.setToolTip("Voltar 10s")
        live_back.clicked.connect(lambda: self.live_vlc_player.set_time(max(0, self.live_vlc_player.get_time() - 10000)))
        refresh_btn = QPushButton("🔄 Atualizar")
        refresh_btn.setToolTip("Recarrega o arquivo da gravação (pega o trecho mais novo) mantendo a posição")
        refresh_btn.clicked.connect(lambda: self._live_load_preview(keep_position=True))
        golive_btn = QPushButton("⏩ Ao vivo")
        golive_btn.setToolTip("Vai para o ponto mais recente da gravação")
        golive_btn.clicked.connect(self._live_go_live)
        self.live_time_label = QLabel("00:00 / 00:00")
        self.live_time_label.setObjectName("timeDisplay")
        self.live_time_label.setMinimumWidth(130)
        controls.addWidget(self.live_play_button)
        controls.addWidget(live_back)
        controls.addWidget(refresh_btn)
        controls.addWidget(golive_btn)
        controls.addWidget(self.live_time_label)
        controls.addStretch()

        self.live_speed_buttons: list[QPushButton] = []
        for rate, label in ((1.0, "1×"), (1.5, "1.5×"), (2.0, "2×")):
            btn = QPushButton(label)
            btn.setObjectName("speedActive" if rate == 1.0 else "speedButton")
            btn.setFixedSize(44 if len(label) > 2 else 38, 28)
            btn.clicked.connect(lambda checked=False, r=rate: self._live_set_speed(r))
            self.live_speed_buttons.append(btn)
            controls.addWidget(btn)
        left_col.addLayout(controls)

        self.live_log = QPlainTextEdit()
        self.live_log.setReadOnly(True)
        self.live_log.setMaximumBlockCount(300)
        self.live_log.setMaximumHeight(90)
        left_col.addWidget(self.live_log)

        left_panel = QWidget()
        left_panel.setLayout(left_col)
        layout.addWidget(left_panel, 3)

        # ── Coluna direita: transcrição + novo corte ──
        right_col = QVBoxLayout()
        right_col.setSpacing(10)

        live_hint = QLabel("Assista a gravação, marque início e fim com 📍, escolha o formato e exporte. "
                           "A legenda do trecho é gerada com Whisper durante a exportação.")
        live_hint.setObjectName("muted")
        live_hint.setWordWrap(True)
        right_col.addWidget(live_hint)

        cut_box = QGroupBox("Novo corte")
        cut_form = QFormLayout(cut_box)
        start_row = QWidget()
        start_layout = QHBoxLayout(start_row); start_layout.setContentsMargins(0, 0, 0, 0)
        self.live_cut_start = TimestampInput()
        mark_start = QPushButton("📍 agora")
        mark_start.setToolTip("Usa a posição atual do player")
        mark_start.clicked.connect(lambda: self.live_cut_start.setValue(self.live_vlc_player.get_time() / 1000))
        start_layout.addWidget(self.live_cut_start, 1); start_layout.addWidget(mark_start)
        end_row = QWidget()
        end_layout = QHBoxLayout(end_row); end_layout.setContentsMargins(0, 0, 0, 0)
        self.live_cut_end = TimestampInput()
        mark_end = QPushButton("📍 agora")
        mark_end.setToolTip("Usa a posição atual do player")
        mark_end.clicked.connect(lambda: self.live_cut_end.setValue(self.live_vlc_player.get_time() / 1000))
        end_layout.addWidget(self.live_cut_end, 1); end_layout.addWidget(mark_end)
        self.live_cut_title = QLineEdit()
        self.live_cut_title.setPlaceholderText("Título do corte (aparece na tela)")
        self.live_cut_format = QComboBox()
        self.live_cut_format.addItem("Estender (9:16 preencher)", "estender")
        self.live_cut_format.addItem("Transparente (9:16 fundo desfocado)", "transparente")
        self.live_cut_format.addItem("Imagem fixa (9:16 imagem + corte)", "imagem")
        self.live_cut_format.addItem("Original / longo (16:9)", "original")
        self.live_cut_format.currentIndexChanged.connect(
            lambda: self.live_image_row.setVisible(self.live_cut_format.currentData() == "imagem"))
        self.live_image_row = QWidget()
        live_img_layout = QHBoxLayout(self.live_image_row); live_img_layout.setContentsMargins(0, 0, 0, 0)
        self.live_image_label = QLabel("Nenhuma imagem")
        live_img_btn = QPushButton("Escolher")
        live_img_btn.clicked.connect(self._live_pick_image)
        live_img_layout.addWidget(self.live_image_label, 1); live_img_layout.addWidget(live_img_btn)
        cut_form.addRow("Início", start_row)
        cut_form.addRow("Fim", end_row)
        cut_form.addRow("Título", self.live_cut_title)
        cut_form.addRow("Formato", self.live_cut_format)
        cut_form.addRow("Imagem", self.live_image_row)
        self.live_image_row.setVisible(False)
        self.live_captions_check = QCheckBox("Gerar legendas (Whisper) do trecho")
        self.live_captions_check.setChecked(True)
        cut_form.addRow(self.live_captions_check)
        self.live_export_btn = QPushButton("✂️ Exportar corte")
        self.live_export_btn.setObjectName("primary")
        self.live_export_btn.clicked.connect(self._live_export_cut)
        cut_form.addRow(self.live_export_btn)
        self.live_cancel_btn = QPushButton("Cancelar")
        self.live_cancel_btn.setObjectName("cancelButton")
        self.live_cancel_btn.clicked.connect(self._live_cancel_cut)
        self.live_cancel_btn.setEnabled(False)
        cut_form.addRow(self.live_cancel_btn)
        self.live_cut_progress = QProgressBar()
        self.live_cut_progress.setRange(0, 1)
        self.live_cut_progress.setValue(0)
        self.live_cut_progress.setTextVisible(False)
        cut_form.addRow(self.live_cut_progress)
        self.live_cut_log = QPlainTextEdit()
        self.live_cut_log.setReadOnly(True)
        self.live_cut_log.setMaximumBlockCount(400)
        self.live_cut_log.setMaximumHeight(150)
        cut_form.addRow(self.live_cut_log)
        right_col.addWidget(cut_box)
        right_col.addStretch()

        right_panel = QWidget()
        right_panel.setLayout(right_col)
        right_panel.setMinimumWidth(340)
        right_panel.setMaximumWidth(430)
        layout.addWidget(right_panel, 2)

        self.tabs.addTab(tab_live, "🔴 Live")

    # ── Gravação ──

    def _live_start_stop(self) -> None:
        if self._live_recording:
            if self._live_process:
                self._live_process.kill()
            if self._live_audio_process:
                self._live_audio_process.kill()
            self.live_start_btn.setText("⏺ Gravar live")
            self.live_status.setText("Parando gravação…")
            return

        url = self.live_url_input.text().strip()
        if not url:
            QMessageBox.warning(self, APP_NAME, "Cole a URL da live primeiro.")
            return
        downloader = yt_dlp_path()
        if not downloader:
            QMessageBox.critical(self, APP_NAME, "yt-dlp não foi encontrado. Consulte o README.")
            return
        if not command_exists("ffmpeg"):
            QMessageBox.critical(self, APP_NAME, "FFmpeg não foi encontrado. Consulte o README.")
            return

        self._live_dir = OUTPUT_DIR / "Lives" / datetime.now().strftime("live_%Y%m%d_%H%M%S")
        self._live_dir.mkdir(parents=True, exist_ok=True)
        self._live_busy = False
        self._live_needs_remux = False
        self.live_log.clear()

        # Se offset foi especificado, log informativo
        offset_str = self.live_seek_offset.text().strip()
        if offset_str:
            self.live_log.appendPlainText(f"ℹ️ Offset de início: {offset_str} (você pode marcar na timeline depois)")
        self.live_log.appendPlainText("● Conectando e gravando…")

        # Com --live-from-start o yt-dlp baixa formatos em sequência: o áudio
        # só começaria quando o vídeo "terminasse" — o que numa live não
        # acontece. Por isso rodamos DOIS yt-dlp em paralelo: um só para o
        # vídeo e outro só para o áudio. Sem -N: cada fragmento é colado no
        # arquivo assim que baixa, mantendo os arquivos sempre legíveis.
        # --extractor-args: player_client=tv,web_safari. Numa live desde o
        #   início os fragmentos DASH do cliente "web" exigem um GVS PO Token
        #   (yt-dlp não gera) e dão HTTP 403; o cliente "tv" não precisa do
        #   token e, com os cookies do navegador (abaixo), vem sem DRM.
        # --cookies-from-browser: lê a sessão logada no YouTube — sem isso os
        #   fragmentos da live são recusados (403) e ficam buracos na gravação.
        # --retries/--fragment-retries: tenta reconectar em caso de falha.
        # -f: prioriza H.264 (mais estável em live) sobre AV1 (instável com
        # fragmentos perdidos). ATENÇÃO: o YouTube reporta o codec como
        # "avc1.*", não "h264" — por isso o filtro precisa ser vcodec^=avc1.
        # Com vcodec=h264 nada casava e o yt-dlp caía no fallback (b[ext=mp4] =
        # formato 18, 360p), gravando a live em qualidade baixíssima.
        base = [str(downloader), "--live-from-start", "--no-part", "--newline",
                 "--extractor-args", "youtube:player_client=tv,web_safari",
                 "--retries", "10", "--fragment-retries", "10"]
        # cookies.txt tem prioridade: não sofre com o bloqueio do banco de
        # cookies que os navegadores Chromium fazem no Windows (issue #7271).
        cookies_browser = self.live_cookies_browser.currentData()
        if self._live_cookies_file and self._live_cookies_file.exists():
            base += ["--cookies", str(self._live_cookies_file)]
            self.live_log.appendPlainText(f"🍪 Usando cookies.txt: {self._live_cookies_file.name}")
        elif cookies_browser:
            base += ["--cookies-from-browser", cookies_browser]
            self.live_log.appendPlainText(
                f"🍪 Lendo cookies do navegador ({cookies_browser}). Se der "
                "'Could not copy cookie database', feche o navegador ou use o "
                "botão 'Arquivo cookies.txt…'.")
        else:
            self.live_log.appendPlainText(
                "⚠️ Sem cookies: o YouTube pode recusar os fragmentos da live "
                "(HTTP 403). Escolha um navegador logado ou um cookies.txt.")
        video_cmd = base + ["-f", "bv*[vcodec^=avc1][ext=mp4]/bv*[ext=mp4]/bv*/b",
                            "-o", str(self._live_dir / "video.%(ext)s"), url]
        audio_cmd = base + ["-f", "ba[ext=m4a]/ba",
                            "-o", str(self._live_dir / "audio.%(ext)s"), url]

        self._live_process = QProcess(self)
        self._live_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._live_process.readyReadStandardOutput.connect(
            lambda: self._live_read_output(self._live_process, "vídeo"))
        self._live_process.finished.connect(self._live_on_ytdlp_finished)
        self._live_process.start(video_cmd[0], video_cmd[1:])

        self._live_audio_process = QProcess(self)
        self._live_audio_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._live_audio_process.readyReadStandardOutput.connect(
            lambda: self._live_read_output(self._live_audio_process, "áudio"))
        self._live_audio_process.finished.connect(self._live_on_audio_finished)
        self._live_audio_process.start(audio_cmd[0], audio_cmd[1:])

        self._live_recording = True
        self.live_start_btn.setText("⏹ Parar gravação")
        self.live_status.setText("● Conectando à live (vídeo + áudio)…")
        self.live_log.appendPlainText(f"Gravando em: {self._live_dir}")

    def _live_read_output(self, process: QProcess, tag: str) -> None:
        data = bytes(process.readAllStandardOutput())
        try:
            output = data.decode("utf-8")
        except UnicodeDecodeError:
            output = data.decode("cp1252", errors="replace")
        for line in output.splitlines():
            line = line.strip()
            # Filtra o spam de fragmentos; mantém o resto
            if line and "Fragment" not in line and "ETA" not in line:
                self.live_log.appendPlainText(f"[{tag}] {line}")

    def _live_on_ytdlp_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        self._live_recording = False
        self.live_start_btn.setText("⏺ Gravar live")
        self.live_status.setText("Gravação encerrada. Você ainda pode marcar períodos e cortar normalmente.")
        self.live_log.appendPlainText("Gravação de vídeo encerrada.")

    def _live_on_audio_finished(self, code: int, status: QProcess.ExitStatus) -> None:
        if code != 0 and self._live_recording:
            self.live_log.appendPlainText(
                "⚠️ Download de áudio separado terminou com erro — o áudio pode estar embutido no vídeo.")
        else:
            self.live_log.appendPlainText("Gravação de áudio encerrada.")

    def _live_files(self) -> tuple[Path | None, Path | None]:
        """Devolve (vídeo, áudio_separado). Áudio None quando o vídeo já tem áudio."""
        if not self._live_dir or not self._live_dir.exists():
            return None, None
        # Formato atual: downloads separados video.* e audio.*
        video = next((p for p in (self._live_dir / f"video.{ext}" for ext in ("mp4", "mkv", "webm", "ts"))
                      if p.exists()), None)
        audio = next((p for p in (self._live_dir / f"audio.{ext}" for ext in ("m4a", "webm", "mp4", "opus"))
                      if p.exists()), None)
        if video is not None:
            return video, audio
        # Compatibilidade com gravações antigas (live.* mesclado ou fragmentado)
        for name in ("live.mp4", "live.mkv", "live.webm", "live.ts"):
            merged = self._live_dir / name
            if merged.exists():
                return merged, None
        videos = sorted(self._live_dir.glob("live.f*.mp4"))
        audios = sorted(self._live_dir.glob("live.f*.m4a"))
        video = videos[0] if videos else (audios[0] if audios else None)
        audio = audios[0] if audios else None
        return video, audio

    # ── Preview ──

    def _live_load_preview(self, keep_position: bool = False, go_end: bool = False) -> None:
        video, audio = self._live_files()
        if video is None:
            QMessageBox.information(self, APP_NAME, "Ainda não há gravação para mostrar. Inicie a gravação primeiro.")
            return
        previous_ms = self.live_vlc_player.get_time() if keep_position else 0
        # Se já sabemos que o VLC não toca o áudio separado, vai direto pro remux
        if audio is not None and self._live_needs_remux:
            self._live_remux_goal = "end" if go_end else previous_ms
            self._live_start_preview_remux()
            return
        self.live_vlc_player.stop()
        self._live_duration = 0.0
        self.live_vlc_media = self.vlc_instance.media_new(str(video))
        if audio is not None:
            try:
                self.live_vlc_media.add_option(f":input-slave={audio.as_uri()}")
            except Exception:
                pass
        self.live_vlc_player.set_media(self.live_vlc_media)
        hwnd = self.live_video_frame.winId()
        if hwnd:
            self.live_vlc_player.set_hwnd(int(hwnd))
        self.live_vlc_player.play()
        self.live_play_button.setText("❚❚")
        self.live_vlc_media.parse_with_options(vlc.MediaParseFlag.local, -1)
        if audio is not None:
            # Tenta anexar o áudio pelo player e depois confere se funcionou
            try:
                slave_type = getattr(vlc.MediaSlaveType, "audio", 1)
                QTimer.singleShot(800, lambda: self.live_vlc_player.add_slave(slave_type, audio.as_uri(), True))
            except Exception:
                pass
            self._live_remux_goal = "end" if go_end else previous_ms
            QTimer.singleShot(3000, self._live_audio_fixup)
        if go_end:
            QTimer.singleShot(900, lambda: self.live_vlc_player.set_time(max(0, self.live_vlc_player.get_length() - 5000)))
        elif keep_position and previous_ms > 0:
            QTimer.singleShot(900, lambda: self.live_vlc_player.set_time(previous_ms))

    def _live_audio_fixup(self) -> None:
        """Confere se a prévia tem áudio; se não tiver, remuxa num arquivo único."""
        if self.live_vlc_player.get_media() is None:
            return
        video, audio = self._live_files()
        if audio is None:
            return  # arquivo único, áudio embutido
        count = self.live_vlc_player.audio_get_track_count()
        if count and count > 0:
            # Faixa existe mas pode estar desselecionada
            if self.live_vlc_player.audio_get_track() == -1:
                try:
                    for track_id, _name in self.live_vlc_player.audio_get_track_description():
                        if track_id != -1:
                            self.live_vlc_player.audio_set_track(track_id)
                            break
                except Exception:
                    pass
            return
        # Sem áudio: lembra a posição atual e parte para o remux
        self._live_needs_remux = True
        self._live_remux_goal = self.live_vlc_player.get_time()
        self._live_start_preview_remux()

    def _live_start_preview_remux(self) -> None:
        if self._live_remux_running:
            return
        video, audio = self._live_files()
        if video is None or audio is None:
            return
        self._live_remux_running = True
        self._live_remux_count += 1
        # Alterna o nome para não esbarrar em lock de arquivo do VLC
        preview = self._live_dir / f"preview_{self._live_remux_count % 2}.mkv"
        # O áudio baixa muito mais rápido que o vídeo; corta a prévia no menor
        # dos dois para a timeline não mostrar tempo que ainda não tem vídeo.
        video_dur = self._live_probe_duration(video)
        audio_dur = self._live_probe_duration(audio)
        durations = [d for d in (video_dur, audio_dur) if d > 0]
        limit = min(durations) if len(durations) == 2 else 0.0
        self.live_log.appendPlainText(
            f"🔧 Preparando prévia com áudio… (vídeo baixado: {as_time(video_dur)} | áudio: {as_time(audio_dur)})")
        self.live_vlc_player.stop()
        command = ["ffmpeg", "-y", "-i", str(video), "-i", str(audio),
                   "-map", "0:v", "-map", "1:a", "-c", "copy"]
        if limit > 0:
            command += ["-t", str(limit)]
        command += [str(preview)]
        self._live_remux_process = QProcess(self)
        self._live_remux_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._live_remux_process.finished.connect(
            lambda code, status: self._live_on_preview_remux(code, preview))
        self._live_remux_process.start(command[0], command[1:])

    def _live_on_preview_remux(self, code: int, preview: Path) -> None:
        self._live_remux_running = False
        if code != 0 or not preview.exists():
            self.live_log.appendPlainText("❌ Falha no remux da prévia.")
            return
        self.live_log.appendPlainText("✅ Prévia com áudio pronta.")
        self._live_duration = 0.0
        self.live_vlc_media = self.vlc_instance.media_new(str(preview))
        self.live_vlc_player.set_media(self.live_vlc_media)
        hwnd = self.live_video_frame.winId()
        if hwnd:
            self.live_vlc_player.set_hwnd(int(hwnd))
        self.live_vlc_player.play()
        self.live_play_button.setText("❚❚")
        self.live_vlc_media.parse_with_options(vlc.MediaParseFlag.local, -1)
        goal = self._live_remux_goal
        if goal == "end":
            QTimer.singleShot(900, lambda: self.live_vlc_player.set_time(max(0, self.live_vlc_player.get_length() - 5000)))
        elif isinstance(goal, int) and goal > 0:
            QTimer.singleShot(900, lambda: self.live_vlc_player.set_time(goal))

    def _live_go_live(self) -> None:
        self._live_load_preview(go_end=True)

    def _live_set_speed(self, rate: float) -> None:
        self.live_vlc_player.set_rate(rate)
        labels = {1.0: "1×", 1.5: "1.5×", 2.0: "2×"}
        for btn in self.live_speed_buttons:
            btn.setObjectName("speedActive" if btn.text() == labels[rate] else "speedButton")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _live_toggle_play(self) -> None:
        if self.live_vlc_player.get_media() is None:
            self._live_load_preview()
            return
        if self.live_vlc_player.is_playing():
            self.live_vlc_player.pause()
            self.live_play_button.setText("▶")
        else:
            self.live_vlc_player.play()
            self.live_play_button.setText("❚❚")

    def _live_poll_vlc(self) -> None:
        if self.live_vlc_player.get_media() is None:
            return
        length = self.live_vlc_player.get_length()
        if length > 0 and int(length) != int(self._live_duration * 1000):
            self._live_duration = length / 1000
            self.live_timeline.setRange(0, length)
        pos = self.live_vlc_player.get_time()
        if not self.live_timeline.isSliderDown():
            self.live_timeline.setValue(pos)
        self.live_time_label.setText(f"{as_time(pos / 1000)} / {as_time(self._live_duration)}")

    def _live_pick_image(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Selecionar imagem do topo", "", "Imagens (*.png *.jpg *.jpeg *.webp)")
        if filename:
            self._live_fixed_image_path = Path(filename)
            self.live_image_label.setText(self._live_fixed_image_path.name)

    def _live_pick_cookies_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Selecionar cookies.txt (logado no youtube.com)", "", "Cookies (*.txt)")
        if not filename:
            return
        self._live_cookies_file = Path(filename)
        self.live_cookies_file_label.setText(f"🍪 {self._live_cookies_file.name} (clique para trocar)")

    def _live_probe_duration(self, path: Path | None) -> float:
        """Duração (s) já gravada num arquivo em crescimento, via ffprobe."""
        if not path or not path.exists():
            return 0.0
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                capture_output=True, text=True, timeout=10)
            return float(result.stdout.strip())
        except (subprocess.SubprocessError, ValueError):
            return 0.0

    def _live_update_status(self) -> None:
        if not self._live_recording:
            return
        video, audio = self._live_files()
        video_dur = self._live_probe_duration(video)
        audio_dur = self._live_probe_duration(audio)
        if video_dur or audio_dur:
            self.live_status.setText(
                f"● Gravando — vídeo baixado: {as_time(video_dur)} | áudio: {as_time(audio_dur)} "
                "(só dá para cortar até onde o vídeo chegou)")

    def _live_cut_busy(self, busy: bool) -> None:
        self.live_export_btn.setDisabled(busy)
        self.live_cancel_btn.setEnabled(busy)
        self.live_cut_progress.setRange(0, 0 if busy else 1)
        if not busy:
            self.live_cut_progress.setValue(0)

    def _live_cancel_cut(self) -> None:
        """Cancela a exportação de corte em andamento na aba Live."""
        self._live_cut_cancelled = True
        proc = self._live_cut_process
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            try:
                proc.finished.disconnect()
            except (TypeError, RuntimeError):
                pass
            proc.kill()
            proc.waitForFinished(2000)
        # Também mata a geração de thumbnail em andamento, se o cancelamento
        # pegou o corte nessa fase (agora assíncrona).
        for thumb_proc in list(self._thumbnail_procs):
            if thumb_proc.state() != QProcess.ProcessState.NotRunning:
                thumb_proc.kill()
                thumb_proc.waitForFinished(2000)
        self._live_cut_process = None
        wav = self._live_pending.get("wav")
        if wav:
            Path(wav).unlink(missing_ok=True)
        self._live_cut_busy(False)
        self.live_cut_log.appendPlainText("⛔ Corte cancelado.")

    def _live_cut_read(self, process: QProcess) -> None:
        data = bytes(process.readAllStandardOutput())
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("cp1252", errors="replace")
        text = text.strip()
        if text:
            self.live_cut_log.appendPlainText(text)

    # ── Exportação de corte da live ──

    def _live_export_cut(self) -> None:
        video, audio = self._live_files()
        if video is None:
            QMessageBox.warning(self, APP_NAME, "Nenhuma gravação disponível ainda.")
            return
        start = self.live_cut_start.value()
        end = self.live_cut_end.value()
        if end <= start:
            QMessageBox.warning(self, APP_NAME, "O fim do corte precisa ser depois do início.")
            return
        mode = self.live_cut_format.currentData()
        if mode == "imagem" and not (self._live_fixed_image_path and self._live_fixed_image_path.exists()):
            QMessageBox.warning(self, APP_NAME, "Escolha a imagem do topo para o formato 'imagem fixa'.")
            return
        # O áudio baixa na frente do vídeo: só deixa cortar o que o vídeo já tem
        video_dur = self._live_probe_duration(video)
        if video_dur > 0 and end > video_dur + 1:
            QMessageBox.warning(
                self, APP_NAME,
                f"O vídeo só foi baixado até {as_time(video_dur)} (o áudio baixa mais rápido).\n"
                f"Marque o fim do corte antes disso, ou aguarde o download do vídeo alcançar.")
            return
        titulo = self.live_cut_title.text().strip()
        self._live_cut_count += 1
        safe_label = "".join(c for c in titulo if c.isalnum() or c in " _-").strip()[:80] or f"corte_live_{self._live_cut_count:02d}"
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        self._live_pending = {
            "start": start, "end": end, "mode": mode, "titulo": titulo,
            "video": video, "audio": audio,
            "output": OUTPUT_DIR / f"{safe_label}.mp4",
        }
        self._live_cut_cancelled = False
        self._live_cut_busy(True)
        self.live_cut_log.clear()

        # Fase 1: legendas do trecho com Whisper (se ligado)
        if self.live_captions_check.isChecked():
            self._whisper_bin = whisper_path()
            if not self._whisper_bin:
                self.live_cut_log.appendPlainText("⚠️ Whisper não encontrado; exportando sem legendas.")
                self._live_run_cut(None)
                return
            source = audio or video
            wav = self._live_dir / f"cut_{self._live_cut_count:02d}.wav"
            self._live_pending["wav"] = wav
            self.live_cut_log.appendPlainText(f"1/3 🎙️ Extraindo áudio de {as_time(start)} — {as_time(end)}…")
            command = ["ffmpeg", "-y", "-ss", str(start), "-t", str(end - start),
                       "-i", str(source), "-vn", "-ac", "1", "-ar", "16000", str(wav)]
            proc = QProcess(self)
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            proc.readyReadStandardOutput.connect(lambda p=proc: self._live_cut_read(p))
            proc.finished.connect(self._live_on_cut_audio)
            self._live_cut_process = proc
            proc.start(command[0], command[1:])
        else:
            self._live_run_cut(None)

    def _live_on_cut_audio(self, code: int, status: QProcess.ExitStatus) -> None:
        wav = self._live_pending.get("wav")
        if code != 0 or not wav or not wav.exists():
            self.live_cut_log.appendPlainText("⚠️ Não deu para extrair o áudio do trecho; exportando sem legendas.")
            self._live_run_cut(None)
            return
        self.live_cut_log.appendPlainText(
            f"2/3 🎙️ Transcrevendo com Whisper (modelo {self.whisper_model.currentData()})…")
        command = self.whisper_command(self._whisper_bin, wav, self._live_dir)
        proc = QProcess(self)
        proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        proc.readyReadStandardOutput.connect(lambda p=proc: self._live_cut_read(p))
        proc.finished.connect(self._live_on_cut_whisper)
        self._live_cut_process = proc
        proc.start(command[0], command[1:])

    def _live_on_cut_whisper(self, code: int, status: QProcess.ExitStatus) -> None:
        wav = self._live_pending.get("wav")
        srt = wav.with_suffix(".srt") if wav else None
        if code == 0 and srt and srt.exists():
            def ready() -> None:
                # Encurtar depois da revisão: a IA lida melhor com frases inteiras.
                shorten_srt_captions(srt)
                self._live_run_cut(srt)

            skip = self.ai_review_skip_reason(srt)
            if skip:
                self.live_cut_log.appendPlainText("⚠️ " + skip)
            if self.ai_review_enabled() and not skip:
                self.live_cut_log.appendPlainText("🤖 Revisando a legenda com IA…")
                started = self._start_ai_review(
                    srt, self._live_pending.get("titulo", ""),
                    lambda ok, msg, *_: (self.live_cut_log.appendPlainText(("✅ " if ok else "⚠️ ") + msg),
                                         ready()),
                )
                if not started:
                    ready()
            else:
                ready()
        elif code != 0:
            self.live_cut_log.appendPlainText(
                f"⚠️ Whisper terminou com erro (código {code}); exportando sem legendas.")
            self._live_run_cut(None)
        else:
            self.live_cut_log.appendPlainText(
                "⚠️ Whisper rodou mas não gerou o arquivo de legenda; exportando sem legendas.")
            self._live_run_cut(None)

    def _live_run_cut(self, srt_path: Path | None) -> None:
        d = self._live_pending
        d["srt"] = srt_path             # guardado p/ gerar o prompt .txt no fim
        start, end, mode, titulo = d["start"], d["end"], d["mode"], d["titulo"]
        video, audio, output = d["video"], d["audio"], d["output"]
        length = end - start

        has_image = mode == "imagem"
        audio_idx = 1 if audio is not None else None
        next_idx = 2 if audio is not None else 1
        image_idx = next_idx if has_image else None

        def build_chain(captions: bool, post_input: int | None = None) -> str:
            chain = build_clip_filter(mode, has_image, image_input=image_idx or 1)
            current = "base"
            if captions and srt_path is not None and srt_path.exists():
                style = ("FontName=Montserrat,FontSize=18,Bold=-1,"
                         "PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,"
                         "BorderStyle=1,Outline=2.5,Shadow=0,Alignment=2,MarginV=60")
                chain += (f";[{current}]subtitles=filename='{filter_path(srt_path)}':"
                          f"fontsdir='{filter_path(FONT_DIR)}':force_style='{style}'[captioned]")
                current = "captioned"
            if titulo:
                font = "C\\:/Windows/Fonts/arialbd.ttf"
                chain += (f";[{current}]drawtext=fontfile='{font}':text='{escape_drawtext(titulo)}':"
                          f"x=(w-text_w)/2:y=30:fontsize=40:fontcolor=white:borderw=3:bordercolor=black[text]")
                current = "text"
            chain, current = self._watermark_chain(chain, current)
            chain, current = self._with_post_frame(chain, current, post_input)
            return chain + f";[{current}]format=yuv420p[outv]"

        seek = ["-ss", str(start), "-t", str(length), "-i", str(video)]
        if audio is not None:
            seek += ["-ss", str(start), "-t", str(length), "-i", str(audio)]
        extra = []
        if has_image:
            extra += ["-loop", "1", "-i", str(self._live_fixed_image_path)]

        # 1) Frame de post: primeiro frame do corte, sem legenda escrita.
        thumb = thumbnail_path(output)
        thumb_seek = ["-ss", str(start), "-i", str(video)]
        if audio is not None:
            thumb_seek += ["-ss", str(start), "-i", str(audio)]
        thumb_command = (
            ["ffmpeg", "-y"] + thumb_seek + extra
            + ["-filter_complex", build_chain(captions=False),
               "-map", "[outv]", "-frames:v", "1", "-q:v", "2", str(thumb)]
        )

        def after_thumbnail(post_frame: Path | None) -> None:
            if getattr(self, "_live_cut_cancelled", False):
                return
            self._live_post_frame = post_frame
            # 2) Esse frame vira a capa: entra por cima do frame 0 do vídeo.
            post_input = None
            if post_frame:
                extra.extend(["-loop", "1", "-i", str(thumb)])
                post_input = next_idx + (1 if has_image else 0)

            audio_map = f"{audio_idx}:a?" if audio_idx is not None else "0:a?"
            command = ["ffmpeg", "-y"] + seek + extra + [
                "-filter_complex", build_chain(captions=True, post_input=post_input),
                "-map", "[outv]", "-map", audio_map,
            ] + self._audio_censor_args(
                self.apply_censorship(srt_path, self.live_cut_log.appendPlainText)) + [
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest",
                str(output),
            ]

            self.live_cut_log.appendPlainText(f"3/3 ✂️ Exportando corte {as_time(start)} — {as_time(end)} ({mode})…")
            proc = QProcess(self)
            proc.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            proc.readyReadStandardOutput.connect(lambda p=proc: self._live_cut_read(p))
            proc.finished.connect(lambda code, status: self._live_on_cut_finished(code, status, output))
            self._live_cut_process = proc
            proc.start(command[0], command[1:])

        self._render_thumbnail_async(thumb_command, thumb, after_thumbnail)

    def _live_on_cut_finished(self, code: int, status: QProcess.ExitStatus, output: Path) -> None:
        self._live_cut_busy(False)
        wav = self._live_pending.get("wav")
        if wav:
            Path(wav).unlink(missing_ok=True)
        if code == 0 and status == QProcess.ExitStatus.NormalExit:
            self.live_cut_log.appendPlainText(f"✅ Corte exportado → {output}")
            self.live_log.appendPlainText(f"✅ Corte exportado → {output.name}")
            # Legenda de Instagram (.txt ao lado do vídeo), gerada pela IA.
            self.deliver_reels_caption(
                self._live_pending.get("srt"), output,
                self.live_cut_log.appendPlainText)
            log_video(self._live_pending.get("titulo", ""), "", "")
            post = getattr(self, "_live_post_frame", None)
            if post:
                self.live_cut_log.appendPlainText(f"🖼️ Post/capa → {post.name}")
            else:
                self.live_cut_log.appendPlainText("⚠️ Não deu para gerar o frame de post.")
            self._live_post_frame = None
        else:
            self.live_cut_log.appendPlainText(
                f"❌ Falha ao exportar {output.name} — veja as mensagens acima para o motivo.")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
