"""Perfil de cada canal de publicação (os canais do Publicador: br_semfim, info, garras...).

O canal do Publicador é o canal de verdade no YouTube/TikTok. O perfil diz o
que ele é (nicho, público, tom), como os cortes dele devem sair (duração,
abertura, formatos, legenda) e qual visual (logo/cores) usa. O Cortador lê o
perfil para pré-selecionar a legenda e o visual da proposta e mostra o resumo
ao escolher o canal; os canais de história (historias/) apontam para o canal de
publicação pelo campo "canal no Publicador".

Fica em $ESTUDIO_DATA/canais.json. O visual continua em config.json
(canal_perfil), que é de onde a produção sempre leu.
"""

from __future__ import annotations

import copy
import json
import os
import re
import threading
from pathlib import Path

FORMATOS = {"dinamico": "crop que segue quem fala", "transparente": "transparente",
            "imagens": "fotos do assunto + corte", "longo": "vídeo longo (16:9 com moldura)"}
CAMPOS_TEXTO = ("nome_exibicao", "descricao", "nicho", "publico", "tom", "fontes", "titulo_estilo", "abertura",
                "observacoes")

PADRAO = {"nome_exibicao": "", "descricao": "", "nicho": "", "publico": "", "tom": "", "fontes": "",
          "titulo_estilo": "", "duracao_min_s": 30, "duracao_max_s": 50, "abertura": "",
          "formatos": ["dinamico", "transparente", "imagens"], "legenda_padrao": "new", "video_longo": True,
          "observacoes": ""}

# Os dois canais de cortes que já estavam no Estúdio. Tirado do que eles
# publicaram até 02/10/2026 (trabalhos.json) e da análise do info de 29/09
# (EDICAO_CANAL.md §1).
SEMENTES = {
    "info": {
        "nome_exibicao": "Informativo Nacional",
        "descricao": "Cortes curtos de política e atualidades do Brasil, tirados de lives, podcasts e entrevistas. "
                     "Cada corte conta um fato ou uma fala com começo, meio e fim.",
        "nicho": "Política e atualidades do Brasil",
        "publico": "Quem acompanha política pelo celular (Shorts e TikTok)",
        "tom": "Informativo e direto, de manchete: quem disse o quê",
        "fontes": "Lives e podcasts de política (ex.: Plantão do MamãeFalei, Redcast)",
        "titulo_estilo": "Manchete descritiva com o nome de quem fala e o que disse ou fez "
                         "(ex.: \"Ricardo Salles desiste e apoia quem chamava de centrão\").",
        "duracao_min_s": 30, "duracao_max_s": 50,
        "abertura": "Pergunta de jornalista, confronto ou \"Urgente\" seguram mais; monólogo técnico ou "
                    "abstrato perde (pista de 10 vídeos, 29/09).",
        "formatos": ["dinamico", "imagens", "transparente", "longo"],
        "legenda_padrao": "new",
        "video_longo": True,
        "observacoes": "Análise de 29/09 (95 Shorts com Analytics): o % assistido decide as views e o público "
                       "assiste ~30–35 s; acima de 70 s, 0 de 48 passaram de ~1.000 views. TikTok: conta "
                       "info_nacional, só vídeos de até 2 min.",
    },
    "br_semfim": {
        "nome_exibicao": "BR Sem Fim",
        "descricao": "Cortes de opinião política e polêmicas, tirados de lives e podcasts brasileiros. O foco é "
                     "a frase forte e o embate entre os convidados.",
        "nicho": "Opinião política e polêmicas",
        "publico": "Quem acompanha o debate político e gosta de embate",
        "tom": "Opinativo e de embate: a frase de impacto vem primeiro",
        "fontes": "Lives e podcasts de política (ex.: Plantão do MamãeFalei, Redcast, Flow)",
        "titulo_estilo": "Cobrança direta ou frase de efeito com o nome de quem fala "
                         "(ex.: \"Artur do Val cobra Porchat por não citar o PT no escândalo\").",
        "duracao_min_s": 30, "duracao_max_s": 50,
        "abertura": "Começar já no confronto ou na frase forte, sem introdução.",
        "formatos": ["dinamico", "imagens", "longo"],
        "legenda_padrao": "new",
        "video_longo": True,
        "observacoes": "Sem conta de TikTok ligada no Publicador. A regra de 30–50 s veio dos dados do info "
                       "(29/09) e vale para todos os canais: conferir na aba Desempenho do Publicador.",
    },
}

_trava = threading.Lock()


def _arquivo(data_dir: Path) -> Path:
    return data_dir / "canais.json"


def _ler(data_dir: Path) -> dict:
    try:
        d = json.loads(_arquivo(data_dir).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _gravar(data_dir: Path, perfis: dict) -> None:
    arq = _arquivo(data_dir)
    tmp = arq.with_name(arq.name + ".tmp")
    tmp.write_text(json.dumps(perfis, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, arq)


def semear(data_dir: Path) -> None:
    """Cria o perfil de br_semfim e info se ainda não existe (nunca sobrescreve o editado)."""
    with _trava:
        perfis = _ler(data_dir)
        faltam = {k: v for k, v in SEMENTES.items() if k not in perfis}
        if faltam:
            perfis.update(copy.deepcopy(faltam))
            _gravar(data_dir, perfis)


def perfil(data_dir: Path, canal: str) -> dict | None:
    p = _ler(data_dir).get(canal)
    return {**copy.deepcopy(PADRAO), **p} if p else None


def todos(data_dir: Path) -> dict:
    return {k: {**copy.deepcopy(PADRAO), **v} for k, v in _ler(data_dir).items()}


def _limpar(dados: dict) -> dict:
    p = copy.deepcopy(PADRAO)
    for k in CAMPOS_TEXTO:
        if k in dados:
            v = str(dados[k] or "").strip()
            if len(v) > 2000:
                raise ValueError(f"O campo {k} passou de 2.000 letras.")
            p[k] = v
    for k in ("duracao_min_s", "duracao_max_s"):
        if k in dados:
            try:
                p[k] = int(float(dados[k]))
            except (TypeError, ValueError):
                raise ValueError("Duração precisa ser um número de segundos.") from None
            if not 5 <= p[k] <= 3600:
                raise ValueError("Duração precisa estar entre 5 e 3600 segundos.")
    if p["duracao_min_s"] > p["duracao_max_s"]:
        raise ValueError("A duração mínima passa da máxima.")
    if "formatos" in dados:
        fmts = [f for f in (dados["formatos"] or []) if f in FORMATOS]
        p["formatos"] = list(dict.fromkeys(fmts))
    if "legenda_padrao" in dados:
        if dados["legenda_padrao"] not in ("new", "old"):
            raise ValueError("Legenda padrão precisa ser new ou old.")
        p["legenda_padrao"] = dados["legenda_padrao"]
    if "video_longo" in dados:
        p["video_longo"] = bool(dados["video_longo"])
    return p


def salvar(data_dir: Path, canal: str, dados: dict) -> dict:
    if not re.fullmatch(r"[\w.-]{1,60}", canal or ""):
        raise ValueError("Nome de canal inválido.")
    p = _limpar(dados)
    with _trava:
        perfis = _ler(data_dir)
        perfis[canal] = p
        _gravar(data_dir, perfis)
    return p


def apagar(data_dir: Path, canal: str) -> None:
    with _trava:
        perfis = _ler(data_dir)
        perfis.pop(canal, None)
        _gravar(data_dir, perfis)
