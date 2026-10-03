"""SQLite: esquema e funções de acesso. Colunas *_json guardam JSON em texto."""
import json
import sqlite3
import threading
from datetime import datetime

from . import config

_lock = threading.RLock()

ESQUEMA = """
CREATE TABLE IF NOT EXISTS canais (
    id INTEGER PRIMARY KEY,
    nome TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    idioma TEXT NOT NULL DEFAULT 'pt-BR',
    descricao TEXT NOT NULL DEFAULT '',
    formato TEXT NOT NULL DEFAULT 'short',
    config_json TEXT NOT NULL DEFAULT '{}',
    criado_em TEXT, atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS projetos (
    id INTEGER PRIMARY KEY,
    canal_id INTEGER NOT NULL REFERENCES canais(id),
    assunto TEXT NOT NULL,
    slug TEXT NOT NULL,
    pasta TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'criado',
    automatico INTEGER NOT NULL DEFAULT 0,
    seed_base INTEGER NOT NULL DEFAULT 777,
    canal_json TEXT NOT NULL DEFAULT '{}',
    historia_json TEXT,
    avaliacao_json TEXT,
    ressalva TEXT,
    trilha TEXT,
    erro TEXT,
    criado_em TEXT, atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS historias (
    id INTEGER PRIMARY KEY,
    projeto_id INTEGER NOT NULL REFERENCES projetos(id),
    versao INTEGER NOT NULL,
    origem TEXT NOT NULL,
    historia_json TEXT NOT NULL,
    checagem_json TEXT,
    avaliacao_json TEXT,
    nota_geral REAL,
    passou INTEGER,
    criado_em TEXT
);
CREATE TABLE IF NOT EXISTS cenas (
    id INTEGER PRIMARY KEY,
    projeto_id INTEGER NOT NULL REFERENCES projetos(id),
    n INTEGER NOT NULL,
    arquivo TEXT,
    seed INTEGER,
    versao INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pendente',
    erro TEXT,
    prompt_final TEXT,
    inicio REAL, fim REAL,
    UNIQUE(projeto_id, n)
);
CREATE TABLE IF NOT EXISTS custos (
    id INTEGER PRIMARY KEY,
    projeto_id INTEGER,
    etapa TEXT NOT NULL,
    servico TEXT NOT NULL,
    modelo TEXT,
    valor_usd REAL NOT NULL,
    real INTEGER NOT NULL DEFAULT 0,
    detalhe TEXT,
    criado_em TEXT
);
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY,
    projeto_id INTEGER,
    nivel TEXT NOT NULL DEFAULT 'info',
    msg TEXT NOT NULL,
    criado_em TEXT
);
CREATE TABLE IF NOT EXISTS biblioteca (
    id INTEGER PRIMARY KEY,
    canal_id INTEGER NOT NULL REFERENCES canais(id),
    tipo TEXT NOT NULL,          -- personagem | ambiente
    chave TEXT NOT NULL,
    descricao_fixa TEXT NOT NULL,
    arquivo TEXT,
    url TEXT,
    url_em TEXT,
    criado_em TEXT,
    UNIQUE(canal_id, tipo, chave)
);
CREATE TABLE IF NOT EXISTS metricas (
    projeto_id INTEGER PRIMARY KEY,
    youtube_id TEXT NOT NULL,
    atualizado_em TEXT,
    dados_json TEXT
);
CREATE TABLE IF NOT EXISTS trilhas (
    id INTEGER PRIMARY KEY,
    canal_id INTEGER NOT NULL REFERENCES canais(id),
    arquivo TEXT NOT NULL,
    nome TEXT NOT NULL,
    ativa INTEGER NOT NULL DEFAULT 1,
    criado_em TEXT
);
"""


def agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, check_same_thread=False, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


def iniciar():
    with _lock, conectar() as con:
        con.executescript(ESQUEMA)
        con.execute("PRAGMA journal_mode = WAL")
        # Migrações de bancos criados antes das colunas novas.
        colunas = {r["name"] for r in con.execute("PRAGMA table_info(projetos)")}
        if "youtube_id" not in colunas:
            con.execute("ALTER TABLE projetos ADD COLUMN youtube_id TEXT")
        if "publicador_json" not in colunas:
            con.execute("ALTER TABLE projetos ADD COLUMN publicador_json TEXT")
        if "modo" not in colunas:
            con.execute("ALTER TABLE projetos ADD COLUMN modo TEXT")
        colunas = {r["name"] for r in con.execute("PRAGMA table_info(biblioteca)")}
        if "estilo" not in colunas:
            con.execute("ALTER TABLE biblioteca ADD COLUMN estilo TEXT")
        if "projeto_id" not in colunas:
            con.execute("ALTER TABLE biblioteca ADD COLUMN projeto_id INTEGER")


def todos(sql: str, params=()) -> list[dict]:
    with _lock, conectar() as con:
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def um(sql: str, params=()) -> dict | None:
    with _lock, conectar() as con:
        r = con.execute(sql, params).fetchone()
        return dict(r) if r else None


def executar(sql: str, params=()) -> int:
    with _lock, conectar() as con:
        cur = con.execute(sql, params)
        return cur.lastrowid


def atualizar(tabela: str, id_: int, **campos):
    if not campos:
        return
    if tabela in ("projetos", "canais"):
        campos["atualizado_em"] = agora()
    cols = ", ".join(f"{k} = ?" for k in campos)
    vals = [json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v for v in campos.values()]
    executar(f"UPDATE {tabela} SET {cols} WHERE id = ?", (*vals, id_))


def carregar_json(txt, padrao=None):
    if txt is None or txt == "":
        return padrao
    try:
        return json.loads(txt)
    except (TypeError, ValueError):
        return padrao


def evento(projeto_id: int | None, msg: str, nivel: str = "info"):
    executar("INSERT INTO eventos (projeto_id, nivel, msg, criado_em) VALUES (?, ?, ?, ?)",
             (projeto_id, nivel, msg, agora()))
    print(f"[{nivel}] projeto {projeto_id}: {msg}", flush=True)
    from . import registro
    registro.escrever(projeto_id, "EVENTO", msg, nivel)
