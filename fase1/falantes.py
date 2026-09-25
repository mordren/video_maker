"""Quem está falando quando (diarização simples), para o crop dinâmico.

O Whisper diz O QUE foi dito e QUANDO (palavras com timestamp), mas não QUEM.
Aqui:
1. Resemblyzer tira uma "impressão digital da voz" (embedding de 256 dim) a
   cada `passo_seg`, cada uma cobrindo ~1.6s de áudio ao redor.
2. Só entram as janelas que caem em fala (palavras do Whisper) — silêncio e
   música gerariam grupos falsos.
3. Agrupamento aglomerativo por similaridade de cosseno separa as vozes.
4. Cada palavra herda o falante da janela mais próxima; palavras seguidas do
   mesmo falante viram um "turno"; turnos curtos demais (tosse, "uhum",
   erro de classificação) são absorvidos pelos vizinhos.

Roda bem em CPU (modelo pequeno); com GPU fica ainda mais rápido.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

log = logging.getLogger("fase1")

TAXA = 16000


def carregar_wav(caminho: Path) -> np.ndarray:
    import librosa
    from resemblyzer.audio import normalize_volume
    from resemblyzer.hparams import audio_norm_target_dBFS
    # Não usa o preprocess_wav do Resemblyzer: ele sempre corta os silêncios
    # longos, o que desloca os tempos em relação às palavras do Whisper.
    wav, _ = librosa.load(str(caminho), sr=TAXA)
    return normalize_volume(wav, audio_norm_target_dBFS, increase_only=True)


def embeddings_no_tempo(wav: np.ndarray, passo_seg: float, device: str | None = None):
    """Devolve (tempos_centro, embeddings) — um embedding por janela."""
    import torch
    from resemblyzer import VoiceEncoder

    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    encoder = VoiceEncoder(device, verbose=False)
    rate = 1.0 / passo_seg
    _, parciais, fatias = encoder.embed_utterance(wav, return_partials=True, rate=rate)
    # cada fatia é um slice em amostras de áudio (após o encoder converter de
    # frames de mel); centro da fatia em segundos:
    centros = np.array([((f.start + f.stop) / 2) / TAXA for f in fatias])
    return centros, parciais


def _em_fala(tempos: np.ndarray, palavras: list[dict], folga: float = 0.3) -> np.ndarray:
    mascara = np.zeros(len(tempos), dtype=bool)
    for p in palavras:
        mascara |= (tempos >= p["start"] - folga) & (tempos <= p["end"] + folga)
    return mascara


def agrupar(embeddings: np.ndarray, n_falantes: int | None, limiar: float) -> np.ndarray:
    from sklearn.cluster import AgglomerativeClustering

    if n_falantes:
        modelo = AgglomerativeClustering(n_clusters=n_falantes, metric="cosine", linkage="average")
    else:
        modelo = AgglomerativeClustering(n_clusters=None, distance_threshold=limiar,
                                         metric="cosine", linkage="average")
    return modelo.fit_predict(embeddings)


def turnos(palavras: list[dict], rotulo_palavra: list[int], turno_minimo: float) -> list[dict]:
    """Agrupa palavras seguidas do mesmo falante em turnos e absorve turnos
    mais curtos que `turno_minimo` no vizinho mais longo."""
    ts: list[dict] = []
    for p, r in zip(palavras, rotulo_palavra):
        if ts and ts[-1]["falante"] == r:
            ts[-1]["fim"] = p["end"]
            ts[-1]["texto"] += " " + p["word"]
        else:
            ts.append({"inicio": p["start"], "fim": p["end"], "falante": r, "texto": p["word"]})

    mudou = True
    while mudou and len(ts) > 1:
        mudou = False
        i = min(range(len(ts)), key=lambda k: ts[k]["fim"] - ts[k]["inicio"])
        if ts[i]["fim"] - ts[i]["inicio"] >= turno_minimo:
            break
        viz = [j for j in (i - 1, i + 1) if 0 <= j < len(ts)]
        j = max(viz, key=lambda k: ts[k]["fim"] - ts[k]["inicio"])
        a, b = sorted((i, j))
        ts[a] = {"inicio": ts[a]["inicio"], "fim": ts[b]["fim"], "falante": ts[j]["falante"],
                 "texto": ts[a]["texto"] + " " + ts[b]["texto"]}
        del ts[b]
        # vizinhos que ficaram com o mesmo falante viram um turno só
        k = 0
        while k < len(ts) - 1:
            if ts[k]["falante"] == ts[k + 1]["falante"]:
                ts[k]["fim"] = ts[k + 1]["fim"]
                ts[k]["texto"] += " " + ts[k + 1]["texto"]
                del ts[k + 1]
            else:
                k += 1
        mudou = True
    for t in ts:
        t["texto"] = " ".join(t["texto"].split())
    return ts


def detectar(wav_path: Path, palavras: list[dict], n_falantes: int | None = None,
             limiar: float = 0.35, passo_seg: float = 0.25, turno_minimo: float = 1.0,
             device: str | None = None) -> list[dict]:
    """Turnos de fala: [{inicio, fim, falante, texto}], falante = 0, 1, ...
    numerado por ordem de primeira aparição."""
    if not palavras:
        return []
    wav = carregar_wav(wav_path)
    tempos, embs = embeddings_no_tempo(wav, passo_seg, device)
    mascara = _em_fala(tempos, palavras)
    if mascara.sum() < 2:
        return [{"inicio": palavras[0]["start"], "fim": palavras[-1]["end"], "falante": 0,
                 "texto": "".join(p["word"] for p in palavras).strip()}]
    tempos, embs = tempos[mascara], embs[mascara]
    rotulos = agrupar(embs, n_falantes, limiar)

    rotulo_palavra = []
    for p in palavras:
        meio = (p["start"] + p["end"]) / 2
        rotulo_palavra.append(int(rotulos[np.argmin(np.abs(tempos - meio))]))

    ts = turnos(palavras, rotulo_palavra, turno_minimo)
    # renumera por ordem de aparição (0 = quem fala primeiro)
    ordem: dict[int, int] = {}
    for t in ts:
        t["falante"] = ordem.setdefault(t["falante"], len(ordem))
    log.info("   falantes: %d, turnos: %d", len(ordem), len(ts))
    return ts


def palavras_do_whisper(json_path: Path) -> list[dict]:
    import json
    dados = json.loads(json_path.read_text(encoding="utf-8"))
    return [{"start": w["start"], "end": w["end"], "word": w["word"]}
            for s in dados["segments"] for w in s.get("words", [])]


if __name__ == "__main__":
    # Teste: python falantes.py <audio.wav> <whisper.json> [n_falantes]
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    n = int(sys.argv[3]) if len(sys.argv) > 3 else None
    for t in detectar(Path(sys.argv[1]), palavras_do_whisper(Path(sys.argv[2])), n_falantes=n):
        print(f"{t['inicio']:6.1f}-{t['fim']:6.1f}s  falante {t['falante']}:  {t['texto'][:90]}")
