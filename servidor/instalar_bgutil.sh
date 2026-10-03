#!/usr/bin/env bash
# Prepara o gerador de PO Token do yt-dlp (sem servidor fixo — cada download
# chama um script Deno descartável). Não precisa de root; roda como mordren.
#
# Sem isso, o YouTube só libera 360p (formato 18) por link — ver o
# comentário em estudio/estudio.py:_baixar.
#
#     bash servidor/instalar_bgutil.sh

set -euo pipefail

DESTINO="$HOME/bgutil-ytdlp-pot-provider"

if [ ! -d "$DESTINO" ]; then
  git clone --quiet --depth 1 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git "$DESTINO"
else
  git -C "$DESTINO" pull --quiet
fi

cd "$DESTINO/server"
deno install --allow-scripts=npm:canvas --frozen

echo "Pronto. O plugin bgutil-ytdlp-pot-provider (instalado via pip em"
echo "fase1/requirements.txt) já acha o script sozinho em $DESTINO/server."
