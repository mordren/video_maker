"""Etapa 5: montagem com FFmpeg.

1. Cada cena vira um clipe (imagem ampliada 2x -> movimento -> sobreposição -> escurecimento na troca).
2. Os clipes são concatenados sem reencodar.
3. Passada final: legenda queimada (ASS), narração atrasada pela introdução e trilha com ducking.

Voz e música são normalizadas para o mesmo volume (NARRACAO_LUFS, padrão -16 LUFS) antes da mistura, então os níveis do canal são relativos à voz
(ex.: música sozinha -6 dB, sob a voz -20 dB).
Ducking: o programa sabe exatamente quando o narrador fala (tempo de cada palavra), então monta um envelope
de volume: música no nível "solo" quando só ela toca e no nível "sob voz" enquanto o narrador fala, com
rampas curtas entre os dois.
"""
import json
import random
import subprocess
from pathlib import Path

from . import config, db, efeitos, historia, narracao


class ErroMontagem(Exception):
    pass


def _ffmpeg(args: list[str], cwd: Path, rotulo: str):
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args], cwd=cwd,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise ErroMontagem(f"FFmpeg falhou em {rotulo}: {r.stderr[-1500:]}")


def _db_para_ganho(v: float) -> float:
    return 10 ** (float(v) / 20)


def segmentos_de_fala(palavras: list[dict], deslocamento: float, juntar: float = 0.7) -> list[tuple[float, float]]:
    segs = []
    for p in palavras:
        a, b = p["inicio"] + deslocamento, p["fim"] + deslocamento
        if segs and a - segs[-1][1] <= juntar:
            segs[-1] = (segs[-1][0], max(segs[-1][1], b))
        else:
            segs.append((a, b))
    return segs


def envelope_musica(segs, solo_db: float, sob_db: float, rampa: float) -> str:
    """Expressão do filtro volume: ganho solo menos (solo - sob) * D(t), D = 1 durante a fala, com rampas."""
    gs, gv = _db_para_ganho(solo_db), _db_para_ganho(sob_db)
    if not segs:
        return f"{gs:.5f}"
    r = max(rampa, 0.05)
    termos = [f"clip(min((t-{a - r:.3f})/{r:.3f},({b + r:.3f}-t)/{r:.3f}),0,1)" for a, b in segs]
    d = termos[0]
    for t in termos[1:]:
        d = f"max({d},{t})"
    return f"{gs:.5f}-{gs - gv:.5f}*{d}"


def escolher_trilha(projeto: dict, canal: dict) -> Path | None:
    if projeto.get("trilha"):
        p = config.BASE / projeto["trilha"]
        if p.exists():
            return p
    ativas = [t for t in canal.get("trilhas", []) if t["ativa"] and (config.BASE / t["arquivo"]).exists()]
    if not ativas:
        return None
    escolhida = random.Random(projeto["id"]).choice(ativas)
    db.atualizar("projetos", projeto["id"], trilha=escolhida["arquivo"])
    return config.BASE / escolhida["arquivo"]


def clipe_cena(img: Path, destino: Path, ctx: dict, efeito: str, extra: str | None, transicao: str, pasta: Path):
    w, h = ctx["w"], ctx["h"]
    cadeia = [f"scale={2 * w}:{2 * h}:force_original_aspect_ratio=increase:flags=lanczos",
              f"crop={2 * w}:{2 * h}", "setsar=1", efeitos.movimento(efeito, ctx)]
    sob = efeitos.sobreposicao(extra, ctx)
    if sob:
        cadeia.append(sob)
    if transicao == "escurecer" and ctx["dur"] > 0.5:
        f = 0.125
        if not ctx.get("primeira"):
            cadeia.append(f"fade=t=in:st=0:d={f}")
        if not ctx.get("ultima"):
            cadeia.append(f"fade=t=out:st={ctx['dur'] - f:.3f}:d={f}")
    cadeia.append("format=yuv420p")
    _ffmpeg(["-i", str(img), "-vf", ",".join(cadeia), "-frames:v", str(ctx["frames"]), "-r", str(ctx["fps"]),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", str(config.MONTAGEM_CRF_CLIPES), "-an", str(destino)], pasta, f"cena {destino.stem}")


def montar(projeto: dict, canal: dict, h: dict, narr: dict) -> Path:
    pasta = config.BASE / projeto["pasta"]
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    cfg = canal["config"]
    mus = cfg.get("musica") or {}
    w, hh, fps = fmt["largura"], fmt["altura"], fmt["fps"]
    trilha = escolher_trilha(projeto, canal)
    intro = float(mus.get("intro_s", 1.5)) if trilha else 0.3
    cauda = float(mus.get("cauda_s", 2.5)) if trilha else 1.0
    total = intro + narr["duracao"] + cauda

    # Limites de cada cena em quadros (evita acumular erro de arredondamento).
    tempos = {t["n"]: t for t in narr["cenas"]}
    cenas = h["cenas"]
    marcas = [0] + [round((intro + tempos[c["n"]]["inicio"]) * fps) for c in cenas[1:]] + [round(total * fps)]
    efeitos.atribuir(h, canal, fmt)
    clipes = pasta / "clipes"
    clipes.mkdir(exist_ok=True)
    lista = []
    max_cena_s = historia.limites(canal)["max_cena_s"]
    for i, c in enumerate(cenas):
        frames = max(marcas[i + 1] - marcas[i], 2)
        dur = frames / fps
        if dur > max_cena_s + (intro if i == 0 else 0) + (cauda if i == len(cenas) - 1 else 0) + 0.5:
            db.evento(projeto["id"], f"Cena {c['n']} ficou com {dur:.1f}s na tela (máximo {max_cena_s:.0f}s).", "aviso")
        img = pasta / "cenas" / f"{c['n']:02d}.jpg"
        if not img.exists():
            raise ErroMontagem(f"A cena {c['n']} não tem imagem")
        ctx = {"w": w, "h": hh, "fps": fps, "frames": frames, "dur": dur, "tensao": c.get("tensao", 2), "n": c["n"],
               "primeira": i == 0, "ultima": i == len(cenas) - 1}
        destino = clipes / f"{c['n']:02d}.mp4"
        clipe_cena(img, destino, ctx, c.get("efeito", "zoom_in"), c.get("extra"), h.get("transicao", "escurecer"), pasta)
        lista.append(f"file 'clipes/{destino.name}'")
        db.evento(projeto["id"], f"Clipe da cena {c['n']} pronto ({dur:.1f}s, {c.get('efeito')}"
                                 f"{' + ' + c['extra'] if c.get('extra') else ''}).")
    (pasta / "clipes.txt").write_text("\n".join(lista) + "\n", encoding="utf-8")
    _ffmpeg(["-f", "concat", "-safe", "0", "-i", "clipes.txt", "-c", "copy", "video_sem_audio.mp4"], pasta, "concatenação")

    # Legendas
    leg = narracao.legendas_ass(narr["palavras"], [c["narracao"] for c in cenas],
                                cfg.get("legenda") or {}, w, hh, intro, pasta / "legendas.ass")

    # Áudio
    filtros = []
    entradas = ["-i", "video_sem_audio.mp4", "-i", "narracao.mp3"]
    atraso = int(intro * 1000)
    lufs = f"{config.NARRACAO_LUFS:g}"
    filtros.append(f"[1:a]loudnorm=I={lufs}:TP=-1.5:LRA=11,aresample=48000,aformat=channel_layouts=stereo,adelay=delays={atraso}:all=1,"
                   f"apad=whole_dur={total:.3f}[voz]")
    if trilha:
        entradas += ["-stream_loop", "-1", "-i", str(trilha)]
        segs = segmentos_de_fala(narr["palavras"], intro)
        env = envelope_musica(segs, mus.get("volume_solo_db", -6), mus.get("volume_sob_voz_db", -20),
                              mus.get("rampa_s", 0.35))
        fade_out = min(max(cauda, 1.0), 3.0)
        filtros.append(f"[2:a]atrim=0:{total + 1:.3f},asetpts=N/SR/TB,loudnorm=I={lufs}:TP=-1.5:LRA=11,"
                       f"aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},asetpts=N/SR/TB,"
                       f"volume='{env}':eval=frame,afade=t=in:st=0:d=0.8,"
                       f"afade=t=out:st={total - fade_out:.3f}:d={fade_out:.3f}[mus]")
        filtros.append("[voz][mus]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[a]")
    else:
        filtros.append("[voz]alimiter=limit=0.95[a]")
    filtros.append(f"[0:v]{'ass=legendas.ass,' if leg else ''}format=yuv420p[v]")
    (pasta / "filtro_final.txt").write_text(";\n".join(filtros), encoding="utf-8")
    saida = pasta / "final.mp4"
    _ffmpeg([*entradas, *config.filtro_de_arquivo("filtro_final.txt"), "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-preset", config.MONTAGEM_PRESET, "-crf", str(fmt["crf"]), "-maxrate", fmt["maxrate"],
             "-bufsize", str(int(fmt["maxrate"][:-1]) * 2) + "M", "-r", str(fps), "-c:a", "aac", "-b:a", f"{config.MONTAGEM_AUDIO_KBPS}k",
             "-movflags", "+faststart", "-t", f"{total:.3f}", "final.mp4"], pasta, "passada final")
    (pasta / "montagem.json").write_text(json.dumps({"intro": intro, "cauda": cauda, "total": total,
                                                     "trilha": str(trilha) if trilha else None,
                                                     "cenas": [{"n": c["n"], "efeito": c.get("efeito"),
                                                                "extra": c.get("extra")} for c in cenas]},
                                                    ensure_ascii=False, indent=1), encoding="utf-8")
    return saida


def testar_efeito(nome: str, imagem: Path, destino: Path, formato: str = "short", tensao: int = 3, dur: float = 5.0):
    """Clipe de 5 s de um efeito sobre uma imagem, sem gerar vídeo inteiro."""
    fmt = config.FORMATOS.get(formato, config.FORMATOS["short"])
    mod = efeitos.registro().get(nome)
    if not mod:
        raise ErroMontagem(f"Efeito desconhecido: {nome}. Disponíveis: {', '.join(efeitos.registro())}")
    ctx = {"w": fmt["largura"], "h": fmt["altura"], "fps": fmt["fps"], "frames": int(dur * fmt["fps"]), "dur": dur,
           "tensao": tensao, "n": 1, "primeira": True, "ultima": True}
    mov, extra = (nome, None) if mod.TIPO == "movimento" else (fmt["movimento_padrao"], nome)
    destino.parent.mkdir(parents=True, exist_ok=True)
    clipe_cena(Path(imagem).resolve(), destino.resolve(), ctx, mov, extra, "corte_seco", destino.parent.resolve())
    return destino
