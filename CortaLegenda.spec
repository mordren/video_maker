# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec para Corta+Legenda (Video Maker).
Bundla o app GUI com PySide6, whisper, yt-dlp, python-vlc.
"""
import sys
from pathlib import Path

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('assets', 'assets'),  # Inclui logo, fontes, etc.
    ],
    hiddenimports=[
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'vlc',
        'yt_dlp',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludedimports=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='Corta+Legenda',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # Sem console (modo GUI puro)
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,  # Pode adicionar um .ico aqui se tiver
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CortaLegenda',
)
