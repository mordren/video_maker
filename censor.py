"""Censura de palavrões e palavras bloqueáveis nos cortes.

Duas coisas acontecem com cada palavra da lista:

- **Áudio**: o volume vai a zero só nos milissegundos da palavra. O tempo exato
  sai do JSON do Whisper (`--word_timestamps`), que marca palavra por palavra.
  Quando não há esse JSON — por exemplo numa legenda pronta baixada junto com o
  vídeo —, o tempo é estimado pela posição do caractere dentro do bloco.
- **Legenda**: a palavra ganha uma grafia alternativa (matar → m4tar). Continua
  legível para quem assiste, mas deixa de casar com o filtro automático da
  plataforma, que lê o texto queimado e a transcrição.

Só a lógica, sem interface e sem dependências externas — dá para testar solto,
fora do app. (O SRT é lido pelos ajudantes de `utils`, importados sob demanda.)
"""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

# Lista inicial, editável na interface. São os palavrões mais comuns; o usuário
# acrescenta as "palavras bloqueáveis" do nicho dele (matar, arma, droga...).
DEFAULT_WORDS = [
    "buceta", "caralho", "cacete", "foda", "foder", "fodido", "merda",
    "porra", "puta", "puto", "putaria", "viado", "corno", "cu", "babaca",
    "arrombado", "desgraça", "filho da puta", "fdp", "piroca", "escroto",
    "otário", "imbecil", "vagabundo", "safado", "bosta", "pqp",
]

# Trocas que mantêm a palavra legível mas quebram o casamento exato do filtro.
_LEET = {"a": "4", "e": "3", "i": "1", "o": "0", "s": "5"}

# Um "token" de palavra: letras (com acento) e números.
_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+")

# Margem de segurança em volta da palavra, em segundos. O começo da consoante
# costuma vazar um pouco antes da marcação do Whisper.
_PAD = 0.06


def strip_accents(text: str) -> str:
    """'Ação' -> 'acao' — para comparar sem depender de acentuação."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Forma usada nas comparações: sem acento e em minúsculas."""
    return strip_accents(text).lower()


def parse_words(raw: str) -> list[str]:
    """Lê a lista da interface (uma por linha, ou separadas por vírgula)."""
    parts = re.split(r"[\n,;]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


def _matches(token: str, patterns: list[str]) -> bool:
    """O token casa com alguma entrada da lista?

    Comparação sem acento e sem caixa. Uma entrada terminada em `*` casa por
    começo da palavra — `put*` pega puta, putas, putaria, sem listar cada uma.
    """
    norm = normalize(token)
    for pattern in patterns:
        p = normalize(pattern)
        if p.endswith("*"):
            if p[:-1] and norm.startswith(p[:-1]):
                return True
        elif norm == p:
            return True
    return False


def _expand(patterns: list[str]) -> tuple[list[str], list[list[str]]]:
    """Separa a lista em entradas de uma palavra e entradas com espaço.

    'filho da puta' precisa ser procurado como sequência de tokens, não como
    token único — daí a segunda lista, já quebrada em palavras.
    """
    single: list[str] = []
    phrases: list[list[str]] = []
    for pattern in patterns:
        pieces = pattern.split()
        if len(pieces) > 1:
            phrases.append(pieces)
        elif pieces:
            single.append(pieces[0])
    return single, phrases


def disguise(word: str) -> str:
    """matar -> m4tar. Troca só o primeiro caractere que tiver equivalente.

    Uma troca basta para o filtro automático não casar a palavra, e mantém a
    leitura fácil — trocar tudo ('m4t4r') atrapalha quem está assistindo.
    """
    for i, char in enumerate(word):
        swap = _LEET.get(char.lower())
        if swap:
            return word[:i] + swap + word[i + 1:]
    # Sem vogal nem 's' (ex.: 'fdp'): esconde o segundo caractere.
    if len(word) > 1:
        return word[0] + "*" + word[2:]
    return "*"


def _token_spans(text: str) -> list[tuple[int, int, str]]:
    """Posições (início, fim, texto) de cada palavra dentro da linha."""
    return [(m.start(), m.end(), m.group()) for m in _TOKEN.finditer(text)]


def _disguise_span(text: str) -> str:
    """Disfarça cada palavra do trecho casado, preservando o que há entre elas.

    Numa expressão como 'filho da puta' cada palavra é tratada por si — do
    contrário só a primeira mudaria e 'puta' continuaria escrito por extenso.
    """
    out: list[str] = []
    cursor = 0
    for start, end, token in _token_spans(text):
        out.append(text[cursor:start])
        out.append(disguise(token))
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def _hits(text: str, patterns: list[str]) -> list[tuple[int, int]]:
    """Trechos de `text` que casam com a lista, como (início, fim) em caracteres."""
    single, phrases = _expand(patterns)
    tokens = _token_spans(text)
    found: list[tuple[int, int]] = []
    used: set[int] = set()

    # Primeiro as expressões com espaço, que têm prioridade sobre a palavra solta.
    for pieces in phrases:
        n = len(pieces)
        for i in range(len(tokens) - n + 1):
            if all(_matches(tokens[i + k][2], [pieces[k]]) for k in range(n)):
                found.append((tokens[i][0], tokens[i + n - 1][1]))
                used.update(range(i, i + n))

    for i, (start, end, token) in enumerate(tokens):
        if i not in used and _matches(token, single):
            found.append((start, end))
    return sorted(found)


def censor_text(text: str, patterns: list[str]) -> tuple[str, int]:
    """Aplica a grafia alternativa nas palavras da lista. Devolve (texto, quantas)."""
    if not patterns or not text:
        return text, 0
    hits = _hits(text, patterns)
    if not hits:
        return text, 0
    out: list[str] = []
    cursor = 0
    for start, end in hits:
        out.append(text[cursor:start])
        out.append(_disguise_span(text[start:end]))
        cursor = end
    out.append(text[cursor:])
    return "".join(out), len(hits)


def censor_srt(path: Path, patterns: list[str]) -> int:
    """Reescreve o SRT com as palavras disfarçadas. Devolve quantas trocou.

    Só o texto muda; os tempos são copiados como estão, então a legenda nunca
    sai de sincronia com o áudio por causa da censura.
    """
    from utils import parse_srt_segments, srt_timestamp

    segments = parse_srt_segments(path)
    if not segments:
        return 0
    lines: list[str] = []
    total = 0
    for i, (start, end, text) in enumerate(segments, start=1):
        new_text, count = censor_text(text, patterns)
        total += count
        lines.extend([str(i), f"{srt_timestamp(start)} --> {srt_timestamp(end)}",
                      new_text, ""])
    if total:
        path.write_text("\n".join(lines), encoding="utf-8")
    return total


# ──────────────────────────────────────────────────────────────
#  Onde silenciar o áudio
# ──────────────────────────────────────────────────────────────

def merge_ranges(ranges: list[tuple[float, float]],
                 gap: float = 0.05) -> list[tuple[float, float]]:
    """Junta faixas que se tocam, para não repetir condição no filtro."""
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [list(ordered[0])]
    for start, end in ordered[1:]:
        if start - merged[-1][1] <= gap:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(round(s, 3), round(e, 3)) for s, e in merged]


def spans_from_whisper_json(path: Path, patterns: list[str]) -> list[tuple[float, float]]:
    """Tempos exatos das palavras da lista, lidos do JSON do Whisper.

    Só funciona quando o Whisper rodou com `--word_timestamps True`; sem isso o
    JSON não traz a chave 'words' e a função devolve lista vazia.
    """
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    single, phrases = _expand(patterns)
    ranges: list[tuple[float, float]] = []
    for segment in data.get("segments", []):
        words = segment.get("words") or []
        clean = []
        for w in words:
            token = str(w.get("word", "")).strip()
            start, end = w.get("start"), w.get("end")
            if token and start is not None and end is not None:
                clean.append((float(start), float(end), token))
        for i, (start, end, token) in enumerate(clean):
            hit = _matches(token.strip(".,!?;:…\"'"), single)
            if not hit:
                for pieces in phrases:
                    n = len(pieces)
                    if i + n <= len(clean) and all(
                        _matches(clean[i + k][2].strip(".,!?;:…\"'"), [pieces[k]])
                        for k in range(n)
                    ):
                        ranges.append((start - _PAD, clean[i + n - 1][1] + _PAD))
                        break
                continue
            ranges.append((start - _PAD, end + _PAD))
    return merge_ranges([(max(0.0, s), e) for s, e in ranges])


def spans_from_srt(path: Path, patterns: list[str]) -> list[tuple[float, float]]:
    """Estimativa dos tempos a partir do SRT, quando não há marcação por palavra.

    O bloco é repartido proporcionalmente ao número de caracteres, então a
    janela é aproximada — daí uma margem maior que a do JSON do Whisper.
    """
    from utils import parse_srt_segments

    ranges: list[tuple[float, float]] = []
    for start, end, text in parse_srt_segments(path):
        if not text:
            continue
        duration = max(0.0, end - start)
        for hit_start, hit_end in _hits(text, patterns):
            a = start + duration * hit_start / len(text)
            b = start + duration * hit_end / len(text)
            ranges.append((max(start, a - _PAD * 2), min(end, b + _PAD * 2)))
    return merge_ranges(ranges)


def mute_spans(srt_path: Path, patterns: list[str]) -> list[tuple[float, float]]:
    """Faixas a silenciar no corte, em segundos contados do início do corte.

    Prefere o JSON do Whisper (palavra a palavra) que fica ao lado do SRT; sem
    ele, cai na estimativa pelo próprio SRT.
    """
    if not patterns:
        return []
    srt_path = Path(srt_path)
    json_path = srt_path.with_suffix(".json")
    if json_path.exists():
        spans = spans_from_whisper_json(json_path, patterns)
        if spans:
            return spans
    return spans_from_srt(srt_path, patterns)


def mute_filter(spans: list[tuple[float, float]]) -> str:
    """Filtro de áudio do FFmpeg que zera o volume nas faixas dadas.

    O `+` entre os `between` funciona como "ou" nas expressões do FFmpeg: basta
    uma faixa bater para o volume ir a zero.
    """
    if not spans:
        return ""
    condition = "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in spans)
    return f"volume=enable='{condition}':volume=0"
