# Fase 1: seleção de blocos

Recebe um vídeo longo (podcast, entrevista, live) e devolve `blocos_finais.json` com os
blocos qualificados e ranqueados. Todos os blocos têm timestamps exatos, que são a
entrada da Fase 2. Esta fase não corta o vídeo.

A segmentação é **semântica, não por tempo fixo**: o DeepSeek lê a transcrição inteira,
numa chamada só, e aponta ele mesmo onde cada bloco candidato começa e termina — os
trechos mais fortes e polêmicos para virar corte, não um recorte por assunto qualquer.
O prompt é o mesmo que o app já usa em produção no painel "Cortes IA"
(`ai_srt._CUTS_PROMPT`), adaptado só para referenciar ID de segmento em vez de escrever
o tempo de cabeça. Ver "Por que mudamos" no fim deste arquivo.

## Configurar

1. Dependência: `pip install -r fase1/requirements.txt` (só o PyYAML; FFmpeg precisa estar
   no PATH, e o Whisper só é usado quando o vídeo não tem `.srt`).

2. **Chave do OpenRouter (JEV):** abra `fase1/.env` e preencha `OPENROUTER_API_KEY` com a sua
   chave do OpenRouter (https://openrouter.ai/settings/keys). O `.env` está no `.gitignore`.

3. **Chave do DeepSeek (segmentação + revisão fina):** o script procura nos seguintes
   lugares, nesta ordem:
   - `DEEPSEEK_API_KEY` no `fase1/.env`
   - Variável de ambiente `DEEPSEEK_API_KEY`
   - Arquivo de config do Corta+Legenda: `~\AppData\Local\CortaLegenda\config.json` (Windows)
   - Se não achar em nenhum lugar, o script para e avisa.

   (Testamos um modelo gratuito do OpenRouter para a revisão fina, mas o rate limit ficou
   ruim demais — 4 min para 44 blocos, com vários timeouts. Voltamos para o DeepSeek: o
   mesmo trabalho levou 17s.)

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

Consequência para a Fase 2: vindo de SRT, `palavras` sai vazio e
`"granularidade": "segmento"`; vindo do Whisper, `palavras` traz o tempo de cada palavra.

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
- **Os limites sempre caem na borda de um segmento.** Depois de cada ajuste (JEV ou LLM),
  texto e palavras são recalculados a partir da transcrição. Os ajustes ficam em `ajustes`.
- **O LLM da etapa 6 só mexe dentro do que viu:** o bloco mais 20s de contexto de cada lado.
  Nenhum ajuste deixa o bloco com menos que `bloco.duracao_minima`.
- **Falha de API não derruba o script.** O item fica com `"erro"` no JSON da etapa, o resto
  segue, e `--workspace` repete só os que falharam. Sem análise do LLM, o bloco recebe
  nota 0,5 nessa parte. Um pedaço de segmentação que falhar (etapa 3) só perde os candidatos
  daquele pedaço — os outros pedaços seguem normalmente.
