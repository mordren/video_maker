# Edição dos cortes — o que já decidimos e o que falta

Registro das ideias e decisões sobre a edição dos shorts (canais info, br_semfim e garras), juntando
as conversas de 26 a 29/09/2026. O objetivo é não repetir teste que já foi rejeitado e saber o que
ainda está aberto. Onde tem número, ele veio de teste ou de dado do YouTube, não de palpite.

Os estilos de legenda testados estão no branch `exp/fase-legenda` (pasta `fase_legenda/`,
com o README de cada rodada).

---

## 1. O que os dados do canal mostram (29/09)

Análise de 95 Shorts do info com o YouTube Analytics. Cada vídeo foi re-pontuado pelo mesmo JEV da
Fase 1. A tabela completa ficou num CSV temporário, fora do repositório.

- **O que decide as views é o % assistido** (correlação +0,59). O público do info assiste ~30–35 s,
  seja qual for a duração do vídeo.
- **Duração:** até 30 s, 5 de 5 passaram de 1.500 views; de 31 a 50 s, 7 de 17; acima de 70 s,
  **0 de 48**, todos parados em ~1.000 (o lote de teste dos Shorts). Desde 07/09 os cortes
  ficaram com ~90 s e nenhum vídeo passou de 1.500.
- **A maior queda da retenção é entre 5 e 10 s** (de 1,08 para 0,73 na mediana). Ela acontece com
  ou sem a abertura em preto e branco, então a abertura não é a causa.
- **Seguram mais** os cortes que abrem com pergunta de jornalista, confronto ou "Urgente".
  **Perdem mais** os que abrem em monólogo técnico ou abstrato. Isso vem de só 10 exemplos, é uma pista.
- **Nota do JEV:** não prevê views nem retenção. A nota "viral" prevê um pouco os inscritos
  ganhos (+0,37) e os compartilhamentos (+0,21). O LLM dá 0,67 em quase tudo.
- Vídeo longo tem mais like por view (quem fica gosta), mas o YouTube não o distribui.

**Já aplicado:** cortes de 30–50 s, começando na frase forte; pergunta de "abertura" no JEV;
duração dentro da nota final (commits `5d84afa` e `c23f65d`).
**Conferir em 1–2 semanas:** se a nota nova acompanha o % assistido (matriz na aba
Desempenho do Publicador).

---

## 2. Formato do vídeo (crop, transparente, fotos)

A regra é sua: o formato depende de valer a pena **ver o rosto**, não de ser monólogo ou conversa.

| Situação | Formato |
|---|---|
| 1 pessoa, ou poucas, com reação, emoção ou embate | **crop** que segue quem fala (LR-ASD) |
| muita gente, grade/mosaico de rostos pequenos, dado abstrato | **transparente** (16:9 com fundo desfocado) |
| o corte inteiro fala de uma pessoa/empresa/lugar específico | **fotos do assunto** em cima, vídeo embaixo |
| 2 pessoas no mesmo plano trocando a fala rápido | **tela dividida** — *ainda não existe* |

- Cada proposta já chega com o formato sugerido pelo JEV (🤖 no combobox), e você troca se quiser.
- **Crop LR-ASD:** "muito bem otimizado". **Não mexer** no jeito que ele é feito; os riscos
  conhecidos (falta de memória com 8 GB, quadro fantasma no fim) foram aceitos. Problema de memória se
  resolve no Estúdio. Detectar rostos em resolução menor ganharia ~4 s/min, mas ficou **suspenso** (pode
  perder rosto pequeno).
- **Fotos do assunto:** várias fotos trocando a cada ~5 s, no começo de uma palavra, com fade. Vêm
  do Wikimedia Commons, conferidas pelo rosto (SFace) e com nota de visão (Gemini Flash-Lite,
  ~US$ 0,001 por corte). O teto que você deu foi ~US$ 0,005 por imagem. A legenda sobe para o meio
  (entre a foto e o vídeo) e o banner desce, só nesse formato.
  - Ponto fraco: LUGAR traz foto aleatória; político local sem foto no Commons cai no crop.

## 3. Micro-edição (mudanças na tela)

Suas regras:
- Uma mudança visual a cada **~7 s no máximo**. **Não** mexer na tela a cada emenda de respiro.
- O **tipo** de mudança deve vir da **emoção** do trecho, não de um relógio.
- A emoção deve ser classificada pelo **JEV** (mais barato e melhor que o DeepSeek), juntando o
  que a voz mostra com o texto. O JEV só aceita texto. O `jev-router` aceita áudio, mas repassa
  para o Gemini, e isso foi rejeitado. Caminho: medir o áudio localmente e mandar o resultado como
  texto no `state` do JEV. **Não implementado.**
- Outra ideia sua: a LLM propõe as edições e o JEV confere se cada uma é coerente.

Tabela inicial de emoção → efeito (proposta de 26/09, ainda não validada por você):

| Emoção / momento | Efeito |
|---|---|
| indignação, ataque, frase forte | destaque na palavra (legenda/cartão) |
| explicação, dado, citação de algo concreto | imagem de cobertura |
| ironia, deboche | efeito sonoro, talvez congelar o quadro |
| clímax, frase de efeito | legenda de destaque grande |

**Testado e rejeitado:**
- **Zoom** (a cada 7 s, degrau seco e depois suave): "estranho", tremia.
- **Imagem em tela cheia no meio da fala:** "desconexa". O B-roll genérico (Pexels/Pixabay) cai no mesmo.

**Aprovado:** o **banco de imagens**, um cartão com foto/logo aparecendo na palavra dita
(Wikidata filtrado pelo tipo do verbete). Existe no piloto (`fase1/microedicao.py`), mas **não está no
Estúdio**.

## 4. Legenda

- **"new" (padrão desde 28/09):** o gancho com brilho (glow) e destaque amarelo de borda branca; o resto
  em karaokê com caixa amarela na palavra falada, frases curtas (até 3 palavras/18 letras), letra 64.
  "old" é o SRT amarelo de antes.
- Rejeitado: frase inteira na tela, letra grande demais (84), caixa invadindo a palavra do lado.
- Demonstração de estilos: 5 s por estilo, não 15 ("ficar vendo de novo e de novo não tem lógica").
- Os 10 estilos pesquisados e os "populares" (Hormozi, MrBeast etc.) estão no branch
  `exp/fase-legenda`.

## 5. Áudio e ritmo

- **Silêncio:** compacto (pausa vira 0,25 s), com um respiro de 0,5 s de vez em quando (só em
  pausa original ≥ 1,2 s, depois do gancho, no máximo 1 a cada 10 s).
- **Abertura:** 2 a 7 s do pico do corte, em preto e branco e câmera lenta, antes do corte principal.
  O JEV escolhe o trecho.
- **Música:** baixa quando alguém fala; `loudnorm` e crossfade nas emendas.
- Não vale para esse conteúdo: sincronizar com a batida da música, acelerar ou pôr câmera lenta
  no corte principal.

## 6. Fluxo de produção

Sua regra: **não gastar processamento com o que vai ser jogado fora.**
- A legenda do YouTube vai para o DeepSeek, que aponta os trechos; o JEV confere; a proposta sai como
  **corte bruto** (quase instantâneo). A Fase 2 (silêncio, abertura, Whisper) e o acabamento só rodam
  no que você mandar produzir.
- **A legenda do YouTube é melhor que a do Whisper** para escolher os trechos. Quando o YouTube
  bloqueia (erro 429), o Estúdio lê o painel "Transcrição" da página do vídeo (commit `a116564`).
- Canal escolhido por corte; o perfil visual (banner) segue o canal do corte.

---

## 7. O que está aberto (por ordem de retorno esperado)

1. **Validar a regra de 30–50 s** com os próximos vídeos (aba Desempenho: % assistido e views).
2. **Abertura dos primeiros 5–10 s:** onde está a maior queda. Priorizar começar em pergunta ou
   confronto; testar se a abertura em preto e branco ajuda ou atrapalha agora que os cortes são curtos.
3. **Cartão de imagem na palavra** (o banco de imagens aprovado) dentro do Estúdio.
4. **Quando a pessoa sai do quadro, trocar para transparente só naquele trecho.** O dado já existe
  (`alvos_por_quadro()` devolve `None` sem rosto); falta renderizar em pedaços.
5. **Tela dividida** para 2 pessoas trocando a fala rápido.
6. **Emoção da voz virando texto para o JEV** e a micro-edição guiada por emoção (limite de 7 s).
7. **Voz:** compressor e reforço de 3–5 kHz antes do `loudnorm` (precisa de teste A/B de ouvido).
8. **Sensação "capenga" depois de cortar os respiros:** afrouxar a pausa ou variar levemente o
  enquadramento na emenda (parente do zoom rejeitado — testar antes).
9. **Whisper pela API dá erro 400 em live longa** e cai no local (~1 h). Achar o trecho que o
  provedor recusa.

## 8. Não voltar nisso

- Zoom de efeito e imagem em tela cheia (rejeitados nos pilotos de 26/09).
- B-roll genérico de banco gratuito.
- Imagem gerada por IA de pessoa real; foto de jornal/Google (risco de direitos autorais).
- MCPs de FFmpeg: o pipeline já chama o ffmpeg direto.
- Mexer no crop LR-ASD.
- Cookies da conta do canal no yt-dlp (risco de bloqueio da conta).
