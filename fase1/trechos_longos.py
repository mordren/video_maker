"""Vídeo longo (15-20 min) a partir da live: uma consulta ao DeepSeek só para isso.

    python trechos_longos.py <video.mp4> --workspace <pasta da Fase 1> [--titulo-live "..."]

Roda depois da Fase 1 (pipeline.py), na mesma pasta: reaproveita a
transcricao.json e grava longos.json. Não mexe nos shorts.

Duas chamadas ao DeepSeek:

1. A transcrição inteira, agrupada em linhas de ~10 s, e o pedido de 1 a 3
   trechos corridos de 15-20 min — cada um com o que tirar de dentro: leitura
   de superchat fora do assunto, problema técnico ("caiu o som", "tá me
   ouvindo?"), recado do canal, desvio que não dá contexto nenhum. É um
   trecho só, com buracos, não uma coletânea.
2. Para cada trecho aprovado, o texto que sobra e o pedido de títulos: 8
   candidatos e o escolhido, com o porquê. O título é o que decide o clique
   do vídeo longo, então ganha uma chamada inteira só para ele.

O silêncio longo (> 4 s) não entra aqui — é medido no áudio na produção
(estudio/video_longo.py).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import yaml

import deepseek_client
import segmenter as sg
from segmentador_llm import _json

AQUI = Path(__file__).resolve().parent
ENDPOINT = "/chat/completions"
log = logging.getLogger("fase1")

_TETO_TOKENS = 90_000   # deepseek-chat tem 128K de contexto; sobra para a resposta
TITULO_MAXIMO = 75      # o prompt pede 70; acima disso o celular corta

_SISTEMA_TRECHOS = """Você é editor de um canal de cortes políticos no YouTube. Recebe a \
transcrição de uma live (ou entrevista, debate, podcast), em linhas "[ID h:mm:ss] texto", onde \
h:mm:ss é quando a linha começa, e escolhe trechos para virar VÍDEO LONGO (não short): \
15 a 20 minutos que alguém assiste do começo ao fim.

O que faz um bom vídeo longo:
- UM fio condutor com começo, meio e fim: uma discussão, uma entrevista, uma polêmica \
explicada e debatida, um embate que se desenvolve. Em live de plantão o assunto muda a cada \
5-10 minutos: aí o trecho pode juntar assuntos VIZINHOS que têm um fio comum (o mesmo \
escândalo visto de vários lados, a mesma eleição, o mesmo convidado, o mesmo adversário) — \
desde que quem assiste perceba o fio. Assuntos sem ligação nenhuma não formam um vídeo.
- Começa num ponto que prende e que se entende sem ter visto o resto da live: a pergunta, \
a notícia, a provocação que abre o assunto. Nunca em cumprimento, abertura de programa ou \
no meio de uma resposta.
- Termina quando o assunto fecha (conclusão, tirada, mudança de tema) — nunca no meio de \
um raciocínio.
- Tem conteúdo forte ao longo do trecho, não só no começo.

DURAÇÃO: o que SOBRA depois dos cortes internos deve ficar entre 15 e 20 minutos. Meça \
pelos tempos das linhas. Trecho que não chega a 15 minutos de conteúdo bom não serve.

É um trecho corrido, mas com buracos: dentro dele, marque em "remover" o que não dá \
contexto nenhum ao assunto e atrapalha quem assiste depois:
- leitura de superchat/comentário que não tem a ver com o tema (se o superchat puxa ou \
alimenta o assunto, fica);
- problema técnico: "caiu o áudio", "tá me ouvindo?", "volta a câmera", espera de convidado;
- recado do canal: pedido de like, inscrição, PIX, propaganda, sorteio, anúncio de outro \
programa;
- conversa paralela ou desvio que sai do assunto e depois volta.
NÃO remova o que muda o sentido da fala, nem pedaços curtos de transição necessários para \
entender a continuação. Cada remoção vai de uma linha inteira até outra linha inteira. \
Na dúvida, não remova.

Responda APENAS com JSON:
{"longos": [{"inicio_id": "L0120", "fim_id": "L0245",
  "remover": [{"inicio_id": "L0150", "fim_id": "L0158", "motivo": "leitura de superchat fora do tema"}],
  "tema": "o assunto em uma frase",
  "comentario": "por que isso rende um bom vídeo longo, em 1-2 frases"}]}

- De 0 a 3 trechos, que não se sobreponham, do mais forte para o mais fraco. QUALIDADE \
ACIMA DE QUANTIDADE: se a live não tem um fio que sustente 15 minutos, devolva \
{"longos": []}.
- "tema": o fio condutor, em uma frase.
- Use só IDs que apareceram na transcrição. Escolha sem tomar partido nem distorcer a fala."""

_SISTEMA_TITULO = """Você escreve títulos de vídeos longos (15-20 min) para um canal de cortes \
políticos no YouTube, em português do Brasil. Recebe o nome da live de origem, o tema e a fala \
do trecho. O título é o que decide se a pessoa clica — e o vídeo precisa entregar o que o \
título promete (o YouTube pune quem abandona nos primeiros segundos).

Como é um bom título:
- ATÉ 70 CARACTERES, contando os espaços (o que passa disso some no celular). Conte antes \
de responder: título mais longo que isso não serve.
- Nome de quem fala ou de quem é alvo, quando for conhecido — nome de gente gera busca e \
clique.
- O conflito, a revelação ou a pergunta concreta do trecho, não o assunto genérico. Ruim: \
"Debate sobre a economia". Bom: "Fulano encurrala ministro sobre o rombo de R$ 40 bi".
- Curiosidade sem mentira: promete o que está na fala, deixa a pessoa querendo ver a \
resposta ou o desfecho.
- No máximo uma ou duas palavras em CAIXA ALTA para dar ênfase; nada de título todo em \
maiúsculas, nada de emoji, nada de hashtag, nada de "INCRÍVEL" ou "você não vai acreditar".
- Não imputa crime a quem a fala não imputa; aspas só para fala que existe no trecho.
- Se a própria conversa põe em dúvida o que está sendo mostrado (pode ser fake, montagem, \
boato), o título não trata aquilo como fato: pergunta ou atribui ("diz", "segundo") — a \
uma pessoa pelo nome, nunca a "live" ou "programa".

Escreva 8 títulos bem diferentes entre si (ângulos diferentes: a fala mais forte entre \
aspas, o confronto, a pergunta, a revelação, o número) e escolha o melhor.

Responda APENAS com JSON:
{"titulos": ["...", "..."], "escolhido": "o melhor, igual a um da lista", "motivo": "por que este"}"""


def _hms(s: float) -> str:
    m, s = divmod(int(s), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}"


def agrupa(segs: list[dict], alvo: float) -> list[dict]:
    """Junta segmentos vizinhos em linhas de pelo menos `alvo` segundos — menos
    linhas, menos tokens, e o limite de um trecho longo não precisa de mais
    precisão que isso."""
    linhas, atual = [], None
    for s in segs:
        if atual is None:
            atual = {"start": s["start"], "end": s["end"], "text": s["text"].strip()}
        else:
            atual["end"] = s["end"]
            atual["text"] += " " + s["text"].strip()
        if atual["end"] - atual["start"] >= alvo:
            linhas.append(atual)
            atual = None
    if atual:
        linhas.append(atual)
    return linhas


def _linhas_prompt(segs: list[dict]) -> tuple[list[dict], list[str]]:
    for alvo in (10, 15, 20, 30, 45):
        linhas = agrupa(segs, alvo)
        texto = [f"[L{i:04d} {_hms(l['start'])}] {l['text']}" for i, l in enumerate(linhas)]
        if sum(len(t) for t in texto) // 4 <= _TETO_TOKENS:
            return linhas, texto
    return linhas, texto


def _perguntar(mensagens: list[dict], cfg: dict, max_tokens: int, temperatura: float) -> tuple[dict, str]:
    """Devolve (JSON da resposta, texto bruto — para continuar a conversa)."""
    resp = deepseek_client.post(ENDPOINT, {
        "model": cfg["modelo"],
        "temperature": temperatura,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
        "messages": mensagens,
    }, cfg.get("timeout", 300), cfg.get("tentativas", 3))
    try:
        conteudo = resp["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise deepseek_client.APIError(f"resposta sem conteúdo: {exc}") from exc
    return _json(conteudo, deepseek_client.APIError), conteudo


def _intervalos(cortes: list[tuple[float, float, str]]) -> list[tuple[float, float, str]]:
    """Ordena e junta remoções que se tocam."""
    saida: list[list] = []
    for a, b, motivo in sorted(cortes):
        if saida and a <= saida[-1][1] + 0.5:
            saida[-1][1] = max(saida[-1][1], b)
            if motivo not in saida[-1][2]:
                saida[-1][2] += "; " + motivo
        else:
            saida.append([a, b, motivo])
    return [tuple(x) for x in saida]


def achar_trechos(segs: list[dict], cfg: dict, titulo_live: str = "") -> tuple[list[dict], str]:
    """Os trechos aprovados e, sem nenhum, o porquê (aparece no trabalho, no Estúdio)."""
    linhas, texto = _linhas_prompt(segs)
    por_id = {f"L{i:04d}": l for i, l in enumerate(linhas)}
    log.info("   transcrição em %d linhas (~%d tokens)", len(linhas), sum(len(t) for t in texto) // 4)
    cabeca = f"Live: {titulo_live}\n\n" if titulo_live else ""
    conversa = [{"role": "system", "content": _SISTEMA_TRECHOS},
                {"role": "user", "content": cabeca + "\n".join(texto)}]
    dados, bruto_txt = _perguntar(conversa, cfg, 4000, 0.3)
    trechos, problemas = _valida(dados, por_id, cfg)
    if problemas:
        # Uma segunda chance: o modelo erra a conta da duração com frequência
        # (no teste de 30/09, um trecho de 26 min e outro de 12 min).
        log.info("   pedindo correção de %d trecho(s) fora da faixa", len(problemas))
        conversa += [{"role": "assistant", "content": bruto_txt},
                     {"role": "user", "content": "Refaça a lista inteira corrigindo isto:\n- " + "\n- ".join(problemas)
                      + f"\nO que sobra de cada trecho precisa ficar entre {cfg['duracao_minima'] + 1} e "
                        f"{cfg['duracao_maxima'] - 1} minutos: encurte (começo mais adiante, fim antes, ou "
                        "remova mais do que não dá contexto) ou estenda até o assunto fechar. Os trechos "
                        "que já estavam bons podem ficar iguais."}]
        primeira = dados
        try:
            dados, _ = _perguntar(conversa, cfg, 4000, 0.3)
        except deepseek_client.APIError as exc:
            log.warning("   correção falhou (%s); fica a primeira resposta", exc)
            dados = primeira
        # Última palavra: o que ainda passa do teto perde o fim (ver _aparar) em
        # vez de sumir — em 02/10 a live inteira ficou sem longo porque o único
        # trecho bom deixava 21,8 min, e o DeepSeek não acertou a conta nem na
        # segunda tentativa.
        corrigidos, problemas = _valida(dados, por_id, cfg, aparar=True)
        if len(corrigidos) >= len(trechos):
            trechos = corrigidos
        for p in problemas:
            log.info("   ainda fora da faixa: %s", p)
    motivo = "" if trechos else (
        "nenhum trecho de 15–20 min com um fio só" + (f" ({'; '.join(problemas)})" if problemas else ""))
    return trechos, motivo


def _sobra(inicio: float, final: float, remover: list[tuple[float, float, str]]) -> float:
    """O que fica do trecho depois das remoções (só as que caem antes de `final`)."""
    return (final - inicio) - sum(min(b, final) - a for a, b, _ in remover if a < final)


def _aparar(inicio: float, final: float, remover: list[tuple[float, float, str]],
            linhas: list[dict], alvo: float) -> float | None:
    """Trecho que passa do teto: o fim recua, linha a linha (sempre no fim de
    uma linha da transcrição, nunca no meio de uma frase), até o que sobra
    caber em `alvo`. None se nem assim couber."""
    for linha in reversed(linhas):
        if inicio < linha["end"] < final and _sobra(inicio, linha["end"], remover) <= alvo:
            return linha["end"]
    return None


def _valida(dados: dict, por_id: dict, cfg: dict, aparar: bool = False) -> tuple[list[dict], list[str]]:
    """Os trechos que servem e, dos que não servem pela duração, o porquê
    (vai no pedido de correção). Com `aparar`, o que passa do teto perde o fim
    até caber nos minutos que o prompt pede (duracao_maxima - 1)."""
    minimo, maximo = cfg["duracao_minima"] * 60, cfg["duracao_maxima"] * 60
    trechos: list[dict] = []
    problemas: list[str] = []
    for bruto in dados.get("longos") or []:
        ini, fim = por_id.get(bruto.get("inicio_id")), por_id.get(bruto.get("fim_id"))
        if not ini or not fim or fim["end"] <= ini["start"]:
            log.warning("   trecho com ID inexistente ignorado: %s", {k: bruto.get(k) for k in ("inicio_id", "fim_id")})
            continue
        inicio, final = ini["start"], fim["end"]
        remover = []
        for r in bruto.get("remover") or []:
            a, b = por_id.get(r.get("inicio_id")), por_id.get(r.get("fim_id"))
            if not a or not b or b["end"] <= a["start"]:
                continue
            a_s, b_s = max(a["start"], inicio), min(b["end"], final)
            if b_s - a_s >= 1 and (a_s > inicio or b_s < final):   # nunca o trecho inteiro
                remover.append((round(a_s, 3), round(b_s, 3), str(r.get("motivo") or "").strip()))
        remover = _intervalos(remover)
        sobra = _sobra(inicio, final, remover)
        comentario = str(bruto.get("comentario") or "").strip()
        novo_fim = _aparar(inicio, final, remover, list(por_id.values()), maximo - 60)             if aparar and sobra > maximo else None
        if novo_fim is not None:
            log.info("   trecho %s-%s: sobravam %.1f min — fim antecipado para %s", _hms(inicio), _hms(final),
                     sobra / 60, _hms(novo_fim))
            comentario += f" (fim encurtado em {(final - novo_fim) / 60:.1f} min para caber em 20 min)"
            final = novo_fim
            remover = [(a, min(b, final), m) for a, b, m in remover if a < final - 1]
            sobra = _sobra(inicio, final, remover)
        if not minimo <= sobra <= maximo:
            log.info("   trecho %s-%s ignorado: sobram %.1f min (faixa %.0f-%.0f)", _hms(inicio), _hms(final),
                     sobra / 60, cfg["duracao_minima"], cfg["duracao_maxima"])
            problemas.append(f"o trecho {bruto.get('inicio_id')}-{bruto.get('fim_id')} "
                             f"({str(bruto.get('tema') or '')[:60]}) deixa {sobra / 60:.1f} minutos")
            continue
        if any(min(final, t["fim"]) - max(inicio, t["inicio"]) > 60 for t in trechos):
            log.info("   trecho %s-%s ignorado: sobrepõe outro", _hms(inicio), _hms(final))
            continue
        trechos.append({"inicio": round(inicio, 3), "fim": round(final, 3),
                        "remover": [{"inicio": a, "fim": b, "motivo": m} for a, b, m in remover],
                        "duracao_estimada": round(sobra, 1),
                        "tema": str(bruto.get("tema") or "").strip(),
                        "comentario": comentario.strip()})
    return trechos, problemas


def titular(trecho: dict, segs: list[dict], cfg: dict, titulo_live: str = "") -> dict:
    """8 títulos e o escolhido, lendo só o que fica no vídeo."""
    fica = [s for s in sg.trecho(segs, trecho["inicio"], trecho["fim"])
            if not any(r["inicio"] <= (s["start"] + s["end"]) / 2 <= r["fim"] for r in trecho["remover"])]
    fala = sg.texto(fica)
    if len(fala) > 60_000:          # ~20 min de fala cabe folgado; só por garantia
        fala = fala[:60_000]
    usuario = f"Live de origem: {titulo_live or '(sem nome)'}\nTema: {trecho['tema']}\n\nFala do trecho:\n{fala}"
    dados, _ = _perguntar([{"role": "system", "content": _SISTEMA_TITULO},
                           {"role": "user", "content": usuario}], cfg, 1500, 0.7)
    titulos = [re.sub(r"\s+", " ", str(t)).strip() for t in dados.get("titulos") or [] if str(t).strip()]
    escolhido = re.sub(r"\s+", " ", str(dados.get("escolhido") or "")).strip() or (titulos[0] if titulos else "")
    if len(escolhido) > TITULO_MAXIMO:
        # o modelo não conta letra direito: fica o primeiro da lista dele que cabe
        curto = next((t for t in titulos if len(t) <= TITULO_MAXIMO), None)
        if curto:
            log.info("   título de %d letras trocado pelo primeiro que cabe em %d", len(escolhido), TITULO_MAXIMO)
            escolhido = curto
    # o escolhido na frente: é a primeira opção que aparece no Estúdio
    titulos = [escolhido] + [t for t in titulos if t != escolhido] if escolhido else titulos
    return {"titulo": escolhido[:100], "titulos": [t[:100] for t in titulos][:8],
            "motivo_titulo": str(dados.get("motivo") or "").strip()}


def rodar(ws: Path, cfg: dict, titulo_live: str) -> int:
    saida = ws / "longos.json"
    if saida.exists():
        log.info("   longos.json já existe — reaproveitado")
        return 0
    dados = json.loads((ws / "transcricao.json").read_text(encoding="utf-8"))
    segs = dados["segmentos"]
    total = segs[-1]["end"] - segs[0]["start"] if segs else 0
    blocos: list[dict] = []
    motivo = ""
    if total < cfg["duracao_minima"] * 60 + 60:
        motivo = f"vídeo de {total / 60:.0f} min: curto demais para um vídeo longo"
        log.info("   %s", motivo)
    else:
        trechos, motivo = achar_trechos(segs, cfg, titulo_live)
        for i, t in enumerate(trechos, 1):
            t["id"] = f"longo_{i:02d}"
            try:
                t.update(titular(t, segs, cfg, titulo_live))
            except Exception as exc:  # noqa: BLE001 — sem título da IA, o tema serve de reserva
                log.warning("   %s: título falhou (%s); fica o tema", t["id"], exc)
                t.update(titulo=t["tema"][:100], titulos=[], motivo_titulo="")
            log.info("   %s %s-%s  ~%.1f min, %d corte(s) interno(s)  «%s»", t["id"], _hms(t["inicio"]),
                     _hms(t["fim"]), t["duracao_estimada"] / 60, len(t["remover"]), t["titulo"])
            blocos.append(t)
    tmp = saida.with_suffix(".tmp")
    tmp.write_text(json.dumps({"blocos": blocos, "motivo": motivo}, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(saida)
    log.info("   %d trecho(s) longo(s) → %s", len(blocos), saida)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Vídeo longo: trechos de 15-20 min da live.")
    ap.add_argument("video", type=Path)
    ap.add_argument("--workspace", type=Path, required=True, help="a pasta da Fase 1 (com transcricao.json)")
    ap.add_argument("--config", type=Path, default=AQUI / "config.yaml")
    ap.add_argument("--titulo-live", default="")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8"))["longo"]
    if not (args.workspace / "transcricao.json").exists():
        log.error("sem transcricao.json em %s (rode a Fase 1 antes)", args.workspace)
        return 1
    try:
        deepseek_client.api_key()
    except deepseek_client.APIError as exc:
        log.error("%s", exc)
        return 2
    return rodar(args.workspace, cfg, args.titulo_live or args.video.stem)


if __name__ == "__main__":
    sys.exit(main())
