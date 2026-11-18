"""ربط Google Cloud (Compute Engine) بدون مكتبات إضافية.

التوثيق (بالترتيب):
  1. بدون مفتاح: المنصة تشتغل على سيرفر في Google مربوط بحساب خدمة، فتأخذ الرمز من خادم
     البيانات الوصفية للسيرفر (الأسلم، ولا يحتاج إنشاء مفاتيح).
  2. مفتاح حساب الخدمة (JSON) ← توقيع JWT ← رمز وصول لمدة ساعة.
الصلاحيات المطلوبة لحساب الخدمة:
  Compute Instance Admin (v1) + Compute Public IP Admin + Service Account User (على حساب السيرفرات)
"""
import base64
import json
import logging
import time

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

SCOPE = 'https://www.googleapis.com/auth/cloud-platform'
COMPUTE = 'https://compute.googleapis.com/compute/v1/projects/{project}/'
IMAGE = 'projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64'
METADATA = 'http://169.254.169.254/computeMetadata/v1/'
_TOKENS = {}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class RestaGcp(models.AbstractModel):
    _name = 'resta.gcp'
    _description = 'Google Cloud'

    # ------------------------------------------------------------ auth
    @api.model
    def _credentials(self):
        """مفتاح JSON إن وُجد، وإلا None (يُستخدم حساب خدمة السيرفر نفسه)."""
        raw = self.env['resta.config'].get('gcp_credentials')
        if not raw:
            return None
        try:
            creds = json.loads(raw)
            creds['client_email'] and creds['private_key']
        except (ValueError, KeyError, TypeError):
            raise UserError(_('مفتاح حساب الخدمة غير صحيح. الصق محتوى ملف JSON كاملاً، أو اتركه فارغاً.'))
        return creds

    @api.model
    def _metadata(self, path):
        try:
            resp = requests.get(METADATA + path, headers={'Metadata-Flavor': 'Google'}, timeout=5)
        except requests.RequestException:
            resp = None
        if resp is None or resp.status_code != 200:
            raise UserError(_('المنصة ليست على سيرفر Google مربوط بحساب خدمة. '
                              'شغّل المنصة على سيرفر Google بحساب resta-platform، أو أضف مفتاح JSON في الإعدادات.'))
        return resp

    @api.model
    def _metadata_token(self):
        cached = _TOKENS.get('__metadata__')
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        data = self._metadata('instance/service-accounts/default/token').json()
        _TOKENS['__metadata__'] = (data['access_token'], int(time.time()) + int(data.get('expires_in', 300)))
        return data['access_token']

    @api.model
    def is_configured(self):
        cfg = self.env['resta.config']
        return bool(cfg.get('gcp_credentials') or cfg.get('gcp_project'))

    @api.model
    def _token(self):
        creds = self._credentials()
        if not creds:
            return self._metadata_token()
        email = creds['client_email']
        cached = _TOKENS.get(email)
        if cached and cached[1] > time.time() + 60:
            return cached[0]
        token_uri = creds.get('token_uri', 'https://oauth2.googleapis.com/token')
        now = int(time.time())
        header = _b64(json.dumps({'alg': 'RS256', 'typ': 'JWT'}).encode())
        claims = _b64(json.dumps({'iss': email, 'scope': SCOPE, 'aud': token_uri,
                                  'iat': now, 'exp': now + 3600}).encode())
        signing_input = f'{header}.{claims}'.encode()
        key = serialization.load_pem_private_key(creds['private_key'].encode(), password=None)
        signature = key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        assertion = signing_input.decode() + '.' + _b64(signature)
        resp = requests.post(token_uri, data={
            'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion}, timeout=20)
        if resp.status_code != 200:
            raise UserError(_('رفض Google تسجيل الدخول: %s', resp.text[:300]))
        data = resp.json()
        _TOKENS[email] = (data['access_token'], now + int(data.get('expires_in', 3600)))
        return data['access_token']

    # ------------------------------------------------------------ api
    @api.model
    def _api(self, method, path, payload=None, params=None, allow_404=False):
        cfg = self.env['resta.config']
        creds = self._credentials()
        project = cfg.get('gcp_project') or (creds or {}).get('project_id')
        if not project and not creds:
            project = self._metadata('project/project-id').text.strip()
        if not project:
            raise UserError(_('حدد مشروع Google Cloud في الإعدادات.'))
        url = COMPUTE.format(project=project) + path
        resp = requests.request(method, url, json=payload, params=params, timeout=40,
                                headers={'Authorization': f'Bearer {self._token()}'})
        if allow_404 and resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            try:
                message = resp.json()['error']['message']
            except Exception:
                message = resp.text[:400]
            raise UserError(_('خطأ من Google Cloud (%(code)s): %(msg)s', code=resp.status_code, msg=message))
        return resp.json() if resp.content else {}

    def _region(self):
        return self.env['resta.config'].get('gcp_region')

    # ------------------------------------------------------------ addresses
    @api.model
    def reserve_address(self, name):
        region = self._region()
        existing = self._api('GET', f'regions/{region}/addresses/{name}', allow_404=True)
        if not existing:
            self._api('POST', f'regions/{region}/addresses', {
                'name': name, 'description': 'Resta server', 'networkTier': 'PREMIUM'})
        for _i in range(30):
            addr = self._api('GET', f'regions/{region}/addresses/{name}', allow_404=True)
            if addr and addr.get('address'):
                return addr['address']
            time.sleep(2)
        raise UserError(_('لم يتم حجز عنوان IP خلال الوقت المحدد.'))

    @api.model
    def release_address(self, name):
        return self._api('DELETE', f'regions/{self._region()}/addresses/{name}', allow_404=True)

    # ------------------------------------------------------------ instances
    @api.model
    def create_instance(self, name, zone, machine_type, disk_gb, ip, metadata, kind='cell', labels=None):
        cfg = self.env['resta.config']
        network = cfg.get('gcp_network')
        sa_email = cfg.get('cell_sa_email')
        body = {
            'name': name,
            'machineType': f'zones/{zone}/machineTypes/{machine_type}',
            'labels': dict({'resta': kind}, **(labels or {})),
            'tags': {'items': ['resta-cell']},
            'disks': [{
                'boot': True, 'autoDelete': True,
                'initializeParams': {
                    'sourceImage': IMAGE, 'diskSizeGb': str(disk_gb),
                    'diskType': f'zones/{zone}/diskTypes/pd-balanced',
                },
            }],
            'networkInterfaces': [{
                'network': network,
                'accessConfigs': [{'name': 'External NAT', 'type': 'ONE_TO_ONE_NAT',
                                   'natIP': ip, 'networkTier': 'PREMIUM'}],
            }],
            'metadata': {'items': [{'key': k, 'value': str(v)} for k, v in metadata.items()]},
            'scheduling': {'automaticRestart': True, 'onHostMaintenance': 'MIGRATE'},
        }
        if sa_email:
            body['serviceAccounts'] = [{'email': sa_email, 'scopes': [SCOPE]}]
        return self._api('POST', f'zones/{zone}/instances', body)

    @api.model
    def get_instance(self, name, zone):
        return self._api('GET', f'zones/{zone}/instances/{name}', allow_404=True)

    @api.model
    def delete_instance(self, name, zone):
        return self._api('DELETE', f'zones/{zone}/instances/{name}', allow_404=True)
