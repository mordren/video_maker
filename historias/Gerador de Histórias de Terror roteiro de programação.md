# Estúdio de Histórias em Vídeo: roteiro de programação

Sep 28, 2026 · @João

## Objetivo e orçamento

O programa transforma um tema em um vídeo pronto para qualquer tipo de história contada com imagens e narração. O primeiro uso é o terror do Garras no Telhado, em short 9:16 de cerca de 1 minuto por até US$ 0,10, com revisão humana antes de publicar. O teste manual de 28/09 ("Três da Manhã") custou cerca de US$ 0,085 e serve de linha de base.

O pipeline tem cinco etapas: escrever a história, avaliar e reescrever até ficar boa, criar as referências visuais, gerar cenas e narração, e montar o vídeo. Cada etapa grava seu resultado em disco, então dá para parar, revisar e retomar de onde parou.

&#91;embedded content: pipeline · 5 etapas, 1 laço, 2 revisões\]

O único laço automático é o da história; da ficha em diante, quem decide refazer é você.

| Etapa | Serviço | Custo alvo (US$) |
| --- | --- | --- |
| História + avaliação (laço) | OpenRouter (texto) + Jev | até 0,010 |
| Narração | OpenRouter (áudio) ou Edge TTS | 0,015 ou 0 |
| Ficha do personagem | Pollinations, flux.2-klein-4b | 0,005 (reaproveitável) |
| Placa de ambiente recorrente | Pollinations, flux.2-klein-4b | 0,005 cada |
| Cenas (até 12 imagens) | Pollinations, flux.2-klein-4b | 0,060 |
| Refações (reserva de 2) | Pollinations | 0,010 |
| Montagem | FFmpeg local | 0 |
| **Total** |  | **cerca de 0,10** |

O orçamento sobe conforme as visualizações. O programa lê o teto de um arquivo de configuração e para a produção se a soma real passar dele.

## Arquitetura modular

O programa separa três coisas: o que ele sabe fazer (módulos), o que vai contar (perfil) e como vai mostrar (formato e estilo). Trocar terror por lenda regional muda só o arquivo de perfil. Trocar short por vídeo longo muda só o arquivo de formato. Nenhum dos dois exige mexer em código.

&#91;embedded content: arquitetura · 3 entradas, 6 módulos\]

As três entradas são arquivos YAML; os módulos são código que não muda de um gênero para outro.

### Os módulos

Cada módulo tem uma interface só e não sabe nada de terror. O de imagem, por exemplo, recebe prompt, referências, largura, altura e seed, e devolve um arquivo. Trocar o Pollinations por outro serviço é escrever um novo provedor para essa mesma interface.

| Módulo | Faz | O que dá para trocar |
| --- | --- | --- |
| texto | escreve e reescreve a história | modelo do OpenRouter |
| juiz | avalia com as perguntas do perfil | Jev ou outro juiz |
| imagem | gera ficha, placa e cenas | provedor e modelo (hoje Pollinations e Klein) |
| voz | narração e tempo de cada palavra | OpenRouter, Edge TTS ou sua voz |
| efeitos | movimentos, transições e sobreposições | cada efeito é um arquivo novo |
| montagem | junta imagem, voz, legenda e trilha | FFmpeg |
| custos | registra cada gasto e aplica o teto | limites no config |

### Perfil: o que contar

O perfil guarda tudo o que é específico de um tipo de história. As perguntas do Jev ficam aqui porque mudam com o gênero: reviravolta e tensão crescente servem para terror, não para uma fábula ou uma curiosidade histórica.

```yaml
# perfis/terror.yaml
nome: terror
regras_escrita:
  - um personagem principal, no máximo dois ambientes
  - narração de causo, frases corridas, só a fala final curta
  - final com reviravolta
estrutura: [gancho, desenvolvimento, virada, susto_final]
juiz:
  gancho_minimo: 8
  perguntas: [...]        # a tabela da Etapa 2
estilo_padrao: nanquim    # aponta para estilos/nanquim.yaml
voz: grave_lenta
trilha: suspense
efeitos:
  permitidos: [zoom_in, zoom_out, pan, tremor, flash, cintilar_luz, vinheta_pulso]
  por_tensao: {1: zoom_out, 3: zoom_in, 5: tremor}
```

Outros perfis possíveis com o mesmo código: lendas do Paraná, histórias reais e curiosidades históricas, fábulas. Cada um traz as próprias regras, perguntas e estilo.

### Formato: como mostrar

|  | Short | Longo |
| --- | --- | --- |
| Proporção | 9:16, 1080×1920 | 16:9, 1920×1080 |
| Duração | cerca de 60 s | 8 a 15 min |
| Tempo por imagem | 4 a 7 s | 15 a 30 s |
| Movimento padrão | zoom de 1,00 a 1,20 | pan lento e parallax |
| Gancho | nos 3 primeiros segundos | nos primeiros 30 s, depois capítulos |
| Legenda | queimada, 2 a 4 palavras | opcional |
| Imagens | até 12 | 20 a 40 |

No longo, a imagem fica 15 a 30 segundos na tela, e zoom sozinho cansa. Por isso a cena é gerada um pouco maior que a tela, e o efeito de pan desliza a câmera por ela. As placas de ambiente valem mais ainda aqui, porque o mesmo lugar volta várias vezes ao longo do vídeo.

O orçamento do longo é outro. Uma estimativa para 10 minutos: 30 imagens a 0,005 dão US$ 0,15, e a narração paga no OpenRouter sairia perto de US$ 0,13 (0,0151 por 1:09 no teste). No longo, a narração deve ser Edge TTS ou sua voz, e o teto fica no arquivo de formato.

### Estilo: como parece

O estilo é um arquivo separado (`estilos/nanquim.yaml`, com o sufixo de prompt e a paleta), para que dois perfis possam usar o mesmo estilo e um perfil possa testar outro. Na linha de comando, `--estilo` sobrepõe o padrão do perfil.

### Pastas

```text
estudio/
  config.yaml            # tetos, provedores, modelos (chaves só em variáveis de ambiente)
  perfis/                # terror.yaml, lendas.yaml, historia_real.yaml ...
  formatos/              # short.yaml, longo.yaml
  estilos/               # nanquim.yaml, animacao_2d.yaml ...
  efeitos/               # zoom.py, pan.py, parallax.py, tremor.py ...
  biblioteca/
    personagens/anselmo/ # ficha.jpg + ficha.json
    ambientes/sitio/     # placa.jpg + placa.json
  projetos/2026-09-28_tres-da-manha/
    estado.json  historia_vN.json  avaliacoes.jsonl  cenas/  narracao.mp3  legendas.srt  final.mp4
  src/modulos/           # texto.py juiz.py imagem.py voz.py montagem.py custos.py
  main.py
```

```text
python main.py novo --perfil terror --formato short "caseiro ouve arranhões no telhado"
python main.py novo --perfil lendas --formato longo --estilo nanquim "a lenda do Lobisomem"
python main.py continuar 2026-09-28_tres-da-manha
python main.py refazer 2026-09-28_tres-da-manha cena 7
python main.py efeito teste pan cenas/03.jpg
```

As chaves ficam em variáveis de ambiente (`OPENROUTER_API_KEY`, `POLLINATIONS_API_KEY`), nunca nos arquivos de configuração nem no Git.

## Etapa 1: escrever a história (OpenRouter)

Um modelo de texto barato do OpenRouter recebe o tema e devolve a história já dividida em cenas, em JSON. Pedir o JSON com esquema fixo (`response_format` com `json_schema`, nos modelos que aceitam) evita ter de interpretar texto solto.

```json
{
  "titulo": "Três da Manhã",
  "gancho": "frase de abertura, lida nos 3 primeiros segundos",
  "personagens": [{"id": "anselmo", "descricao_fixa": "65 anos, magro, barba grisalha, camisa xadrez vermelha..."}],
  "ambientes": [{"id": "sitio", "descricao_fixa": "casa de madeira, telhado de zinco, eucaliptos", "recorrente": true}],
  "cenas": [
    {"n": 1, "narracao": "...", "prompt_imagem": "...", "personagem": "anselmo", "ambiente": "sitio", "tensao": 2}
  ]
}
```

Regras do perfil terror (em \`perfis/terror.yaml\`; outro perfil traz as suas), tiradas do que funcionou no teste:

- Um personagem principal e no máximo dois ambientes. Cada ambiente a mais custa uma placa de referência e aumenta o risco de inconsistência.
- Narração de causo, com frases corridas. Nada de sequências de frases curtas de efeito ("Arranhões. Lentos."), que soam como texto gerado. A única fala curta permitida é a do susto final.
- Entre 150 e 170 palavras para cerca de 60 segundos. O teste teve cerca de 190 palavras em 1:09, ou 2,75 palavras por segundo.
- Cada cena descreve algo desenhável e nenhuma passa de 7 segundos de fala. Uma frase longa demais vira duas cenas.
- Texto dentro da imagem só quando for essencial. O modelo erra letras com frequência, e o nome pode ser posto na edição.
- Final com reviravolta que muda o sentido de algo mostrado antes.

Contagem de palavras, duração estimada e número de cenas são calculados em Python, nunca pedidos ao modelo. Uma história fora dos limites volta direto para reescrita sem gastar com a avaliação.

Para medir o custo real, cada chamada ao OpenRouter pede a contabilidade de uso na requisição e soma o valor devolvido em `custos.py`. Confirmar o nome exato do campo na documentação do OpenRouter antes de programar.

## Etapa 2: avaliar com Jev e reescrever até ficar boa

A história só segue para as imagens quando passa em todas as notas de corte abaixo, ou quando o laço gasta US$ 0,01. O Jev (`typesafe/jev-1.13`, pelo Pollinations em `POST /alpha/decisions`) recebe a história inteira em `state` e um mapa de perguntas. Ele devolve, para cada pergunta, uma resposta com confiança e probabilidades.

### O gancho

O gancho é feito à parte, porque decide se a pessoa para de rolar a tela. O modelo de texto escreve 5 ganchos numa chamada só. O Jev escolhe o melhor com uma pergunta `choice`, e depois dá nota a ele com uma pergunta `score`. Se o melhor dos 5 não passar, gera outros 5 mostrando ao modelo os anteriores e as notas.

### Perguntas e notas de corte do perfil terror

| Pergunta | Tipo | Passa com |
| --- | --- | --- |
| Quem está rolando o feed para nos 3 primeiros segundos com esse gancho? | score 0–10 | 8 ou mais |
| O personagem mantém aparência, idade e comportamento do começo ao fim? | noul | 0,85 ou mais |
| A sequência de fatos é coerente, sem furos nem contradições? | noul | 0,85 ou mais |
| O final muda o sentido de algo mostrado antes? | score 0–10 | 7 ou mais |
| A tensão cresce de cena para cena? | score 0–10 | 7 ou mais |
| Dá para entender tudo ouvindo uma vez só, sem pronome ambíguo? | noul | 0,80 ou mais |
| Cada prompt de imagem mostra o que a narração daquela cena diz? | noul | 0,85 ou mais |
| A história evita violência explícita e qualquer menção a menores em perigo? | noul | 0,90 ou mais (trava) |
| A história é original, e não cópia de creepypasta conhecida? | score 0–10 | 6 ou mais |

Na nota de score, use o valor esperado das probabilidades e não só a opção mais provável. Um 8 com 40% de chance de ser 5 não é um 8 seguro. A documentação do Jev avisa que ele pode ficar confiante mesmo quando falta informação, por isso o `state` leva a história completa, com as descrições fixas de personagem e ambiente.

### Quanto é bom?

Ninguém sabe ainda, nem o Jev. As notas acima são um chute inicial razoável. O número certo vem do seu canal: o programa grava todas as notas em `avaliacoes.jsonl`, e depois de 15 a 20 shorts você cruza a nota do gancho com a taxa de quem assistiu em vez de pular, no YouTube Studio. A nota de corte vai para onde essa taxa começa a subir de verdade. Se nota alta e retenção não andarem juntas, o problema está na pergunta, e é ela que muda.

### O laço

1. Checagens em Python primeiro: número de palavras, duração por cena, JSON válido. Falhou, volta para reescrita sem chamar o Jev.
2. Jev avalia todas as perguntas de uma vez.
3. Passou em tudo, a história é aprovada.
4. Não passou, o modelo de texto recebe a versão atual, as perguntas que falharam e as notas, e reescreve só o que falhou. Reescrever do zero joga fora o que já estava bom.
5. Antes de cada chamada, `custos.py` soma o gasto real do laço com a estimativa da próxima. Se passar de US$ 0,01, para.

O laço também para depois de 5 rodadas, ou quando duas rodadas seguidas não melhoram a nota geral. Nesses casos fica a versão de maior nota, marcada como "aprovada com ressalva" no `estado.json`, e você decide se segue.

O preço do Jev é cobrado por token e não foi medido ainda. A primeira execução real deve registrar quanto custou uma avaliação completa, para calibrar quantas rodadas cabem em US$ 0,01.

## Etapa 2b: normalizar o texto com o humanizer-br

Todo texto que vai ao público passa pelo humanizer-br antes de sair: a narração, o gancho, o título e a descrição do YouTube. É um sétimo módulo, `revisao`, e o perfil diz se ele roda (`revisao: humanizer-br`). Um perfil técnico ou de referência pode desligar.

O humanizer-br é um conjunto de instruções, não um programa. O módulo carrega o `SKILL.md` a partir de um caminho no `config.yaml` (por exemplo `recursos/humanizer-br/SKILL.md`) e manda como prompt de sistema numa chamada ao OpenRouter, no modo embutido: a resposta é só o texto final. Quando você atualizar a skill, basta trocar o arquivo.

### Onde entra e por quê

As regras da skill atuam em três pontos, do mais barato ao mais caro:

1. **Na escrita.** As regras principais (sem "não X, mas Y", sem frase de efeito solta, sem travessão, sem tríade forçada, sem gerúndio conclusivo) já vão no prompt da Etapa 1. Prevenir sai mais barato que corrigir.
2. **Uma passada depois da aprovação.** O humanizer roda uma vez só, sobre a versão que o Jev aprovou. Rodar a cada volta do laço gastaria o dobro e reescreveria texto que ainda ia mudar.
3. **Checagem em Python, custo zero.** Depois da passada, um verificador procura travessão, hífen duplo e as palavras da lista de vocabulário da skill (robusto, crucial, tapeçaria, fomentar...). Se achar, manda de volta ao humanizer uma vez, apontando o trecho.

### O humanizer não pode estragar o que o Jev aprovou

Reescrever muda palavras, e palavras mudam sentido. Depois da passada, o Jev roda uma rechecagem curta com três perguntas: a nota do gancho, a coerência dos fatos e se cada cena ainda bate com o prompt de imagem dela. Se alguma cair abaixo da nota de corte, aquela cena volta para a versão anterior à passada, e o resto fica humanizado.

A narração é dividida por cena, então a passada devolve o mesmo número de cenas na mesma ordem. O Python confere a contagem; se não bater, descarta a passada inteira e mantém a versão aprovada.

Custo estimado: uma passada e uma rechecagem somam menos de US$ 0,002, fora do teto de US$ 0,01 do laço. O valor real entra no registro de custos como qualquer outra chamada.

## Etapa 3: consistência visual e imagens

A ordem é fixa: primeiro a ficha do personagem, depois a placa de cada ambiente recorrente, e só então as cenas, cada uma usando as referências que lhe cabem. Tudo no estilo nanquim do `config.yaml`, que pode ser trocado depois sem mexer no código.

1. **Ficha do personagem.** Se `biblioteca/personagens/<id>` já existe, reaproveita e não gasta nada. Se não existe, gera a ficha (frente, perfil, costas e três expressões, 3:2), sobe para `media.pollinations.ai` e guarda a URL e a `descricao_fixa` no `ficha.json`.
2. **Placa do ambiente.** Só para ambiente marcado como recorrente, ou seja, que aparece em duas cenas ou mais. É uma imagem do lugar vazio, sem personagem, com os elementos que precisam se repetir: a casa, o telhado de zinco, os eucaliptos. Vai para a biblioteca do mesmo jeito.
3. **Cenas.** Cada prompt é montado em Python: `prompt_imagem` + `descricao_fixa` do personagem + `descricao_fixa` do ambiente + sufixo do estilo. As referências entram conforme a cena: ficha quando o personagem aparece, placa quando o ambiente é recorrente. A seed é a base do projeto mais o número da cena, para que refazer uma cena não mude as outras.

### Ponto a testar antes de programar tudo

Não está confirmado que o flux.2-klein-4b aceita duas referências na mesma chamada, nem em que formato (o gerador HTML manda as URLs separadas por vírgula, sem teste). Três saídas, na ordem:

- O Klein aceita duas referências: segue como está.
- Não aceita: montar em Python uma colagem única (ficha à esquerda, placa à direita) e mandar como uma referência só. Custo zero.
- A colagem piora o resultado: testar o `qwen/qwen-image-2.1`, que aceita até 10 referências, e ver se o preço cabe no orçamento.

### Revisão antes de montar

Depois das cenas, o programa gera uma folha de contato (todas as cenas numa grade só) e para. Você olha, manda `refazer <projeto> cena 7` para o que ficou ruim, e só depois libera a montagem. O Jev julga texto, não imagem, então essa checagem continua sendo humana. As refações entram na conta do orçamento, com reserva de duas por vídeo.

## Etapas 4 e 5: narração e montagem

### Narração

A narração é gerada num arquivo único, com todas as cenas em sequência, porque a entonação sai mais natural do que em pedaços colados. O modelo de áudio do OpenRouter custou US$ 0,0151 por 1:09 no teste. O Edge TTS fica configurado como alternativa gratuita, e a sua própria voz como terceira opção, que também ajuda contra a regra do YouTube sobre conteúdo repetitivo e produzido em massa.

### Onde cada imagem entra

Cada imagem entra quando a frase dela começa, não em intervalos iguais. O método principal é o **Whisper local** (faster-whisper), que devolve o tempo de cada palavra. Com isso o programa sabe onde cada frase começa e já gera as legendas.

Se o Whisper não estiver disponível, vale o método usado no teste, que acertou todas as trocas:

1. Estimar o fim de cada frase pela proporção de caracteres acumulados.
2. Achar as pausas do áudio com o filtro `silencedetect` do FFmpeg (-35 dB, 0,35 s).
3. Encaixar cada estimativa na pausa mais próxima e cortar no meio dela.

### Montagem (FFmpeg)

- No short, 1080×1920, 30 quadros por segundo, H.264 com CRF 21 e taxa máxima de 3 Mb/s. O teste ficou com 18,5 MB.
- Cada imagem é ampliada para o dobro antes do zoom, para o movimento não tremer. O zoom alterna entre aproximar e afastar, de 1,00 a 1,10, e sobe para 1,15 a 1,20 nas cenas com `tensao` 4 ou 5.
- Escurecimento de 0,25 s na troca de cena.
- Nenhuma cena passa de 7 segundos. A cena final do teste ficou 11 segundos parada e foi o ponto mais fraco do vídeo; a Etapa 1 já divide frases longas em duas cenas.
- Legenda queimada, de 2 a 4 palavras por vez, letra grande, no terço central da tela. Short sem legenda perde retenção.
- Trilha de fundo da Biblioteca de Áudio do YouTube, cerca de 20 dB abaixo da voz.
- Sem grão de filme que muda a cada quadro. No teste ele levou o arquivo a 199 MB. Se quiser grão, use um grão fixo.

Melhoria para depois: parallax 2.5D com mapa de profundidade (Depth Anything, local e gratuito), que dá sensação de câmera se mexendo dentro da cena. No nanquim funciona bem, porque as sombras separam os planos.

## Biblioteca de efeitos

Cada efeito é um arquivo em `efeitos/`: uma função que recebe a imagem, a duração, o formato e alguns parâmetros, e devolve o trecho de filtro do FFmpeg. Criar um efeito novo não mexe em nada do resto, e ele passa a valer para qualquer perfil que o liste em `efeitos.permitidos`.

O JSON de cada cena ganha os campos `efeito` e `transicao`. Quem escolhe é a regra do perfil (por exemplo, tensão 5 recebe tremor). O modelo de texto pode sugerir, mas só entre os permitidos; um nome fora da lista é trocado pelo padrão do formato.

| Efeito | O que faz | Onde rende mais |
| --- | --- | --- |
| zoom\_in, zoom\_out | aproxima ou afasta devagar | short, qualquer perfil |
| pan | desliza a câmera por uma imagem maior que a tela | longo 16:9 |
| parallax | separa frente e fundo com mapa de profundidade e move a câmera | clímax, longo |
| tremor | tremida curta da imagem | susto |
| flash | 2 ou 3 quadros claros ou escuros | revelação |
| cintilar\_luz | varia o brilho como lanterna ou vela | cenas noturnas |
| vinheta\_pulso | bordas escurecem e clareiam no ritmo | tensão crescente |
| sobreposicao | chuva, poeira ou neblina de vídeo gratuito por cima | ambientação |
| corte\_seco, escurecer, deslizar | transições entre cenas | todas |

Para testar um efeito sem gerar vídeo inteiro: `python main.py efeito teste <efeito> <imagem>` produz um clipe de 5 segundos.

### Animação por IA fica como encaixe desligado

Vídeo gerado por IA custou 0,48 Pollen por clipe no teste, quase cinco vezes o orçamento de um short inteiro. O módulo de efeitos tem um encaixe `animacao_ia` que começa desligado. Quando o canal justificar, ele pode ser ligado para uma ou duas cenas por vídeo, como o susto final, com teto próprio no formato.

## Custos, lições, riscos e ordem de implementação

### Controle de custo

O `custos.py` grava cada chamada no `estado.json`: serviço, modelo, valor e se o valor é real ou estimado. O OpenRouter devolve o custo real na resposta. No Pollinations, use uma chave só para o programa (a `scary-mite` já aparece no extrato) com limite de gasto definido no painel. Assim, mesmo com erro no código, a conta nunca passa do teto.

No início de cada execução, o programa lê o catálogo de modelos e confere se o preço do Klein continua em 0,005. Se mudou, avisa antes de gerar.

### Lições dos testes de 28/09

| O que aconteceu | Custo | Regra no programa |
| --- | --- | --- |
| Um clipe de vídeo com amazon/nova-reel-v1 | 0,48 Pollen | Vídeo gerado por IA fica fora; o movimento é feito no FFmpeg |
| community/uncensored-image-v2 | 0,095 Pollen | Nenhum modelo da comunidade; eles rodam em servidor de terceiros |
| openai/gpt-image-2 e flux.1-kontext-pro | 0,044 e 0,03 Pollen | Fora da lista de modelos permitidos |
| flux.2-klein-4b com ficha de referência | 0,005 Pollen | Modelo padrão, consistência aprovada |
| 3 refações em 14 imagens | 0,015 Pollen | Reserva de 2 refações no orçamento |
| Grão de filme por quadro | arquivo de 199 MB | Sem grão variável; CRF 21 e 3 Mb/s |
| Última cena com 11 s | ritmo fraco | Máximo de 7 s por cena |

### Riscos

- **Política do YouTube** contra conteúdo repetitivo e produzido em massa. Revisão humana antes de publicar, roteiros originais e, se possível, sua voz.
- **Notas do Jev sem calibração.** Até cruzar com a retenção real, uma nota alta não garante um short bom.
- **Duas referências no Klein** não foram testadas (Etapa 3 tem o plano B).
- **Catálogo do Pollinations muda** com frequência: modelos novos, preços e nomes trocados.

### Ordem de implementação

1. `custos.py` e `clientes.py`: chamadas a OpenRouter, Pollinations e Jev, com registro de custo desde a primeira linha. Junto, o leitor de perfil, formato e estilo, com terror, short e nanquim como primeiros arquivos.
2. `historia.py` com as checagens em Python (palavras, duração, JSON).
3. `avaliacao.py` com o Jev. Rodar uma vez e medir quanto custa uma avaliação completa.
4. Teste de duas referências no Klein, antes de escrever o `visual.py`.
5. `visual.py` com biblioteca de personagens e ambientes e a folha de contato.
6. `narracao.py` e o alinhamento com Whisper.
7. `montagem.py`, partindo do comando FFmpeg que gerou o "Três da Manhã", com zoom e pan como primeiros efeitos; os outros entram um de cada vez.
8. `main.py` com os comandos `novo`, `continuar` e `refazer` e as paradas para revisão.
9. Refazer o "Três da Manhã" pelo programa e comparar custo e qualidade com o manual.
10. Depois de 15 a 20 shorts publicados, recalibrar as notas de corte com a retenção real. Só então abrir o formato longo e o segundo perfil.
