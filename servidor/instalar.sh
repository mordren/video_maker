#!/usr/bin/env bash
#
# Instala o Publicador num Debian, como serviço que sobe junto com a máquina.
#
# Rode como root, dentro da pasta "servidor" já copiada para o Debian:
#
#     su -                        (ou sudo -i, se este Debian tiver sudo)
#     bash instalar.sh
#
# Pode rodar de novo a qualquer momento para atualizar o programa: só o que
# está em /opt é substituído — a fila, os vídeos e as credenciais ficam em
# /srv/publicador e não são tocados.

set -euo pipefail

# Chamado por "su -c", o PATH herdado é o do usuário comum e não tem os
# diretórios de administração — sem isso, o useradd aqui embaixo "não existe".
export PATH="$PATH:/usr/sbin:/sbin"

APP_DIR=${APP_DIR:-/opt/publicador}
DATA_DIR=${DATA_DIR:-/srv/publicador}
PORTA=${PORTA:-8080}
USUARIO=${USUARIO:-publicador}
FUSO=${FUSO:-America/Sao_Paulo}
SERVICO=publicador

ORIGEM="$(cd "$(dirname "$0")" && pwd)"

if [ "$(id -u)" -ne 0 ]; then
  echo "Precisa ser root. Vire root com 'su -' e rode de novo:" >&2
  echo "  bash $0" >&2
  echo "(ou 'sudo bash instalar.sh', se este Debian tiver sudo)" >&2
  exit 1
fi

# youtube_browser_upload.py e tiktok_upload.py são os mesmos módulos usados
# pelo programa do Windows; podem estar aqui ao lado ou na pasta de cima (a
# raiz do projeto).
if [ -f "$ORIGEM/youtube_browser_upload.py" ]; then
  YT="$ORIGEM/youtube_browser_upload.py"
elif [ -f "$ORIGEM/../youtube_browser_upload.py" ]; then
  YT="$ORIGEM/../youtube_browser_upload.py"
else
  echo "Não achei o youtube_browser_upload.py — copie ele junto (mesma pasta ou a de cima)." >&2
  exit 1
fi
if [ -f "$ORIGEM/tiktok_upload.py" ]; then
  TT="$ORIGEM/tiktok_upload.py"
elif [ -f "$ORIGEM/../tiktok_upload.py" ]; then
  TT="$ORIGEM/../tiktok_upload.py"
else
  echo "Não achei o tiktok_upload.py — copie ele junto (mesma pasta ou a de cima)." >&2
  exit 1
fi

echo "==> Instalando dependências do sistema"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# xvfb: o envio ao YouTube e ao TikTok abrem um navegador de verdade
# (Playwright), e este Debian não tem tela — o xvfb-run dá a eles um display
# virtual para rodar.
apt-get install -y -qq python3 python3-venv xvfb

echo "==> Criando usuário e pastas"
if ! id -u "$USUARIO" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$USUARIO"
fi
mkdir -p "$APP_DIR" "$DATA_DIR/videos" "$DATA_DIR/tmp"
chown -R "$USUARIO:$USUARIO" "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "==> Preparando o Python do serviço (venv)"
if [ ! -x "$APP_DIR/venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/venv"
fi
# Atualizar o pip é só higiene: se a internet estiver ruim, segue sem isso em
# vez de derrubar uma atualização que talvez nem precise baixar nada.
if ! "$APP_DIR/venv/bin/pip" install --quiet --upgrade pip; then
  echo "    (sem internet para atualizar o pip; seguindo com a versão atual)"
fi
# As bibliotecas, essas sim, são obrigatórias — mas com mais paciência num
# link instável. Se já estiverem instaladas, o pip nem vai à rede.
if ! "$APP_DIR/venv/bin/pip" install --quiet --retries 10 --timeout 30 \
      -r "$ORIGEM/requirements.txt"; then
  echo "Não deu para instalar as bibliotecas do Python (internet?)." >&2
  echo "O serviço continua rodando a versão anterior. Tente de novo:" >&2
  echo "  bash $0" >&2
  exit 1
fi

echo "==> Instalando o Chromium do Playwright (envio ao YouTube e ao TikTok)"
# PLAYWRIGHT_BROWSERS_PATH=0 guarda o navegador dentro do próprio venv (em vez
# do cache do usuário) — o serviço roda com ProtectHome=yes, que esconde o
# home de quem o systemd usaria por padrão.
if ! PLAYWRIGHT_BROWSERS_PATH=0 "$APP_DIR/venv/bin/python" -m playwright install --with-deps chromium; then
  echo "    (não deu para instalar agora; o envio ao YouTube e ao TikTok ficam indisponíveis até rodar de novo)"
fi

echo "==> Copiando o programa para $APP_DIR"
install -m 644 "$ORIGEM/publicador.py" "$APP_DIR/publicador.py"
install -m 644 "$YT" "$APP_DIR/youtube_browser_upload.py"
install -m 644 "$TT" "$APP_DIR/tiktok_upload.py"
install -m 644 "$ORIGEM/requirements.txt" "$APP_DIR/requirements.txt"
rm -rf "$APP_DIR/templates"
mkdir -p "$APP_DIR/templates"
install -m 644 "$ORIGEM/templates/"*.html "$APP_DIR/templates/"

echo "==> Instalando o serviço do systemd"
sed -e "s|__APP_DIR__|$APP_DIR|g" \
    -e "s|__DATA_DIR__|$DATA_DIR|g" \
    -e "s|__USUARIO__|$USUARIO|g" \
    -e "s|__PORTA__|$PORTA|g" \
    -e "s|__FUSO__|$FUSO|g" \
    "$ORIGEM/publicador.service" > "/etc/systemd/system/$SERVICO.service"
systemctl daemon-reload
systemctl enable --quiet "$SERVICO"
systemctl restart "$SERVICO"

sleep 2
if ! systemctl is-active --quiet "$SERVICO"; then
  echo
  echo "O serviço não subiu. Veja o motivo com:  journalctl -u $SERVICO -n 40" >&2
  exit 1
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Pronto. Abra no navegador:  http://${IP:-localhost}:$PORTA"
echo
echo "Próximo passo: na tela 'Canais', mande os arquivos"
echo "youtube_browser_cookies_<canal>.txt (cookies exportados do navegador,"
echo "logado em studio.youtube.com) e, se algum canal também publica no"
echo "TikTok, o tiktok_cookies_<conta>.txt."
echo
echo "Comandos úteis:"
echo "  systemctl status $SERVICO         estado do serviço"
echo "  journalctl -u $SERVICO -f         acompanhar os erros"
echo "  ls -lh $DATA_DIR/videos           vídeos guardados no HD"
