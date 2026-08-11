"""Corta+Legenda — editor de vídeo offline baseado em FFmpeg e Whisper."""

from __future__ import annotations

import os
import sys

os.environ["QT_DISABLE_HW_VIDEO_DECODING"] = "1"
os.environ["QT_FFMPEG_NO_HWACCEL"] = "1"
os.environ["QMEDIAPLAYER_USE_HW"] = "0"

import subprocess
import tempfile
from pathlib import Path

import vlc
from PySide6.QtCore import QProcess, Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSlider, QSpinBox, QTabWidget, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from utils import (
    APP_NAME, DEFAULT_LOGO, DOWNLOAD_DIR, FONT_DIR, OUTPUT_DIR,
    PROJECT_DIR, YTDLP_BUNDLED, YTDLP_SYSTEM,
    as_time, build_clip_filter, build_srt_for_clip, command_exists,
    escape_drawtext, filter_path, parse_csv_moments, parse_time_string,
    shorten_srt_captions, yt_dlp_path,
    TimestampInput,
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1180, 780)
        self.video_path: Path | None = None
        self.logo_path: Path | None = DEFAULT_LOGO if DEFAULT_LOGO.exists() else None
        self.fixed_image_path: Path | None = None
        self.caption_path: Path | None = None
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

        self.build_ui()

        # Timer para atualizar timeline e duração (VLC não tem sinais Qt)
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(100)
        self._poll_timer.timeout.connect(self._poll_vlc)
        self._poll_timer.timeout.connect(self._csv_poll_vlc)
        self._poll_timer.start()

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
        source_layout.addWidget(self.url_input)
        source_layout.addLayout(acceleration_row)
        source_layout.addWidget(self.download_button)
        source_layout.addWidget(download_hint)
        panel.addWidget(source_box)

        cut_box = QGroupBox("1. Recorte")
        cut_form = QFormLayout(cut_box)
        self.start_input = self.time_input()
        self.end_input = self.time_input()
        self.start_input.secondsChanged.connect(self.sync_cut_range)
        self.end_input.secondsChanged.connect(self.sync_cut_range)
        cut_form.addRow("Início", self.start_input)
        cut_form.addRow("Fim", self.end_input)
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

        overlay_box = QGroupBox("3. Logo e texto")
        overlay_form = QFormLayout(overlay_box)
        logo_row = QWidget()
        logo_layout = QHBoxLayout(logo_row); logo_layout.setContentsMargins(0, 0, 0, 0)
        self.logo_label = QLabel(self.logo_path.name if self.logo_path else "Sem logo")
        logo_button = QPushButton("Escolher")
        logo_button.clicked.connect(self.pick_logo)
        logo_layout.addWidget(self.logo_label, 1); logo_layout.addWidget(logo_button)
        self.logo_position = QComboBox()
        self.logo_position.addItems(["Canto superior direito", "Canto superior esquerdo", "Canto inferior direito", "Canto inferior esquerdo"])
        self.logo_position.setCurrentIndex(3)
        self.logo_size = QSpinBox(); self.logo_size.setRange(40, 600); self.logo_size.setValue(250); self.logo_size.setSuffix(" px")
        self.text_input = QLineEdit("Glauber Fugiu do Mamãe Falei!")
        overlay_form.addRow("Logo", logo_row)
        overlay_form.addRow("Posição", self.logo_position)
        overlay_form.addRow("Largura", self.logo_size)
        overlay_form.addRow("Texto", self.text_input)
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

        self.export_button = QPushButton("Exportar vídeo")
        self.export_button.setObjectName("primary")
        self.export_button.clicked.connect(self.export_video)
        self.progress = QProgressBar(); self.progress.setRange(0, 1); self.progress.setValue(0); self.progress.setTextVisible(False)
        self.log = QPlainTextEdit(); self.log.setReadOnly(True); self.log.setMaximumBlockCount(300); self.log.setMaximumHeight(95)
        panel.addWidget(self.export_button)
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

    def pick_logo(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "Selecionar logo", "", "Imagens (*.png *.jpg *.jpeg *.webp)")
        if filename:
            self.logo_path = Path(filename)
            self.logo_label.setText(self.logo_path.name)

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
        self.progress.setRange(0, 0 if busy else 1)
        if not busy: self.progress.setValue(0)

    def run_process(self, command: list[str], success_message: str, caption: bool = False) -> None:
        self.set_busy(True, caption)
        self.log.appendPlainText("\n> " + " ".join(command[:3]) + " …")
        self.process = QProcess(self)
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self.append_process_output)
        self.process.finished.connect(lambda code, status: self.process_finished(code, status, success_message, caption))
        self.process.start(command[0], command[1:])

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
                if srt_files:
                    self.caption_path = srt_files[-1]
                    shorten_srt_captions(self.caption_path)
                    self.caption_status.setText(f"Legendas prontas: {self.caption_path.name}")
            QMessageBox.information(self, APP_NAME, message)
        else:
            QMessageBox.critical(self, APP_NAME, "O processamento falhou. Veja o log abaixo e confirme se FFmpeg/Whisper estão instalados.")

    def generate_captions(self) -> None:
        if not self.validate_video(): return
        if not command_exists("whisper"):
            QMessageBox.warning(self, APP_NAME, "Whisper não foi encontrado. Instale as dependências do README para usar legendas offline.")
            return
        start, length = self.cut_values()
        audio_path = self.work_dir / "trecho_para_legendar.wav"
        try:
            subprocess.run(["ffmpeg", "-y", "-ss", str(start), "-t", str(length), "-i", str(self.video_path), "-vn", "-ac", "1", "-ar", "16000", str(audio_path)], check=True, capture_output=True)
        except subprocess.CalledProcessError:
            QMessageBox.critical(self, APP_NAME, "Não foi possível extrair o áudio do trecho.")
            return
        command = ["whisper", str(audio_path), "--model", "small", "--language", "Portuguese", "--task", "transcribe", "--output_format", "srt", "--output_dir", str(self.work_dir)]
        self.run_process(command, "Legendas geradas. Elas serão aplicadas na exportação.", caption=True)

    def download_video(self) -> None:
        url = self.url_input.text().strip()
        if not url:
            QMessageBox.warning(self, APP_NAME, "Cole a URL de um vídeo para baixar.")
            return
        downloader = yt_dlp_path()
        if not downloader:
            QMessageBox.critical(self, APP_NAME, "yt-dlp não foi encontrado. Consulte o README para instalá-lo.")
            return
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        command = [str(downloader), "--no-playlist", "-N", str(self.fragment_count.value()), "-f", "bv*+ba/b", "--merge-output-format", "mp4", "-P", str(DOWNLOAD_DIR), "-o", "%(title).200B.%(ext)s", url]
        self.run_process(command, f"Download concluído em:\n{DOWNLOAD_DIR}\n\nAgora selecione o vídeo para editá-lo.")

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
        # Logo (se houver) é a entrada 1; a imagem fixa vem logo depois.
        self._image_input_index = 2 if self.logo_path else 1
        filters = self.video_filters()
        command = ["ffmpeg", "-y", "-ss", str(start), "-t", str(length), "-i", str(self.video_path)]
        if self.logo_path: command += ["-loop", "1", "-i", str(self.logo_path)]
        if mode == "vertical_image": command += ["-loop", "1", "-i", str(self.fixed_image_path)]
        command += ["-filter_complex", filters, "-map", "[outv]", "-map", "0:a?", "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", "-shortest", filename]
        self.run_process(command, f"Vídeo exportado em:\n{filename}")

    def video_filters(self) -> str:
        mode = self.ratio.currentData()
        locations = [("W-w-42", "42"), ("42", "42"), ("W-w-42", "H-h-42"), ("42", "H-h-42")]
        logo_applied = False
        if mode == "vertical_crop":
            chain, label = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920", "base"
        elif mode == "vertical_blur":
            chain = "[0:v]split=2[bg][fg];[bg]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,boxblur=20:10[blur];[fg]scale=1080:1920:force_original_aspect_ratio=decrease[fit]"
            if self.logo_path:
                x, y = locations[self.logo_position.currentIndex()]
                chain += f";[1:v]scale={self.logo_size.value()}:-1[logo];[fit][logo]overlay={x}:{y}[fit_with_logo];[blur][fit_with_logo]overlay=(W-w)/2:(H-h)/2"
                logo_applied = True
            else:
                chain += ";[blur][fit]overlay=(W-w)/2:(H-h)/2"
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
        if self.logo_path and not logo_applied:
            x, y = locations[self.logo_position.currentIndex()]
            chain += f";[1:v]scale={self.logo_size.value()}:-1[logo];[{current}][logo]overlay={x}:{y}[with_logo]"
            current = "with_logo"
        if self.text_input.text().strip():
            font = "C\\:/Windows/Fonts/arialbd.ttf"
            text = escape_drawtext(self.text_input.text().strip())
            chain += f";[{current}]drawtext=fontfile='{font}':text='{text}':x=(w-text_w)/2:y=h*0.12:fontsize=54:fontcolor=white:borderw=3:bordercolor=black[text]"
            current = "text"
        if self.caption_path and self.caption_path.exists():
            style = "FontName=Montserrat,FontSize=18,Bold=-1,PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=0,Alignment=2,MarginV=60"
            chain += f";[{current}]subtitles=filename='{filter_path(self.caption_path)}':fontsdir='{filter_path(FONT_DIR)}':force_style='{style}'[captioned]"
            current = "captioned"
        return chain + f";[{current}]format=yuv420p[outv]"

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
        right_panel.addWidget(format_select_box)

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
            "Colunas (com cabeçalho): inicio, fim, titulo, formato, imagem.\n"
            "• formato: estender / transparente / imagem / original (vazio = padrão abaixo)\n"
            "• imagem: caminho do PNG/JPG, usado só quando formato=imagem"
        )
        csv_hint.setObjectName("muted")
        csv_hint.setWordWrap(True)
        csv_box_layout.addWidget(csv_hint)
        panel.addWidget(csv_box)

        # Logo e texto
        overlay_box = QGroupBox("Logo e Texto")
        overlay_form = QFormLayout(overlay_box)
        logo_row = QWidget()
        logo_layout = QHBoxLayout(logo_row); logo_layout.setContentsMargins(0, 0, 0, 0)
        self.csv_logo_label = QLabel(self.logo_path.name if self.logo_path else "Sem logo")
        logo_button = QPushButton("Escolher")
        logo_button.clicked.connect(self.pick_logo)
        logo_layout.addWidget(self.csv_logo_label, 1); logo_layout.addWidget(logo_button)
        self.csv_logo_position = QComboBox()
        self.csv_logo_position.addItems(["Canto superior direito", "Canto superior esquerdo", "Canto inferior direito", "Canto inferior esquerdo"])
        self.csv_logo_position.setCurrentIndex(3)
        self.csv_logo_size = QSpinBox(); self.csv_logo_size.setRange(40, 600); self.csv_logo_size.setValue(250); self.csv_logo_size.setSuffix(" px")
        overlay_form.addRow("Logo", logo_row)
        overlay_form.addRow("Posição", self.csv_logo_position)
        overlay_form.addRow("Largura", self.csv_logo_size)
        text_hint = QLabel("O título de cada corte aparecerá na tela automaticamente")
        text_hint.setObjectName("muted")
        overlay_form.addRow(text_hint)
        self.csv_captions = QCheckBox("Gerar legendas com Whisper (sincronizadas por corte)")
        self.csv_captions.setChecked(True)
        overlay_form.addRow(self.csv_captions)
        panel.addWidget(overlay_box)

        # Preview
        preview_box = QGroupBox("Momentos detetados")
        preview_layout = QVBoxLayout(preview_box)
        self.csv_table = QTableWidget(0, 5)
        self.csv_table.setHorizontalHeaderLabels(["Início", "Fim", "Título", "Formato", "Comentário"])
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
            # Tabela de preview
            fmt = m.get("formato") or "(padrão)"
            if m.get("formato") == "imagem" and not (m.get("image_path") and m["image_path"].exists()):
                fmt = "imagem ⚠ sem arquivo"
            self.csv_table.setItem(i, 0, QTableWidgetItem(as_time(m["start_s"])))
            self.csv_table.setItem(i, 1, QTableWidgetItem(as_time(m["end_s"])))
            self.csv_table.setItem(i, 2, QTableWidgetItem(m["label"]))
            self.csv_table.setItem(i, 3, QTableWidgetItem(fmt))
            self.csv_table.setItem(i, 4, QTableWidgetItem(m.get("comentario", "")))
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

        self._csv_use_whisper = self.csv_captions.isChecked() and command_exists("whisper")
        if self.csv_captions.isChecked() and not self._csv_use_whisper:
            QMessageBox.warning(self, APP_NAME, "Whisper não foi encontrado. Os cortes serão gerados sem legendas geradas automaticamente.")

        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        total = len(self._csv_moments)
        self.csv_progress.setRange(0, total)
        self.csv_progress.setValue(0)
        self.csv_process_btn.setDisabled(True)
        self.csv_log.clear()
        legend_note = "com legendas Whisper" if self._csv_use_whisper else "sem legendas automáticas"
        self.csv_log.appendPlainText(f"Iniciando corte de {total} momentos ({legend_note})...\n")

        self._csv_batch_index = 0
        self._csv_clip_srt = None
        self._csv_process_output = ""
        self._csv_total = total
        self._csv_errors: list[str] = []
        self._process_next_csv()

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
        """Mostra a imagem quando formato é 'imagem'."""
        if self._csv_selected_index < 0 or self._csv_selected_index >= len(self._csv_moments):
            self.csv_clip_image_label.setText("")
            return
        m = self._csv_moments[self._csv_selected_index]
        mode = m.get("_format_override") or m.get("formato") or ""
        if mode == "imagem":
            image = m.get("image_path")
            if image and Path(image).exists():
                self.csv_clip_image_label.setText(f"📷 Imagem: {image.name}")
            else:
                self.csv_clip_image_label.setText("⚠️ Imagem: não encontrada")
        else:
            self.csv_clip_image_label.setText("")

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
        if self._csv_batch_index >= self._csv_total:
            self._csv_batch_done()
            return

        m = self._csv_moments[self._csv_batch_index]
        self._csv_clip_mode = self._clip_mode(m)
        self.csv_log.appendPlainText(
            f"[{self._csv_batch_index + 1}/{self._csv_total}] {m['label']} ({self._csv_clip_mode})"
        )
        self._csv_clip_srt = None

        # Fase 1: legendas via Whisper (se ligado e disponível).
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
            self.csv_log.appendPlainText("   🎙️ Transcrevendo com Whisper…")
            command = ["whisper", str(audio_path), "--model", "small", "--language",
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
        if code == 0 and status == QProcess.ExitStatus.NormalExit and srt_path.exists():
            shorten_srt_captions(srt_path)
            self._csv_clip_srt = srt_path
        else:
            self.csv_log.appendPlainText("   ⚠️ Whisper falhou neste corte; seguindo sem legenda.")
        self._csv_export_clip()

    def _csv_export_clip(self) -> None:
        m = self._csv_moments[self._csv_batch_index]
        start, end, titulo = m["start_s"], m["end_s"], m["label"]
        mode = self._csv_clip_mode
        safe_label = "".join(c for c in titulo if c.isalnum() or c in " _-").strip()[:80]
        output = OUTPUT_DIR / f"{safe_label}.mp4"

        has_image = mode == "imagem"
        chain = build_clip_filter(mode, has_image)
        current = "base"

        # Aplicar logo se existir
        logo_applied = False
        if self.logo_path and self.logo_path.exists():
            locations = [("W-w-42", "42"), ("42", "42"), ("W-w-42", "H-h-42"), ("42", "H-h-42")]
            x, y = locations[self.csv_logo_position.currentIndex()]
            chain += f";[1:v]scale={self.csv_logo_size.value()}:-1[logo];[{current}][logo]overlay={x}:{y}[with_logo]"
            current = "with_logo"
            logo_applied = True

        # Aplicar legendas se existir
        if self._csv_clip_srt and self._csv_clip_srt.exists():
            style = ("FontName=Montserrat,FontSize=18,Bold=-1,"
                     "PrimaryColour=&H0000D7FF,OutlineColour=&H00000000,"
                     "BorderStyle=1,Outline=2.5,Shadow=0,Alignment=2,MarginV=60")
            chain += (f";[{current}]subtitles=filename='{filter_path(self._csv_clip_srt)}':"
                      f"fontsdir='{filter_path(FONT_DIR)}':force_style='{style}'[captioned]")
            current = "captioned"

        # Aplicar texto: usar título do corte
        text = titulo.strip()
        if text:
            font = "C\\:/Windows/Fonts/arialbd.ttf"
            escaped_text = escape_drawtext(text)
            chain += f";[{current}]drawtext=fontfile='{font}':text='{escaped_text}':x=(w-text_w)/2:y=h*0.12:fontsize=54:fontcolor=white:borderw=3:bordercolor=black[text]"
            current = "text"

        chain += f";[{current}]format=yuv420p[outv]"

        command = ["ffmpeg", "-y", "-ss", str(start), "-t", str(end - start), "-i", str(self.video_path)]
        if self.logo_path and self.logo_path.exists():
            command += ["-loop", "1", "-i", str(self.logo_path)]
        if has_image:
            img_idx = 2 if self.logo_path and self.logo_path.exists() else 1
            command += ["-loop", "1", "-i", str(m["image_path"])]
        command += [
            "-filter_complex", chain, "-map", "[outv]", "-map", "0:a?",
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
        self._process_next_csv()

    def _csv_batch_done(self) -> None:
        self.csv_process_btn.setDisabled(False)
        errors = len(self._csv_errors)
        ok = self._csv_total - errors
        self.csv_log.appendPlainText(f"\n🎉 Concluído! {ok} cortes salvos em:\n{OUTPUT_DIR}")
        if errors:
            self.csv_log.appendPlainText(f"⚠️ {errors} falhas: {', '.join(self._csv_errors)}")
        QMessageBox.information(self, APP_NAME, f"Processamento concluído!\n\n✅ {ok} vídeos exportados\n❌ {errors} falhas\n\nPasta: {OUTPUT_DIR}")


if __name__ == "__main__":
    app = QApplication(sys.argv)
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
