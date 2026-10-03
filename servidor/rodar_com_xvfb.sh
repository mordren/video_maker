#!/usr/bin/env bash
# Dá um display X virtual ao comando recebido, sem depender do pacote
# "xvfb-run" (que nem toda distro empacota) — só precisa do binário Xvfb,
# que vem no xorg-server-xvfb. Usado pelo Publicador: os envios ao YouTube e
# ao TikTok abrem um Chromium de verdade (Playwright), e esta máquina não
# tem tela.
set -euo pipefail

DISPLAY_NUM=":97"
Xvfb "$DISPLAY_NUM" -screen 0 1920x1080x24 -nolisten tcp &
XVFB_PID=$!
trap 'kill "$XVFB_PID" 2>/dev/null || true' EXIT
export DISPLAY="$DISPLAY_NUM"
sleep 1
exec "$@"
