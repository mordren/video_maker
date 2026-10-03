"""Acabamento de um corte: do clipe da Fase 2 (16:9, já limpo, com a abertura
em preto e branco) ao vídeo 9:16 pronto para publicar.

Mesma receita da exportação do programa de desktop (app.py, `_csv_export_clip`)
— legenda queimada, GC (tarja com título/manchete e logo), marca d'água,
censura (áudio + legenda), trilha com ducking e o frame de capa —, só que sem
a interface Qt, para rodar no serviço web. A única troca de propósito é o
formato: em vez do crop central fixo ("estender"), o crop dinâmico que segue
quem está falando (fase1/falante_ativo.py, LR-ASD; reserva: crop_dinamico.py).

Um Whisper só por corte: as palavras servem para a legenda e para os tempos
exatos da censura (e para o crop antigo saber quem fala, se o LR-ASD falhar). O crop não mexe no tempo do
vídeo, então a transcrição do clipe 16:9 vale para o 9:16.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
for pasta in (RAIZ, RAIZ / "fase1"):
    if str(pasta) not in sys.path:
        sys.path.insert(0, str(pasta))

import ai_srt  # noqa: E402
import audio_qa  # noqa: E402
import censor  # noqa: E402
import crop_dinamico  # noqa: E402
import falantes  # noqa: E402
import trilhas  # noqa: E402
from cg_generator import (  # noqa: E402
    _FONT, DEFAULT_COLORS, LT_BOTTOM_MARGIN, LT_HEIGHT, _text_width, create_lower_third)
from utils import (  # noqa: E402
    FONT_DIR, escape_drawtext, filter_path, shorten_srt_captions, srt_has_content,
    srt_timestamp)

log = logging.getLogger("estudio")

# Mesma conta do app.py: MarginV do libass é em unidades do script (~288 de
# altura num .srt), não em pixels — sobe a legenda para parar acima do GC.
LT_CAPTION_GAP = 140
LT_CAPTION_MARGIN_V = round((LT_HEIGHT + LT_BOTTOM_MARGIN + LT_CAPTION_GAP) * 288 / 1920)
ESTILO_LEGENDA = ("FontName=Montserrat,FontSize=18,Bold=-1,PrimaryColour=&H0000D7FF,"
                  "OutlineColour=&H00000000,BorderStyle=1,Outline=2.5,Shadow=0,Alignment=2,"
                  "MarginV={margem}")

# O formato "imagens" (foto em cima, vídeo embaixo — utils.build_clip_filter
# modo "imagem") empilha dois blocos de 608px (1216 no total) centralizados
# no quadro de 1920: a emenda foto/vídeo fica em 960 (a metade), com uma
# barra preta de 352px embaixo do vídeo (1568 a 1920). Legenda e GC mudam de
# lugar só nesse formato — a conta normal (acima) pensa num vídeo que ocupa
# o quadro inteiro: a legenda cairia em cima da foto, e o GC ficaria colado
# no vídeo em vez de usar a barra preta.
COLAGEM_EMENDA = 960                          # onde a foto termina e o vídeo começa
COLAGEM_LEGENDA_MARGIN_V = round(COLAGEM_EMENDA * 288 / 1920)
COLAGEM_GC_BOTTOM_MARGIN = 96                 # GC centralizado na barra preta embaixo do vídeo

# "new" (padrão): glow no gancho + karaokê com caixa (estudio/legenda_nova.py);
# "old": o SRT amarelo de sempre (ESTILO_LEGENDA).
LEGENDAS = ("new", "old")


def _margem_legenda_px(colagem: bool, tem_gc: bool) -> int:
    """Mesma posição da legenda antiga, em pixels do quadro de 1920 (a antiga
    usa MarginV em unidades de 288, ver LT_CAPTION_MARGIN_V)."""
    if colagem:
        return COLAGEM_EMENDA
    if tem_gc:
        return LT_HEIGHT + LT_BOTTOM_MARGIN + LT_CAPTION_GAP
    return round(60 * 1920 / 288)


# ---------------------------------------------------------------------------
# Perfil visual (logo, cores, marca d'água) — o mesmo do programa de desktop
# ---------------------------------------------------------------------------

@dataclass
class Perfil:
    nome: str
    logo: Path | None = None
    marca_dagua: str = ""
    cor_marca: str = "#FFFF00"
    cores: dict = field(default_factory=lambda: dict(DEFAULT_COLORS))


def perfis() -> list[str]:
    return sorted((ai_srt.load_config().get("profiles") or {}).keys())


def carregar_perfil(nome: str) -> Perfil:
    dados = ai_srt.load_profile(nome) or {}
    logo = Path(dados["brand_logo"]) if dados.get("brand_logo") else None
    cores = dict(DEFAULT_COLORS)
    for chave in cores:
        salvo = str(dados.get(f"brand_color_{chave}") or "").strip()
        if salvo:
            cores[chave] = salvo
    if logo and not logo.exists():
        log.warning("   perfil %r: logo não encontrado (%s) — o corte sai sem a tarja", nome, logo)
    return Perfil(nome=nome, logo=logo if logo and logo.exists() else None,
                  marca_dagua=str(dados.get("brand_watermark") or ""),
                  cor_marca=str(dados.get("brand_wm_color") or "#FFFF00"), cores=cores)


# ---------------------------------------------------------------------------
# Transcrição (uma por corte)
# ---------------------------------------------------------------------------

def liberar_modelos() -> None:
    """Devolve a VRAM ao sistema entre cortes.

    O Whisper agora roda pela API (sem custo de GPU local) ou, na reserva, um
    processo à parte por corte (transcricao.rodar_whisper) — nenhum dos dois
    fica com modelo carregado neste processo. Isto ainda importa pelo crop
    antigo (MediaPipe) e por qualquer torch que sobre alocado aqui."""
    import gc
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass


def transcrever(video: Path, pasta: Path, modelo: str = "small", idioma: str = "pt") -> tuple[Path, list[dict]]:
    """Tempo por palavra: tenta a API (whisper-large-v3-turbo via OpenRouter —
    ~10x mais rápido, custo trivial), cai pro Whisper local sem chave ou se a
    API falhar (fase1/transcricao.py:transcrever_audio). Grava <pasta>/
    legenda.srt (encurtada em blocos curtos, como o app faz) e legenda.json
    (formato do Whisper, que a censura usa para achar o segundo exato de cada
    palavra). Devolve (srt, palavras)."""
    import transcricao
    segs, fonte = transcricao.transcrever_audio(video, pasta, modelo, idioma)
    log.info("   transcrição: %s", fonte)
    srt = pasta / "legenda.srt"
    (pasta / "legenda.json").write_text(json.dumps({"segments": segs}, ensure_ascii=False), encoding="utf-8")
    linhas = []
    n = 0
    for s in segs:
        texto = s["text"].strip()
        if texto:
            n += 1
            linhas += [str(n), f"{srt_timestamp(s['start'])} --> {srt_timestamp(s['end'])}", texto, ""]
    srt.write_text("\n".join(linhas), encoding="utf-8")
    palavras = [w for s in segs for w in s.get("words", [])]
    return srt, palavras


# ---------------------------------------------------------------------------
# Revisão da legenda com IA (título, manchete, trechos sensíveis, trilha)
# ---------------------------------------------------------------------------

def revisar_legenda(srt: Path, contexto: str, pasta_trilhas: Path | None) -> dict:
    """Revisa a legenda com o DeepSeek (se ligado na config do app) e pega
    título (chapéu do GC), subtítulo (manchete), trechos sensíveis e trilha.
    Sem chave ou desligado, devolve tudo vazio — o corte segue sem isso."""
    from utils import review_srt_with_ai

    cfg = ai_srt.load_config()
    chave = (cfg.get("api_key") or "").strip() or ai_srt.api_key_from_env()
    vazio = {"titulo": "", "subtitulo": "", "sensiveis": [], "musica": ""}
    if not (cfg.get("ai_enabled", True) and chave and srt_has_content(srt)):
        return vazio
    rotulos = [r for r, _ in trilhas.list_tracks(pasta_trilhas)] if pasta_trilhas else []
    try:
        _, _, _, titulo, subtitulo, sensiveis, musica = review_srt_with_ai(
            srt, chave, cfg.get("model") or ai_srt.DEFAULT_MODEL, contexto,
            with_title=True, musicas=rotulos or None)
    except Exception as exc:  # noqa: BLE001 — sem revisão, segue com a legenda crua
        log.warning("   revisão com IA falhou: %s", exc)
        return vazio
    return {"titulo": titulo, "subtitulo": subtitulo, "sensiveis": sensiveis, "musica": musica}


def palavras_censuradas(sensiveis: list[dict]) -> list[str]:
    """Lista de censura da config do app + o que a IA apontou (igual ao app)."""
    cfg = ai_srt.load_config()
    if not cfg.get("censor_enabled", True):
        return []
    palavras = censor.parse_words(cfg.get("censor_words") or "")
    for item in sensiveis or []:
        trecho = str(item.get("trecho", "")).strip()
        if trecho and trecho.lower() not in (p.lower() for p in palavras):
            palavras.append(trecho)
    return palavras


# ---------------------------------------------------------------------------
# Render final: legenda + GC + marca d'água + censura + trilha + capa
# ---------------------------------------------------------------------------

def duracao_de(arquivo: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(arquivo)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def _cadeia_video(srt: Path | None, cg_entrada: int | None, titulo: str, perfil: Perfil,
                  capa_entrada: int | None, colagem: bool = False, ass: Path | None = None) -> str:
    cadeia, atual = "[0:v]setsar=1[base]", "base"
    if ass is not None:
        cadeia += (f";[{atual}]ass=filename='{filter_path(ass)}':"
                   f"fontsdir='{filter_path(FONT_DIR)}'[leg]")
        atual = "leg"
    elif srt is not None and srt_has_content(srt):
        margem = (COLAGEM_LEGENDA_MARGIN_V if colagem
                 else LT_CAPTION_MARGIN_V if cg_entrada is not None else 60)
        estilo = ESTILO_LEGENDA.format(margem=margem)
        cadeia += (f";[{atual}]subtitles=filename='{filter_path(srt)}':"
                   f"fontsdir='{filter_path(FONT_DIR)}':force_style='{estilo}'[leg]")
        atual = "leg"
    if cg_entrada is not None:
        margem_gc = COLAGEM_GC_BOTTOM_MARGIN if colagem else LT_BOTTOM_MARGIN
        cadeia += f";[{atual}][{cg_entrada}:v]overlay=x=0:y=main_h-{LT_HEIGHT + margem_gc}[cg]"
        atual = "cg"
    elif titulo.strip():
        from utils import format_title_for_video
        cadeia += (f";[{atual}]drawtext=fontfile='{_FONT}':text='{format_title_for_video(titulo)}':"
                   "x=(w-text_w)/2:y=30:fontsize=40:fontcolor=white:borderw=3:bordercolor=black[tit]")
        atual = "tit"
    texto = perfil.marca_dagua.strip()
    if texto:
        tamanho = int((1080 - 60) * 1000 / _text_width(texto, 1000))
        cadeia += (f";[{atual}]drawtext=fontfile='{_FONT}':text='{escape_drawtext(texto)}':"
                   f"x=(w-text_w)/2:y=70:fontsize={tamanho}:fontcolor={perfil.cor_marca}@0.8:"
                   "borderw=2:bordercolor=black@0.4[wm]")
        atual = "wm"
    if capa_entrada is not None:
        # O PNG da capa cobre só o frame 0: é o quadro que as redes pegam
        # como capa, e sai limpo (sem legenda escrita).
        cadeia += f";[{atual}][{capa_entrada}:v]overlay=0:0:enable='lt(n,1)'[capa]"
        atual = "capa"
    return cadeia + f";[{atual}]format=yuv420p[outv]"


def renderizar_final(vertical: Path, destino: Path, srt: Path | None, titulo: str, subtitulo: str,
                     perfil: Perfil, censura: list[str], trilha: Path | None, pasta: Path,
                     colagem: bool = False, legenda: str = "new", palavras: list[dict] | None = None,
                     fim_gancho: float = 0.0) -> dict:
    """Queima tudo no 9:16. Devolve {"capa": Path|None, "silenciados": n, "trocadas": n,
    "audio": relatório do audio_qa.qa}.

    `colagem`: True quando o clipe já chegou no formato "imagens" (foto em
    cima, vídeo embaixo) — muda onde a legenda e o GC ficam (ver as
    constantes COLAGEM_* acima).

    `legenda`: "new" (glow no gancho + karaokê com caixa, a partir das
    `palavras`; `fim_gancho` = segundos iniciais que são o gancho) ou "old"
    (o SRT amarelo). Sem palavras, a "new" cai para a "old"."""
    cfg = ai_srt.load_config()
    duracao = duracao_de(vertical)

    cg = None
    if perfil.logo is not None and (titulo.strip() or subtitulo.strip()):
        cg = create_lower_third(titulo, subtitulo, pasta, perfil.logo, colors=perfil.cores)

    # Capa: o primeiro quadro com GC e marca d'água, sem legenda escrita.
    capa = destino.with_suffix(".png")
    entradas_capa = ["-i", str(vertical)] + (["-loop", "1", "-i", str(cg)] if cg else [])
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *entradas_capa, "-filter_complex",
                        _cadeia_video(None, 1 if cg else None, titulo, perfil, None, colagem),
                        "-map", "[outv]", "-frames:v", "1", "-q:v", "2", str(capa)],
                       capture_output=True, text=True)
    if r.returncode != 0 or not capa.exists():
        log.warning("   capa não saiu: %s", r.stderr[-300:])
        capa = None

    # Censura: os tempos saem ANTES de disfarçar o texto (depois "porra" vira
    # "p0rra" e não casa mais com a lista) — mesma ordem do app.
    silenciar, trocadas = [], 0
    if censura and srt is not None and srt_has_content(srt):
        if cfg.get("censor_mute", True):
            silenciar = censor.mute_spans(srt, censura)
        if cfg.get("censor_caption", True):
            trocadas = censor.censor_srt(srt, censura)
    filtro_censura = censor.mute_filter(silenciar)

    ass = None
    if legenda == "new" and palavras:
        import legenda_nova
        ass = pasta / "legenda.ass"
        censura_texto = censura if cfg.get("censor_caption", True) else []
        if not legenda_nova.gerar(palavras, ass, fim_gancho, _margem_legenda_px(colagem, cg is not None),
                                  censura_texto):
            ass = None

    entradas = ["-i", str(vertical)]
    n = 1
    cg_entrada = capa_entrada = trilha_entrada = None
    if cg:
        entradas += ["-loop", "1", "-i", str(cg)]
        cg_entrada, n = n, n + 1
    if capa:
        entradas += ["-loop", "1", "-i", str(capa)]
        capa_entrada, n = n, n + 1
    if trilha is not None and trilha.exists():
        entradas += ["-i", str(trilha)]
        trilha_entrada, n = n, n + 1

    cadeia = _cadeia_video(srt, cg_entrada, titulo, perfil, capa_entrada, colagem, ass)
    if trilha_entrada is not None:
        fala = "0:a"
        if filtro_censura:
            cadeia += f";[0:a]{filtro_censura}[sp]"
            fala = "sp"
        cadeia += ";" + trilhas.mix_chain(fala, trilha_entrada, "aout", duration=duracao)
        mapa_audio, extra = "[aout]", []
    else:
        mapa_audio, extra = "0:a?", (["-af", filtro_censura] if filtro_censura else [])

    cmd = ["ffmpeg", "-y", "-loglevel", "error", *entradas, "-filter_complex", cadeia,
           "-map", "[outv]", "-map", mapa_audio, *extra, "-t", f"{duracao:.3f}",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(destino)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou no acabamento: {r.stderr[-800:]}")
    # Volume do arquivo final (a trilha e o AAC mexem no que a Fase 2 deixou
    # em -14): mede e, se saiu da faixa, refaz só o áudio (audio_qa.py).
    audio = audio_qa.qa(destino, fala=vertical if trilha_entrada is not None else None, cfg=cfg)
    if audio:
        log.info("   %s", audio_qa.linha_log(audio))
    return {"capa": capa, "silenciados": len(silenciar), "trocadas": trocadas, "audio": audio}


# ---------------------------------------------------------------------------
# Ponta a ponta de um corte
# ---------------------------------------------------------------------------

def para_transparente(clipe: Path, destino: Path) -> None:
    """16:9 inteiro, centralizado sobre ele mesmo desfocado — o formato
    "Transparente (9:16 fundo desfocado)" do programa de desktop."""
    from utils import build_clip_filter
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clipe), "-filter_complex",
                        build_clip_filter("transparente", False), "-map", "[base]", "-map", "0:a?",
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-c:a", "copy",
                        str(destino)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou no transparente: {r.stderr[-600:]}")


def para_imagens(clipe: Path, destino: Path, pasta: Path, gancho: str, comentario: str,
                 palavras: list[dict]) -> list[dict] | None:
    """Fotos do assunto em cima, trocando ao longo do corte, e o corte 16:9
    embaixo (estudio/imagens.py: Commons + rosto conferido + nota do modelo de
    visão). Devolve os créditos das fotos, ou None (sem gravar `destino`) se
    não achou ninguém com foto — quem chama cai para o formato de reserva."""
    import imagens
    segmentos = json.loads((pasta / "legenda.json").read_text(encoding="utf-8"))["segments"]
    _l, _a, fps, _d = crop_dinamico.resolucao_de(clipe)
    try:
        return imagens.montar(clipe, destino, pasta, gancho, comentario, segmentos, palavras,
                              duracao_de(clipe), fps or 30.0)
    except Exception as exc:  # noqa: BLE001 — sem fotos, o corte sai no formato de reserva
        log.warning("   formato imagens falhou (%s)", exc)
        destino.unlink(missing_ok=True)
        return None


def crop_falante_ativo(clipe: Path, destino: Path, cfg_crop: dict, pasta: Path) -> bool:
    """Crop 9:16 pelo LR-ASD (fase1/falante_ativo.py), num processo à parte:
    quando ele sai, a VRAM e a RAM (~3 GB por minuto de vídeo) voltam inteiras.
    Devolve False se não deu (sem os pesos, sem GPU, erro) — aí vale o crop antigo."""
    script = RAIZ / "fase1" / "falante_ativo.py"
    pesos = [RAIZ / "fase1" / "models" / n for n in ("det_10g.onnx", "lrasd_finetuning_TalkSet.model")]
    if not all(p.exists() for p in pesos):
        log.warning("   crop LR-ASD: faltam os pesos em fase1/models; usando o crop antigo")
        return False
    liberar_modelos()           # o Whisper de um corte anterior não pode dividir a placa com ele
    cfg = pasta / "config_crop.yaml"
    import yaml
    cfg.write_text(yaml.safe_dump(cfg_crop, allow_unicode=True), encoding="utf-8")
    r = subprocess.run([sys.executable, str(script), str(clipe), str(destino), "--config", str(cfg)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       cwd=str(RAIZ / "fase1"))
    cfg.unlink(missing_ok=True)
    resumo = next((l[7:] for l in r.stdout.splitlines() if l.startswith("RESUMO ")), None)
    if r.returncode != 0 or resumo is None or not destino.exists():
        log.warning("   crop LR-ASD falhou (%s); usando o crop antigo", (r.stderr or r.stdout)[-400:])
        return False
    log.info("   crop LR-ASD: %s", resumo)
    return True


def finalizar_corte(clipe: Path, pasta: Path, perfil: Perfil, cfg_crop: dict, contexto: str = "",
                    titulo_sugerido: str = "", manchete_sugerida: str = "",
                    pasta_trilhas: Path | None = None, preferir_sugeridos: bool = False,
                    musica_sugerida: str = "", formato: str = "dinamico", gancho: str = "",
                    permitir_crop: bool = True, legenda: str = "new", fim_gancho: float = 0.0) -> dict:
    """Clipe da Fase 2 -> <pasta>/final.mp4 (9:16 pronto). Devolve os metadados
    do corte para a tela de revisão.

    `preferir_sugeridos` (lote CSV): título, manchete e trilha escritos no CSV
    valem mais que os da IA, como no programa de desktop; no automático é o
    contrário — a IA leu a fala do corte e o sugerido é só o gancho da Fase 1.

    `formato`: "dinamico" (crop 9:16 que segue quem fala), "transparente"
    (16:9 inteiro sobre fundo desfocado) ou "imagens" (fotos do assunto em
    cima, trocando ao longo do corte, e o corte 16:9 embaixo — cai para
    "dinamico" se não achar ninguém com foto). Só vale para clipe que chega
    16:9; `gancho`/`contexto` são o texto que a Fase 1 já escreveu, usados
    para achar o assunto do "imagens".

    `permitir_crop=False` (servidor com pouca RAM, ver estudio.py): nada de
    crop — o "imagens" sem foto cai para o transparente, não para o crop.

    `legenda`: "new" ou "old" (ver renderizar_final). `fim_gancho`: duração
    da abertura que a Fase 2 pôs no começo do clipe (relatorio.json).
    """
    pasta.mkdir(parents=True, exist_ok=True)
    cf = cfg_crop.get("falantes", {})

    # Clipe que já chega vertical (upload em 9:16) não se corta de novo.
    largura, altura, _fps, _dur = crop_dinamico.resolucao_de(clipe)
    ja_vertical = altura > largura
    vertical = clipe if ja_vertical else pasta / "vertical.mp4"

    # O crop vem ANTES do Whisper: roda num processo à parte e precisa da
    # placa livre (4 GB). O "imagens" é o contrário: precisa das palavras
    # (quando trocar a foto, quem é citado) e não usa a placa.
    feito = ja_vertical
    colagem = False    # só True se o "imagens" realmente saiu assim (achou assunto e foto)
    creditos: list[dict] = []
    srt = palavras = None
    if not feito and formato == "imagens":
        srt, palavras = transcrever(clipe, pasta, cf.get("whisper_modelo", "small"), cf.get("idioma", "pt"))
        creditos = para_imagens(clipe, vertical, pasta, gancho, contexto, palavras)
        feito = colagem = creditos is not None
        creditos = creditos or []
        if not feito:
            log.info("   sem fotos: o corte sai no %s", "crop que segue quem fala" if permitir_crop
                     else "transparente (pouca RAM: crop desligado)")
    reserva = None if feito else ("transparente" if not permitir_crop else "dinamico")
    if not feito and (formato == "transparente" or not permitir_crop):
        para_transparente(clipe, vertical)
        feito = True
    if not feito and cfg_crop.get("falante_ativo", {}).get("ativo", True):
        feito = crop_falante_ativo(clipe, vertical, cfg_crop, pasta)

    if srt is None:
        srt, palavras = transcrever(clipe, pasta, cf.get("whisper_modelo", "small"), cf.get("idioma", "pt"))

    # Reserva: o crop antigo (MediaPipe + quem fala pela voz)
    turnos: list[dict] = []
    if not feito:
        if cf.get("ativo", True) and palavras:
            wav = pasta / "audio.wav"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clipe), "-ac", "1",
                            "-ar", "16000", str(wav)], check=True)
            try:
                turnos = falantes.detectar(wav, palavras, n_falantes=cf.get("n_falantes"),
                                           limiar=cf.get("limiar", 0.35))
            finally:
                wav.unlink(missing_ok=True)
        crop_dinamico.processar(clipe, vertical, turnos, cfg_crop)

    # A IA revisa com a frase inteira; só depois a legenda é picada em blocos
    # curtos (mesma ordem do app: "a IA lida melhor com frases inteiras").
    ia = revisar_legenda(srt, contexto, pasta_trilhas)
    shorten_srt_captions(srt)

    if preferir_sugeridos:
        titulo = titulo_sugerido or ia["titulo"]
        subtitulo = manchete_sugerida or ia["subtitulo"]
        musica = musica_sugerida or ia["musica"]
    else:
        titulo = ia["titulo"] or titulo_sugerido
        subtitulo = ia["subtitulo"] or manchete_sugerida
        musica = ia["musica"] or musica_sugerida
    trilha = trilhas.find_track(musica, pasta_trilhas) if (musica and pasta_trilhas) else None
    if not ai_srt.load_config().get("music_enabled", True):
        trilha = None

    final = pasta / "final.mp4"
    info = renderizar_final(vertical, final, srt, titulo, subtitulo, perfil,
                            palavras_censuradas(ia["sensiveis"]), trilha, pasta, colagem=colagem,
                            legenda=legenda, palavras=palavras, fim_gancho=fim_gancho)
    # Ritmo no vertical, antes de apagá-lo: sem a legenda queimada, o karaokê
    # não conta como mudança de tela (ritmo.py).
    import ritmo
    rit = ritmo.relatorio(vertical, duracao_de(vertical), palavras or [], fim_gancho,
                          ritmo.ler_trocas_fotos(pasta) if colagem else None, ai_srt.load_config())
    log.info("   %s", ritmo.linha_log(rit))
    if not ja_vertical:
        vertical.unlink(missing_ok=True)
    return {
        "arquivo": final.name,
        "capa": info["capa"].name if info["capa"] else "",
        "legenda": srt.name,
        "estilo_legenda": legenda,
        "titulo": titulo,
        "subtitulo": subtitulo,
        "trilha": trilha.name if trilha else "",
        "silenciados": info["silenciados"],
        "palavras_trocadas": info["trocadas"],
        "falantes": len({t["falante"] for t in turnos}),
        "duracao": round(duracao_de(final), 1),
        "texto": " ".join(p["word"] for p in palavras)[:600],
        "creditos_imagens": creditos,
        "formato_reserva": reserva if formato == "imagens" else None,
        "audio": info["audio"],
        "ritmo": rit,
    }
