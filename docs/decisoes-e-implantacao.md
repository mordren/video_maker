# Decisões e implantação no servidor local

Resumo para a IA (ou pessoa) que vai atualizar o Estúdio no computador do João. Tudo o que está em
"Já implementado" está no `main` do GitHub (`mordren/gerador_video`). O que está em "Pendente" foi só
discutido e ainda precisa ser feito.

## 1. Decisões tomadas (e por quê)

| Decisão | Motivo |
|---|---|
| Só o DeepSeek 4.1 Flash escreve. O Opus foi testado e **removido**. | O Opus não melhorou os textos, custava 3x mais (US$ 0,013 a 0,028 por gancho) e no OpenRouter não aceita raciocínio desligado. |
| Canal de terror vale para **qualquer época e lugar** (cidade, medieval, rural...), não só lenda rural. | O canal é de terror em geral: fantasmas, aparições, assassinos, maldições. |
| Novo canal **Garras Novo** (`garras-novo`), baseado na skill "Terror Short PT-BR" do João. | Oito modos narrativos, regras de escrita e perguntas do Jev específicas. |
| Regras **sem exemplos concretos** (ex.: nada de "tocha e taverna"). | O modelo copia os exemplos em vez de entender a regra. |
| Coerência de época e lugar vem do **assunto**: rural usa termos rurais, medieval usa termos medievais. | A palavra "guarita" apareceu numa história medieval. |
| O **modo narrativo** é escolhido pelo DeepSeek junto com os ganchos, evitando o modo do vídeo anterior. | O rodízio fixo por número do projeto forçou "Relato encontrado" onde não cabia e gerou texto incoerente. |
| "Relato encontrado" = a narração inteira **é** o documento, na voz de quem o fez; ninguém narra de fora nem conta como foi achado. | A versão antiga gerou "foi achado um bilhete" e depois "acordei no chão". |
| O gancho escolhido pelo Jev **fica fixo**; o DeepSeek escreve só a **continuação** e o código junta os dois. | Pedir o gancho de volta fazia o modelo reescrevê-lo e a história saía com a abertura repetida. |
| **Não remendar: escrever de novo.** O laço escreve até 4 histórias completas do zero e fica a de maior nota. | Nos testes, reescrever mexia a nota em ±0,3 e as mesmas perguntas continuavam falhando. |
| Regras do **humanizer-br vão no prompt de escrita**; depois, só passada **dirigida** nos trechos que o verificador em Python aponta. | A passada inteira mudava fatos e derrubou a coerência para 0,57 num teste. |
| Revisão (parada 1) mostra o **texto corrido do narrador**, editável. Mudar a narração redivide as cenas. | Ver a história em caixinhas de cena atrapalhava a leitura. |
| Tela do canal em **abas**, com **Roteiro** em destaque e botão "Ver o prompt completo". | Configuração estava complexa demais; as regras do roteirista são o mais importante. |
| Botão **Enviar ao Publicador** na aba Vídeo. | O `publicador.py` existia mas não estava ligado a nada. |

## 2. Já implementado (no `main`)

- **Publicador**: `POST /api/projetos/{id}/publicador`, botão na aba Vídeo, campo "Canal no Publicador" na aba Custos e publicação do canal. Variável `PUBLICADOR_URL` no `.env`.
- **Canal Garras Novo**: semeado sozinho na primeira vez que o sistema abre (`estudio/canais.py`, `GARRAS_NOVO` e `MODOS_TERROR`). Não mexe nos canais que já existem.
- **Migrações automáticas** ao abrir:
  - coluna `projetos.modo`;
  - coluna `projetos.publicador_json`;
  - regras antigas do Garras Novo e o texto antigo do modo "Relato encontrado" trocados pelos novos, só se ainda forem idênticos aos da versão anterior.
- **Laço novo** (`historia.laco`):
  1. o DeepSeek escreve 5 ganchos (120 a 150 caracteres) e escolhe o modo;
  2. o Jev dá nota, com até 3 rodadas;
  3. o melhor gancho fica fixo;
  4. o DeepSeek escreve até `LACO_MAX_HISTORIAS` (padrão 4) histórias do zero, cada uma com um só ajuste de tamanho se precisar;
  5. o laço para na primeira que passa em tudo; senão fica a melhor (segurança > tamanho > nota geral).
- **Humanizer** (`estudio/revisao.py`): sem achados do verificador, nenhuma chamada. Com achados, até 2 voltas só nos trechos. O verificador pega:
  - travessão;
  - vocabulário da seção 12 do SKILL.md;
  - "não é X, é Y";
  - comparação decorativa ("como quem", "como se") no fim da frase ou repetida.
- **Tela**:
  - aba História com o texto corrido (`PUT /api/projetos/{id}/texto`, etapa `decupar` no pipeline);
  - canal em abas;
  - `POST /api/canais/previa-prompt` mostra o prompt exato.

## 3. Passos para atualizar o servidor local (Windows)

1. **Antes de tudo, ver se há mudanças locais no código.** O João disse que mexeu "lá no sistema". Pelo que foi dito, foi na tela (banco de dados), mas confira:
   ```
   git status
   git diff
   ```
   Se houver arquivos alterados, **não sobrescreva**: mostre ao João e combine (commit numa branch, ou `git stash`).
2. **Faça backup do banco** antes de abrir a versão nova (as migrações alteram o banco):
   ```
   copy dados\estudio.db dados\estudio_backup.db
   ```
3. Atualize o código e as dependências:
   ```
   git pull origin main
   .venv\Scripts\python -m pip install -r requirements.txt
   ```
4. Confira o `.env`:
   - `MODELO_GANCHO`, `PRECO_GANCHO_*` e `GANCHO_RACIOCINIO` não são mais usados; podem ser apagados;
   - `PUBLICADOR_URL` precisa apontar para o Publicador da rede;
   - `LACO_MAX_HISTORIAS=4` é opcional.
5. **Teste sem gastar nada** (pasta separada, respostas falsas):
   ```
   .venv\Scripts\python main.py --simulacao --raiz %TEMP%\est_sim novo --canal garras-novo "teste"
   .venv\Scripts\python main.py --simulacao --raiz %TEMP%\est_sim servidor --porta 8010
   ```
   Abra http://localhost:8010 e confira:
   - o canal Garras Novo abre na aba Roteiro com 11 regras e 8 modos;
   - "Ver o prompt completo" abre a janela com os prompts;
   - o projeto de teste mostra o texto corrido na aba História.
6. Suba o servidor de verdade (`.venv\Scripts\python main.py`) e **reinicie** se já estava rodando.
7. Primeiro teste real (custa menos de US$ 0,01 só no texto): crie um vídeo no Garras Novo **sem** automático e pare na revisão da história. Leia o registro: deve aparecer "Ganchos rodada N (modo): ..." e "História k/4 ...".
8. Na tela do canal Garras Novo, preencha "Canal no Publicador" antes de usar o botão de envio.

## 4. Pendente (decidir com o João antes de fazer)

1. **Calibrar os cortes do Jev no Garras Novo.** Em 16 histórias, quatro perguntas de sim/não nunca passaram. Por isso toda execução gasta as 4 histórias e termina "com ressalva".

   | Pergunta | Corte atual | Maior nota vista | Proposta |
   |---|---|---|---|
   | personagem_consistente | 0,85 | 0,80 | 0,70 |
   | modo_respeitado | 0,80 | 0,71 | 0,70 |
   | regra_sobrenatural | 0,80 | 0,77 | 0,70 |
   | coerencia | 0,85 | 0,84 | 0,75 |

   As histórias ruins ficaram entre 0,17 e 0,38, então o corte novo ainda as barra. As perguntas de nota (0 a 10) estão bem calibradas. Muda-se pela aba Juiz (Jev) do canal; `personagem_consistente` e `coerencia` são universais, e para mudar o corte basta criar no canal uma pergunta com a mesma chave.
2. **Dois padrões novos no `Humanizer br-v1/skills/humanizer-br/SKILL.md`:**
   - comparação decorativa pendurada no fim da frase;
   - fileira de frases curtas em narração ("Toc. Toc. Toc.", "Subi. Nada.").

   Nesse segundo caso, também ensinar o verificador (`revisao.verificar`) a apontar 3 ou mais frases seguidas de até 4 palavras. Atenção: a skill "humanizer-br" da conta Claude do João é outra cópia e não muda com isso.
3. **Registro de defeitos na revisão.** Na parada 1, o João marca um trecho e escolhe o tipo (época, coerência, clichê, frase de IA, imagem confusa). Guardar numa tabela nova.
4. **Banco de testes.** Um comando (ex.: `python main.py testes`) roda o Jev e o verificador sobre os trechos marcados e mostra quantos defeitos continuam sendo pegos. Deve ser rodado a cada mudança de regra ou de corte.
5. Depois de 15 a 20 vídeos publicados, cruzar os defeitos e as notas com a retenção do YouTube (a tela Desempenho já mede) antes de criar regras novas.

## 5. Cuidados

- **Não acumular regras.** O Flash já recebe 11 regras do canal e 11 padrões do humanizer, e ignora parte delas. Regra nova deve substituir uma velha. O que dá para detectar por padrão de texto vai para o verificador em Python, e o que é julgamento vira pergunta do Jev.
- **O gancho é fixo.** As reescritas não mexem nele, mas o humanizer pode corrigi-lo. Se a nota do Jev para o gancho cair mais de 2 pontos, volta o original.
- **Custos.** Uns US$ 0,005 por vídeo na parte de texto (DeepSeek + Jev), antes das imagens e da narração. O teto do laço (`LACO_TETO_USD`, padrão US$ 0,01) segura o gasto.
- **Frases confusas.** Nenhum modelo testado (DeepSeek, Jev, Opus) pegou frases como "a janela acendeu". A revisão do João na parada 1 continua sendo o filtro final.
