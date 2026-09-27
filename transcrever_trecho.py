"""Transcreve um trecho para o app: Whisper pela API, com o local de reserva.

Roda como processo à parte (o app chama por QProcess, igual chamava o
whisper.exe), então cancelar, ler o log e o `finished` continuam como antes.
Grava <pasta>/<nome do wav>.srt e, pela API, também o .json no formato do
Whisper (palavra a palavra), que a censura usa para achar o segundo exato.

Pela API (openai/whisper-large-v3-turbo via OpenRouter, a mesma chave do
fase1/.env que o Estúdio usa) 30s de áudio saem em poucos segundos; o Whisper
local neste notebook (PyTorch x64 emulado no Snapdragon) levou 54s em 30s de
áudio, medido em 27/09/2026. Sem chave ou se a API falhar, cai no local.

Uso:
    transcrever_trecho.py AUDIO.wav --pasta DIR [--de VIDEO --inicio S --duracao S]
                          [--modelo small] [--palavras]
Com --de, extrai antes o áudio do trecho para AUDIO.wav (assim nem a extração
roda na thread da interface).
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from utils import srt_timestamp, whisper_path

sys.path.append(str(Path(__file__).resolve().parent / "fase1"))


def extrair_audio(video: Path, inicio: float, duracao: float | None, wav: Path) -> None:
    trecho = ["-ss", str(inicio)] + (["-t", str(duracao)] if duracao else [])
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *trecho, "-i", str(video),
                    "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)


def frases(segs: list[dict], pausa: float = 0.4, max_s: float = 6.0) -> list[tuple[float, float, str]]:
    """Reparte a transcrição da API em frases curtas pelos tempos das palavras.

    A API devolve blocos longos (um só de 18s num teste de 20s), e o app
    (shorten_srt_captions) divide o tempo de cada bloco na proporção do texto
    — numa pausa no meio, a legenda adiantava. Quebrando aqui em pausa,
    ponto final ou ~6s, cada bloco já começa e termina no tempo certo, como
    saía do Whisper local. Segmento sem palavras entra inteiro.
    """
    saida: list[tuple[float, float, str]] = []
    for s in segs:
        palavras = s.get("words") or []
        if not palavras:
            if s["text"].strip():
                saida.append((s["start"], s["end"], s["text"].strip()))
            continue
        atual: list[dict] = []
        for w in palavras:
            if atual and (w["start"] - atual[-1]["end"] > pausa
                          or w["end"] - atual[0]["start"] > max_s
                          or (atual[-1]["word"][-1:] in ".?!" and len(atual) >= 3)):
                saida.append((atual[0]["start"], atual[-1]["end"], " ".join(x["word"] for x in atual)))
                atual = []
            atual.append(w)
        if atual:
            saida.append((atual[0]["start"], atual[-1]["end"], " ".join(x["word"] for x in atual)))
    return saida


def pela_api(wav: Path, pasta: Path) -> bool:
    """Tenta a API; devolve False (sem gravar nada) se não tiver chave ou falhar."""
    import transcricao
    from openrouter import APIError, api_key
    try:
        api_key()
    except APIError:
        print("ℹ️ Sem OPENROUTER_API_KEY no fase1/.env; usando o Whisper local.", flush=True)
        return False
    inicio = time.time()
    try:
        segs = transcricao.rodar_whisper_api(wav, "pt")
    except Exception as exc:  # noqa: BLE001 — qualquer falha da API cai no local
        print(f"⚠️ Whisper pela API falhou ({exc}); usando o Whisper local.", flush=True)
        return False
    (pasta / f"{wav.stem}.json").write_text(
        json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
    linhas: list[str] = []
    for n, (ini, fim, texto) in enumerate(frases(segs), 1):
        linhas += [str(n), f"{srt_timestamp(ini)} --> {srt_timestamp(fim)}", texto, ""]
    (pasta / f"{wav.stem}.srt").write_text("\n".join(linhas), encoding="utf-8")
    print(f"✅ Transcrito pela API (whisper-large-v3-turbo) em {time.time() - inicio:.1f}s.", flush=True)
    return True


def whisper_local(wav: Path, pasta: Path, modelo: str, palavras: bool) -> int:
    binario = whisper_path()
    if not binario:
        print("⚠️ Whisper local não encontrado.", flush=True)
        return 1
    # Palavra por palavra deixa o local mais lento: só quando a censura vai
    # silenciar o áudio (mesma regra que o app tinha em whisper_command).
    cmd = [binario, str(wav), "--model", modelo, "--language", "Portuguese",
           "--task", "transcribe", "--output_dir", str(pasta)]
    cmd += ["--word_timestamps", "True", "--output_format", "all"] if palavras \
        else ["--output_format", "srt"]
    print(f"🎙️ Whisper local (modelo {modelo})…", flush=True)
    return subprocess.run(cmd).returncode


def main() -> int:
    # O app lê a saída como UTF-8; sem isto o Windows usaria cp1252 no pipe e
    # os emojis do log derrubariam o print.
    sys.stdout.reconfigure(encoding="utf-8")
    # O cliente do OpenRouter avisa pelo logger "fase1" quando a chamada
    # falha e ele vai tentar de novo (espera 2s, 4s…): no log do app, isso
    # explica uma transcrição que demorou em vez de parecer travada.
    logging.basicConfig(stream=sys.stdout, level=logging.WARNING, format="⏳ %(message)s")
    p = argparse.ArgumentParser()
    p.add_argument("wav", type=Path)
    p.add_argument("--pasta", type=Path, required=True)
    p.add_argument("--de", type=Path)
    p.add_argument("--inicio", type=float, default=0.0)
    p.add_argument("--duracao", type=float)
    p.add_argument("--modelo", default="small")
    p.add_argument("--palavras", action="store_true")
    a = p.parse_args()

    if a.de:
        try:
            extrair_audio(a.de, a.inicio, a.duracao, a.wav)
        except subprocess.CalledProcessError:
            print("⚠️ Não foi possível extrair o áudio do trecho.", flush=True)
            return 2
    if pela_api(a.wav, a.pasta):
        return 0
    return whisper_local(a.wav, a.pasta, a.modelo, a.palavras)


if __name__ == "__main__":
    sys.exit(main())
