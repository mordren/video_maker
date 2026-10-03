"""Configurações do Cortador, editáveis na aba Configurações da página única.

O esquema sai no mesmo formato da aba Configurações do Estúdio de Histórias
(historias/estudio/configuracoes.py), para a tela desenhar os dois lados com o
mesmo código. As chaves vão para a tela com o prefixo "c:".

Onde cada valor fica (a parte antes do primeiro ponto da chave):
- fase1.*   fase1/config.yaml (seleção dos trechos, nota, vídeo longo)
- cortes.*  fase1/config_cortes.yaml (Fase 2: silêncio, abertura, transição)
- mod.*     constante de um módulo do acabamento (legenda, fotos, áudio, ritmo,
            moldura) ou do próprio Estúdio
- precos.*  preços usados para estimar o custo (fase1/custos_api.py)
  ...todos esses só guardam o que difere do padrão, em
  $ESTUDIO_DATA/config_cortador.json. Os YAML do repositório continuam sendo o
  padrão: uma implantação nova do código não apaga o que foi mudado aqui.
- env.*     chaves em fase1/.env (editado no lugar, linha por linha)
- email.*   PUBLICADOR_EMAIL_* em publicador.env (avisos por e-mail)
- estudio.* config.json do Estúdio (endereço do Publicador, sons de transição)
- crop.*    fase1/config_crop.yaml — SÓ LEITURA: o crop LR-ASD não se mexe
            (decisão de 28/09/2026)

O Estúdio aplica fase1.* e cortes.* a cada trabalho (cópia mesclada do YAML na
pasta do trabalho) e mod.* direto nos módulos, na hora — nada disso precisa
reiniciar, menos o que a tela marca.
"""

from __future__ import annotations

import copy
import importlib
import json
import math
import os
import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
FASE1 = RAIZ / "fase1"
ARQ_ENV = FASE1 / ".env"
PREFIXO = "c:"
_trava = threading.RLock()
_DATA: Path | None = None
_EMAIL_ENV: Path | None = None
_PADRAO_MOD: dict[str, object] = {}
_backup_env: set[Path] = set()


@dataclass
class Campo:
    chave: str
    rotulo: str
    tipo: str             # segredo | texto | url | caminho | inteiro | numero | escolha | booleano | leitura
    ajuda: str = ""
    minimo: float | None = None
    maximo: float | None = None
    passo: float | None = None
    unidade: str = ""
    opcoes: list | None = None
    ao_vivo: bool = True
    escala: float = 1.0   # valor na tela = valor no código / escala (ex.: segundos -> dias)


@dataclass
class Grupo:
    id: str
    titulo: str
    descricao: str
    campos: list
    testes: list


def _f(chave, rotulo, tipo, ajuda="", **kw) -> Campo:
    return Campo(chave, rotulo, tipo, ajuda, **kw)


_WHISPER = [(m, m) for m in ("tiny", "base", "small", "medium", "large-v3", "turbo")]
_XFADE = [(t, t) for t in ("fade", "fadeblack", "fadewhite", "dissolve", "distance", "wipeleft", "wiperight",
                           "slideup", "slidedown", "smoothleft", "circlecrop", "radial", "pixelize")]


def _montar() -> list[Grupo]:
    acesso = Grupo("c_acesso", "Chaves", "Chaves que o Cortador usa (fase1/.env no servidor, fora do Git). "
                   "OpenRouter: JEV, Whisper pela API e visão das fotos. DeepSeek: escolha dos trechos, vídeo "
                   "longo, títulos e revisão da legenda.", [
        _f("env.OPENROUTER_API_KEY", "Chave do OpenRouter (cortes)", "segredo",
           "JEV, Whisper pela API, nota de visão das fotos."),
        _f("env.DEEPSEEK_API_KEY", "Chave do DeepSeek (cortes)", "segredo",
           "Segmentação (Fase 1), vídeo longo, título e revisão da legenda."),
    ], [("openrouter", "Testar o OpenRouter"), ("deepseek", "Testar o DeepSeek")])

    selecao = Grupo("c_selecao", "Seleção dos trechos", "Fase 1: o DeepSeek lê a transcrição e aponta os "
                    "candidatos; o JEV dá a nota e afina as bordas. Vale para o próximo trabalho.", [
        _f("fase1.bloco.duracao_minima", "Duração mínima do corte", "numero",
           "O prompt pede 30–50 s; a margem cobre o arredondamento na borda (antes de 29/09 era 55).",
           minimo=5, maximo=600, passo=1, unidade="s"),
        _f("fase1.bloco.duracao_maxima", "Duração máxima do corte", "numero",
           "Acima de 70 s nenhum Short do info passou de ~1.000 views (análise de 29/09).",
           minimo=10, maximo=900, passo=1, unidade="s"),
        _f("fase1.segmentacao.modelo", "Modelo da segmentação (DeepSeek)", "texto",
           "Lê a transcrição inteira e aponta os trechos."),
        _f("fase1.segmentacao.contexto_seg", "Contexto extra em cada borda", "numero",
           "Só importa quando o vídeo é dividido em pedaços.", minimo=0, maximo=600, passo=5, unidade="s"),
        _f("fase1.segmentacao.timeout", "Tempo limite da segmentação", "inteiro", "", minimo=10, maximo=1800,
           passo=10, unidade="s"),
        _f("fase1.segmentacao.tentativas", "Tentativas da segmentação", "inteiro", "", minimo=1, maximo=10),
        _f("fase1.jev.modelo", "Modelo do JEV", "texto", "Qualifica cada candidato (viral, ritmo, abertura)."),
        _f("fase1.jev.limiar_precisa_melhora", "JEV: limiar para ajustar bordas", "numero",
           "Acima disso os limites sugeridos pelo JEV são aplicados.", minimo=0, maximo=1, passo=0.05),
        _f("fase1.jev.opcoes_limite", "JEV: segmentos candidatos a nova borda", "inteiro",
           "Quantos segmentos do começo/fim podem virar o novo limite.", minimo=1, maximo=20),
        _f("fase1.jev.paralelo", "JEV: chamadas em paralelo", "inteiro", "", minimo=1, maximo=32),
        _f("fase1.jev.timeout", "JEV: tempo limite", "inteiro", "", minimo=5, maximo=600, unidade="s"),
        _f("fase1.jev.tentativas", "JEV: tentativas", "inteiro", "", minimo=1, maximo=10),
        _f("fase1.llm.modelo", "Modelo da revisão fina", "texto", "Ajusta os limites dos blocos que sobraram."),
        _f("fase1.llm.contexto_segundos", "Revisão fina: fala antes/depois", "numero", "", minimo=0, maximo=120,
           passo=1, unidade="s"),
        _f("fase1.llm.paralelo", "Revisão fina: em paralelo", "inteiro", "", minimo=1, maximo=16),
        _f("fase1.llm.timeout", "Revisão fina: tempo limite", "inteiro", "", minimo=10, maximo=1800, unidade="s"),
        _f("fase1.llm.tentativas", "Revisão fina: tentativas", "inteiro", "", minimo=1, maximo=10),
        _f("fase1.consolidacao.sobreposicao_maxima", "Sobreposição máxima entre cortes", "numero",
           "Acima disso dois blocos contam como o mesmo.", minimo=0, maximo=1, passo=0.05),
        _f("fase1.transcricao.whisper_modelo", "Whisper local (reserva)", "escolha",
           "Usado só quando não há legenda do YouTube e a API falha.", opcoes=_WHISPER),
        _f("fase1.transcricao.whisper_idioma", "Idioma do Whisper local", "texto", ""),
    ], [])

    nota = Grupo("c_nota", "Nota final", "Como os candidatos são ordenados. Os pesos somam 1. Desde 29/09: "
                 "viral + abertura + duração + ritmo; o LLM ficou sem peso (dava 0,67 em quase tudo).", [
        _f("fase1.ranking.peso_viral", "Peso: viral", "numero", "", minimo=0, maximo=1, passo=0.05),
        _f("fase1.ranking.peso_abertura", "Peso: abertura", "numero",
           "Os primeiros 5–10 s prendem? (a maior queda da retenção é aí)", minimo=0, maximo=1, passo=0.05),
        _f("fase1.ranking.peso_duracao", "Peso: duração", "numero", "", minimo=0, maximo=1, passo=0.05),
        _f("fase1.ranking.peso_ritmo", "Peso: ritmo", "numero", "", minimo=0, maximo=1, passo=0.05),
        _f("fase1.ranking.peso_llm", "Peso: LLM", "numero", "", minimo=0, maximo=1, passo=0.05),
        _f("fase1.ranking.duracao_ideal", "Duração ideal", "numero", "Nota de duração 1 até aqui.",
           minimo=5, maximo=600, passo=1, unidade="s"),
        _f("fase1.ranking.duracao_zero", "Duração com nota zero", "numero", "Cai em linha reta até 0 aqui.",
           minimo=10, maximo=900, passo=1, unidade="s"),
    ], [])

    corte = Grupo("c_corte", "Corte (Fase 2)", "Só roda no corte que você manda produzir: silêncio, "
                  "recomeço de frase e volume. Vale para a próxima produção.", [
        _f("cortes.silencio.noise_db", "Silêncio abaixo de", "numero", "", minimo=-80, maximo=-5, passo=1,
           unidade="dB"),
        _f("cortes.silencio.duracao_minima", "Pausa mínima para contar", "numero", "", minimo=0.05, maximo=5,
           passo=0.05, unidade="s"),
        _f("cortes.silencio.limiar_corte", "Pausa encolhida a partir de", "numero", "", minimo=0.1, maximo=10,
           passo=0.05, unidade="s"),
        _f("cortes.silencio.duracao_alvo", "Pausa encolhida para", "numero", "Compacto (decisão de 28/09).",
           minimo=0.05, maximo=3, passo=0.05, unidade="s"),
        _f("cortes.silencio.margem_seguranca", "Margem até a palavra", "numero", "", minimo=0, maximo=1,
           passo=0.01, unidade="s"),
        _f("cortes.silencio.respiro.ativo", "Respiro de vez em quando", "booleano",
           "Só em pausa que já era longa, fora do gancho e espaçada."),
        _f("cortes.silencio.respiro.duracao", "Respiro: duração", "numero", "", minimo=0.1, maximo=3,
           passo=0.05, unidade="s"),
        _f("cortes.silencio.respiro.pausa_minima", "Respiro: pausa original mínima", "numero", "", minimo=0.2,
           maximo=10, passo=0.1, unidade="s"),
        _f("cortes.silencio.respiro.depois_de", "Respiro: nunca antes de", "numero", "Nada de respiro no gancho.",
           minimo=0, maximo=60, passo=0.5, unidade="s"),
        _f("cortes.silencio.respiro.intervalo", "Respiro: no máximo um a cada", "numero", "", minimo=1,
           maximo=120, passo=1, unidade="s"),
        _f("cortes.bordas.max_silencio_borda", "Silêncio máximo no começo/fim", "numero", "", minimo=0,
           maximo=3, passo=0.05, unidade="s"),
        _f("cortes.recomeco.ativo", "Cortar recomeço de frase", "booleano", "Heurística conservadora."),
        _f("cortes.recomeco.min_palavras", "Recomeço: palavras mínimas repetidas", "inteiro", "", minimo=1,
           maximo=10),
        _f("cortes.recomeco.max_palavras", "Recomeço: palavras máximas", "inteiro", "", minimo=1, maximo=30),
        _f("cortes.recomeco.gap_minimo", "Recomeço: pausa mínima", "numero",
           "Sem ela, ênfase retórica vira hesitação.", minimo=0, maximo=2, passo=0.01, unidade="s"),
        _f("cortes.recomeco.gap_maximo", "Recomeço: pausa máxima", "numero", "", minimo=0, maximo=5, passo=0.05,
           unidade="s"),
        _f("cortes.loudness.alvo_lufs", "Volume (loudnorm)", "numero", "", minimo=-30, maximo=-6, passo=0.5,
           unidade="LUFS"),
        _f("cortes.loudness.true_peak", "Pico máximo (loudnorm)", "numero", "", minimo=-9, maximo=0, passo=0.1,
           unidade="dBTP"),
        _f("cortes.loudness.faixa_lra", "Faixa dinâmica (LRA)", "numero", "", minimo=1, maximo=30, passo=0.5),
        _f("cortes.whisper.modelo", "Whisper local da Fase 2 (reserva)", "escolha",
           "A Fase 2 usa a API; o local é só se ela falhar.", opcoes=_WHISPER),
        _f("cortes.saida.crossfade_ms", "Crossfade de áudio nas emendas", "inteiro", "Evita o clique.",
           minimo=0, maximo=500, unidade="ms"),
    ], [])

    abertura = Grupo("c_abertura", "Abertura e transição", "O pico do corte em preto e branco antes do corte "
                     "principal, escolhido pelo JEV, e a transição entre os dois.", [
        _f("cortes.abertura.ativo", "Abertura ligada", "booleano", ""),
        _f("cortes.abertura.duracao_minima", "Abertura: duração mínima", "numero", "", minimo=0.5, maximo=30,
           passo=0.1, unidade="s"),
        _f("cortes.abertura.duracao_maxima", "Abertura: duração máxima", "numero", "", minimo=1, maximo=30,
           passo=0.1, unidade="s"),
        _f("cortes.abertura.max_candidatos", "Abertura: candidatos para o JEV", "inteiro", "", minimo=2,
           maximo=60),
        _f("cortes.abertura.margem_inicio", "Abertura: não pegar antes de", "numero",
           "Senão a abertura repete o começo do corte.", minimo=0, maximo=60, passo=0.5, unidade="s"),
        _f("cortes.abertura.escala_de_cinza", "Abertura em preto e branco", "booleano", ""),
        _f("cortes.jev.modelo", "Modelo do JEV (abertura)", "texto", ""),
        _f("cortes.jev.timeout", "JEV da abertura: tempo limite", "inteiro", "", minimo=5, maximo=600,
           unidade="s"),
        _f("cortes.jev.tentativas", "JEV da abertura: tentativas", "inteiro", "", minimo=1, maximo=10),
        _f("cortes.transicao.ativo", "Som de transição", "booleano", "Efeito sorteado da pasta de sons."),
        _f("estudio.pasta_transicoes", "Pasta dos sons de transição", "caminho", "No servidor."),
        _f("cortes.transicao.fator_slow", "Câmera lenta no fim da abertura", "numero",
           "Limite técnico de 2x (atempo).", minimo=1, maximo=2, passo=0.1, unidade="x"),
        _f("cortes.transicao.volume_whoosh", "Volume do efeito", "numero", "", minimo=0, maximo=4, passo=0.05),
        _f("cortes.transicao.volume_voz_no_slow", "Voz durante o efeito", "numero", "", minimo=0, maximo=1,
           passo=0.05),
        _f("cortes.transicao.duracao_fade", "Crossfade abertura → corte", "numero", "", minimo=0, maximo=3,
           passo=0.05, unidade="s"),
        _f("cortes.transicao.tipo_fade", "Tipo da transição (xfade)", "escolha", "", opcoes=_XFADE),
    ], [])

    legenda = Grupo("c_legenda", "Legenda", "Estilo \"new\" (glow no gancho + karaokê com caixa amarela), "
                    "aprovado em 28/09. Vale na próxima produção.", [
        _f("mod.estudio.LEGENDA_PADRAO", "Legenda padrão das propostas", "escolha",
           "Pré-seleção do combo de legenda (cada canal pode ter a sua em Canais).",
           opcoes=[("new", "new (glow + karaokê)"), ("old", "old (amarela)")]),
        _f("mod.legenda_nova.MAX_PALAVRAS", "Palavras por frase na tela", "inteiro", "\"É um short\": frase curta.",
           minimo=1, maximo=8),
        _f("mod.legenda_nova.MAX_LETRAS", "Letras por frase na tela", "inteiro", "Cabe numa linha na fonte 64.",
           minimo=6, maximo=40),
        _f("mod.legenda_nova.PAUSA_QUEBRA", "Pausa que quebra a frase", "numero", "", minimo=0.1, maximo=2,
           passo=0.05, unidade="s"),
        _f("mod.legenda_nova.TAMANHO", "Tamanho da letra", "inteiro", "84 foi rejeitado (grande demais).",
           minimo=30, maximo=120),
        _f("mod.legenda_nova.FOLGA_CAIXA", "Folga da caixa amarela", "inteiro", "", minimo=0, maximo=30,
           unidade="px"),
        _f("mod.legenda_nova.GLOW_PALAVRAS", "Gancho: palavras por linha", "inteiro", "", minimo=1, maximo=10),
        _f("mod.legenda_nova.GLOW_NORMAL", "Gancho: tamanho da letra", "inteiro", "", minimo=30, maximo=140),
        _f("mod.legenda_nova.GLOW_DESTAQUE", "Gancho: tamanho do destaque", "inteiro", "", minimo=30, maximo=180),
    ], [])

    fotos = Grupo("c_fotos", "Fotos do assunto", "Formato \"imagens\": fotos do Wikimedia Commons conferidas "
                  "pelo rosto (SFace) e pela visão; trocam a cada ~5 s.", [
        _f("mod.imagens.MODELO_VISAO", "Modelo de visão (nota da foto)", "texto",
           "~US$ 0,00003 por foto com o Gemini Flash-Lite."),
        _f("mod.imagens.NOTA_MIN", "Nota mínima da visão", "numero", "", minimo=0, maximo=10, passo=0.5),
        _f("mod.imagens.TROCA_ALVO", "Segundos por foto", "numero", "", minimo=1, maximo=30, passo=0.5,
           unidade="s"),
        _f("mod.imagens.TROCA_MIN", "Segundos mínimos por foto", "numero", "", minimo=0.5, maximo=30, passo=0.5,
           unidade="s"),
        _f("mod.imagens.MENCAO_MIN", "Citação troca a foto depois de", "numero", "", minimo=0, maximo=30,
           passo=0.5, unidade="s"),
        _f("mod.imagens.FADE", "Fade entre fotos", "numero", "", minimo=0, maximo=3, passo=0.05, unidade="s"),
        _f("mod.imagens.SIMILARIDADE_MIN", "Rosto: semelhança mínima (SFace)", "numero",
           "Limiar oficial 0,363; aqui mais exigente.", minimo=0.2, maximo=0.9, passo=0.01),
        _f("mod.imagens.ROSTO_MIN", "Rosto: tamanho mínimo", "inteiro", "", minimo=20, maximo=600, unidade="px"),
        _f("mod.imagens.MAX_CANDIDATAS", "Fotos baixadas por verbete", "inteiro", "", minimo=5, maximo=300),
        _f("mod.imagens.CACHE_VALIDADE", "Refazer a lista de fotos a cada", "numero", "", minimo=0.1, maximo=365,
           passo=1, unidade="dias", escala=86400),
    ], [])

    audio = Grupo("c_audio", "Áudio e ritmo", "Conferência do volume do corte pronto (audio_qa) e o "
                  "relatório de ritmo do card de revisão.", [
        _f("mod.audio_qa.PADRAO.audio_qa_enabled", "Conferir o volume do corte pronto", "booleano", ""),
        _f("mod.audio_qa.PADRAO.audio_qa_auto_fix", "Corrigir sozinho (só o áudio)", "booleano", ""),
        _f("mod.audio_qa.PADRAO.audio_lufs_target", "Alvo da correção", "numero", "", minimo=-30, maximo=-6,
           passo=0.5, unidade="LUFS"),
        _f("mod.audio_qa.PADRAO.audio_lufs_min", "Volume mínimo aceito", "numero", "", minimo=-30, maximo=-6,
           passo=0.5, unidade="LUFS"),
        _f("mod.audio_qa.PADRAO.audio_lufs_max", "Volume máximo aceito", "numero", "", minimo=-30, maximo=-6,
           passo=0.5, unidade="LUFS"),
        _f("mod.audio_qa.PADRAO.audio_true_peak_max", "Pico máximo aceito", "numero", "", minimo=-9, maximo=0,
           passo=0.1, unidade="dBTP"),
        _f("mod.audio_qa.PADRAO.audio_true_peak_fix", "Pico alvo da correção", "numero", "", minimo=-9, maximo=0,
           passo=0.1, unidade="dBTP"),
        _f("mod.audio_qa.PADRAO.audio_lra_max", "LRA máximo aceito", "numero", "", minimo=1, maximo=30,
           passo=0.5),
        _f("mod.audio_qa.PADRAO.audio_lra_fix", "LRA alvo da correção", "numero", "", minimo=1, maximo=30,
           passo=0.5),
        _f("mod.audio_qa.PADRAO.audio_mix_delta_max", "Quanto a trilha pode somar à fala", "numero", "",
           minimo=0, maximo=10, passo=0.1, unidade="LU"),
        _f("mod.ritmo.PADRAO.pattern_gap_max_seconds", "Ritmo: tela parada a partir de", "numero",
           "Uma mudança a cada ~7 s no máximo (sua regra).", minimo=1, maximo=60, passo=0.5, unidade="s"),
        _f("mod.ritmo.PADRAO.visual_change_min_gap", "Ritmo: mudança colada abaixo de", "numero", "",
           minimo=0, maximo=10, passo=0.1, unidade="s"),
        _f("mod.ritmo.PADRAO.speech_density_min", "Ritmo: fala arrastada abaixo de", "numero", "", minimo=0,
           maximo=6, passo=0.1, unidade="palavra/s"),
        _f("mod.ritmo.PADRAO.speech_density_min_duration", "Ritmo: arrastado só a partir de", "numero", "",
           minimo=0, maximo=20, passo=0.5, unidade="s"),
        _f("mod.ritmo.PADRAO.scdet_limiar", "Ritmo: sensibilidade da troca de plano", "numero", "", minimo=1,
           maximo=50, passo=0.5),
    ], [])

    longo = Grupo("c_longo", "Vídeo longo", "Consulta à parte ao DeepSeek por trechos corridos de 15–20 min; "
                  "na produção sai o silêncio > 4 s e entra a moldura do canal.", [
        _f("fase1.longo.modelo", "Modelo do vídeo longo", "texto", ""),
        _f("fase1.longo.duracao_minima", "Duração mínima (o que sobra)", "numero", "", minimo=1, maximo=120,
           passo=0.5, unidade="min"),
        _f("fase1.longo.duracao_maxima", "Duração máxima", "numero", "", minimo=1, maximo=180, passo=0.5,
           unidade="min"),
        _f("fase1.longo.timeout", "Tempo limite", "inteiro", "", minimo=10, maximo=3600, unidade="s"),
        _f("fase1.longo.tentativas", "Tentativas", "inteiro", "", minimo=1, maximo=10),
        _f("mod.video_longo.SILENCIO_DB", "Silêncio abaixo de", "numero", "", minimo=-80, maximo=-5, passo=1,
           unidade="dB"),
        _f("mod.video_longo.SILENCIO_LONGO", "Cortar pausa maior que", "numero", "", minimo=0.5, maximo=60,
           passo=0.5, unidade="s"),
        _f("mod.video_longo.SILENCIO_SOBRA", "A pausa cortada vira", "numero", "", minimo=0, maximo=5, passo=0.1,
           unidade="s"),
        _f("mod.video_longo.PEDACO_MINIMO", "Sobra mínima entre dois cortes", "numero", "", minimo=0, maximo=5,
           passo=0.1, unidade="s"),
        _f("mod.moldura.RAIO", "Moldura: raio do canto", "inteiro", "", minimo=0, maximo=120, unidade="px"),
        _f("mod.moldura.BORDA", "Moldura: espessura da borda", "inteiro", "", minimo=0, maximo=40, unidade="px"),
    ], [])

    estudio = Grupo("c_estudio", "Estúdio e Publicador", "Para onde vão os cortes revisados e os avisos "
                    "por e-mail (pouca RAM). As variáveis de e-mail ficam em publicador.env.", [
        _f("estudio.publicador_url", "Endereço do Publicador", "url", "A fila de postagem (POST /api/videos)."),
        _f("mod.estudio.RAM_MINIMA_GB", "RAM mínima para o crop", "inteiro",
           "Abaixo disso o crop que segue quem fala é desligado (BIOS subindo com 8 GB).", minimo=1, maximo=256,
           unidade="GB", ao_vivo=False),
        _f("email.PUBLICADOR_EMAIL_PARA", "Avisos: e-mail de destino", "texto", ""),
        _f("email.PUBLICADOR_EMAIL_DE", "Avisos: remetente", "texto", "Vazio = o usuário do SMTP."),
        _f("email.PUBLICADOR_EMAIL_SMTP_HOST", "Avisos: servidor SMTP", "texto", "Ex.: smtp.gmail.com"),
        _f("email.PUBLICADOR_EMAIL_SMTP_PORT", "Avisos: porta SMTP", "inteiro", "587 (STARTTLS) ou 465 (SSL).",
           minimo=1, maximo=65535),
        _f("email.PUBLICADOR_EMAIL_SMTP_USER", "Avisos: usuário SMTP", "texto", ""),
        _f("email.PUBLICADOR_EMAIL_SMTP_SENHA", "Avisos: senha SMTP", "segredo", "Senha de app do Gmail."),
    ], [("publicador", "Testar o Publicador")])

    # Na tela, por milhão de tokens (como as tabelas do DeepSeek e do OpenRouter); no código, por token.
    por_milhao = dict(minimo=0, maximo=1000, unidade="US$/milhão de tokens", escala=1e-6)
    precos = Grupo("c_precos", "Preços (estimativa)", "Usados na aba Custos quando a API não devolve o custo "
                   "real (o DeepSeek nunca devolve; o OpenRouter devolve no chat).", [
        _f("precos.deepseek_entrada", "DeepSeek: entrada (sem cache)", "numero", "", **por_milhao),
        _f("precos.deepseek_entrada_cache", "DeepSeek: entrada (cache)", "numero", "", **por_milhao),
        _f("precos.deepseek_saida", "DeepSeek: saída", "numero", "", **por_milhao),
        _f("precos.jev_entrada", "JEV: entrada", "numero", "", **por_milhao),
        _f("precos.openrouter_entrada", "OpenRouter chat: entrada", "numero", "", **por_milhao),
        _f("precos.openrouter_saida", "OpenRouter chat: saída", "numero", "", **por_milhao),
        _f("precos.whisper_minuto", "Whisper pela API", "numero", "", minimo=0, maximo=1, passo=0.0001,
           unidade="US$/min"),
    ], [])
    return [acesso, selecao, nota, corte, abertura, legenda, fotos, audio, longo, estudio, precos]


GRUPOS = _montar()
CAMPOS = {c.chave: c for g in GRUPOS for c in g.campos}


# ──────────────────────────────────────────────────────────────────
#  Arquivos
# ──────────────────────────────────────────────────────────────────

def _arq_json() -> Path:
    return _DATA / "config_cortador.json"


def _ler_overlay() -> dict:
    try:
        d = json.loads(_arq_json().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def overlay() -> dict:
    return _ler_overlay()


def _escrever(caminho: Path, texto: str) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_name(caminho.name + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, caminho)


def _yaml(nome: str) -> dict:
    return yaml.safe_load((FASE1 / nome).read_text(encoding="utf-8")) or {}


def _pegar(d: dict, caminho: str):
    for parte in caminho.split("."):
        if not isinstance(d, dict) or parte not in d:
            return None
        d = d[parte]
    return d


def _definir_caminho(d: dict, caminho: str, valor) -> None:
    *pais, folha = caminho.split(".")
    for p in pais:
        d = d.setdefault(p, {})
    d[folha] = valor


def mesclar_yaml(nome: str, secao: str) -> dict:
    """O YAML do repositório com o que foi mudado na tela por cima."""
    cfg = _yaml(nome)
    for caminho, valor in (_ler_overlay().get(secao) or {}).items():
        _definir_caminho(cfg, caminho, valor)
    return cfg


def _ler_env(arq: Path) -> dict[str, str]:
    saida = {}
    try:
        for linha in arq.read_text(encoding="utf-8-sig").splitlines():
            linha = linha.strip()
            if linha and not linha.startswith("#") and "=" in linha:
                k, v = linha.split("=", 1)
                saida[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return saida


def _gravar_env(arq: Path, sets: dict[str, str], remover: list[str]) -> None:
    """Troca só as linhas das chaves; o resto do arquivo (comentários, ordem) fica."""
    original = arq.read_text(encoding="utf-8-sig") if arq.exists() else ""
    linhas = original.splitlines()
    for k in remover:
        linhas = [ln for ln in linhas if not re.match(rf"^\s*{re.escape(k)}\s*=", ln)]
    for k, v in sets.items():
        nova = f"{k}={v}"
        for i, ln in enumerate(linhas):
            if re.match(rf"^\s*{re.escape(k)}\s*=", ln):
                linhas[i] = nova
                break
        else:
            linhas.append(nova)
    if arq.exists() and arq not in _backup_env:
        shutil.copy2(arq, arq.with_name(arq.name + ".bak"))
        _backup_env.add(arq)
    _escrever(arq, "\n".join(linhas) + "\n")
    try:
        os.chmod(arq, 0o600)
    except OSError:
        pass


def _config_estudio() -> dict:
    try:
        return json.loads((_DATA / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ──────────────────────────────────────────────────────────────────
#  Constantes dos módulos (mod.*)
# ──────────────────────────────────────────────────────────────────

_PROPRIO = None          # o módulo do Estúdio (pode ser "__main__": nunca reimportar)
_PADROES_FIXOS = {"mod.estudio.RAM_MINIMA_GB": 16}   # lido na importação, já com o valor salvo


def _alvo_mod(chave: str):
    """mod.<modulo>.<ATTR>[.<item do dict>] -> (objeto, nome, é_item_de_dict)."""
    _, modulo, resto = chave.split(".", 2)
    mod = _PROPRIO if modulo == "estudio" else importlib.import_module(modulo)
    if "." in resto:
        attr, item = resto.split(".", 1)
        return getattr(mod, attr), item, True
    return mod, resto, False


def _ler_mod(chave: str):
    obj, nome, item = _alvo_mod(chave)
    return obj[nome] if item else getattr(obj, nome)


def _definir_mod(chave: str, valor) -> None:
    obj, nome, item = _alvo_mod(chave)
    if item:
        obj[nome] = valor
    else:
        setattr(obj, nome, valor)


def _aplicar_mods() -> None:
    ov = _ler_overlay().get("modulos") or {}
    for chave in (k for k in CAMPOS if k.startswith("mod.")):
        rest = chave[4:]
        if not CAMPOS[chave].ao_vivo:
            continue
        try:
            _definir_mod(chave, ov[rest] if rest in ov else _PADRAO_MOD[chave])
        except (ImportError, AttributeError, KeyError):
            pass


def iniciar(data_dir: Path, email_env: Path, proprio) -> None:
    """Guarda os padrões das constantes e aplica o config_cortador.json por cima."""
    global _DATA, _EMAIL_ENV, _PROPRIO
    _DATA, _EMAIL_ENV, _PROPRIO = data_dir, email_env, proprio
    for chave in (k for k in CAMPOS if k.startswith("mod.")):
        try:
            _PADRAO_MOD[chave] = _PADROES_FIXOS[chave] if chave in _PADROES_FIXOS else copy.deepcopy(_ler_mod(chave))
        except (ImportError, AttributeError, KeyError):
            pass
    ov = _ler_overlay().get("modulos") or {}
    for chave in (k for k in CAMPOS if k.startswith("mod.")):   # na partida vale até o que pede reinício
        if chave[4:] in ov:
            try:
                _definir_mod(chave, ov[chave[4:]])
            except (ImportError, AttributeError, KeyError):
                pass


def valor_salvo(data_dir: Path, chave: str, padrao):
    """Para quem lê a constante na importação, antes do iniciar (RAM_MINIMA_GB)."""
    try:
        ov = json.loads((data_dir / "config_cortador.json").read_text(encoding="utf-8")).get("modulos") or {}
        return type(padrao)(ov.get(chave, padrao))
    except (OSError, ValueError, TypeError, AttributeError):
        return padrao


# ──────────────────────────────────────────────────────────────────
#  Valores
# ──────────────────────────────────────────────────────────────────

def _na_tela(c: Campo, v):
    """Valor do código na unidade da tela (dias, US$/milhão...); 12 algarismos tiram o resto de float (0,28 e não
    0,27999999999999997)."""
    if c.escala == 1 or not isinstance(v, (int, float)) or isinstance(v, bool):
        return v
    r = float(f"{v / c.escala:.12g}")
    return int(r) if c.tipo == "inteiro" and r == int(r) else r


def _no_codigo(c: Campo, v):
    if c.escala == 1 or not isinstance(v, (int, float)) or isinstance(v, bool):
        return v
    r = float(f"{v * c.escala:.12g}")
    return int(r) if c.tipo == "inteiro" and r == int(r) else r


def _padrao(c: Campo):
    k = c.chave
    if k.startswith("fase1."):
        return _pegar(_yaml("config.yaml"), k[6:])
    if k.startswith("cortes."):
        return _pegar(_yaml("config_cortes.yaml"), k[7:])
    if k.startswith("mod."):
        return _na_tela(c, _PADRAO_MOD.get(k))
    if k.startswith("precos."):
        import custos_api
        return _na_tela(c, custos_api.PRECOS_PADRAO.get(k[7:]))
    if k == "estudio.publicador_url":
        return "http://127.0.0.1:8080"
    if k == "estudio.pasta_transicoes":
        return str(_DATA / "transicoes")
    if k == "email.PUBLICADOR_EMAIL_SMTP_HOST":
        return "smtp.gmail.com"
    if k == "email.PUBLICADOR_EMAIL_SMTP_PORT":
        return 587
    return "" if c.tipo != "booleano" else False


def _atual(c: Campo):
    """(valor salvo ou None, origem)."""
    k = c.chave
    ov = _ler_overlay()
    if k.startswith("fase1.") and k[6:] in (ov.get("fase1") or {}):
        return ov["fase1"][k[6:]], "config_cortador.json"
    if k.startswith("cortes.") and k[7:] in (ov.get("cortes") or {}):
        return ov["cortes"][k[7:]], "config_cortador.json"
    if k.startswith("mod.") and k[4:] in (ov.get("modulos") or {}):
        return _na_tela(c, ov["modulos"][k[4:]]), "config_cortador.json"
    if k.startswith("precos.") and k[7:] in (ov.get("precos") or {}):
        return _na_tela(c, ov["precos"][k[7:]]), "config_cortador.json"
    if k.startswith("env."):
        v = _ler_env(ARQ_ENV).get(k[4:]) or ""
        return (v or None), ("fase1/.env" if v else "padrão")
    if k.startswith("email."):
        v = _ler_env(_EMAIL_ENV).get(k[6:]) or ""
        return (v or None), ("publicador.env" if v else "padrão")
    if k.startswith("estudio."):
        v = _config_estudio().get(k[8:])
        return (v or None), ("config.json" if v else "padrão")
    return None, "padrão"


def _nome(c: Campo) -> str:
    return f"«{c.rotulo}»"


def _converter(c: Campo, bruto):
    t = c.tipo
    if t == "leitura":
        raise ValueError(f"{_nome(c)} é só leitura.")
    if t == "booleano":
        if isinstance(bruto, bool):
            return bruto
        if str(bruto).strip().lower() in ("1", "true", "sim", "on"):
            return True
        if str(bruto).strip().lower() in ("0", "false", "nao", "não", "off", ""):
            return False
        raise ValueError(f"{_nome(c)} precisa ser ligado ou desligado.")
    if t in ("inteiro", "numero"):
        try:
            num = float(str(bruto).strip().replace(",", ".")) if not isinstance(bruto, (int, float)) else float(bruto)
        except ValueError:
            raise ValueError(f"{_nome(c)} precisa ser um número.") from None
        if isinstance(bruto, bool) or not math.isfinite(num):
            raise ValueError(f"{_nome(c)} precisa ser um número.")
        if t == "inteiro":
            if num != int(num):
                raise ValueError(f"{_nome(c)} precisa ser um número inteiro.")
            num = int(num)
        if (c.minimo is not None and num < c.minimo) or (c.maximo is not None and num > c.maximo):
            raise ValueError(f"{_nome(c)} precisa estar entre {c.minimo:g} e {c.maximo:g}"
                             f"{' ' + c.unidade if c.unidade else ''}.")
        return num
    v = str(bruto).strip()
    if not v:
        raise ValueError(f"{_nome(c)} não pode ficar vazio (use Restaurar padrão).")
    if re.search(r"[\x00-\x1f\x7f]", v):
        raise ValueError(f"{_nome(c)} não pode ter quebra de linha.")
    if t == "segredo" and not re.fullmatch(r"[^\s'\"]{8,512}", v):
        raise ValueError(f"{_nome(c)} precisa ter de 8 a 512 caracteres, sem espaços nem aspas.")
    if t == "url":
        p = urlparse(v)
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError(f"{_nome(c)} precisa ser um endereço http:// completo.")
        v = v.rstrip("/")
    if t == "escolha":
        validos = [o[0] for o in c.opcoes or []]
        if v not in validos:
            raise ValueError(f"{_nome(c)} precisa ser uma de: {', '.join(validos)}.")
    if t == "texto" and c.chave.endswith((".modelo", "MODELO_VISAO")) and not re.fullmatch(r"[A-Za-z0-9._:/@+~\-]+", v):
        raise ValueError(f"{_nome(c)} só aceita letras, números e . _ : / @ + ~ - (ex.: fabricante/modelo).")
    if t == "caminho" and not v.startswith("/") and not re.match(r"^[A-Za-z]:[\\/]", v):
        raise ValueError(f"{_nome(c)} precisa ser um caminho completo.")
    return v


def _mascara(v: str | None) -> dict:
    return {"definido": bool(v), "final": v[-4:] if v and len(v) >= 12 else ""}


def _serializar(c: Campo) -> dict:
    bruto, origem = _atual(c)
    pad = _padrao(c)
    if c.tipo == "segredo":
        valor, pad = _mascara(bruto), None
    elif c.tipo == "booleano":
        valor = bool(bruto) if bruto is not None else bool(pad)
    else:
        valor = bruto
    pendente = False
    if not c.ao_vivo and c.chave.startswith("mod."):
        try:
            em_uso = _ler_mod(c.chave)
            pendente = em_uso != (bruto if bruto is not None else pad)
        except (ImportError, AttributeError, KeyError):
            pass
    return {"chave": PREFIXO + c.chave, "rotulo": c.rotulo, "tipo": c.tipo, "ajuda": c.ajuda,
            "onde": c.chave.split(".", 1)[0], "ao_vivo": c.ao_vivo, "minimo": c.minimo, "maximo": c.maximo,
            "passo": c.passo, "unidade": c.unidade,
            "opcoes": [{"valor": v, "rotulo": r} for v, r in c.opcoes] if c.opcoes else None,
            "sugestoes": None, "formato": None, "campo": None, "padrao": pad, "padrao_rotulo": None,
            "valor": valor, "origem": origem, "pendente_reinicio": pendente, "aviso": ""}


def _grupo_crop() -> dict:
    """config_crop.yaml inteiro, só para ver: o crop LR-ASD não se mexe."""
    campos = []

    def andar(d, prefixo=""):
        for k, v in d.items():
            caminho = f"{prefixo}{k}"
            if isinstance(v, dict):
                andar(v, caminho + ".")
            else:
                campos.append({"chave": PREFIXO + "crop." + caminho, "rotulo": caminho, "tipo": "leitura",
                               "ajuda": "", "onde": "crop", "ao_vivo": True, "minimo": None, "maximo": None,
                               "passo": None, "unidade": "", "opcoes": None, "sugestoes": None, "formato": None,
                               "campo": None, "padrao": v, "padrao_rotulo": None, "valor": v, "origem": "config_crop.yaml",
                               "pendente_reinicio": False, "aviso": ""})
    andar(_yaml("config_crop.yaml"))
    return {"id": "c_crop", "titulo": "Crop (só leitura)",
            "descricao": "fase1/config_crop.yaml do crop que segue quem fala (LR-ASD). Fica só para consulta: "
                         "por decisão sua (28/09/2026) o jeito de fazer o crop não muda.",
            "testes": [], "campos": campos}


def estado() -> dict:
    grupos = [{"id": g.id, "titulo": g.titulo, "descricao": g.descricao,
               "testes": [{"servico": s, "rotulo": r} for s, r in g.testes],
               "campos": [_serializar(c) for c in g.campos]} for g in GRUPOS]
    grupos.append(_grupo_crop())
    avisos = []
    soma = sum(float(_valor_efetivo(CAMPOS[f"fase1.ranking.peso_{p}"]) or 0)
               for p in ("viral", "abertura", "duracao", "ritmo", "llm"))
    if abs(soma - 1) > 0.011:
        avisos.append(f"Os pesos da nota final somam {soma:.2f} (o normal é 1).")
    return {"grupos": grupos, "formatos": {}, "env_arquivo": str(ARQ_ENV), "json_arquivo": str(_arq_json()),
            "avisos": avisos}


def _valor_efetivo(c: Campo):
    bruto, _ = _atual(c)
    return bruto if bruto is not None else _padrao(c)


# ──────────────────────────────────────────────────────────────────
#  Salvar
# ──────────────────────────────────────────────────────────────────

def _sem_prefixo(chave: str) -> str:
    return chave[len(PREFIXO):] if chave.startswith(PREFIXO) else chave


def salvar(alteracoes: dict) -> dict:
    """{chave: valor} só do que mudou. Tudo ou nada: um valor inválido cancela o lote.
    Vazio volta ao padrão; nos segredos, vazio mantém."""
    if not isinstance(alteracoes, dict):
        raise ValueError("O corpo precisa ser um objeto {chave: valor}.")
    sets, resets, mantidos = {}, [], []
    for chave_tela, bruto in alteracoes.items():
        chave = _sem_prefixo(chave_tela)
        c = CAMPOS.get(chave)
        if c is None:
            raise ValueError(f"Campo desconhecido: {chave_tela}")
        vazio = bruto is None or (isinstance(bruto, str) and not bruto.strip())
        if c.tipo == "segredo" and vazio:
            mantidos.append(chave_tela)
        elif vazio and c.tipo != "booleano":
            resets.append(chave)
        else:
            sets[chave] = _converter(c, bruto)
    return {**_gravar(sets, resets), "mantidos": mantidos}


def restaurar(chaves: list[str]) -> dict:
    desconhecidas = [k for k in chaves if _sem_prefixo(k) not in CAMPOS]
    if desconhecidas:
        raise ValueError(f"Campo desconhecido: {', '.join(desconhecidas)}")
    return {**_gravar({}, [_sem_prefixo(k) for k in chaves]), "mantidos": []}


def _gravar(sets: dict, resets: list[str]) -> dict:
    with _trava:
        ov = _ler_overlay()
        secoes = {"fase1.": "fase1", "cortes.": "cortes", "mod.": "modulos", "precos.": "precos"}
        env_sets, env_rem, mail_sets, mail_rem = {}, [], {}, []
        cfg_estudio = _config_estudio()
        mudou_estudio = False
        for chave in resets:
            for pre, sec in secoes.items():
                if chave.startswith(pre):
                    (ov.get(sec) or {}).pop(chave[len(pre):], None)
            if chave.startswith("env."):
                env_rem.append(chave[4:])
            elif chave.startswith("email."):
                mail_rem.append(chave[6:])
            elif chave.startswith("estudio.") and chave[8:] in cfg_estudio:
                cfg_estudio.pop(chave[8:])
                mudou_estudio = True
        for chave, valor in sets.items():
            c = CAMPOS[chave]
            for pre, sec in secoes.items():
                if chave.startswith(pre):
                    codigo = _no_codigo(c, valor)
                    if valor == _padrao(c):
                        (ov.get(sec) or {}).pop(chave[len(pre):], None)
                    else:
                        ov.setdefault(sec, {})[chave[len(pre):]] = codigo
            if chave.startswith("env."):
                env_sets[chave[4:]] = str(valor)
            elif chave.startswith("email."):
                mail_sets[chave[6:]] = str(valor)
            elif chave.startswith("estudio."):
                cfg_estudio[chave[8:]] = valor
                mudou_estudio = True
        pesos = [float(_pegar_valor_ov(ov, f"fase1.ranking.peso_{p}")) for p in ("viral", "abertura", "duracao",
                                                                                   "ritmo", "llm")]
        if abs(sum(pesos) - 1) > 0.011:
            raise ValueError(f"Os pesos da nota final precisam somar 1 (estão somando {sum(pesos):.2f}).")
        ov = {k: v for k, v in ov.items() if v}
        if ov:
            _escrever(_arq_json(), json.dumps(ov, ensure_ascii=False, indent=2) + "\n")
        else:
            _arq_json().unlink(missing_ok=True)
        if env_sets or env_rem:
            _gravar_env(ARQ_ENV, env_sets, env_rem)
            for k in env_rem:
                os.environ.pop(k, None)
            os.environ.update(env_sets)   # o processo do Estúdio e os subprocessos já usam a chave nova
        if mail_sets or mail_rem:
            _gravar_env(_EMAIL_ENV, mail_sets, mail_rem)
        if mudou_estudio:
            _escrever(_DATA / "config.json", json.dumps(cfg_estudio, ensure_ascii=False, indent=2))
        _aplicar_mods()
        feitos = list(sets) + list(resets)
        reiniciar = [PREFIXO + k for k in feitos if not CAMPOS[k].ao_vivo]
        return {"salvos": [PREFIXO + k for k in sets], "restaurados": [PREFIXO + k for k in resets],
                "ao_vivo": [PREFIXO + k for k in feitos if CAMPOS[k].ao_vivo], "reiniciar": reiniciar}


def _pegar_valor_ov(ov: dict, chave: str):
    resto = chave[6:]
    if resto in (ov.get("fase1") or {}):
        return ov["fase1"][resto]
    return _padrao(CAMPOS[chave])


# ──────────────────────────────────────────────────────────────────
#  Testes de conexão (nada que gaste crédito)
# ──────────────────────────────────────────────────────────────────

def testar(servico: str) -> dict:
    import requests
    if servico == "openrouter":
        chave = os.environ.get("OPENROUTER_API_KEY") or _ler_env(ARQ_ENV).get("OPENROUTER_API_KEY")
        if not chave:
            return {"ok": False, "mensagem": "A chave do OpenRouter não está definida."}
        try:
            r = requests.get("https://openrouter.ai/api/v1/key", headers={"Authorization": f"Bearer {chave}"},
                             timeout=15)
        except requests.RequestException as e:
            return {"ok": False, "mensagem": f"O OpenRouter não respondeu ({type(e).__name__})."}
        if r.status_code != 200:
            return {"ok": False, "mensagem": f"O OpenRouter recusou a chave (HTTP {r.status_code})."}
        d = (r.json() or {}).get("data") or {}
        return {"ok": True, "mensagem": f"Chave aceita; gasto total US$ {float(d.get('usage') or 0):.2f}"
                + (f", limite restante US$ {float(d['limit_remaining']):.2f}." if d.get("limit_remaining") is not None
                   else ", sem limite de gasto.")}
    if servico == "deepseek":
        chave = _ler_env(ARQ_ENV).get("DEEPSEEK_API_KEY") or os.environ.get("DEEPSEEK_API_KEY")
        if not chave:
            try:
                import deepseek_client
                chave = deepseek_client.api_key()
            except Exception:  # noqa: BLE001
                chave = ""
        if not chave:
            return {"ok": False, "mensagem": "A chave do DeepSeek não está definida."}
        try:
            r = requests.get("https://api.deepseek.com/user/balance",
                             headers={"Authorization": f"Bearer {chave}"}, timeout=15)
        except requests.RequestException as e:
            return {"ok": False, "mensagem": f"O DeepSeek não respondeu ({type(e).__name__})."}
        if r.status_code != 200:
            return {"ok": False, "mensagem": f"O DeepSeek recusou a chave (HTTP {r.status_code})."}
        saldos = [f"{b.get('total_balance')} {b.get('currency')}" for b in (r.json().get("balance_infos") or [])]
        return {"ok": True, "mensagem": "Chave aceita; saldo " + (", ".join(saldos) or "?") + "."}
    if servico == "publicador":
        base = (_config_estudio().get("publicador_url") or "http://127.0.0.1:8080").rstrip("/")
        try:
            r = requests.get(base + "/api/estado", timeout=8)
            canais = [c.get("nome") for c in (r.json().get("canais") or [])]
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mensagem": f"O Publicador não respondeu em {base} ({type(e).__name__})."}
        return {"ok": True, "mensagem": f"O Publicador respondeu em {base}; canais: {', '.join(canais) or 'nenhum'}."}
    raise ValueError(f"Serviço desconhecido: {servico}")
