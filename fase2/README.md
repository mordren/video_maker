# Fase 2: depuração mecânica dos blocos

Pega o `blocos_finais.json` que a Fase 1 produziu e, para cada bloco, gera um clipe pronto:
pausas longas encolhidas, recomeço de frase removido, silêncio nas bordas aparado, volume
normalizado. Sem IA nesta etapa — tudo aqui é sinal de áudio e regra determinística.

O gancho de 2,5s (com JEV) **não está incluído ainda** — é uma peça separada, que entra
depois que a base mecânica estiver validada de ouvido.

## Configurar

```
pip install -r fase2/requirements.txt
```

Precisa do FFmpeg (com `ffprobe`) no PATH e do Whisper — os dois já são dependência da Fase
1/do app. Não precisa de nenhuma chave de API: esta fase não chama IA nenhuma.

## Rodar

```
python fase2/pipeline.py <caminho>/blocos_finais.json
```

- Salva em `<pasta do blocos_finais.json>/cortes/` por padrão, ou em `--saida <pasta>`.
- Cada bloco vira `cortes/<id>.mp4` mais `cortes/<id>/relatorio.json` (todo corte aplicado,
  com o motivo) e `cortes/<id>/bruto.json` (a transcrição por palavra do Whisper, para
  auditoria ou para a próxima etapa reaproveitar).
- Um bloco que falhar não derruba os outros; o erro fica registrado em
  `cortes/relatorio_geral.json`.

## Por que roda Whisper de novo, se a Fase 1 já transcreveu?

A Fase 1 quase sempre usa o `.srt` do vídeo (rota mais rápida e barata), que só tem
granularidade de **segmento** (frase), não de palavra. Cortar recomeço de frase com
precisão — sem comer a palavra vizinha — exige saber o tempo exato de cada palavra. Rodar
Whisper aqui, nos ~6-8 blocos que sobraram da Fase 1 (não no vídeo inteiro), é barato: o
gargalo real do pipeline é o Whisper (~85-90s por bloco de ~1min, modelo `small` em CPU) —
o resto (silêncio, recomeço, render) leva uns 10-15s por bloco.

## O que cada etapa faz

1. **Recorte bruto** — `ffmpeg -ss/-t -c copy` do vídeo original, só o intervalo do bloco.
   Rápido (sem reencode) porque o corte fino de verdade é feito depois, na renderização.
2. **Transcrição por palavra** — Whisper com `--word_timestamps` nesse recorte.
3. **Silêncio** (`silencio.py`) — `ffmpeg silencedetect` acha as pausas; uma pausa mais
   longa que `limiar_corte` (0,8s por padrão) é **encolhida**, não removida inteira, para
   `duracao_alvo` (0,25s) — cortar a pausa toda deixaria o corte sem ar, tudo jump cut.
   Silêncio sobrando nas bordas do bloco é aparado (`bordas.max_silencio_borda`).
4. **Recomeço de frase** (`recomeco.py`) — a pessoa começa a dizer algo, para, recomeça
   repetindo o início ("eu acho, eu acho que..."). Só corta repetição literal de 2+
   palavras com uma pausa real entre as duas tentativas (`gap_minimo`..`gap_maximo`).
5. **Render** (`renderiza.py`) — os cortes de silêncio e recomeço se mesclam num único
   plano; o complementar (o que sobra) é concatenado com `trim`/`atrim` + `concat`, com um
   fade curto (`crossfade_ms`) em cada junção interna para não soar um "clique", e
   `loudnorm` no áudio inteiro.

## Um falso-positivo real que apareceu no teste (e o que ele ensina)

Testando com vídeo real, o detector de recomeço pegou "considere Eduardo **um idiota**, ele
é **um idiota** perigoso" como se fosse hesitação — cortando o primeiro "um idiota" fora.
Não é: é repetição retórica intencional (dobrar a ênfase), dita de corrida, sem pausa.

O bug: o gap entre a primeira e a segunda menção estava sendo medido do jeito errado quando
havia palavras no meio ("ele é") — pegava a transição normal da própria frase, não uma pausa
de hesitação. Corrigido para medir sempre a pausa imediatamente antes de onde a repetição
recomeça. Depois disso, esse caso passou a exigir `gap_minimo` (0,12s) — que a repetição
retórica, dita fluida, não tem — e parou de ser cortado.

Fica como lembrete de que esta heurística é conservadora mas não infalível: sempre vale
ouvir o resultado, não só olhar a contagem de cortes. Cada corte grava o motivo em
`relatorio.json` — é o primeiro lugar a olhar se um clipe saiu estranho.

## Limitações conhecidas

- **Recorte inicial usa `-c copy`** (sem reencode) — corta no keyframe mais próximo do
  timestamp pedido, não exatamente nele. Pode entrar meio segundo de sobra no começo/fim do
  recorte bruto; o trim de bordas (etapa 3) deveria absorver isso na maioria dos casos.
- **`loudnorm` de passada única**, não as duas passadas que dão a medição mais precisa —
  mais rápido, ligeiramente menos exato. Trocar por two-pass é uma melhoria futura barata
  se a normalização não ficar precisa o suficiente nos testes.
- **A heurística de recomeço ainda pode ter falso-positivo/negativo** em casos que o teste
  não cobriu. `recomeco.ativo: false` desliga essa etapa inteira sem afetar o resto.
