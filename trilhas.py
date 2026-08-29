"""Trilhas de fundo: catálogo da pasta e mixagem sob a fala.

A pasta é a fonte da verdade — o app lista o que está lá, e o nome do arquivo
é o rótulo do clima ("05-confronto.mp3" -> "confronto"). Assim dá para
acrescentar ou tirar música sem tocar no código, e é essa lista de rótulos que
vai para a IA escolher.

A mixagem faz três coisas, nesta ordem, e todas importam:

1. `loudnorm` iguala o volume das faixas. Sem isso um drone chega sussurrando e
   uma orquestral chega gritando, e a escolha da IA viraria loteria de volume.
2. `sidechaincompress` abaixa a música sozinha quando alguém fala (ducking).
   Volume fixo ou some, ou come a fala.
3. `alimiter` segura o pico da soma, para a soma das duas não estourar.

Sem Qt, para poder ser testado solto.
"""

from __future__ import annotations

import re
from pathlib import Path

# Pasta padrão das trilhas. Fica fora do projeto de propósito: são dezenas de
# MB que não têm por que entrar no git nem subir para o OneDrive.
DEFAULT_MUSIC_DIR = Path.home() / "Videos" / "CortaLegenda" / "trilhas"

EXTENSIONS = (".mp3", ".m4a", ".wav", ".ogg", ".flac")

# Volume da trilha, em LUFS, antes do ducking. Quanto maior (mais perto de
# zero), mais presente a música. -27 deixa a fala mandando com folga.
MUSIC_LUFS = -27


def label_for(path: Path) -> str:
    """Rótulo do clima a partir do nome do arquivo.

    "05-confronto.mp3" -> "confronto"; "grave orquestral.mp3" -> "grave orquestral".
    O número da frente é só para ordenar na pasta, não faz parte do rótulo.
    """
    nome = Path(path).stem
    nome = re.sub(r"^\s*\d+\s*[-_. ]\s*", "", nome)   # tira "05-" do começo
    return nome.replace("_", " ").replace("-", " ").strip().lower()


def list_tracks(folder: Path | None = None) -> list[tuple[str, Path]]:
    """Trilhas disponíveis, como (rótulo, caminho), em ordem de nome."""
    folder = Path(folder) if folder else DEFAULT_MUSIC_DIR
    if not folder.is_dir():
        return []
    faixas = [p for p in sorted(folder.iterdir())
              if p.is_file() and p.suffix.lower() in EXTENSIONS]
    return [(label_for(p), p) for p in faixas]


def find_track(label: str, folder: Path | None = None) -> Path | None:
    """Acha a trilha pelo rótulo que a IA devolveu.

    A IA responde em texto livre, então a comparação é frouxa de propósito:
    sem caixa, e aceitando que ela devolva o nome do arquivo inteiro ou só uma
    parte do rótulo. Devolve None quando não dá para ter certeza.
    """
    if not label:
        return None
    alvo = label.strip().lower()
    faixas = list_tracks(folder)
    if not faixas:
        return None
    for rotulo, caminho in faixas:                     # igual
        if alvo == rotulo or alvo == caminho.stem.lower():
            return caminho
    for rotulo, caminho in faixas:                     # contido
        if alvo in rotulo or rotulo in alvo:
            return caminho
    return None


def mix_chain(speech_label: str, music_input: int, out_label: str = "aout",
              lufs: int = MUSIC_LUFS, duration: float = 0.0) -> str:
    """Cadeia de filtros que põe a trilha sob a fala, com ducking.

    `speech_label` é o rótulo da fala já pronta (por exemplo "0:a" ou um
    "[sp]" que veio da censura); `music_input` é o índice da entrada da música.
    Com `duration`, a música ganha um fade de saída no fim do corte.
    """
    fades = "afade=t=in:d=1.5"
    if duration > 4:
        fades += f",afade=t=out:st={duration - 3:.2f}:d=3"
    return (
        f"[{music_input}:a]loudnorm=I={lufs}:TP=-9:LRA=11,{fades}[bg];"
        f"[bg][{speech_label}]sidechaincompress="
        f"threshold=0.03:ratio=8:attack=15:release=350[duck];"
        f"[{speech_label}][duck]amix=inputs=2:duration=first:normalize=0[premix];"
        f"[premix]alimiter=limit=0.95[{out_label}]"
    )
