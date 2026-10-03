"""Ritmo do corte pronto: onde a tela fica parada e onde a fala arrasta.

Só mede e aponta — nada aqui muda o vídeo. A regra (EDICAO_CANAL.md §3) é uma
mudança visual a cada ~7 s: mais que isso parado o espectador sai, e mudar a
cada emenda de respiro polui.

Mudança visual vem de três lugares:
- troca de plano no vídeo (`scdet` do ffmpeg no vertical, ANTES da legenda:
  com o karaokê queimado, cada palavra contaria como mudança). Pega a troca de
  quem aparece no crop LR-ASD sem mexer no crop, e os cortes do próprio vídeo
  de origem (telejornal troca de plano a cada ~1 s — por isso mudanças
  "coladas" são só informação, não alerta: não fomos nós que editamos);
- troca de foto do formato imagens (tem fade, o scdet pode não ver);
- a emenda da abertura com o corte principal (dissolve de 0,35 s).

Fala arrastada sai das palavras do Whisper (tempo de cada palavra), não do SRT:
o SRT é picotado em blocos de até 3 palavras, e a conta por bloco não diz nada.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

PADRAO = {
    "pattern_gap_max_seconds": 7.0,      # mais que isso sem mudança = parado
    "visual_change_min_gap": 2.5,        # menos que isso entre mudanças = colado
    "speech_density_min": 1.5,           # palavras/s abaixo disso = arrastado
    "speech_density_min_duration": 2.0,  # trecho arrastado mais curto que isso não conta
    "scdet_limiar": 10.0,
}
JANELA = 3.0   # s — "densidade de informação a cada 3 segundos"
PASSO = 0.5    # s entre uma janela e a próxima
_SCD = re.compile(r"lavfi\.scd\.time:\s*([\d.]+)")


def config(cfg: dict | None = None) -> dict:
    final = dict(PADRAO)
    for chave, valor in (cfg or {}).items():
        if chave in PADRAO and valor is not None:
            final[chave] = float(valor)
    return final


def cortes_de_cena(video: Path, limiar: float = PADRAO["scdet_limiar"]) -> list[float]:
    """Tempos (s) em que o quadro muda de plano. Reduz para 108 px de largura
    antes de comparar: fica mais rápido (~2 s num clipe de 70 s) e ignora
    detalhe pequeno."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(video), "-an",
                        "-vf", f"scale=108:-2,scdet=t={limiar}", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou no scdet: {r.stderr[-300:]}")
    return [float(t) for t in _SCD.findall(r.stderr)]


def mudancas_visuais(cenas: list[float], trocas_fotos: list[float] | None = None,
                     fim_gancho: float = 0.0) -> list[float]:
    """Junta as fontes, ordena e tira as repetidas (menos de 0,5 s entre si).
    O tempo 0 não é mudança: é o começo."""
    todos = sorted(t for t in [*cenas, *(trocas_fotos or []), *([fim_gancho] if fim_gancho > 0 else [])]
                   if t > 0.1)
    unicos: list[float] = []
    for t in todos:
        if not unicos or t - unicos[-1] >= 0.5:
            unicos.append(round(t, 2))
    return unicos


def trechos_parados(mudancas: list[float], duracao: float, max_gap: float) -> list[dict]:
    """Trechos com mais de `max_gap` s sem nenhuma mudança visual."""
    marcos = [0.0, *mudancas, duracao]
    return [{"de": round(a, 1), "ate": round(b, 1), "dur": round(b - a, 1)}
            for a, b in zip(marcos, marcos[1:]) if b - a > max_gap]


def mudancas_coladas(mudancas: list[float], min_gap: float) -> list[dict]:
    """Mudanças a menos de `min_gap` s da anterior."""
    return [{"de": a, "ate": b, "dur": round(b - a, 1)}
            for a, b in zip(mudancas, mudancas[1:]) if b - a < min_gap]


def fala_arrastada(palavras: list[dict], min_pps: float, min_dur: float,
                   inicio: float = 0.0) -> list[dict]:
    """Trechos em que a fala fica abaixo de `min_pps` palavras/s.

    Janela de 3 s andando de 0,5 em 0,5 s; uma palavra conta na janela pelo
    meio dela. Janelas seguidas abaixo do limite viram um trecho só. Antes de
    `inicio` (a abertura) não conta: ela é curta de propósito."""
    ws = [w for w in palavras if "start" in w and "end" in w]
    if not ws:
        return []
    meios = [((w["start"] + w["end"]) / 2, w.get("word", "").strip()) for w in ws]
    fim = ws[-1]["end"]
    trechos: list[list[float]] = []
    t = max(inicio, ws[0]["start"])
    while t + JANELA <= fim + 1e-6:
        n = sum(1 for m, _ in meios if t <= m < t + JANELA)
        if n / JANELA < min_pps:
            if trechos and t <= trechos[-1][1]:
                trechos[-1][1] = t + JANELA
            else:
                trechos.append([t, t + JANELA])
        t += PASSO
    saida = []
    for a, b in trechos:
        if b - a < min_dur:
            continue
        dentro = [p for m, p in meios if a <= m < b]
        if len(dentro) / (b - a) >= min_pps:   # janelas juntas podem passar do limite na média
            continue
        saida.append({"de": round(a, 1), "ate": round(b, 1), "dur": round(b - a, 1),
                      "palavras": len(dentro), "pps": round(len(dentro) / (b - a), 2),
                      "texto": " ".join(dentro)[:80]})
    return saida


def relatorio(vertical: Path, duracao: float, palavras: list[dict], fim_gancho: float = 0.0,
              trocas_fotos: list[float] | None = None, cfg: dict | None = None) -> dict:
    """Tudo junto, para o card de revisão. Nunca levanta: sem medida, {"erro"}."""
    c = config(cfg)
    try:
        mudancas = mudancas_visuais(cortes_de_cena(vertical, c["scdet_limiar"]), trocas_fotos, fim_gancho)
        return {
            "mudancas": len(mudancas),
            "parados": trechos_parados(mudancas, duracao, c["pattern_gap_max_seconds"]),
            "colados": mudancas_coladas(mudancas, c["visual_change_min_gap"]),
            "arrastados": fala_arrastada(palavras or [], c["speech_density_min"],
                                         c["speech_density_min_duration"], fim_gancho),
            "duracao": round(duracao, 1),
        }
    except Exception as exc:  # noqa: BLE001 — conferência não derruba o corte
        return {"erro": str(exc)[:300]}


def ler_trocas_fotos(pasta: Path) -> list[float]:
    """Tempos das trocas de foto que o formato imagens gravou (ou [])."""
    arq = pasta / "trocas_fotos.json"
    try:
        return [float(t) for t in json.loads(arq.read_text(encoding="utf-8"))]
    except (OSError, ValueError, TypeError):
        return []


def linha_log(r: dict) -> str:
    if not r:
        return ""
    if "erro" in r:
        return f"ritmo: não deu para medir ({r['erro']})"
    partes = [f"ritmo: {r['mudancas']} mudança(s) visual(is) em {r['duracao']:.0f} s"]
    partes += [f"parado {p['de']:.1f}→{p['ate']:.1f} ({p['dur']:.1f} s)" for p in r["parados"]]
    if r["colados"]:
        partes.append(f"{len(r['colados'])} colada(s)")
    if r["arrastados"]:
        partes.append(f"fala arrastada {len(r['arrastados'])}")
    return " · ".join(partes) + (" ⚠️" if r["parados"] else " ✅")
