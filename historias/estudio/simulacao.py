"""Respostas falsas para ESTUDIO_SIMULACAO=1: testa o fluxo inteiro sem gastar nada."""
import io
import random
import textwrap

FRASES = [
    "Naquela noite o vento batia forte nas janelas da casa velha",
    "Ele acendeu o lampião e ficou escutando o barulho que vinha lá de fora",
    "O cachorro, que nunca tinha medo de nada, se escondeu debaixo da mesa",
    "Pela fresta da porta dava para ver uma luz amarela passando devagar no quintal",
    "Quando saiu com a lanterna, o chão de terra estava marcado por pegadas compridas",
    "As marcas terminavam bem no meio do terreiro, como se a coisa tivesse subido",
    "Foi aí que o telhado de zinco rangeu em cima da cabeça dele",
    "Ele voltou correndo, trancou a porta e encostou a cadeira na maçaneta",
    "Passou o resto da madrugada acordado, sentado na beira da cama",
    "De manhã a vizinha contou que ninguém morava naquela casa fazia vinte anos",
    "E disse que o último caseiro sumiu numa noite de vento igual àquela, sem deixar nem as botas",
]


def chat(tarefa, mensagens, ctx):
    rnd = random.Random(tarefa + str(len(str(mensagens))))
    if tarefa == "ganchos":
        return {"modo": "Terror cotidiano", "ganchos": [
            "Três da manhã, e alguém bateu na porta da casa que não tem vizinho nenhum.",
            "O cachorro parou de latir no mesmo instante em que o telhado começou a ranger.",
            "Ninguém mora naquela casa desde 2004, mas a luz da cozinha acende toda noite.",
            "Meu avô jurava que o vento daquela serra chamava as pessoas pelo nome.",
            "Ele achou pegadas no quintal que começavam no mato e terminavam no ar.",
        ]}
    if tarefa == "narrativa":
        gancho = ctx.get("gancho") or FRASES[0]
        return {"titulo": f"Simulação: {ctx.get('assunto', '')[:40]}",
                "descricao_youtube": "Um caseiro sozinho numa noite de vento descobre quem morava ali antes.",
                "narracao": gancho + " " + " ".join(f + "." for f in FRASES[1:])}
    if tarefa == "decupagem":
        n = len(ctx.get("trechos") or [])
        grupos, i = [], 1
        while i <= n:  # agrupa de 1 a 2 trechos, como a história "pediria"
            ate = min(n, i + (0 if i == 1 or i % 3 == 0 else 1))
            grupos.append((i, ate))
            i = ate + 1
        cenas = [{"de": de, "ate": ate, "prompt_imagem": f"Seu Joaquim, scene {k}, dramatic night lighting",
                  "pessoas": [{"id": "joaquim", "onde": "in the center, in the foreground",
                               "faz": f"looking over his shoulder, moment {k}"}] if k % 2 == 0 else [], "ambiente": "casa" if k < len(grupos) - 2 else "quintal",
                  "tensao": min(5, 1 + k // 2), "destaque": f"object {k}"} for k, (de, ate) in enumerate(grupos, 1)]
        return {"personagens": [{"id": "joaquim", "nome": "Seu Joaquim",
                                 "descricao_fixa": "70-year-old thin Brazilian farmer, white stubble, straw hat, blue plaid shirt"}],
                "ambientes": [{"id": "casa", "descricao_fixa": "old wooden farmhouse, tin roof, eucalyptus trees"},
                              {"id": "quintal", "descricao_fixa": "dirt yard with a well and a dog house"}],
                "cenas": cenas}
    if tarefa == "prompts_cena":
        return {"cenas": [{"n": a["n"], "prompt_imagem": a["prompt_imagem_atual"] + ", clearer",
                           "pessoas": a.get("pessoas_atuais", []), "destaque": "the lamp"}
                          for a in ctx.get("alvo", [])]}
    if tarefa == "reescrita":
        return ctx.get("historia")
    if tarefa == "humanizer":
        e = ctx.get("entrada")
        return e
    if tarefa == "sugestao_canal":
        return {"regras_escrita": ["narração em primeira pessoa", "ritmo de conversa", "final com revelação"],
                "estrutura": ["gancho", "contexto", "revelacao"],
                "perguntas": [{"chave": "revelacao_final", "tipo": "score", "corte": 7,
                               "pergunta": "A revelação do final surpreende?"}],
                "estilo_nome": "Cinema", "estilo_sufixo": "cinematic still, soft light, no text",
                "max_personagens": 1, "max_ambientes": 2, "voz_genero": "Male",
                "efeitos_por_tensao": ["zoom_out", "zoom_in", "pan", "zoom_in", "tremor"],
                "extras_por_tensao": ["nenhum", "nenhum", "cintilar_luz", "vinheta_pulso", "flash"],
                "musica_humor": "ambiente misterioso", "sugestoes_trilha": ["Cinematic dark"],
                "legenda_preset": "impacto"}
    return {}


def decidir(state, perguntas, ctx):
    rnd = random.Random(len(str(state)))
    out = {}
    for k, q in perguntas.items():
        if q["type"] == "score":
            n = len(q["criteria"])
            alvo = n - 2 if not k.startswith("g") else rnd.randint(n - 4, n - 1)
            probs = {str(i): 0.0 for i in range(n)}
            probs[str(alvo)] = 0.8
            probs[str(max(alvo - 1, 0))] += 0.2
            out[k] = {"type": "score", "score": alvo, "legend": {str(i): c for i, c in enumerate(q["criteria"])},
                      "probabilities": probs, "confidence": 0.8}
        elif q["type"] == "choice":
            ks = list(q["criteria"])
            out[k] = {"type": "choice", "choice": ks[0], "probabilities": {x: 1 / len(ks) for x in ks}, "confidence": 0.5}
        else:
            out[k] = {"type": "noul", "noul": 0.93}
    return out


def imagem(prompt, largura, altura, seed, refs):
    from PIL import Image, ImageDraw
    rnd = random.Random(seed)
    base = (rnd.randint(10, 60), rnd.randint(10, 50), rnd.randint(30, 90))
    img = Image.new("RGB", (largura, altura), base)
    d = ImageDraw.Draw(img)
    for i in range(0, altura, 8):
        c = tuple(min(255, v + i * 60 // altura) for v in base)
        d.rectangle([0, i, largura, i + 8], fill=c)
    for _ in range(25):
        x, y, r = rnd.randint(0, largura), rnd.randint(0, altura), rnd.randint(20, 160)
        d.ellipse([x - r, y - r, x + r, y + r], outline=(200, 180, 90), width=4)
    texto = textwrap.fill(f"SIMULACAO seed {seed} refs {len(refs)}\n{prompt[:220]}", 38)
    d.multiline_text((40, 40), texto, fill=(255, 255, 255), spacing=8)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()
