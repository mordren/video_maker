#!/usr/bin/env bash
#
# Único passo que precisa de root nesta máquina (Arch, 192.168.31.133): os
# pacotes de sistema que faltam e os dois serviços do systemd. O resto —
# clonar o repositório, o venv do Python, os dados (trilhas, logos, cookies,
# a chave do OpenRouter) — já foi feito sem precisar de senha.
#
# Rode como root, dentro do repositório já clonado em ~/video_maker:
#
#     sudo bash servidor/instalar-arch.sh
#
# Pode rodar de novo a qualquer momento: só reinstala os pacotes que
# faltarem e reinicia os dois serviços — trabalhos, fila e cookies não são
# tocados (ficam em ~/VideoMaker, fora do repositório).

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Precisa ser root. Rode: sudo bash $0" >&2
  exit 1
fi

ORIGEM="$(cd "$(dirname "$0")" && pwd)"

echo "==> Instalando pacotes do sistema que faltam"
# xorg-server-xvfb: display virtual para o Chromium do Publicador (esta
# máquina não tem tela). deno: exigido pelo yt-dlp para baixar do YouTube
# (o cliente "default" dá HTTP 403 sem ele — ver estudio/estudio.py).
pacman -S --needed --noconfirm xorg-server-xvfb deno

echo "==> Instalando os serviços no systemd"
install -m 644 "$ORIGEM/videomaker-publicador.service" /etc/systemd/system/videomaker-publicador.service
install -m 644 "$ORIGEM/videomaker-estudio.service" /etc/systemd/system/videomaker-estudio.service
chmod +x "$ORIGEM/rodar_com_xvfb.sh"

systemctl daemon-reload
systemctl enable --quiet videomaker-publicador videomaker-estudio
systemctl restart videomaker-publicador videomaker-estudio

sleep 3
FALHOU=0
for s in videomaker-publicador videomaker-estudio; do
  if ! systemctl is-active --quiet "$s"; then
    echo "$s não subiu — veja: journalctl -u $s -n 40" >&2
    FALHOU=1
  fi
done
[ "$FALHOU" -eq 0 ] || exit 1

echo
echo "Pronto. Abra no navegador (rede local):"
echo "  Estúdio:    http://192.168.31.133:8090"
echo "  Publicador: http://192.168.31.133:8080"
echo
echo "Comandos úteis:"
echo "  systemctl status videomaker-estudio videomaker-publicador"
echo "  journalctl -u videomaker-estudio -f"
echo "  journalctl -u videomaker-publicador -f"
