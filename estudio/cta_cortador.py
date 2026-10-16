"""CTA em vídeo por canal, do lado do Cortador.

O CTA de cada canal é um .mp4 curto que o usuário envia na tela do canal (aba Canais, "CTA em vídeo") e que fica no
Estúdio de Histórias: <ESTUDIO_RAIZ das Histórias>/dados/cta/<canal>.mp4. A chave é o nome do canal NO PUBLICADOR em
minúsculas (garras, info, br_semfim...), que é o `canal` do corte.

Os dois apps têm um pacote chamado `estudio` e ambientes virtuais diferentes, então o código das Histórias NÃO é
importado aqui: a anexação roda no ambiente delas, num subprocesso (`python main.py cta-anexar`, no diretório historias/).
Isso também a deixa independente de o serviço das Histórias (porta 8091) estar no ar.

Short: o CTA só entra na hora do envio (`anexar_copia`), numa cópia temporária do final.mp4; o vídeo revisado não é
tocado, então reenviar nunca duplica o CTA. Vídeo longo: `caminho_cta` devolve o arquivo do canal, que o video_longo
cola no fim da produção (com o cta/cta.mp4 global como reserva).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from pathlib import Path

import historias_ponte

log = logging.getLogger("estudio")

NOME_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")     # igual ao estudio/cta.py das Histórias
TEMPO_MAXIMO_S = 1800                                   # um short de 60 s leva bem menos; isto só evita travar o envio


def chave(canal: str | None) -> str:
    """Nome do canal no Publicador como chave do CTA; "" se não servir de nome de arquivo."""
    n = (canal or "").strip().lower()
    return n if NOME_OK.match(n) else ""


def _raiz_historias() -> Path:
    data_dir = Path(os.environ.get("ESTUDIO_DATA") or r"C:\VideoMaker\estudio")
    return historias_ponte.raiz_dados(data_dir)


def caminho_cta(canal: str | None) -> Path | None:
    """O arquivo do CTA do canal, ou None se o canal não tem (ou o nome é inválido)."""
    k = chave(canal)
    if not k:
        return None
    arq = _raiz_historias() / "dados" / "cta" / f"{k}.mp4"
    return arq if arq.is_file() else None


def _python_historias() -> Path | None:
    candidatos = [os.environ.get("ESTUDIO_HISTORIAS_PYTHON") or "",
                  str(historias_ponte.CODIGO / ".venv" / "bin" / "python"),
                  str(historias_ponte.CODIGO / ".venv" / "Scripts" / "python.exe")]
    return next((Path(c) for c in candidatos if c and Path(c).is_file()), None)


def _ambiente() -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "ESTUDIO_DATA")}
    env["ESTUDIO_RAIZ"] = str(_raiz_historias())
    env.setdefault("ESTUDIO_ENV", str(_raiz_historias() / ".env"))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def anexar_copia(original: Path, canal: str | None, pasta_tmp: Path) -> tuple[Path | None, str]:
    """Faz em `pasta_tmp` uma cópia de `original` com o CTA do canal no fim. O original não é alterado.

    Devolve (cópia, mensagem). `cópia` é None quando não há o que anexar (canal sem CTA: mensagem vazia) ou quando
    a anexação falhou (mensagem com o motivo, para o log do trabalho): em ambos os casos quem chama envia o original.
    Quem chama apaga `pasta_tmp` depois do uso."""
    k = chave(canal)
    if not k or caminho_cta(k) is None:
        return None, ""
    py = _python_historias()
    if py is None:
        return None, "não achei o ambiente virtual das Histórias (historias/.venv)"
    pasta_tmp.mkdir(parents=True, exist_ok=True)
    saida = pasta_tmp / original.name
    try:
        r = subprocess.run([str(py), "main.py", "cta-anexar", str(original), "--canal", k, "-o", str(saida)],
                           cwd=str(historias_ponte.CODIGO), env=_ambiente(), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=TEMPO_MAXIMO_S)
    except subprocess.TimeoutExpired:
        return None, f"a anexação passou de {TEMPO_MAXIMO_S // 60} min"
    except OSError as e:
        return None, f"não consegui rodar o ambiente das Histórias: {e}"
    if r.returncode != 0:
        detalhe = (r.stderr or r.stdout or "").strip().splitlines()
        return None, (detalhe[-1] if detalhe else f"código de saída {r.returncode}")[:300]
    if not saida.is_file() or saida.stat().st_size == 0:
        return None, ""          # o comando disse "nada a fazer": sem CTA
    linha = (r.stdout or "").strip().splitlines()
    return saida, (linha[0] if linha else "")[:200]
