"""Crop dinâmico 9:16: recorta o clipe já pronto (Fase 2) seguindo o rosto
mais proeminente em cada trecho, em vez de um crop central fixo.

Roda SÓ no clipe final (~1-2min, já cortado/limpo pela Fase 2), nunca no
vídeo bruto — ver [[video-maker-fase3-crop-dinamico]] na memória do projeto.

Limitação conhecida (v1): segue sempre o MAIOR rosto detectado no frame, não
"quem está falando" — funciona bem para um orador sozinho falando para a
câmera (o caso comum dos cortes deste projeto), mas não escolhe entre vários
rostos numa cena com mais de uma pessoa. Diarização (saber quem fala quando)
resolveria isso, mas ainda não foi implementada/confirmada como disponível.

Fluxo:
1. `detectar_trajetoria`: MediaPipe Face Detection a cada `passo_seg`
   segundos (não frame a frame — caro à toa, rosto não pula de posição tão
   rápido), devolve o centro/tamanho do maior rosto por amostra.
2. `preencher_gaps`: interpola linearmente os trechos sem detecção (rosto
   virou de perfil, saiu do quadro) a partir dos pontos válidos vizinhos.
3. `suavizar`: média móvel sobre a trajetória, para não tremer a cada
   pequena oscilação da detecção.
4. `renderizar`: gera os comandos de crop dinâmico (`sendcmd` do FFmpeg) a
   partir da trajetória suavizada e corta+escala para 9:16.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger("fase1")


def duracao_de(arquivo: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=noprint_wrappers=1:nokey=1", str(arquivo)],
                       capture_output=True, text=True, check=True)
    return float(r.stdout.strip())


def resolucao_de(arquivo: Path) -> tuple[int, int, float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate", "-of", "csv=p=0", str(arquivo)],
                       capture_output=True, text=True, check=True)
    w, h, fps_raw = r.stdout.strip().split(",")
    num, den = fps_raw.split("/")
    return int(w), int(h), float(num) / float(den)


# ---------------------------------------------------------------------------
# Etapa 1 — detecção
# ---------------------------------------------------------------------------

MODELO_PADRAO = Path(__file__).resolve().parent / "models" / "blaze_face_short_range.tflite"


def detectar_trajetoria(video: Path, passo_seg: float = 0.2, confianca_minima: float = 0.5,
                        modelo: Path = MODELO_PADRAO) -> list[dict | None]:
    """Amostra o vídeo a cada `passo_seg` segundos e acha o maior rosto de
    cada amostra. Devolve uma lista alinhada no tempo (índice i = tempo
    i*passo_seg); item é None quando nenhum rosto foi detectado ali.

    cx, cy, tamanho são frações de 0 a 1 (posição/tamanho relativos ao
    frame), não pixels — independe da resolução do vídeo.

    Usa a Tasks API do MediaPipe (>=0.10): o namespace antigo `mp.solutions`
    foi removido nas versões recentes, e o modelo (`.tflite`) precisa ser
    baixado à parte — não vem mais embutido no pacote pip.
    """
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    if not modelo.exists():
        raise FileNotFoundError(
            f"Modelo de detecção facial não encontrado em {modelo}. Baixe com:\n"
            f"  curl -L -o \"{modelo}\" "
            f"https://storage.googleapis.com/mediapipe-models/face_detector/"
            f"blaze_face_short_range/float16/latest/blaze_face_short_range.tflite")

    cap = cv2.VideoCapture(str(video))
    if not cap.isOpened():
        raise RuntimeError(f"não consegui abrir {video} com OpenCV")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    passo_frames = max(1, round(passo_seg * fps))

    opcoes = mp_vision.FaceDetectorOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(modelo)),
        min_detection_confidence=confianca_minima)
    pontos: list[dict | None] = []
    with mp_vision.FaceDetector.create_from_options(opcoes) as detector:
        indice = 0
        while indice < total_frames:
            cap.set(cv2.CAP_PROP_POS_FRAMES, indice)
            ok, frame = cap.read()
            if not ok:
                break
            altura, largura = frame.shape[:2]
            imagem = mp.Image(image_format=mp.ImageFormat.SRGB,
                              data=cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            resultado = detector.detect(imagem)
            pontos.append(_maior_rosto(resultado, largura, altura))
            indice += passo_frames
    cap.release()
    achados = sum(1 for p in pontos if p)
    log.info("   trajetória: %d amostras (passo %.2fs), rosto achado em %d (%.0f%%)",
             len(pontos), passo_seg, achados, 100 * achados / max(1, len(pontos)))
    return pontos


def _maior_rosto(resultado, largura_frame: int, altura_frame: int) -> dict | None:
    if not resultado.detections:
        return None
    maior = max(resultado.detections, key=lambda d: d.bounding_box.width)
    box = maior.bounding_box  # em pixels, nesta versão da API (não fração 0-1)
    return {
        "cx": (box.origin_x + box.width / 2) / largura_frame,
        "cy": (box.origin_y + box.height / 2) / altura_frame,
        "tamanho": box.width / largura_frame,
    }


# ---------------------------------------------------------------------------
# Etapas 2-3 — preenche gaps e suaviza
# ---------------------------------------------------------------------------

def preencher_gaps(pontos: list[dict | None]) -> list[dict]:
    """Interpola linearmente os `None` a partir dos vizinhos válidos. Gaps
    nas pontas (antes do 1º ou depois do último ponto válido) copiam o valor
    válido mais próximo (não dá para interpolar sem os dois lados)."""
    indices_validos = [i for i, p in enumerate(pontos) if p is not None]
    if not indices_validos:
        raise ValueError("nenhum rosto detectado no vídeo inteiro")
    saida: list[dict] = []
    for i in range(len(pontos)):
        if pontos[i] is not None:
            saida.append(pontos[i])
            continue
        antes = max((j for j in indices_validos if j < i), default=None)
        depois = min((j for j in indices_validos if j > i), default=None)
        if antes is None:
            saida.append(pontos[depois])
        elif depois is None:
            saida.append(pontos[antes])
        else:
            t = (i - antes) / (depois - antes)
            saida.append({k: pontos[antes][k] + t * (pontos[depois][k] - pontos[antes][k])
                          for k in ("cx", "cy", "tamanho")})
    return saida


def suavizar(pontos: list[dict], janela_amostras: int) -> list[dict]:
    """Média móvel centrada, para a câmera não tremer a cada micro-ajuste da
    detecção quadro a quadro."""
    n = len(pontos)
    meia = janela_amostras // 2
    saida = []
    for i in range(n):
        ini, fim = max(0, i - meia), min(n, i + meia + 1)
        janela = pontos[ini:fim]
        saida.append({k: sum(p[k] for p in janela) / len(janela) for k in ("cx", "cy", "tamanho")})
    return saida


# ---------------------------------------------------------------------------
# Etapa 4 — comandos de crop + render
# ---------------------------------------------------------------------------

ALVO_CROP = "crop@c"


def gerar_comandos(pontos: list[dict], passo_seg: float, largura_orig: int, altura_orig: int,
                   crop_w: int, crop_h: int) -> str:
    """Um comando `sendcmd` do FFmpeg por amostra: a posição x,y do crop
    (canto superior esquerdo) centralizada no rosto, sem deixar a janela
    sair da borda do frame original.

    O alvo tem que ser o nome COMPLETO da instância (`crop@c`), não só `c`:
    com o nome curto o FFmpeg responde "Function not implemented" a cada
    comando (só no log debug, sem erro) e o crop fica parado no x=0 inicial.
    """
    linhas = []
    for i, p in enumerate(pontos):
        cx_px = p["cx"] * largura_orig
        cy_px = p["cy"] * altura_orig
        x = min(max(0, cx_px - crop_w / 2), largura_orig - crop_w)
        y = min(max(0, cy_px - crop_h / 2), altura_orig - crop_h)
        tempo = i * passo_seg
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


def processar(video: Path, destino: Path, passo_seg: float = 0.2, janela_suavizacao_seg: float = 1.0,
             confianca_minima: float = 0.5, largura_saida: int = 1080,
             altura_saida: int = 1920, modelo: Path = MODELO_PADRAO) -> None:
    """Ponta a ponta: detecta, preenche, suaviza e renderiza o crop 9:16."""
    largura_orig, altura_orig, _fps = resolucao_de(video)
    crop_h = altura_orig
    crop_w = round((altura_orig * 9 / 16) / 2) * 2  # múltiplo de 2 (exigência de encoder)
    if crop_w > largura_orig:  # vídeo já mais estreito que 9:16 — usa a largura toda
        crop_w = largura_orig - (largura_orig % 2)
        crop_h = round((crop_w * 16 / 9) / 2) * 2

    brutos = detectar_trajetoria(video, passo_seg, confianca_minima, modelo)
    preenchidos = preencher_gaps(brutos)
    janela_amostras = max(1, round(janela_suavizacao_seg / passo_seg))
    suaves = suavizar(preenchidos, janela_amostras)
    comandos = gerar_comandos(suaves, passo_seg, largura_orig, altura_orig, crop_w, crop_h)
    renderizar(video, destino, comandos, crop_w, crop_h, largura_saida, altura_saida)
