"""CTA em vídeo por canal: guarda o .mp4 que cada canal manda e o anexa como ÚLTIMA parte do vídeo montado.

O mesmo mecanismo serve ao fluxo de histórias (pipeline.etapa_video, logo depois da montagem) e ao Cortador (função
`anexar`, comando `python main.py cta-anexar` ou POST /api/cta/{canal}/anexar).

O CTA é identificado pelo nome do canal NO PUBLICADOR (garras, info, br_semfim...), que é o que os dois fluxos têm em
comum. Arquivos em dados/cta/<canal>.mp4 + <canal>.json (nome original, duração, data); a pasta dados/ não vai para o git.
Um novo envio substitui o anterior (grava em .tmp e troca).

Anexar = reencodar o vídeo principal + o CTA num único filtro `concat`, com o CTA normalizado para o principal
(resolução com barras pretas, setsar=1, fps, yuv420p, áudio AAC com a mesma taxa e canais; sem áudio, entra silêncio).
Nada é gravado por cima do original antes de a conferência (ffprobe) passar.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from . import config

MAX_BYTES = 300 * 1024 * 1024   # tamanho máximo do envio
MAX_DURACAO_S = 60.0            # um CTA tem que ser curto
TOLERANCIA_S = 0.35             # diferença aceita entre (principal + CTA) e o vídeo montado
NOME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class ErroCTA(Exception):
    pass


# ---------------------------------------------------------------- nomes e pastas

def chave(nome: str) -> str:
    """Nome do canal no Publicador, validado (vira nome de arquivo: nada de barras, pontos ou '..')."""
    n = (nome or "").strip().lower()
    if not NOME_OK.match(n):
        raise ErroCTA("Nome de canal inválido: use letras, números, '_' ou '-' (até 64 caracteres)")
    return n


def chave_do_canal(canal: dict) -> str:
    """Chave do CTA de um canal de histórias: o canal no Publicador; sem ele, o slug do canal. "" se não der."""
    for bruto in ((canal.get("config") or {}).get("publicador_canal"), canal.get("slug")):
        try:
            if (bruto or "").strip():
                return chave(bruto)
        except ErroCTA:
            continue
    return ""


def pasta() -> Path:
    return config.DADOS / "cta"


def caminho(canal: str) -> Path:
    return pasta() / f"{chave(canal)}.mp4"


def _meta(canal: str) -> Path:
    return pasta() / f"{chave(canal)}.json"


def existe(canal: str) -> bool:
    try:
        return caminho(canal).is_file()
    except ErroCTA:
        return False


# ---------------------------------------------------------------- ffprobe

def _sem_janela() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def _num(v, padrao=None):
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else padrao
    except (TypeError, ValueError):
        return padrao


def _taxa(txt: str | None) -> str | None:
    """'30000/1001' -> o mesmo texto, desde que seja uma fração válida e positiva."""
    m = re.fullmatch(r"(\d+)/(\d+)", (txt or "").strip())
    if m and int(m.group(1)) > 0 and int(m.group(2)) > 0:
        return f"{int(m.group(1))}/{int(m.group(2))}"
    return None


def sondar(arquivo: Path) -> dict:
    """Dados do arquivo pelo ffprobe: duração, vídeo (largura, altura, fps) e áudio (taxa, canais)."""
    try:
        r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries",
                            "format=duration,format_name,size:stream=codec_type,codec_name,width,height,r_frame_rate,"
                            "avg_frame_rate,duration,sample_rate,channels,bit_rate,disposition",
                            "-of", "json", str(arquivo)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
                           **_sem_janela())
    except (OSError, subprocess.TimeoutExpired) as e:
        raise ErroCTA(f"Não consegui rodar o ffprobe ({config.FFPROBE}): {e}")
    try:
        j = json.loads(r.stdout or "{}")
    except ValueError:
        j = {}
    if r.returncode != 0 or not j.get("format"):
        raise ErroCTA("Arquivo ilegível: o ffprobe não reconheceu como vídeo")
    streams = j.get("streams") or []
    capa = lambda s: (s.get("disposition") or {}).get("attached_pic")  # noqa: E731
    v = next((s for s in streams if s.get("codec_type") == "video" and not capa(s)), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    dur_formato = _num(j["format"].get("duration"), 0.0)
    out = {"formato": j["format"].get("format_name", ""), "tamanho": int(_num(j["format"].get("size"), 0)),
           "duracao": dur_formato, "video": None, "audio": None}
    if v:
        fps = _taxa(v.get("r_frame_rate")) or _taxa(v.get("avg_frame_rate")) or "30/1"
        out["video"] = {"codec": v.get("codec_name"), "largura": int(v.get("width") or 0), "altura": int(v.get("height") or 0),
                        "fps": fps, "duracao": _num(v.get("duration")) or dur_formato}
    if a:
        out["audio"] = {"codec": a.get("codec_name"), "taxa": int(_num(a.get("sample_rate"), 48000) or 48000),
                        "canais": int(a.get("channels") or 2), "kbps": int((_num(a.get("bit_rate"), 0) or 0) / 1000)}
    return out


# ---------------------------------------------------------------- guardar, ver, remover

def _info_de(canal: str, arq: Path, meta: dict) -> dict:
    return {"canal": canal, "existe": True, "nome_original": meta.get("nome_original") or arq.name,
            "duracao_s": round(float(meta.get("duracao_s") or 0), 2), "tamanho": arq.stat().st_size,
            "largura": meta.get("largura"), "altura": meta.get("altura"), "fps": meta.get("fps"),
            "audio": bool(meta.get("audio")), "enviado_em": meta.get("enviado_em"),
            "arquivo_url": f"/api/cta/{canal}/arquivo?v={arq.stat().st_mtime_ns}"}


def info(canal: str) -> dict:
    """Situação do CTA do canal: {"canal", "existe": False} ou os dados do arquivo guardado."""
    canal = chave(canal)
    arq = caminho(canal)
    if not arq.is_file():
        return {"canal": canal, "existe": False}
    meta = {}
    try:
        meta = json.loads(_meta(canal).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        pass
    if meta.get("tamanho") != arq.stat().st_size:   # arquivo trocado à mão: lê de novo do disco
        try:
            s = sondar(arq)
            meta = {"nome_original": arq.name, "duracao_s": (s["video"] or {}).get("duracao") or s["duracao"],
                    "largura": (s["video"] or {}).get("largura"), "altura": (s["video"] or {}).get("altura"),
                    "fps": (s["video"] or {}).get("fps"), "audio": bool(s["audio"]),
                    "enviado_em": datetime.fromtimestamp(arq.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")}
        except ErroCTA:
            pass
    return _info_de(canal, arq, meta)


def validar(arquivo: Path) -> dict:
    """Confere um candidato a CTA: tem vídeo, dura entre 0 e 60 s e é um MP4/MOV. Devolve o resultado do sondar."""
    s = sondar(arquivo)
    if not s["video"] or not s["video"]["largura"] or not s["video"]["altura"]:
        raise ErroCTA("O arquivo não tem imagem de vídeo")
    if not any(f in s["formato"] for f in ("mp4", "mov")):
        raise ErroCTA(f"O arquivo não é um MP4 (formato lido: {s['formato'] or '?'})")
    dur = s["video"]["duracao"]
    if dur <= 0.2:
        raise ErroCTA("O vídeo está vazio ou curto demais")
    if dur > MAX_DURACAO_S + 0.05:
        raise ErroCTA(f"O CTA tem {dur:.1f} s; o máximo é {MAX_DURACAO_S:.0f} s")
    return s


def guardar(canal: str, origem: Path, nome_original: str = "", mover: bool = False) -> dict:
    """Valida o arquivo `origem` e o coloca como CTA do canal, substituindo o anterior (troca atômica).
    `mover`: o arquivo é um temporário do upload e pode ser consumido em vez de copiado."""
    canal = chave(canal)
    if Path(nome_original or origem.name).suffix.lower() != ".mp4":
        raise ErroCTA("Envie um arquivo .mp4")
    s = validar(origem)
    pasta().mkdir(parents=True, exist_ok=True)
    tmp_video = pasta() / f"{canal}.mp4.tmp"
    tmp_meta = pasta() / f"{canal}.json.tmp"
    try:
        if mover:
            os.replace(origem, tmp_video)
        else:
            shutil.copyfile(origem, tmp_video)
        meta = {"nome_original": Path(nome_original or origem.name).name, "duracao_s": s["video"]["duracao"],
                "largura": s["video"]["largura"], "altura": s["video"]["altura"], "fps": s["video"]["fps"],
                "audio": bool(s["audio"]), "tamanho": tmp_video.stat().st_size,
                "enviado_em": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp_video, caminho(canal))
        os.replace(tmp_meta, _meta(canal))
    except OSError as e:
        raise ErroCTA(f"Não consegui gravar o CTA (o arquivo atual está em uso?): {e}")
    finally:
        tmp_video.unlink(missing_ok=True)
        tmp_meta.unlink(missing_ok=True)
    return info(canal)


def receber(canal: str, fluxo, nome_original: str) -> dict:
    """Recebe o upload (arquivo aberto), limita o tamanho, valida e guarda."""
    canal = chave(canal)
    if Path(nome_original or "").suffix.lower() != ".mp4":
        raise ErroCTA("Envie um arquivo .mp4")
    pasta().mkdir(parents=True, exist_ok=True)
    recebido = pasta() / f"{canal}.upload.tmp"
    try:
        total = 0
        with open(recebido, "wb") as f:
            while bloco := fluxo.read(1024 * 1024):
                total += len(bloco)
                if total > MAX_BYTES:
                    raise ErroCTA(f"Arquivo grande demais (máximo {MAX_BYTES // 1024 // 1024} MB)")
                f.write(bloco)
        if total == 0:
            raise ErroCTA("O arquivo veio vazio")
        return guardar(canal, recebido, nome_original, mover=True)
    finally:
        recebido.unlink(missing_ok=True)


def remover(canal: str) -> bool:
    canal = chave(canal)
    achou = False
    for p in (caminho(canal), _meta(canal)):
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                raise ErroCTA(f"Não consegui apagar {p.name}: {e}")
            achou = True
    return achou


def listar() -> list[dict]:
    if not pasta().is_dir():
        return []
    return [info(p.stem) for p in sorted(pasta().glob("*.mp4")) if NOME_OK.match(p.stem)]


# ---------------------------------------------------------------- anexar ao vídeo montado

def _marcador(video: Path) -> Path:
    return video.with_name(video.stem + ".cta.json")


def _assinatura(video: Path) -> dict:
    st = video.stat()
    return {"tamanho": st.st_size, "mtime_ns": st.st_mtime_ns}


def _ja_tem_cta(video: Path) -> bool:
    """O marcador guarda a assinatura (tamanho + data) do vídeo logo depois de receber o CTA; um vídeo montado de
    novo muda de assinatura, então só o mesmo arquivo já com CTA é pulado (nunca duplica)."""
    try:
        m = json.loads(_marcador(video).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return m.get("assinatura") == _assinatura(video)


def _grafo(p: dict, c: dict, d0: float, d1: float, w: int, h: int, fps: str) -> tuple[str, int, int]:
    """Filtro: principal [0] + CTA [1] -> [v][a]. Devolve (grafo, taxa, canais) do áudio de saída (taxa 0 = sem áudio)."""
    com_audio = bool(p["audio"] or c["audio"])
    taxa = (p["audio"] or c["audio"] or {}).get("taxa", 48000) if com_audio else 0
    canais = 1 if (p["audio"] or c["audio"] or {}).get("canais") == 1 else 2
    if p["audio"]:
        taxa, canais = p["audio"]["taxa"], (1 if p["audio"]["canais"] == 1 else 2)
    lay = "mono" if canais == 1 else "stereo"
    g = [f"[0:v]fps={fps},setsar=1,format=yuv420p,setpts=PTS-STARTPTS[v0]",
         f"[1:v]scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2:flags=lanczos,"
         f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps={fps},format=yuv420p,setpts=PTS-STARTPTS[v1]"]
    if com_audio:
        for i, (tem, dur) in enumerate(((p["audio"], d0), (c["audio"], d1))):
            if tem:
                # Trava o áudio na duração do vídeo do trecho, para o concat não deslocar o que vem depois.
                g.append(f"[{i}:a]aresample={taxa},aformat=sample_fmts=fltp:channel_layouts={lay},"
                         f"apad=whole_dur={dur:.4f},atrim=0:{dur:.4f},asetpts=PTS-STARTPTS[a{i}]")
            else:
                g.append(f"anullsrc=r={taxa}:cl={lay},atrim=0:{dur:.4f},asetpts=PTS-STARTPTS[a{i}]")
        g.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]")
    else:
        g.append("[v0][v1]concat=n=2:v=1:a=0[v]")
    return ";".join(g), taxa, canais


def anexar(video: Path, canal: str, saida: Path | None = None, *, crf: int | None = None, maxrate: str | None = None,
           preset: str | None = None) -> dict:
    """Acrescenta o CTA do canal no fim de `video`. Sem `saida`, troca o próprio arquivo (só depois de conferir).

    Devolve {"anexado": False, "motivo": "sem_cta" | "ja_anexado"} quando não há o que fazer, ou
    {"anexado": True, "saida", "duracao_antes", "duracao_cta", "duracao_depois", ...}. Falha de verdade = ErroCTA
    (o arquivo original fica intacto). Quem chama decide se uma falha derruba o fluxo."""
    video = Path(video)
    destino = Path(saida) if saida else video
    nome = chave(canal)
    arq_cta = caminho(nome)
    if not arq_cta.is_file():
        return {"anexado": False, "motivo": "sem_cta", "canal": nome}
    if not video.is_file():
        raise ErroCTA(f"Vídeo não encontrado: {video}")
    if _ja_tem_cta(video):
        if destino != video:
            shutil.copyfile(video, destino)
        return {"anexado": False, "motivo": "ja_anexado", "canal": nome, "saida": str(destino)}

    p, c = sondar(video), sondar(arq_cta)
    if not p["video"] or not p["video"]["largura"]:
        raise ErroCTA("O vídeo principal não tem imagem")
    if not c["video"]:
        raise ErroCTA("O CTA guardado não tem imagem; envie de novo")
    w, h, fps = p["video"]["largura"], p["video"]["altura"], p["video"]["fps"]
    d0, d1 = p["video"]["duracao"], c["video"]["duracao"]
    grafo, taxa, canais = _grafo(p, c, d0, d1, w, h, fps)

    crf = crf if crf is not None else 18
    tmp = destino.with_name(destino.stem + ".cta-tmp.mp4")
    cmd = [config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-i", str(arq_cta),
           "-filter_complex", grafo, "-map", "[v]"]
    if taxa:
        cmd += ["-map", "[a]"]
    cmd += ["-c:v", "libx264", "-preset", preset or config.MONTAGEM_PRESET, "-crf", str(crf), "-pix_fmt", "yuv420p"]
    m = re.fullmatch(r"(\d+)([MmKk])", maxrate or "")
    if m:
        cmd += ["-maxrate", maxrate, "-bufsize", f"{int(m.group(1)) * 2}{m.group(2)}"]
    if taxa:
        kbps = p["audio"]["kbps"] if p["audio"] and p["audio"]["kbps"] >= 64 else config.MONTAGEM_AUDIO_KBPS
        cmd += ["-c:a", "aac", "-b:a", f"{min(max(kbps, 96), 320)}k", "-ar", str(taxa), "-ac", str(canais)]
    cmd += ["-movflags", "+faststart", str(tmp)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **_sem_janela())
        if r.returncode != 0:
            raise ErroCTA(f"FFmpeg falhou ao anexar o CTA: {r.stderr[-1200:]}")
        s = sondar(tmp)
        esperado = d0 + d1
        if not s["video"] or (taxa and not s["audio"]):
            raise ErroCTA("O vídeo com CTA saiu sem imagem ou sem áudio")
        if abs(s["video"]["duracao"] - esperado) > max(TOLERANCIA_S, 2.5 / 30):
            raise ErroCTA(f"Duração inesperada com o CTA: {s['video']['duracao']:.2f} s (esperado {esperado:.2f} s)")
        try:
            os.replace(tmp, destino)
        except OSError as e:
            raise ErroCTA(f"Não consegui gravar o vídeo com CTA em {destino} (arquivo em uso?): {e}")
    finally:
        tmp.unlink(missing_ok=True)
    _marcador(destino).write_text(json.dumps({"canal": nome, "assinatura": _assinatura(destino),
                                              "cta": info(nome).get("nome_original")}, ensure_ascii=False),
                                  encoding="utf-8")
    return {"anexado": True, "canal": nome, "saida": str(destino), "duracao_antes": round(d0, 2),
            "duracao_cta": round(d1, 2), "duracao_depois": round(s["video"]["duracao"], 2),
            "resolucao": f"{w}x{h}", "fps": fps, "cta_sem_audio": not c["audio"]}
