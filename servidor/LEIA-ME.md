# Publicador — fila de vídeos no servidor local

Serviço web que roda no Debian da rede (`192.168.31.130`) e publica os vídeos
no YouTube **um por vez, com intervalo aleatório** (por padrão entre 2h30 e
3h30) e **contando separado para cada canal**. Os arquivos ficam no HD do
servidor, e a lista é montada e editada pelo navegador.

O que ele faz:

- Cada vídeo já sai **publicado com a privacidade escolhida** assim que o
  envio e as verificações do YouTube terminam — sem agendamento (o envio
  por navegador já espera essas verificações antes de publicar).
- Lista de vídeos na ordem em que devem ser publicados, com **adicionar,
  excluir, reordenar e trocar o canal** de cada um.
- **Vários canais**: cada vídeo escolhe o seu, e **cada canal tem a sua própria
  configuração completa** — privacidade, faixa de espera, sorteio e
  descrição padrão (a mesma ideia da aba do programa no Windows) — escolhida
  por um seletor de canal, e **cada um corre no seu próprio relógio**, então
  dois canais não dividem a mesma espera.
- **Publicar também no TikTok**: um canal pode ter uma conta do TikTok
  vinculada (na própria tela de configuração dele). Nesse caso, todo vídeo do
  canal sai **na mesma leva** para o YouTube e para essa conta do TikTok —
  mesmo arquivo, mesma vez da fila. O TikTok sai logo **depois** que o upload
  do YouTube termina, não junto: os dois sobem o mesmo vídeo pela mesma
  internet, e em paralelo o YouTube sufoca o TikTok a ponto de a página dele
  nem abrir.
- Depois de cada publicação, sorteia um tempo dentro da faixa configurada
  *daquele canal* e só então manda o próximo. Opcionalmente sorteia também
  **qual** vídeo daquele canal sai.
- Título de cada vídeo: o nome do arquivo (editável na lista).
- Os arquivos são **copiados para o HD do servidor** pelo navegador, e ficam lá
  até você mandar apagar.

---

## 1. Copiar os arquivos para o servidor

No PowerShell do Windows, dentro da pasta do projeto:

```bash
scp -r servidor youtube_browser_upload.py tiktok_upload.py SEU_USUARIO@192.168.31.130:~/publicador-instalacao/
```

(Se a pasta não existir, crie antes: `ssh SEU_USUARIO@192.168.31.130 "mkdir -p ~/publicador-instalacao"`.)

## 2. Instalar

Entre no servidor e vire root (este Debian não tem `sudo` para o usuário comum):

```bash
ssh SEU_USUARIO@192.168.31.130
```

```bash
su -
```

```bash
bash ~SEU_USUARIO/publicador-instalacao/servidor/instalar.sh
```

Num Debian com `sudo`, a alternativa é `cd ~/publicador-instalacao/servidor && sudo bash instalar.sh`.

O instalador cria o usuário `publicador`, instala o Python do serviço em
`/opt/publicador`, guarda os dados em `/srv/publicador` e liga o serviço no
systemd (sobe junto com a máquina). Rodar de novo depois **atualiza** o
programa sem mexer na fila nem nos vídeos.

Se o servidor tiver firewall ligado, libere a porta:

```bash
sudo ufw allow 8080/tcp
```

## 3. Conectar os canais

O YouTube não tem login programático aqui — nem no servidor, nem no programa
do Windows. O acesso é pelos **cookies de uma sessão já logada**, exportados
do navegador, do mesmo jeito que o TikTok logo abaixo:

1. Faça login em `studio.youtube.com` num navegador normal, no canal certo
   (não precisa ser no servidor).
2. Exporte os cookies com uma extensão do tipo "Get cookies.txt" — **só do
   site youtube.com/studio.youtube.com**, não o navegador inteiro (senão vai
   cookie de outras contas Google suas junto).
3. Renomeie o arquivo para `youtube_browser_cookies_<canal>.txt` (o nome
   escolhido aqui é o que identifica esse canal em toda a tela, ex.:
   `youtube_browser_cookies_br.txt`, `youtube_browser_cookies_info.txt`).
4. Abra `http://192.168.31.130:8080`, na seção **Canais** escolha esse
   arquivo e clique em *Enviar credenciais*. O canal aparece como
   **conectado**.

Cookies expiram de tempos em tempos (o Google renova alguns sozinho por
segurança enquanto o navegador do envio está aberto — o serviço já regrava o
arquivo com os mais recentes a cada envio bem-sucedido, o que ajuda bastante).
Se mesmo assim pedir login de novo, reexporte e mande o arquivo atualizado da
mesma forma, mesmo nome.

### Conectar uma conta do TikTok (opcional)

O TikTok não tem token nem app registrado — o acesso é pelos **cookies de uma
sessão já logada**, exportados do navegador:

1. Faça login em tiktok.com num navegador normal (não precisa ser no servidor).
2. Exporte os cookies com uma extensão do tipo "Get cookies.txt" — **só do site
   tiktok.com**, não o navegador inteiro (senão vai cookie de outras contas
   suas, tipo Google/e-mail, junto).
3. Renomeie o arquivo para `tiktok_cookies_<apelido>.txt` (o apelido é livre,
   ex.: `tiktok_cookies_info.txt`) e mande na seção **Canais**, junto dos
   arquivos do YouTube.
4. Na **Configuração dos envios** do canal do YouTube correspondente, escolha
   essa conta em *Publicar também no TikTok*.

Cookies expiram de tempos em tempos (sessão encerrada, troca de senha) — quando
isso acontecer, os envios ao TikTok começam a falhar (fica no histórico) e
basta reexportar e mandar o arquivo de novo, mesmo apelido.

## 4. Avisos por e-mail (opcional)

Sem tela ligada, ninguém vê o histórico da página no dia a dia — por isso o
Publicador pode mandar um e-mail nestes casos:

- Um vídeo (YouTube ou TikTok) **falhou de vez**, depois de esgotar as
  tentativas automáticas, e está esperando você clicar em *Tentar de novo*.
- Uma conta do TikTok ficou **sem cookies importados**.
- O serviço **acabou de subir** — sinal indireto de que ele (ou a máquina)
  tinha caído: não existe como avisar *durante* uma queda de verdade, porque
  nada roda numa máquina desligada. Isso também dispara depois de cada
  atualização (`instalar.sh` reinicia o serviço no fim), então é normal
  receber um e-mail desses toda vez que você mexer no servidor.

Para ligar, crie (ou edite) `/srv/publicador/publicador.env` no servidor —
esse arquivo **não** é tocado pela instalação, então sobrevive a atualizações:

```bash
cat > /srv/publicador/publicador.env <<'EOF'
PUBLICADOR_EMAIL_PARA=seu-email@gmail.com
PUBLICADOR_EMAIL_SMTP_HOST=smtp.gmail.com
PUBLICADOR_EMAIL_SMTP_PORT=587
PUBLICADOR_EMAIL_SMTP_USER=conta-que-envia@gmail.com
PUBLICADOR_EMAIL_SMTP_SENHA=xxxx xxxx xxxx xxxx
EOF
chmod 600 /srv/publicador/publicador.env
systemctl restart publicador
```

`PUBLICADOR_EMAIL_SMTP_SENHA` no Gmail **não é a senha normal da conta** — é
uma "senha de app" (Conta Google → Segurança → Verificação em duas etapas →
Senhas de app), porque o Gmail recusa login de SMTP comum com 2FA ligado.
Pode ser uma conta Gmail só para isso, sem relação com os canais do YouTube.
Sem `PUBLICADOR_EMAIL_PARA` (ou sem o arquivo), o serviço sobe normalmente e
só o histórico da página registra os erros — nada quebra.

## 5. Usar

1. **Adicionar vídeos**: escolha os arquivos, escolha o canal e clique em
   *Subir para o servidor*. Eles entram no fim da lista.
2. Ajuste título, canal e ordem na tabela.
3. Em **Configuração dos envios**, escolha o **canal** no seletor do topo e
   ajuste a privacidade, a faixa de espera em minutos, quantos minutos até o
   vídeo ficar público, o sorteio da ordem, a descrição padrão e, se quiser,
   a conta do TikTok para onde esse canal também publica (com a legenda
   padrão dela) — tudo isso é só daquele canal; para configurar outro, troque
   o seletor (se houver alteração não salva, ele avisa antes de trocar).
4. Clique em **▶ Ligar fila**. O primeiro vídeo de cada canal sai na hora; a
   partir daí cada canal espera o intervalo sorteado depois do envio anterior
   *dele*. No alto da página aparece uma linha por canal com o tempo que falta
   e um **⏳** para sortear um horário novo só daquele canal.

Detalhes que importam no dia a dia:

- Sem agendamento: o vídeo já sai com a privacidade escolhida assim que o
  envio termina — não fica um tempo como Privado esperando o YouTube abrir.
- As ~3 horas contam a partir do fim de cada upload *daquele canal*, então a
  distância entre uma publicação e a seguinte do mesmo canal continua a mesma.
- Os envios nunca acontecem em paralelo: se dois canais vencerem a espera ao
  mesmo tempo, um sai logo depois do outro.
- **Parar fila** não cancela um envio que já começou, mas nada novo sai depois dele.
- **Enviar já** manda um vídeo específico fora da vez (e a contagem das 3h
  reinicia a partir dele).
- O **⏳** de cada canal recalcula o próximo envio *daquele* canal a partir de
  agora. Um canal que nunca enviou está livre e manda assim que a fila liga.
- **Vídeo recusado pelo YouTube volta sozinho para a fila**, no fim da lista,
  com o motivo à mostra e a marca *Repetindo*. As novas chances vêm em 30, 60,
  90… minutos (até 5 tentativas) — tempo suficiente para um problema passageiro
  da tela do Studio (ou dos cookies) se resolver. Enquanto isso os outros
  vídeos seguem saindo normalmente. Só depois das 5 tentativas ele vira
  *Falhou* e espera um clique em *Tentar de novo*.
- **Queda de internet no servidor** é tratada à parte, em dois níveis. Se cair
  *durante* o upload, ele é retomado de onde parou (pedaços de 1 MB, até 30
  quedas por vídeo) — não recomeça o arquivo. Se cair *antes* de começar (DNS,
  login), o vídeo continua *Na fila* e o envio é refeito sozinho, esperando 5,
  10, 15… até 30 minutos entre as tentativas; só depois de ~3h insistindo é que
  vira *Falhou*. No histórico essas tentativas aparecem com 🌐. Se acontecer
  direto, o problema é o link do Debian — confira com
  `getent hosts oauth2.googleapis.com` e `ping -c4 1.1.1.1`.
- Vídeo já publicado continua na lista (com o link) e o arquivo continua no HD.
  **🧹 Limpar publicados** tira da lista e apaga os arquivos.
- Um item cujo canal não está conectado fica esperando, sem atrapalhar os outros.
- **TikTok recusado** (cookies expirados, tela de upload mudou) não afeta o
  YouTube — fica só registrado no histórico, com ⚠️, e não tenta de novo
  sozinho: corrija (geralmente reimportando os cookies) e o próximo vídeo do
  canal já sai certo.
- Quando o TikTok falha, o servidor guarda **um retrato da tela no instante do
  erro**, que abre em `http://192.168.31.130:8080/tiktok-falha.png`. É o jeito
  mais rápido de saber o que houve de verdade: tela de login (cookies
  venceram), captcha, ou uma tela que mudou de lugar. A biblioteca sozinha só
  diria "falhou", sem dizer por quê.
- O botão **🧪 Testar TikTok** manda um vídeo que já está no servidor só para o
  TikTok, sem passar pelo YouTube — serve para testar a conta do TikTok sem
  esperar (nem arriscar) um envio de verdade ao YouTube.

## Onde fica cada coisa no servidor

```
/opt/publicador/            programa (substituído a cada instalação)
/srv/publicador/videos/     os vídeos
/srv/publicador/fila.json   a lista e o próximo horário de cada canal
/srv/publicador/config.json privacidade, intervalo, sorteio, descrição e TikTok de cada canal
/srv/publicador/publicador.log  histórico dos envios
/srv/publicador/youtube_browser_cookies_<canal>.txt   credenciais dos canais
/srv/publicador/tiktok_cookies_<conta>.txt             cookies das contas do TikTok
```

## Ajustes de rede que este servidor precisou

Duas configurações **fora do programa**, feitas na máquina, sem as quais os
envios ao YouTube e ao TikTok (os dois controlam um navegador de verdade) não
funcionam. Elas não vêm no `instalar.sh` de propósito: mexem na rede do
servidor inteiro, não só neste serviço. Se a máquina for reinstalada, refaça
as duas.

**1. IPv6 desligado.** A rede devolvia endereços IPv6 no DNS, mas a conexão
IPv6 não funcionava de verdade. O sintoma não parecia rede: o navegador dizia
"sem internet" (`ERR_NETWORK_CHANGED`, `ERR_NAME_NOT_RESOLVED`) enquanto o
`getent` resolvia os mesmos domínios normalmente, e tudo funcionava de forma
intermitente. Também era o motivo de o download do Chromium falhar na
instalação.

```bash
cat /etc/sysctl.d/99-sem-ipv6.conf     # deve ter disable_ipv6 = 1 nas duas linhas
```

**2. DNS fixo e travado.** O `/etc/resolv.conf` aponta para 1.1.1.1 e 8.8.8.8 e
está marcado como **imutável** (`chattr +i`), para nenhum serviço de rede
sobrescrever. Se algum dia precisar mudar o DNS, destrave antes, senão a
edição falha sem explicar por quê:

```bash
chattr -i /etc/resolv.conf     # destrava para editar
chattr +i /etc/resolv.conf     # trava de novo
```

Para conferir rapidamente se a rede está sadia para o YouTube e o TikTok:

```bash
curl -4 -sS -o /dev/null -w '%{http_code}\n' --max-time 20 https://studio.youtube.com/
curl -4 -sS -o /dev/null -w '%{http_code}\n' --max-time 20 https://www.tiktok.com/
```

## Manutenção

```bash
systemctl status publicador        # está rodando?
journalctl -u publicador -f        # erros ao vivo
sudo systemctl restart publicador  # reiniciar
du -sh /srv/publicador/videos      # espaço usado pelos vídeos
```

O histórico dos envios também aparece no fim da página, e o `fila.json` é um
arquivo de texto comum — dá para conferir ou corrigir na mão com o serviço
parado.

## Limites conhecidos

- O envio ao YouTube controla a tela real do Studio (Playwright) em vez de
  falar com a API oficial — trocado de propósito: a API tem cota apertada
  (~6 vídeos/dia por projeto do Google Cloud) e, pela experiência medida
  neste projeto, os vídeos enviados por ela quase não eram entregues (a
  maioria ficava em 3-4 views). Sem API não há cota diária, mas é mais
  frágil que ela: contraria os Termos de Serviço do YouTube e quebra se a
  tela do Studio mudar — quando isso acontecer, o envio começa a falhar
  (fica no histórico com ⚠️) até o código ser atualizado. Cada falha salva
  um retrato da tela do instante do erro
  (`/srv/publicador/youtube_browser_falha.png` e `.html`), do mesmo jeito
  que o TikTok logo abaixo.
- O envio ao TikTok usa uma biblioteca não-oficial (`tiktok-uploader`), que
  controla um navegador de verdade em vez de falar com uma API — não existe
  API de publicação acessível sem aprovação de app. É mais frágil que o
  YouTube: se o TikTok mudar a tela de upload ou passar a bloquear a
  automação com mais rigor, o envio para de funcionar até a biblioteca (ou
  este código) ser atualizado. Roda num display virtual (Xvfb) porque o
  servidor não tem tela.
- A página não tem senha: quem alcança `192.168.31.130:8080` na rede local
  controla a fila. Não exponha essa porta para a internet.
- Se o navegador perder o servidor (Wi-Fi oscilando, serviço parado), aparece
  uma tarja vermelha no alto avisando — os botões não funcionam nesse estado, e
  a página volta sozinha quando a conexão voltar.
