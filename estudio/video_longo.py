"""Produção do vídeo longo (15-20 min) que a Fase 1 apontou (fase1/trechos_longos.py).

Sem crop, sem legenda, sem GC: o vídeo fica em 16:9, dentro da moldura do
canal (moldura.py). O que muda em relação à live:

- saem os pedaços que o DeepSeek marcou em "remover" (superchat fora do
  tema, problema técnico, recado do canal);
- sai o silêncio longo: pausa de mais de 4 s vira 0,5 s. Não é o
  compacto dos shorts (que tira quase toda respiração) — num vídeo longo a
  fala precisa respirar, só o "buraco" incomoda;
- o áudio é nivelado a -14 LUFS (e conferido pelo audio_qa, como nos shorts).

No começo vai um gancho de ~5 s em preto e branco (a melhor parte do vídeo,
como a abertura dos shorts), colado sem reencode, e no fim o CTA: o do canal (aba Canais; cta_cortador.py) ou,
sem ele, o cta/cta.mp4 global (voz + música, sem texto).

Tudo num passo só do ffmpeg: select/aselect com os pedaços que ficam, a
moldura por cima e o h264 na placa (NVENC).
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import audio_qa
import moldura
import renderiza
from silencio import detectar_silencios

log = logging.getLogger("estudio")

SILENCIO_DB = -30          # mesmo limiar da Fase 2 (config_cortes.yaml)
SILENCIO_LONGO = 4.0       # pausa acima disso é cortada...
SILENCIO_SOBRA = 0.5       # ...e vira isso (metade de cada lado)
PEDACO_MINIMO = 0.4        # sobra menor que isso entre dois cortes some junto


def _duracao(arquivo: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", str(arquivo)], capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def pedacos_que_ficam(duracao: float, remover: list[tuple[float, float]],
                      silencios: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """[0, duracao] menos as remoções e o miolo dos silêncios longos."""
    fora = [(a, b) for a, b in remover]
    meio = SILENCIO_SOBRA / 2
    fora += [(a + meio, b - meio) for a, b in silencios if b - a > SILENCIO_LONGO]
    fora = sorted((max(0.0, a), min(duracao, b)) for a, b in fora if b > a)
    ficam, t = [], 0.0
    for a, b in fora:
        if a > t:
            ficam.append((t, a))
        t = max(t, b)
    if t < duracao:
        ficam.append((t, duracao))
    return [(a, b) for a, b in ficam if b - a >= PEDACO_MINIMO]


PREVIA_ALVO = 15.0         # duração da prévia (corte inicial) do vídeo longo
PREVIA_MAXIMO = 18.0
PREVIA_PASSO = 25.0        # as janelas candidatas começam a cada ~25 s do trecho
PREVIA_MAX_CANDIDATOS = 60
CTA = Path(__file__).resolve().parent / "cta" / "cta.mp4"   # 6 s, voz + música, sem texto
GANCHO_ALVO = 4.0          # abertura em P&B do vídeo final: ~4-7 s do começo da melhor parte
GANCHO_MAXIMO = 7.0


def janelas_previa(segs: list[dict], inicio: float, fim: float,
                   remover: list[dict]) -> list[dict]:
    """Janelas candidatas de ~15 s à prévia: começam no início de uma fala
    (segmento da transcrição), terminam no fim de outra, e não pegam nada do
    que vai sair do vídeo. [{inicio, fim, texto}], no tempo da live."""
    fora = [(r["inicio"], r["fim"]) for r in remover]
    dentro = [s for s in segs if s["start"] >= inicio and s["end"] <= fim and s["text"].strip()]
    passo = max(PREVIA_PASSO, (fim - inicio) / PREVIA_MAX_CANDIDATOS)
    janelas, proximo = [], inicio
    for i, s in enumerate(dentro):
        if s["start"] < proximo:
            continue
        fim_j, j = s["end"], i
        while fim_j - s["start"] < PREVIA_ALVO - 1 and j + 1 < len(dentro):
            j += 1
            fim_j = dentro[j]["end"]
        if fim_j - s["start"] < PREVIA_ALVO - 3:
            break                                  # sobrou pouco trecho: sem janela cheia
        fim_j = min(fim_j, s["start"] + PREVIA_MAXIMO)
        if any(a < fim_j and b > s["start"] for a, b in fora):
            continue
        texto = " ".join(x["text"].strip() for x in dentro[i:j + 1] if x["start"] < fim_j)
        # o gancho do final (abertura em P&B) é o começo da janela, até o fim de uma fala
        gancho = next((x["end"] for x in dentro[i:j + 1] if x["end"] - s["start"] >= GANCHO_ALVO), fim_j)
        janelas.append({"inicio": round(s["start"], 3), "fim": round(fim_j, 3), "texto": texto,
                        "gancho_fim": round(min(gancho, fim_j, s["start"] + GANCHO_MAXIMO), 3)})
        proximo = s["start"] + passo
    return janelas


def cortar_previa(fonte: Path, inicio: float, fim: float, destino: Path) -> None:
    """Recorte preciso (recodificado) de `inicio` a `fim` da live, 16:9, sem legenda."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    _codificar(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{inicio:.3f}", "-t", f"{fim - inicio:.3f}",
                "-i", str(fonte)], destino)


def _codificar(cmd_base: list[str], saida: Path) -> None:
    # cq 21 deu ~4,5 Mbps numa live de estúdio (18 min = 595 MB); o YouTube
    # recomenda ~8 Mbps em 1080p30. O teto só segura cena de muito movimento.
    teto = ["-maxrate", "12M", "-bufsize", "24M"]
    nvenc = ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "21", "-b:v", "0", *teto]
    x264 = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", *teto]
    resto = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", "-movflags", "+faststart", str(saida)]
    try:
        subprocess.run(cmd_base + nvenc + resto, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        log.warning("   NVENC falhou (%s); codificando na CPU", (exc.stderr or "").strip()[-200:])
        r = subprocess.run(cmd_base + x264 + resto, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"ffmpeg falhou: {(r.stderr or '').strip()[-400:]}")


def _abertura(fonte: Path, ini: float, fim: float, png: Path, pasta: Path) -> Path:
    """O gancho do vídeo final: a melhor parte, em preto e branco, na mesma
    moldura (só o vídeo fica cinza), para colar antes do vídeo. Mesmo codec e
    áudio do vídeo principal, para a colagem ser sem reencode."""
    saida = pasta / "abertura.mp4"
    filtro = (f"[0:v]hue=s=0[_g];" + moldura.filtro("_g", "1:v", "v") + ";"
              "[0:a]loudnorm=I=-14:TP=-2:LRA=11[a]")
    _codificar(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{ini:.3f}", "-t", f"{fim - ini:.3f}",
                "-i", str(fonte), "-loop", "1", "-i", str(png), "-filter_complex", filtro,
                "-map", "[v]", "-map", "[a]", "-shortest"], saida)
    return saida


def _fps(arquivo: Path) -> str:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=r_frame_rate", "-of", "default=nw=1:nk=1", str(arquivo)],
                       capture_output=True, text=True, check=True)
    return r.stdout.strip()


def _tem_audio(arquivo: Path) -> bool:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries", "stream=codec_type",
                        "-of", "default=nw=1:nk=1", str(arquivo)], capture_output=True, text=True)
    return r.returncode == 0 and bool(r.stdout.strip())


def _com_cta(final: Path, png: Path, pasta: Path, cta: Path = CTA) -> bool:
    """Cola o CTA (o do canal, ou estudio/cta/cta.mp4) no fim do vídeo, dentro da mesma moldura,
    na taxa de quadros do vídeo, e sem reencodar os 15-20 min. CTA sem áudio entra com silêncio.
    Devolve se o CTA entrou."""
    clipe, junto = pasta / "cta.mp4", pasta / "final_cta.mp4"
    try:
        com_audio = _tem_audio(cta)
        filtro = (f"[0:v]fps={_fps(final)}[_f];" + moldura.filtro("_f", "1:v", "v") + ";"
                  f"[{0 if com_audio else 2}:a]aresample=48000,aformat=channel_layouts=stereo[a]")
        # Sem faixa de áudio: o silêncio (anullsrc) e a moldura em loop não têm fim, então o `-shortest` sozinho não
        # encerra; a saída é limitada à duração do CTA.
        silencio = [] if com_audio else ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        limite = [] if com_audio else ["-t", f"{_duracao(cta):.3f}"]
        _codificar(["ffmpeg", "-y", "-loglevel", "error", "-i", str(cta), "-loop", "1", "-i", str(png), *silencio,
                    "-filter_complex", filtro, "-map", "[v]", "-map", "[a]", "-shortest", *limite], clipe)
        renderiza.concatenar([final, clipe], junto)
        junto.replace(final)
        return True
    except Exception as exc:  # noqa: BLE001 — sem o CTA o vídeo sai igual
        log.warning("   CTA falhou (%s); vídeo sem ele", exc)
        junto.unlink(missing_ok=True)
        return False
    finally:
        clipe.unlink(missing_ok=True)


def _cta_do_canal(canal: str | None) -> tuple[Path | None, str]:
    """(arquivo, origem) do CTA do vídeo longo: o do canal no Publicador, se ele enviou um (aba Canais); senão o
    cta/cta.mp4 global; senão nenhum. A origem ("canal" ou "global") vai para o log e para os metadados."""
    try:
        import cta_cortador
        do_canal = cta_cortador.caminho_cta(canal)
    except Exception as exc:  # noqa: BLE001 — sem achar o do canal, vale o global
        log.warning("   CTA do canal indisponível (%s); usando o global", exc)
        do_canal = None
    if do_canal is not None:
        return do_canal, "canal"
    return (CTA, "global") if CTA.exists() else (None, "")


def produzir(fonte: Path, inicio: float, fim: float, remover: list[dict], perfil_nome: str,
             pasta: Path, titulo: str, gancho: tuple[float, float] | None = None,
             canal: str | None = None) -> dict:
    """`fonte` é o vídeo da live inteira (ou o recorte bruto, com `inicio`=0);
    `inicio`/`fim` e as remoções estão no tempo dela. Grava <pasta>/final.mp4
    e devolve os metadados para a tela de revisão. `canal`: nome no Publicador,
    para o CTA em vídeo do canal (reserva: o cta.mp4 global)."""
    pasta.mkdir(parents=True, exist_ok=True)
    duracao = fim - inicio
    rel = [(max(0.0, r["inicio"] - inicio), min(duracao, r["fim"] - inicio)) for r in remover]

    wav = pasta / "audio.wav"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{inicio:.3f}", "-t", f"{duracao:.3f}",
                    "-i", str(fonte), "-vn", "-ac", "1", "-ar", "16000", str(wav)], check=True)
    try:
        silencios = detectar_silencios(wav, SILENCIO_DB, SILENCIO_LONGO, duracao)
    finally:
        wav.unlink(missing_ok=True)
    longos = [(a, b) for a, b in silencios if b - a > SILENCIO_LONGO]
    ficam = pedacos_que_ficam(duracao, rel, silencios)
    if not ficam:
        raise RuntimeError("nada sobrou do trecho depois dos cortes")
    esperado = sum(b - a for a, b in ficam)
    log.info("   %d pedaço(s): %.1f min → %.1f min (%d remoção(ões), %d silêncio(s) > %.0f s, −%.0f s)",
             len(ficam), duracao / 60, esperado / 60, len(rel), len(longos), SILENCIO_LONGO,
             sum(b - a - SILENCIO_SOBRA for a, b in longos))

    png = moldura.gerar(perfil_nome, pasta / "moldura.png")
    escolha = "+".join(f"between(t,{a:.3f},{b:.3f})" for a, b in ficam)
    filtro = (f"[0:v]select='{escolha}',setpts=N/FRAME_RATE/TB[_sel];"
              + moldura.filtro("_sel", "1:v", "v") + ";"
              f"[0:a]aselect='{escolha}',asetpts=N/SR/TB,loudnorm=I=-14:TP=-2:LRA=11[a]")
    final = pasta / "final.mp4"
    _codificar(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{inicio:.3f}", "-t", f"{duracao:.3f}",
                "-i", str(fonte), "-loop", "1", "-i", str(png), "-filter_complex", filtro,
                "-map", "[v]", "-map", "[a]", "-shortest"], final)

    if gancho:
        # `gancho` está no tempo de `fonte`, como `inicio`/`fim`
        try:
            ab = _abertura(fonte, gancho[0], gancho[1], png, pasta)
            junto = pasta / "final_gancho.mp4"
            renderiza.concatenar([ab, final], junto)
            junto.replace(final)
            ab.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 — sem o gancho o vídeo sai igual
            log.warning("   gancho em P&B falhou (%s); vídeo sem ele", exc)
            (pasta / "abertura.mp4").unlink(missing_ok=True)
            (pasta / "final_gancho.mp4").unlink(missing_ok=True)

    cta, origem_cta = _cta_do_canal(canal)
    cta_usado = ""
    if cta is not None:
        log.info("   CTA %s: %s", "do canal " + str(canal) if origem_cta == "canal" else "global", cta.name)
        cta_usado = origem_cta if _com_cta(final, png, pasta, cta) else ""

    dur_final = _duracao(final)
    capa = pasta / "capa.jpg"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{min(60.0, dur_final / 4):.1f}",
                    "-i", str(final), "-frames:v", "1", "-q:v", "3", str(capa)], check=False)
    audio = audio_qa.qa(final)
    if audio:
        log.info("   %s", audio_qa.linha_log(audio))
    return {
        "arquivo": final.name,
        "capa": capa.name if capa.exists() else "",
        "titulo": titulo,
        "subtitulo": "",
        "duracao": round(dur_final, 1),
        "silencios_cortados": len(longos),
        "remocoes": len(rel),
        "audio": audio,
        "cta": cta_usado,
    }
