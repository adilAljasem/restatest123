#!/bin/bash
# سكربت إقلاع سيرفرات ريستا في Google Cloud (يشتغل تلقائياً كـ startup-script مع كل تشغيل)
# 1) يثبت Docker وrestic  2) ينزل الوكيل  3) يسجّل في المنصة ويستلم إعداداته  4) يجهز السيرفر
set -euo pipefail
exec > >(tee -a /var/log/resta-bootstrap.log) 2>&1
MD="http://metadata.google.internal/computeMetadata/v1"
md() { curl -fsS -H "Metadata-Flavor: Google" "$MD/$1"; }

if [ -f /etc/resta/.bootstrapped ]; then
  systemctl start resta-agent || true
  exit 0
fi

PLATFORM_URL="$(md instance/attributes/resta-platform-url)"
TOKEN="$(md instance/attributes/resta-register-token)"
SERVER_ID="$(md instance/attributes/resta-server-id)"
AGENT_REPO="$(md instance/attributes/resta-agent-repo)"
AGENT_BRANCH="$(md instance/attributes/resta-agent-branch)"
GIT_TOKEN="$(md instance/attributes/resta-git-token || true)"
INTERNAL_IP="$(md instance/network-interfaces/0/ip)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 git python3-venv curl jq bzip2
systemctl enable --now docker

RESTIC_VER=0.17.3
if ! command -v restic >/dev/null; then
  curl -fsSL -o /tmp/restic.bz2 "https://github.com/restic/restic/releases/download/v${RESTIC_VER}/restic_${RESTIC_VER}_linux_amd64.bz2"
  bunzip2 -f /tmp/restic.bz2 && install -m 0755 /tmp/restic /usr/local/bin/restic
fi

# ذاكرة احتياطية 4GB تحمي من توقف أودو عند الضغط
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q /swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# كود الوكيل
REPO_URL="$AGENT_REPO"
if [ -n "$GIT_TOKEN" ]; then REPO_URL="${AGENT_REPO/https:\/\//https://x-access-token:${GIT_TOKEN}@}"; fi
rm -rf /opt/resta-agent-src
# المستودع فيه أودو كامل؛ الوكيل يحتاج مجلدي agent و infra فقط
git clone --depth 1 --filter=blob:none --sparse --branch "$AGENT_BRANCH" "$REPO_URL" /opt/resta-agent-src
git -C /opt/resta-agent-src sparse-checkout set agent infra
python3 -m venv /opt/resta-agent
/opt/resta-agent/bin/pip install -q --upgrade pip
/opt/resta-agent/bin/pip install -q /opt/resta-agent-src/agent

# التسجيل في المنصة مرة واحدة (الرمز يُلغى بعد الاستخدام)
mkdir -p /etc/resta && chmod 700 /etc/resta
if [ ! -s /etc/resta/register.json ]; then
  for i in $(seq 1 30); do
    if curl -fsS -X POST "$PLATFORM_URL/resta/agent/register" -H 'Content-Type: application/json' \
         -d "{\"server_id\": $SERVER_ID, \"token\": \"$TOKEN\", \"internal_ip\": \"$INTERNAL_IP\"}" \
         -o /etc/resta/register.json.tmp; then
      mv /etc/resta/register.json.tmp /etc/resta/register.json
      break
    fi
    echo "registration attempt $i failed, retrying in 20s"; sleep 20
  done
fi
test -s /etc/resta/register.json
chmod 600 /etc/resta/register.json

/opt/resta-agent/bin/resta-agent setup --input /etc/resta/register.json

for unit in resta-agent.service resta-backup.service resta-backup.timer; do
  install -m 0644 "/opt/resta-agent-src/infra/cell/systemd/$unit" /etc/systemd/system/
done
systemctl daemon-reload
systemctl enable --now resta-agent.service resta-backup.timer
rm -f /etc/resta/register.json
touch /etc/resta/.bootstrapped
echo "resta bootstrap done"
