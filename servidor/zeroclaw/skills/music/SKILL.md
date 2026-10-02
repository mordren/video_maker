---
name: music
description: Montar vídeos longos de música (playlists lo-fi/phonk com imagens ou clipe) no Music do Estúdio com o comando estudio music; escolher faixas, ajustar, prévia e renderizar.
version: 1.0.0
author: João Carlos
tags: [videos, music, playlist, lofi, estudio, api]
---

# Music: vídeos de playlist (Estúdio)

Use quando o João pedir um **vídeo de músicas**, uma playlist em vídeo (lo-fi,
phonk...), uma prévia, ou quiser trocar faixas e ajustes do Music.

## Como fazer

Use a ferramenta de shell com o comando `estudio`. **Não use curl.** A primeira
linha da saída é sempre `HTTP <código>`.

Rode o comando **exatamente como nos exemplos**, sozinho: sem `2>&1`, sem `| head`,
sem redirecionar nada e sem juntar dois comandos. Erros já saem na saída normal.

Nos argumentos **não use** `$`, `;`, `|`, `&`, `>`, `<` nem crase (o sistema
bloqueia o comando).

## Comandos

```sh
estudio music estado
estudio music faixas
estudio music usar todas
estudio music usar "Baile Phonk.mp3, Redboom.mp3, Iron Will.mp3"
estudio music ajustar segundos_imagem=15 ordem=aleatoria
estudio music ajustar texto.ligado=false chuva=true
estudio music previa
estudio music renderizar --nome "Phonk Drift Mix"
estudio music progresso
estudio music cancelar
estudio music saidas
```

- As músicas, imagens e clipes ficam nas pastas do servidor; pelo Telegram dá
  para **escolher e ordenar** as faixas que já estão lá, não enviar arquivos.
- `usar`: define as faixas do vídeo, na ordem dada (nome do arquivo ou título,
  separados por vírgula). `usar todas` volta a usar a pasta inteira.
- `ajustar chave=valor` (várias de uma vez). Chaves úteis:
  - `modo=slideshow` (imagens) ou `modo=clipe` (um vídeo curto em loop);
  - `ordem=nome` ou `ordem=aleatoria` (ordem das imagens);
  - `segundos_imagem=20` (3 a 600), `transicao_s=2`, `crossfade_s=3` (entre músicas);
  - `ken_burns=true|false` (zoom lento), `ajuste=cortar|preencher`;
  - `vinheta=true|false`, `grao=true|false`, `chuva=true|false`;
  - `texto.ligado=true|false` (nome da faixa na tela), `texto.posicao=inf_esq`
    (inf_esq, inf_centro, inf_dir, sup_esq, sup_dir, centro), `texto.tamanho=34`;
  - o nome do arquivo final vai no `renderizar --nome "Nome do vídeo"`.
  - valor com espaço vai entre aspas, a chave junto: `"texto.cor=#ffcc00"`.
- `previa`: renderiza só um trecho (rápido) para conferir; `renderizar` faz o
  vídeo inteiro (1280x720, pode levar de 10 a 40 minutos conforme a duração).

## Fluxo

1. `estudio music estado` para ver quantas faixas, a duração total e a config.
2. Ajuste o que o João pedir (`usar`, `ajustar`) e confirme o resultado em uma linha.
3. Se ele quiser conferir, `previa`; depois `renderizar`. Responda que começou
   e, quando ele perguntar, `progresso` (mostra o % e o tempo que falta).
4. Pronto: mande o link do vídeo (`saidas` lista os últimos). O link funciona
   na rede de casa. O Music **não publica**: o vídeo fica na pasta de saída.

## Respostas

- HTTP 200: deu certo.
- HTTP 409: já há uma renderização rodando; espere ou `cancelar` (só se o João pedir).
- HTTP 400: ajuste inválido ou faixa não encontrada (rode `faixas` para os nomes certos).
- HTTP 502 ou HTTP 0: o serviço de Histórias/Music ou o Estúdio está fora do ar.

## Regras

- Não cancele uma renderização sem o João pedir.
- Não mude ajustes que ele não pediu.
- Responda em português, curto, sem colar a saída inteira.
