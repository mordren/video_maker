---
name: historias
description: Criar vídeos de histórias narradas (terror e outros canais) no Estúdio de Histórias com o comando estudio historias; escrever, gerar imagens, narrar, montar e publicar.
version: 1.0.0
author: João Carlos
tags: [videos, historias, terror, shorts, narracao, estudio, api]
---

# Vídeos de histórias narradas (Estúdio)

Use quando o João pedir uma **história**, um vídeo narrado, um Short de terror
(Garras), ou quiser ver, aprovar, refazer ou publicar uma história.

## Como fazer

Use a ferramenta de shell com o comando `estudio`. **Não use curl.** A primeira
linha da saída é sempre `HTTP <código>`.

Rode o comando **exatamente como nos exemplos**, sozinho: sem `2>&1`, sem `| head`,
sem redirecionar nada e sem juntar dois comandos. Erros já saem na saída normal.

Nos textos **não use** `$`, `;`, `|`, `&`, `>`, `<` nem crase (o sistema
bloqueia o comando). Escreva "e" no lugar de "&". Coloque o assunto entre aspas.

## Comandos

```sh
estudio historias canais
estudio historias nova --canal garras-novo --assunto "o caseiro que acha uma porta nova no porão toda segunda"
estudio historias nova --canal garras-novo --assunto "..." --revisar
estudio historias lista
estudio historias ver ultima
estudio historias ver 12
estudio historias texto 12
estudio historias eventos 12
estudio historias aprovar-historia 12
estudio historias aprovar-imagens 12
estudio historias refazer-cena 12 3 --prompt "a woman holding a lantern in a dark cellar"
estudio historias refazer-historia 12
estudio historias refazer-imagens 12
estudio historias recomecar 12
estudio historias continuar 12
estudio historias enviar 12 --confirmado
estudio historias excluir 12 --confirmado
```

- **Canais** (`--canal`, pelo slug): `garras-novo` (terror atmosférico, o
  padrão para terror) e `garras-no-telhado`. Os dois publicam no canal Garras.
  `estudio historias canais` mostra a descrição de cada um.
- **Automático** (padrão): escreve, revisa no juiz, gera as imagens, narra e
  monta sozinho, em uns 5 a 10 minutos. Se a história reprovar no juiz, o
  Estúdio recomeça sozinho com outra abordagem.
- `--revisar`: para em dois pontos para o João aprovar (texto e imagens).
  Use só se ele pedir para revisar antes.
- `refazer-cena ID N --prompt "..."`: refaz a imagem da cena N; o prompt da
  imagem é em **inglês**. Depois, `aprovar-imagens` para montar o vídeo.

## Fluxo

1. Pegue o **assunto** (uma frase com o gancho da história) e o **canal**. Sem
   canal e sendo terror, use `garras-novo`. Sem assunto, pergunte.
2. `estudio historias nova ...` e responda com o número da história.
3. Quando o João perguntar, `estudio historias ver <id>`: mostra o estado, a
   ressalva do juiz e os links (vídeo, folha de contato com todas as imagens,
   narração). Os links funcionam na rede de casa.
4. Parou em "esperando aprovar a história": mostre o texto (`texto <id>`),
   resumido, e aprove só se o João mandar. Em "esperando aprovar as imagens":
   mande o link da folha de contato.
5. Pronto: mande o link do vídeo. **Publicar** (`enviar --confirmado`) põe na
   fila do Publicador, que **publica no YouTube/TikTok de verdade**.

## Respostas

- HTTP 200: deu certo. Resuma em uma linha.
- HTTP 404: história não existe (ou canal errado: use o slug do `canais`).
- HTTP 409: ação não cabe no estado atual (ex.: ainda processando). Rode `ver`.
- HTTP 400: dado inválido ou o Publicador recusou; diga o motivo.
- HTTP 502 ou HTTP 0: o serviço de Histórias ou o Estúdio está fora do ar.

## Regras

- **Nunca** use `--confirmado` (enviar, excluir) sem o João pedir com todas as
  letras naquela conversa. Na dúvida, pergunte antes.
- Uma história por vez é o normal; a fila processa uma de cada vez.
- `recomecar` apaga texto, imagens e vídeo da história: só com pedido do João.
- Responda em português, curto, sem colar a saída inteira.
