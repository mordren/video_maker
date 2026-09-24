"""Detecta recomeço de frase: a pessoa começa a dizer algo, para, e recomeça
repetindo o início — comum em fala espontânea, deixa o corte visivelmente sujo.

Conservador de propósito: só corta quando a MESMA sequência de `min_palavras`+
palavras se repete literalmente, com uma pausa curta entre a tentativa
abortada e o recomeço. Isso evita confundir com ênfase retórica intencional
("não, não, não vou aceitar isso") — que normalmente repete só 1 palavra, ou
tem um ritmo de fala diferente (sem a mesma pausa "de hesitação").

Ainda assim é uma heurística, não uma certeza — por isso fica fácil desligar
(`recomeco.ativo: false` no config) e cada corte grava o motivo, para revisão.
"""

from __future__ import annotations


def _norm(palavra: str) -> str:
    return palavra.strip().lower().strip(".,!?;:…\"'—-")


def detectar_recomecos(palavras: list[dict], min_palavras: int, max_palavras: int,
                       gap_maximo: float, gap_minimo: float = 0.12) -> list[dict]:
    """`palavras`: [{"word", "start", "end"}, ...] em ordem, tempos absolutos (do vídeo).

    Devolve os intervalos do início abortado a cortar — mantém a segunda
    tentativa (a que segue até completar a frase). Ganancioso: tenta primeiro
    o maior prefixo repetido a partir de cada posição.
    """
    normed = [_norm(p["word"]) for p in palavras]
    n = len(palavras)
    cortes: list[dict] = []
    i = 0
    while i <= n - min_palavras:
        achou = _melhor_repeticao(normed, palavras, i, min_palavras, max_palavras, gap_maximo, gap_minimo)
        if achou:
            j_novo, tam = achou
            trecho = " ".join(p["word"] for p in palavras[i:i + tam])
            cortes.append({
                "inicio": palavras[i]["start"],
                "fim": palavras[j_novo]["start"],
                "tipo": "recomeco",
                "motivo": f'recomeço de frase: "{trecho}" repetido',
            })
            i = j_novo
        else:
            i += 1
    return cortes


def _melhor_repeticao(normed: list[str], palavras: list[dict], i: int, min_palavras: int,
                      max_palavras: int, gap_maximo: float, gap_minimo: float) -> tuple[int, int] | None:
    """A partir de `i`, procura o maior `tam` (min_palavras..max_palavras) tal que
    normed[i:i+tam] reaparece logo em seguida, com uma pausa real (não fala
    contínua) entre a tentativa abortada e o recomeço — devolve (posição onde o
    recomeço reaparece, tam).

    O gap mínimo é o que separa hesitação genuína de repetição retórica
    intencional ("não, não vou aceitar", "um idiota, ele é um idiota
    perigoso"): quem tropeça e recomeça faz uma pausa perceptível; quem repete
    para dar ênfase emenda a frase sem pausa nenhuma.
    """
    n = len(normed)
    limite = min(max_palavras, (n - i) // 2)
    for tam in range(limite, min_palavras - 1, -1):
        prefixo = normed[i:i + tam]
        j = i + tam
        # a repetição pode começar logo em seguida (j) ou com 1-2 palavras de
        # sobra no meio (hesitação tipo "eu... eu acho, eu acho que")
        for offset in range(0, 3):
            k = j + offset
            if k + tam > n:
                continue
            if normed[k:k + tam] != prefixo:
                continue
            # a pausa que importa é sempre a que precede k (onde a repetição de
            # fato começa) — com offset > 0 isso não é o fim do prefixo original,
            # é o fim da última palavra de transição ("idiota [ele é] um idiota"
            # não pode medir o gap entre "idiota" e "um": ali no meio é fala
            # contínua da mesma frase, não a pausa de hesitação).
            gap = palavras[k]["start"] - palavras[k - 1]["end"]
            if gap_minimo <= gap <= gap_maximo:
                return k, tam
    return None
