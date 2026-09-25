"""Estúdio: a produção dos cortes pelo navegador, rodando na máquina da GPU.

Cola o link do YouTube (ou manda o arquivo) e o serviço faz o processo
inteiro sozinho, um trabalho por vez:

    1. baixa o vídeo (yt-dlp, com a legenda do YouTube quando houver)
    2. Fase 1 — escolhe os melhores trechos (fase1/pipeline.py: DeepSeek + JEV)
       ou, no lote CSV, usa os tempos do CSV
    3. Fase 2 — corte mecânico (silêncio, recomeço, loudness) com a abertura
       em preto e branco + transição (fase1/pipeline_cortes.py)
    4. acabamento de cada corte (finalizar.py): crop dinâmico 9:16 que segue
       quem fala, legenda, GC, marca d'água, censura, trilha e capa

Os cortes prontos ficam em "Revisar": assiste, ajusta o título e o canal, e
manda para a fila do Publicador (outro serviço, na mesma máquina) — ou
descarta. Nada é publicado sem passar pela revisão.

A mesma API que a página usa serve para automatizar depois (ex.: um bot do
Telegram mandando links): POST /api/trabalhos com JSON {"url", "canal", "perfil"}.

Estado em ESTUDIO_DATA (padrão C:\\VideoMaker\\estudio):
    trabalhos.json          a lista de trabalhos e dos cortes de cada um
    config.json             canal -> perfil visual, endereço do Publicador, pastas
    trabalhos/<id>/         entrada/, fase1/, cortes/, final/<corte>/, trabalho.log
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import yaml
from flask import Flask, jsonify, make_response, render_template, request, send_from_directory

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
FASE1 = RAIZ / "fase1"
sys.path.insert(0, str(AQUI))

import finalizar  # noqa: E402  (também põe RAIZ e fase1 no sys.path)
import ai_srt  # noqa: E402
from utils import parse_csv_moments  # noqa: E402

DATA_DIR = Path(os.environ.get("ESTUDIO_DATA") or r"C:\VideoMaker\estudio")
TRABALHOS_DIR = DATA_DIR / "trabalhos"
ESTADO_PATH = DATA_DIR / "trabalhos.json"
CONFIG_PATH = DATA_DIR / "config.json"
YTDLP = RAIZ / "tools" / "yt-dlp.exe"
EXTENSOES = {".mp4", ".mov", ".mkv", ".avi", ".webm"}
TAMANHO_MAXIMO = 12 * 1024 * 1024 * 1024

CONFIG_PADRAO = {
    "publicador_url": "http://127.0.0.1:8080",
    "pasta_transicoes": str(DATA_DIR / "transicoes"),
    "canal_perfil": {},             # canal do Publicador -> perfil visual (logo/cores)
}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = TAMANHO_MAXIMO
_TRAVA = threading.RLock()
_cancelar: set[str] = set()        # ids de trabalhos com cancelamento pedido
_processo: subprocess.Popen | None = None
log = logging.getLogger("estudio")


# ──────────────────────────────────────────────────────────────────
#  Estado em disco
# ──────────────────────────────────────────────────────────────────

def _ler_json(caminho: Path, padrao):
    """Lê o estado. Só devolve o padrão se o arquivo NÃO EXISTE.

    No Windows, ler no mesmo instante em que o arquivo é trocado (o .tmp
    substituindo o original) dá "arquivo em uso" — tratar isso como "vazio"
    fazia a API responder "corte não existe" (medido em 25/09/2026) e, pior,
    quem gravasse em cima dessa leitura apagaria todos os trabalhos. Então
    tenta de novo, e se não conseguir, falha em vez de fingir que está vazio.
    """
    for tentativa in range(20):
        try:
            return json.loads(caminho.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return deepcopy(padrao)
        except (OSError, ValueError):
            if tentativa == 19:
                raise
            time.sleep(0.05)


def _gravar_json(caminho: Path, dados) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    tmp.write_text(json.dumps(dados, ensure_ascii=False, indent=2), encoding="utf-8")
    for tentativa in range(20):
        try:
            tmp.replace(caminho)
            return
        except PermissionError:  # alguém lendo o arquivo neste instante (Windows)
            if tentativa == 19:
                raise
            time.sleep(0.05)


def carregar_trabalhos() -> list[dict]:
    # A trava (reentrante) também na leitura: dentro deste processo, ler nunca
    # cruza com a troca do arquivo por uma gravação.
    with _TRAVA:
        return _ler_json(ESTADO_PATH, [])


def salvar_trabalhos(trabalhos: list[dict]) -> None:
    with _TRAVA:
        _gravar_json(ESTADO_PATH, trabalhos)


def carregar_config() -> dict:
    return {**deepcopy(CONFIG_PADRAO), **_ler_json(CONFIG_PATH, {})}


def salvar_config(cfg: dict) -> None:
    _gravar_json(CONFIG_PATH, cfg)


def _atualizar(tid: str, **campos) -> dict | None:
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        for t in trabalhos:
            if t["id"] == tid:
                t.update(campos)
                salvar_trabalhos(trabalhos)
                return t
    return None


def _atualizar_corte(tid: str, cid: str, **campos) -> dict | None:
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        for t in trabalhos:
            if t["id"] == tid:
                for c in t.get("cortes", []):
                    if c["id"] == cid:
                        c.update(campos)
                        salvar_trabalhos(trabalhos)
                        return c
    return None


def _pasta(tid: str) -> Path:
    return TRABALHOS_DIR / Path(tid).name


def _registrar(tid: str, texto: str) -> None:
    with (_pasta(tid) / "trabalho.log").open("a", encoding="utf-8") as f:
        f.write(f"{datetime.now():%H:%M:%S}  {texto}\n")


class _LogDoTrabalho(logging.Handler):
    """Manda o logging dos módulos (finalizar, crop, falantes) para o log do
    trabalho em andamento — o worker é uma thread só, então há no máximo um."""
    atual: str | None = None

    def emit(self, registro: logging.LogRecord) -> None:
        if self.atual:
            try:
                _registrar(self.atual, registro.getMessage())
            except OSError:
                pass


_handler = _LogDoTrabalho()
for nome in ("estudio", "fase1"):
    logging.getLogger(nome).addHandler(_handler)
    logging.getLogger(nome).setLevel(logging.INFO)


# ──────────────────────────────────────────────────────────────────
#  Etapas
# ──────────────────────────────────────────────────────────────────

class Cancelado(Exception):
    pass


def _checar_cancelamento(tid: str) -> None:
    if tid in _cancelar:
        raise Cancelado()


def _rodar(tid: str, cmd: list[str]) -> None:
    """Roda um comando, copiando a saída para o log do trabalho; cancela se pedido."""
    global _processo
    _registrar(tid, "$ " + " ".join(Path(c).name if i == 0 else c for i, c in enumerate(cmd)))
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                          text=True, encoding="utf-8", errors="replace", cwd=str(FASE1)) as p:
        _processo = p
        try:
            for linha in p.stdout:
                linha = linha.rstrip()
                if linha and "%|" not in linha:        # pula barras de progresso
                    _registrar(tid, "   " + linha[:500])
                if tid in _cancelar:
                    p.kill()
                    raise Cancelado()
            p.wait()
        finally:
            _processo = None
    if p.returncode != 0:
        programa = Path(cmd[1] if Path(cmd[0]).stem.lower().startswith("python") else cmd[0]).name
        raise RuntimeError(f"{programa} terminou com erro (código {p.returncode}) — veja o log")


def _baixar(tid: str, url: str, destino: Path) -> Path:
    destino.mkdir(parents=True, exist_ok=True)
    # Mesmas opções do programa de desktop (download_video): legenda do próprio
    # YouTube em pt ao lado do vídeo — a Fase 1 usa ela e pula o Whisper do
    # vídeo inteiro. Clientes mweb/tv_simply: o "default" dava HTTP 403 (ver
    # app.py); precisam do deno no PATH (servico_windows/ambiente.cmd).
    # --progress-delta: uma linha de progresso a cada 20 s, não a cada pedaço.
    cmd = [str(YTDLP), "--no-playlist", "--match-filter", "!is_live", "--progress-delta", "20",
           "--extractor-args", "youtube:player_client=mweb,tv_simply",
           "--retries", "10", "--fragment-retries", "10", "--extractor-retries", "3",
           "-N", "8", "-f", "bv*[height<=1080]+ba/b", "--merge-output-format", "mp4",
           "-P", str(destino), "-o", "%(title).150B.%(ext)s",
           "--write-subs", "--write-auto-subs", "--sub-langs", "pt-BR,pt,pt-orig",
           "--sub-format", "ttml/best", "--convert-subs", "srt", "--no-abort-on-error", url]
    _rodar(tid, cmd)
    videos = [p for p in destino.iterdir() if p.suffix.lower() in EXTENSOES]
    if not videos:
        raise RuntimeError("o download terminou mas nenhum vídeo apareceu na pasta")
    return max(videos, key=lambda p: p.stat().st_size)


def _blocos_do_csv(video: Path, csv_path: Path, destino: Path) -> Path:
    momentos = parse_csv_moments(csv_path)
    if not momentos:
        raise RuntimeError("o CSV não tem nenhuma linha válida (colunas inicio, fim, titulo)")
    blocos = [{"id": f"corte_{i:02d}", "rank": i, "inicio": m["start_s"], "fim": m["end_s"],
               "duracao": m["end_s"] - m["start_s"], "gancho": m.get("label", ""),
               "subtitulo": m.get("subtitulo", ""), "musica": m.get("musica", ""),
               "comentario": m.get("comentario", "")}
              for i, m in enumerate(momentos, 1)]
    destino.mkdir(parents=True, exist_ok=True)
    caminho = destino / "blocos_finais.json"
    _gravar_json(caminho, {"video": str(video), "origem": "csv", "blocos": blocos})
    return caminho


def _config_cortes(tid: str) -> Path:
    """config_cortes.yaml do fase1, com a pasta de sons de transição desta máquina."""
    cfg = yaml.safe_load((FASE1 / "config_cortes.yaml").read_text(encoding="utf-8"))
    cfg["transicao"]["pasta_sons"] = carregar_config()["pasta_transicoes"]
    caminho = _pasta(tid) / "config_cortes.yaml"
    caminho.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return caminho


def processar_trabalho(t: dict) -> None:
    tid = t["id"]
    pasta = _pasta(tid)
    py = sys.executable

    # 1. vídeo de entrada
    entrada = pasta / "entrada"
    video = next((p for p in entrada.glob("*") if p.suffix.lower() in EXTENSOES), None) \
        if entrada.exists() else None
    if video is None:
        if not t.get("url"):
            raise RuntimeError("trabalho sem vídeo e sem link")
        _atualizar(tid, etapa="baixando o vídeo")
        video = _baixar(tid, t["url"], entrada)
    _atualizar(tid, titulo_video=video.stem)
    _checar_cancelamento(tid)

    # 2. trechos: Fase 1 (automático) ou CSV
    if t["tipo"] == "csv":
        blocos_path = _blocos_do_csv(video, entrada / t["csv"], pasta / "fase1")
    else:
        _atualizar(tid, etapa="escolhendo os melhores trechos (Fase 1)")
        _rodar(tid, [py, str(FASE1 / "pipeline.py"), str(video), "--workspace", str(pasta / "fase1")])
        blocos_path = pasta / "fase1" / "blocos_finais.json"
    blocos = json.loads(blocos_path.read_text(encoding="utf-8")).get("blocos") or []
    if not blocos:
        _atualizar(tid, etapa="", mensagem="Nenhum trecho forte o bastante neste vídeo.")
        return
    _registrar(tid, f"{len(blocos)} trecho(s) para cortar")
    _checar_cancelamento(tid)

    # 3. Fase 2: corte mecânico + abertura em preto e branco — só dos trechos
    # que ainda não têm corte pronto (retomada depois de cair no meio)
    feitos = {c["id"] for c in t.get("cortes", [])}
    pendentes = [b for b in blocos if b["id"] not in feitos
                 and not (pasta / "cortes" / f"{b['id']}.mp4").exists()]
    if pendentes:
        _atualizar(tid, etapa=f"cortando {len(pendentes)} trecho(s) (silêncio, abertura cinza)")
        dados = json.loads(blocos_path.read_text(encoding="utf-8"))
        dados["blocos"] = pendentes
        pendentes_path = pasta / "blocos_pendentes.json"
        _gravar_json(pendentes_path, dados)
        _rodar(tid, [py, str(FASE1 / "pipeline_cortes.py"), str(pendentes_path),
                     "--saida", str(pasta / "cortes"), "--config", str(_config_cortes(tid))])

    # 4. acabamento de cada corte
    cfg_crop = yaml.safe_load((FASE1 / "config_crop.yaml").read_text(encoding="utf-8"))
    perfil = finalizar.carregar_perfil(t["perfil"])
    musicas = ai_srt.load_config().get("music_dir")
    pasta_trilhas = Path(musicas) if musicas else None
    csv = t["tipo"] == "csv"
    for i, bloco in enumerate(blocos, 1):
        clipe = pasta / "cortes" / f"{bloco['id']}.mp4"
        if bloco["id"] in feitos or not clipe.exists():
            continue
        _checar_cancelamento(tid)
        _atualizar(tid, etapa=f"acabamento {i}/{len(blocos)}: crop, legenda, GC")
        _registrar(tid, f"── {bloco['id']}: {bloco.get('gancho', '')}")
        try:
            meta = finalizar.finalizar_corte(
                clipe, pasta / "final" / bloco["id"], perfil, cfg_crop,
                contexto=bloco.get("comentario", ""),
                # CSV: coluna titulo = chapéu do GC, subtitulo = manchete (como no app).
                # Automático: o gancho da Fase 1 é a manchete de reserva.
                titulo_sugerido=bloco.get("gancho", "") if csv else "",
                manchete_sugerida=bloco.get("subtitulo", "") if csv else bloco.get("gancho", ""),
                musica_sugerida=bloco.get("musica", ""), preferir_sugeridos=csv,
                pasta_trilhas=pasta_trilhas)
            corte = {"id": bloco["id"], "status": "revisar", "canal": t["canal"],
                     "titulo_publicacao": (meta["subtitulo"] or bloco.get("gancho") or bloco["id"])[:100],
                     "gancho": bloco.get("gancho", ""), "comentario": bloco.get("comentario", ""),
                     "nota": bloco.get("nota_final"), **meta}
        except Cancelado:
            raise
        except Exception as exc:  # noqa: BLE001 — um corte falhar não derruba os outros
            _registrar(tid, f"⚠️ {bloco['id']} falhou no acabamento: {exc}")
            corte = {"id": bloco["id"], "status": "erro", "mensagem": str(exc)[:400],
                     "gancho": bloco.get("gancho", "")}
        with _TRAVA:
            trabalhos = carregar_trabalhos()
            for tt in trabalhos:
                if tt["id"] == tid:
                    tt.setdefault("cortes", []).append(corte)
            salvar_trabalhos(trabalhos)
        # o clipe da Fase 2 já virou final.mp4; apagar economiza disco
        clipe.unlink(missing_ok=True)


def _laco() -> None:
    while True:
        try:
            with _TRAVA:
                proximo = next((t for t in carregar_trabalhos() if t["status"] == "aguardando"), None)
                if proximo:
                    _atualizar(proximo["id"], status="processando", etapa="começando",
                               iniciado_em=datetime.now().isoformat(timespec="seconds"))
            if proximo is None:
                time.sleep(3)
                continue
            tid = proximo["id"]
            _LogDoTrabalho.atual = tid
            _registrar(tid, "▶ começou")
            try:
                processar_trabalho(proximo)
                _atualizar(tid, status="pronto", etapa="",
                           terminado_em=datetime.now().isoformat(timespec="seconds"))
                _registrar(tid, "✅ pronto")
            except Cancelado:
                _atualizar(tid, status="cancelado", etapa="")
                _registrar(tid, "⏹ cancelado")
            except Exception as exc:  # noqa: BLE001
                _atualizar(tid, status="erro", etapa="", mensagem=str(exc)[:500])
                _registrar(tid, f"❌ {exc}")
            finally:
                _cancelar.discard(tid)
                _LogDoTrabalho.atual = None
        except Exception as exc:  # noqa: BLE001 — o laço nunca morre
            log.error("laço: %s", exc)
            time.sleep(5)


# ──────────────────────────────────────────────────────────────────
#  Publicador (outro serviço, mesma máquina)
# ──────────────────────────────────────────────────────────────────

def _publicador_estado() -> dict:
    import requests
    try:
        r = requests.get(carregar_config()["publicador_url"] + "/api/estado", timeout=4)
        return r.json()
    except Exception:  # noqa: BLE001
        return {}


def _nome_arquivo(titulo: str) -> str:
    """O Publicador usa o nome do arquivo como título do vídeo no YouTube."""
    nome = re.sub(r'[\x00-\x1f<>"/\\|?*]+', " ", titulo)
    nome = re.sub(r"\s*:\s*", " - ", nome)          # "Teste: x" -> "Teste - x"
    nome = re.sub(r"\s+", " ", nome).strip(" .")
    return (nome[:100] or "corte") + ".mp4"


# ──────────────────────────────────────────────────────────────────
#  Rotas
# ──────────────────────────────────────────────────────────────────

@app.get("/")
def pagina():
    resposta = make_response(render_template("index.html"))
    resposta.headers["Cache-Control"] = "no-store"
    return resposta


@app.get("/api/estado")
def estado():
    pub = _publicador_estado()
    cfg = carregar_config()
    return jsonify({
        "trabalhos": list(reversed(carregar_trabalhos())),
        "perfis": finalizar.perfis(),
        "perfil_padrao": ai_srt.get_current_profile_name(),   # o perfil ativo no app
        "canais": [c["nome"] for c in pub.get("canais", [])],
        "publicador_ok": bool(pub),
        "publicador_porta": cfg["publicador_url"].rsplit(":", 1)[-1],
        "canal_perfil": cfg["canal_perfil"],
    })


def _novo_id() -> str:
    return f"{datetime.now():%Y%m%d-%H%M%S}-{uuid4().hex[:4]}"


@app.post("/api/trabalhos")
def novo_trabalho():
    """Link ou arquivo de vídeo (+ CSV no lote). Aceita formulário ou JSON."""
    dados = request.get_json(silent=True) or request.form
    url = str(dados.get("url") or "").strip()
    canal = str(dados.get("canal") or "").strip()
    perfil = str(dados.get("perfil") or "").strip()
    arquivo = request.files.get("video")
    csv = request.files.get("csv")
    if not url and not (arquivo and arquivo.filename):
        return jsonify({"erro": "Cole um link do YouTube ou escolha um arquivo de vídeo."}), 400
    if url and not re.match(r"https?://", url):
        return jsonify({"erro": "O link precisa começar com http:// ou https://"}), 400
    if perfil not in finalizar.perfis():
        return jsonify({"erro": f"Perfil visual '{perfil}' não existe."}), 400

    tid = _novo_id()
    entrada = _pasta(tid) / "entrada"
    entrada.mkdir(parents=True, exist_ok=True)
    t = {"id": tid, "criado_em": datetime.now().isoformat(timespec="seconds"), "tipo": "auto",
         "url": url, "perfil": perfil, "canal": canal, "status": "aguardando", "etapa": "",
         "mensagem": "", "cortes": [], "titulo_video": url}
    if arquivo and arquivo.filename:
        nome = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', "_", Path(arquivo.filename).name)
        if Path(nome).suffix.lower() not in EXTENSOES:
            shutil.rmtree(_pasta(tid), ignore_errors=True)
            return jsonify({"erro": "Formato de vídeo não aceito."}), 400
        arquivo.save(entrada / nome)
        t["titulo_video"] = Path(nome).stem
    if csv and csv.filename:
        csv.save(entrada / "cortes.csv")
        t["tipo"], t["csv"] = "csv", "cortes.csv"
        try:
            n = len(parse_csv_moments(entrada / "cortes.csv"))
        except Exception as exc:  # noqa: BLE001
            shutil.rmtree(_pasta(tid), ignore_errors=True)
            return jsonify({"erro": f"CSV inválido: {exc}"}), 400
        if n == 0:
            shutil.rmtree(_pasta(tid), ignore_errors=True)
            return jsonify({"erro": "O CSV não tem nenhuma linha válida."}), 400
    _registrar(tid, f"criado ({t['tipo']}) — {url or t['titulo_video']}")
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        trabalhos.append(t)
        salvar_trabalhos(trabalhos)
        if canal:  # lembra o perfil usado com este canal
            cfg = carregar_config()
            cfg["canal_perfil"][canal] = perfil
            salvar_config(cfg)
    return jsonify({"id": tid})


@app.post("/api/trabalhos/<tid>/cancelar")
def cancelar(tid: str):
    with _TRAVA:
        t = next((t for t in carregar_trabalhos() if t["id"] == tid), None)
        if t is None:
            return jsonify({"erro": "Trabalho não existe."}), 404
        if t["status"] == "aguardando":
            _atualizar(tid, status="cancelado")
        elif t["status"] == "processando":
            _cancelar.add(tid)
    return jsonify({"ok": True})


@app.post("/api/trabalhos/<tid>/repetir")
def repetir(tid: str):
    """Põe de novo na fila (retoma: a Fase 1 reaproveita o que já tinha feito)."""
    t = _atualizar(tid, status="aguardando", etapa="", mensagem="")
    return (jsonify({"ok": True}) if t else (jsonify({"erro": "Trabalho não existe."}), 404))


@app.post("/api/trabalhos/<tid>/remover")
def remover(tid: str):
    with _TRAVA:
        trabalhos = carregar_trabalhos()
        t = next((t for t in trabalhos if t["id"] == tid), None)
        if t is None:
            return jsonify({"erro": "Trabalho não existe."}), 404
        if t["status"] == "processando":
            return jsonify({"erro": "Cancele antes de remover."}), 409
        salvar_trabalhos([x for x in trabalhos if x["id"] != tid])
    shutil.rmtree(_pasta(tid), ignore_errors=True)
    return jsonify({"ok": True})


@app.get("/api/trabalhos/<tid>/log")
def ver_log(tid: str):
    caminho = _pasta(tid) / "trabalho.log"
    linhas = caminho.read_text(encoding="utf-8", errors="replace").splitlines()[-400:] \
        if caminho.exists() else []
    return jsonify({"linhas": linhas})


@app.get("/midia/<tid>/<cid>/<arquivo>")
def midia(tid: str, cid: str, arquivo: str):
    return send_from_directory(_pasta(tid) / "final" / Path(cid).name, Path(arquivo).name,
                               conditional=True, max_age=0)


@app.post("/api/cortes/<tid>/<cid>")
def editar_corte(tid: str, cid: str):
    dados = request.get_json(silent=True) or {}
    campos = {}
    if "titulo_publicacao" in dados:
        campos["titulo_publicacao"] = str(dados["titulo_publicacao"]).strip()[:100]
    if "canal" in dados:
        campos["canal"] = str(dados["canal"]).strip()
    c = _atualizar_corte(tid, cid, **campos)
    return jsonify({"ok": True}) if c else (jsonify({"erro": "Corte não existe."}), 404)


@app.post("/api/cortes/<tid>/<cid>/descartar")
def descartar(tid: str, cid: str):
    c = _atualizar_corte(tid, cid, status="descartado")
    return jsonify({"ok": True}) if c else (jsonify({"erro": "Corte não existe."}), 404)


@app.post("/api/cortes/<tid>/<cid>/voltar")
def voltar(tid: str, cid: str):
    c = _atualizar_corte(tid, cid, status="revisar")
    return jsonify({"ok": True}) if c else (jsonify({"erro": "Corte não existe."}), 404)


@app.post("/api/cortes/<tid>/<cid>/enviar")
def enviar(tid: str, cid: str):
    """Manda o corte para a fila do Publicador (que publica no YouTube/TikTok)."""
    import requests
    t = next((t for t in carregar_trabalhos() if t["id"] == tid), None)
    c = next((c for c in (t or {}).get("cortes", []) if c["id"] == cid), None)
    if c is None:
        return jsonify({"erro": "Corte não existe."}), 404
    if c.get("status") != "revisar":
        return jsonify({"erro": "Este corte não está esperando revisão."}), 409
    if not c.get("canal"):
        return jsonify({"erro": "Escolha o canal antes de enviar."}), 400
    final = _pasta(tid) / "final" / cid / c["arquivo"]
    nome = _nome_arquivo(c.get("titulo_publicacao") or cid)
    try:
        with final.open("rb") as f:
            r = requests.post(carregar_config()["publicador_url"] + "/api/videos",
                              data={"canal": c["canal"]}, files={"arquivos": (nome, f, "video/mp4")},
                              timeout=600)
        resposta = r.json()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"erro": f"Publicador fora do ar? {exc}"}), 502
    if r.status_code != 200 or not resposta.get("adicionados"):
        return jsonify({"erro": resposta.get("erro") or f"Publicador respondeu {r.status_code}"}), 502
    _atualizar_corte(tid, cid, status="na_fila", arquivo_publicador=resposta["adicionados"][0],
                     enviado_em=datetime.now().isoformat(timespec="seconds"))
    _registrar(tid, f"📤 {cid} foi para a fila do Publicador ({c['canal']}): {resposta['adicionados'][0]}")
    return jsonify({"ok": True, "arquivo": resposta["adicionados"][0]})


@app.errorhandler(413)
def grande_demais(_erro):
    return jsonify({"erro": "Arquivo maior que 12 GB."}), 413


# Um processo só (waitress com threads) → exatamente um laço de trabalho, e a
# GPU nunca é disputada por dois trabalhos.
TRABALHOS_DIR.mkdir(parents=True, exist_ok=True)
with _TRAVA:
    _ts = carregar_trabalhos()
    for _t in _ts:
        if _t["status"] == "processando":   # o serviço caiu no meio: retoma
            _t["status"], _t["etapa"] = "aguardando", "retomando depois de reiniciar"
    salvar_trabalhos(_ts)
threading.Thread(target=_laco, daemon=True, name="trabalhos").start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("ESTUDIO_PORTA", 8090)), threaded=True)
