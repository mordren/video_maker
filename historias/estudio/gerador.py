"""Gerador de Vídeo: junta uma playlist e um fundo visual num vídeo 16:9 longo, pronto para o YouTube (canais lo-fi).

Pastas: musicas (mp3 e wav), imagens (fundo do slideshow), videos (clipes para o loop; os vídeos da pasta de imagens
também valem) e saida (vídeo + capítulos). Cada uma pode ser escolhida na tela; sem escolha, valem as subpastas de
config.BASE com esses nomes.

Uma renderização por vez, numa thread própria; a interface só consulta o progresso.
1. Análise: cada faixa é decodificada uma vez para medir o loudness e a duração exata (fica em cache).
2. Áudio: ganho de cada faixa até -14 LUFS, crossfade entre faixas (acrossfade) e limitador -> audio.m4a.
   A faixa seguinte começa quando a anterior começa a sumir, então o vídeo dura a soma das faixas menos os
   crossfades, e os capítulos usam a mesma conta.
3. Fundo: no slideshow, o Pillow ajusta cada imagem a 16:9 e o FFmpeg faz o Ken Burns (zoompan) e o crossfade
   (xfade). Só UM ciclo de imagens é renderizado, com o fim emendando no começo; a passada final repete o ciclo até
   o fim do áudio. O ciclo sai em blocos de poucas imagens, cada um terminando no meio de uma imagem parada, e os
   blocos são colados sem reencodar: a memória não cresce com o número de imagens. No modo clipe, o vídeo
   escolhido entra direto em loop.
4. Passada final: chuva, grão, vinheta e o nome de cada faixa (drawtext com fade), em H.264 na GPU NVIDIA (NVENC)
   quando ela existe, senão na CPU (libx264). Junto sai o .txt de capítulos.
A prévia usa o mesmo caminho numa janela de 10 s (por padrão, a troca da 1ª para a 2ª faixa).
"""
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import quote

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

from . import config

# Resolução e fps vêm de config (aba Configurações); só mudam depois de reiniciar o servidor. Os demais parâmetros
# do gerador (alvo de loudness, prévia, CRF, bitrate, preset...) são config.GERADOR_*, lidos na hora do uso.
LARGURA, ALTURA, FPS = config.GERADOR_LARGURA, config.GERADOR_ALTURA, config.GERADOR_FPS
EXT_AUDIO = {".mp3", ".wav"}
EXT_IMAGEM = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
EXT_VIDEO = {".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi"}
IMAGENS_POR_BLOCO = 6
POSICOES = ("inf_esq", "inf_centro", "inf_dir", "sup_esq", "sup_dir", "centro")

PASTAS_PADRAO = {"musicas": config.GERADOR_MUSICAS, "imagens": config.GERADOR_IMAGENS,
                 "videos": config.GERADOR_VIDEOS, "saida": config.GERADOR_SAIDA}   # valem quando a config não escolhe outra
ROTULOS = {"musicas": "músicas", "imagens": "imagens", "videos": "vídeos", "saida": "saída"}
PASTAS_CLIPE = ("videos", "imagens")
TRABALHO = config.DADOS / "gerador"          # cache, miniaturas, prévia e temporários
ARQ_CONFIG = config.DADOS / "gerador.json"   # últimas configurações e a lista de faixas
ARQ_CACHE = TRABALHO / "cache.json"
ARQ_PREVIA = TRABALHO / "previa.mp4"
TRABALHO.mkdir(parents=True, exist_ok=True)

PADRAO = {
    "modo": "slideshow",          # slideshow | clipe
    "ordem": "nome",              # nome | aleatoria
    "semente": 1,
    "segundos_imagem": 20,
    "transicao_s": 2.0,
    "ken_burns": True,
    "ajuste": "cortar",           # cortar | preencher (imagem inteira sobre fundo desfocado)
    "clipe": "",
    "crossfade_s": 3.0,
    "normalizar": True,
    "texto": {"ligado": True, "posicao": "inf_esq", "fonte": "", "tamanho": 34, "cor": "#ffffff"},
    "vinheta": True,
    "grao": False,
    "chuva": False,
    "nome_saida": "lofi_mix",
    "previa_inicio": None,        # segundos; None = troca entre a 1ª e a 2ª faixa
    "pastas": dict.fromkeys(PASTAS_PADRAO, ""),   # caminho escolhido; "" = a pasta padrão ao lado do programa
}


class ErroGerador(Exception):
    pass


class Ocupado(ErroGerador):
    pass


class _Cancelado(Exception):
    pass


# ---------------------------------------------------------------- títulos, tempo e capítulos

_LIXO = re.compile(r"^[\s\-–—_.,:;|#~*+=)\]}]+")
_ABRE = re.compile(r"^[(\[{]\s*(?=\d)")
_PALAVRA = re.compile(r"^(?:faixa|track|trilha|m[uú]sica|song|cd|disco?|parte|part|pt)\s*(?=\d)", re.I)
_NUMERO = re.compile(r"^\d+")


def titulo_da_faixa(nome: str) -> str:
    """Nome do arquivo sem extensão, sem o número da faixa e o que vem grudado nele ("01 - ", "[02] ", "Track 3_")."""
    base = " ".join(Path(nome).stem.replace("_", " ").split())
    t = _LIXO.sub("", base)
    t = _ABRE.sub("", t)
    t = _PALAVRA.sub("", t)
    t = _NUMERO.sub("", t)
    t = _LIXO.sub("", t).strip()
    return t or base


def linha_do_tempo(duracoes: list[float], crossfade: float) -> tuple[float, list[float], float]:
    """Crossfade efetivo, início de cada faixa no vídeo e duração total.

    O crossfade não pode passar de metade de uma faixa do meio (os dois fades dela se encostariam) nem da duração
    da primeira e da última."""
    n = len(duracoes)
    if n < 2:
        return 0.0, [0.0] * n, float(sum(duracoes))
    limite = min(duracoes[0], duracoes[-1]) - 0.1
    if n > 2:
        limite = min(limite, *(d / 2 - 0.05 for d in duracoes[1:-1]))
    xf = round(max(0.0, min(float(crossfade), limite)), 3)
    inicios, t = [], 0.0
    for d in duracoes:
        inicios.append(t)
        t += d - xf
    return xf, inicios, float(sum(duracoes)) - xf * (n - 1)


def carimbo(s: float, longo: bool) -> str:
    s = int(s + 0.01)  # para baixo: o capítulo nunca começa depois da música
    h, m, x = s // 3600, s % 3600 // 60, s % 60
    return f"{h:02d}:{m:02d}:{x:02d}" if longo else f"{m:02d}:{x:02d}"


def texto_capitulos(titulos: list[str], inicios: list[float], total: float) -> str:
    longo = total >= 3600
    return "\n".join(f"{carimbo(t, longo)} {nome}" for t, nome in zip(inicios, titulos)) + "\n"


def _nome_seguro(nome: str) -> str:
    nome = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "", nome or "").strip(" .")
    return nome[:80] or PADRAO["nome_saida"]


def _cor(v) -> str:
    v = str(v or "").strip()
    return v.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", v) else "#ffffff"


def _num(v, padrao: float, minimo: float, maximo: float) -> float:
    try:
        return min(max(float(v), minimo), maximo)
    except (TypeError, ValueError):
        return padrao


def _valor_pasta(bruto, padrao: Path) -> str:
    """Caminho absoluto da pasta escolhida. Vazio quando não há escolha, o valor não serve (nem é texto, é um arquivo)
    ou é a própria pasta padrão. Caminho relativo conta a partir da pasta do programa."""
    if not isinstance(bruto, str):
        return ""
    texto = bruto.strip().strip('"').strip()  # o Explorer ("Copiar como caminho") põe aspas
    if not texto or "\x00" in texto:
        return ""
    try:
        p = Path(texto).expanduser()
        p = (p if p.is_absolute() else config.BASE / p).resolve()
        if p.is_file():
            return ""
    except (OSError, ValueError, RuntimeError):
        return ""
    return "" if p == padrao.resolve() else str(p)


def pasta(nome: str, cfg: dict | None = None) -> Path:
    """Pasta de trabalho: a escolhida na config (a passada ou, sem ela, a salva) ou, sem escolha, a padrão."""
    if nome not in PASTAS_PADRAO:
        raise ErroGerador("pasta desconhecida")
    if cfg is None:
        cfg = _carregar().get("config")
    bruto = (cfg or {}).get("pastas")
    escolhida = _valor_pasta(bruto.get(nome) if isinstance(bruto, dict) else None, PASTAS_PADRAO[nome])
    return Path(escolhida) if escolhida else PASTAS_PADRAO[nome]


def normalizar_config(cfg: dict | None) -> dict:
    """Mescla com o padrão e põe cada valor dentro do que o renderizador aceita."""
    c = {**PADRAO, **(cfg or {})}
    c["texto"] = {**PADRAO["texto"], **((cfg or {}).get("texto") or {})}
    bruto = (cfg or {}).get("pastas")
    bruto = bruto if isinstance(bruto, dict) else {}
    c["pastas"] = {k: _valor_pasta(bruto.get(k), p) for k, p in PASTAS_PADRAO.items()}
    c["modo"] = c["modo"] if c["modo"] in ("slideshow", "clipe") else "slideshow"
    c["ordem"] = c["ordem"] if c["ordem"] in ("nome", "aleatoria") else "nome"
    c["ajuste"] = c["ajuste"] if c["ajuste"] in ("cortar", "preencher") else "cortar"
    c["semente"] = int(_num(c["semente"], 1, 0, 2**31))
    c["segundos_imagem"] = _num(c["segundos_imagem"], 20, 3, 600)
    c["transicao_s"] = _num(c["transicao_s"], 2, 0.3, min(5.0, c["segundos_imagem"] / 2))
    c["crossfade_s"] = _num(c["crossfade_s"], 3, 0, 15)
    pasta, _, nome = str(c["clipe"] or "").replace("\\", "/").rpartition("/")
    c["clipe"] = f"{pasta}/{Path(nome).name}" if pasta in PASTAS_CLIPE else Path(nome).name
    for k in ("ken_burns", "normalizar", "vinheta", "grao", "chuva"):
        c[k] = bool(c[k])
    t = c["texto"]
    t["ligado"] = bool(t["ligado"])
    t["posicao"] = t["posicao"] if t["posicao"] in POSICOES else "inf_esq"
    t["tamanho"] = int(_num(t["tamanho"], 34, 12, 120))
    t["cor"] = _cor(t["cor"])
    t["fonte"] = _achar_fonte(str(t["fonte"] or "")) or fonte_padrao()
    c["nome_saida"] = _nome_seguro(c["nome_saida"])
    p = c["previa_inicio"]
    c["previa_inicio"] = None if p in (None, "") else _num(p, 0, 0, 10**6)
    return c


# ---------------------------------------------------------------- fontes do Windows

_fontes: list[dict] | None = None
_SIMBOLOS = re.compile(r"symbol|wingdings|webdings|marlett|mdl2|icons|emoji|holomdl|fluent", re.I)


def _pastas_fontes() -> list[Path]:
    if os.name == "nt":
        return [Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"]
    # Linux (servidor .133): as do usuário primeiro, depois a do repositório (Montserrat) e as do sistema
    return [Path.home() / ".local" / "share" / "fonts", Path.home() / ".fonts",
            config.CODIGO.parent / "assets" / "fonts", Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]


def fontes() -> list[dict]:
    global _fontes
    if _fontes is None:
        achadas: dict[str, str] = {}
        for pasta in _pastas_fontes():
            if not pasta.is_dir():
                continue
            for f in sorted(pasta.iterdir() if os.name == "nt" else pasta.rglob("*")):
                if f.suffix.lower() not in (".ttf", ".otf", ".ttc"):
                    continue
                try:
                    familia, estilo = ImageFont.truetype(str(f), 12).getname()
                except Exception:
                    continue
                nome = familia if estilo in ("Regular", "Normal", "Book", "") else f"{familia} {estilo}"
                if not _SIMBOLOS.search(nome):
                    achadas.setdefault(nome, str(f))
        _fontes = [{"nome": n, "arquivo": a} for n, a in sorted(achadas.items(), key=lambda x: x[0].lower())]
    return _fontes


def _achar_fonte(caminho: str) -> str:
    """O arquivo da fonte escolhida; uma escolha feita no Windows (C:\\Windows\\Fonts\\x.ttf) vale no servidor
    Linux se o mesmo arquivo estiver numa das pastas de fontes de lá."""
    if not caminho:
        return ""
    if Path(caminho).is_file():
        return caminho
    nome = PureWindowsPath(caminho).name
    for pasta in _pastas_fontes():
        if (pasta / nome).is_file():
            return str(pasta / nome)
    return ""


def fonte_padrao() -> str:
    for pasta in _pastas_fontes()[:3]:
        for nome in ("seguisb.ttf", "segoeui.ttf", "arial.ttf", "Montserrat-ExtraBold.ttf"):  # Segoe UI Semibold
            if (pasta / nome).is_file():
                return str(pasta / nome)
    return fontes()[0]["arquivo"] if fontes() else ""


# ---------------------------------------------------------------- arquivos e cache (duração e loudness)

_cache_lock = threading.Lock()
_cache: dict | None = None


def _entrada_cache(p: Path) -> dict:
    """Entrada do arquivo no cache; volta vazia se o arquivo mudou."""
    global _cache
    st = p.stat()
    chave = str(p.resolve()).lower()
    with _cache_lock:
        if _cache is None:
            try:
                _cache = json.loads(ARQ_CACHE.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _cache = {}
        e = _cache.get(chave)
        if not e or e.get("mtime") != st.st_mtime or e.get("tamanho") != st.st_size:
            e = _cache[chave] = {"mtime": st.st_mtime, "tamanho": st.st_size}
        return e


def _gravar_cache():
    with _cache_lock:
        if _cache is not None:
            tmp = ARQ_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
            tmp.replace(ARQ_CACHE)


def _sem_janela() -> dict:
    # ffmpeg em prioridade baixa e sem abrir console: o computador e a interface continuam respondendo.
    if os.name == "nt":
        return {"creationflags": subprocess.BELOW_NORMAL_PRIORITY_CLASS | subprocess.CREATE_NO_WINDOW}
    return {}


def _sondar(p: Path) -> dict:
    """Duração e se tem vídeo (ffprobe), com cache."""
    e = _entrada_cache(p)
    if "duracao" not in e:
        r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration:stream=codec_type",
                            "-of", "json", str(p)], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", **_sem_janela())
        try:
            j = json.loads(r.stdout)
            e["duracao"] = float(j["format"]["duration"])
            e["video"] = any(s.get("codec_type") == "video" for s in j.get("streams", []))
        except (ValueError, KeyError, TypeError):
            e["duracao"], e["video"] = 0.0, False
            e["ilegivel"] = True
    return e


def _listar(dono: Path, exts: set[str]) -> list[Path]:
    try:
        return sorted((f for f in dono.iterdir() if f.is_file() and f.suffix.lower() in exts),
                      key=lambda f: f.name.lower())
    except OSError:  # pasta que não existe ou sem permissão
        return []


def _criar_pasta(p: Path):
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ErroGerador(f"Não consegui criar a pasta {p}: {e.strerror or e}")


def _garantir(qual: str, cfg: dict | None = None) -> Path:
    """Pasta pronta para gravar. A de saída e as padrões são criadas se faltarem; uma pasta escolhida que sumiu
    dá erro (pastas de entrada não são criadas fora do programa)."""
    p = pasta(qual, cfg)
    if not p.is_dir():
        if qual != "saida" and p != PASTAS_PADRAO[qual]:
            raise ErroGerador(f"A pasta de {ROTULOS[qual]} {p} não existe.")
        _criar_pasta(p)
    return p


def info_faixa(p: Path) -> dict:
    e = _sondar(p)
    return {"nome": p.name, "titulo": titulo_da_faixa(p.name), "duracao": e.get("duracao", 0.0),
            "erro": "arquivo de áudio ilegível" if e.get("ilegivel") else None}


def _infos(arquivos: list[Path]) -> list[dict]:
    with ThreadPoolExecutor(max_workers=6) as ex:
        infos = list(ex.map(info_faixa, arquivos))
    _gravar_cache()
    return infos


def musicas_da_pasta(cfg: dict | None = None) -> list[dict]:
    return _infos(_listar(pasta("musicas", cfg), EXT_AUDIO))


def clipes(cfg: dict | None = None) -> list[dict]:
    achados, vistas = [], set()
    for q in PASTAS_CLIPE:
        d = pasta(q, cfg)
        if d in vistas:  # a mesma pasta escolhida para as duas: não repete os vídeos
            continue
        vistas.add(d)
        achados += [{"nome": f"{q}/{v.name}", "duracao": _sondar(v)["duracao"]} for v in _listar(d, EXT_VIDEO)]
    return achados


def caminho_clipe(valor: str, cfg: dict | None = None) -> Path:
    """"videos/x.mp4" ou "imagens/x.mp4"; só o nome procura primeiro na pasta de vídeos."""
    q, _, nome = valor.rpartition("/")
    nome = Path(nome).name
    if q in PASTAS_CLIPE:
        return pasta(q, cfg) / nome
    pastas = [pasta(p, cfg) for p in PASTAS_CLIPE]
    return next((d / nome for d in pastas if (d / nome).is_file()), pastas[0] / nome)


def receber(nome: str, arquivo, qual: str) -> dict:
    """Guarda um arquivo enviado (arrastado do Explorer) na pasta. Mesmo nome e mesmo tamanho = reaproveita."""
    exts = EXT_AUDIO if qual == "musicas" else EXT_VIDEO
    nome = Path(nome or "").name
    if Path(nome).suffix.lower() not in exts:
        raise ErroGerador(f"{nome or 'arquivo'}: formato não aceito (use {', '.join(sorted(exts))}).")
    destino = _garantir(qual) / nome
    tmp = destino.with_name(destino.name + ".parcial")
    with open(tmp, "wb") as f:
        shutil.copyfileobj(arquivo, f)
    if destino.exists() and destino.stat().st_size == tmp.stat().st_size:
        tmp.unlink()
    else:
        n = 2
        while destino.exists():
            destino = destino.with_name(f"{Path(nome).stem} ({n}){Path(nome).suffix}")
            n += 1
        tmp.replace(destino)
    if qual == "musicas":
        return info_faixa(destino)
    return {"nome": f"{qual}/{destino.name}", "duracao": _sondar(destino)["duracao"]}


def miniatura(nome: str) -> Path:
    origem = pasta("imagens") / Path(nome).name
    if not origem.is_file():
        raise ErroGerador("imagem não encontrada")
    st = origem.stat()
    dono = hashlib.md5(str(origem).lower().encode("utf-8")).hexdigest()[:6]  # o mesmo nome em pastas diferentes
    destino = TRABALHO / "miniaturas" / f"{origem.stem}_{dono}_{int(st.st_mtime)}_{st.st_size}.jpg"
    if not destino.exists():
        destino.parent.mkdir(exist_ok=True)
        with Image.open(origem) as im:
            ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (256, 144), Image.LANCZOS).save(destino, quality=85)
    return destino


def url_saida(nome: str) -> str:
    """Endereço de um arquivo da pasta de saída (a pasta pode estar em qualquer lugar do disco)."""
    return f"/api/gerador/saida/{quote(nome)}"


def saidas(limite: int = 20, cfg: dict | None = None) -> list[dict]:
    try:
        vids = sorted(pasta("saida", cfg).glob("*.mp4"), key=lambda f: f.stat().st_mtime, reverse=True)[:limite]
    except OSError:
        return []
    return [{"nome": v.name, "tamanho": v.stat().st_size,
             "data": datetime.fromtimestamp(v.stat().st_mtime).isoformat(timespec="minutes"),
             "url": url_saida(v.name),
             "capitulos": url_saida(v.with_suffix(".txt").name) if v.with_suffix(".txt").exists() else None}
            for v in vids]


def abrir_pasta(nome: str):
    p = _garantir(nome)
    if os.name != "nt":
        raise ErroGerador(f"Abra a pasta {p} pelo gerenciador de arquivos.")
    os.startfile(p)  # noqa: S606 (servidor local: abre o Explorer neste computador)


# ---------------------------------------------------------------- seletor de pasta (janela do Windows)

_seletor = threading.Lock()
_SCRIPT_TK = (  # o diretório inicial e o título chegam por argv: nada de texto do usuário dentro do código
    "import sys, tkinter\n"
    "from tkinter import filedialog\n"
    "raiz = tkinter.Tk()\n"
    "raiz.withdraw()\n"
    "raiz.attributes('-topmost', True)\n"
    "escolhida = filedialog.askdirectory(parent=raiz, initialdir=sys.argv[1], title=sys.argv[2], mustexist=False)\n"
    "raiz.destroy()\n"
    "sys.stdout.write(escolhida or '')\n"
)
_SCRIPT_PS = "; ".join([  # plano B: a janela do próprio Windows; dados pelo ambiente (GERADOR_INICIAL, GERADOR_TITULO)
    "Add-Type -AssemblyName System.Windows.Forms",
    "[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)",
    "$dono = New-Object System.Windows.Forms.Form",
    "$dono.TopMost = $true",  # dono sempre por cima: a janela de escolha vem para a frente
    "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog",
    "$dlg.Description = $env:GERADOR_TITULO",
    "$dlg.SelectedPath = $env:GERADOR_INICIAL",
    "if ($dlg.ShowDialog($dono) -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dlg.SelectedPath) }",
    "$dono.Dispose()",
])


def _existente(caminho: str) -> str:
    """A pasta mais próxima de caminho que existe: o seletor não abre num lugar que não há."""
    d = Path(caminho) if caminho else Path.home()
    while not d.is_dir() and d != d.parent:
        d = d.parent
    return str(d if d.is_dir() else Path.home())


def escolher_pasta(inicial: str, titulo: str = "Escolha a pasta") -> str | None:
    """Abre a janela de escolha de pasta neste computador (o servidor é local) e devolve o caminho, ou None se o
    usuário cancelou. Roda em outro processo: o tkinter quer a thread principal e o servidor tem várias."""
    if not _seletor.acquire(blocking=False):
        raise Ocupado("Já há uma janela de escolha de pasta aberta.")
    try:
        inicial = _existente(inicial)
        tentativas = [([sys.executable, "-X", "utf8", "-c", _SCRIPT_TK, inicial, titulo], {})]
        if os.name == "nt":
            tentativas.append((["powershell", "-NoProfile", "-STA", "-Command", _SCRIPT_PS],
                               {"GERADOR_INICIAL": inicial, "GERADOR_TITULO": titulo}))
        sem_console = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        erro = ""
        for cmd, ambiente in tentativas:
            try:
                r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=600, env={**os.environ, **ambiente}, **sem_console)
            except subprocess.TimeoutExpired:
                raise ErroGerador("A janela de escolha de pasta ficou aberta por tempo demais.")
            except OSError as e:
                erro = str(e)
                continue
            if r.returncode == 0:  # saída vazia = cancelou
                escolhida = r.stdout.strip().lstrip("﻿")
                return str(Path(escolhida)) if escolhida else None  # barras no estilo do sistema
            erro = (r.stderr.strip().splitlines() or [""])[-1]
        raise ErroGerador(f"Não consegui abrir a janela de escolha de pasta ({erro or 'sem detalhes'}). "
                          "Digite o caminho no campo.")
    finally:
        _seletor.release()


def escolher(qual: str) -> str | None:
    """Escolha de uma das pastas do gerador, começando na que vale hoje."""
    return escolher_pasta(str(pasta(qual)), f"Escolha a pasta de {ROTULOS[qual]}")


# ---------------------------------------------------------------- configurações salvas

def _carregar() -> dict:
    try:
        return json.loads(ARQ_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def salvar(cfg: dict, faixas: list[str]):
    """Grava a config e a lista de faixas. A lista vale para uma pasta de músicas ("pasta_faixas"): se esta save
    troca a pasta e a lista recebida não é toda dela, a lista é descartada e estado() lê a pasta nova inteira."""
    cfg = normalizar_config(cfg)
    antes = _carregar()
    nomes = [Path(n).name for n in faixas if n]
    atual = pasta("musicas", cfg)
    velha = Path(antes.get("pasta_faixas") or pasta("musicas", antes.get("config")))
    dados = {"config": cfg, "pasta_faixas": str(atual)}
    if velha == atual or (nomes and all((atual / n).is_file() for n in nomes)):
        dados["faixas"] = nomes
    tmp = ARQ_CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(ARQ_CONFIG)


def estado() -> dict:
    salvo = _carregar()
    cfg = normalizar_config(salvo.get("config"))
    if "semente" not in (salvo.get("config") or {}):
        cfg["semente"] = random.randint(1, 10**6)
    na_pasta = musicas_da_pasta(cfg)
    por_nome = {f["nome"]: f for f in na_pasta}
    da_pasta = Path(salvo.get("pasta_faixas") or pasta("musicas", salvo.get("config"))) == pasta("musicas", cfg)
    faixas = [por_nome[n] for n in salvo["faixas"] if n in por_nome] if "faixas" in salvo and da_pasta else na_pasta
    lista_clipes = clipes(cfg)
    _gravar_cache()
    with _lock:
        trab = _atual.para_api() if _atual else None
    pastas = {k: pasta(k, cfg) for k in PASTAS_PADRAO}
    if trab and trab["tipo"] == "video" and trab["resultado"] and Path(trab["resultado"]["arquivo"]).parent != pastas["saida"]:
        trab = None  # o último vídeo ficou noutra pasta de saída: o painel apontaria para um arquivo que a rota não acha
    return {"pastas": {k: str(v) for k, v in pastas.items()}, "pastas_existem": {k: v.is_dir() for k, v in pastas.items()},
            "config": cfg, "faixas": faixas, "na_pasta": na_pasta,
            "imagens": [f.name for f in _listar(pastas["imagens"], EXT_IMAGEM)], "clipes": lista_clipes,
            "codificador": codificador(), "trabalho": trab, "saidas": saidas(cfg=cfg),
            "alvo_lufs": config.GERADOR_ALVO_LUFS, "resolucao": f"{LARGURA}x{ALTURA}", "fps": FPS}


# ---------------------------------------------------------------- codificador (GPU NVIDIA ou CPU)

_codificador: dict | None = None


def codificador() -> dict:
    """Testa uma vez se o NVENC funciona de verdade (o FFmpeg pode ter o codificador sem haver placa NVIDIA)."""
    global _codificador
    if not config.GERADOR_USAR_GPU:  # desligado na aba Configurações
        return {"gpu": False, "nome": "libx264", "rotulo": "CPU (libx264)", "motivo": "GPU desligada em Configurações"}
    if _codificador is None:
        try:
            r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                "color=c=black:s=256x144:d=0.2", "-c:v", "h264_nvenc", "-f", "null", "-"],
                               capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                               **_sem_janela())
            ok, motivo = r.returncode == 0, (r.stderr.strip().splitlines() or [""])[0]
        except (OSError, subprocess.TimeoutExpired) as e:
            ok, motivo = False, str(e)
        _codificador = {"gpu": ok, "nome": "h264_nvenc" if ok else "libx264",
                        "rotulo": "GPU NVIDIA (NVENC)" if ok else "CPU (libx264)",
                        "motivo": "" if ok else f"NVENC indisponível: {motivo[:160]}"}
    return _codificador


def _desligar_gpu(motivo: str):
    global _codificador
    _codificador = {"gpu": False, "nome": "libx264", "rotulo": "CPU (libx264)", "motivo": motivo[:200]}


COR_HD = "scale=out_color_matrix=bt709:out_range=tv"   # JPEG/PNG chegam em faixa cheia: converte para o padrão HD
MARCA_HD = "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709"  # o x264 lê a cor do quadro
_TAGS_COR = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


def _args_video(final: bool) -> list[str]:
    """Na CPU, veryfast: com grão ou chuva o tempo vai quase todo para o codificador, e o veryfast leva metade do
    tempo do fast. O YouTube reencoda tudo de qualquer jeito."""
    gpu = codificador()["gpu"]
    maxrate, buffer = f"{config.GERADOR_MAXRATE_MBPS}M", f"{2 * config.GERADOR_MAXRATE_MBPS}M"
    if final and gpu:
        a = ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(config.GERADOR_CQ_GPU),
             "-b:v", "0", "-maxrate", maxrate, "-bufsize", buffer, "-profile:v", "high", "-bf", "2"]
    elif final:
        a = ["-c:v", "libx264", "-preset", config.GERADOR_PRESET, "-crf", str(config.GERADOR_CRF), "-maxrate", maxrate,
             "-bufsize", buffer, "-profile:v", "high"]
    elif gpu:  # intermediário: qualidade alta, vai ser reencodado na passada final
        a = ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "constqp", "-qp", "17"]
    else:
        a = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]
    return a + ["-g", "60", "-pix_fmt", "yuv420p", *_TAGS_COR]


# ---------------------------------------------------------------- texturas (vinheta e chuva)

def _vinheta(destino: Path):
    """Preto com alfa radial: sobrepor uma máscara pronta custa 1/6 do filtro vignette do FFmpeg."""
    g = Image.radial_gradient("L").resize((LARGURA, ALTURA), Image.BICUBIC)  # 0 no centro, 128 no meio das bordas
    alfa = g.point(lambda v: int(255 * 0.78 * min(1.0, max(0.0, (v / 128 - 0.55) / 0.8)) ** 1.6))
    img = Image.new("RGBA", (LARGURA, ALTURA), (0, 0, 0, 0))
    img.putalpha(alfa)
    img.save(destino)


CHUVA = [  # (arquivo, gotas, comprimento, alfa, largura, desfoque, velocidade px/s)
    ("chuva_longe.png", 420, (14, 28), (35, 80), 1, 0.6, 620),
    ("chuva_perto.png", 240, (35, 70), (60, 125), 2, 0.0, 1150),
]
ANGULO_CHUVA = 0.17  # rad (~10°), inclinada para a direita


def _camada_chuva(destino: Path, gotas, comp, alfa, largura, desfoque, semente):
    """Textura que emenda nas quatro bordas, repetida 2x2: a passada final só desliza a imagem."""
    import math
    rnd = random.Random(semente)
    tile = Image.new("RGBA", (LARGURA, ALTURA), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    s, c = math.sin(ANGULO_CHUVA), math.cos(ANGULO_CHUVA)
    for _ in range(gotas):
        x, y, n = rnd.uniform(0, LARGURA), rnd.uniform(0, ALTURA), rnd.uniform(*comp)
        cor = (215, 225, 255, rnd.randint(*alfa))
        for ox in (-LARGURA, 0, LARGURA):
            for oy in (-ALTURA, 0, ALTURA):
                d.line([(x + ox, y + oy), (x + ox + n * s, y + oy + n * c)], fill=cor, width=largura)
    if desfoque:
        tile = tile.filter(ImageFilter.GaussianBlur(desfoque))
    grade = Image.new("RGBA", (2 * LARGURA, 2 * ALTURA))
    for ox in (0, LARGURA):
        for oy in (0, ALTURA):
            grade.paste(tile, (ox, oy))
    grade.save(destino)


def _texturas_chuva() -> list[tuple[Path, float, float]]:
    import math
    saida = []
    for i, (arq, gotas, comp, alfa, larg, desf, vel) in enumerate(CHUVA):
        p = TRABALHO / arq
        if not p.exists():
            _camada_chuva(p, gotas, comp, alfa, larg, desf, 7 + i)
        saida.append((p, vel * math.sin(ANGULO_CHUVA), vel * math.cos(ANGULO_CHUVA)))
    return saida


# ---------------------------------------------------------------- imagens do slideshow

KEN_BURNS = [  # (zoom, x, y); {p} vai de 0 a 1 ao longo da imagem
    ("1+0.10*{p}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),        # aproxima
    ("1.08", "(iw-iw/zoom)*{p}", "(ih-ih/zoom)/2"),            # desliza para a direita
    ("1.10-0.10*{p}", "(iw-iw/zoom)/2", "(ih-ih/zoom)/2"),     # afasta
    ("1.08", "(iw-iw/zoom)*(1-{p})", "(ih-ih/zoom)/2"),        # desliza para a esquerda
]


def _preparar_imagem(origem: Path, destino: Path, w: int, h: int, ajuste: str):
    try:
        with Image.open(origem) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P", "PA"):
                im = im.convert("RGBA")
                fundo = Image.new("RGB", im.size, (0, 0, 0))
                fundo.paste(im, mask=im.getchannel("A"))
                im = fundo
            else:
                im = im.convert("RGB")
            if ajuste == "preencher":
                fundo = ImageOps.fit(im, (w // 8, h // 8), Image.BILINEAR).filter(ImageFilter.GaussianBlur(5))
                fundo = ImageEnhance.Brightness(fundo.resize((w, h), Image.BICUBIC)).enhance(0.55)
                frente = ImageOps.contain(im, (w, h), Image.LANCZOS)
                fundo.paste(frente, ((w - frente.width) // 2, (h - frente.height) // 2))
                im = fundo
            else:
                im = ImageOps.fit(im, (w, h), Image.LANCZOS)
            im.save(destino, "JPEG", quality=92)
    except OSError as e:
        raise ErroGerador(f"Não consegui abrir a imagem {origem.name}: {e}")


class _Slides:
    """Cronologia do slideshow em quadros. A imagem k entra no quadro k*T (crossfade de X quadros) e fica até
    (k+1)*T+X. O ciclo de K imagens começa no quadro X (imagem 0 parada) e termina em K*T+X, onde a imagem K
    (= imagem 0, mesmo Ken Burns) está exatamente no mesmo ponto: repetir o ciclo não dá salto."""

    def __init__(self, imagens: list[Path], cfg: dict):
        self.imagens = imagens
        self.k = len(imagens)
        self.t = round(cfg["segundos_imagem"] * FPS)
        self.x = max(1, min(round(cfg["transicao_s"] * FPS), self.t // 2))
        self.ken_burns = cfg["ken_burns"]
        self.ciclo = self.k * self.t

    def blocos(self, quadros: int) -> list[tuple[int, int]]:
        """Divide [X, X+quadros) em blocos que terminam no meio de uma imagem parada."""
        ini, fim = self.x, self.x + quadros
        limites, k = [ini], IMAGENS_POR_BLOCO
        while True:
            meio = k * self.t + self.x + (self.t - self.x) // 2
            if meio >= fim - FPS:
                break
            limites.append(meio)
            k += IMAGENS_POR_BLOCO
        limites.append(fim)
        return list(zip(limites, limites[1:]))

    def filtro(self, ca: int, cb: int) -> tuple[list[str], str]:
        """Entradas e filtro de um trecho [ca, cb) da cronologia (quadros)."""
        ka, kb = (ca - self.x) // self.t, (cb - 1) // self.t
        n = self.t + self.x
        entradas, partes = [], []
        for j, k in enumerate(range(ka, kb + 1)):
            quadros = n if k < kb else min(n, max(cb - k * self.t, self.x + 1))
            entradas += ["-framerate", str(FPS), "-i", self.imagens[k % self.k].name]
            if self.ken_burns:
                z, x, y = (e.format(p=f"on/{n - 1}") for e in KEN_BURNS[(k % self.k) % len(KEN_BURNS)])
                partes.append(f"[{j}:v]zoompan=z='{z}':x='{x}':y='{y}':d={quadros}:s={LARGURA}x{ALTURA}:fps={FPS},"
                              "setsar=1" + f"[s{j}]")
            else:
                partes.append(f"[{j}:v]loop=loop={quadros - 1}:size=1:start=0,setpts=N/{FPS}/TB,setsar=1[s{j}]")
        ultimo = "s0"
        for j in range(1, kb - ka + 1):
            partes.append(f"[{ultimo}][s{j}]xfade=transition=fade:duration={self.x / FPS:.6f}:"
                          f"offset={j * self.t / FPS:.6f}[x{j}]")
            ultimo = f"x{j}"
        ini = ca - ka * self.t
        partes.append(f"[{ultimo}]trim=start_frame={ini}:end_frame={ini + cb - ca},setpts=PTS-STARTPTS,{COR_HD},"
                     f"format=yuv420p,{MARCA_HD}[v]")
        return entradas, ";\n".join(partes)



# ---------------------------------------------------------------- trabalho (renderização em segundo plano)

# Pesos do progresso: segundos de processamento por segundo de vídeo (medidos num Snapdragon X, FFmpeg x64 na CPU).
# Grão e chuva quase não custam no filtro, mas o ruído leva o codificador ao teto de bitrate: aí ele demora o dobro.
PESO_ANALISE = 0.012
PESO_AUDIO = 0.01
PESO_IMAGEM = 0.5             # por imagem preparada (Pillow)
PESO_KEN_BURNS = 0.12
PESO_ESTATICO = 0.03
PESO_FINAL_LIMPO = 0.09       # texto e vinheta incluídos
PESO_FINAL_RUIDO = 0.20       # com grão ou chuva
PESO_GPU = 0.4                # NVENC: fração do tempo da CPU nas etapas que codificam vídeo

_lock = threading.Lock()
_atual: "Trabalho | None" = None


class Trabalho:
    def __init__(self, tipo: str):
        self.id = uuid.uuid4().hex[:8]
        self.tipo = tipo  # previa | video
        self.pasta = TRABALHO / f"temp_{tipo}"
        self.estado = "rodando"
        self.etapa, self.n_etapa, self.total_etapas = "Preparando", 0, 0
        self.inicio, self.fim = time.time(), None
        self.erro, self.avisos, self.resultado = None, [], None
        self.cancelado = False
        self.procs: set[subprocess.Popen] = set()
        self._nomes: list[str] = []
        self._pesos: list[float] = []
        self._feito = self._frac = 0.0
        self._n = 0

    # Progresso: cada etapa tem um peso (segundos estimados); a fração geral pondera as etapas.
    def planejar(self, etapas: list[tuple[str, float]]):
        self._nomes = [n for n, _ in etapas]
        self._pesos = [max(p, 0.2) for _, p in etapas]
        self.total_etapas = len(etapas)

    def etapa_(self, nome: str):
        self.checar()
        if self.n_etapa:
            self._feito += self._pesos[self.n_etapa - 1]
        self.n_etapa = self._nomes.index(nome) + 1
        self.etapa, self._frac = nome, 0.0

    def avancar(self, frac: float):
        self._frac = min(max(frac, self._frac), 1.0)

    def progresso(self) -> float:
        if self.estado == "pronto":
            return 1.0
        total = sum(self._pesos) or 1.0
        atual = self._pesos[self.n_etapa - 1] * self._frac if self.n_etapa else 0.0
        return min((self._feito + atual) / total, 0.999)

    def checar(self):
        if self.cancelado:
            raise _Cancelado()

    def para_api(self) -> dict:
        p = self.progresso()
        decorrido = (self.fim or time.time()) - self.inicio
        restante = decorrido * (1 - p) / p if self.estado == "rodando" and p >= 0.02 and decorrido >= 3 else None
        return {"id": self.id, "tipo": self.tipo, "estado": self.estado, "etapa": self.etapa,
                "n_etapa": self.n_etapa, "total_etapas": self.total_etapas, "progresso": round(p, 4),
                "decorrido": round(decorrido, 1), "restante": round(restante) if restante is not None else None,
                "erro": self.erro, "avisos": self.avisos, "resultado": self.resultado}

    def ffmpeg(self, args: list[str], rotulo: str, duracao: float | None = None, cwd: Path | None = None,
               nivel: str = "error", parte: tuple[float, float] = (0.0, 1.0)) -> tuple[str, float]:
        """Roda o FFmpeg lendo o progresso (-progress pipe:1). Devolve o log e o último tempo processado."""
        self.checar()
        self._n += 1
        log = self.pasta / f"ffmpeg_{self._n:03d}_{threading.get_ident()}.log"
        cmd = [config.FFMPEG, "-hide_banner", "-nostats", "-loglevel", nivel, "-y", "-progress", "pipe:1", *args]
        tempo = 0.0
        with open(log, "w", encoding="utf-8", errors="replace") as err:
            try:
                proc = subprocess.Popen(cmd, cwd=cwd or self.pasta, stdout=subprocess.PIPE, stderr=err, text=True,
                                        encoding="utf-8", errors="replace", **_sem_janela())
            except FileNotFoundError:
                raise ErroGerador("FFmpeg não encontrado. Instale o FFmpeg e deixe-o no PATH.")
            self.procs.add(proc)
            try:
                for linha in proc.stdout:
                    if self.cancelado:
                        proc.kill()
                        break
                    if linha.startswith("out_time_us="):
                        try:
                            tempo = int(linha.split("=", 1)[1]) / 1e6
                        except ValueError:
                            continue
                        if duracao:
                            self.avancar(parte[0] + (parte[1] - parte[0]) * min(tempo / duracao, 1.0))
                proc.wait()
            finally:
                self.procs.discard(proc)
        self.checar()
        texto = log.read_text(encoding="utf-8", errors="replace")
        if proc.returncode != 0:
            raise ErroGerador(_traduzir_erro(texto, rotulo, self.pasta))
        return texto, tempo

    def ffmpeg_video(self, montar, rotulo: str, duracao: float | None, final: bool, **kw):
        """Como ffmpeg(), mas se o NVENC falhar, desliga a GPU e repete na CPU."""
        gpu = codificador()["gpu"]
        try:
            return self.ffmpeg(montar(_args_video(final)), rotulo, duracao, **kw)
        except ErroGerador as e:
            if not gpu or not re.search(r"nvenc|cuda", str(e), re.I):
                raise
            _desligar_gpu(f"NVENC falhou durante a renderização: {str(e)[:120]}")
            self.avisos.append("A GPU NVIDIA falhou no meio; o resto foi feito na CPU.")
            return self.ffmpeg(montar(_args_video(final)), rotulo, duracao, **kw)


def _traduzir_erro(log: str, rotulo: str, pasta: Path) -> str:
    if re.search(r"No space left|not enough space|Disk full|disk is full", log, re.I):
        return (f"O disco encheu durante {rotulo} (sobraram {_gb(shutil.disk_usage(pasta).free)}). "
                "Libere espaço e tente de novo.")
    linhas = [l.strip() for l in log.strip().splitlines() if l.strip()][-6:]
    return f"FFmpeg falhou em {rotulo}: " + " | ".join(linhas)[-700:]


def _gb(b: float) -> str:
    return f"{b / 1e9:.1f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def _checar_espaco(itens: list[tuple[Path, float]]):
    """Soma o que vai para cada disco (temporários em dados/ e vídeo em saida/ podem estar em discos diferentes)."""
    por_disco: dict[str, list] = {}
    for pasta, n in itens:
        disco = os.path.splitdrive(str(pasta.resolve()))[0] or str(pasta)
        por_disco.setdefault(disco, [0.0, pasta])[0] += n
    for disco, (n, pasta) in por_disco.items():
        livre = shutil.disk_usage(pasta).free
        if livre < n:
            raise ErroGerador(f"Falta espaço no disco {disco}. Este vídeo precisa de cerca de {_gb(n)} "
                              f"e há {_gb(livre)} livres. Libere espaço ou faça um vídeo mais curto.")


def _validar(cfg: dict, nomes: list[str], tipo: str) -> dict:
    """Erros que dá para ver antes de começar: lista vazia, arquivo sumido, pasta sem imagens, falta de disco."""
    if not (shutil.which(config.FFMPEG) or Path(config.FFMPEG).exists()):
        raise ErroGerador("FFmpeg não encontrado. Instale o FFmpeg e deixe-o no PATH.")
    pm = pasta("musicas", cfg)
    if not pm.is_dir():
        raise ErroGerador(f"A pasta de músicas {pm} não existe. Escolha outra pasta na tela.")
    if not nomes:
        raise ErroGerador("A lista de músicas está vazia. Arraste arquivos .mp3 ou .wav para a lista "
                          "ou clique em Carregar da pasta.")
    arquivos = []
    for n in nomes:
        p = pm / Path(n).name
        if not p.is_file():
            raise ErroGerador(f'A música "{n}" não está mais na pasta de músicas ({pm}). Tire-a da lista ou '
                              "devolva o arquivo.")
        arquivos.append(p)
    infos = _infos(arquivos)
    ruins = [i["nome"] for i in infos if i["erro"] or i["duracao"] < 1]
    if ruins:
        raise ErroGerador(f"Não consegui ler estas músicas: {', '.join(ruins)}.")
    imagens, clipe = [], None
    if cfg["modo"] == "slideshow":
        pi = pasta("imagens", cfg)
        if not pi.is_dir():
            raise ErroGerador(f"A pasta de imagens {pi} não existe. Escolha outra pasta na tela ou use o modo "
                              "clipe em loop.")
        imagens = _listar(pi, EXT_IMAGEM)
        if not imagens:
            raise ErroGerador(f"Nenhuma imagem na pasta de imagens ({pi}). Coloque arquivos .jpg, .png "
                              "ou .webp lá e clique em Atualizar, ou use o modo clipe em loop.")
    else:
        if not cfg["clipe"]:
            raise ErroGerador("Escolha o clipe que vai ficar em loop (os vídeos das pastas de vídeos e de imagens "
                              "aparecem na lista).")
        clipe = caminho_clipe(cfg["clipe"], cfg)
        if not clipe.is_file():
            q = cfg["clipe"].partition("/")[0]
            if q in PASTAS_CLIPE and not pasta(q, cfg).is_dir():
                raise ErroGerador(f"A pasta de {ROTULOS[q]} {pasta(q, cfg)} não existe. Escolha outra pasta na tela.")
            raise ErroGerador(f'O clipe "{cfg["clipe"]}" não foi encontrado nas pastas de vídeos e de imagens.')
        e = _sondar(clipe)
        if not e.get("video") or e["duracao"] <= 0:
            raise ErroGerador(f'"{clipe.name}" não parece um vídeo válido.')
    _, _, total = linha_do_tempo([i["duracao"] for i in infos], cfg["crossfade_s"])
    saida = None
    if tipo == "video":
        saida = _garantir("saida", cfg)  # criada na hora se não existir
        base = min(len(imagens) * cfg["segundos_imagem"], total) if imagens else 0
        # o teto de bitrate da saída (6 Mbps + áudio) garante o pior caso; o fundo intermediário fica perto de 8 Mbps
        _checar_espaco([(TRABALHO, total * 0.2e6 / 8 + base * 8e6 / 8 + 100e6),
                        (saida, total * 6.3e6 / 8 * 1.03 + 50e6)])
    else:
        _checar_espaco([(TRABALHO, 150e6)])
    return {"arquivos": arquivos, "imagens": imagens, "clipe": clipe, "total": total, "saida": saida}


def iniciar(tipo: str, cfg: dict, faixas: list[str]) -> dict:
    global _atual
    salvar(cfg, faixas)
    cfg = normalizar_config(cfg)
    with _lock:
        if _atual and _atual.estado == "rodando":
            raise Ocupado("Já há uma renderização em andamento. Espere terminar ou cancele.")
        plano = _validar(cfg, faixas, tipo)
        _atual = Trabalho(tipo)
        threading.Thread(target=_executar, args=(_atual, cfg, plano), daemon=True, name=f"gerador-{tipo}").start()
        return _atual.para_api()


def progresso() -> dict | None:
    with _lock:
        return _atual.para_api() if _atual else None


def cancelar():
    with _lock:
        if not _atual or _atual.estado != "rodando":
            raise ErroGerador("Não há renderização em andamento.")
        _atual.cancelado = True
        for p in list(_atual.procs):
            try:
                p.kill()
            except OSError:
                pass


def _executar(job: Trabalho, cfg: dict, plano: dict):
    parcial = None
    try:
        shutil.rmtree(job.pasta, ignore_errors=True)
        job.pasta.mkdir(parents=True)
        arquivos = plano["arquivos"]
        slideshow = cfg["modo"] == "slideshow"
        previa = job.tipo == "previa"
        pendentes = [p for p in arquivos if not _entrada_cache(p).get("exata")]
        janela = min(config.GERADOR_DURACAO_PREVIA, plano["total"]) if previa else plano["total"]
        nome_final = "Renderizando a prévia" if previa else "Renderizando o vídeo"
        gpu = PESO_GPU if codificador()["gpu"] else 1.0
        etapas = []
        if pendentes:
            etapas.append(("Analisando as músicas", sum(_sondar(p)["duracao"] for p in pendentes) * PESO_ANALISE))
        etapas.append(("Mixando o áudio", janela * PESO_AUDIO + 1))
        if slideshow:
            base = janela if previa else min(len(plano["imagens"]) * cfg["segundos_imagem"], plano["total"])
            n_img = min(len(plano["imagens"]), int(base / cfg["segundos_imagem"]) + 2)
            etapas.append(("Preparando as imagens", n_img * PESO_IMAGEM * (1 if cfg["ken_burns"] else 0.4)))
            etapas.append(("Montando o fundo", base * (PESO_KEN_BURNS if cfg["ken_burns"] else PESO_ESTATICO)
                           * gpu + 1))
        fator = PESO_FINAL_RUIDO + (0.012 if cfg["grao"] and cfg["chuva"] else 0) if cfg["grao"] or cfg["chuva"]             else PESO_FINAL_LIMPO
        etapas.append((nome_final, janela * fator * gpu + 1))
        job.planejar(etapas)

        # 1. análise (loudness e duração exata de cada faixa, com cache)
        if pendentes:
            job.etapa_("Analisando as músicas")
            _analisar(job, pendentes)
        infos = [_entrada_cache(p) for p in arquivos]
        duracoes = [e["duracao"] for e in infos]
        titulos = [titulo_da_faixa(p.name) for p in arquivos]
        xf, inicios, total = linha_do_tempo(duracoes, cfg["crossfade_s"])
        if len(arquivos) > 1 and xf < cfg["crossfade_s"] - 0.01:
            job.avisos.append(f"Crossfade reduzido para {xf:.1f} s: há faixa curta demais para {cfg['crossfade_s']:g} s.")
        ganhos = [_ganho(e) if cfg["normalizar"] else 0.0 for e in infos]
        if previa:
            dur = min(config.GERADOR_DURACAO_PREVIA, total)
            w0 = cfg["previa_inicio"] if cfg["previa_inicio"] is not None else \
                (max(0.0, inicios[1] - 4.0) if len(arquivos) > 1 else 0.0)
            w0 = min(w0, max(0.0, total - dur))
        else:
            w0, dur = 0.0, total

        # 2. áudio
        job.etapa_("Mixando o áudio")
        _mixar(job, arquivos, duracoes, ganhos, inicios, xf, w0, dur, cfg["normalizar"])

        # 3. fundo
        if slideshow:
            fundo, repetir = _fundo_slides(job, cfg, plano["imagens"], total, w0, dur)
            ss = 0.0
        else:
            fundo, repetir = plano["clipe"], True
            ss = w0 % max(_sondar(fundo)["duracao"], 0.04)

        # 4. passada final
        fins = inicios[1:] + [total]
        letreiros = [(titulos[i], inicios[i] - w0, fins[i] - w0) for i in range(len(arquivos))
                     if inicios[i] < w0 + dur and fins[i] > w0]
        if previa:
            destino = job.pasta / "previa.mp4"
        else:
            destino = plano["saida"] / f"{cfg['nome_saida']}_{datetime.now():%Y-%m-%d_%H-%M}.mp4"
            if destino.exists():
                destino = destino.with_name(f"{cfg['nome_saida']}_{datetime.now():%Y-%m-%d_%H-%M-%S}.mp4")
        parcial = destino.with_name(destino.stem + ".parcial.mp4")
        job.etapa_(nome_final)
        _passada_final(job, cfg, fundo, repetir, ss, not slideshow, letreiros, dur, parcial)

        if previa:
            parcial.replace(ARQ_PREVIA)
            job.resultado = {"video": f"/api/gerador/previa.mp4?v={job.id}", "inicio": round(w0, 2),
                             "duracao": round(dur, 2), "total": round(total, 2)}
        else:
            parcial.replace(destino)
            capitulos = texto_capitulos(titulos, inicios, total)
            txt = destino.with_suffix(".txt")
            txt.write_text(capitulos, encoding="utf-8")
            job.resultado = {"video": url_saida(destino.name), "arquivo": str(destino),
                             "capitulos": url_saida(txt.name), "texto_capitulos": capitulos,
                             "duracao": round(total, 2), "tamanho": destino.stat().st_size,
                             "codificador": codificador()["rotulo"], "crossfade": xf}
        parcial = None
        job.estado = "pronto"
    except _Cancelado:
        job.estado = "cancelado"
    except ErroGerador as e:
        job.estado, job.erro = "erro", str(e)
    except Exception as e:  # erro de programa: o traceback vai para o console do servidor
        traceback.print_exc()
        job.estado, job.erro = "erro", f"Erro inesperado: {type(e).__name__}: {e}"
    finally:
        job.fim = time.time()
        if parcial is not None:
            parcial.unlink(missing_ok=True)
        if job.estado != "erro" or not os.environ.get("GERADOR_MANTER_TEMP"):
            shutil.rmtree(job.pasta, ignore_errors=True)


def _ganho(e: dict) -> float:
    lufs = e.get("lufs")
    if lufs is None:
        return 0.0
    return max(-config.GERADOR_GANHO_MAX_DB, min(config.GERADOR_GANHO_MAX_DB, config.GERADOR_ALVO_LUFS - lufs))


def _analisar(job: Trabalho, arquivos: list[Path]):
    feitos = [0]

    def um(p: Path):
        log, tempo = job.ffmpeg(["-i", str(p), "-vn", "-af", "aresample=48000,aformat=sample_fmts=fltp:channel_layouts="
                                 "stereo,ebur128=peak=true:framelog=verbose", "-f", "null", "-"],
                                f"a análise de {p.name}", nivel="info")
        i = re.findall(r"\bI:\s+(-?[\d.]+|-inf)\s+LUFS", log)
        pico = re.findall(r"\bPeak:\s+(-?[\d.]+|-inf)\s+dBFS", log)
        e = _entrada_cache(p)
        e["lufs"] = float(i[-1]) if i and i[-1] != "-inf" else None
        e["pico"] = float(pico[-1]) if pico and pico[-1] != "-inf" else None
        if tempo > 0:
            e["duracao"] = tempo  # duração decodificada: é a que o acrossfade usa
        e["exata"] = True
        feitos[0] += 1
        job.avancar(feitos[0] / len(arquivos))

    with ThreadPoolExecutor(max_workers=3) as ex:
        list(ex.map(um, arquivos))
    _gravar_cache()


def _mixar(job: Trabalho, arquivos, duracoes, ganhos, inicios, xf, w0, dur, limitar: bool):
    """Mistura só as faixas que tocam na janela [w0, w0+dur): na prévia são uma ou duas; no vídeo, todas."""
    n = len(arquivos)
    a = next(i for i in range(n) if inicios[i] + duracoes[i] > w0 + 1e-6)
    b = max(i for i in range(n) if inicios[i] < w0 + dur - 1e-6)
    entradas, partes = [], []
    for j, i in enumerate(range(a, b + 1)):
        entradas += ["-i", arquivos[i].name]
        partes.append(f"[{j}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,"
                      f"volume={ganhos[i]:.2f}dB[a{j}]")
    m = b - a + 1
    ultimo = "a0"
    if m > 1 and xf > 0:
        for j in range(1, m):  # qsin nos dois lados: a potência fica constante durante a troca
            partes.append(f"[{ultimo}][a{j}]acrossfade=d={xf:.3f}:c1=qsin:c2=qsin[x{j}]")
            ultimo = f"x{j}"
    elif m > 1:
        partes.append("".join(f"[a{j}]" for j in range(m)) + f"concat=n={m}:v=0:a=1[x]")
        ultimo = "x"
    fim = ["alimiter=limit=0.891:level=0:latency=1"] if limitar else []  # teto em -1 dBFS, sem auto-nível
    fim.append(f"atrim=start={w0 - inicios[a]:.4f}:duration={dur:.4f},asetpts=PTS-STARTPTS")
    partes.append(f"[{ultimo}]{','.join(fim)}[aout]")
    script = job.pasta / "filtro_audio.txt"
    script.write_text(";\n".join(partes), encoding="utf-8")
    # cwd na pasta de músicas (a de quando o trabalho começou): só o nome de cada arquivo vai na linha de comando
    job.ffmpeg([*entradas, *config.filtro_de_arquivo(str(script)), "-map", "[aout]", "-c:a", "aac", "-b:a", f"{config.GERADOR_AUDIO_KBPS}k",
                "-ar", "48000", str(job.pasta / "audio.m4a")], "a mixagem do áudio", dur, cwd=arquivos[0].parent)


def _fundo_slides(job: Trabalho, cfg: dict, imagens: list[Path], total: float, w0: float, dur: float):
    """Renderiza o ciclo do slideshow (ou só a janela da prévia). Devolve o arquivo e se ele precisa repetir."""
    ordem = list(imagens)
    if cfg["ordem"] == "aleatoria":
        random.Random(cfg["semente"]).shuffle(ordem)
    sl = _Slides([job.pasta / f"img_{i:04d}.jpg" for i in range(len(ordem))], cfg)
    total_q = round(total * FPS)
    base_q = min(sl.ciclo, total_q)
    repetir = total_q > base_q
    if job.tipo == "previa":
        w0q = round(w0 * FPS)
        ini = sl.x + (w0q % sl.ciclo if repetir else w0q)
        trechos = [(ini, ini + round(dur * FPS))]
    else:
        trechos = sl.blocos(base_q)

    job.etapa_("Preparando as imagens")
    usadas = sorted({k % sl.k for ca, cb in trechos for k in range((ca - sl.x) // sl.t, (cb - 1) // sl.t + 1)})
    w, h = (2 * LARGURA, 2 * ALTURA) if sl.ken_burns else (LARGURA, ALTURA)  # 2x: o zoom lento não treme
    for n, i in enumerate(usadas):
        job.checar()
        _preparar_imagem(ordem[i], sl.imagens[i], w, h, cfg["ajuste"])
        job.avancar((n + 1) / len(usadas))

    job.etapa_("Montando o fundo")
    total_trechos = sum(cb - ca for ca, cb in trechos)
    feito, blocos = 0, []
    for i, (ca, cb) in enumerate(trechos):
        entradas, filtro = sl.filtro(ca, cb)
        script = job.pasta / f"filtro_fundo_{i:03d}.txt"
        script.write_text(filtro, encoding="utf-8")
        saida = job.pasta / f"fundo_{i:03d}.mp4"
        parte = (feito / total_trechos, (feito + cb - ca) / total_trechos)

        def montar(v, e=entradas, s=script, q=cb - ca, o=saida):
            return [*e, *config.filtro_de_arquivo(s.name), "-map", "[v]", "-frames:v", str(q), *v, "-an", o.name]
        job.ffmpeg_video(montar, f"o bloco {i + 1} de {len(trechos)} do fundo", (cb - ca) / FPS, final=False, parte=parte)
        blocos.append((montar, saida, codificador()["nome"]))
        feito += cb - ca
    for i, (montar, saida, cod) in enumerate(blocos):  # a GPU caiu no meio: refaz na CPU o que saiu no NVENC
        if cod != codificador()["nome"]:
            job.ffmpeg_video(montar, f"o bloco {i + 1} do fundo (CPU)", None, final=False)
    if len(blocos) == 1:
        return blocos[0][1], repetir
    (job.pasta / "fundo.txt").write_text("".join(f"file '{s.name}'\n" for _, s, _ in blocos), encoding="utf-8")
    job.ffmpeg(["-f", "concat", "-safe", "0", "-i", "fundo.txt", "-c", "copy", "fundo.mp4"], "a junção do fundo")
    for _, s, _ in blocos:
        s.unlink(missing_ok=True)
    return job.pasta / "fundo.mp4", repetir


def _posicao_texto(pos: str, ascent: int, descent: int) -> tuple[str, str]:
    """x e a linha de base do texto (y_align=baseline): a altura não pula entre títulos com e sem descendentes."""
    m = config.GERADOR_MARGEM_TEXTO
    lado = pos.split("_")[1] if "_" in pos else "centro"
    x = {"esq": str(m), "dir": f"w-tw-{m}", "centro": "(w-tw)/2"}[lado]
    if pos.startswith("inf"):
        y = f"h-{m}-{descent}"
    elif pos.startswith("sup"):
        y = f"{m}+{ascent}"
    else:
        y = f"(h+{ascent}-{descent})/2"
    return x, y


def _passada_final(job: Trabalho, cfg: dict, fundo: Path, repetir: bool, ss: float, clipe: bool,
                   letreiros: list[tuple[str, float, float]], dur: float, destino: Path):
    entradas = (["-stream_loop", "-1"] if repetir else []) + (["-ss", f"{ss:.3f}"] if ss > 0 else [])
    entradas += ["-i", str(fundo), "-i", "audio.m4a"]
    idx, partes = 2, []
    W, H = LARGURA, ALTURA
    if clipe and cfg["ajuste"] == "preencher":
        partes += [f"[0:v]fps={FPS},split=2[cf][cd]",
                   f"[cd]scale={W // 8}:{H // 8}:force_original_aspect_ratio=increase:out_color_matrix=bt709:out_range=tv,"
                   f"crop={W // 8}:{H // 8},"
                   f"gblur=sigma=2,scale={W}:{H},eq=brightness=-0.15[cdb]",
                   f"[cf]scale={W}:{H}:force_original_aspect_ratio=decrease:out_color_matrix=bt709:out_range=tv[cff]",
                   "[cdb][cff]overlay=(W-w)/2:(H-h)/2,setsar=1,format=yuv420p[bg]"]
    elif clipe:
        partes.append(f"[0:v]scale={W}:{H}:force_original_aspect_ratio=increase:out_color_matrix=bt709:out_range=tv,"
                      f"crop={W}:{H},fps={FPS},setsar=1,format=yuv420p[bg]")
    else:
        partes.append(f"[0:v]fps={FPS},setsar=1,format=yuv420p[bg]")
    atual = "bg"
    if cfg["chuva"]:
        for i, (png, vx, vy) in enumerate(_texturas_chuva()):
            entradas += ["-i", str(png)]
            partes.append(f"[{idx}:v]{COR_HD},format=yuva420p,loop=loop=-1:size=1:start=0,setpts=N/{FPS}/TB[cv{i}]")
            partes.append(f"[{atual}][cv{i}]overlay=x='mod(t*{vx:.1f},{W})-{W}':y='mod(t*{vy:.1f},{H})-{H}':"
                          f"shortest=1[ch{i}]")
            atual, idx = f"ch{i}", idx + 1
    if cfg["grao"]:
        partes.append(f"[{atual}]noise=c0s=10:c0f=t[gr]")
        atual = "gr"
    if cfg["vinheta"]:
        _vinheta(job.pasta / "vinheta.png")
        entradas += ["-i", "vinheta.png"]
        partes.append(f"[{idx}:v]{COR_HD},format=yuva420p,loop=loop=-1:size=1:start=0,setpts=N/{FPS}/TB[vm]")
        partes.append(f"[{atual}][vm]overlay=0:0:shortest=1[vg]")
        atual, idx = "vg", idx + 1
    t = cfg["texto"]
    if t["ligado"] and letreiros:
        fonte = Path(t["fonte"])
        copia = job.pasta / f"fonte{fonte.suffix.lower()}"  # caminho relativo: nada de escapar "C:" no filtro
        shutil.copyfile(fonte, copia)
        ascent, descent = ImageFont.truetype(str(fonte), t["tamanho"]).getmetrics()
        x, y = _posicao_texto(t["posicao"], ascent, descent)
        textos = []
        for i, (nome, a, b) in enumerate(letreiros):
            (job.pasta / f"titulo_{i:03d}.txt").write_text(nome, encoding="utf-8")
            f = min(config.GERADOR_FADE_TEXTO, (b - a) / 3)
            alfa = f"min(1,max(0,min((t-{a:.3f})/{f:.3f},({b:.3f}-t)/{f:.3f})))"
            textos.append(f"drawtext=fontfile={copia.name}:textfile=titulo_{i:03d}.txt:expansion=none:"
                          f"fontsize={t['tamanho']}:fontcolor=0x{t['cor'][1:]}:shadowcolor=black@0.55:shadowx=2:"
                          f"shadowy=2:y_align=baseline:x={x}:y={y}:alpha='{alfa}':enable='between(t,{a:.3f},{b:.3f})'")
        partes.append(f"[{atual}]" + ",\n".join(textos) + "[tx]")
        atual = "tx"
    partes.append(f"[{atual}]format=yuv420p,{MARCA_HD}[v]")
    (job.pasta / "filtro_final.txt").write_text(";\n".join(partes), encoding="utf-8")

    def montar(v):
        return [*entradas, *config.filtro_de_arquivo("filtro_final.txt"), "-map", "[v]", "-map", "1:a", *v,
                "-r", str(FPS), "-c:a", "copy", "-t", f"{dur:.3f}", "-movflags", "+faststart", str(destino)]
    job.ffmpeg_video(montar, "a passada final", dur, final=True)
