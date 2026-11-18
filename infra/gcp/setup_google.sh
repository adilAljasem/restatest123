#!/bin/bash
# تجهيز Google Cloud لمنصة ريستا بأمر واحد (يُلصق في Cloud Shell).
# آمن للتكرار: إذا انقطع أو فيه شيء موجود من قبل، شغّله مرة ثانية ويكمل.
#
# قبل التشغيل: المشروع مربوط بحساب فوترة فعّال (للشركات السعودية عن طريق CNTXT).
set -uo pipefail

REGION="${REGION:-me-central2}"
ZONE="${ZONE:-me-central2-a}"
PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
[ -n "$PROJECT" ] || { echo "اختر المشروع أولاً: gcloud config set project <رقم المشروع>"; exit 1; }
gcloud config set project "$PROJECT" >/dev/null 2>&1
echo "== المشروع: $PROJECT"

ok()   { echo "   ✓ $*"; }
fail() { echo "   ✗ $*"; exit 1; }

# 1) الفوترة
if [ "$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null)" != "True" ]; then
  fail "المشروع غير مربوط بحساب فوترة فعّال. اربطه من صفحة الفوترة ثم أعد التشغيل."
fi
ok "الفوترة فعّالة"

# 2) الخدمات
gcloud services enable compute.googleapis.com storage.googleapis.com iap.googleapis.com \
  || fail "تعذّر تفعيل الخدمات"
ok "الخدمات مفعّلة"

# 3) الشبكة والجدار الناري
gcloud compute networks describe default >/dev/null 2>&1 \
  || gcloud compute networks create default --subnet-mode=auto || fail "تعذّر إنشاء الشبكة"
rule() {  # name, then the rest of the arguments
  local name="$1"; shift
  if gcloud compute firewall-rules describe "$name" >/dev/null 2>&1; then
    gcloud compute firewall-rules update "$name" "$@" >/dev/null 2>&1 || true
  else
    gcloud compute firewall-rules create "$name" --network=default --direction=INGRESS --action=ALLOW "$@" \
      || fail "تعذّر إنشاء قاعدة $name"
  fi
}
CF_IPS="$(curl -fsS https://www.cloudflare.com/ips-v4 | paste -sd, -)"
[ -n "$CF_IPS" ] || fail "تعذّر جلب عناوين Cloudflare"
rule resta-https-cloudflare --rules=tcp:443 --source-ranges="$CF_IPS" --target-tags=resta-cell,resta-platform
rule resta-agent-from-platform --rules=tcp:8090 --source-tags=resta-platform --target-tags=resta-cell
rule resta-cells-to-platform --rules=tcp:8069 --source-tags=resta-cell --target-tags=resta-platform
rule resta-ssh-iap --rules=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=resta-cell,resta-platform
for open_rule in default-allow-ssh default-allow-rdp; do
  gcloud compute firewall-rules describe "$open_rule" >/dev/null 2>&1 \
    && gcloud compute firewall-rules delete "$open_rule" --quiet >/dev/null 2>&1
done
ok "الجدار الناري: الإنترنت عبر Cloudflare فقط، والدخول للطوارئ عبر IAP"

# 4) حسابات الخدمة (بدون مفاتيح: المنصة تستخدم حساب سيرفرها مباشرة)
PLATFORM_SA="resta-platform@${PROJECT}.iam.gserviceaccount.com"
CELL_SA="resta-cell@${PROJECT}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "$PLATFORM_SA" >/dev/null 2>&1 \
  || gcloud iam service-accounts create resta-platform --display-name="Resta Platform" || fail "حساب المنصة"
gcloud iam service-accounts describe "$CELL_SA" >/dev/null 2>&1 \
  || gcloud iam service-accounts create resta-cell --display-name="Resta Cells" || fail "حساب السيرفرات"
sleep 10  # حسابات الخدمة الجديدة تحتاج ثوانٍ قبل منحها صلاحيات
for role in roles/compute.instanceAdmin.v1 roles/compute.publicIpAdmin; do
  gcloud projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:$PLATFORM_SA" --role="$role" \
    --condition=None >/dev/null || fail "صلاحية $role"
done
gcloud iam service-accounts add-iam-policy-binding "$CELL_SA" --member="serviceAccount:$PLATFORM_SA" \
  --role=roles/iam.serviceAccountUser >/dev/null || fail "صلاحية استخدام حساب السيرفرات"
ok "حسابات الخدمة والصلاحيات"

# 5) مخزن النسخ الاحتياطية
BUCKET="${PROJECT}-resta-backups"
gcloud storage buckets describe "gs://$BUCKET" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
  || fail "تعذّر إنشاء مخزن النسخ"
for role in roles/storage.objectAdmin roles/storage.legacyBucketReader; do
  gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" --member="serviceAccount:$CELL_SA" \
    --role="$role" >/dev/null || fail "صلاحية المخزن $role"
done
ok "مخزن النسخ الاحتياطية: $BUCKET"

# 6) سيرفر المنصة
gcloud compute addresses describe resta-platform-ip --region="$REGION" >/dev/null 2>&1 \
  || gcloud compute addresses create resta-platform-ip --region="$REGION" || fail "تعذّر حجز العنوان"
if ! gcloud compute instances describe resta-platform --zone="$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create resta-platform \
    --zone="$ZONE" --machine-type=e2-standard-2 \
    --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud \
    --boot-disk-size=60GB --boot-disk-type=pd-balanced \
    --address=resta-platform-ip --tags=resta-platform \
    --service-account="$PLATFORM_SA" --scopes=cloud-platform \
    || fail "تعذّر إنشاء سيرفر المنصة (راجع الحصة في المنطقة $REGION)"
fi
EXTERNAL_IP="$(gcloud compute addresses describe resta-platform-ip --region="$REGION" --format='value(address)')"
INTERNAL_IP="$(gcloud compute instances describe resta-platform --zone="$ZONE" \
  --format='value(networkInterfaces[0].networkIP)')"
ok "سيرفر المنصة جاهز"

cat <<MSG

==================== انتهى تجهيز Google ====================
احفظ هذه القيم:
  عنوان سيرفر المنصة الخارجي (لسجل control في Cloudflare):  $EXTERNAL_IP
  رابط المنصة الداخلي (لإعدادات المنصة):                     http://$INTERNAL_IP:8069
  مشروع Google Cloud:                                        $PROJECT
  حساب خدمة السيرفرات:                                       $CELL_SA
  Bucket النسخ الاحتياطية:                                    $BUCKET
مفتاح JSON غير مطلوب: اترك حقله فارغاً في إعدادات المنصة.

الخطوة التالية: الدخول لسيرفر المنصة
  gcloud compute ssh resta-platform --zone=$ZONE --tunnel-through-iap
=============================================================
MSG
