#!/usr/bin/env bash
# Instala (ou atualiza) o serviço de Histórias no servidor Arch (.133), sem sudo.
#
#   bash ~/video_maker/servidor/instalar-historias.sh
#
# Pode rodar de novo: só recria o venv se ele não existir, atualiza as
# dependências e reinicia o serviço. Os dados ficam em ~/VideoMaker/historias
# (banco, projetos, biblioteca, trilhas, Music e o .env com as chaves).
set -euo pipefail

CODIGO="$HOME/video_maker/historias"
DADOS="$HOME/VideoMaker/historias"
UNIDADE="$HOME/.config/systemd/user/videomaker-historias.service"

cd "$CODIGO"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

mkdir -p "$DADOS" "$HOME/VideoMaker/tmp" "$(dirname "$UNIDADE")"
[ -f "$DADOS/.env" ] || cp .env.example "$DADOS/.env"
chmod 600 "$DADOS/.env"

# Fontes da legenda e do Music copiadas do Windows (Arial, Arial Black...): ficam
# em ~/.local/share/fonts; o fontconfig só as acha depois do fc-cache.
command -v fc-cache >/dev/null && fc-cache -f "$HOME/.local/share/fonts" >/dev/null 2>&1 || true

cp "$HOME/video_maker/servidor/videomaker-historias.service" "$UNIDADE"
systemctl --user daemon-reload
systemctl --user enable videomaker-historias.service >/dev/null
systemctl --user restart videomaker-historias.service
sleep 3
systemctl --user --no-pager --lines=5 status videomaker-historias.service || true
curl -fsS -m 10 http://127.0.0.1:8091/api/status >/dev/null && echo "Histórias no ar em 127.0.0.1:8091"
