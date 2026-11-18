from odoo import api, models

DEFAULTS = {
    'gcp_region': 'me-central2',
    'gcp_zone': 'me-central2-a',
    'gcp_network': 'global/networks/default',
    'cell_machine_type': 'e2-standard-4',
    'cell_disk_gb': '200',
    'base_domain': 'restaerp.com',
    # مشروع ريستا الكامل (أودو المخفف + الموديولات) في نفس المستودع
    'saas_repo': 'https://github.com/walsoodi/restash.git',
    'saas_branch': 'staging',
    'modules_dinein': 'resta_edition_dinein',
    'modules_fastfood': 'resta_edition_qsr',
    'agent_repo': 'https://github.com/walsoodi/restash.git',
    'agent_branch': 'staging',
    'enterprise_branch': '19.0',
    'load_threshold': '70',
    'grace_days': '7',
    'trial_days': '14',
    'cell_capacity': '10',
    'default_lang': 'ar_001',
}


class RestaConfig(models.AbstractModel):
    _name = 'resta.config'
    _description = 'إعدادات منصة ريستا'

    @api.model
    def get(self, key, default=None):
        value = self.env['ir.config_parameter'].sudo().get_param('resta.' + key)
        if value in (None, False, ''):
            return DEFAULTS.get(key, default)
        return value

    @api.model
    def get_int(self, key, default=0):
        try:
            return int(float(self.get(key, default)))
        except (TypeError, ValueError):
            return default

    @api.model
    def get_bool(self, key):
        return str(self.get(key, '')).lower() in ('1', 'true', 'yes')

    @api.model
    def modules_for(self, edition):
        raw = self.get('modules_' + edition, '') or ''
        return [m.strip() for m in raw.replace('\n', ',').split(',') if m.strip()]
