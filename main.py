"""Estúdio de Histórias em Vídeo.

    python main.py                                   # abre o servidor em http://localhost:8000
    python main.py novo --canal garras-no-telhado "caseiro ouve arranhões no telhado" [--automatico]
    python main.py continuar 12
    python main.py refazer 12 cena 7
    python main.py efeito teste zoom_in caminho/imagem.jpg [--formato short]
"""
import argparse
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")


def _preparar():
    from estudio import canais, db, pipeline
    db.iniciar()
    canais.semear()
    pipeline.iniciar()
    return pipeline


def _aguardar(pipeline, pid):
    pipeline._fila.join()
    p = pipeline.obter(pid)
    print(f"\nProjeto {pid}: {p['status']}" + (f" · {p['ressalva']}" if p.get("ressalva") else "") +
          (f" · erro: {p['erro']}" if p.get("erro") else ""))
    print(f"Pasta: {p['pasta']}")


def main():
    ap = argparse.ArgumentParser(description="Estúdio de Histórias em Vídeo")
    sub = ap.add_subparsers(dest="cmd")
    s = sub.add_parser("servidor")
    s.add_argument("--porta", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1")
    n = sub.add_parser("novo")
    n.add_argument("assunto")
    n.add_argument("--canal", default="garras-no-telhado")
    n.add_argument("--automatico", action="store_true", help="não para nas revisões")
    c = sub.add_parser("continuar")
    c.add_argument("projeto", type=int)
    r = sub.add_parser("refazer")
    r.add_argument("projeto", type=int)
    r.add_argument("cena", choices=["cena"])
    r.add_argument("n", type=int)
    e = sub.add_parser("efeito")
    e.add_argument("acao", choices=["teste"])
    e.add_argument("nome")
    e.add_argument("imagem")
    e.add_argument("--formato", default="short")
    e.add_argument("--tensao", type=int, default=3)
    ap.add_argument("--simulacao", action="store_true", help="não chama APIs pagas (respostas falsas)")
    ap.add_argument("--raiz", help="pasta alternativa para banco, projetos, biblioteca e trilhas")
    a = ap.parse_args()
    # Precisa vir antes de importar o pacote estudio (config lê o ambiente na importação).
    if a.simulacao:
        os.environ["ESTUDIO_SIMULACAO"] = "1"
    if a.raiz:
        os.environ["ESTUDIO_RAIZ"] = a.raiz

    if a.cmd in (None, "servidor"):
        import uvicorn
        porta = getattr(a, "porta", 8000)
        host = getattr(a, "host", "127.0.0.1")
        print(f"Abrindo em http://localhost:{porta}")
        uvicorn.run("estudio.web:app", host=host, port=porta)
    elif a.cmd == "novo":
        pipeline = _preparar()
        from estudio import canais
        canal = canais.resolver(a.canal)
        if not canal:
            sys.exit(f"Canal '{a.canal}' não existe. Canais: {', '.join(c['slug'] for c in canais.listar())}")
        pid = pipeline.criar(canal["id"], a.assunto, a.automatico)
        _aguardar(pipeline, pid)
    elif a.cmd == "continuar":
        pipeline = _preparar()
        pipeline.continuar(a.projeto)
        _aguardar(pipeline, a.projeto)
    elif a.cmd == "refazer":
        pipeline = _preparar()
        pipeline.enfileirar(a.projeto, "refazer", n=a.n)
        _aguardar(pipeline, a.projeto)
    elif a.cmd == "efeito":
        from estudio import montagem
        destino = Path("testes_efeitos") / f"{a.nome}_{Path(a.imagem).stem}.mp4"
        montagem.testar_efeito(a.nome, Path(a.imagem), destino, a.formato, a.tensao)
        print(f"Clipe de teste: {destino}")


if __name__ == "__main__":
    main()
