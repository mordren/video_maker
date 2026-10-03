"""Estúdio de Histórias em Vídeo.

    python main.py                                   # abre o servidor em http://localhost:8000
    python main.py novo --canal garras-no-telhado "caseiro ouve arranhões no telhado" [--automatico]
    python main.py continuar 12
    python main.py refazer 12 cena 7
    python main.py efeito teste zoom_in caminho/imagem.jpg [--formato short]
    python main.py comfy-historia 8 [--motor qwen|kontext] [-o arquivo.json]   # história do projeto como workflow do Comfy
    python main.py comfy-baixar [--prefixo hist8] [-o pasta]   # baixa a pasta output do ComfyUI
    python main.py comfy "prompt" [--ref ficha.jpg] [--ref placa.jpg] [--formato short|longo|ficha] [--seed 42]
"""
import argparse
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


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
    k = sub.add_parser("comfy", help="gera uma imagem só no ComfyUI (sem banco, sem cair na Pollinations)")
    k.add_argument("prompt")
    k.add_argument("--ref", action="append", default=[], help="imagem de referência (pode repetir)")
    k.add_argument("--formato", choices=["short", "longo", "ficha"], default="short")
    k.add_argument("--seed", type=int, default=42)
    hs = sub.add_parser("comfy-historia", help="exporta a história de um projeto como workflow do Comfy (abrir na interface)")
    hs.add_argument("projeto", type=int)
    hs.add_argument("--motor", choices=["qwen", "kontext"], default="qwen", help="modelo do workflow")
    hs.add_argument("-o", "--saida", help="arquivo de saída (padrão: testes_comfy/historia_N.json)")
    bx = sub.add_parser("comfy-baixar", help="baixa as imagens da pasta output do ComfyUI (pelo túnel)")
    bx.add_argument("--prefixo", default="", help="só os arquivos que começam assim (ex.: hist8)")
    bx.add_argument("-o", "--saida", default="testes_comfy/saida", help="pasta de destino")
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
    elif a.cmd == "comfy-historia":
        from estudio import canais, comfy_historia, db
        db.iniciar()
        canais.semear()
        destino, resumo = comfy_historia.salvar(a.projeto, Path(a.saida) if a.saida else None, a.motor)
        print(resumo)
        print("Workflow:", destino)
    elif a.cmd == "comfy-baixar":
        from estudio import comfy
        if not comfy.no_ar():
            sys.exit("Comfy fora do ar: confira a célula do Colab e a COMFY_URL no .env.")
        novas = comfy.baixar_saidas(Path(a.saida), a.prefixo)
        print(f"{len(novas)} imagem(ns) nova(s) em {a.saida}")
        for f in novas:
            print(" ", f.name)
    elif a.cmd == "comfy":
        import time
        from estudio import comfy, config
        print(f"Comfy em {config.comfy_url() or '(COMFY_URL vazia no .env)'}...")
        if not comfy.no_ar():
            sys.exit("Comfy fora do ar: confira se a célula do Colab está rodando e a COMFY_URL no .env.")
        fmt = config.FORMATOS["short"]
        w, h = {"short": (fmt["img_largura"], fmt["img_altura"]),
                "longo": (config.FORMATOS["longo"]["img_largura"], config.FORMATOS["longo"]["img_altura"]),
                "ficha": (fmt["ficha_largura"], fmt["ficha_altura"])}[a.formato]
        inicio = time.time()
        dados = comfy.gerar(a.prompt, w, h, a.seed, [Path(r) for r in a.ref])
        destino = Path("testes_comfy") / f"{time.strftime('%Y%m%d_%H%M%S')}_{a.formato}_s{a.seed}.jpg"
        destino.parent.mkdir(exist_ok=True)
        destino.write_bytes(dados)
        print(f"Pronto em {time.time() - inicio:.0f}s: {destino}")


if __name__ == "__main__":
    main()
