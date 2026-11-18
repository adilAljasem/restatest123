#!/bin/bash
# تركيب منصة ريستا على سيرفر المنصة في Google Cloud (Ubuntu 24.04)
#
# قبل التشغيل:
#   1) انسخ شهادة Cloudflare Origin إلى /opt/resta-platform/certs/origin.pem و origin.key
#   2) شغّل:  sudo GIT_TOKEN=<مفتاح GitHub قراءة فقط> bash install_platform.sh
#   المنصة تشتغل على نفس صورة ريستا (أودو 19 المخفف) المبنية من Dockerfile المستودع.
set -euo pipefail
ROOT=/opt/resta-platform
BRANCH="${BRANCH:-staging}"
REPO="${REPO:-https://github.com/walsoodi/restash.git}"
: "${GIT_TOKEN:?اكتب GIT_TOKEN=... قبل الأمر}"

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 git curl
systemctl enable --now docker

mkdir -p "$ROOT"/{certs,data/postgres,data/odoo}
if [ ! -s "$ROOT/certs/origin.pem" ] || [ ! -s "$ROOT/certs/origin.key" ]; then
  echo "ضع شهادة Cloudflare Origin في $ROOT/certs/origin.pem و origin.key ثم أعد التشغيل"; exit 1
fi
chmod 600 "$ROOT/certs/origin.key"

AUTH_REPO="${REPO/https:\/\//https://x-access-token:${GIT_TOKEN}@}"
if [ -d "$ROOT/src/.git" ]; then
  git -C "$ROOT/src" remote set-url origin "$AUTH_REPO"
  git -C "$ROOT/src" fetch --depth 1 origin "$BRANCH" && git -C "$ROOT/src" reset --hard FETCH_HEAD
else
  git clone --depth 1 --branch "$BRANCH" "$AUTH_REPO" "$ROOT/src"
fi
ADDONS=/opt/odoo/odoo/addons,/opt/odoo/resta_addons,/opt/odoo/bs_addons

if [ ! -f "$ROOT/.env" ]; then
  echo "DB_PASSWORD=$(openssl rand -hex 24)" > "$ROOT/.env"
  echo "MASTER_PASSWORD=$(openssl rand -hex 24)" >> "$ROOT/.env"
  chmod 600 "$ROOT/.env"
fi
. "$ROOT/.env"

cat > "$ROOT/odoo.conf" <<CONF
[options]
db_host = db
db_port = 5432
db_user = odoo
db_password = ${DB_PASSWORD}
admin_passwd = ${MASTER_PASSWORD}
db_name = platform
dbfilter = ^platform\$
list_db = False
addons_path = ${ADDONS}
data_dir = /var/lib/odoo
proxy_mode = True
workers = 4
max_cron_threads = 2
gevent_port = 8072
limit_time_real = 1200
CONF
chmod 644 "$ROOT/odoo.conf"
cp "$ROOT/src/infra/platform/compose.yml" "$ROOT/compose.yml"
cp "$ROOT/src/infra/platform/nginx.conf" "$ROOT/nginx.conf"

cd "$ROOT"
docker compose up -d db
until docker compose exec -T db pg_isready -U odoo >/dev/null 2>&1; do sleep 2; done
echo "بناء صورة ريستا (أول مرة 10-15 دقيقة)..."
docker compose build odoo
docker compose run --rm --no-deps -T --user root --entrypoint chown odoo -R odoo:odoo /var/lib/odoo
if ! docker compose exec -T db psql -U odoo -lqt | cut -d'|' -f1 | grep -qw platform; then
  # شركة المنصة سعودية قبل تثبيت المحاسبة: شجرة الحسابات السعودية وضريبة 15% والريال
  docker compose run --rm -T odoo odoo -c /etc/odoo/platform.conf -d platform \
    -i base --load-language=ar_001 --stop-after-init
  docker compose run --rm -T odoo odoo shell -c /etc/odoo/platform.conf -d platform --no-http <<'PY'
sar = env.ref('base.SAR')
sar.active = True
env.company.write({'country_id': env.ref('base.sa').id, 'currency_id': sar.id})
env.cr.commit()
PY
  docker compose run --rm -T odoo odoo -c /etc/odoo/platform.conf -d platform \
    -i resta_platform,l10n_sa --stop-after-init
else
  docker compose run --rm -T odoo odoo -c /etc/odoo/platform.conf -d platform \
    -u resta_platform --stop-after-init
fi
docker compose up -d
INTERNAL_IP=$(curl -fsS -H "Metadata-Flavor: Google" \
  http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/ip || hostname -I | awk '{print $1}')
cat <<MSG

تم تركيب المنصة.
- لوحة التحكم: https://control.<نطاقكم>  (اربط control في Cloudflare بعنوان هذا السيرفر الخارجي)
- رابط المنصة الداخلي للإعدادات: http://${INTERNAL_IP}:8069
- كلمة سر قاعدة البيانات وكلمة سر أودو الرئيسية في: $ROOT/.env
- أول دخول: admin / admin  — غيّرها فوراً وفعّل التحقق بخطوتين.
MSG
