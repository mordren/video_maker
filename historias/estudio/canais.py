"""Gerenciador de canais. Cada canal guarda uma vez só tudo o que decide como a história é escrita,
julgada, mostrada e sonorizada. A IA sugere a configuração a partir da descrição; você revisa e salva."""
import asyncio
import copy
import json
import re
import unicodedata

from . import clientes, config, db

ESTILOS = {
    "nanquim": {
        "nome": "Quadrinho sombrio / nanquim",
        "sufixo": "dark graphic novel illustration, heavy black ink shadows, high contrast, limited color palette of "
                  "deep blue and sickly yellow, rough hand-drawn linework, flat shading, horror comic style, no text",
    },
    "animacao_2d": {
        "nome": "2D animado adulto",
        "sufixo": "2D animated series style, clean bold outlines, cel shading, muted dark colors, cold moonlight, "
                  "eerie atmosphere, cinematic framing, no text",
    },
    "massinha": {
        "nome": "Stop-motion / massinha",
        "sufixo": "stop-motion claymation style, handmade miniature set, textured clay figures, dramatic small "
                  "practical lights, shallow depth of field, no text",
    },
    "cinema": {
        "nome": "Cinema realista",
        "sufixo": "cinematic film still, dramatic lighting, deep shadows, 35mm photography, muted desaturated "
                  "colors, no text",
    },
    "pulp_50": {
        "nome": "Quadrinho de terror anos 50 (pulp)",
        "prefixo": "1950s pulp horror comic book panel, bold black inks, halftone dots, vintage four-color print, "
                   "lurid saturated colors, coarse printed texture, edge-to-edge artwork, the scene fills the entire canvas",
        "sufixo": "sensational horror, unsettling, dread, no text, no speech balloons, no caption boxes",
        "recorte": 0.04,  # o estilo desenha moldura de papel; o programa tira 4% de cada borda de cada cena
    },
    # O prefixo vem antes da cena (o Flux dá mais peso ao começo do prompt); o sufixo, depois.
    # [ELEMENTO EM VERMELHO] é trocado pelo "destaque" que o roteirista escolhe para cada cena.
    "ito_manga": {
        "nome": "Mangá Junji Ito (P&B com um detalhe vermelho)",
        "prefixo": "Junji Ito manga style, traditional Japanese manga illustration, BLACK AND WHITE INK ON PAPER, "
                   "flat 2D composition, no depth of field, visible ink contour lines, dense cross-hatching, "
                   "solid black fills, screentone dot patterns, halftone shading, grainy paper texture",
        "sufixo": "no blur, no photo lighting, shadows as solid black shapes, "
                  "ONLY ONE element in vivid blood red: [ELEMENTO EM VERMELHO], everything else strictly monochrome, "
                  "unsettling, uncanny, dread, gekiga manga, Kazuo Umezu influence, "
                  "no realism, no 3D, no photo, no render, no text",
    },
}

LEGENDAS = {
    "impacto": {"fonte": "Arial Black", "tamanho": 86, "cor": "#FFFFFF", "destaque": "#FFD400", "contorno": 7,
                "sombra": 2, "posicao": "centro", "palavras_min": 2, "palavras_max": 4, "maiusculas": True,
                "karaoke": True},
    "classica": {"fonte": "Arial", "tamanho": 64, "cor": "#FFFFFF", "destaque": "#FFFFFF", "contorno": 4,
                 "sombra": 1, "posicao": "baixo", "palavras_min": 3, "palavras_max": 6, "maiusculas": False,
                 "karaoke": False},
    "minimal": {"fonte": "Segoe UI Semibold", "tamanho": 70, "cor": "#F2F2F2", "destaque": "#F2F2F2",
                "contorno": 3, "sombra": 0, "posicao": "centro", "palavras_min": 2, "palavras_max": 3,
                "maiusculas": False, "karaoke": False},
    "nenhuma": {"desligada": True},
}

EFEITOS_MOVIMENTO = ["zoom_in", "zoom_out", "pan", "tremor"]
EFEITOS_SOBREPOSICAO = ["flash", "cintilar_luz", "vinheta_pulso"]

# Perguntas que valem para qualquer canal. Pergunta com trava que falha reprova a história (recomeça com outra vibe).
PERGUNTAS_UNIVERSAIS = {
    "gancho": {"tipo": "score", "corte": 8, "trava": False,
               "pergunta": "Quem está rolando o feed para nos 3 primeiros segundos com esse gancho (a narração da cena 1)?"},
    "personagem_consistente": {"tipo": "noul", "corte": 0.85, "trava": False,
                               "pergunta": "O personagem principal mantém aparência, idade e comportamento do começo ao fim?"},
    "coerencia": {"tipo": "noul", "corte": 0.85, "trava": False,
                  "pergunta": "A sequência de fatos é coerente, sem furos nem contradições?"},
    "sem_enfeite": {"tipo": "noul", "corte": 0.85, "trava": False,
                    "pergunta": "Todo detalhe estranho cabe na lógica da história e serve a ela, sem imagem que soa "
                                "bonita mas não fecha (ex.: água escorrendo de um poço que já secou) e sem final-charada?"},
    "entendimento_audio": {"tipo": "noul", "corte": 0.80, "trava": False,
                           "pergunta": "Dá para entender tudo ouvindo uma vez só, sem pronome ambíguo?"},
    "prompts_batem": {"tipo": "noul", "corte": 0.85, "trava": False,
                      "pergunta": "Cada prompt de imagem mostra o que a narração daquela cena diz?"},
    "seguranca": {"tipo": "noul", "corte": 0.90, "trava": True,
                  "pergunta": "A história evita violência explícita, conteúdo sexual e qualquer menção a menores em perigo?"},
}

CONFIG_PADRAO = {
    "regras_escrita": [
        "um personagem principal",
        "frases corridas, sem sequência de frases curtas de efeito",
        "final forte que muda o sentido de algo mostrado antes",
    ],
    "estrutura": ["gancho", "desenvolvimento", "virada", "final"],
    "max_personagens": 2,
    # Lugares diferentes que podem virar imagem. Placa de referência só sai para os que aparecem em 2+ cenas.
    "max_ambientes": 6,
    "palavras_por_segundo": 2.75,
    "palavras_min": None,   # None = usa o do formato
    "palavras_max": None,
    "duracao_min_s": None,  # segundos de narração; preenchido, vale mais que palavras_min/max
    "duracao_max_s": None,
    "imagens_min": None,    # None = usa o do formato
    "imagens_max": None,
    "juiz": {"perguntas": {}},  # específicas do canal; as universais entram sempre
    "estilo": copy.deepcopy(ESTILOS["cinema"]),
    "voz": {"provedor": "qwen", "modelo": "qwen/qwen-audio-3.0-tts-flash", "nome": "loongjohn", "taxa": "+0%", "tom": "+0Hz",
            "instrucoes": "", "instrucoes_tensao_alta": "", "pausa_entre_cenas_s": 0.3},
    "legenda": {"preset": "impacto", **LEGENDAS["impacto"]},
    "musica": {"volume_solo_db": -6.0, "volume_sob_voz_db": -20.0, "intro_s": 1.5, "cauda_s": 2.5,
               "rampa_s": 0.35, "humor": ""},
    "efeitos": {"permitidos": EFEITOS_MOVIMENTO + EFEITOS_SOBREPOSICAO,
                "por_tensao": {"1": "zoom_out", "2": "zoom_in", "3": "zoom_in", "4": "zoom_in", "5": "tremor"},
                "extras_por_tensao": {"4": "vinheta_pulso", "5": "flash"},
                "transicao": "escurecer"},
    "humanizer": True,
    "orcamento_usd": 0.10,
    "teto_laco_usd": None,  # None = usa LACO_TETO_USD do .env
    "reservas_refacao": 2,
    "resolucao_base": 1080,
    # Desligado: cada vídeo cria personagens e ambientes novos (tudo vai para a biblioteca mesmo assim).
    # Ligado: reaproveita ficha/placa com o mesmo id, descrição parecida e o MESMO estilo visual.
    "reaproveitar_biblioteca": False,
    "modos_narrativos": [],  # [{"nome", "regra"}]: um por vídeo, em rodízio (ver historia.dica_modo)
    "publicador_canal": "",  # nome do canal no Publicador (ex.: garras)
    "cta": "Siga para mais histórias.",  # vai no prompt do roteirista (historia.instrucao_final), que escreve o convite no fim da narração; vazio = sem CTA
    "sugestoes_trilha": [],
}

TERROR_GARRAS = {
    "nome": "Garras no Telhado",
    "idioma": "pt-BR",
    "formato": "short",
    "descricao": "Histórias de terror curtas em qualquer época e lugar: cidade grande, estrada, prédio, floresta, "
                 "castelo, aldeia medieval. Fantasmas, aparições, gente que não devia estar ali, assassinos, "
                 "maldições. Shorts de 1 minuto com final que vira a história.",
    "config": {
        "regras_escrita": [
            "Comece com algo que capture a atenção e crie motivo para ficar no vídeo: um mistério, uma pergunta, uma situação impossível, um conflito que o observador quer resolver.",
            "Um narrador só, uma identidade só do começo ao fim; mesma voz, mesmo ponto de vista.",
            "O narrador quer algo específico e toma uma decisão; o horror vem dessa decisão, não de observar coisas estranhas.",
            "Cada frase causa a próxima; encadeia eventos em sequência lógica, sem pulos ou imagens desconexas.",
            "Escolha uma regra sobrenatural (tempo voltando, duplo, possessão, maldição) e mantenha ela inteira até o fim.",
            "Escolha um modo narrativo (confissão, relato encontrado, depoimento, testemunha) e mantenha o tom do começo ao fim.",
            "Um personagem principal, no máximo dois ambientes; o ambiente e a época seguem o assunto pedido.",
            "Vocabulário, objetos e cenário da época e do lugar: na cidade medieval use lanterna, não celular.",
            "Frases corridas e naturais, como alguém contando; só o final pode ser curto se virar a história.",
            "Final com reviravolta que muda o sentido de algo mostrado antes.",
            "Texto escrito na imagem só quando for absolutamente essencial.",
        ],
        "estrutura": ["gancho", "desenvolvimento", "virada", "susto_final"],
        "max_personagens": 1,
        "max_ambientes": 6,
        "juiz": {"perguntas": {
            "final_reviravolta": {"tipo": "score", "corte": 7, "trava": False,
                                  "pergunta": "O final muda o sentido de algo mostrado antes?"},
            "tensao_crescente": {"tipo": "score", "corte": 7, "trava": False,
                                 "pergunta": "A tensão cresce de cena para cena?"},
            "originalidade": {"tipo": "score", "corte": 6, "trava": False,
                              "pergunta": "A história é original, e não cópia de creepypasta conhecida?"},
        }},
        "estilo": copy.deepcopy(ESTILOS["nanquim"]),
        "voz": {"provedor": "qwen", "modelo": "qwen/qwen-audio-3.0-tts-flash", "nome": "loongjohn", "taxa": "-4%", "tom": "+0Hz",
                "instrucoes": "voz grave e calma de quem conta uma história de terror em voz baixa, ritmo lento, clima de suspense",
                "instrucoes_tensao_alta": "mais tenso e baixo, quase sussurrando", "pausa_entre_cenas_s": 0.3},
        "musica": {"volume_solo_db": -6.0, "volume_sob_voz_db": -21.0, "intro_s": 1.5, "cauda_s": 3.0,
                   "rampa_s": 0.35, "humor": "suspense sombrio, drone grave, sem batida marcada"},
        "sugestoes_trilha": [],
    },
}


MODOS_TERROR = [
    {"nome": "Confissão", "regra": "alguém relata algo que aconteceu consigo; 1ª pessoa, tom íntimo, como quem fala baixo"},
    {"nome": "Relato encontrado", "regra": "a narração inteira é um único documento (áudio, bilhete, diário, carta, laudo) "
     "lido do começo ao fim, na voz de quem o fez, no tempo em que o fez; ele termina interrompido ou sem conclusão, "
     "e o que aconteceu depois fica só no que falta. Ninguém narra de fora nem conta como o documento foi achado"},
    {"nome": "Terror cotidiano", "regra": "começa banal, numa situação comum; o absurdo entra sem alarde"},
    {"nome": "Horror psicológico", "regra": "o medo nasce da percepção e da dúvida; nada é confirmado, "
     "o ouvinte duvida do narrador"},
    {"nome": "Sobrenatural", "regra": "presenças e fatos que desafiam a realidade, com regras próprias e consistentes "
     "(o que a coisa pode e não pode fazer)"},
    {"nome": "Horror cósmico", "regra": "a descoberta de algo incompreensível sobre a existência; escala humana contra "
     "algo indiferente, e a linguagem do narrador falha"},
    {"nome": "Final retroativo", "regra": "a última frase muda o sentido de tudo; a pista é plantada cedo, sem destaque"},
    {"nome": "Terror silencioso", "regra": "narração enxuta, com pausas; o medo vem do que se vê e se ouve "
     "(olhar, som, objeto, ausência), não do que se explica"},
]

REGRA_NARRADOR = ("Um narrador só, uma identidade só do começo ao fim; mesma voz, mesmo ponto de vista. Se o narrador "
                  "morre, a voz dele para antes, e só a última frase pode contar de fora o que se viu depois.")
ESTRUTURA_GARRAS_NOVO = [
    "Gancho: situação estranha ou detalhe fora do lugar que prende a atenção, sem explicação.",
    "Desenvolvimento: criação de tensão com imagens sensoriais antes de qualquer revelação.",
    "Complicação: o estranho ganha peso e aperta o narrador; tudo continua fazendo sentido dentro da história.",
    "Reviravolta: detalhe plantado antes, sem destaque, muda o sentido de algo já mostrado.",
    "Final: amarra a história; pode ficar aberto, mas a única ponta solta é uma que a própria história criou.",
]
PERGUNTA_FINAL = ("O final amarra a história e deixa no máximo uma ponta solta, nascida do que já foi contado, sem "
                  "mistério novo nem frase-charada que o ouvinte precisa decifrar?")

GARRAS_NOVO = {
    "nome": "Garras Novo",
    "idioma": "pt-BR",
    "formato": "short",
    "descricao": "Terror curto e atmosférico em qualquer época e lugar: cidade, estrada, prédio, floresta, castelo, "
                 "aldeia medieval. Fantasmas, aparições, gente que não devia estar ali, assassinos, maldições. "
                 "Gancho imediato, tensão crescente e final perturbador, em um dos oito modos narrativos do canal.",
    "config": {
        "palavras_min": 150,
        "palavras_max": 170,
        "regras_escrita": [
            "Comece com algo que capture a atenção e crie motivo para ficar no vídeo: um mistério, uma pergunta, uma situação impossível, um conflito que o observador quer resolver.",
            REGRA_NARRADOR,
            "O narrador quer algo específico e toma uma decisão; o horror vem dessa decisão, não de observar coisas estranhas.",
            "Cada frase causa a próxima; encadeia eventos em sequência lógica, sem pulos ou imagens desconexas.",
            "Escolha uma regra sobrenatural (tempo voltando, duplo, possessão, maldição) e mantenha ela inteira até o fim.",
            "Escolha um modo narrativo (confissão, relato encontrado, terror cotidiano, horror psicológico, sobrenatural, horror cósmico, final retroativo, terror silencioso) e mantenha o tom do começo ao fim.",
            "Personagem principal mantém aparência, idade e comportamento do começo ao fim; nenhuma mudança inexplicada.",
            "A sequência de fatos é coerente, sem furos, contradições ou saltos de lógica.",
            "Dá para entender tudo ouvindo uma vez só: sem pronomes ambíguos, sem nomes repetidos em contextos diferentes, sem mudanças de perspectiva.",
            "Vocabulário, objetos, profissões, lugares e costumes coerentes com a época, o lugar e o meio do assunto; nenhuma palavra que pertença a outro tempo ou a outro ambiente.",
            "Nada de clichê (\"era só um sonho\", espelho que mostra outra pessoa, criança que canta). Nada de gore gratuito: prefira o terror psicológico e atmosférico.",
            "Frases de tamanhos variados, com ritmo crescente; a frase curta vale como golpe isolado, nunca em fileira.",
            "Imagens sensoriais concretas (som, cheiro, luz, temperatura) em vez de adjetivos de medo (\"aterrorizante\", \"sinistro\").",
            "Evita violência explícita, conteúdo sexual e qualquer menção a menores em perigo.",
            "Texto dentro da imagem só quando for essencial.",
            "Não repita a estrutura \"ouvi passos, vi uma figura, era algo no final\" sem que o tema peça.",
        ],
        "modos_narrativos": MODOS_TERROR,
        "estrutura": ESTRUTURA_GARRAS_NOVO,
        "max_personagens": 2,
        "max_ambientes": 4,
        "juiz": {"perguntas": {
            "gancho": {"tipo": "score", "corte": 8, "trava": True,
                       "pergunta": "Quem está rolando o feed para nos 3 primeiros segundos com esse gancho? Ele traz uma situação estranha ou um detalhe impossível, sem explicar, em poucas palavras?"},
            "personagem_consistente": {"tipo": "noul", "corte": 0.85, "trava": True,
                                       "pergunta": "O personagem principal mantém aparência, idade e comportamento do começo ao fim, sem mudanças inexplicadas?"},
            "coerencia": {"tipo": "noul", "corte": 0.85, "trava": True,
                          "pergunta": "A sequência de fatos é coerente, sem furos, contradições ou saltos de lógica?"},
            "entendimento_audio": {"tipo": "noul", "corte": 0.85, "trava": True,
                                   "pergunta": "Dá para entender tudo ouvindo uma vez só: sem pronomes ambíguos, sem nomes repetidos em contextos diferentes?"},
            "seguranca": {"tipo": "noul", "corte": 0.90, "trava": True,
                          "pergunta": "A história evita violência explícita, conteúdo sexual e qualquer menção a menores em perigo?"},
            "modo_respeitado": {"tipo": "noul", "corte": 0.85, "trava": True,
                                "pergunta": "A história segue um único modo narrativo do começo ao fim, sem misturar estilos ou perspectivas? Qual é o modo escolhido?"},
            "regra_sobrenatural": {"tipo": "noul", "corte": 0.85, "trava": False,
                                   "pergunta": "O que é sobrenatural ou impossível segue uma regra própria e consistente (o que a coisa pode e não pode fazer), sem mudar no meio?"},
            "medo_especifico": {"tipo": "score", "corte": 7, "trava": False,
                                "pergunta": "A história ativa um medo específico e reconhecível (ser observado, perder o controle, ser esquecido, estar errado sobre algo), e não só sustos soltos?"},
            "tensao_antes_da_revelacao": {"tipo": "score", "corte": 7, "trava": False,
                                          "pergunta": "A tensão cresce antes de qualquer revelação, com imagens sensoriais concretas em vez de adjetivos de medo?"},
            "reviravolta_plantada": {"tipo": "score", "corte": 7, "trava": False,
                                     "pergunta": "A reviravolta ou o detalhe impossível tem uma pista plantada antes, sem destaque, e muda o sentido de algo já mostrado?"},
            "final_incomodo": {"tipo": "score", "corte": 7, "trava": False, "pergunta": PERGUNTA_FINAL},
            "sem_cliche": {"tipo": "noul", "corte": 0.80, "trava": False,
                           "pergunta": "A história evita clichês (era só um sonho, espelho com outra pessoa, criança cantando, passos e figura no final) e gore gratuito?"},
            "epoca_e_lugar": {"tipo": "noul", "corte": 0.85, "trava": False,
                              "pergunta": "Objetos, vocabulário e cenário combinam com a época e o lugar do assunto?"},
        }},
        "estilo": copy.deepcopy(ESTILOS["nanquim"]),
        "voz": {"provedor": "qwen", "modelo": "qwen/qwen-audio-3.0-tts-flash", "nome": "loongjohn", "taxa": "-4%", "tom": "+0Hz",
                "instrucoes": "voz grave e contida de quem conta uma história de terror em voz baixa, ritmo lento, pausas antes das revelações, clima de suspense",
                "instrucoes_tensao_alta": "mais tenso e baixo, quase sussurrando", "pausa_entre_cenas_s": 0.3},
        "musica": {"volume_solo_db": -6.0, "volume_sob_voz_db": -21.0, "intro_s": 1.5, "cauda_s": 3.0,
                   "rampa_s": 0.35, "humor": "suspense sombrio, drone grave, sem batida marcada"},
        "sugestoes_trilha": [],
        "humanizer": True,
        "orcamento_usd": 0.15,
    },
}


def slugify(txt: str) -> str:
    txt = unicodedata.normalize("NFKD", txt).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", txt.lower()).strip("-")[:60] or "canal"


def mesclar(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = mesclar(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def corte_na_escala(q: dict) -> dict:
    """noul é probabilidade (0 a 1) e score é nota (0 a 10). Um corte na escala do outro tipo (ex.: noul com corte 7)
    nunca passaria, e com trava reprovaria toda história: 7 vira 0,7 e 0,8 vira 8."""
    try:
        corte = float(q.get("corte"))
    except (TypeError, ValueError):
        return q
    if q.get("tipo") == "noul" and corte > 1:
        q["corte"] = round(min(corte, 10) / 10, 3)
    elif q.get("tipo") == "score" and 0 < corte <= 1:
        q["corte"] = round(corte * 10, 2)
    return q


def perguntas_do_canal(cfg: dict) -> dict:
    """Universais + específicas. Uma específica com a mesma chave sobrepõe a universal (ex.: corte do gancho)."""
    perguntas = copy.deepcopy(PERGUNTAS_UNIVERSAIS)
    for k, v in (cfg.get("juiz", {}).get("perguntas") or {}).items():
        perguntas[k] = corte_na_escala(mesclar(perguntas.get(k, {}), v))
    return perguntas


def obter(canal_id: int) -> dict | None:
    c = db.um("SELECT * FROM canais WHERE id = ?", (canal_id,))
    if not c:
        return None
    c["config"] = mesclar(CONFIG_PADRAO, db.carregar_json(c.pop("config_json"), {}))
    for q in ((c["config"].get("juiz") or {}).get("perguntas") or {}).values():
        corte_na_escala(q)
    c["trilhas"] = db.todos("SELECT * FROM trilhas WHERE canal_id = ? ORDER BY id", (canal_id,))
    return c


def listar() -> list[dict]:
    canais = []
    for c in db.todos("SELECT id FROM canais ORDER BY nome"):
        canal = obter(c["id"])
        canal["projetos"] = db.um("SELECT COUNT(*) AS n FROM projetos WHERE canal_id = ?", (c["id"],))["n"]
        canais.append(canal)
    return canais


def salvar(dados: dict, canal_id: int | None = None) -> int:
    nome = (dados.get("nome") or "").strip()
    if not nome:
        raise ValueError("O canal precisa de um nome")
    cfg = mesclar(CONFIG_PADRAO, dados.get("config") or {})
    # A lista de perguntas é substituída por inteiro (a mesclagem manteria perguntas apagadas na tela).
    if "juiz" in (dados.get("config") or {}):
        cfg["juiz"]["perguntas"] = dados["config"]["juiz"].get("perguntas") or {}
    for q in (cfg["juiz"].get("perguntas") or {}).values():
        corte_na_escala(q)
    valores = (nome, dados.get("idioma") or "pt-BR", dados.get("descricao") or "",
               dados.get("formato") or "short", json.dumps(cfg, ensure_ascii=False))
    if canal_id:
        db.executar("UPDATE canais SET nome=?, idioma=?, descricao=?, formato=?, config_json=?, atualizado_em=? "
                    "WHERE id=?", (*valores, db.agora(), canal_id))
        return canal_id
    slug = slugify(nome)
    base, i = slug, 2
    while db.um("SELECT id FROM canais WHERE slug = ?", (slug,)):
        slug, i = f"{base}-{i}", i + 1
    return db.executar("INSERT INTO canais (nome, slug, idioma, descricao, formato, config_json, criado_em, "
                       "atualizado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (nome, slug, *valores[1:], db.agora(), db.agora()))


def semear():
    if not db.um("SELECT id FROM canais LIMIT 1"):
        salvar(TERROR_GARRAS)
    if not db.um("SELECT id FROM canais WHERE slug = ?", (slugify(GARRAS_NOVO["nome"]),)):
        salvar(GARRAS_NOVO)
    migrar_regras_garras_novo()
    migrar_voz_qwen()


_MODO_RELATO_ANTIGO = ("a narração lê um áudio, gravação, laudo, bilhete ou mensagem achado por alguém; "
                       "o formato do documento aparece no próprio texto lido")


# Regras da primeira versão do Garras Novo trocadas pelas atuais (as que você editou na tela ficam como estão).
_REGRAS_ANTIGAS = {
    "Escolha UM modo narrativo e o respeite do começo ao fim, sem misturar. O vídeo recebe um modo sugerido, para o "
    "canal variar; troque só se o assunto já indicar outro claramente.": 0,
    "Gancho na primeira frase, de até 15 palavras: situação estranha ou detalhe impossível, sem explicar.": 2,
    "Vocabulário, objetos e cenário da época e do lugar do assunto (celular e semáforo na cidade, tocha e taverna na "
    "Idade Média), sem trazer o clima de outro cenário.": 6,
    "Um narrador só, uma identidade só do começo ao fim; mesma voz, mesmo ponto de vista.": 1,
}
# Estrutura e pergunta do final da versão que pedia "deixa algo sem explicar" (gerava final-charada).
_ESTRUTURA_ANTIGA = {
    "Gancho: situação estranha ou detalhe impossível que prende a atenção, sem explicação.": 0,
    "Complicação: o que é impossível ganha peso; a realidade se quebra.": 2,
    "Final: ambíguo, incômodo ou macabro; deixa algo sem explicar, em vez de fechar tudo.": 4,
}
_PERGUNTA_FINAL_ANTIGA = ("O final é ambíguo, incômodo ou macabro e deixa algo sem explicar, em vez de fechar tudo com "
                          "uma explicação?")


def migrar_regras_garras_novo():
    c = db.um("SELECT id, config_json FROM canais WHERE slug = ?", (slugify(GARRAS_NOVO["nome"]),))
    if not c:
        return
    cfg = db.carregar_json(c["config_json"], {})
    regras = cfg.get("regras_escrita") or []
    novas = [GARRAS_NOVO["config"]["regras_escrita"][_REGRAS_ANTIGAS[r]] if r in _REGRAS_ANTIGAS else r for r in regras]
    modos = cfg.get("modos_narrativos") or []
    atual = {m["nome"]: m["regra"] for m in MODOS_TERROR}
    modos_novos = [{**m, "regra": atual[m["nome"]]} if m.get("regra") == _MODO_RELATO_ANTIGO else m for m in modos]
    estrutura = cfg.get("estrutura") or []
    estrutura_nova = [ESTRUTURA_GARRAS_NOVO[_ESTRUTURA_ANTIGA[e]] if e in _ESTRUTURA_ANTIGA else e for e in estrutura]
    final = ((cfg.get("juiz") or {}).get("perguntas") or {}).get("final_incomodo") or {}
    mudou_final = final.get("pergunta") == _PERGUNTA_FINAL_ANTIGA
    if mudou_final:
        final["pergunta"] = PERGUNTA_FINAL
    if novas != regras or modos_novos != modos or estrutura_nova != estrutura or mudou_final:
        cfg["regras_escrita"], cfg["modos_narrativos"], cfg["estrutura"] = novas, modos_novos, estrutura_nova
        db.executar("UPDATE canais SET config_json = ? WHERE id = ?", (json.dumps(cfg, ensure_ascii=False), c["id"]))


def migrar_voz_qwen():
    """Canais do Edge (sem provedor) ou do Qwen antigo do Pollinations passam para o Qwen do OpenRouter.
    Velocidade e instruções são mantidas."""
    for c in db.todos("SELECT id, slug, config_json FROM canais"):
        cfg = db.carregar_json(c["config_json"], {})
        voz = cfg.get("voz") or {}
        antigo_pollinations = str(voz.get("modelo", "")).startswith("qwen/qwen3-tts")
        if voz.get("provedor") and not antigo_pollinations:
            continue
        # O canal de terror semeado ganha a instrução de narração se ainda não tiver uma.
        modelo_voz = TERROR_GARRAS["config"]["voz"] if c["slug"] == slugify(TERROR_GARRAS["nome"]) else CONFIG_PADRAO["voz"]
        cfg["voz"] = {**CONFIG_PADRAO["voz"], "taxa": voz.get("taxa", "+0%"),
                      "instrucoes": voz.get("instrucoes") or modelo_voz["instrucoes"],
                      "instrucoes_tensao_alta": voz.get("instrucoes_tensao_alta") or modelo_voz["instrucoes_tensao_alta"]}
        db.executar("UPDATE canais SET config_json = ? WHERE id = ?", (json.dumps(cfg, ensure_ascii=False), c["id"]))


def resolver(ref) -> dict | None:
    """Aceita id numérico ou slug (para a API externa)."""
    if ref is None:
        return None
    if str(ref).isdigit():
        return obter(int(ref))
    c = db.um("SELECT id FROM canais WHERE slug = ?", (str(ref),))
    return obter(c["id"]) if c else None


# ---------------------------------------------------------------- vozes do Edge TTS

_vozes_cache: list[dict] = []

# Qwen TTS pelo OpenRouter. As vozes vêm do catálogo (campo supported_voices de cada modelo).
MODELOS_QWEN = {"qwen/qwen-audio-3.0-tts-flash": "Qwen-Audio-3.0-TTS Flash (US$ 0,015 por 1.000 caracteres)",
                "qwen/qwen-audio-3.0-tts-plus": "Qwen-Audio-3.0-TTS Plus (US$ 0,020 por 1.000 caracteres)"}
VOZES_QWEN_RESERVA = {"qwen/qwen-audio-3.0-tts-flash": ["loongjohn", "longanhuan_v3.6"]}
_vozes_qwen_cache: dict[str, list[str]] = {}


def vozes_qwen(modelo: str) -> list[str]:
    if not _vozes_qwen_cache:
        try:
            import httpx
            r = httpx.get("https://openrouter.ai/api/v1/models", params={"output_modalities": "speech"}, timeout=20)
            for m in r.json().get("data", []):
                if m.get("supported_voices"):
                    _vozes_qwen_cache[m["id"]] = list(m["supported_voices"])
        except Exception:
            pass
    return _vozes_qwen_cache.get(modelo) or VOZES_QWEN_RESERVA.get(modelo, [])


def vozes(idioma: str | None = None, provedor: str = "edge", modelo: str | None = None) -> list[dict]:
    global _vozes_cache
    if provedor == "qwen":
        return [{"nome": v, "idioma": idioma or "pt-BR", "genero": "", "estilo": ""}
                for v in vozes_qwen(modelo or "qwen/qwen-audio-3.0-tts-flash")]
    if not _vozes_cache:
        import edge_tts
        try:
            _vozes_cache = asyncio.run(edge_tts.list_voices())
        except RuntimeError:  # já existe um loop rodando (chamado de dentro de código async)
            loop = asyncio.new_event_loop()
            _vozes_cache = loop.run_until_complete(edge_tts.list_voices())
            loop.close()
    lista = [{"nome": v["ShortName"], "idioma": v["Locale"], "genero": v["Gender"],
              "estilo": ", ".join(v.get("VoiceTag", {}).get("VoicePersonalities", []))} for v in _vozes_cache]
    if idioma:
        pref = idioma.split("-")[0].lower()
        lista = [v for v in lista if v["idioma"].lower() == idioma.lower()] or \
                [v for v in lista if v["idioma"].lower().startswith(pref)]
    return sorted(lista, key=lambda v: v["nome"])


# ---------------------------------------------------------------- sugestão por IA

SCHEMA_SUGESTAO = {
    "type": "object",
    "additionalProperties": False,
    "required": ["regras_escrita", "estrutura", "perguntas", "estilo_nome", "estilo_sufixo", "max_personagens",
                 "max_ambientes", "voz_genero", "efeitos_por_tensao", "extras_por_tensao", "musica_humor",
                 "sugestoes_trilha", "legenda_preset"],
    "properties": {
        "regras_escrita": {"type": "array", "items": {"type": "string"}},
        "estrutura": {"type": "array", "items": {"type": "string"}},
        "perguntas": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["chave", "tipo", "pergunta", "corte"],
            "properties": {"chave": {"type": "string"}, "tipo": {"type": "string", "enum": ["score", "noul"]},
                           "pergunta": {"type": "string"}, "corte": {"type": "number"}}}},
        "estilo_nome": {"type": "string"},
        "estilo_sufixo": {"type": "string"},
        "max_personagens": {"type": "integer"},
        "max_ambientes": {"type": "integer"},
        "voz_genero": {"type": "string", "enum": ["Male", "Female"]},
        "efeitos_por_tensao": {"type": "array", "items": {"type": "string", "enum": EFEITOS_MOVIMENTO}},
        "extras_por_tensao": {"type": "array", "items": {"type": "string", "enum": EFEITOS_SOBREPOSICAO + ["nenhum"]}},
        "musica_humor": {"type": "string"},
        "sugestoes_trilha": {"type": "array", "items": {"type": "string"}},
        "legenda_preset": {"type": "string", "enum": ["impacto", "classica", "minimal"]},
    },
}


def sugerir(nome: str, descricao: str, idioma: str, formato: str) -> dict:
    """O DeepSeek lê a descrição do canal e propõe a configuração. Nada é salvo aqui."""
    universais = "\n".join(f"- {k}: {v['pergunta']}" for k, v in PERGUNTAS_UNIVERSAIS.items())
    sistema = (
        "Você configura canais de vídeos narrados com imagens (histórias curtas contadas por um narrador). "
        "A partir do nome e da descrição do canal, proponha a configuração que faz as histórias desse canal "
        "funcionarem. Responda só com o JSON pedido."
    )
    usuario = f"""Canal: {nome}
Descrição: {descricao}
Idioma da narração: {idioma}
Formato: {config.FORMATOS.get(formato, config.FORMATOS['short'])['nome']}

Preencha:
- regras_escrita: 4 a 7 regras concretas para o roteirista deste gênero (tom, ritmo, o que evitar). Escreva em português.
- estrutura: as batidas da história em ordem (3 a 5 nomes curtos em snake_case).
- perguntas: 2 a 4 perguntas ESPECÍFICAS do gênero para um juiz que avalia a história antes de virar vídeo.
  O juiz já faz estas perguntas universais, não repita:
{universais}
  Use tipo "score" (nota 0 a 10, corte entre 6 e 8) para qualidade graduada e "noul" (probabilidade 0 a 1,
  corte entre 0.8 e 0.9) para sim/não. chave em snake_case. Pergunta em português, respondível lendo só o texto.
- estilo_nome e estilo_sufixo: estilo visual das imagens. O sufixo vai no fim de todo prompt de imagem, em inglês,
  descrevendo técnica, luz e paleta, terminando com "no text".
- max_personagens: personagens com ficha visual (em short, 1 ou 2). max_ambientes: lugares diferentes que podem virar imagem (3 a 6).
- voz_genero: Male ou Female, a voz que combina com o canal.
- efeitos_por_tensao: 5 movimentos de câmera, um para cada nível de tensão de 1 a 5.
- extras_por_tensao: 5 efeitos de sobreposição para tensão 1 a 5 (use "nenhum" quando não couber).
- musica_humor: descrição curta do tipo de trilha de fundo (humor, instrumentos, andamento).
- sugestoes_trilha: 3 a 5 termos de busca para achar trilhas na Biblioteca de Áudio do YouTube (gênero e humor do filtro dela).
- legenda_preset: impacto (grande, centro, 2-4 palavras), classica (embaixo, frase) ou minimal."""
    resp, _ = clientes.chat("sugestao_canal", [{"role": "system", "content": sistema},
                                               {"role": "user", "content": usuario}],
                            SCHEMA_SUGESTAO, temperatura=0.5, etapa="canal",
                            contexto={"nome": nome, "descricao": descricao})
    return _sugestao_para_config(resp, idioma)


def _sugestao_para_config(s: dict, idioma: str) -> dict:
    perguntas = {}
    for p in s.get("perguntas", [])[:5]:
        chave = slugify(p.get("chave", "")).replace("-", "_") or f"p{len(perguntas) + 1}"
        if chave in PERGUNTAS_UNIVERSAIS:
            continue
        tipo = p.get("tipo") if p.get("tipo") in ("score", "noul") else "score"
        corte = float(p.get("corte", 7 if tipo == "score" else 0.85))
        corte = min(max(corte, 5), 9) if tipo == "score" else min(max(corte, 0.6), 0.95)
        perguntas[chave] = {"tipo": tipo, "pergunta": p.get("pergunta", ""), "corte": corte, "trava": False}

    def por_tensao(lista, validos, padrao):
        lista = [x for x in (lista or []) if x in validos][:5]
        while len(lista) < 5:
            lista.append(padrao)
        return {str(i + 1): v for i, v in enumerate(lista)}

    extras = por_tensao(s.get("extras_por_tensao"), EFEITOS_SOBREPOSICAO + ["nenhum"], "nenhum")
    extras = {k: v for k, v in extras.items() if v != "nenhum"}
    voz = "longanhuan_v3.6" if s.get("voz_genero") == "Female" else "loongjohn"
    preset = s.get("legenda_preset") if s.get("legenda_preset") in LEGENDAS else "impacto"
    return {
        "regras_escrita": s.get("regras_escrita") or CONFIG_PADRAO["regras_escrita"],
        "estrutura": [slugify(x).replace("-", "_") for x in (s.get("estrutura") or CONFIG_PADRAO["estrutura"])],
        "max_personagens": max(1, min(int(s.get("max_personagens", 2)), 3)),
        "max_ambientes": max(2, min(int(s.get("max_ambientes", 5)), 8)),
        "juiz": {"perguntas": perguntas},
        "estilo": {"nome": s.get("estilo_nome", "Personalizado"), "sufixo": s.get("estilo_sufixo", "")},
        "voz": {**CONFIG_PADRAO["voz"], "nome": voz},
        "efeitos": {"permitidos": EFEITOS_MOVIMENTO + EFEITOS_SOBREPOSICAO,
                    "por_tensao": por_tensao(s.get("efeitos_por_tensao"), EFEITOS_MOVIMENTO, "zoom_in"),
                    "extras_por_tensao": extras, "transicao": "escurecer"},
        "musica": {"humor": s.get("musica_humor", "")},
        "legenda": {"preset": preset, **LEGENDAS[preset]},
        "sugestoes_trilha": s.get("sugestoes_trilha") or [],
    }
