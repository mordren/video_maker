"""Etapa 4: narração num arquivo único (entonação mais natural) com o tempo de cada palavra.

O Edge TTS devolve WordBoundary para cada palavra; com isso o programa sabe onde cada cena começa
e gera as legendas sem precisar do Whisper.
"""
import asyncio
import json
import re
import subprocess
from pathlib import Path

from . import config

TICKS = 10_000_000  # WordBoundary vem em unidades de 100 ns


def _executar_async(coro):
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


async def _sintetizar(texto: str, voz: dict, destino: Path) -> list[dict]:
    import edge_tts
    com = edge_tts.Communicate(texto, voz.get("nome") or "pt-BR-AntonioNeural", rate=voz.get("taxa") or "+0%",
                               pitch=voz.get("tom") or "+0Hz", boundary="WordBoundary")
    palavras = []
    with open(destino, "wb") as f:
        async for bloco in com.stream():
            if bloco["type"] == "audio":
                f.write(bloco["data"])
            elif bloco["type"] == "WordBoundary":
                palavras.append({"texto": bloco["text"], "inicio": bloco["offset"] / TICKS,
                                 "fim": (bloco["offset"] + bloco["duration"]) / TICKS})
    return palavras


def sintetizar(texto: str, voz: dict, destino: Path) -> list[dict]:
    return _executar_async(_sintetizar(texto, voz, destino))


def duracao(arquivo: Path) -> float:
    r = subprocess.run([config.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(arquivo)],
                       capture_output=True, text=True)
    return float(json.loads(r.stdout)["format"]["duration"])


def _alinhar(cenas: list[dict], palavras: list[dict]) -> list[dict]:
    """Atribui cada palavra do TTS a uma cena, procurando-a no texto completo a partir da posição anterior."""
    texto = ""
    limites = []
    for c in cenas:
        ini = len(texto)
        texto += c["narracao"] + " "
        limites.append((ini, len(texto)))
    baixo = texto.lower()
    pos = 0
    for p in palavras:
        alvo = p["texto"].lower().strip()
        achou = baixo.find(alvo, pos, pos + 80 + len(alvo)) if alvo else -1
        if achou >= 0:
            pos = achou + len(alvo)
            ref = achou
        else:
            ref = pos
        p["cena"] = next((i for i, (a, b) in enumerate(limites) if a <= ref < b), len(cenas) - 1)
    return palavras


def narrar(projeto_pasta: Path, cenas: list[dict], voz: dict, projeto_id=None) -> dict:
    """Gera narracao.mp3 e devolve {'duracao', 'palavras', 'cenas': [{'n','inicio','fim'}]} (tempos da narração)."""
    if (voz.get("provedor") or "edge") == "qwen":
        return narrar_por_cena(projeto_pasta, cenas, voz, projeto_id)
    return narrar_edge(projeto_pasta, cenas, voz)


def narrar_edge(projeto_pasta: Path, cenas: list[dict], voz: dict) -> dict:
    destino = projeto_pasta / "narracao.mp3"
    texto = "\n".join(c["narracao"] for c in cenas)
    palavras = sintetizar(texto, voz, destino)
    if not palavras:
        raise RuntimeError("O Edge TTS não devolveu os tempos das palavras")
    total = duracao(destino)
    _alinhar(cenas, palavras)
    inicios = []
    for i, c in enumerate(cenas):
        dessa = [p for p in palavras if p["cena"] == i]
        inicios.append(dessa[0]["inicio"] if dessa else None)
    # Cena sem palavra reconhecida: interpola entre as vizinhas.
    for i in range(len(inicios)):
        if inicios[i] is None:
            ant = next((inicios[j] for j in range(i - 1, -1, -1) if inicios[j] is not None), 0.0)
            prox = next((inicios[j] for j in range(i + 1, len(inicios)) if inicios[j] is not None), total)
            inicios[i] = (ant + prox) / 2
    inicios[0] = 0.0
    tempos = []
    for i, c in enumerate(cenas):
        fim = inicios[i + 1] if i + 1 < len(cenas) else total
        tempos.append({"n": c["n"], "inicio": round(inicios[i], 3), "fim": round(fim, 3)})
    resultado = {"duracao": total, "palavras": palavras, "cenas": tempos}
    (projeto_pasta / "narracao.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
    return resultado


def previa(voz: dict, texto: str, destino: Path) -> Path:
    if (voz.get("provedor") or "edge") == "qwen":
        dados, ext = _qwen_sintetizar(texto, voz, None, None)
        bruto = destino.with_name(f"{destino.stem}_bruto.{ext}")
        bruto.write_bytes(dados)
        _ffmpeg_audio(["-i", str(bruto), "-af", _filtro_velocidade(voz) or "anull", "-c:a", "libmp3lame", "-q:a", "3",
                       str(destino)])
        bruto.unlink(missing_ok=True)
        return destino
    sintetizar(texto, voz, destino)
    return destino


# ---------------------------------------------------------------- Qwen TTS (OpenRouter), uma cena por vez
#
# O Qwen não devolve o tempo das palavras. Por isso a narração é gerada cena por cena e as partes são
# juntadas com uma pausa curta: a troca de imagem cai exatamente no começo de cada parte. Dentro da cena,
# o tempo de cada palavra é distribuído pelo tamanho das palavras e pelas pausas da pontuação (serve para
# as legendas; cada cena tem no máximo 7 s, então o erro fica pequeno).
# O modelo não aceita "speed": a velocidade é aplicada depois, no FFmpeg (atempo, sem mudar o tom).
# O jeito de falar vai na instrução, uma opção do provedor Alibaba.


def _ffmpeg_audio(args: list[str]):
    r = subprocess.run([config.FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"FFmpeg (áudio) falhou: {r.stderr[-800:]}")


def _filtro_velocidade(voz: dict) -> str | None:
    """'taxa' no formato do Edge (ex.: -6%) vira atempo para o Qwen."""
    m = re.match(r"^([+-]?\d+(?:\.\d+)?)%$", str(voz.get("taxa") or "").strip())
    if not m or float(m.group(1)) == 0:
        return None
    return f"atempo={max(0.5, min(2.0, 1 + float(m.group(1)) / 100)):.3f}"


def _instrucoes(voz: dict, tensao: int | None) -> str | None:
    base = (voz.get("instrucoes") or "").strip()
    alta = (voz.get("instrucoes_tensao_alta") or "").strip()
    if tensao and tensao >= 4 and alta:
        return f"{base}. {alta}" if base else alta
    return base or None


def _qwen_sintetizar(texto: str, voz: dict, tensao: int | None, projeto_id) -> tuple[bytes, str]:
    import httpx

    from . import custos
    if config.SIMULACAO:
        # Em simulação usa o Edge (grátis) só para ter áudio de verdade no teste.
        tmp = Path(config.DADOS) / "sim_qwen.mp3"
        sintetizar(texto, {"nome": "pt-BR-AntonioNeural"}, tmp)
        custos.registrar(projeto_id, "narracao", "simulacao", voz.get("modelo"), 0.0, True, texto[:40])
        return tmp.read_bytes(), "mp3"
    if not config.OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY não configurada no arquivo .env")
    modelo = voz.get("modelo") or "qwen/qwen-audio-3.0-tts-flash"
    corpo = {"model": modelo, "input": texto, "voice": voz.get("nome") or "loongjohn", "response_format": "mp3"}
    instr = _instrucoes(voz, tensao)
    if instr:
        corpo["provider"] = {"options": {"alibaba": {"instruction": instr}}}
    ultimo = None
    for _ in range(config.API_TENTATIVAS):
        try:
            r = httpx.post(config.OPENROUTER_TTS_URL, json=corpo, timeout=config.API_TIMEOUT_S,
                           headers={"Authorization": f"Bearer {config.OPENROUTER_API_KEY}",
                                    "Content-Type": "application/json"})
        except (httpx.TimeoutException, httpx.TransportError) as e:
            ultimo = e
            continue
        if r.status_code >= 500 or r.status_code == 429:
            ultimo = f"HTTP {r.status_code}: {r.text[:200]}"
            continue
        tipo = r.headers.get("content-type", "")
        if r.status_code != 200 or "json" in tipo:
            raise RuntimeError(f"Qwen TTS (OpenRouter) HTTP {r.status_code}: {r.text[:300]}")
        if "pcm" in tipo:  # se o provedor ignorar o mp3, chega PCM cru (24 kHz, mono)
            bruto = Path(config.DADOS) / "qwen_pcm.raw"
            bruto.write_bytes(r.content)
            wav = bruto.with_suffix(".wav")
            taxa = re.search(r"rate=(\d+)", tipo)
            _ffmpeg_audio(["-f", "s16le", "-ar", taxa.group(1) if taxa else "24000", "-ac", "1", "-i", str(bruto),
                           str(wav)])
            dados, ext = wav.read_bytes(), "wav"
        else:
            dados, ext = r.content, "mp3" if ("mpeg" in tipo or "mp3" in tipo) else "wav"
        custos.registrar(projeto_id, "narracao", "openrouter", modelo, len(texto) * config.PRECO_TTS_POR_CARACTERE, False,
                         f"voz {corpo['voice']} · {len(texto)} caracteres · {r.headers.get('x-generation-id', '')}")
        return dados, ext
    raise RuntimeError(f"Qwen TTS sem resposta: {ultimo}")


def _pesos_palavras(texto: str) -> list[tuple[str, float]]:
    """Palavras da cena com um peso de tempo: tamanho da palavra + pausa da pontuação que vem depois."""
    saida = []
    for m in re.finditer(r"[\wÀ-ÿ]+(?:['’-][\wÀ-ÿ]+)*[^\w\sÀ-ÿ]*", texto):
        bruto = m.group(0)
        palavra = re.match(r"[\wÀ-ÿ]+(?:['’-][\wÀ-ÿ]+)*", bruto).group(0)
        peso = len(palavra) + 1.5
        if re.search(r"[,;:]$", bruto):
            peso += 4
        elif re.search(r"[.!?…]$", bruto):
            peso += 7
        saida.append((palavra, peso))
    return saida


def narrar_por_cena(projeto_pasta: Path, cenas: list[dict], voz: dict, projeto_id=None) -> dict:
    import hashlib
    pasta = projeto_pasta / "narracao_cenas"
    pasta.mkdir(exist_ok=True)
    pausa = float(voz.get("pausa_entre_cenas_s", 0.3))
    velocidade = _filtro_velocidade(voz)
    limpeza = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,areverse,"
               "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.05,areverse")
    partes = []
    for c in cenas:
        chave = hashlib.sha1(json.dumps([c["narracao"], voz, c.get("tensao")], sort_keys=True,
                                        ensure_ascii=False).encode()).hexdigest()[:12]
        pronto = pasta / f"{c['n']:02d}_{chave}.wav"
        if not pronto.exists():  # reaproveita o áudio se texto e voz não mudaram ("Montar de novo" não paga de novo)
            dados, ext = _qwen_sintetizar(c["narracao"], voz, c.get("tensao"), projeto_id)
            bruto = pasta / f"{c['n']:02d}_bruto.{ext}"
            bruto.write_bytes(dados)
            filtro = limpeza + (f",{velocidade}" if velocidade else "")
            _ffmpeg_audio(["-i", str(bruto), "-af", filtro, "-ar", "24000", "-ac", "1", str(pronto)])
            bruto.unlink(missing_ok=True)
            for velho in pasta.glob(f"{c['n']:02d}_*.wav"):
                if velho != pronto:
                    velho.unlink(missing_ok=True)
        partes.append((c, pronto, duracao(pronto)))

    # Junta tudo com a pausa entre as cenas.
    silencio = pasta / "pausa.wav"
    _ffmpeg_audio(["-f", "lavfi", "-i", f"anullsrc=r=24000:cl=mono", "-t", f"{pausa:.3f}", str(silencio)])
    lista = pasta / "lista.txt"
    linhas = []
    for i, (_, arq, _) in enumerate(partes):
        linhas.append(f"file '{arq.name}'")
        if i < len(partes) - 1:
            linhas.append("file 'pausa.wav'")
    lista.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    destino = projeto_pasta / "narracao.mp3"
    _ffmpeg_audio(["-f", "concat", "-safe", "0", "-i", str(lista), "-c:a", "libmp3lame", "-q:a", "2", str(destino)])

    palavras, tempos, t = [], [], 0.0
    for i, (c, _, dur) in enumerate(partes):
        pesos = _pesos_palavras(c["narracao"])
        total = sum(p for _, p in pesos) or 1.0
        cursor = t
        for palavra, peso in pesos:
            fatia = dur * peso / total
            palavras.append({"texto": palavra, "inicio": round(cursor, 3),
                             "fim": round(cursor + min(fatia, len(palavra) * 0.09 + 0.12), 3), "cena": i})
            cursor += fatia
        fim = t + dur + (pausa if i < len(partes) - 1 else 0)
        tempos.append({"n": c["n"], "inicio": round(t if i else 0.0, 3), "fim": round(fim, 3)})
        t = fim
    total = duracao(destino)
    tempos[-1]["fim"] = round(total, 3)
    resultado = {"duracao": total, "palavras": palavras, "cenas": tempos, "provedor": "qwen"}
    (projeto_pasta / "narracao.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=1), encoding="utf-8")
    return resultado


# ---------------------------------------------------------------- legendas (ASS)

def _cor_ass(hexcor: str, alpha: str = "00") -> str:
    h = (hexcor or "#FFFFFF").lstrip("#")
    if len(h) != 6:
        h = "FFFFFF"
    return f"&H{alpha}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def _tempo_ass(s: float) -> str:
    s = max(0.0, s)
    h = int(s // 3600)
    m = int(s % 3600 // 60)
    return f"{h}:{m:02d}:{s % 60:05.2f}"


def _escapar(t: str) -> str:
    return t.replace("\\", "").replace("{", "(").replace("}", ")")


def _blocos(palavras: list[dict], minimo: int, maximo: int) -> list[list[dict]]:
    blocos, atual = [], []
    for i, p in enumerate(palavras):
        atual.append(p)
        prox = palavras[i + 1] if i + 1 < len(palavras) else None
        fim_frase = re.search(r"[.!?…]$", p["texto"]) is not None
        pausa = re.search(r"[,;:]$", p["texto"]) is not None
        troca_cena = prox is not None and prox["cena"] != p["cena"]
        if prox is None or troca_cena or len(atual) >= maximo or fim_frase or (pausa and len(atual) >= minimo):
            blocos.append(atual)
            atual = []
    return blocos


def _palavras_com_pontuacao(texto_cenas: list[str], palavras: list[dict]) -> list[dict]:
    """O WordBoundary vem sem pontuação; recupera a pontuação do texto original para quebrar as legendas."""
    completo = " ".join(texto_cenas)
    pos = 0
    saida = []
    for p in palavras:
        alvo = p["texto"]
        i = completo.lower().find(alvo.lower(), pos, pos + 80 + len(alvo))
        if i >= 0:
            fim = i + len(alvo)
            m = re.match(r"[^\w\s]*", completo[fim:])
            pont = m.group(0) if m else ""
            texto = completo[i:fim] + pont
            pos = fim
        else:
            texto = alvo
        saida.append({**p, "texto": texto})
    return saida


def legendas_ass(palavras: list[dict], texto_cenas: list[str], estilo: dict, largura: int, altura: int,
                 deslocamento: float, destino: Path) -> Path | None:
    if estilo.get("desligada") or estilo.get("preset") == "nenhuma":
        return None
    palavras = _palavras_com_pontuacao(texto_cenas, palavras)
    minimo = int(estilo.get("palavras_min", 2))
    maximo = int(estilo.get("palavras_max", 4))
    maius = bool(estilo.get("maiusculas"))
    karaoke = bool(estilo.get("karaoke"))
    centro = estilo.get("posicao", "centro") == "centro"
    alinhamento = 5 if centro else 2
    margem_v = 0 if centro else int(altura * 0.16)
    escala = altura / 1920 if largura < altura else altura / 1080 * 0.75
    tamanho = int(float(estilo.get("tamanho", 80)) * escala)
    cab = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {largura}
PlayResY: {altura}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Leg,{estilo.get('fonte', 'Arial Black')},{tamanho},{_cor_ass(estilo.get('cor'))},{_cor_ass(estilo.get('destaque'))},&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,{estilo.get('contorno', 6)},{estilo.get('sombra', 2)},{alinhamento},{int(largura * 0.08)},{int(largura * 0.08)},{margem_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    linhas = []
    blocos = _blocos(palavras, minimo, maximo)
    destaque = _cor_ass(estilo.get("destaque"))
    for bi, bloco in enumerate(blocos):
        ini = bloco[0]["inicio"]
        prox_ini = blocos[bi + 1][0]["inicio"] if bi + 1 < len(blocos) else bloco[-1]["fim"] + 0.4
        fim = min(prox_ini, bloco[-1]["fim"] + 0.6)
        textos = [_escapar(p["texto"].upper() if maius else p["texto"]) for p in bloco]
        if karaoke and len(bloco) > 1:
            for wi, p in enumerate(bloco):
                w_ini = p["inicio"]
                w_fim = bloco[wi + 1]["inicio"] if wi + 1 < len(bloco) else fim
                partes = [f"{{\\c{destaque}}}{t}{{\\r}}" if j == wi else t for j, t in enumerate(textos)]
                linhas.append(f"Dialogue: 0,{_tempo_ass(w_ini + deslocamento)},{_tempo_ass(w_fim + deslocamento)},Leg,,0,0,0,,"
                              + " ".join(partes))
        else:
            linhas.append(f"Dialogue: 0,{_tempo_ass(ini + deslocamento)},{_tempo_ass(fim + deslocamento)},Leg,,0,0,0,,"
                          + " ".join(textos))
    destino.write_text(cab + "\n".join(linhas) + "\n", encoding="utf-8")
    return destino
