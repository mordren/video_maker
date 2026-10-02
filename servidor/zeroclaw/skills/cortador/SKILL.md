---
name: cortador
description: Cortar vídeos do YouTube em Shorts (Cortador do Estúdio) com o comando estudio cortes; criar trabalho, ver propostas, produzir, revisar e publicar.
version: 1.0.0
author: João Carlos
tags: [videos, cortes, shorts, youtube, estudio, api]
---

# Cortador de vídeos (Estúdio)

Use quando o João pedir para **cortar um vídeo**, "tirar cortes" de um link do
YouTube, ver os cortes de um trabalho, produzir, trocar título/canal, descartar
ou publicar um corte. Também para "o que está rodando no estúdio" e "quanto
gastei".

## Como fazer

Use a ferramenta de shell com o comando `estudio`. Ele já fala com o Estúdio
(servidor 192.168.31.133, porta 8090). **Não use curl.** A primeira linha da
saída é sempre `HTTP <código>`.

Rode o comando **exatamente como nos exemplos**, sozinho: sem `2>&1`, sem `| head`,
sem redirecionar nada e sem juntar dois comandos. Erros já saem na saída normal.

Nos argumentos **não use** `$`, `;`, `|`, `&`, `>`, `<` nem crase (o sistema
bloqueia o comando). Link do YouTube com `&` (ex.: `&t=30s`, `&list=...`):
mande só até o id do vídeo, ou só o id de 11 letras. O comando limpa o resto.

## Comandos

```sh
estudio cortes canais
estudio cortes novo --url https://www.youtube.com/watch?v=ID --canal info
estudio cortes lista
estudio cortes ver ultimo
estudio cortes ver 20261002-020750-1f02
estudio cortes produzir 20261002-020750-1f02 bloco_006 --formato dinamico
estudio cortes titulo 20261002-020750-1f02 bloco_006 "Título novo do Short"
estudio cortes canal 20261002-020750-1f02 bloco_006 br_semfim
estudio cortes descartar 20261002-020750-1f02 bloco_004
estudio cortes voltar 20261002-020750-1f02 bloco_004
estudio cortes enviar 20261002-020750-1f02 bloco_006 --confirmado
estudio cortes cancelar 20261002-020750-1f02
estudio cortes repetir 20261002-020750-1f02
estudio status
estudio custos
```

- **Canais** (`--canal`): `info` (Informativo Nacional) e `br_semfim` (BR Sem Fim).
  Rode `estudio cortes canais` para ver nicho, duração ideal e formatos de cada um.
  Se o João não disser o canal, pergunte.
- **Formatos** (`--formato`): `dinamico` (crop 9:16 que segue quem fala, o padrão),
  `transparente` (vídeo inteiro com fundo desfocado) e `imagens` (fotos do
  assunto). Sem pedido do João, use o `formato sugerido` que aparece no `ver`.
- `--legenda new|old`: estilo da legenda; sem pedido, não passe (usa o do canal).
- `--visual PERFIL`: só se o João pedir outro visual.
- `ver` esconde os descartados; `--todos` mostra todos.

## Fluxo

1. **Novo trabalho**: `estudio cortes novo --url ... --canal ...`. A Fase 1
   (download, transcrição e escolha dos trechos) leva de 5 a 30 minutos.
   Responda com o id do trabalho e diga que avisa quando o João perguntar.
2. **Ver as propostas**: `estudio cortes ver <trabalho>` lista os cortes com id
   (ex.: `bloco_006`), duração, nota e título. Mostre ao João os melhores
   (nota maior, duração entre 30 e 50 s) em lista curta.
3. **Produzir**: só os cortes que o João escolher (ou, se ele disser "produz os
   melhores", os 3 de maior nota). Cada um leva alguns minutos.
4. **Revisar**: quando o `ver` mostrar `PRONTO para revisar`, mande ao João o
   link do vídeo (funciona na rede de casa) e o título.
5. **Publicar**: `estudio cortes enviar ... --confirmado` manda para a fila do
   Publicador, que **publica no YouTube/TikTok de verdade**.

## Respostas

- HTTP 200: deu certo. Resuma em uma ou duas linhas.
- HTTP 400: dado inválido (a mensagem diz o quê). Corrija ou pergunte.
- HTTP 404: trabalho ou corte não existe. Rode `estudio cortes lista` ou `ver`.
- HTTP 409: o corte já está na fila/produção, ou o servidor está com pouca RAM
  (aí só `transparente` e `imagens` funcionam). Explique ao João.
- HTTP 502: Publicador fora do ar. HTTP 0: Estúdio fora do ar.

## Regras

- **Nunca** use `--confirmado` (enviar, remover) sem o João pedir com todas as
  letras naquela conversa ("publica", "manda pro publicador", "pode postar").
  Na dúvida, pergunte: "Publico o corte X no canal Y?".
- Não produza todos os cortes de uma vez sem pedido: cada produção ocupa a GPU.
- Não invente ids: copie da saída do `lista`/`ver`.
- Responda em português, curto, sem colar a saída inteira.
