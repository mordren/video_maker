# Fase 1: seleção de blocos

Recebe um vídeo longo (podcast, entrevista, live) e devolve `blocos_finais.json` com os
blocos coerentes, pontuados e ranqueados. Todos os blocos têm timestamps exatos, que são a
entrada da Fase 2. Esta fase não corta o vídeo.

## Configurar

1. Dependência: `pip install -r fase1/requirements.txt` (só o PyYAML; FFmpeg precisa estar
   no PATH, e o Whisper só é usado quando o vídeo não tem `.srt`).

2. **Chave do OpenRouter (JEV):** abra `fase1/.env` e preencha `OPENROUTER_API_KEY` com a sua
   chave do OpenRouter (https://openrouter.ai/settings/keys). O `.env` está no `.gitignore`.

3. **Chave do DeepSeek (LLM contextual):** o script procura nos seguintes lugares, nesta ordem:
   - `DEEPSEEK_API_KEY` no `fase1/.env`
   - Variável de ambiente `DEEPSEEK_API_KEY`
   - Arquivo de config do Corta+Legenda: `~\AppData\Local\CortaLegenda\config.json` (Windows)
   - Se não achar em nenhum lugar, o script para e avisa.

   (Testamos antes um modelo gratuito do OpenRouter aqui, mas o rate limit ficou ruim
   demais — 4 min para 44 blocos, com vários timeouts. Voltamos para o DeepSeek.)

## Rodar

```
python fase1/pipeline.py "caminho/do/video.mp4"
```

- Cria `workspace_<data_hora>/` na pasta atual, com um JSON por etapa e o `pipeline.log`.
- `--ate-etapa 3` para depois das janelas, sem chamar nenhuma API (bom para conferir a
  transcrição).
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
| 3 | Janelas de 90 s com 15 s de sobreposição, encaixadas nas bordas dos segmentos | `janelas.json` |
| 4 | JEV `noul`: tem um assunto/ideia aproveitável? (descarta abaixo de 0,4) | `jev_etapa4_coerencia.json` |
| 5 | JEV `score`: potencial viral de 1 a 5 | `jev_etapa5_viral.json` |
| 6 | JEV: ritmo (`noul`), precisa de ajuste (`noul`) e em qual segmento começar/terminar (`choice`) | `jev_etapa6_qualidade.json` |
| 7 | Deduplicação (sobreposição > 70% → fica o de maior score) | `blocos_jev.json` |
| 8 | LLM: coerência, coesão, problemas e sugestão de corte | `llm_etapa8.json`, `blocos_llm.json` |
| 9–10 | Ranqueamento (viral 0,5 · coerência 0,2 · LLM 0,3) | `blocos_finais.json` |

Os parâmetros ficam em `config.yaml`: tamanho da janela, limiares, pesos e modelos.

## Detalhes que vale saber

- **A pergunta da etapa 4 é sobre conteúdo, não sobre os cortes.** A janela é recortada às
  cegas por tempo fixo, então quase nunca começa/termina no lugar exato de uma ideia — pedir
  "começo e fim perfeitos" aqui reprovava quase tudo (testado: média de 0,13 de coerência).
  A etapa 4 só filtra lixo óbvio (transição, chamada, sem assunto); os limites são ajustados
  depois, nas etapas 6 e 8.
- **O JEV não escreve timestamps.** Ele só responde perguntas tipadas. Na etapa 6, os
  primeiros e os últimos segmentos do bloco viram opções de `choice`, e o novo limite é o
  segmento que ele escolher.
- **O score viral sai fracionário** (ex.: 3,7). É a média dos níveis de 1 a 5 ponderada
  pelas probabilidades que o JEV devolve.
- **Os limites sempre caem na borda de um segmento.** Depois de cada ajuste (JEV ou LLM),
  texto e palavras são recalculados a partir da transcrição. Os ajustes ficam em `ajustes`.
- **O LLM só mexe dentro do que viu:** o bloco mais 20 s de contexto de cada lado.
  Nenhum ajuste deixa o bloco com menos de 20 s.
- **Os scores do JEV não são recalculados depois dos ajustes.** Eles valem para a janela
  que foi avaliada.
- **Falha de API não derruba o script.** O item fica com `"erro"` no JSON da etapa, o resto
  segue, e `--workspace` repete só os que falharam. Sem análise do LLM, o bloco recebe
  nota 0,5 nessa parte.
