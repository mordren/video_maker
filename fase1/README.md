# Fase 1: seleção de blocos

Recebe um vídeo longo (podcast, entrevista, live) e devolve `blocos_finais.json` com os
blocos qualificados e ranqueados. Todos os blocos têm timestamps exatos, que são a
entrada da Fase 2. Esta fase não corta o vídeo.

A segmentação é **semântica, não por tempo fixo**: o DeepSeek lê a transcrição inteira e
aponta ele mesmo onde cada bloco candidato começa e termina — por assunto, não por grade
de segundos. (A v1 usava janelas de tempo fixo com sobreposição; testado e descartado: uma
grade cega raramente coincide com o início/fim real de uma ideia. Ver "Por que mudamos" no
fim deste arquivo.)

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
| 3 | **Segmentação semântica** — DeepSeek lê a transcrição em pedaços de ~15 min (com folga de contexto na borda) e aponta os blocos candidatos por ID de segmento, com um "gancho" resumindo o porquê | `candidatos.json`, `blocos_candidatos.json` |
| 4 | **JEV qualifica**, numa chamada por bloco: `score` viral de 1 a 5, `noul` de ritmo, `noul` de precisa-ajustar e `choice` de onde afinar início/fim | `jev_qualificacao.json` |
| 5 | Deduplicação (sobreposição > 70% → fica o de maior score) | `blocos_jev.json` |
| 6 | LLM (DeepSeek): coerência, coesão, problemas e sugestão de corte — só nos blocos que sobraram da consolidação | `llm_revisao.json`, `blocos_llm.json` |
| 7–8 | Ranqueamento (viral 0,5 · ritmo 0,2 · LLM 0,3) | `blocos_finais.json` |

Os parâmetros ficam em `config.yaml`: tamanho do pedaço de segmentação, faixa de duração do
bloco, pesos e modelos.

## Por que mudamos (v1 → v2)

A v1 cortava o vídeo em janelas de tempo fixo (45s, depois 90s) com sobreposição, e usava o
JEV como filtro de "isso é coerente?" antes de gastar o LLM. Dois problemas apareceram
testando com vídeo real:

1. **Uma grade de tempo fixo corta às cegas.** Um gancho forte raramente começa exatamente
   num múltiplo de 90s do início do vídeo. A sobreposição ajudava pouco — ainda era uma
   grade rígida tentando capturar algo sem ritmo fixo.
2. **O filtro do JEV não tinha poder discriminativo real.** Pedir "começo e fim perfeitos"
   numa janela cortada às cegas reprovava quase tudo (média de 0,13 de coerência). Suavizar
   a pergunta para "tem conteúdo aproveitável" resolveu a rejeição, mas o preço foi o filtro
   aprovar quase tudo (44 de 45) — deixou de filtrar de verdade.
3. Isso também expôs uma ineficiência: a v1 fazia **3 chamadas separadas ao JEV por bloco**
   (coerência, viral, qualidade), quando a Decisions API responde várias perguntas em
   paralelo numa única chamada sem custo extra de latência.

A v2 inverte a ordem: quem entende semântica (DeepSeek) faz a segmentação — aponta os
próprios limites dos blocos, olhando a transcrição inteira de uma vez — e quem é
rápido/barato (JEV) qualifica os candidatos já bem formados, numa chamada só por bloco.
Testado no mesmo vídeo: 34 candidatos, todos dentro da faixa de duração, cada um com um
assunto reconhecível (STF, Lula, Carmen Lúcia, Trump...), pipeline inteiro em ~32s.

## Detalhes que vale saber

- **O DeepSeek não inventa timestamp.** A transcrição é mandada numerada por segmento
  (`[s0042] texto`); o modelo só aponta o ID de início/fim de cada bloco, nunca escreve o
  tempo — o tempo real vem sempre da transcrição. Isso evita o erro comum de LLM arredondar
  ou inventar um número decimal.
- **Segmentação é chunking por limite de contexto, não por conteúdo.** Os pedaços de ~15 min
  mandados ao DeepSeek existem só para caber no contexto do modelo, com margem de segundos
  de contexto extra nas bordas para não perder um bloco que atravessa a fronteira do pedaço.
  Um candidato só é aceito se pelo menos uma ponta (início ou fim) cai dentro do núcleo do
  pedaço — evita duplicar o mesmo bloco em dois pedaços vizinhos.
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
