"""Configurações globais do estúdio, editáveis na aba Configurações.

Dois lugares guardam os valores:
- env: o arquivo .env (config.ENV_ARQUIVO; a variável ESTUDIO_ENV troca o caminho). Chaves e tudo o que já era variável de
  ambiente. A gravação edita o arquivo no lugar: só a linha da chave muda; comentários, outras linhas e a ordem ficam.
- json: dados/configuracoes.json, só com o que difere do padrão (constantes que antes eram código, como o FORMATOS).

Depois de salvar, os atributos de config e o dicionário FORMATOS são atualizados no lugar: o que o programa lê na hora do
uso passa a valer sem reiniciar. Campos com ao_vivo=False são lidos na importação (ou criam pastas/binários) e só valem
depois de reiniciar o servidor.

Este módulo não importa config no topo: o config.py o importa no fim e chama carregar_inicial().
"""
import copy
import io
import json
import math
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

ARQUIVO_JSON = "configuracoes.json"
FORMATOS_ORDEM = ("short", "longo")
PRESETS_X264 = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]

_trava = threading.RLock()
_backup_feito = False          # o .env.bak sai antes da primeira gravação de cada execução
AVISOS: list[str] = []         # problemas achados ao ler o configuracoes.json
_PADRAO_FORMATOS: dict = {}    # FORMATOS como o config.py define, antes de o json entrar
_PADROES_ATTR: dict = {}       # idem para os atributos simples
DO_CONFIG = object()           # padrão = o valor que o config.py tinha antes de o json entrar


def _cfg():
    from . import config
    return config


# ---------------------------------------------------------------- esquema

@dataclass
class Campo:
    chave: str                       # variável de ambiente, nome do atributo do config ou FORMATOS.<formato>.<campo>
    rotulo: str
    tipo: str                        # segredo | texto | url | caminho | inteiro | numero | escolha | booleano
    padrao: Any                      # valor, função sem argumentos, ou DO_CONFIG
    ajuda: str
    onde: str = "json"               # env | json
    ao_vivo: bool = True
    minimo: float | None = None
    maximo: float | None = None
    passo: float | None = None
    unidade: str = ""
    opcoes: Any = None               # [(valor, rotulo)] ou função que devolve isso
    sugestoes: Any = None            # sugestões para texto livre (datalist)
    padrao_rotulo: str | None = None  # o que mostrar como padrão quando ele é "automático"
    attr: str | None = None          # atributo do config atualizado ao vivo (padrão: a própria chave)
    formato: str | None = None       # só nos campos do FORMATOS
    campo: str | None = None
    validar: Callable | None = None  # (campo, valor) -> levanta ValueError
    aplicar: Callable | None = None  # (config, valor) -> aplica ao vivo, quando setattr não basta


@dataclass
class Grupo:
    id: str
    titulo: str
    descricao: str
    campos: list[Campo] = field(default_factory=list)
    testes: list[tuple[str, str]] = field(default_factory=list)   # (serviço, rótulo do botão)


def _nome(c: Campo) -> str:
    return f"«{c.rotulo}» ({c.chave})"


def _v_par(c, v):
    if v % 2:
        raise ValueError(f"{_nome(c)} precisa ser um número par (o H.264 não aceita tamanho ímpar).")


def _v_modelo(c, v):
    if not re.fullmatch(r"[A-Za-z0-9._:/@+\-]+", v):
        raise ValueError(f"{_nome(c)} só aceita letras, números e . _ : / @ + - (ex.: fabricante/modelo).")


def _v_ffmpeg_dir(c, v):
    pasta = Path(v.strip('"'))
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if not (pasta / exe).is_file():
        raise ValueError(f"{_nome(c)}: não achei {exe} em {pasta}. Informe a pasta que contém o ffmpeg e o ffprobe.")


def _v_arquivo(c, v):
    if not Path(v).is_file():
        raise ValueError(f"{_nome(c)}: o arquivo {v} não existe.")


def _v_workflow(c, v):
    caminho = Path(v)
    if not caminho.is_absolute():
        caminho = _cfg().COMFY_PASTA / (v if v.endswith(".json") else f"{v}.json")
    if not caminho.is_file():
        raise ValueError(f"{_nome(c)}: não achei o workflow {caminho}. Use o nome de um arquivo de estudio/comfy "
                         "(ex.: kontext, qwen_edit) ou o caminho completo de um JSON.")


def _v_raiz(c, v):
    if not Path(v).is_absolute():
        raise ValueError(f"{_nome(c)} precisa ser um caminho completo (ex.: D:\\estudio).")


def _sugestoes_workflow() -> list[str]:
    return sorted(p.stem for p in _cfg().COMFY_PASTA.glob("*.json") if not p.stem.startswith("teste_"))


def _opcoes_movimento() -> list[tuple[str, str]]:
    from . import efeitos
    return [(e["nome"], e["nome"]) for e in efeitos.listar() if e["tipo"] == "movimento"]


def _aplicar_ffmpeg(cfg, _valor):
    cfg.reescolher_ffmpeg()
    ger = sys.modules.get("estudio.gerador")
    if ger is not None:
        ger._codificador = None   # o teste do NVENC é refeito com o FFmpeg novo


def _aplicar_raciocinio(cfg, valor):
    cfg.RACIOCINIO_TAREFAS = {t.strip() for t in str(valor).lower().split(",") if t.strip()}


def _aplicar_humanizer(cfg, valor):
    cfg.HUMANIZER_SKILL = Path(valor)


def _padrao_humanizer():
    return str(_cfg().CODIGO / "Humanizer br-v1" / "skills" / "humanizer-br" / "SKILL.md")


def _env(chave, rotulo, tipo, padrao, ajuda, **kw) -> Campo:
    return Campo(chave, rotulo, tipo, padrao, ajuda, onde="env", **kw)


def _json(chave, rotulo, tipo, ajuda, padrao=DO_CONFIG, **kw) -> Campo:
    return Campo(chave, rotulo, tipo, padrao, ajuda, onde="json", **kw)


# Campos do FORMATOS: (campo, rótulo, tipo, mínimo, máximo, passo, unidade, ajuda)
_CAMPOS_FORMATO = [
    ("max_imagens", "Quadros (imagens) por história: máximo", "inteiro", 1, 100, 1, "",
     "Quantas imagens, no máximo, uma história pode ter. O canal pode pedir menos em Imagens mín./máx."),
    ("min_cenas", "Quadros (imagens) por história: mínimo", "inteiro", 1, 100, 1, "",
     "Quantas imagens, no mínimo, uma história precisa ter."),
    ("palavras_min", "Palavras da narração: mínimo", "inteiro", 20, 10000, 10, "palavras",
     "Tamanho mínimo do texto narrado (o canal pode trocar por duração em segundos)."),
    ("palavras_max", "Palavras da narração: máximo", "inteiro", 20, 10000, 10, "palavras",
     "Tamanho máximo do texto narrado."),
    ("max_cena_s", "Segundos por cena: máximo", "numero", 2, 120, 0.5, "s",
     "Nenhuma cena fica mais tempo que isso na tela; define quantas imagens a história pede."),
    ("largura", "Largura do vídeo", "inteiro", 240, 7680, 2, "px", "Largura do vídeo final (número par)."),
    ("altura", "Altura do vídeo", "inteiro", 240, 7680, 2, "px", "Altura do vídeo final (número par)."),
    ("fps", "Quadros por segundo", "inteiro", 10, 120, 1, "fps", "Taxa de quadros do vídeo final."),
    ("crf", "Qualidade do vídeo (CRF)", "inteiro", 0, 51, 1, "",
     "Menor é melhor qualidade e arquivo maior; de 18 a 23 é o normal."),
    ("maxrate", "Teto de bitrate do vídeo", "inteiro", 1, 100, 1, "Mbps", "Limita o bitrate de pico do vídeo final."),
    ("img_largura", "Largura da imagem gerada", "inteiro", 256, 8192, 16, "px",
     "Tamanho pedido ao gerador de imagens; depois é ajustado ao quadro do vídeo."),
    ("img_altura", "Altura da imagem gerada", "inteiro", 256, 8192, 16, "px", "Idem, altura."),
    ("ficha_largura", "Largura da ficha/placa", "inteiro", 256, 4096, 16, "px",
     "Tamanho das fichas de personagem e placas de ambiente usadas como referência."),
    ("ficha_altura", "Altura da ficha/placa", "inteiro", 256, 4096, 16, "px", "Idem, altura."),
    ("movimento_padrao", "Movimento de câmera padrão", "escolha", None, None, None, "",
     "Efeito usado quando o canal não define movimento para a tensão da cena."),
]
_FMT_PARA_UI = {"maxrate": lambda v: int(str(v).rstrip("Mm"))}
_FMT_PARA_CODIGO = {"maxrate": lambda v: f"{int(v)}M"}


def _campos_formatos() -> list[Campo]:
    saida = []
    for fmt in FORMATOS_ORDEM:
        for nome, rotulo, tipo, mn, mx, passo, unidade, ajuda in _CAMPOS_FORMATO:
            validar = _v_par if nome in ("largura", "altura") else None
            saida.append(Campo(f"FORMATOS.{fmt}.{nome}", rotulo, tipo, DO_CONFIG, ajuda, formato=fmt, campo=nome,
                               minimo=mn, maximo=mx, passo=passo, unidade=unidade, validar=validar,
                               opcoes=_opcoes_movimento if tipo == "escolha" else None))
    return saida


def _montar() -> list[Grupo]:
    chaves = Grupo("acesso", "Chaves e acesso",
                   "As chaves ficam no arquivo .env deste computador, que o Git ignora. Nada aqui sai inteiro pela API.",
                   [_env("OPENROUTER_API_KEY", "Chave do OpenRouter", "segredo", "",
                         "Juiz (Jev), narração (Qwen TTS) e o texto quando o provedor é o OpenRouter.",
                         attr="OPENROUTER_API_KEY"),
                    _env("DEEPSEEK_API_KEY", "Chave do DeepSeek (oficial)", "segredo", "",
                         "Texto, quando o provedor de texto é a API oficial do DeepSeek.", attr="DEEPSEEK_API_KEY"),
                    _env("POLLINATIONS_API_KEY", "Chave do Pollinations", "segredo", "",
                         "Imagens. Use uma chave só para o programa, com limite de gasto no painel do Pollinations.",
                         attr="POLLINATIONS_API_KEY"),
                    _env("ESTUDIO_API_TOKEN", "Token da API do estúdio", "segredo", "",
                         "Protege /api/* quando ela é acessada de fora deste computador (localhost continua livre).",
                         attr="API_TOKEN")],
                   [("openrouter", "Testar a chave do OpenRouter")])

    modelos = Grupo("modelos", "Modelos de IA",
                    "Os modelos de cada etapa e como o estúdio fala com o OpenRouter.",
                    [_env("PROVEDOR_TEXTO", "Provedor do texto", "escolha", "openrouter",
                          "Quem escreve ganchos, histórias, cenas e a revisão. DeepSeek oficial usa deepseek-chat (ou "
                          "deepseek-reasoner nas etapas com raciocínio) e cobra direto da conta do DeepSeek; OpenRouter "
                          "usa o modelo abaixo.", attr="PROVEDOR_TEXTO",
                          opcoes=[("openrouter", "OpenRouter (modelo abaixo)"), ("deepseek", "DeepSeek oficial")]),
                     _env("MODELO_TEXTO", "Modelo de texto (OpenRouter)", "texto", "deepseek/deepseek-v4.1-flash",
                          "Só vale quando o provedor do texto é o OpenRouter.", attr="MODELO_TEXTO",
                          validar=_v_modelo),
                     _env("RACIOCINIO_TAREFAS", "Etapas com raciocínio (thinking)", "texto", "narrativa,reescrita",
                          "Etapas do texto que pensam antes de responder, separadas por vírgula; nenhuma = desligado, "
                          "todas = todas as etapas. Etapas: ganchos, narrativa, reescrita, decupagem, prompts_cena, humanizer, "
                          "sugestao_canal. Mais lento e com mais tokens de saída.", attr="RACIOCINIO_TAREFAS",
                          aplicar=_aplicar_raciocinio),
                     _env("MODELO_JUIZ", "Modelo do juiz", "texto", "typesafe/jev-1.13",
                          "Avalia ganchos e histórias pelas perguntas do canal.", attr="MODELO_JUIZ", validar=_v_modelo),
                     _env("MODELO_IMAGEM", "Modelo de imagem (Pollinations)", "texto", "black-forest-labs/flux.2-klein-4b",
                          "Usado quando o motor de imagem é o Pollinations.", attr="MODELO_IMAGEM", validar=_v_modelo),
                     _json("API_TIMEOUT_S", "Tempo limite por chamada", "inteiro",
                           "Texto, Jev e narração: quanto esperar uma resposta antes de tentar de novo.",
                           minimo=10, maximo=1800, passo=10, unidade="s"),
                     _json("API_TENTATIVAS", "Tentativas por chamada", "inteiro",
                           "Quantas vezes repetir uma chamada que falhou por rede, erro 5xx ou limite de uso.",
                           minimo=1, maximo=10, passo=1)])

    historia = Grupo("historia", "História e laço do Jev",
                     "Quanto o laço pode gastar e quando ele desiste. O que é por canal fica em Canais.",
                     [_env("LACO_TETO_USD", "Teto do laço da história", "numero", 0.01,
                           "Gasto máximo (ganchos, escrita, Jev e reescritas) por execução do laço; o canal pode ter o seu.",
                           attr="LACO_TETO_USD", minimo=0.001, maximo=10, passo=0.005, unidade="US$"),
                      _env("LACO_MAX_HISTORIAS", "Histórias por vídeo", "inteiro", 4,
                           "Histórias inteiras escritas do zero (mesmo gancho) até uma passar no Jev; fica a de melhor nota.",
                           attr="LACO_MAX_HISTORIAS", minimo=1, maximo=10, passo=1),
                      _env("RECOMECOS_TRAVA", "Recomeços se reprovar na trava", "inteiro", 3,
                           "Reprovada na segurança (ou noutra pergunta com trava): recomeça do gancho com outra vibe "
                           "(outro modo narrativo e outro tom) até esse número de vezes; depois para na revisão.",
                           attr="RECOMECOS_TRAVA", minimo=0, maximo=10, passo=1),
                      _json("GANCHO_MAX_RODADAS", "Rodadas de ganchos", "inteiro",
                            "Cada rodada pede 5 ganchos; se o melhor não passa, vem outra rodada vendo os anteriores.",
                            minimo=1, maximo=8, passo=1),
                      _json("FOLGA_PALAVRAS", "Folga no total de palavras", "numero",
                            "Tolerância antes de mandar reescrever só o tamanho (0,05 = 5% para mais ou para menos).",
                            minimo=0, maximo=0.5, passo=0.01),
                      _json("FOLGA_PALAVRAS_CENA", "Folga de palavras por cena", "inteiro",
                            "Palavras a mais por cena que ainda são aceitas.", minimo=0, maximo=20, passo=1),
                      _json("COERENCIA_MINIMA", "Humanizer: coerência mínima", "numero",
                            "Se a nota de coerência cair abaixo disso depois do humanizer, volta o texto aprovado.",
                            minimo=0, maximo=1, passo=0.05),
                      _json("GANCHO_FOLGA", "Humanizer: queda aceita no gancho", "numero",
                            "Pontos que a nota do gancho pode ficar abaixo do corte antes de voltar a primeira frase.",
                            minimo=0, maximo=10, passo=0.5),
                      _json("VOLTAS_VERIFICADOR", "Humanizer: voltas do verificador", "inteiro",
                            "Quantas vezes os trechos com cara de IA voltam ao humanizer (0 desliga a reescrita).",
                            minimo=0, maximo=5, passo=1)])

    formatos = Grupo("formatos", "Formatos",
                     "Short 9:16 e Longo 16:9 lado a lado: quadros (imagens) por história, tamanho do texto, resolução e "
                     "qualidade. Valem para todos os canais do formato; o canal só restringe dentro desses limites.",
                     _campos_formatos())

    imagens = Grupo("imagens", "Imagens",
                    "Gerador de imagem (ComfyUI no Colab ou API Pollinations) e como ele é chamado. Gerador, link e "
                    "workflow são relidos a cada imagem: trocar o link do túnel do Colab não exige reiniciar.",
                    [_env("MOTOR_IMAGEM", "Gerador de imagem", "escolha", "comfy",
                          "Com o link do ComfyUI respondendo, as imagens saem dele (sem custo por imagem); sem link ou "
                          "com o Colab fora, vão pela API (Pollinations, paga por imagem).",
                          opcoes=[("comfy", "ComfyUI se o link responder, senão API"),
                                  ("pollinations", "Só API (Pollinations)")]),
                     _env("COMFY_URL", "Endereço do ComfyUI", "url", "",
                          "O link do túnel muda a cada sessão do Colab. Sem ele, tudo vai pela API (Pollinations).",
                          padrao_rotulo="(não definido)"),
                     _env("COMFY_WORKFLOW", "Workflow do ComfyUI", "texto", "kontext",
                          "Nome de um arquivo de estudio/comfy: kontext (Flux Kontext, o padrão) ou qwen_edit; ou o "
                          "caminho completo de um JSON.",
                          validar=_v_workflow, sugestoes=_sugestoes_workflow),
                     _env("COMFY_MEGAPIXELS", "Megapixels no ComfyUI", "numero", 0.6,
                          "Área da imagem gerada no Comfy (a ampliação vem depois). Menos é mais rápido e leve; "
                          "perto de 1 dá mais detalhe.",
                          attr="COMFY_MEGAPIXELS", minimo=0.25, maximo=4, passo=0.05, unidade="MP"),
                     _env("COMFY_MEGAPIXELS_REF", "Megapixels de cada referência", "numero", 0.4,
                          "Tamanho com que a ficha e a placa entram no Comfy. Com 3 referências, é o que mais pesa.",
                          attr="COMFY_MEGAPIXELS_REF", minimo=0.1, maximo=2, passo=0.05, unidade="MP"),
                     _env("COMFY_TIMEOUT_S", "Tempo limite por imagem no Comfy", "inteiro", 900,
                          "Inclui a espera na fila do Comfy.", attr="COMFY_TIMEOUT_S", minimo=30, maximo=7200, passo=30,
                          unidade="s"),
                     _json("COMFY_PAUSA_S", "Pausa depois de uma queda do Comfy", "inteiro",
                           "Depois de uma queda, as imagens seguintes vão para o Pollinations por este tempo.",
                           minimo=0, maximo=3600, passo=10, unidade="s"),
                     _json("IMAGEM_TIMEOUT_S", "Tempo limite por imagem no Pollinations", "inteiro",
                           "Quanto esperar uma imagem do Pollinations.", minimo=30, maximo=1800, passo=10, unidade="s"),
                     _json("IMAGENS_SIMULTANEAS", "Imagens ao mesmo tempo", "inteiro",
                           "Cenas geradas em paralelo. Mais rápido, mas esbarra no limite de uso do provedor.",
                           minimo=1, maximo=8, passo=1)],
                    [("comfy", "Testar o ComfyUI")])

    montagem = Grupo("montagem", "Narração e montagem",
                     "Volume e codificação do vídeo de histórias. Resolução, fps e CRF do vídeo ficam em Formatos.",
                     [_json("NARRACAO_LUFS", "Volume da voz e da trilha", "numero",
                            "Voz e trilha são normalizadas para este volume antes da mistura (os níveis do canal são relativos à voz).",
                            minimo=-30, maximo=-6, passo=0.5, unidade="LUFS"),
                      _json("MONTAGEM_PRESET", "Preset do codificador (vídeo final)", "escolha",
                            "Mais lento comprime melhor, no mesmo CRF. O medium é o equilíbrio.",
                            opcoes=[(p, p) for p in PRESETS_X264]),
                      _json("MONTAGEM_AUDIO_KBPS", "Bitrate do áudio", "inteiro",
                            "AAC do vídeo final.", minimo=64, maximo=320, passo=32, unidade="kbps"),
                      _json("MONTAGEM_CRF_CLIPES", "CRF dos clipes de cada cena", "inteiro",
                            "Qualidade dos clipes intermediários, antes da passada final. Menor = melhor e mais pesado.",
                            minimo=0, maximo=40, passo=1)])

    music = Grupo("music", "Music",
                  "Parâmetros globais do gerador de playlists (aba Music). Pastas, crossfade, texto e efeitos "
                  "de cada vídeo ficam na própria aba.",
                  [_json("GERADOR_LARGURA", "Largura do vídeo", "inteiro",
                         "Resolução do vídeo da playlist. Vale depois de reiniciar o servidor.",
                         ao_vivo=False, minimo=320, maximo=3840, passo=2, unidade="px", validar=_v_par),
                   _json("GERADOR_ALTURA", "Altura do vídeo", "inteiro",
                         "Vale depois de reiniciar o servidor.",
                         ao_vivo=False, minimo=240, maximo=2160, passo=2, unidade="px", validar=_v_par),
                   _json("GERADOR_FPS", "Quadros por segundo", "inteiro",
                         "Vale depois de reiniciar o servidor.", ao_vivo=False, minimo=10, maximo=60, passo=1, unidade="fps"),
                   _json("GERADOR_ALVO_LUFS", "Volume alvo das faixas", "numero",
                         "Cada faixa é levada a este volume antes do crossfade (o YouTube usa -14).",
                         minimo=-30, maximo=-6, passo=0.5, unidade="LUFS"),
                   _json("GERADOR_GANHO_MAX_DB", "Ganho máximo por faixa", "numero",
                         "Limite do ajuste de volume de uma faixa, para cima ou para baixo.",
                         minimo=0, maximo=40, passo=1, unidade="dB"),
                   _json("GERADOR_DURACAO_PREVIA", "Duração da prévia", "numero",
                         "Janela renderizada no botão de prévia.", minimo=3, maximo=60, passo=1, unidade="s"),
                   _json("GERADOR_MARGEM_TEXTO", "Margem do nome da faixa", "inteiro",
                         "Distância entre o nome da faixa e a borda do vídeo.", minimo=0, maximo=300, passo=2, unidade="px"),
                   _json("GERADOR_FADE_TEXTO", "Fade do nome da faixa", "numero",
                         "Duração da entrada e da saída do nome da faixa.", minimo=0.1, maximo=10, passo=0.1, unidade="s"),
                   _json("GERADOR_CRF", "CRF do vídeo final (CPU)", "inteiro",
                         "libx264 na passada final. Menor = melhor qualidade e arquivo maior.",
                         minimo=0, maximo=51, passo=1),
                   _json("GERADOR_CQ_GPU", "CQ do vídeo final (GPU NVIDIA)", "inteiro",
                         "Mesma ideia do CRF, no codificador NVENC.", minimo=0, maximo=51, passo=1),
                   _json("GERADOR_PRESET", "Preset do codificador (CPU)", "escolha",
                         "No fundo com grão ou chuva quase todo o tempo vai para o codificador; veryfast é o equilíbrio.",
                         opcoes=[(p, p) for p in PRESETS_X264]),
                   _json("GERADOR_MAXRATE_MBPS", "Teto de bitrate do vídeo final", "inteiro",
                         "O buffer do codificador é o dobro disso.", minimo=1, maximo=100, passo=1, unidade="Mbps"),
                   _json("GERADOR_AUDIO_KBPS", "Bitrate do áudio", "inteiro",
                         "AAC da playlist.", minimo=64, maximo=320, passo=32, unidade="kbps"),
                   _json("GERADOR_USAR_GPU", "Usar a GPU NVIDIA quando houver", "booleano",
                         "Desligado, a renderização usa sempre a CPU (libx264)."),
                   _env("GERADOR_MANTER_TEMP", "Manter os temporários quando o render falha", "booleano", False,
                        "Guarda a pasta de trabalho do gerador depois de um erro, para investigar.")])

    publicador = Grupo("publicador", "Publicador e YouTube",
                       "Fila de postagem na rede local. A conexão com o YouTube é feita na aba Desempenho (arquivo de "
                       "cliente do Google em dados/); não há parâmetros globais dela aqui. O nome do canal no Publicador "
                       "fica na tela de cada canal.",
                       [_env("PUBLICADOR_URL", "Endereço do Publicador", "url", "http://192.168.31.157:8080",
                             "Onde o vídeo pronto é enviado (POST /api/videos).", attr="PUBLICADOR_URL"),
                        _json("PUBLICADOR_TIMEOUT_S", "Tempo limite do envio", "inteiro",
                              "Quanto esperar o upload do vídeo para o Publicador.",
                              minimo=30, maximo=7200, passo=30, unidade="s")],
                       [("publicador", "Testar o Publicador")])

    sistema = Grupo("sistema", "Sistema e custos",
                    "FFmpeg, humanizer, pasta de dados, preços de referência (estimativa quando a API não devolve o "
                    "custo) e endereços dos serviços.",
                    [_env("FFMPEG_DIR", "Pasta do FFmpeg", "caminho", "",
                          "Pasta com ffmpeg e ffprobe. Vazio: a build ARM64 de %LOCALAPPDATA%\\Programs\\ffmpeg-arm64\\bin, "
                          "se existir, senão o que estiver no PATH.", validar=_v_ffmpeg_dir, aplicar=_aplicar_ffmpeg,
                          padrao_rotulo="(automático)"),
                     _env("HUMANIZER_SKILL", "Arquivo do humanizer-br (SKILL.md)", "caminho", _padrao_humanizer,
                          "Regras e vocabulário que o humanizer usa.", validar=_v_arquivo, aplicar=_aplicar_humanizer),
                     _env("ESTUDIO_RAIZ", "Pasta de dados do estúdio", "caminho", lambda: str(_cfg().CODIGO),
                          "Onde ficam banco, projetos, biblioteca e trilhas. Mudar NÃO move nada: os dados antigos "
                          "continuam na pasta anterior. Vale depois de reiniciar o servidor.",
                          ao_vivo=False, validar=_v_raiz),
                     _env("POLLEN_USD", "Valor de 1 Pollen", "numero", 1.0,
                          "Converte o preço em Pollen do Pollinations para dólar (1 Pollen ~ US$ 1).",
                          attr="POLLEN_USD", minimo=0.01, maximo=100, passo=0.05, unidade="US$"),
                     _json("PRECO_IMAGEM_PADRAO", "Preço de referência da imagem", "numero",
                           "Por imagem do modelo de imagem, quando o catálogo não informa.",
                           minimo=0, maximo=5, passo=0.001, unidade="US$"),
                     _json("PRECO_TEXTO_ENTRADA", "Preço de referência: texto (entrada)", "numero",
                           "Por token de entrada, quando a API não devolve o custo.",
                           minimo=0, maximo=0.01, passo=0.0000001, unidade="US$/token"),
                     _json("PRECO_TEXTO_SAIDA", "Preço de referência: texto (saída)", "numero",
                           "Por token de saída.", minimo=0, maximo=0.01, passo=0.0000001, unidade="US$/token"),
                     _json("PRECO_JEV_TOKEN_ENTRADA", "Preço de referência: Jev", "numero",
                           "Por token de entrada do juiz.", minimo=0, maximo=0.01, passo=0.00000001, unidade="US$/token"),
                     _json("PRECO_TTS_POR_CARACTERE", "Preço de referência: narração", "numero",
                           "Por caractere narrado no Qwen TTS.", minimo=0, maximo=0.01, passo=0.000001, unidade="US$/caractere"),
                     _json("EST_TEXTO", "Estimativa de uma chamada de escrita", "numero",
                           "Usada pelo teto do laço até haver custo medido.", minimo=0, maximo=1, passo=0.001, unidade="US$"),
                     _json("EST_JEV", "Estimativa de uma chamada do Jev", "numero",
                           "Idem, para o juiz.", minimo=0, maximo=1, passo=0.001, unidade="US$"),
                     _json("OPENROUTER_URL", "OpenRouter: chat", "url", "Endereço do chat completions.", unidade=""),
                     _json("OPENROUTER_DECISOES_URL", "OpenRouter: Jev", "url", "Endereço da Decisions API."),
                     _json("OPENROUTER_MODELOS_URL", "OpenRouter: modelos", "url",
                           "Base da API (a verificação da chave usa o mesmo endereço, com /key)."),
                     _json("OPENROUTER_TTS_URL", "OpenRouter: narração", "url", "Endereço do text-to-speech."),
                     _json("POLLI_URL", "Pollinations: imagens", "url", "Endereço da geração de imagens."),
                     _json("POLLI_MEDIA_URL", "Pollinations: mídia", "url", "Endereço do envio de referências.")])
    return [chaves, modelos, historia, formatos, imagens, montagem, music, publicador, sistema]


GRUPOS: list[Grupo] = _montar()
CAMPOS: dict[str, Campo] = {c.chave: c for g in GRUPOS for c in g.campos}


# ---------------------------------------------------------------- valores

def _padrao(c: Campo):
    if c.padrao is DO_CONFIG:
        if c.formato:
            return _FMT_PARA_UI.get(c.campo, lambda v: v)(_PADRAO_FORMATOS[c.formato][c.campo])
        return copy.deepcopy(_PADROES_ATTR[c.chave])
    return c.padrao() if callable(c.padrao) else c.padrao


def _converter(c: Campo, bruto):
    """Valida e converte o que veio da tela (ou do arquivo) para o tipo do campo. Levanta ValueError com o campo."""
    t = c.tipo
    if t == "booleano":
        if isinstance(bruto, bool):
            return bruto
        if isinstance(bruto, str) and bruto.strip().lower() in ("1", "true", "sim", "on", "yes"):
            return True
        if isinstance(bruto, str) and bruto.strip().lower() in ("0", "false", "nao", "não", "off", "no", ""):
            return False
        raise ValueError(f"{_nome(c)} precisa ser verdadeiro ou falso.")
    if t in ("inteiro", "numero"):
        quer = "um número inteiro" if t == "inteiro" else "um número"
        if isinstance(bruto, bool):
            raise ValueError(f"{_nome(c)} precisa ser {quer}.")
        try:
            num = float(bruto.strip().replace(",", ".") if isinstance(bruto, str) else bruto)
        except (TypeError, ValueError):
            raise ValueError(f"{_nome(c)} precisa ser {quer}.") from None
        if not math.isfinite(num):
            raise ValueError(f"{_nome(c)} precisa ser {quer}.")
        if t == "inteiro":
            if num != int(num):
                raise ValueError(f"{_nome(c)} precisa ser {quer}.")
            num = int(num)
        if (c.minimo is not None and num < c.minimo) or (c.maximo is not None and num > c.maximo):
            faixa = (f"entre {c.minimo:g} e {c.maximo:g}" if c.minimo is not None and c.maximo is not None
                     else f"no mínimo {c.minimo:g}" if c.minimo is not None else f"no máximo {c.maximo:g}")
            raise ValueError(f"{_nome(c)} precisa estar {faixa}{' ' + c.unidade if c.unidade else ''}.")
        if c.validar:
            c.validar(c, num)
        return num
    if not isinstance(bruto, str):
        raise ValueError(f"{_nome(c)} precisa ser um texto.")
    v = bruto.strip()
    if not v:
        raise ValueError(f"{_nome(c)} não pode ficar vazio (para voltar ao padrão, use Restaurar padrão).")
    if re.search(r"[\x00-\x1f\x7f]", v):
        raise ValueError(f"{_nome(c)} não pode ter quebra de linha nem caracteres de controle.")
    if t == "segredo":
        if not re.fullmatch(r"[^\s'\"]{8,512}", v):
            raise ValueError(f"{_nome(c)} precisa ter de 8 a 512 caracteres, sem espaços nem aspas.")
    elif t == "url":
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc or re.search(r"\s", v) or len(v) > 500:
            raise ValueError(f"{_nome(c)} precisa ser um endereço http:// ou https:// completo.")
    elif t == "escolha":
        validos = {str(o[0]).lower(): o[0] for o in _opcoes(c)}
        if v.lower() not in validos:
            raise ValueError(f"{_nome(c)} precisa ser uma de: {', '.join(map(str, validos.values()))}.")
        v = validos[v.lower()]
    elif t == "caminho":
        v = v.strip('"')
        if len(v) > 500:
            raise ValueError(f"{_nome(c)} é longo demais.")
    elif len(v) > 300:
        raise ValueError(f"{_nome(c)} é longo demais (máximo 300 caracteres).")
    if c.validar:
        c.validar(c, v)
    return v


def _opcoes(c: Campo) -> list[tuple]:
    o = c.opcoes() if callable(c.opcoes) else c.opcoes
    return list(o or [])


def _texto_env(valor) -> str:
    if isinstance(valor, bool):
        return "1"
    if isinstance(valor, float):
        return repr(valor)
    return str(valor)


def _mascara(valor: str | None) -> dict:
    return {"definido": bool(valor), "final": valor[-4:] if valor and len(valor) >= 12 else ""}


# ---------------------------------------------------------------- .env

def _ler_env() -> dict[str, str]:
    try:
        return {k: v for k, v in dotenv_values(_cfg().ENV_ARQUIVO).items() if v is not None}
    except (OSError, UnicodeDecodeError):
        return {}


def _env_efetivo(chave: str, env: dict) -> tuple[str | None, str]:
    """(valor configurado ou None, origem). Mesma ordem do _env_vivo do config: o arquivo antes do sistema."""
    arq = (env.get(chave) or "").strip()
    if arq:
        return arq, ".env"
    sis = (_cfg().AMBIENTE_ANTES.get(chave) or "").strip()
    if sis:
        return sis, "ambiente do sistema"
    return None, "padrão"


def _linha_env(chave: str, valor: str) -> str:
    """CHAVE=valor do jeito que o python-dotenv devolve exatamente o mesmo valor (conferido de volta)."""
    candidatos = []
    if not re.search(r"[\s#'\"\\]", valor):
        candidatos.append(valor)
    if "'" not in valor:
        candidatos.append(f"'{valor}'")
    candidatos.append('"' + valor.replace("\\", "\\\\").replace('"', '\\"') + '"')
    for cand in candidatos:
        linha = f"{chave}={cand}"
        if dotenv_values(stream=io.StringIO(linha)).get(chave) == valor:
            return linha
    raise ValueError(f"Não consegui gravar o valor de {chave} no .env sem que ele mude (tem ${{...}} ou aspas demais).")


def _novo_texto_env(sets: dict[str, str], remover: list[str]) -> str | None:
    """O .env com as linhas alteradas no lugar. None se nada mudou."""
    arq = _cfg().ENV_ARQUIVO
    original = arq.read_bytes().decode("utf-8") if arq.exists() else ""
    eol = "\r\n" if "\r\n" in original else "\n"
    partes = original.split("\n")   # cada parte pode terminar em \r: fica como estava
    if partes and partes[-1] == "":
        partes.pop()
        termina_em_quebra = True
    else:
        termina_em_quebra = not partes

    def casa(chave, comentada):
        prefixo = r"#\s*" if comentada else r"(?:export\s+)?"
        pad = r"^\ufeff?\s*" + prefixo + re.escape(chave) + r"\s*="
        return [i for i, ln in enumerate(partes) if re.match(pad, ln)]

    def cauda_cr(i):
        return "\r" if partes[i].endswith("\r") else ""

    for chave in remover:
        for i in reversed(casa(chave, False)):
            del partes[i]
    for chave, valor in sets.items():
        linha = _linha_env(chave, valor)
        ativas = casa(chave, False)
        if ativas:
            partes[ativas[-1]] = linha + cauda_cr(ativas[-1])
            continue
        comentadas = casa(chave, True)   # o exemplo comentado vira a linha de verdade, no mesmo lugar
        if comentadas:
            partes[comentadas[0]] = linha + cauda_cr(comentadas[0])
        else:
            partes.append(linha + ("\r" if eol == "\r\n" else ""))
    novo = "\n".join(partes) + ("\n" if termina_em_quebra and partes else "")
    return None if novo == original else novo


def _escrever_atomico(destino: Path, texto: str):
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_name(destino.name + ".tmp")
    tmp.write_bytes(texto.encode("utf-8"))
    os.replace(tmp, destino)


def _gravar_env_texto(texto: str):
    global _backup_feito
    arq = _cfg().ENV_ARQUIVO
    if not _backup_feito and arq.exists():
        shutil.copy2(arq, arq.with_name(arq.name + ".bak"))
    _backup_feito = True
    _escrever_atomico(arq, texto)


# ---------------------------------------------------------------- configuracoes.json

def _caminho_json() -> Path:
    return _cfg().DADOS / ARQUIVO_JSON


def _achatar(dados: dict) -> dict:
    plano = {}
    for k, v in dados.items():
        if isinstance(v, dict):
            for k2, v2 in _achatar(v).items():
                plano[f"{k}.{k2}"] = v2
        else:
            plano[k] = v
    return plano


def _aninhar(plano: dict) -> dict:
    saida: dict = {}
    for chave in sorted(plano):
        *caminho, folha = chave.split(".")
        d = saida
        for p in caminho:
            d = d.setdefault(p, {})
        d[folha] = plano[chave]
    return saida


def _ler_json() -> tuple[dict, list[str]]:
    """Valores do arquivo, já validados. Entradas inválidas ou desconhecidas são descartadas com um aviso."""
    caminho = _caminho_json()
    if not caminho.exists():
        return {}, []
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            raise ValueError("o arquivo precisa ser um objeto JSON")
    except (OSError, ValueError) as e:
        return {}, [f"{ARQUIVO_JSON} ilegível, valores ignorados: {e}"]
    plano, avisos = {}, []
    for chave, bruto in _achatar(dados).items():
        c = CAMPOS.get(chave)
        if c is None or c.onde != "json":
            avisos.append(f"{ARQUIVO_JSON}: campo desconhecido ignorado ({chave})")
            continue
        try:
            plano[chave] = _converter(c, bruto)
        except ValueError as e:
            avisos.append(f"{ARQUIVO_JSON}: {e} Valor ignorado.")
    return plano, avisos


def _definir_json(c: Campo, efetivo):
    cfg = _cfg()
    if c.formato:
        cfg.FORMATOS[c.formato][c.campo] = _FMT_PARA_CODIGO.get(c.campo, lambda v: v)(efetivo)   # no lugar: quem guardou a referência vê
    else:
        setattr(cfg, c.attr or c.chave, efetivo)


def carregar_inicial():
    """Chamado pelo fim do config.py: guarda os padrões e aplica o configuracoes.json por cima."""
    global _PADRAO_FORMATOS
    cfg = _cfg()
    _PADRAO_FORMATOS = copy.deepcopy(cfg.FORMATOS)
    for c in CAMPOS.values():
        if c.onde == "json" and not c.formato and c.padrao is DO_CONFIG:
            _PADROES_ATTR[c.chave] = copy.deepcopy(getattr(cfg, c.attr or c.chave))
    plano, avisos = _ler_json()
    AVISOS[:] = avisos
    for chave, valor in plano.items():
        _definir_json(CAMPOS[chave], valor)   # na partida vale também o que é "só depois de reiniciar"


# ---------------------------------------------------------------- salvar e restaurar

def _checar_cruzado(plano: dict):
    def efetivo(chave):
        return plano.get(chave, _padrao(CAMPOS[chave]))
    for fmt in FORMATOS_ORDEM:
        nome = _cfg().FORMATOS[fmt]["nome"]
        for menor, maior in (("min_cenas", "max_imagens"), ("palavras_min", "palavras_max")):
            a, b = efetivo(f"FORMATOS.{fmt}.{menor}"), efetivo(f"FORMATOS.{fmt}.{maior}")
            if a > b:
                ca, cb = CAMPOS[f"FORMATOS.{fmt}.{menor}"], CAMPOS[f"FORMATOS.{fmt}.{maior}"]
                raise ValueError(f"{nome}: {_nome(ca)} ({a:g}) não pode passar de {_nome(cb)} ({b:g}).")


def _aplicar_env(c: Campo):
    """Põe o valor do .env no ambiente do processo e, se o campo vale ao vivo, no atributo do config."""
    cfg = _cfg()
    env = _ler_env()
    bruto, _ = _env_efetivo(c.chave, env)
    if bruto is not None:
        os.environ[c.chave] = bruto
    elif c.chave in cfg.AMBIENTE_ANTES:
        os.environ[c.chave] = cfg.AMBIENTE_ANTES[c.chave]
    else:
        os.environ.pop(c.chave, None)
    if not c.ao_vivo:
        return
    try:
        valor = _padrao(c) if bruto is None else _converter(c, bruto)
    except ValueError:
        return   # valor torto no .env: o programa segue com o que já usava
    if c.aplicar:
        c.aplicar(cfg, valor)
    elif c.attr:
        setattr(cfg, c.attr, valor)


def _gravar(sets: dict, resets: list[str]) -> dict:
    with _trava:
        plano, _ = _ler_json()
        novo = dict(plano)
        for k in resets:
            if CAMPOS[k].onde == "json":
                novo.pop(k, None)
        for k, v in sets.items():
            c = CAMPOS[k]
            if c.onde != "json":
                continue
            if v == _padrao(c):   # o json guarda só o que difere do padrão
                novo.pop(k, None)
            else:
                novo[k] = v
        _checar_cruzado(novo)
        env_sets = {k: _texto_env(v) for k, v in sets.items() if CAMPOS[k].onde == "env"}
        env_resets = [k for k in resets if CAMPOS[k].onde == "env"]
        texto_env = _novo_texto_env(env_sets, env_resets) if (env_sets or env_resets) else None
        if texto_env is not None:
            _gravar_env_texto(texto_env)
        if novo != plano:
            if novo:
                _escrever_atomico(_caminho_json(), json.dumps(_aninhar(novo), ensure_ascii=False, indent=2) + "\n")
            else:
                _caminho_json().unlink(missing_ok=True)
        ao_vivo, reiniciar = [], []
        for k in [*sets, *resets]:
            c = CAMPOS[k]
            if c.onde == "env":
                _aplicar_env(c)
            elif c.ao_vivo:
                _definir_json(c, novo.get(k, _padrao(c)))
            (ao_vivo if c.ao_vivo else reiniciar).append(k)
        return {"salvos": list(sets), "restaurados": list(resets), "ao_vivo": ao_vivo, "reiniciar": reiniciar}


def salvar(alteracoes: dict) -> dict:
    """Recebe {chave: valor} só dos campos alterados. Tudo ou nada: um valor inválido cancela o lote (ValueError).
    Vazio = volta ao padrão (remove do .env ou do json), menos nos segredos, onde vazio = manter."""
    if not isinstance(alteracoes, dict):
        raise ValueError("O corpo precisa ser um objeto {chave: valor}.")
    sets, resets, mantidos = {}, [], []
    for chave, bruto in alteracoes.items():
        c = CAMPOS.get(chave)
        if c is None:
            raise ValueError(f"Campo desconhecido: {chave}")
        vazio = bruto is None or (isinstance(bruto, str) and not bruto.strip())
        if c.tipo == "segredo":
            if vazio:
                mantidos.append(chave)
            else:
                sets[chave] = _converter(c, bruto)
        elif c.tipo == "booleano" and c.onde == "env":   # ausente = falso
            if _converter(c, False if vazio else bruto):
                sets[chave] = True
            else:
                resets.append(chave)
        elif vazio:
            resets.append(chave)
        else:
            sets[chave] = _converter(c, bruto)
    resultado = _gravar(sets, resets)
    resultado["mantidos"] = mantidos
    return resultado


def restaurar(chaves: list[str]) -> dict:
    """Volta ao padrão. É também o "Apagar" dos segredos."""
    desconhecidas = [k for k in chaves if k not in CAMPOS]
    if desconhecidas:
        raise ValueError(f"Campo desconhecido: {', '.join(desconhecidas)}")
    resultado = _gravar({}, list(dict.fromkeys(chaves)))
    resultado["mantidos"] = []
    return resultado


# ---------------------------------------------------------------- leitura para a tela

def _para_ui_bruto(c: Campo, bruto: str):
    try:
        return _converter(c, bruto)
    except ValueError:
        return bruto   # a tela mostra o que está no arquivo, mesmo torto


def _serializar(c: Campo, env: dict, plano_json: dict) -> dict:
    cfg = _cfg()
    aviso = ""
    if c.onde == "env":
        bruto, origem = _env_efetivo(c.chave, env)
        if c.tipo == "segredo":
            valor = _mascara(bruto)
        elif c.tipo == "booleano":
            valor = bool(_para_ui_bruto(c, bruto)) if bruto is not None else bool(_padrao(c))
        else:
            valor = None if bruto is None else _para_ui_bruto(c, bruto)
        sis = (cfg.AMBIENTE_ANTES.get(c.chave) or "").strip()
        if origem == ".env" and sis and sis != bruto:
            aviso = "Também definida no ambiente do sistema: ao reiniciar, o valor do sistema vence o do .env."
        efetivo = bruto
    else:
        if c.chave in plano_json:
            origem, valor = "configuracoes.json", plano_json[c.chave]
        else:
            origem, valor = "padrão", None
        if c.tipo == "booleano" and valor is None:
            valor = bool(_padrao(c))
        efetivo = plano_json.get(c.chave)
    pendente = False
    if not c.ao_vivo:
        if c.chave == "ESTUDIO_RAIZ":
            em_uso = str(cfg.BASE)
            esperado = efetivo if efetivo is not None else _padrao(c)
            pendente = Path(esperado).resolve() != Path(em_uso).resolve()
        else:
            em_uso = getattr(cfg, c.attr or c.chave, None)
            esperado = plano_json.get(c.chave, _padrao(c))
            pendente = em_uso != esperado
    opcoes = [{"valor": v, "rotulo": r} for v, r in _opcoes(c)] if c.tipo == "escolha" else None
    sugestoes = c.sugestoes() if callable(c.sugestoes) else c.sugestoes
    return {"chave": c.chave, "rotulo": c.rotulo, "tipo": c.tipo, "ajuda": c.ajuda, "onde": c.onde,
            "ao_vivo": c.ao_vivo, "minimo": c.minimo, "maximo": c.maximo, "passo": c.passo, "unidade": c.unidade,
            "opcoes": opcoes, "sugestoes": sugestoes, "formato": c.formato, "campo": c.campo,
            "padrao": None if c.tipo == "segredo" else _padrao(c), "padrao_rotulo": c.padrao_rotulo,
            "valor": valor, "origem": origem, "pendente_reinicio": pendente, "aviso": aviso}


def estado() -> dict:
    cfg = _cfg()
    env = _ler_env()
    plano, avisos_json = _ler_json()
    return {"grupos": [{"id": g.id, "titulo": g.titulo, "descricao": g.descricao,
                        "testes": [{"servico": s, "rotulo": r} for s, r in g.testes],
                        "campos": [_serializar(c, env, plano) for c in g.campos]} for g in GRUPOS],
            "formatos": {k: v["nome"] for k, v in cfg.FORMATOS.items()},
            "env_arquivo": str(cfg.ENV_ARQUIVO), "json_arquivo": str(_caminho_json()),
            "avisos": avisos_json}


# ---------------------------------------------------------------- testes de conexão (nada que gaste créditos)

def testar(servico: str) -> dict:
    cfg = _cfg()
    if servico == "openrouter":
        if cfg.SIMULACAO:
            return {"ok": True, "mensagem": "Modo simulação: nada foi enviado ao OpenRouter."}
        if not cfg.OPENROUTER_API_KEY:
            return {"ok": False, "mensagem": "A chave do OpenRouter não está definida."}
        url = cfg.OPENROUTER_MODELOS_URL.rsplit("/", 1)[0] + "/key"
        try:
            r = httpx.get(url, headers={"Authorization": f"Bearer {cfg.OPENROUTER_API_KEY}"}, timeout=15)
        except httpx.HTTPError as e:
            return {"ok": False, "mensagem": f"O OpenRouter não respondeu ({type(e).__name__})."}
        if r.status_code in (401, 403):
            return {"ok": False, "mensagem": f"O OpenRouter recusou a chave (HTTP {r.status_code})."}
        if r.status_code != 200:
            return {"ok": False, "mensagem": f"O OpenRouter respondeu HTTP {r.status_code}."}
        dados = (r.json() or {}).get("data") or {}
        restante = dados.get("limit_remaining")
        return {"ok": True, "mensagem": "Chave aceita pelo OpenRouter" +
                (f"; limite restante US$ {float(restante):.2f}." if isinstance(restante, (int, float)) else
                 "; sem limite de gasto definido nela." if dados.get("limit") is None else ".")}
    if servico == "comfy":
        from . import comfy
        url = cfg.comfy_url()
        if not url:
            return {"ok": False, "mensagem": "O endereço do ComfyUI não está definido."}
        if comfy.no_ar():
            from . import clientes
            clientes.marcar_comfy_no_ar()
            if cfg.motor_imagem() != "comfy":
                return {"ok": True, "mensagem": f"O ComfyUI respondeu em {url}, mas o gerador está em \"Só API "
                                                "(Pollinations)\": as imagens vão pela API."}
            return {"ok": True, "mensagem": f"O ComfyUI respondeu em {url}: as imagens vão para ele."}
        return {"ok": False, "mensagem": f"O ComfyUI não respondeu em {url} (túnel caído ou Colab desligado): "
                                         "as imagens vão pela API (Pollinations)."}
    if servico == "publicador":
        base = cfg.PUBLICADOR_URL.rstrip("/")
        try:
            r = httpx.get(base, timeout=8)
        except httpx.HTTPError as e:
            return {"ok": False, "mensagem": f"O Publicador não respondeu em {base} ({type(e).__name__})."}
        return {"ok": r.status_code < 500, "mensagem": f"O Publicador respondeu HTTP {r.status_code} em {base}."}
    raise ValueError(f"Serviço desconhecido: {servico}")
