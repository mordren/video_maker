# Estúdio de Histórias em Vídeo

Transforma um assunto num vídeo narrado com imagens, seguindo o roteiro em
`Gerador de Histórias de Terror roteiro de programação.md`.

## Como rodar

```bash
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env      # e preencha as duas chaves
.venv\Scripts\python main.py
```

Abra http://localhost:8000. Para testar tudo sem gastar nada (respostas falsas, imagens de mentira, dados numa pasta separada):

```bash
.venv\Scripts\python main.py --simulacao --raiz %TEMP%\est_sim servidor --porta 8010
```

## O que acontece com cada vídeo

1. **Ganchos**: o DeepSeek escreve 5, o Jev dá nota a cada um. Se o melhor não passa do corte, gera mais 5 vendo os anteriores (até 3 rodadas).
2. **História**: escrita em JSON com o gancho como narração da cena 1.
3. **Laço**: checagens em Python (palavras, cenas, segundos por cena, limites de personagens/ambientes), de graça. Só depois vai para o Jev. O que falhou volta para reescrita parcial.
   O laço para quando tudo passa, quando o gasto chega a US$ 0,01, depois de 5 rodadas ou depois de 2 rodadas seguidas sem melhora. Nos três últimos casos fica a melhor versão, marcada "aprovada com ressalva". Se a pergunta de segurança falhar, a história fica REPROVADA e nunca segue sozinha.
4. **Humanizer-br**: uma passada só, sobre a versão aprovada, com o `SKILL.md` como prompt de sistema.
   Depois, um verificador em Python procura travessão e o vocabulário da seção 12 da skill; se achar, a história volta uma vez ao humanizer.
   O Jev confere de novo o gancho, a coerência e se cada cena ainda bate com a imagem. A cena que piorou volta ao texto anterior. Se a coerência cair, a passada inteira é descartada.
5. **Parada 1**: você revisa e edita o texto na tela e aprova.
6. **Imagens**: primeiro a ficha do personagem, depois a placa de cada ambiente recorrente, depois as cenas.
   Ficha e placa ficam na biblioteca do canal e são reaproveitadas de graça nos próximos vídeos. As referências vão ao Klein separadas por `|` (ele aceita até 10).
7. **Parada 2**: você confere a folha de contato, refaz as cenas ruins e aprova. Cada refação usa uma seed nova.
8. **Narração**: Edge TTS num arquivo único, com o tempo de cada palavra. Com isso cada imagem entra quando a frase dela começa, e as legendas saem sem Whisper.
9. **Montagem**: é feita no FFmpeg. Cada cena ganha o efeito da tabela de tensão do canal, com escurecimento de 0,25 s na troca e legenda queimada (karaokê opcional).
   A trilha fica com ducking: voz e música são normalizadas a -16 LUFS, e a música baixa enquanto o narrador fala e sobe quando só ela toca (intro e cauda configuráveis).

No modo **automático** (caixa na tela ou `"automatico": true` na API), as paradas 1 e 2 são puladas.

## Canais

Cada canal guarda uma vez só:
- a descrição;
- as regras de escrita;
- as perguntas do Jev (as universais entram sempre, e as do canal se somam a elas);
- o estilo visual;
- a voz e o idioma;
- a legenda;
- a trilha (arquivos e volumes);
- os efeitos por tensão;
- o orçamento.

O botão "Sugerir configuração com IA" pede ao DeepSeek uma proposta a partir do nome e da descrição, e você revisa antes de salvar. Um canal em outro idioma usa uma voz do Edge nesse idioma; o humanizer-br só roda em português.

## Gerador de Vídeo (aba própria)

Monta vídeos longos 1280×720 · 30 fps para canais de música lo-fi: uma playlist de `musicas/` (mp3 e wav) e um fundo
(slideshow das imagens de `imagens/` ou um clipe em loop), com o nome da faixa atual na tela. O vídeo e o `.txt` de
capítulos ("00:00 Nome") vão para `saida/`.

- Cada faixa é medida uma vez (loudness e duração exata, em cache) e normalizada para -14 LUFS; o crossfade entre faixas
  encurta o vídeo, e os capítulos e a troca de título usam a mesma conta.
- O slideshow renderiza um ciclo só (Ken Burns e crossfade), que emenda sem salto e se repete até o fim do áudio.
- Codifica na GPU NVIDIA (NVENC) quando o teste dela passa; senão, na CPU. A prévia de 10 s usa o mesmo caminho.
- As pastas de músicas, imagens, vídeos e saída podem ser escolhidas na tela (campo "Pasta" com o botão "Escolher…");
  o padrão é `musicas/`, `imagens/`, `videos/` e `saida/` ao lado do programa. A pasta de saída é criada se não existir.
- As últimas configurações (incluindo as pastas) e a lista ficam em `dados/gerador.json`; cache, miniaturas e temporários em `dados/gerador/`.

## Configurações (aba própria)

A aba **Configurações** reúne os parâmetros globais: chaves, modelos, formatos (inclusive quantos quadros/imagens cabem numa história), imagens, montagem, Music, Publicador e FFmpeg. Chaves e variáveis de ambiente são gravadas no `.env` (a variável `ESTUDIO_ENV` troca o caminho dele; a primeira gravação de cada execução deixa um `.env.bak`); o resto fica em `dados/configuracoes.json`. A maioria vale na hora; o que precisar de reinício é marcado na tela.

## API

```
POST /api/projetos              {"canal": "garras-no-telhado", "assunto": "...", "automatico": true}
GET  /api/projetos/{id}          status, história, notas, cenas, custos, links dos arquivos
GET  /api/projetos/{id}/eventos  registro
POST /api/projetos/{id}/aprovar-historia | aprovar-imagens | continuar | refazer-historia
POST /api/projetos/{id}/cenas/{n}/refazer   {"prompt_imagem": "opcional"}
```

Com `ESTUDIO_API_TOKEN` no `.env`, chamadas de fora deste computador precisam de `Authorization: Bearer <token>`.

## Linha de comando

```
python main.py novo --canal garras-no-telhado "caseiro ouve arranhões no telhado" [--automatico]
python main.py continuar 12
python main.py refazer 12 cena 7
python main.py efeito teste tremor projetos/.../cenas/03.jpg
```

## Pastas

- `estudio/`: código. Os efeitos ficam em `estudio/efeitos/`, um arquivo por efeito; um arquivo novo aparece sozinho na lista.
- `dados/estudio.db`: banco SQLite.
- `projetos/<data>_<id>_<assunto>/`: `historia_vN.json`, `avaliacoes.jsonl`, `estado.json`, `cenas/`, `folha_contato.jpg`, `narracao.mp3`, `legendas.ass`, `final.mp4`.
- `biblioteca/<canal>/personagens|ambientes/`: fichas e placas reaproveitáveis.
- `trilhas/<canal>/`: músicas enviadas pela tela do canal.

## Calibração

O arquivo `avaliacoes.jsonl` de cada projeto guarda todas as notas. Depois de 15 a 20 shorts, compare a nota do gancho com a taxa de quem continuou assistindo no YouTube Studio e ajuste os cortes na tela do canal.
