"""Crop dinâmico 9:16 que se comporta como um cinegrafista, não como um
rastreador: segura o enquadramento parado e só mexe quando precisa.

Roda SÓ no clipe final (~1-2min, já cortado/limpo pela Fase 2), nunca no
vídeo bruto — ver [[video-maker-fase3-crop-dinamico]] na memória do projeto.

Regras (vieram de testar a v1, que seguia o rosto amostra a amostra com média
móvel: a câmera ficava "procurando" a cada palavra e, nas trocas de câmera do
vídeo original, deslizava de um plano pro outro):

1. **Planos**: o vídeo de origem já tem cortes de câmera (programa de TV,
   podcast multicâmera). Detectados com o `scdet` do FFmpeg; em cada corte o
   crop PULA para a nova posição — nunca desliza através do corte.
2. **Câmera parada**: dentro de um plano a posição fica fixa. Só re-enquadra
   se o rosto ficar fora de uma zona morta por `tempo_fora` segundos, e aí faz
   um movimento curto (`tempo_pan`) e para de novo; nunca antes de ter ficado
   parada `segura_minimo` segundos.
3. **Quem fala**: num plano com mais de um rosto, vai para quem está
   falando. As trocas de pessoa só acontecem nas trocas de turno de fala
   (`falantes.py`, pela voz); qual rosto é o de quem fala sai do movimento
   da boca (blendshape `jawOpen` do MediaPipe Face Landmarker) durante o
   turno. Turnos curtos (interjeição, "uhum") não trocam.
"""

from __future__ import annotations

import logging
import re
import statistics
import subprocess
from pathlib import Path

log = logging.getLogger("fase1")

MODELO_PADRAO = Path(__file__).resolve().parent / "models" / "face_landmarker.task"
URL_MODELO = ("https://storage.googleapis.com/mediapipe-models/face_landmarker/"
              "face_landmarker/float16/latest/face_landmarker.task")


def resolucao_de(arquivo: Path) -> tuple[int, int, float, float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate:format=duration", "-of", "json", str(arquivo)],
                       capture_output=True, text=True, check=True)
    import json
    d = json.loads(r.stdout)
    s = d["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    return int(s["width"]), int(s["height"]), float(num) / float(den), float(d["format"]["duration"])


# ---------------------------------------------------------------------------
# Etapa 1 — cortes de câmera do vídeo de origem
# ---------------------------------------------------------------------------

def cortes_de_cena(video: Path, limiar: float = 10.0) -> list[float]:
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(video), "-an", "-vf",
                        f"scdet=threshold={limiar}:sc_pass=1", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    tempos = [float(t) for t in re.findall(r"lavfi\.scd\.time: ([0-9.]+)", r.stderr)]
    log.info("   cortes de câmera: %d", len(tempos))
    return tempos


# ---------------------------------------------------------------------------
# Etapa 2 — rostos (posição + abertura da boca) ao longo do tempo
# ---------------------------------------------------------------------------

def detectar_rostos(video: Path, passo_seg: float = 0.1, confianca_minima: float = 0.5,
                    max_rostos: int = 4, modelo: Path = MODELO_PADRAO) -> tuple[list[float], list[list[dict]]]:
    """Lê o vídeo em sequência (sem seek — seek do OpenCV em H.264 é
    impreciso) e roda o Face Landmarker a cada `passo_seg`. Devolve
    (tempos, rostos_por_amostra); cada rosto é {cx, cy, tamanho, boca} em
    frações do frame, boca = jawOpen (0 fechada .. 1 aberta)."""
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    if not modelo.exists():
        raise FileNotFoundError(f"Modelo não encontrado em {modelo}. Baixe com:\n"
                                f"  curl -L -o \"{modelo}\" {URL_MODELO}")

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"não consegui abrir {video} com OpenCV")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    passo_frames = max(1, round(passo_seg * fps))

    opcoes = mp_vision.FaceLandmarkerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(modelo)),
        running_mode=mp_vision.RunningMode.VIDEO, num_faces=max_rostos,
        min_face_detection_confidence=confianca_minima, output_face_blendshapes=True)
    tempos: list[float] = []
    amostras: list[list[dict]] = []
    with mp_vision.FaceLandmarker.create_from_options(opcoes) as detector:
        indice = 0
        while True:
            ok = cap.grab()
            if not ok:
                break
            if indice % passo_frames == 0:
                ok, frame = cap.retrieve()
                if not ok:
                    break
                t = indice / fps
                imagem = mp.Image(image_format=mp.ImageFormat.SRGB,
                                  data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                res = detector.detect_for_video(imagem, int(round(t * 1000)))
                tempos.append(t)
                amostras.append(_rostos(res))
            indice += 1
    cap.release()
    com = sum(1 for a in amostras if a)
    multi = sum(1 for a in amostras if len(a) > 1)
    log.info("   rostos: %d amostras (passo %.2fs), com rosto %.0f%%, com 2+ rostos %.0f%%",
             len(amostras), passo_seg, 100 * com / max(1, len(amostras)),
             100 * multi / max(1, len(amostras)))
    return tempos, amostras


def _rostos(res) -> list[dict]:
    saida = []
    for i, pontos in enumerate(res.face_landmarks):
        xs = [p.x for p in pontos]
        ys = [p.y for p in pontos]
        boca = 0.0
        if res.face_blendshapes:
            boca = next((c.score for c in res.face_blendshapes[i] if c.category_name == "jawOpen"), 0.0)
        saida.append({"cx": (min(xs) + max(xs)) / 2, "cy": (min(ys) + max(ys)) / 2,
                      "tamanho": max(xs) - min(xs), "boca": boca})
    return saida


# ---------------------------------------------------------------------------
# Etapa 3 — em cada plano, qual rosto enquadrar em cada momento
# ---------------------------------------------------------------------------

def _trilhas(amostras: list[list[dict]], distancia_max: float = 0.08) -> list[dict[int, dict]]:
    """Liga os rostos de amostras seguidas numa mesma "trilha" (a mesma
    pessoa) pela proximidade horizontal. Devolve [ {indice_amostra: rosto} ]."""
    trilhas: list[dict[int, dict]] = []
    ultimo_cx: list[float] = []
    for i, rostos in enumerate(amostras):
        livres = set(range(len(trilhas)))
        for r in sorted(rostos, key=lambda r: -r["tamanho"]):
            melhor = min(livres, key=lambda k: abs(ultimo_cx[k] - r["cx"]), default=None)
            if melhor is not None and abs(ultimo_cx[melhor] - r["cx"]) <= distancia_max:
                trilhas[melhor][i] = r
                ultimo_cx[melhor] = r["cx"]
                livres.discard(melhor)
            else:
                trilhas.append({i: r})
                ultimo_cx.append(r["cx"])
    return trilhas


def _movimento_boca(trilha: dict[int, dict], indices: list[int]) -> float:
    valores = [trilha[i]["boca"] for i in indices if i in trilha]
    if len(valores) < 3:
        return 0.0
    # variação da boca, não abertura média: quem fala abre e fecha; quem
    # escuta de boca entreaberta tem média alta e variação baixa.
    return statistics.pstdev(valores)


def escolher_alvos(tempos: list[float], amostras: list[list[dict]], planos: list[tuple[float, float]],
                   turnos: list[dict], turno_minimo: float) -> list[tuple[float, float] | None]:
    """Para cada amostra, o (cx, cy) do rosto a enquadrar (ou None)."""
    alvos: list[tuple[float, float] | None] = [None] * len(tempos)
    for ini, fim in planos:
        idx = [i for i, t in enumerate(tempos) if ini <= t < fim]
        if not idx:
            continue
        trilhas = [t for t in _trilhas([amostras[i] for i in idx])]
        # re-indexa as trilhas para o índice global
        trilhas = [{idx[k]: r for k, r in tr.items()} for tr in trilhas]
        # descarta trilhas que quase não aparecem (detecção espúria)
        trilhas = [tr for tr in trilhas if len(tr) >= max(3, 0.25 * len(idx))] or trilhas
        if not trilhas:
            continue

        # trechos do plano entre trocas de turno (só turnos longos contam)
        fronteiras = [ini] + [t["inicio"] for t in turnos
                              if ini < t["inicio"] < fim and t["fim"] - t["inicio"] >= turno_minimo] + [fim]
        escolhida = None
        for a, b in zip(fronteiras, fronteiras[1:]):
            idx_trecho = [i for i in idx if a <= tempos[i] < b]
            if not idx_trecho:
                continue
            if len(trilhas) == 1:
                escolhida = trilhas[0]
            else:
                em_fala = [i for i in idx_trecho if any(t["inicio"] <= tempos[i] <= t["fim"] for t in turnos)]
                base = em_fala or idx_trecho
                candidata = max(trilhas, key=lambda tr: (_movimento_boca(tr, base),
                                                         sum(1 for i in base if i in tr)))
                if _movimento_boca(candidata, base) < 0.02 and escolhida is not None:
                    candidata = escolhida  # ninguém mexe a boca: fica onde está
                elif _movimento_boca(candidata, base) < 0.02:
                    candidata = max(trilhas, key=lambda tr: statistics.mean(r["tamanho"] for r in tr.values()))
                escolhida = candidata
            ultimo = None
            for i in idx_trecho:
                r = escolhida.get(i)
                if r is None and ultimo and amostras[i]:
                    # a trilha escolhida sumiu (rosto saiu e voltou em outro
                    # ponto, detecção falhou): pega o rosto mais perto de onde
                    # estava, se não for longe demais para ser outra pessoa
                    perto = min(amostras[i], key=lambda f: abs(f["cx"] - ultimo[0]))
                    r = perto if abs(perto["cx"] - ultimo[0]) <= 0.3 else None
                alvos[i] = (r["cx"], r["cy"]) if r else None
                ultimo = alvos[i] or ultimo
    return alvos


# ---------------------------------------------------------------------------
# Etapa 4 — movimento de câmera: parada, re-enquadra só quando precisa
# ---------------------------------------------------------------------------

def _mediana_proxima(alvos, tempos, i0: int, fim: float, janela: float) -> tuple[float, float] | None:
    vals = [alvos[i] for i in range(i0, len(tempos)) if tempos[i] < min(fim, tempos[i0] + janela) and alvos[i]]
    if not vals:
        return None
    return statistics.median(v[0] for v in vals), statistics.median(v[1] for v in vals)


def trajetoria_camera(tempos: list[float], alvos: list[tuple[float, float] | None],
                      planos: list[tuple[float, float]], fps: float, zona_morta: float,
                      tempo_fora: float, tempo_pan: float, segura_minimo: float) -> list[tuple[float, float, float]]:
    """Keyframes (tempo, cx, cy) do centro do crop, em frações do frame.
    Entre keyframes a posição fica parada (sendcmd só muda quando mandado);
    pans são emitidos quadro a quadro com ease-in-out."""
    keys: list[tuple[float, float, float]] = []
    ultimo = (0.5, 0.5)
    for ini, fim in planos:
        idx = [i for i, t in enumerate(tempos) if ini <= t < fim]
        if not idx:
            continue
        pos = _mediana_proxima(alvos, tempos, idx[0], fim, 1.0) or ultimo
        # meio quadro antes do corte, para o 1º quadro do plano novo já sair certo
        keys.append((max(0.0, ini - 0.5 / fps), *pos))
        parado_desde = ini
        fora_desde = None
        alvo_anterior = None
        for i in idx:
            t, alvo = tempos[i], alvos[i]
            if alvo is None:
                continue
            # troca de pessoa dentro do plano (alvo pulou longe): corte seco
            if alvo_anterior and abs(alvo[0] - alvo_anterior[0]) > 0.15:
                pos = _mediana_proxima(alvos, tempos, i, fim, 1.0) or alvo
                keys.append((t, *pos))
                parado_desde, fora_desde = t, None
            alvo_anterior = alvo
            if abs(alvo[0] - pos[0]) <= zona_morta and abs(alvo[1] - pos[1]) <= zona_morta:
                fora_desde = None
                continue
            fora_desde = fora_desde if fora_desde is not None else t
            if t - fora_desde >= tempo_fora and t - parado_desde >= segura_minimo:
                novo = _mediana_proxima(alvos, tempos, i, fim, 1.0) or alvo
                duracao = min(tempo_pan, max(0.0, fim - t - 0.05))
                n = max(1, round(duracao * fps))
                for k in range(1, n + 1):
                    s = k / n
                    e = s * s * (3 - 2 * s)  # ease-in-out
                    keys.append((t + k / fps, pos[0] + e * (novo[0] - pos[0]), pos[1] + e * (novo[1] - pos[1])))
                pos = novo
                parado_desde, fora_desde = t + duracao, None
        ultimo = pos
    return keys


# ---------------------------------------------------------------------------
# Etapa 5 — comandos de crop + render
# ---------------------------------------------------------------------------

ALVO_CROP = "crop@c"


def gerar_comandos(keys: list[tuple[float, float, float]], largura_orig: int, altura_orig: int,
                   crop_w: int, crop_h: int) -> str:
    """Um comando `sendcmd` por keyframe: a posição x,y do crop (canto
    superior esquerdo) centralizada no alvo, sem sair da borda do frame.

    O alvo tem que ser o nome COMPLETO da instância (`crop@c`), não só `c`:
    com o nome curto o FFmpeg responde "Function not implemented" a cada
    comando (só no log debug, sem erro) e o crop fica parado no x=0 inicial.
    """
    linhas = []
    for tempo, cx, cy in keys:
        x = min(max(0, cx * largura_orig - crop_w / 2), largura_orig - crop_w)
        y = min(max(0, cy * altura_orig - crop_h / 2), altura_orig - crop_h)
        linhas.append(f"{tempo:.3f} {ALVO_CROP} x {x:.1f}, {ALVO_CROP} y {y:.1f};")
    return "\n".join(linhas)


def renderizar(video: Path, destino: Path, comandos: str, crop_w: int, crop_h: int,
               largura_saida: int, altura_saida: int) -> None:
    arquivo_comandos = destino.with_suffix(".cmds.txt")
    arquivo_comandos.write_text(comandos, encoding="utf-8")
    try:
        # O ":" de "C:" colide com o separador chave=valor do próprio filtro —
        # mesmo entre aspas simples, precisa ser escapado.
        caminho_escapado = arquivo_comandos.as_posix().replace(":", r"\:")
        filtro = (
            f"sendcmd=f='{caminho_escapado}',"
            f"{ALVO_CROP}=w={crop_w}:h={crop_h}:x=0:y=0,"
            f"scale={largura_saida}:{altura_saida}"
        )
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", filtro,
                        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
                        "-c:a", "copy", str(destino)], check=True, capture_output=True)
    finally:
        arquivo_comandos.unlink(missing_ok=True)


def processar(video: Path, destino: Path, turnos: list[dict], cfg: dict,
              modelo: Path = MODELO_PADRAO) -> dict:
    """Ponta a ponta. `turnos` vem de falantes.detectar (lista vazia = não
    sabe quem fala; aí num plano com 2+ rostos decide só pela boca)."""
    cd, cc, cs = cfg["deteccao"], cfg["camera"], cfg["saida"]
    largura, altura, fps, duracao = resolucao_de(video)
    crop_h = altura
    crop_w = round((altura * 9 / 16) / 2) * 2  # múltiplo de 2 (exigência de encoder)
    if crop_w > largura:  # vídeo já mais estreito que 9:16 — usa a largura toda
        crop_w = largura - (largura % 2)
        crop_h = round((crop_w * 16 / 9) / 2) * 2

    cortes = cortes_de_cena(video, cd["limiar_corte"])
    limites = [0.0] + cortes + [duracao]
    planos = [(a, b) for a, b in zip(limites, limites[1:]) if b > a]

    tempos, amostras = detectar_rostos(video, cd["passo_seg"], cd["confianca_minima"],
                                       cd["max_rostos"], modelo)
    alvos = escolher_alvos(tempos, amostras, planos, turnos, cc["turno_minimo"])
    keys = trajetoria_camera(tempos, alvos, planos, fps, cc["zona_morta"], cc["tempo_fora"],
                             cc["tempo_pan"], cc["segura_minimo"])
    renderizar(video, destino, gerar_comandos(keys, largura, altura, crop_w, crop_h),
               crop_w, crop_h, cs["largura"], cs["altura"])
    return {"planos": len(planos), "keyframes": len(keys)}
