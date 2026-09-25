# Fase 1: mineração de cortes virais

Dois pipelines na mesma pasta, um alimenta o outro:

- **`pipeline.py`** — recebe um vídeo longo e devolve `blocos_finais.json`: os melhores
  trechos para virar corte, com timestamp exato, ranqueados. Não corta o vídeo.
- **`pipeline_cortes.py`** — recebe esse `blocos_finais.json` e gera, para cada bloco, um
  `.mp4` pronto: pausas longas encolhidas, recomeço de frase removido, silêncio de borda
  aparado, volume normalizado, abertura de ~2-3s. Sem IA além da abertura.

## Rodar tudo de uma vez

```
python fase1/rodar_tudo.py "caminho/do/video.mp4"
```

Roda a seleção e, se sair pelo menos um bloco, já corta cada um em seguida — um comando só.
Se o vídeo não tiver nenhum trecho forte o bastante, para depois da seleção (não tem sentido
chamar o corte sem nada para cortar).

No Windows, `Cortar.bat` faz o mesmo com duplo clique: pede o caminho do vídeo, ou arraste o
arquivo de vídeo por cima do `.bat` que ele já roda com esse caminho.

Rodar cada parte separada (útil para conferir os candidatos antes de gastar tempo com o
corte, ou para reprocessar só uma das duas) está documentado abaixo, em cada parte.

## Configurar

1. Dependência: `pip install -r fase1/requirements.txt` (PyYAML + Whisper). FFmpeg (com
   `ffprobe`) precisa estar no PATH — usado pelos dois pipelines.

2. **Chave do OpenRouter (JEV):** abra `fase1/.env` e preencha `OPENROUTER_API_KEY` com a sua
   chave do OpenRouter (https://openrouter.ai/settings/keys). O `.env` está no `.gitignore`.
   Os dois pipelines usam: `pipeline.py` na etapa 4, `pipeline_cortes.py` só na abertura
   (`abertura.ativo: false` no `config_cortes.yaml` tira essa dependência do segundo).

3. **Chave do DeepSeek (segmentação + revisão fina):** o script procura nos seguintes
   lugares, nesta ordem:
   - `DEEPSEEK_API_KEY` no `fase1/.env`
   - Variável de ambiente `DEEPSEEK_API_KEY`
   - Arquivo de config do Corta+Legenda: `~\AppData\Local\CortaLegenda\config.json` (Windows)
   - Se não achar em nenhum lugar, o script para e avisa.

   (Testamos um modelo gratuito do OpenRouter para a revisão fina, mas o rate limit ficou
   ruim demais — 4 min para 44 blocos, com vários timeouts. Voltamos para o DeepSeek: o
   mesmo trabalho levou 17s.)

---

# Parte A — `pipeline.py`: seleção de blocos

A segmentação é **semântica, não por tempo fixo**: o DeepSeek lê a transcrição inteira,
numa chamada só, e aponta ele mesmo onde cada bloco candidato começa e termina — os
trechos mais fortes e polêmicos para virar corte, não um recorte por assunto qualquer.
O prompt é o mesmo que o app já usa em produção no painel "Cortes IA"
(`ai_srt._CUTS_PROMPT`), adaptado só para referenciar ID de segmento em vez de escrever
o tempo de cabeça. Ver "Por que mudamos" mais abaixo.

## Rodar

```
python fase1/pipeline.py "caminho/do/video.mp4"
```

- Cria `workspace_<data_hora>/` na pasta atual, com um JSON por etapa e o `pipeline.log`.
- `--ate-etapa 2` para depois da transcrição, sem chamar nenhuma API.
- `--ate-etapa 3` para depois da segmentação, gastando só a API do DeepSeek (bem mais barata
  que rodar o JEV/LLM também) — bom para conferir se os candidatos fazem sentido antes de
  gastar o resto.
- `--workspace <pasta>` retoma uma execução: o que já foi feito é reaproveitado e só os
  itens que falharam (ou que ainda não rodaram) voltam para a API.

## SRT ou Whisper

O script procura uma legenda ao lado do vídeo, nesta ordem: `video.srt`, `video.pt*.srt`
(pt-BR > pt > pt-orig), `video.en.srt` e depois qualquer `video.*.srt`. Usa a primeira
que for legível e tiver fala. Aí o Whisper não roda. A legenda automática do YouTube
é limpa: as repetições da legenda "rolante" e anotações como `[música]` saem.

Sem SRT utilizável, roda o Whisper com `--word_timestamps True`. A fonte usada fica em
`fonte_transcricao.txt` e no log.

Consequência para a Parte B: vindo de SRT, `palavras` sai vazio e
`"granularidade": "segmento"`; vindo do Whisper, `palavras` traz o tempo de cada palavra —
mas isso pouco importa na prática, porque `pipeline_cortes.py` roda o Whisper de novo por
bloco de qualquer forma (ver por quê, na Parte B).

## Etapas

| # | O quê | Arquivo |
|---|---|---|
| 1 | Áudio WAV mono 16 kHz | `audio.wav` |
| 2 | Transcrição (SRT ou Whisper) | `transcricao.json`, `fonte_transcricao.txt` |
| 3 | **Segmentação semântica** — DeepSeek lê a transcrição inteira (numa chamada só, salvo se estourar o limite de contexto) e escolhe os melhores cortes por ID de segmento: título curto e comentário com o porquê, duração-alvo de 1min-2min30 | `candidatos.json`, `blocos_candidatos.json` |
| 4 | **JEV qualifica**, numa chamada por bloco: `score` viral de 1 a 5, `noul` de ritmo, `noul` de precisa-ajustar e `choice` de onde afinar início/fim | `jev_qualificacao.json` |
| 5 | Deduplicação (sobreposição > 70% → fica o de maior score) | `blocos_jev.json` |
| 6 | LLM (DeepSeek): coerência, coesão, problemas e sugestão de corte — só nos blocos que sobraram da consolidação | `llm_revisao.json`, `blocos_llm.json` |
| 7–8 | Ranqueamento (viral 0,5 · ritmo 0,2 · LLM 0,3) | `blocos_finais.json` |

Os parâmetros ficam em `config.yaml`: faixa de duração do bloco, pesos e modelos.

## Por que mudamos (v1 → v2 → v3)

**v1:** o vídeo era cortado em janelas de tempo fixo (45s, depois 90s) com sobreposição, e o
JEV filtrava "isso é coerente?" antes de gastar o LLM. Dois problemas apareceram testando
com vídeo real:

1. **Uma grade de tempo fixo corta às cegas.** Um gancho forte raramente começa exatamente
   num múltiplo de 90s do início do vídeo.
2. **O filtro do JEV não tinha poder discriminativo real.** Pedir "começo e fim perfeitos"
   numa janela cortada às cegas reprovava quase tudo (média de 0,13 de coerência). Suavizar
   a pergunta resolveu a rejeição, mas o preço foi o filtro aprovar quase tudo (44 de 45) —
   deixou de filtrar de verdade.
3. A v1 também fazia **3 chamadas separadas ao JEV por bloco**, quando a Decisions API
   responde várias perguntas em paralelo numa única chamada sem custo extra de latência.

**v2:** inverteu a ordem — DeepSeek segmenta por conteúdo (aponta os próprios limites dos
blocos), JEV qualifica os candidatos numa chamada só. Usava um prompt de segmentação
genérico escrito do zero, dividindo a transcrição em pedaços de ~15 min. Funcionou (34
candidatos, cada um com assunto reconhecível), mas dois pontos ficaram capengas:

1. **O prompt era genérico.** O app já tinha, em produção, um prompt testado e específico
   para o que este projeto realmente quer (`ai_srt._CUTS_PROMPT`, painel "Cortes IA"): pede
   categorias de gancho (declaração-tese, ataque nominal, contradição, carga emocional...),
   duração obrigatória de 1min-2min30, "pegar o raciocínio inteiro, não só a frase de
   efeito" e "qualidade acima de quantidade" — pode devolver poucos cortes, ou nenhum.
2. **O chunking em pedaços de 15 min era desnecessário.** O deepseek-chat tem contexto de
   64K tokens; a transcrição inteira de uma live de 56min tem uns 17K — cabe numa chamada
   só. Dividir sem necessidade só multiplicava chamadas e ainda arriscava cortar um bloco
   forte bem na fronteira de dois pedaços.

**v3 (atual):** troca o prompt genérico pelo `_CUTS_PROMPT` do app (adaptado para referenciar
ID de segmento em vez de escrever "m:ss" de cabeça) e manda a transcrição inteira numa
chamada só — só divide em pedaços (grandes, não de 15 min) se estourar um teto de tokens de
segurança. Testado no mesmo vídeo: 1 chamada, ~17K tokens, 8 candidatos (rigorosos:
"LACAIO DA FAMÍLIA TRUMP", "NÃO CUMPRO DECISÃO ILEGAL"...), todos com título e comentário
de por que funcionam como short, pipeline inteiro em ~15s do zero.

## Detalhes que vale saber

- **O DeepSeek não inventa timestamp.** A transcrição é mandada numerada por segmento
  (`[s0042] texto`); o modelo só aponta o ID de início/fim de cada bloco, nunca escreve o
  tempo — o tempo real vem sempre da transcrição. Isso evita o erro comum de LLM arredondar
  ou inventar um número decimal.
- **Segmentação é 1 chamada na imensa maioria dos vídeos.** Só existe pedaço (chunking) por
  limite de contexto do modelo — a transcrição inteira é tentada primeiro. Se estourar o
  teto de tokens (`_TETO_TOKENS_PEDACO` em `segmentador_llm.py`), divide no menor número de
  pedaços que cabe, com margem de contexto nas bordas para não perder um bloco que atravessa
  a fronteira. Um candidato só é aceito se pelo menos uma ponta (início ou fim) cai dentro do
  núcleo do pedaço — evita duplicar o mesmo bloco em dois pedaços vizinhos.
- **O prompt de seleção é o mesmo do app** (painel "Cortes IA"), então herda as mesmas
  regras: qualidade acima de quantidade (pode devolver poucos cortes por vídeo, ou nenhum),
  duração-alvo de 1-2min30, e o aviso "⚠️" no comentário quando o corte imputa crime ou
  xinga alguém identificável.
- **O JEV faz um afinamento fino de borda, não um redesenho do bloco.** As opções de
  início/fim que ele recebe são só os primeiros/últimos `opcoes_limite` segmentos do bloco
  já proposto pela segmentação — o grosso do corte já veio semântico.
- **O score viral sai fracionário** (ex.: 3,7). É a média dos níveis de 1 a 5 ponderada
  pelas probabilidades que o JEV devolve. É o sinal mais especulativo do ranqueamento — o
  JEV só vê texto, sem tom de voz nem expressão; vale desconfiar dele sem validar contra
  desempenho real (o app já grava `historico.json` de vídeos publicados, dá para cruzar).
- **O JEV recebe o `gancho`/`comentario` que o DeepSeek já escreveu ao escolher o corte
  (etapa 3), não só o texto pelado.** Testado sem isso: a ordem de força que o próprio
  DeepSeek já dá aos candidatos (ele ordena por força, é regra do prompt) não tinha
  correlação nenhuma com o score do JEV (~0,0 em dois vídeos testados) — o JEV estava
  rejulgando do zero, com menos informação que a triagem editorial já tinha. Com o
  contexto, a correlação subiu para +0,49 e +0,67: concorda bastante, mas não copia (não é
  1,0) — sinal de que ainda está julgando por conta própria, só que informado.
- **Os limites sempre caem na borda de um segmento.** Depois de cada ajuste (JEV ou LLM),
  texto e palavras são recalculados a partir da transcrição. Os ajustes ficam em `ajustes`.
- **O LLM da etapa 6 só mexe dentro do que viu:** o bloco mais 20s de contexto de cada lado.
  Nenhum ajuste deixa o bloco com menos que `bloco.duracao_minima`.
- **Falha de API não derruba o script.** O item fica com `"erro"` no JSON da etapa, o resto
  segue, e `--workspace` repete só os que falharam. Sem análise do LLM, o bloco recebe
  nota 0,5 nessa parte. Um pedaço de segmentação que falhar (etapa 3) só perde os candidatos
  daquele pedaço — os outros pedaços seguem normalmente.

---

# Parte B — `pipeline_cortes.py`: depuração mecânica

Pega o `blocos_finais.json` da Parte A e, para cada bloco, gera um clipe pronto: pausas
longas encolhidas, recomeço de frase removido, silêncio nas bordas aparado, volume
normalizado, e uma abertura de ~2-3s no início (o pico do bloco, escolhido pelo JEV) antes
do corte principal começar. Só a abertura usa IA — o resto é sinal de áudio e regra
determinística (ver "Abertura", mais abaixo).

## Rodar

```
python fase1/pipeline_cortes.py <caminho>/blocos_finais.json
```

- Salva em `<pasta do blocos_finais.json>/cortes/` por padrão, ou em `--saida <pasta>`.
- Cada bloco vira `cortes/<id>.mp4` (o clipe pronto — já com a abertura, se gerada) mais
  `cortes/<id>/relatorio.json` (todo corte aplicado, com o motivo, e o trecho escolhido
  para a abertura) e `cortes/<id>/bruto.json` (a transcrição por palavra do Whisper, para
  auditoria). Os intermediários (`bruto.mp4`, `principal.mp4`, `abertura.mp4`) são apagados
  ao final de cada bloco — sem isso, cada um deixaria 3-4 cópias de dezenas de MB para trás.
- Um bloco que falhar não derruba os outros; o erro fica registrado em
  `cortes/relatorio_geral.json`.
- Parâmetros em `config_cortes.yaml` (não o `config.yaml` da Parte A).

## Por que roda Whisper de novo, se a Parte A já transcreveu?

A Parte A quase sempre usa o `.srt` do vídeo (rota mais rápida e barata), que só tem
granularidade de **segmento** (frase), não de palavra. Cortar recomeço de frase com
precisão — sem comer a palavra vizinha — exige saber o tempo exato de cada palavra. Rodar
Whisper aqui, nos ~6-8 blocos que sobraram da Parte A (não no vídeo inteiro), é barato: o
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
5. **Render do corte principal** (`renderiza.py`) — os cortes de silêncio e recomeço se
   mesclam num único plano; o complementar (o que sobra) é concatenado com `trim`/`atrim` +
   `concat`, com um fade curto (`crossfade_ms`) em cada junção interna para não soar um
   "clique", e `loudnorm` no áudio inteiro.
6. **Abertura** (`gancho.py` + JEV) — opcional, ver seção própria mais abaixo. Renderizada à
   parte e colada na frente do corte principal com o demuxer concat do FFmpeg.

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
`relatorio.json` — é o primeiro lugar a olhar se um clipe soar estranho.

## Limitações conhecidas

- **Recorte inicial usa `-c copy`** (sem reencode) — corta no keyframe mais próximo do
  timestamp pedido, não exatamente nele. Pode entrar meio segundo de sobra no começo/fim do
  recorte bruto; o trim de bordas (etapa 3) deveria absorver isso na maioria dos casos.
- **`loudnorm` de passada única**, não as duas passadas que dão a medição mais precisa —
  mais rápido, ligeiramente menos exato. Trocar por two-pass é uma melhoria futura barata
  se a normalização não ficar precisa o suficiente nos testes.
- **A heurística de recomeço ainda pode ter falso-positivo/negativo** em casos que o teste
  não cobriu. `recomeco.ativo: false` desliga essa etapa inteira sem afetar o resto.

## Abertura (o único trecho desta fase que usa IA)

Se `abertura.ativo: true` (padrão), depois de calcular os cortes de silêncio/recomeço:

1. `gancho.py` gera candidatos de 1,8-3,2s, ancorados em início de palavra e preferindo
   terminar em pontuação de frase, **sempre fora dos trechos já cortados** — um candidato
   que cruzasse um corte mostraria, na abertura, algo que depois "some" quando o corte
   principal começa.
2. O JEV (`jev_client.escolher_gancho`) escolhe, entre até `max_candidatos` opções, o trecho
   que mais impacta sozinho — recebe o `gancho`/`comentario` que o DeepSeek escreveu na
   segmentação (Parte A) como contexto do que se espera encontrar.
3. Esse trecho é renderizado à parte — em escala de cinza por padrão
   (`abertura.escala_de_cinza`, filtro `hue=s=0`), para diferenciar visualmente do corte
   principal, que segue colorido — com o mesmo `loudnorm`.
4. **Transição** (`transicao.py`, se `transicao.ativo: true`): sorteia um som da pasta
   `transicao.pasta_sons` (ex.: `C:\Users\joaor\Videos\CortaLegenda\transition`, sons de
   "whoosh"/câmera) e desacelera o FINAL da abertura em `fator_slow`x até durar exatamente o
   tempo desse som — a voz original nesse trecho é abafada (`volume_voz_no_slow`) e o efeito
   entra por cima (`volume_whoosh`). Existe porque as pausas longas já foram cortadas: sem
   isso, não sobra vídeo "neutro" para cobrir o tempo de um som de transição sem parecer um
   buraco — o vídeo estica visualmente em vez disso.
5. A abertura (já com a transição) é colada na frente do corte principal com o demuxer
   concat do FFmpeg (sem reencode: os dois já saíram do mesmo codec).

Sem `OPENROUTER_API_KEY` configurada, ou com `abertura.ativo: false`, o pipeline segue
normal e só não gera a abertura (nem a transição, que depende dela) — nenhuma das duas é
obrigatória para o resto funcionar.

Testado com vídeo real: de 20 candidatos, o JEV escolheu "Eduardo Bolsonaro é um imbecil
completo. O papel dele é justamente..." (a frase de efeito do bloco) em ~2s.
