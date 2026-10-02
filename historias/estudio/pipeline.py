"""Orquestra as etapas. Uma fila em segundo plano processa uma ação por vez; cada etapa grava o resultado
no banco e em disco, então dá para parar, revisar e retomar.

Estados:  na_fila -> escrevendo -> revisando_texto -> aguardando_historia (parada 1)
          -> gerando_imagens -> aguardando_imagens (parada 2) -> narrando -> montando -> pronto
          erro | interrompido (servidor reiniciou no meio) -> "continuar" retoma da etapa certa.
No modo automático as paradas são puladas. A história reprovada numa pergunta com trava (segurança etc.) recomeça
com outra vibe até RECOMECOS_TRAVA vezes; se ainda assim reprovar, para na revisão.
"""
import json
import queue
import re
import random
import shutil
import threading
import traceback
from datetime import date
from pathlib import Path

from . import canais, config, custos, db, efeitos, historia, montagem, narracao, registro, revisao, visual

_fila: "queue.Queue[tuple[int, str, dict]]" = queue.Queue()
_trabalhador: threading.Thread | None = None
EM_ANDAMENTO = ("na_fila", "escrevendo", "revisando_texto", "decupando", "gerando_imagens", "refazendo", "narrando",
                "montando")


def iniciar():
    global _trabalhador
    for p in db.todos(f"SELECT id, status FROM projetos WHERE status IN ({','.join('?' * len(EM_ANDAMENTO))})",
                      EM_ANDAMENTO):
        db.atualizar("projetos", p["id"], status="interrompido", erro=f"servidor reiniciado durante '{p['status']}'")
    if _trabalhador is None or not _trabalhador.is_alive():
        _trabalhador = threading.Thread(target=_laco, daemon=True, name="pipeline")
        _trabalhador.start()


def enfileirar(projeto_id: int, acao: str, **args):
    if acao != "refazer":
        db.atualizar("projetos", projeto_id, status="na_fila", erro=None)
    _fila.put((projeto_id, acao, args))


def _laco():
    while True:
        pid, acao, args = _fila.get()
        if not db.um("SELECT id FROM projetos WHERE id = ?", (pid,)):  # projeto excluído enquanto estava na fila
            _fila.task_done()
            continue
        try:
            {"historia": etapa_historia, "imagens": etapa_imagens, "refazer": etapa_refazer, "decupar": etapa_decupar,
             "video": etapa_video}[acao](pid, **args)
        except custos.OrcamentoEstourado as e:
            db.evento(pid, str(e), "erro")
            db.atualizar("projetos", pid, status="erro", erro=str(e))
        except Exception as e:
            traceback.print_exc()
            registro.escrever(pid, "ERRO", f"{acao}: {type(e).__name__}", "erro",
                              dados={"traceback": traceback.format_exc()})
            db.evento(pid, f"{type(e).__name__}: {e}", "erro")
            db.atualizar("projetos", pid, status="erro", erro=f"{acao}: {e}")
        finally:
            _gravar_estado(pid)
            _fila.task_done()


# ---------------------------------------------------------------- projetos

def obter(pid: int) -> dict | None:
    p = db.um("SELECT * FROM projetos WHERE id = ?", (pid,))
    if not p:
        return None
    p["historia"] = db.carregar_json(p.pop("historia_json"))
    p["avaliacao"] = db.carregar_json(p.pop("avaliacao_json"))
    p["canal_snapshot"] = db.carregar_json(p.pop("canal_json"), {})
    return p


def criar(canal_id: int, assunto: str, automatico: bool = False) -> int:
    canal = canais.obter(canal_id)
    if not canal:
        raise ValueError("Canal não encontrado")
    assunto = assunto.strip()
    if not assunto:
        raise ValueError("Informe o assunto")
    slug = canais.slugify(assunto)[:40]
    pid = db.executar("INSERT INTO projetos (canal_id, assunto, slug, pasta, status, automatico, seed_base, canal_json, "
                      "criado_em, atualizado_em) VALUES (?, ?, ?, '', 'na_fila', ?, ?, ?, ?, ?)",
                      (canal_id, assunto, slug, int(automatico), random.randint(100000, 999999),
                       json.dumps(canal, ensure_ascii=False, default=str), db.agora(), db.agora()))
    pasta = config.PROJETOS / f"{date.today().isoformat()}_{pid:04d}_{slug}"
    (pasta / "cenas").mkdir(parents=True, exist_ok=True)
    db.atualizar("projetos", pid, pasta=pasta.relative_to(config.BASE).as_posix())
    db.evento(pid, f"Projeto criado no canal {canal['nome']}: \"{assunto}\"{' (automático)' if automatico else ''}")
    enfileirar(pid, "historia")
    return pid


def _pasta_segura(p: dict) -> Path | None:
    """Pasta do projeto, só se estiver mesmo dentro de projetos/ (nunca apaga nada fora dela)."""
    if not p.get("pasta"):
        return None
    pasta = (config.BASE / p["pasta"]).resolve()
    raiz = config.PROJETOS.resolve()
    if pasta == raiz or not pasta.is_relative_to(raiz):
        return None
    return pasta


def recomecar(pid: int):
    """Apaga história, versões, imagens, narração e vídeo e escreve tudo de novo com o mesmo assunto.
    Custos já gastos continuam registrados; fichas e placas da biblioteca do canal ficam."""
    p = obter(pid)
    if not p:
        raise ValueError("Projeto não encontrado")
    if p["status"] in EM_ANDAMENTO:
        raise ValueError("O projeto está em processamento; espere terminar")
    db.executar("DELETE FROM historias WHERE projeto_id = ?", (pid,))
    db.executar("DELETE FROM cenas WHERE projeto_id = ?", (pid,))
    db.executar("UPDATE projetos SET historia_json = NULL, avaliacao_json = NULL, ressalva = NULL, erro = NULL, "
                "trilha = NULL, seed_base = ? WHERE id = ?", (random.randint(100000, 999999), pid))
    pasta = _pasta_segura(p)
    if pasta and pasta.exists():
        for item in pasta.iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        (pasta / "cenas").mkdir(exist_ok=True)
    db.evento(pid, "Recomeçado do zero: história, imagens, narração e vídeo apagados.")
    enfileirar(pid, "historia")


def refazer_imagens(pid: int):
    """Descarta fichas, placas e cenas deste projeto e gera tudo de novo (ex.: depois de trocar o estilo).
    A história fica como está; as imagens antigas continuam na biblioteca."""
    p = obter(pid)
    if not p or not p["historia"]:
        raise ValueError("O projeto ainda não tem história")
    if p["status"] in EM_ANDAMENTO:
        raise ValueError("O projeto está em processamento; espere terminar")
    h = p["historia"]
    for obj in h.get("personagens", []) + h.get("ambientes", []):
        obj.pop("bib_chave", None)
    db.executar("DELETE FROM cenas WHERE projeto_id = ?", (pid,))
    db.atualizar("projetos", pid, historia_json=h)
    pasta = _pasta_segura(p)
    if pasta:
        for sub in ("cenas", "clipes", "testes"):
            shutil.rmtree(pasta / sub, ignore_errors=True)
        (pasta / "cenas").mkdir(exist_ok=True)
        (pasta / "folha_contato.jpg").unlink(missing_ok=True)
        (pasta / "final.mp4").unlink(missing_ok=True)
    db.evento(pid, "Refazendo todas as imagens (fichas, placas e cenas) com o estilo atual do canal.")
    enfileirar(pid, "imagens")


def excluir(pid: int):
    p = obter(pid)
    if not p:
        raise ValueError("Projeto não encontrado")
    if p["status"] in EM_ANDAMENTO:
        raise ValueError("O projeto está em processamento; espere terminar")
    pasta = _pasta_segura(p)
    for tabela in ("eventos", "historias", "cenas"):
        db.executar(f"DELETE FROM {tabela} WHERE projeto_id = ?", (pid,))
    # Os gastos continuam no total geral, mas soltos do número do projeto (o SQLite pode reaproveitá-lo).
    db.executar("UPDATE custos SET projeto_id = NULL, detalhe = COALESCE(detalhe, '') || ? WHERE projeto_id = ?",
                (f" · projeto #{pid} excluído: {p['assunto'][:60]}", pid))
    db.executar("DELETE FROM metricas WHERE projeto_id = ?", (pid,))
    db.executar("DELETE FROM projetos WHERE id = ?", (pid,))
    registro.esquecer(pid)
    registro.escrever(pid, "EVENTO", f"Projeto excluído: \"{p['assunto']}\"")
    if pasta and pasta.exists():
        shutil.rmtree(pasta, ignore_errors=True)


def _gravar_estado(pid: int):
    p = obter(pid)
    if not p or not p["pasta"]:
        return
    estado = {"id": pid, "assunto": p["assunto"], "status": p["status"], "ressalva": p["ressalva"], "erro": p["erro"],
              "custos": custos.resumo(pid), "atualizado_em": p["atualizado_em"]}
    try:
        (config.BASE / p["pasta"] / "estado.json").write_text(json.dumps(estado, ensure_ascii=False, indent=2,
                                                                         default=str), encoding="utf-8")
    except OSError:
        pass


def _contexto(pid: int):
    p = obter(pid)
    canal = canais.obter(p["canal_id"])
    return p, canal


def continuar(pid: int):
    p = obter(pid)
    if not p["historia"]:
        enfileirar(pid, "historia")
    elif p["status"] in ("aguardando_historia",):
        enfileirar(pid, "imagens")
    else:
        faltam = db.um("SELECT COUNT(*) AS n FROM cenas WHERE projeto_id = ? AND status = 'ok'", (pid,))["n"]
        if faltam < len(p["historia"]["cenas"]):
            enfileirar(pid, "imagens")
        else:
            enfileirar(pid, "video")


# ---------------------------------------------------------------- etapas

def etapa_historia(pid: int):
    p, canal = _contexto(pid)
    db.atualizar("projetos", pid, status="escrevendo")
    db.executar("DELETE FROM cenas WHERE projeto_id = ?", (pid,))
    # Reprovada na trava (segurança ou outra pergunta que trava) não serve: recomeça do gancho com outra vibe.
    reprovadas: list[dict] = []
    total = config.RECOMECOS_TRAVA + 1
    while True:
        res = historia.laco(p, canal, reprovadas)
        falhas = historia.falhas_trava(res["avaliacao"])
        if not falhas or len(reprovadas) + 1 >= total:
            break
        reprovadas.append({"modo": p.get("modo"), "gancho": res.get("gancho") or res["historia"].get("gancho", ""),
                           "falhas": falhas})
        db.evento(pid, f"Reprovada na trava ({', '.join(falhas)}): recomeçando com outra vibe "
                       f"({len(reprovadas) + 1}/{total}).", "aviso")
    h, aval, ressalva = res["historia"], res["avaliacao"], res["ressalva"]
    db.evento(pid, f"História {'aprovada' if not ressalva else 'aprovada com ressalva: ' + ressalva} "
                   f"(versão {res['versao']}, nota {aval['nota_geral'] if aval else '-'})")

    db.atualizar("projetos", pid, status="revisando_texto")
    reprovada = bool(ressalva and ressalva.startswith("REPROVADA"))
    if not reprovada:
        antes = historia.narracao_completa(h)
        h, rel = revisao.humanizar(p, canal, h)
        db.evento(pid, f"Humanizer: {rel['motivo']}. Sinais antes: {len(rel['achados_antes'])}, "
                       f"depois: {len(rel['achados_depois'])}.")
        registro.escrever(pid, "HUMAN", rel["motivo"],
                          dados={"antes": antes, "depois": historia.narracao_completa(h),
                                 **{k: v for k, v in rel.items() if k != "rechecagem"}})
        if rel.get("aplicado"):
            checagem = {"problemas": [], "metricas": historia.checar_narrativa(h, canal)[1], "humanizer": rel}
            historia.salvar_versao(p, h, "humanizada", checagem, rel.get("rechecagem"))

    _decupar_e_salvar(p, canal, h, avaliacao_json=aval, ressalva=ressalva)
    if p["automatico"] and not reprovada:
        enfileirar(pid, "imagens")


def etapa_decupar(pid: int, narrativa: dict):
    """Texto corrido editado à mão na parada 1: divide em cenas de novo e volta para a sua revisão."""
    p, canal = _contexto(pid)
    h = historia.normalizar_narrativa(narrativa)
    historia.salvar_versao(p, h, "editada", {"problemas": [], "metricas": historia.checar_narrativa(h, canal)[1]}, None)
    _decupar_e_salvar(p, canal, h)
    db.evento(pid, "Texto editado dividido em cenas de novo.")


def _decupar_e_salvar(p: dict, canal: dict, h: dict, **campos):
    """Só com o texto final a história é dividida em cenas."""
    pid = p["id"]
    db.atualizar("projetos", pid, status="decupando")
    db.executar("DELETE FROM cenas WHERE projeto_id = ?", (pid,))
    h = historia.decupar(p, canal, h, historia.biblioteca_para_decupagem(canal))
    problemas, metricas = historia.checar(h, canal)
    if problemas:
        db.evento(pid, "Decupagem: " + "; ".join(problemas), "aviso")
    historia.salvar_versao(p, h, "decupagem", {"problemas": [], "metricas": metricas, "avisos": problemas}, None)
    fmt = config.FORMATOS.get(canal["formato"], config.FORMATOS["short"])
    efeitos.atribuir(h, canal, fmt)
    db.atualizar("projetos", pid, historia_json=h, status="aguardando_historia", **campos)


def etapa_imagens(pid: int):
    p, canal = _contexto(pid)
    db.atualizar("projetos", pid, status="gerando_imagens")
    h = visual.gerar_todas(p, canal, p["historia"])
    db.atualizar("projetos", pid, historia_json=h, status="aguardando_imagens")
    erros = db.um("SELECT COUNT(*) AS n FROM cenas WHERE projeto_id = ? AND status != 'ok'", (pid,))["n"]
    db.evento(pid, "Imagens prontas. Confira a folha de contato." if not erros else f"{erros} cena(s) sem imagem.")
    if p["automatico"] and not erros:
        enfileirar(pid, "video")


def etapa_refazer(pid: int, n: int):
    p, canal = _contexto(pid)
    anterior = p["status"]
    db.atualizar("projetos", pid, status="refazendo")
    try:
        visual.refazer(p, canal, p["historia"], n)
        db.evento(pid, f"Cena {n} refeita.")
    finally:
        db.atualizar("projetos", pid, status=anterior if anterior not in EM_ANDAMENTO else "aguardando_imagens")


def etapa_video(pid: int):
    p, canal = _contexto(pid)
    h = p["historia"]
    pasta = config.BASE / p["pasta"]
    db.atualizar("projetos", pid, status="narrando")
    voz = canal["config"].get("voz") or {}
    qwen = (voz.get("provedor") or "edge") == "qwen"
    db.evento(pid, f"Narrando com {voz.get('nome')} ({voz.get('modelo') if qwen else 'Edge TTS'})"
                   f"{', uma cena por vez' if qwen else ''}...")
    # O CTA do canal entra só na fala e na legenda da última cena; o texto aprovado da história não muda.
    ultima = h["cenas"][-1]
    fala = ultima["narracao"]
    cta = (canal["config"].get("cta") or "").strip()
    if cta and re.search(r"(siga|segue|inscreva|curta|compartilhe).*(canal|perfil|hist[óo]rias|v[ií]deos|mais)",
                          " ".join(historia.frases(fala)[-2:]), re.I):
        db.evento(pid, "A história já termina com um convite para seguir; o CTA do canal não foi acrescentado.", "aviso")
        cta = ""
    if cta:
        ultima["narracao"] = f"{fala} {cta if cta[-1] in '.!?' else cta + '.'}"
    try:
        narr = narracao.narrar(pasta, h["cenas"], voz, pid)
        if not qwen:
            custos.registrar(pid, "narracao", "edge-tts", voz.get("nome"), 0.0, True, f"{narr['duracao']:.1f}s")
        for t in narr["cenas"]:
            db.executar("UPDATE cenas SET inicio = ?, fim = ? WHERE projeto_id = ? AND n = ?", (t["inicio"], t["fim"], pid, t["n"]))
        db.evento(pid, f"Narração com {narr['duracao']:.1f}s{f' (CTA no fim: {cta})' if cta else ''}.")
        db.atualizar("projetos", pid, status="montando")
        saida = montagem.montar(obter(pid), canal, h, narr)
    finally:
        ultima["narracao"] = fala
    db.atualizar("projetos", pid, historia_json=h, status="pronto")
    tam = saida.stat().st_size / 1024 / 1024
    db.evento(pid, f"Vídeo pronto: {saida.name} ({tam:.1f} MB). Custo total US$ {custos.total(pid):.4f}.")
