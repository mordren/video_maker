"""Caminhos, chaves e parâmetros globais. Tudo que é por canal fica no banco (gerenciador de canais)."""
import os
import shutil
from pathlib import Path

from dotenv import load_dotenv

CODIGO = Path(__file__).resolve().parent.parent
load_dotenv(CODIGO / ".env")
BASE = CODIGO

# ESTUDIO_RAIZ troca a pasta onde ficam banco, projetos, biblioteca e trilhas (útil para testes).
BASE = Path(os.getenv("ESTUDIO_RAIZ", BASE)).resolve()
DADOS = BASE / "dados"
PROJETOS = BASE / "projetos"
BIBLIOTECA = BASE / "biblioteca"
TRILHAS = BASE / "trilhas"
for _p in (DADOS, PROJETOS, BIBLIOTECA, TRILHAS):
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
MODELO_IMAGEM = os.getenv("MODELO_IMAGEM", "black-forest-labs/flux.2-klein-4b")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_DECISOES_URL = "https://openrouter.ai/api/alpha/decisions"
OPENROUTER_MODELOS_URL = "https://openrouter.ai/api/v1/models"
POLLI_URL = "https://gen.pollinations.ai"
POLLI_MEDIA_URL = "https://media.pollinations.ai"

# 1 Pollen ~ US$ 1 (mesma conta usada no roteiro).
POLLEN_USD = float(os.getenv("POLLEN_USD", "1.0"))

# Preços de referência para estimativa quando a API não devolve o custo real.
PRECO_IMAGEM_PADRAO = 0.005           # Klein, por imagem
PRECO_JEV_TOKEN_ENTRADA = 0.00000004431  # Pollinations; OpenRouter costuma ser parecido
PRECO_TEXTO_ENTRADA = 0.0000003
PRECO_TEXTO_SAIDA = 0.0000012

# Laço da história (Etapa 2)
LACO_TETO_USD = float(os.getenv("LACO_TETO_USD", "0.01"))
# Histórias inteiras escritas do zero (com o mesmo gancho) até uma passar no Jev; fica a de melhor nota.
LACO_MAX_HISTORIAS = int(os.getenv("LACO_MAX_HISTORIAS", "4"))
GANCHO_MAX_RODADAS = 3

FFMPEG = shutil.which("ffmpeg") or "ffmpeg"
FFPROBE = shutil.which("ffprobe") or "ffprobe"

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
