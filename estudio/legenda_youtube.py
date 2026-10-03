"""Legenda do YouTube para a Fase 1 — pela página do vídeo, quando o yt-dlp não consegue.

O endereço de legendas do YouTube (timedtext) passou a responder 429 para o
IP de casa (visto em 29/09/2026). Com o PO token de legenda no pedido também,
e até num navegador de verdade. O painel "Transcrição" da página do vídeo
usa outro caminho (youtubei/get_transcript) e continuou funcionando do mesmo
IP. Este script abre a página num Chromium sem janela, abre esse painel e
grava as falas como SRT.

    python legenda_youtube.py <url> <saida.srt>

Sai com código 0 se gravou, 1 se não achou transcrição e 2 em erro.
Roda como processo à parte, para o "cancelar" do Estúdio conseguir matar.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Os dois formatos do painel (o novo "view-model" e o antigo "renderer").
_SEGMENTOS = "transcript-segment-view-model, ytd-transcript-segment-renderer"

_ABRIR_PAINEL = """() => {
  const b = [...document.querySelectorAll('button')].find(x =>
    /show transcript|mostrar transcri/i.test((x.innerText || '') + ' ' + (x.getAttribute('aria-label') || '')));
  if (b) { b.click(); return true; }
  return false;
}"""

# Cada segmento tem "m:ss" (ou "h:mm:ss"), um rótulo falado ("12 seconds")
# para leitor de tela e o texto; fica só o início em segundos e o texto.
_LER_SEGMENTOS = """(sel) => [...document.querySelectorAll(sel)].map(s => {
  const partes = s.innerText.split('\\n').map(x => x.trim()).filter(Boolean);
  const ts = partes[0] || '';
  const seg = ts.split(':').map(Number).reduce((a, b) => a * 60 + b, 0);
  const texto = partes.slice(1).filter(x => !/^(\\d+ (hours?|minutes?|seconds?|horas?|minutos?|segundos?),? ?)+$/i.test(x)).join(' ');
  return [seg, texto];
})"""


def _hms(s: float) -> str:
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def transcricao_da_pagina(url: str, espera_s: float = 60) -> tuple[list[tuple[float, str]], float]:
    """(falas [(início, texto)], duração do vídeo em s) — lista vazia se não tiver."""
    # O Chromium do Playwright no .133 fica dentro do pacote (o Publicador roda
    # com isso também); o serviço do Estúdio não define essa variável.
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")
    from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

    with sync_playwright() as p:
        # channel="chromium": o Chromium completo no modo sem janela novo — é o
        # que já está instalado para o Publicador (o "headless shell" não está).
        navegador = p.chromium.launch(headless=True, channel="chromium")
        try:
            # Em inglês para o botão e os rótulos não dependerem do idioma do servidor.
            pagina = navegador.new_context(locale="en-US", viewport={"width": 1280, "height": 900}).new_page()
            pagina.goto(url, wait_until="domcontentloaded", timeout=int(espera_s * 1000))
            # O botão "Show transcript" só existe depois que a descrição monta.
            try:
                pagina.wait_for_function(_ABRIR_PAINEL, timeout=int(espera_s * 1000), polling=1000)
            except PWTimeout:
                return [], 0.0
            try:
                pagina.wait_for_function(f"() => document.querySelectorAll('{_SEGMENTOS}').length > 0",
                                         timeout=int(espera_s * 1000), polling=500)
            except PWTimeout:
                return [], 0.0
            # Espera a lista parar de crescer (em live longa ela chega em mais de uma leva).
            anterior = -1
            for _ in range(30):
                n = pagina.evaluate(f"() => document.querySelectorAll('{_SEGMENTOS}').length")
                if n == anterior:
                    break
                anterior = n
                pagina.wait_for_timeout(1500)
            falas = [(float(s), t.strip()) for s, t in pagina.evaluate(_LER_SEGMENTOS, _SEGMENTOS) if t.strip()]
            duracao = pagina.evaluate(
                "() => Number(window.ytInitialPlayerResponse?.videoDetails?.lengthSeconds || 0)") or 0.0
            return sorted(falas), float(duracao)
        finally:
            navegador.close()


def gravar_srt(falas: list[tuple[float, str]], duracao: float, saida: Path) -> None:
    """O painel só dá o início (em segundos inteiros): o fim é o próximo início,
    sem esticar por cima de pausa longa (teto proporcional ao texto)."""
    blocos = []
    for k, (ini, texto) in enumerate(falas):
        prox = falas[k + 1][0] if k + 1 < len(falas) else (duracao or ini + 10)
        fim = min(prox, ini + max(3.0, len(texto.split()) * 0.5 + 1.5))
        if fim <= ini:
            fim = ini + 1.0
        blocos.append(f"{k + 1}\n{_hms(ini)} --> {_hms(fim)}\n{texto}\n")
    saida.write_text("\n".join(blocos), encoding="utf-8")


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    url, saida = sys.argv[1], Path(sys.argv[2])
    try:
        falas, duracao = transcricao_da_pagina(url)
    except Exception as exc:  # noqa: BLE001 — é plano B: qualquer falha só vira aviso
        print(f"transcrição pela página falhou: {type(exc).__name__}: {exc}")
        return 2
    if len(falas) < 5:
        print(f"a página não tem transcrição utilizável ({len(falas)} fala(s))")
        return 1
    gravar_srt(falas, duracao, saida)
    print(f"transcrição pela página: {len(falas)} falas, até {falas[-1][0] / 60:.0f} min → {saida.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
