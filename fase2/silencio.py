"""Detecta silêncio via `ffmpeg silencedetect` e decide o que encolher.

Não corta a pausa fora inteira — encolhe: uma pausa longa de hesitação vira uma
pausa curta (duração-alvo), preservando o respiro natural do discurso. Cortar a
pausa inteira deixaria o corte sem ar nenhum e os jump cuts ficariam abruptos
demais.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

_INICIO = re.compile(r"silence_start:\s*(-?[\d.]+)")
_FIM = re.compile(r"silence_end:\s*(-?[\d.]+)")


def detectar_silencios(wav: Path, noise_db: float, duracao_minima: float,
                       duracao_total: float) -> list[tuple[float, float]]:
    """Devolve [(inicio, fim), ...] dos trechos de silêncio, em segundos, relativos ao wav.

    `duracao_total` fecha o último silêncio quando o áudio termina em silêncio
    (o ffmpeg não emite `silence_end` nesse caso — o silêncio só "acaba" no
    fim do arquivo).
    """
    cmd = ["ffmpeg", "-i", str(wav), "-af",
           f"silencedetect=noise={noise_db}dB:d={duracao_minima}", "-f", "null", "-"]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    inicios = [float(m) for m in _INICIO.findall(r.stderr)]
    fins = [float(m) for m in _FIM.findall(r.stderr)]
    if len(fins) < len(inicios):
        fins.append(duracao_total)
    return list(zip(inicios, fins))


def plano_de_encolhimento(silencios: list[tuple[float, float]], limiar_corte: float,
                          duracao_alvo: float, margem: float) -> list[dict]:
    """Para cada silêncio mais longo que `limiar_corte`, um corte interno que o
    encolhe para `duracao_alvo`, sempre deixando `margem` de silêncio real nas
    pontas (nunca corta rente à palavra vizinha)."""
    cortes = []
    for inicio, fim in silencios:
        duracao = fim - inicio
        if duracao <= limiar_corte:
            continue
        sobra = max(0.0, duracao_alvo - 2 * margem)
        corte_ini = inicio + margem + sobra / 2
        corte_fim = fim - margem - sobra / 2
        if corte_fim > corte_ini:
            cortes.append({"inicio": round(corte_ini, 3), "fim": round(corte_fim, 3),
                           "tipo": "silencio",
                           "motivo": f"pausa de {duracao:.2f}s encolhida para ~{duracao_alvo:.2f}s"})
    return cortes


def apara_bordas(silencios: list[tuple[float, float]], duracao_total: float,
                 max_borda: float) -> tuple[float, float]:
    """Novo (inicio, fim) do bloco depois de aparar silêncio sobrando nas pontas."""
    inicio, fim = 0.0, duracao_total
    if silencios and silencios[0][0] <= 0.05 and silencios[0][1] - silencios[0][0] > max_borda:
        inicio = silencios[0][1] - max_borda
    if silencios and silencios[-1][1] >= duracao_total - 0.05 and silencios[-1][1] - silencios[-1][0] > max_borda:
        fim = silencios[-1][0] + max_borda
    return round(max(0.0, inicio), 3), round(min(duracao_total, fim), 3)
