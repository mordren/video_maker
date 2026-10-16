"""Caminhos, chaves e parâmetros globais. Tudo que é por canal fica no banco (gerenciador de canais)."""
import os
import shutil
import subprocess
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

CODIGO = Path(__file__).resolve().parent.parent
# Arquivo .env com as chaves e variáveis. ESTUDIO_ENV troca o caminho (os testes usam um .env falso); a aba
# Configurações grava aqui, no lugar.
ENV_ARQUIVO = Path(os.getenv("ESTUDIO_ENV") or CODIGO / ".env").resolve()
# O que veio do sistema, antes de o .env entrar no ambiente: a aba Configurações usa para dizer de onde vem cada valor.
AMBIENTE_ANTES = dict(os.environ)
load_dotenv(ENV_ARQUIVO)
BASE = CODIGO

# ESTUDIO_RAIZ troca a pasta onde ficam banco, projetos, biblioteca e trilhas (útil para testes).
BASE = Path(os.getenv("ESTUDIO_RAIZ", BASE)).resolve()
DADOS = BASE / "dados"
PROJETOS = BASE / "projetos"
BIBLIOTECA = BASE / "biblioteca"
TRILHAS = BASE / "trilhas"
# Gerador de Vídeo (playlists lo-fi em 16:9): músicas e imagens de entrada, vídeos e capítulos de saída.
GERADOR_MUSICAS = BASE / "musicas"
GERADOR_IMAGENS = BASE / "imagens"
GERADOR_SAIDA = BASE / "saida"
GERADOR_VIDEOS = BASE / "videos"   # clipes para o modo "clipe em loop" (também aceita os vídeos de imagens/)
for _p in (DADOS, PROJETOS, BIBLIOTECA, TRILHAS, GERADOR_MUSICAS, GERADOR_IMAGENS, GERADOR_SAIDA):
    _p.mkdir(parents=True, exist_ok=True)

DB_PATH = DADOS / "estudio.db"

HUMANIZER_SKILL = Path(os.getenv("HUMANIZER_SKILL", CODIGO / "Humanizer br-v1" / "skills" / "humanizer-br" / "SKILL.md"))

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
POLLINATIONS_API_KEY = os.getenv("POLLINATIONS_API_KEY", "").strip()
# Publicador (fila de postagem na rede local). O nome do canal lá fica na tela de cada canal.
PUBLICADOR_URL = os.getenv("PUBLICADOR_URL", "http://192.168.31.157:8080").strip()
# Token opcional para proteger a API (/api/*) quando ela for exposta fora do computador.
API_TOKEN = os.getenv("ESTUDIO_API_TOKEN", "").strip()

# Modo simulação: nenhuma chamada paga, respostas falsas. Serve para testar o fluxo inteiro.
SIMULACAO = os.getenv("ESTUDIO_SIMULACAO", "0") == "1"

MODELO_TEXTO = os.getenv("MODELO_TEXTO", "deepseek/deepseek-v4.1-flash")
MODELO_JUIZ = os.getenv("MODELO_JUIZ", "typesafe/jev-1.13")
# Tarefas de texto que rodam com o raciocínio (thinking) do DeepSeek ligado, separadas por vírgula. Vazio = nenhuma;
# "todas" liga em tudo. Tarefas: ganchos, narrativa, reescrita, decupagem, prompts_cena, humanizer, sugestao_canal.
RACIOCINIO_TAREFAS = {t.strip() for t in os.getenv("RACIOCINIO_TAREFAS", "narrativa,reescrita").lower().split(",") if t.strip()}
# Provedor do texto: "openrouter" (usa MODELO_TEXTO) ou "deepseek" (API oficial: deepseek-chat, ou deepseek-reasoner nas
# tarefas com raciocínio). Juiz (Jev) e narração seguem no OpenRouter; a imagem tem o motor próprio.
PROVEDOR_TEXTO = os.getenv("PROVEDOR_TEXTO", "openrouter").strip().lower()
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "").strip()
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
PRECO_DEEPSEEK_ENTRADA = 0.28e-6         # US$ por token, entrada sem cache
PRECO_DEEPSEEK_ENTRADA_CACHE = 0.028e-6  # entrada que veio do cache
PRECO_DEEPSEEK_SAIDA = 0.42e-6           # saída, já contando os tokens de raciocínio
ROTULO_DEEPSEEK = "deepseek (oficial)"   # o que a tabela de custos mostra como modelo


def modelo_texto() -> str:
    """O modelo como aparece em Custos e no relatório: o do OpenRouter, ou o rótulo da API oficial."""
    return ROTULO_DEEPSEEK if PROVEDOR_TEXTO == "deepseek" else MODELO_TEXTO


MODELO_IMAGEM = os.getenv("MODELO_IMAGEM", "black-forest-labs/flux.2-klein-4b")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DECISOES_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODELOS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_TTS_URL = "https://openrouter.ai/api/v1/audio/speech"
# Motor de imagem: "pollinations" (padrão) ou "comfy" (ComfyUI no Colab, exposto por túnel).
# Motor, URL e workflow são relidos do .env a cada imagem: o túnel do Colab muda de endereço a cada sessão e
# assim basta trocar a linha no .env, sem reiniciar o estúdio.
COMFY_PASTA = CODIGO / "estudio" / "comfy"
COMFY_MEGAPIXELS = float(os.getenv("COMFY_MEGAPIXELS", "0.6"))  # área da imagem gerada; 1.0 rende mais, pesa o dobro
COMFY_MEGAPIXELS_REF = float(os.getenv("COMFY_MEGAPIXELS_REF", "0.4"))  # cada referência (ficha/placa) enviada
COMFY_TIMEOUT_S = int(os.getenv("COMFY_TIMEOUT_S", "900"))     # inclui a espera na fila do Comfy


def _env_vivo(nome: str, padrao: str = "") -> str:
    return (dotenv_values(ENV_ARQUIVO).get(nome) or os.getenv(nome) or padrao).strip()


def motor_imagem() -> str:
    """"comfy" (padrão) = ComfyUI quando o link responder, senão a API (Pollinations); "pollinations" = só a API."""
    return _env_vivo("MOTOR_IMAGEM", "comfy").lower()


def comfy_workflow() -> Path:
    """COMFY_WORKFLOW=kontext (padrão) ou qwen_edit (arquivos de estudio/comfy) ou o caminho completo de outro JSON."""
    nome = _env_vivo("COMFY_WORKFLOW", "kontext")
    caminho = Path(nome)
    if not caminho.is_absolute():
        caminho = COMFY_PASTA / (nome if nome.endswith(".json") else f"{nome}.json")
    return caminho


def comfy_url() -> str:
    return _env_vivo("COMFY_URL").rstrip("/")


POLLI_URL = "https://gen.pollinations.ai"
POLLI_MEDIA_URL = "https://media.pollinations.ai"

# 1 Pollen ~ US$ 1 (mesma conta usada no roteiro).
POLLEN_USD = float(os.getenv("POLLEN_USD", "1.0"))

# Preços de referência para estimativa quando a API não devolve o custo real.
PRECO_IMAGEM_PADRAO = 0.005           # Klein, por imagem
PRECO_JEV_TOKEN_ENTRADA = 0.00000004431  # Pollinations; OpenRouter costuma ser parecido
PRECO_TEXTO_ENTRADA = 0.0000003
PRECO_TEXTO_SAIDA = 0.0000012
PRECO_TTS_POR_CARACTERE = 0.000015    # qwen-audio-3.0-tts-flash (conferido: 96 caracteres = US$ 0,00144)

# Laço da história (Etapa 2)
LACO_TETO_USD = float(os.getenv("LACO_TETO_USD", "0.01"))
# Histórias inteiras escritas do zero (com o mesmo gancho) até uma passar no Jev; fica a de melhor nota.
LACO_MAX_HISTORIAS = int(os.getenv("LACO_MAX_HISTORIAS", "4"))
# Reprovada numa pergunta com trava (segurança etc.): quantas vezes recomeçar do gancho com outra vibe antes de parar.
RECOMECOS_TRAVA = int(os.getenv("RECOMECOS_TRAVA", "3"))
GANCHO_MAX_RODADAS = 3
FOLGA_PALAVRAS = 0.05      # tolerância no total de palavras, antes de mandar reescrever (0,05 = 5% para mais ou menos)
FOLGA_PALAVRAS_CENA = 2    # palavras a mais por cena
EST_TEXTO = 0.003          # estimativa (US$) de uma chamada de escrita, usada até haver medição
EST_JEV = 0.001            # idem, uma chamada do Jev
# Humanizer: só é desfeito em caso grave.
COERENCIA_MINIMA = 0.6
GANCHO_FOLGA = 2.0
VOLTAS_VERIFICADOR = 2

# Rede, imagens e publicador
API_TIMEOUT_S = 180        # texto, Jev e narração (OpenRouter)
API_TENTATIVAS = 3
IMAGEM_TIMEOUT_S = 240     # Pollinations, por imagem
IMAGENS_SIMULTANEAS = 3    # cenas geradas ao mesmo tempo
COMFY_PAUSA_S = 120        # depois de uma queda do Comfy, tempo mandando as imagens para a Pollinations
PUBLICADOR_TIMEOUT_S = 600  # envio do vídeo pronto

# Narração e montagem
NARRACAO_LUFS = -16.0      # voz e trilha são normalizadas para este volume antes da mistura
MONTAGEM_PRESET = "medium"
MONTAGEM_AUDIO_KBPS = 192
MONTAGEM_CRF_CLIPES = 16   # clipes intermediários (cada cena); o vídeo final usa o crf do formato

# Gerador de Vídeo (aba Music). Resolução e fps valem depois de reiniciar o servidor.
GERADOR_LARGURA, GERADOR_ALTURA, GERADOR_FPS = 1280, 720, 30
GERADOR_ALVO_LUFS = -14.0  # referência de loudness do YouTube
GERADOR_GANHO_MAX_DB = 20.0
GERADOR_DURACAO_PREVIA = 10.0
GERADOR_MARGEM_TEXTO = 40  # px, nome da faixa na tela
GERADOR_FADE_TEXTO = 1.0
GERADOR_CRF = 20           # libx264, passada final
GERADOR_CQ_GPU = 21        # NVENC, passada final
GERADOR_PRESET = "veryfast"
GERADOR_MAXRATE_MBPS = 6   # teto de bitrate do vídeo final; o buffer é o dobro
GERADOR_AUDIO_KBPS = 192
GERADOR_USAR_GPU = True    # usa o NVENC quando a placa NVIDIA existir


def _exe(pasta: Path, nome: str) -> Path:
    return pasta / (nome + ".exe" if os.name == "nt" else nome)


def _escolher_ffmpeg() -> tuple[str, str, str]:
    """(ffmpeg, ffprobe, origem). Num Snapdragon X (Windows on ARM) o FFmpeg x64 do PATH roda emulado e é bem mais
    lento que a build ARM64 nativa. Ordem: FFMPEG_DIR do .env, a build ARM64 em %LOCALAPPDATA%\\Programs\\ffmpeg-arm64\\bin,
    o PATH."""
    candidatas = [("FFMPEG_DIR", os.getenv("FFMPEG_DIR", "").strip().strip('"'))]
    if os.getenv("LOCALAPPDATA"):
        arm64 = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "ffmpeg-arm64" / "bin"
        candidatas.append(("pasta ARM64 padrão", str(arm64)))
    for origem, pasta in candidatas:
        if pasta and _exe(Path(pasta), "ffmpeg").is_file():
            sonda = _exe(Path(pasta), "ffprobe")
            return (str(_exe(Path(pasta), "ffmpeg")),
                    str(sonda) if sonda.is_file() else shutil.which("ffprobe") or "ffprobe", origem)
    return shutil.which("ffmpeg") or "ffmpeg", shutil.which("ffprobe") or "ffprobe", "PATH"


def arquitetura_exe(caminho: str) -> str | None:
    """Arquitetura de um .exe do Windows, lida do cabeçalho PE (64 bytes + 6 bytes: não carrega o arquivo)."""
    achado = shutil.which(caminho)
    if not achado:
        return None
    try:
        with open(achado, "rb") as f:
            cab = f.read(64)
            if cab[:2] != b"MZ":
                return None
            f.seek(int.from_bytes(cab[60:64], "little"))
            pe = f.read(6)
    except OSError:
        return None
    if pe[:4] != b"PE\0\0":
        return None
    return {0xAA64: "ARM64", 0x8664: "x64", 0x014C: "x86", 0xA641: "ARM64EC"}.get(int.from_bytes(pe[4:6], "little"))


FFMPEG, FFPROBE, FFMPEG_ORIGEM = _escolher_ffmpeg()
FFMPEG_ARQUITETURA = arquitetura_exe(FFMPEG)


_opcao_filtro: str | None = None


def reescolher_ffmpeg():
    """Lê FFMPEG_DIR de novo e escolhe o FFmpeg outra vez (a aba Configurações chama depois de salvar)."""
    global FFMPEG, FFPROBE, FFMPEG_ORIGEM, FFMPEG_ARQUITETURA, _opcao_filtro
    FFMPEG, FFPROBE, FFMPEG_ORIGEM = _escolher_ffmpeg()
    FFMPEG_ARQUITETURA = arquitetura_exe(FFMPEG)
    _opcao_filtro = None


def filtro_de_arquivo(arquivo: str) -> list[str]:
    """Argumentos que leem o grafo de filtros de um arquivo (a linha de comando do Windows é curta demais para
    um drawtext por faixa). O FFmpeg 8 tirou o -filter_complex_script: a build ARM64 só aceita -/filter_complex."""
    global _opcao_filtro
    if _opcao_filtro is None:
        try:
            r = subprocess.run([FFMPEG, "-hide_banner", "-h", "long"], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=30,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            antigo = "-filter_complex_script" in r.stdout
        except (OSError, subprocess.TimeoutExpired):
            antigo = False
        _opcao_filtro = "-filter_complex_script" if antigo else "-/filter_complex"
    return [_opcao_filtro, arquivo]

FORMATOS = {
    "short": {
        "nome": "Short 9:16",
        "largura": 1080, "altura": 1920, "fps": 30,
        "img_largura": 1088, "img_altura": 1920,
        "ficha_largura": 1024, "ficha_altura": 1536,
        "max_imagens": 9, "min_cenas": 5,
        "palavras_min": 150, "palavras_max": 170,
        # 9 s por cena deixa a história decidir entre ~7 e 9 imagens (mais que isso repetia a mesma coisa).
        "max_cena_s": 9.0,
        "crf": 21, "maxrate": "3M",
        "movimento_padrao": "zoom_in",
    },
    "longo": {
        "nome": "Longo 16:9",
        "largura": 1920, "altura": 1080, "fps": 30,
        "img_largura": 2048, "img_altura": 1152,
        "ficha_largura": 1024, "ficha_altura": 1536,
        "max_imagens": 40, "min_cenas": 20,
        "palavras_min": 1300, "palavras_max": 2400,
        "max_cena_s": 30.0,
        "crf": 21, "maxrate": "6M",
        "movimento_padrao": "pan",
    },
}


# Valores salvos na aba Configurações (dados/configuracoes.json) entram por cima dos padrões acima. Precisa ser a
# última linha: o módulo configuracoes lê os atributos deste arquivo já prontos (FORMATOS é atualizado no lugar).
from . import configuracoes as _configuracoes  # noqa: E402

_configuracoes.carregar_inicial()
