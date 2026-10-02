#!/usr/bin/env bash
# Liga o ZeroClaw (bot do Telegram na TV box) ao Estúdio de vídeo do .133:
# skills cortador, historias e music, que usam o comando "estudio".
#
# O que este script faz:
#   1. Instala o comando /usr/local/bin/estudio (Python puro), que fala com a API
#      do Estúdio em http://192.168.31.133:8090 (Cortador, Histórias e Music).
#      O ZeroClaw bloqueia $, ;, |, &, > e < nos comandos, por isso a skill usa
#      este comando e não curl.
#   2. Instala as skills cortador, historias e music no pacote "videos".
#   3. Libera o comando "estudio" no perfil de risco "padrao" e liga o pacote
#      "videos" ao agente "assistente" (mantendo o que já estava lá).
#   4. Reinicia o ZeroClaw e testa a conexão com o Estúdio.
#
# Pode rodar de novo sem problema. Copie a pasta servidor/zeroclaw para o box e
# rode como root:   bash instalar-skills-estudio.sh
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Rode como root." >&2; exit 1; }

AQUI=$(cd "$(dirname "$0")" && pwd)
ZC_HOME=/home/zeroclaw
ZC_DIR=$ZC_HOME/.zeroclaw
CONFIG=$ZC_DIR/config.toml
BUNDLE=videos
zc() { runuser -u zeroclaw -- env HOME=$ZC_HOME /usr/local/bin/zeroclaw "$@"; }

[ -f "$CONFIG" ] || { echo "Config do ZeroClaw não encontrada ($CONFIG)" >&2; exit 1; }
for f in estudio skills/cortador/SKILL.md skills/historias/SKILL.md skills/music/SKILL.md; do
  [ -f "$AQUI/$f" ] || { echo "Falta $AQUI/$f" >&2; exit 1; }
done

echo "==> 1/4 Instalando o comando estudio"
install -m 755 "$AQUI/estudio" /usr/local/bin/estudio

echo "==> 2/4 Instalando as skills (pacote $BUNDLE)"
for s in cortador historias music; do
  install -d -m 755 "$ZC_DIR/shared/skills/$BUNDLE/$s"
  install -m 644 "$AQUI/skills/$s/SKILL.md" "$ZC_DIR/shared/skills/$BUNDLE/$s/SKILL.md"
done
chown -R zeroclaw:zeroclaw "$ZC_DIR/shared"
zc skills bundle add "$BUNDLE" >/dev/null 2>&1 || true   # já existir não é erro

echo "==> 3/4 Liberando o comando estudio e ligando o pacote $BUNDLE ao assistente"
cp -a "$CONFIG" "$CONFIG.antes-estudio"
# As listas atuais + o que falta (sem repetir), num JSON Patch para o zeroclaw config patch.
python3 - "$CONFIG" "$BUNDLE" > /tmp/zc-patch-estudio.json <<'PY'
import json, sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
bundle = sys.argv[2]
cmds = cfg["risk_profiles"]["padrao"].get("allowed_commands", [])
pacotes = cfg["agents"]["assistente"].get("skill_bundles", [])
ops = [{"op": "replace", "path": "/risk_profiles/padrao/allowed_commands",
        "value": cmds + [c for c in ["estudio"] if c not in cmds]},
       {"op": "add", "path": "/agents/assistente/skill_bundles",
        "value": pacotes + [b for b in [bundle] if b not in pacotes]}]
print(json.dumps(ops))
PY
zc config patch - < /tmp/zc-patch-estudio.json
rm -f /tmp/zc-patch-estudio.json

echo "==> 4/4 Reiniciando o ZeroClaw e testando o Estúdio"
systemctl restart zeroclaw
sleep 15
echo
echo "Serviço:   $(systemctl is-active zeroclaw)"
echo "Comandos:  $(grep '^allowed_commands' "$CONFIG" | grep -o '"estudio"' || echo 'estudio NÃO liberado')"
echo "Pacotes:   $(grep -A8 '^\[agents.assistente\]' "$CONFIG" | grep '^skill_bundles' || echo '?')"
echo "Skills:"
zc skills list 2>&1 | grep -i -E "cortador|historias|music" || echo "  (as skills não apareceram; confira com: zeroclaw skills list)"
echo "Teste (como o usuário do bot):"
runuser -u zeroclaw -- /usr/local/bin/estudio status 2>&1 | head -12 || true
echo
echo "Pronto. No Telegram, mande /new e depois, por exemplo:"
echo "  \"o que está rodando no estúdio?\""
echo "  \"faz uma história de terror sobre um farol que acende sozinho\""
echo "  \"corta esse vídeo pro canal info: https://www.youtube.com/watch?v=...\""
