"""Crop 9:16 que segue quem está falando, visto pela boca + voz (LR-ASD).

    python falante_ativo.py <clipe_16x9.mp4> <saida_9x16.mp4> [--config config_crop.yaml]

Roda como processo à parte (o finalizar.py chama assim): quando termina, toda
a VRAM e a RAM usadas voltam para o sistema, sem depender de coletor.

Mesma lógica da demonstração do LR-ASD (Columbia_test.py: seguir rostos por
sobreposição dentro de cada plano, interpolar, suavizar com mediana de 13
quadros, recortar com margem de 40%) — o que muda é COMO, porque a demo
gastava 3,5-7s por segundo de vídeo e esta versão 0,33-0,42s (medido em
26/09/2026 num podcast de 2 e de 5 pessoas, ~90% das decisões iguais às da
demo):

- Decodificação na placa (torchcodec/NVDEC): os quadros já chegam na VRAM,
  sem passar 4,7 GB por minuto de vídeo por um pipe do ffmpeg.
- O cinza de todos os quadros fica em RAM pinada (~3 GB por minuto em
  1080p); o recorte dos rostos sobe de lá direto para a GPU (roi_align), sem
  gravar um vídeo por rosto.
- Detector SCRFD (det_10g) na GPU a cada 5 quadros (+ o 1º de cada plano), no
  lugar do S3FD em todo quadro.
- Troca de câmera com a mesma conta do scdet do ffmpeg, na mesma passada.
- Pontuação do LR-ASD em lote, 4 janelas (1, 2, 4, 6s) em vez de 11: mesma
  precisão, 2,7x mais rápido. Meia precisão (fp16) foi testada e piorou tudo.

Ainda dá para ganhar ~3s por minuto detectando em 640x352 em vez de 960x544
(o SCRFD cai de ~6s para ~2s), mas rosto pequeno de uma grade de 5 pessoas
pode sumir — suspenso de propósito; está como foi testado e aprovado.

Depois de saber quem fala em cada quadro, o enquadramento usa as mesmas
regras de cinegrafista do crop_dinamico.py (câmera parada, pula nas trocas de
plano, corte seco na troca de pessoa, turno curto não troca).
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn.functional as F

AQUI = Path(__file__).resolve().parent
MODELO_ROSTO = AQUI / "models" / "det_10g.onnx"                        # SCRFD (InsightFace buffalo_l)
MODELO_ASD = AQUI / "models" / "lrasd_finetuning_TalkSet.model"      # LR-ASD, ajustado no TalkSet

DET_LARGURA = 960            # detecção em 960x544 (rosto pequeno de grade ainda aparece) — ver docstring
DET_PASSO = 5                # detecta a cada 5 quadros
DET_LIMIAR = 0.6
FPS = 25                     # o LR-ASD espera 25 quadros/s (4 de áudio por quadro)
IOU_TRILHA = 0.5             # = demo
FALHAS_MAX = 10              # = demo (quadros)
TRILHA_MIN = 10              # = demo (quadros)
MARGEM = 0.40                # = demo (cropScale)
JANELAS = [1, 2, 4, 6]       # s; a demo usa 11 (1,1,1,2,2,2,3,3,4,5,6) com a mesma precisão
ALTURA_CINZA_MAX = 1080      # vídeo 4K: o cinza guardado na RAM desce para 1080p

log = logging.getLogger("fase1")


def sonda(video: Path) -> tuple[int, int, float]:
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream=width,height,r_frame_rate", "-of", "json", str(video)],
                       capture_output=True, text=True, check=True)
    s = json.loads(r.stdout)["streams"][0]
    n, d = s["r_frame_rate"].split("/")
    return s["width"], s["height"], float(n) / float(d)


# ---------------------------------------------------------------------------
# Modelos
# ---------------------------------------------------------------------------

class SCRFD:
    """det_10g convertido de ONNX para PyTorch (onnx2torch). Em lote >1 a rede
    convertida mistura os quadros — por isso um quadro por passada."""

    def __init__(self, caminho: Path):
        import onnx
        import onnx2torch
        self.rede = onnx2torch.convert(onnx.load(str(caminho))).cuda().eval()
        self._centros: dict = {}

    def _ancoras(self, h, w, passo):
        chave = (h, w, passo)
        if chave not in self._centros:
            ys, xs = torch.meshgrid(torch.arange(h // passo), torch.arange(w // passo), indexing="ij")
            c = torch.stack([xs, ys], -1).reshape(-1, 2).float().cuda() * passo
            self._centros[chave] = c.repeat_interleave(2, 0)       # 2 âncoras por posição
        return self._centros[chave]

    @torch.no_grad()
    def detecta(self, rgb: torch.Tensor, limiar: float) -> torch.Tensor:
        """rgb (1,3,H,W) 0-255 -> (N,5) [x1,y1,x2,y2,nota]."""
        from torchvision.ops import nms
        _, _, h, w = rgb.shape
        saidas = self.rede((rgb - 127.5) / 128.0)
        caixas, notas = [], []
        for k, passo in enumerate((8, 16, 32)):
            n = (h // passo) * (w // passo) * 2
            sc = saidas[k].view(n)
            bb = saidas[k + 3].view(n, 4) * passo
            ok = sc > limiar
            if ok.any():
                c = self._ancoras(h, w, passo)[ok]
                d = bb[ok]
                caixas.append(torch.stack([c[:, 0] - d[:, 0], c[:, 1] - d[:, 1],
                                           c[:, 0] + d[:, 2], c[:, 1] + d[:, 3]], 1))
                notas.append(sc[ok])
        if not caixas:
            return torch.zeros((0, 5), device="cuda")
        cx, nt = torch.cat(caixas), torch.cat(notas)
        manter = nms(cx, nt, 0.4)
        return torch.cat([cx[manter], nt[manter, None]], 1)


class LRASD(torch.nn.Module):
    def __init__(self, caminho: Path):
        super().__init__()
        from lrasd.Model import ASD_Model
        from lrasd.loss import lossAV
        self.model = ASD_Model()
        self.lossAV = lossAV()
        meu = self.state_dict()
        for nome, valor in torch.load(str(caminho), map_location="cpu").items():
            nome = nome.replace("module.", "")
            if nome in meu and meu[nome].shape == valor.shape:   # o peso também traz lossV (treino)
                meu[nome].copy_(valor)
        self.cuda().eval()

    @torch.no_grad()
    def nota(self, audio: torch.Tensor, video: torch.Tensor) -> np.ndarray:
        """audio (B, T*4, 13) MFCC, video (B, T, 112, 112) -> nota por quadro (>0 = falando)."""
        m = self.model
        o = m.forward_audio_visual_backend(m.forward_audio_frontend(audio), m.forward_visual_frontend(video))
        return self.lossAV.forward(o, labels=None)


# ---------------------------------------------------------------------------
# Trilhas de rosto (mesmo algoritmo da demo, sobre detecções esparsas)
# ---------------------------------------------------------------------------

def _iou(a, b):
    xa, ya, xb, yb = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0, xb - xa) * max(0, yb - ya)
    return inter / float((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter)


def trilhas_do_plano(faces_por_quadro: list[list[dict]], ini: int, fim: int) -> list[dict]:
    from scipy.interpolate import interp1d
    trilhas = []
    faces = [list(f) for f in faces_por_quadro]
    while True:
        trilha = []
        for quadro in faces:
            for face in list(quadro):
                if not trilha:
                    trilha.append(face)
                    quadro.remove(face)
                elif face["frame"] - trilha[-1]["frame"] <= FALHAS_MAX:
                    if _iou(face["bbox"], trilha[-1]["bbox"]) > IOU_TRILHA:
                        trilha.append(face)
                        quadro.remove(face)
                        break
                else:
                    break
        if not trilha:
            break
        f = np.array([x["frame"] for x in trilha])
        b = np.array([x["bbox"] for x in trilha])
        # detecção esparsa: estende até DET_PASSO-1 quadros para cada lado, sem sair do plano
        a0, a1 = max(ini, f[0] - (DET_PASSO - 1)), min(fim, f[-1] + (DET_PASSO - 1))
        if a1 - a0 + 1 <= TRILHA_MIN:
            continue
        quadros = np.arange(a0, a1 + 1)
        if len(f) == 1:
            bi = np.repeat(b, len(quadros), 0)
        else:
            bi = np.stack([interp1d(f, b[:, j], bounds_error=False, fill_value=(b[0, j], b[-1, j]))(quadros)
                           for j in range(4)], 1)
        trilhas.append({"frame": quadros, "bbox": bi})
    return trilhas


# ---------------------------------------------------------------------------
# Análise: quem fala em cada quadro
# ---------------------------------------------------------------------------

def analisar(video: Path) -> dict:
    """Devolve {"W","H","n","cortes","trilhas","notas","tempos"}: trilhas com
    frame/x/y/s (centro e meio-lado do rosto, pixels do vídeo, suavizados) e,
    para cada uma, a nota do LR-ASD por quadro (>0 = falando)."""
    import python_speech_features
    from scipy import signal
    from torchcodec.decoders import VideoDecoder
    from torchvision.ops import roi_align

    tempos: dict[str, float] = {}
    t0 = time.perf_counter()
    W, H, _ = sonda(video)
    dh = int(round(H * DET_LARGURA / W / 32)) * 32
    escala = W / DET_LARGURA, H / dh
    g = min(1.0, ALTURA_CINZA_MAX / H)
    Hc, Wc = round(H * g), round(W * g)

    det = SCRFD(MODELO_ROSTO)
    asd = LRASD(MODELO_ASD)
    tempos["modelos"] = time.perf_counter() - t0

    # 1. decodifica na GPU, guarda o cinza na RAM, acha trocas de câmera e rostos
    t0 = time.perf_counter()
    dec = VideoDecoder(str(video), device="cuda")
    fps_origem = dec.metadata.average_fps
    n = int(len(dec) * FPS / fps_origem)
    # 25 quadros/s como a demo: o quadro de origem mais próximo de cada instante
    origem = [min(len(dec) - 1, int(round(k * fps_origem / FPS))) for k in range(n)]
    cinza = torch.empty((n, Hc, Wc), dtype=torch.uint8).pin_memory()
    pesos = torch.tensor([0.299, 0.587, 0.114], device="cuda").view(3, 1, 1)
    faces: dict[int, list] = {}
    cortes, anterior, mafd_ant, ultimo_i, quadro = [], None, 0.0, -1, None
    with torch.no_grad():
        for q, i in enumerate(origem):
            if i != ultimo_i:
                quadro = dec[i]                          # (3, H, W) uint8, já na GPU
                ultimo_i = i
            y = (quadro.float() * pesos).sum(0)
            if g < 1:
                y = F.interpolate(y[None, None], size=(Hc, Wc), mode="area")[0, 0]
            cinza[q].copy_(y.round().to(torch.uint8), non_blocking=True)
            # troca de câmera: mesma conta do scdet do ffmpeg (limiar 10), em 160x90
            peq = F.interpolate(y[None, None], size=(90, 160), mode="area")[0, 0]
            if anterior is not None:
                mafd = float((peq - anterior).abs().mean()) * 100 / 255
                if min(mafd, abs(mafd - mafd_ant)) >= 10:
                    cortes.append(q)
                mafd_ant = mafd
            anterior = peq
            if q % DET_PASSO == 0 or (cortes and cortes[-1] == q):
                rgb = F.interpolate(quadro[None].float(), size=(dh, DET_LARGURA), mode="area")
                faces[q] = [{"frame": q, "bbox": [c[0] * escala[0], c[1] * escala[1],
                                                  c[2] * escala[0], c[3] * escala[1]]}
                            for c in det.detecta(rgb, DET_LIMIAR).tolist()]
    torch.cuda.synchronize()
    del dec, quadro, det
    tempos["decodificar+rostos"] = time.perf_counter() - t0

    # 2. trilhas por plano, suavizadas como na demo
    t0 = time.perf_counter()
    planos = [(a, b - 1) for a, b in zip([0] + cortes, cortes + [n]) if b - a > TRILHA_MIN]
    faces_lista = [faces.get(i, []) for i in range(n)]
    trilhas = []
    for a, b in planos:
        trilhas += trilhas_do_plano(faces_lista[a: b + 1], a, b)
    for t in trilhas:
        bb = t["bbox"]
        t["s"] = signal.medfilt(np.maximum(bb[:, 3] - bb[:, 1], bb[:, 2] - bb[:, 0]) / 2, 13)
        t["y"] = signal.medfilt((bb[:, 1] + bb[:, 3]) / 2, 13)
        t["x"] = signal.medfilt((bb[:, 0] + bb[:, 2]) / 2, 13)
        del t["bbox"]

    # 3. recorte 112x112 de cada rosto, da RAM para a GPU
    pad = int(max((t["s"].max() for t in trilhas), default=1) * g * (1 + 2 * MARGEM)) + 2
    recortes = [torch.empty((len(t["frame"]), 112, 112), dtype=torch.uint8, device="cuda") for t in trilhas]
    por_quadro: dict[int, list[tuple[int, int]]] = {}
    for ti, t in enumerate(trilhas):
        for k, q in enumerate(t["frame"]):
            por_quadro.setdefault(int(q), []).append((ti, k))
    for q in sorted(por_quadro):
        y = cinza[q].cuda(non_blocking=True).view(1, 1, Hc, Wc).float()
        y = F.pad(y, (pad, pad, pad, pad), value=110.0)
        caixas = []
        for ti, k in por_quadro[q]:
            t = trilhas[ti]
            bs, my, mx = t["s"][k] * g, t["y"][k] * g + pad, t["x"][k] * g + pad
            x0, y0 = mx - bs * (1 + MARGEM), my - bs
            lado = 2 * bs * (1 + MARGEM)
            # a demo redimensiona o recorte para 224 e pega o miolo 112x112
            caixas.append([0, x0 + lado / 4, y0 + lado / 4, x0 + 3 * lado / 4, y0 + 3 * lado / 4])
        out = roi_align(y, torch.tensor(caixas, device="cuda", dtype=torch.float32), (112, 112),
                        1.0, 2, True).round().clamp(0, 255).to(torch.uint8)
        for j, (ti, k) in enumerate(por_quadro[q]):
            recortes[ti][k] = out[j, 0]
    del cinza
    tempos["trilhas+recortes"] = time.perf_counter() - t0

    # 4. áudio (MFCC) e nota do LR-ASD por quadro
    t0 = time.perf_counter()
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000",
                        "-f", "s16le", "-"], capture_output=True, check=True)
    audio = np.frombuffer(r.stdout, np.int16)
    notas = []
    with torch.no_grad():
        for t, v in zip(trilhas, recortes):
            a0, a1 = int(t["frame"][0] / FPS * 16000), int((t["frame"][-1] + 1) / FPS * 16000)
            mf = python_speech_features.mfcc(audio[a0:a1], 16000, numcep=13, winlen=0.025, winstep=0.010)
            comp = min((mf.shape[0] - mf.shape[0] % 4) / 100, v.shape[0])
            A = torch.from_numpy(mf[: int(round(comp * 100))]).float().cuda()
            V = v[: int(round(comp * FPS))].float()
            todas = []
            for d in JANELAS:
                n_seg = int(math.ceil(comp / d))
                cheios = min(n_seg, int(comp // d))
                parte = []
                por_lote = max(1, 600 // (d * FPS))    # ~600 quadros por passada (~1,5 GB de VRAM)
                for i0 in range(0, cheios, por_lote):
                    i1 = min(cheios, i0 + por_lote)
                    parte.append(asd.nota(A[i0 * d * 100: i1 * d * 100].view(i1 - i0, d * 100, 13),
                                          V[i0 * d * FPS: i1 * d * FPS].view(i1 - i0, d * FPS, 112, 112)))
                if n_seg > cheios:
                    parte.append(asd.nota(A[cheios * d * 100:].unsqueeze(0), V[cheios * d * FPS:].unsqueeze(0)))
                todas.append(np.concatenate(parte).astype(np.float32))
            m = min(len(x) for x in todas)
            notas.append(np.mean([x[:m] for x in todas], 0))
    tempos["lrasd"] = time.perf_counter() - t0
    del recortes, asd
    gc.collect()
    torch.cuda.empty_cache()
    return {"W": W, "H": H, "n": n, "cortes": cortes, "trilhas": trilhas, "notas": notas, "tempos": tempos}


# ---------------------------------------------------------------------------
# Quem enquadrar em cada quadro
# ---------------------------------------------------------------------------

def alvos_por_quadro(an: dict, turno_minimo: float, suaviza_s: float = 1.0) -> list[tuple[float, float] | None]:
    """Para cada quadro (25/s), o centro (fração do frame) do rosto a enquadrar.

    Em cada plano: a cada quadro, quem tem a maior nota (média de ~1s) e
    positiva está falando; quando ninguém fala, fica em quem falou por último.
    Trocas mais curtas que `turno_minimo` são engolidas pelo turno vizinho
    (interjeição, "uhum", erro de um quadro) — como é feito com o clipe
    inteiro já analisado, a troca acontece no começo da fala, sem atraso.
    Plano sem ninguém falando enquadra o maior rosto; sem rosto, None (a
    câmera fica onde estava)."""
    from scipy.ndimage import uniform_filter1d

    W, H, n = an["W"], an["H"], an["n"]
    meia = max(1, int(suaviza_s * FPS / 2))
    minimo = int(turno_minimo * FPS)
    alvos: list[tuple[float, float] | None] = [None] * n
    limites = [0] + an["cortes"] + [n]
    for a, b in zip(limites, limites[1:]):
        daqui = [i for i, t in enumerate(an["trilhas"]) if a <= t["frame"][0] < b]
        if not daqui:
            continue
        # nota suavizada de cada trilha em cada quadro do plano (-inf onde não aparece)
        notas = np.full((len(daqui), b - a), -np.inf, dtype=np.float32)
        for j, i in enumerate(daqui):
            t, s = an["trilhas"][i], an["notas"][i]
            if len(s):
                s = uniform_filter1d(s, 2 * meia + 1, mode="nearest")
            f = t["frame"][: len(s)] - a
            notas[j, f] = s
        melhor = notas.argmax(0)
        fala = notas.max(0) > 0
        rotulo = np.where(fala, melhor, -1)
        # sem fala: segue quem falou por último (antes do 1º que fala: o 1º que fala)
        ultimo = next((r for r in rotulo if r >= 0), -1)
        if ultimo < 0:      # ninguém fala no plano: o maior rosto
            ultimo = int(np.argmax([np.mean(an["trilhas"][i]["s"]) for i in daqui]))
        for k in range(len(rotulo)):
            if rotulo[k] < 0:
                rotulo[k] = ultimo
            ultimo = rotulo[k]
        # engole turnos curtos no vizinho (repete até estabilizar)
        mudou = True
        while mudou:
            mudou = False
            corridas = []
            k = 0
            while k < len(rotulo):
                k1 = k
                while k1 < len(rotulo) and rotulo[k1] == rotulo[k]:
                    k1 += 1
                corridas.append((k, k1))
                k = k1
            if len(corridas) < 2:
                break
            for c, (k0, k1) in enumerate(corridas):
                if k1 - k0 < minimo:
                    vizinho = rotulo[corridas[c - 1][0]] if c > 0 else rotulo[corridas[c + 1][0]]
                    rotulo[k0:k1] = vizinho
                    mudou = True
                    break
        # posição: o rosto escolhido; se a trilha dele falhar ali, o rosto mais perto de onde estava
        pos = None
        for k in range(b - a):
            q = a + k
            t = an["trilhas"][daqui[rotulo[k]]]
            idx = q - t["frame"][0]
            if 0 <= idx < len(t["frame"]):
                pos = (float(t["x"][idx]) / W, float(t["y"][idx]) / H)
            else:
                presentes = [an["trilhas"][i] for i in daqui
                             if 0 <= q - an["trilhas"][i]["frame"][0] < len(an["trilhas"][i]["frame"])]
                if presentes and pos is not None:
                    perto = min(presentes, key=lambda t: abs(t["x"][q - t["frame"][0]] / W - pos[0]))
                    x = perto["x"][q - perto["frame"][0]] / W
                    if abs(x - pos[0]) <= 0.3:
                        pos = (float(x), float(perto["y"][q - perto["frame"][0]]) / H)
            alvos[q] = pos
    return alvos


def processar(video: Path, destino: Path, cfg: dict) -> dict:
    """Clipe 16:9 -> crop 9:16 seguindo quem fala. Devolve um resumo."""
    import crop_dinamico as cd

    t0 = time.perf_counter()
    an = analisar(video)
    cc, cs = cfg["camera"], cfg["saida"]
    largura, altura, fps, duracao = cd.resolucao_de(video)
    crop_h = altura
    crop_w = round((altura * 9 / 16) / 2) * 2
    if crop_w > largura:
        crop_w = largura - (largura % 2)
        crop_h = round((crop_w * 16 / 9) / 2) * 2

    alvos = alvos_por_quadro(an, cc["turno_minimo"])
    # a câmera decide a cada 2 quadros (0,08s) — o crop antigo amostrava a cada 0,1s
    amostras = list(range(0, an["n"], 2))
    tempos = [q / FPS for q in amostras]
    limites = [0.0] + [q / FPS for q in an["cortes"]] + [duracao]
    planos = [(a, b) for a, b in zip(limites, limites[1:]) if b > a]
    keys = cd.trajetoria_camera(tempos, [alvos[q] for q in amostras], planos, fps, cc["zona_morta"],
                                cc["tempo_fora"], cc["tempo_pan"], cc["segura_minimo"])
    t1 = time.perf_counter()
    cd.renderizar(video, destino, cd.gerar_comandos(keys, largura, altura, crop_w, crop_h),
                  crop_w, crop_h, cs["largura"], cs["altura"], nvenc=True)
    tempos_etapas = {k: round(v, 1) for k, v in an["tempos"].items()}
    tempos_etapas["render"] = round(time.perf_counter() - t1, 1)
    resumo = {"planos": len(planos), "rostos": len(an["trilhas"]), "keyframes": len(keys),
              "duracao": round(duracao, 1), "tempos_s": tempos_etapas,
              "total_s": round(time.perf_counter() - t0, 1)}
    log.info("   crop LR-ASD: %d planos, %d trilhas de rosto, %.0fs para %.0fs de vídeo %s",
             resumo["planos"], resumo["rostos"], resumo["total_s"], duracao, tempos_etapas)
    return resumo


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("video", type=Path)
    ap.add_argument("destino", type=Path)
    ap.add_argument("--config", type=Path, default=AQUI / "config_crop.yaml")
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    import yaml
    resumo = processar(a.video, a.destino, yaml.safe_load(a.config.read_text(encoding="utf-8")))
    print("RESUMO " + json.dumps(resumo, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
