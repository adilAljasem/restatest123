"""ربط Cloudflare: رابط لكل مطعم يشير لسيرفره (مع الحماية والتشفير من Cloudflare)."""
import logging

import requests

from odoo import _, api, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)
API = 'https://api.cloudflare.com/client/v4/'


class RestaCloudflare(models.AbstractModel):
    _name = 'resta.cloudflare'
    _description = 'Cloudflare'

    @api.model
    def _enabled(self):
        cfg = self.env['resta.config']
        return bool(cfg.get('cf_token') and cfg.get('cf_zone_id'))

    @api.model
    def _call(self, method, path, payload=None, params=None):
        cfg = self.env['resta.config']
        resp = requests.request(method, API + path, json=payload, params=params, timeout=30, headers={
            'Authorization': f"Bearer {cfg.get('cf_token')}", 'Content-Type': 'application/json'})
        data = resp.json() if resp.content else {}
        if resp.status_code >= 400 or not data.get('success', True):
            errors = data.get('errors') or resp.text[:300]
            raise UserError(_('خطأ من Cloudflare: %s', errors))
        return data

    @api.model
    def fqdn(self, sub):
        return f"{sub}.{self.env['resta.config'].get('base_domain')}"

    @api.model
    def upsert(self, sub, ip, proxied=True):
        if not self._enabled():
            _logger.warning('Cloudflare is not configured; skipping DNS for %s', sub)
            return False
        zone = self.env['resta.config'].get('cf_zone_id')
        name = self.fqdn(sub)
        found = self._call('GET', f'zones/{zone}/dns_records', params={'type': 'A', 'name': name})
        record = {'type': 'A', 'name': name, 'content': ip, 'proxied': proxied, 'ttl': 1,
                  'comment': 'Resta platform'}
        if found.get('result'):
            rid = found['result'][0]['id']
            self._call('PUT', f'zones/{zone}/dns_records/{rid}', record)
        else:
            self._call('POST', f'zones/{zone}/dns_records', record)
        return True

    @api.model
    def remove(self, sub):
        if not self._enabled():
            return False
        zone = self.env['resta.config'].get('cf_zone_id')
        found = self._call('GET', f'zones/{zone}/dns_records', params={'type': 'A', 'name': self.fqdn(sub)})
        for rec in found.get('result') or []:
            self._call('DELETE', f"zones/{zone}/dns_records/{rec['id']}")
        return True
