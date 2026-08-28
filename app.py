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
from datetime import datetime
from pathlib import Path

import vlc
from PySide6.QtCore import QEvent, QObject, QProcess, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from utils import (
    APP_NAME, DOWNLOAD_DIR, FONT_DIR, OUTPUT_DIR,
    PROJECT_DIR, YTDLP_BUNDLED, YTDLP_SYSTEM,
    as_time, build_clip_filter, build_srt_for_clip, command_exists,
    escape_drawtext, filter_path, find_video_subtitle, format_title_for_video,
    render_thumbnail, review_srt_with_ai, thumbnail_path,
    parse_csv_moments, parse_srt_segments, parse_time_string, segments_to_srt,
    shorten_srt_captions, srt_has_content, whisper_path, yt_dlp_path,
    TimestampInput,
)

import ai_srt
from cg_generator import LT_BOTTOM_MARGIN, LT_HEIGHT, create_lower_third

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

    progress = Signal(int, int)      # linhas prontas, total
    done = Signal(bool, str)         # deu certo?, mensagem para o log

    def __init__(self, path: Path, api_key: str, model: str,
                 context: str = "", parent=None) -> None:
        super().__init__(parent)
        self._path, self._api_key = path, api_key
        self._model, self._context = model, context

    def run(self) -> None:
        try:
            changed, total = review_srt_with_ai(
                self._path, self._api_key, self._model, self._context,
                progress=lambda ready, all_: self.progress.emit(ready, all_),
            )
        except ai_srt.AiError as error:
            self.done.emit(False, f"Revisão com IA falhou: {error}")
        except Exception as error:                     # noqa: BLE001
            self.done.emit(False, f"Revisão com IA falhou: {error}")
        else:
            self.done.emit(True, f"Legenda revisada pela IA: {changed}/{total} blocos alterados.")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 780)
        self.video_path: Path | None = None
        self.fixed_image_path: Path | None = None
        self.caption_path: Path | None = None
        self.cg_icon_path: Path | None = Path("G:/My Drive/Canais/Informativo Nacional/icone informativo.png") if Path("G:/My Drive/Canais/Informativo Nacional/icone informativo.png").exists() else None
        self.duration = 0.0
        self.work_dir = Path(tempfile.mkdtemp(prefix="corta_legenda_"))
        self.process: QProcess | None = None
        self.playback_speed = 1.0
        self._vlc_seeking = False

        self.vlc_instance = vlc.Instance("--avcodec-hw=none")  # Desabilita hardware decoding
        self.vlc_player = self.vlc_instance.media_player_new()
        self.vlc_media: vlc.Media | None = None
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
        self._live_cut_process: QProcess | None = None
        self._live_cut_count = 0
        self._live_pending: dict = {}
        self._live_needs_remux = False
        self._live_remux_running = False
        self._live_remux_goal: object = 0
        self._live_remux_count = 0
        self._live_remux_process: QProcess | None = None

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
        source_layout.addWidget(self.url_input)
        source_layout.addLayout(acceleration_row)
        source_layout.addWidget(self.download_button)
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

        overlay_box = QGroupBox("3. Texto")
        overlay_form = QFormLayout(overlay_box)
        self.text_input = QLineEdit("Glauber Fugiu do Mamãe Falei!")
        self.subtitle_input = QLineEdit("")
        self.subtitle_input.setPlaceholderText("Chapéu/subtítulo (ex: ELEIÇÕES 2026)")
        overlay_form.addRow("Título", self.text_input)
        overlay_form.addRow("Subtítulo", self.subtitle_input)
        self.use_cg = QCheckBox("Usar lower-third 'Informativo Nacional' (rodapé)")
        self.use_cg.setChecked(bool(self.cg_icon_path))
        self.use_cg.setEnabled(bool(self.cg_icon_path))
        overlay_form.addRow(self.use_cg)
        panel.addWidget(overlay_box)

        caption_box = QGroupBox("4. Legendas")
        caption_layout = QVBoxLayout(caption_box)
        self.caption_status = QLabel("As legendas serão geradas somente para o recorte.")
        self.caption_status.setWordWrap(True)
        caption_style = QLabel("Estilo: Montserrat amarela, contorno preto e blocos curtos.")
        caption_style.setObjectName("muted")
        self.caption_button = QPushButton("Gerar legendas do recorte")
        self.caption_button.clicked.connect(self.generate_captions)
        caption_layout.addWidget(self.caption_status)
        caption_layout.addWidget(caption_style)
        caption_layout.addWidget(self.caption_button)
        panel.addWidget(caption_box)

        panel.addWidget(self._build_ai_box())

        self.export_button = QPushButton("Exportar vídeo")
        self.export_button.setObjectName("primary")
        self.export_button.clicked.connect(self.export_video)
        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self.cancel_edit)
        self.cancel_button.setEnabled(False)
        self.progress = QProgressBar(); self.progress.setRange(0, 1); self.progress.setValue(0); self.progress.setTextVisible(False)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(300); self.log.setMaximumHeight(95)
        panel.addWidget(self.export_button)
        panel.addWidget(self.cancel_button)
        panel.addWidget(self.progress)
        panel.addWidget(self.log)
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
                # Troca "default,web_safari" por "web_embedded" (clientes diferentes)
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
                        started = self._start_ai_review(
                            self.caption_path, self.text_input.text().strip(),
                            lambda ok, msg: (self.log.appendPlainText(("✅ " if ok else "⚠️ ") + msg),
                                             ready(" (revisada pela IA)" if ok else "")),
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
        existing = find_video_subtitle(self.video_path)
        if existing:
            out = self.work_dir / "trecho_para_legendar.srt"
            if segments_to_srt(parse_srt_segments(existing), start, start + length, out):
                shorten_srt_captions(out)
                self.caption_path = out
                self.caption_status.setText(f"Legenda do vídeo reaproveitada: {existing.name}")
                QMessageBox.information(self, APP_NAME, f"Legenda do vídeo reaproveitada ({existing.name}).\nSerá aplicada na exportação.")
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
        command = [whisper, str(audio_path), "--model", "base", "--language", "Portuguese", "--task", "transcribe", "--output_format", "srt", "--output_dir", str(self.work_dir)]
        self.run_process(command, "Legendas geradas. Elas serão aplicadas na exportação.", caption=True)

    # ──────────────────────────────────────────────────────────────
    #  Revisão da legenda com IA (DeepSeek) — vale para os 3 tipos de corte
    # ──────────────────────────────────────────────────────────────

    def _build_ai_box(self) -> QGroupBox:
        """Caixa de configuração da revisão com IA, na aba Edição.

        A configuração é única e vale para os três fluxos (edição, CSV e live).
        """
        config = ai_srt.load_config()
        box = QGroupBox("5. Revisão da legenda com IA (DeepSeek)")
        layout = QVBoxLayout(box)

        self.ai_enabled = QCheckBox("Revisar as legendas com IA antes de queimar no vídeo")
        self.ai_enabled.setChecked(bool(config.get("ai_enabled")))
        self.ai_enabled.toggled.connect(self._save_ai_config)
        layout.addWidget(self.ai_enabled)

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
                      "com os tempos originais.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        self.ai_button = QPushButton("Revisar a legenda atual com IA")
        self.ai_button.clicked.connect(self._review_current_caption)
        layout.addWidget(self.ai_button)
        return box

    def _save_ai_config(self) -> None:
        ai_srt.save_config({
            "ai_enabled": self.ai_enabled.isChecked(),
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

    def _start_ai_review(self, path: Path, context: str, on_done) -> bool:
        """Dispara a revisão em segundo plano. False se não deu para começar."""
        if not srt_has_content(path):
            return False
        key = self._ai_key()
        if not key:
            return False
        worker = SrtReviewWorker(path, key, self.ai_model.currentText().strip(), context, self)
        worker.done.connect(on_done)
        worker.finished.connect(worker.deleteLater)
        self._ai_worker = worker           # segura a referência enquanto roda
        worker.start()
        return True

    def _review_current_caption(self) -> None:
        """Botão da aba Edição: revisa a legenda já gerada, sob demanda."""
        if not srt_has_content(self.caption_path):
            QMessageBox.warning(self, APP_NAME, "Gere as legendas do recorte antes de revisar.")
            return
        if not self._ai_key():
            QMessageBox.warning(self, APP_NAME,
                                "Informe a chave da API do DeepSeek (ou defina DEEPSEEK_API_KEY).")
            return
        self.ai_button.setEnabled(False)
        self.ai_button.setText("Revisando com IA…")

        def finished(ok: bool, message: str) -> None:
            self.ai_button.setEnabled(True)
            self.ai_button.setText("Revisar a legenda atual com IA")
            self.log.appendPlainText(("✅ " if ok else "⚠️ ") + message)
            self.caption_status.setText(message)
            if not ok:
                QMessageBox.warning(self, APP_NAME, message)

        self._start_ai_review(self.caption_path, self.text_input.text().strip(), finished)

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
        # --extractor-args player_client: o cliente "android_vr" às vezes entrega
        #   URLs de mídia que dão HTTP 403; "default,web_safari" é mais estável.
        # --*-retries: reenfileira automaticamente falhas transitórias (403/429).
        command = [str(downloader), "--no-playlist", "--match-filter", "!is_live",
                   "--extractor-args", "youtube:player_client=default,web_safari",
                   "--retries", "10", "--fragment-retries", "10", "--extractor-retries", "3",
                   "-N", str(self.fragment_count.value()), "-f", "bv*+ba/b",
                   "--merge-output-format", "mp4", "-P", str(DOWNLOAD_DIR),
                   "-o", "%(title).200B.%(ext)s"]
        if self.download_subs.isChecked():
            # Legendas do próprio vídeo (oficiais + automáticas do YouTube) em
            # pt-br, convertidas para .srt ao lado do vídeo. A lista de idiomas
            # é enxuta de propósito: incluir "pt.*" puxa dezenas de traduções
            # automáticas e dispara HTTP 429 (Too Many Requests).
            command += ["--write-subs", "--write-auto-subs",
                        "--sub-langs", "pt-BR,pt,pt-orig",
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
                self.work_dir, self.cg_icon_path,
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
        self._post_frame = render_thumbnail(
            ["ffmpeg", "-y", "-ss", str(start)] + inputs
            + ["-filter_complex", self.video_filters(captions=False),
               "-map", "[outv]", "-frames:v", "1", "-q:v", "2", str(thumb)],
            thumb,
        )
        # 2) O frame de post entra como primeiro frame do vídeo exportado.
        post_input = None
        if self._post_frame:
            inputs += ["-loop", "1", "-i", str(thumb)]
            post_input = n_inputs

        command = ["ffmpeg", "-y", "-ss", str(start), "-t", str(length)] + inputs
        command += ["-filter_complex", self.video_filters(post_input=post_input), "-map", "[outv]", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", "-shortest", filename]
        # Ao terminar, salva a transcrição (pt-br) ao lado do vídeo, se houver.
        self._srt_to_export = self.caption_path if srt_has_content(self.caption_path) else None
        self._srt_export_target = Path(filename).with_suffix(".srt")
        self.run_process(command, f"Vídeo exportado em:\n{filename}")

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
        chain, current = self._with_post_frame(chain, current, post_input)
        return chain + f";[{current}]format=yuv420p[outv]"

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

        # Texto
        overlay_box = QGroupBox("Texto")
        overlay_form = QFormLayout(overlay_box)
        text_hint = QLabel("O título de cada corte aparecerá na tela automaticamente")
        text_hint.setObjectName("muted")
        overlay_form.addRow(text_hint)
        self.csv_captions = QCheckBox("Gerar legendas com Whisper (sincronizadas por corte)")
        self.csv_captions.setChecked(True)
        overlay_form.addRow(self.csv_captions)
        self.csv_use_cg = QCheckBox("Usar lower-third 'Informativo Nacional' (rodapé)")
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
        # dispensa o Whisper. Só cai no Whisper quando não há legenda pronta.
        self._csv_source_srt = find_video_subtitle(self.video_path) if self._csv_captions_on else None
        self._csv_use_whisper = self._csv_captions_on and self._whisper_bin is not None
        if self._csv_captions_on and not self._csv_source_srt and not self._whisper_bin:
            QMessageBox.warning(self, APP_NAME, "Sem legenda pronta e Whisper não encontrado. Os cortes serão gerados sem legendas.")

        # Salva os cortes numa subpasta com o nome do vídeo de origem.
        safe_dir = "".join(c for c in self.video_path.stem if c.isalnum() or c in " _-").strip()[:120] or "Cortes"
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
        self.csv_process_btn.setDisabled(False)
        self.csv_cancel_btn.setEnabled(False)
        self.csv_progress.setRange(0, 1)
        self.csv_progress.setValue(0)
        self.csv_log.appendPlainText("\n⛔ Processamento cancelado.")

    def _csv_on_moment_selected(self) -> None:
        rows = self.csv_moments_list.selectionModel().selectedRows()
        if not rows:
            self._csv_selected_index = -1
            self.csv_clip_format_label.setText("Nenhum trecho selecionado")
            self.csv_clip_format.blockSignals(True)
            self.csv_clip_format.setCurrentIndex(0)
            self.csv_clip_format.blockSignals(False)
            self.csv_vlc_player.stop()
            return
        idx = rows[0].row()
        self._csv_selected_index = idx
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
            self.csv_log.appendPlainText("   🎙️ Transcrevendo com Whisper (modelo base)…")
            command = [self._whisper_bin, str(audio_path), "--model", "base", "--language",
                       "Portuguese", "--task", "transcribe", "--output_format", "srt",
                       "--output_dir", str(self.work_dir)]
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
                    lambda ok, msg: (self.csv_log.appendPlainText(f"   {'✅' if ok else '⚠️'} {msg}"),
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
            cg_path = create_lower_third(titulo, m.get("subtitulo", ""), self.work_dir, self.cg_icon_path)
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
        self._csv_post_frame = render_thumbnail(
            ["ffmpeg", "-y", "-ss", str(start)] + inputs
            + ["-filter_complex", build_chain(captions=False),
               "-map", "[outv]", "-frames:v", "1", "-q:v", "2", str(thumb)],
            thumb,
        )
        # 2) Esse frame vira a capa: entra por cima do frame 0 do vídeo.
        post_input = None
        if self._csv_post_frame:
            inputs += ["-loop", "1", "-i", str(thumb)]
            post_input = n_inputs

        command = ["ffmpeg", "-y", "-ss", str(start), "-t", str(end - start)] + inputs
        command += [
            "-filter_complex", build_chain(captions=True, post_input=post_input),
            "-map", "[outv]", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-shortest",
            str(output),
        ]

        self._csv_process = QProcess(self)
        self._csv_process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._csv_process.readyReadStandardOutput.connect(self._csv_read_process_output)
        self._csv_process.finished.connect(lambda code, status: self._csv_on_finished(code, status, output, titulo))
        self._csv_process_output = ""
        self._csv_process.start(command[0], command[1:])

    def _csv_read_process_output(self) -> None:
        """Captura saída do FFmpeg durante o processamento."""
        data = bytes(self._csv_process.readAllStandardOutput())
        try:
            output = data.decode("utf-8")
        except UnicodeDecodeError:
            output = data.decode("cp1252", errors="replace")
        self._csv_process_output += output

    def _csv_on_finished(self, code: int, status: QProcess.ExitStatus, output: Path, label: str) -> None:
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
        # --extractor-args: usa player_client=default,web_safari para evitar 403.
        # --retries/--fragment-retries: tenta reconectar em caso de falha.
        # -f: prioriza H.264 (mais estável em live) sobre AV1 (instável com fragmentos perdidos).
        base = [str(downloader), "--live-from-start", "--no-part", "--newline",
                 "--extractor-args", "youtube:player_client=default,web_safari",
                 "--retries", "10", "--fragment-retries", "10"]
        video_cmd = base + ["-f", "bv*[vcodec=h264][ext=mp4]/bv*[ext=mp4]/b[ext=mp4]",
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
        proc = self._live_cut_process
        if proc is not None and proc.state() != QProcess.ProcessState.NotRunning:
            try:
                proc.finished.disconnect()
            except (TypeError, RuntimeError):
                pass
            proc.kill()
            proc.waitForFinished(2000)
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
        self.live_cut_log.appendPlainText("2/3 🎙️ Transcrevendo com Whisper (modelo base)…")
        command = [self._whisper_bin, str(wav), "--model", "base", "--language", "Portuguese",
                   "--task", "transcribe", "--output_format", "srt", "--output_dir", str(self._live_dir)]
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
                    lambda ok, msg: (self.live_cut_log.appendPlainText(("✅ " if ok else "⚠️ ") + msg),
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
        self._live_post_frame = render_thumbnail(
            ["ffmpeg", "-y"] + thumb_seek + extra
            + ["-filter_complex", build_chain(captions=False),
               "-map", "[outv]", "-frames:v", "1", "-q:v", "2", str(thumb)],
            thumb,
        )
        # 2) Esse frame vira a capa: entra por cima do frame 0 do vídeo.
        post_input = None
        if self._live_post_frame:
            extra += ["-loop", "1", "-i", str(thumb)]
            post_input = next_idx + (1 if has_image else 0)

        audio_map = f"{audio_idx}:a?" if audio_idx is not None else "0:a?"
        command = ["ffmpeg", "-y"] + seek + extra + [
            "-filter_complex", build_chain(captions=True, post_input=post_input),
            "-map", "[outv]", "-map", audio_map,
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

    def _live_on_cut_finished(self, code: int, status: QProcess.ExitStatus, output: Path) -> None:
        self._live_cut_busy(False)
        wav = self._live_pending.get("wav")
        if wav:
            Path(wav).unlink(missing_ok=True)
        if code == 0 and status == QProcess.ExitStatus.NormalExit:
            self.live_cut_log.appendPlainText(f"✅ Corte exportado → {output}")
            self.live_log.appendPlainText(f"✅ Corte exportado → {output.name}")
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
