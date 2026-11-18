from contextlib import contextmanager
from unittest.mock import patch

from odoo.tests import TransactionCase

from ..models.agent_client import AgentError  # noqa: F401  (يُستخدم في الاختبارات)

AGENT = 'odoo.addons.resta_platform.models.agent_client.RestaAgent'
CF = 'odoo.addons.resta_platform.models.cloudflare.RestaCloudflare'


class RestaCommon(TransactionCase):
    """لا تعتمد على أدوات اختبار المحاسبة: نسخة أودو المخففة تحذف مجلدات الاختبارات."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        if not company.chart_template:
            cls.env['account.chart.template'].try_loading('generic_coa', company=company, install_demo=False)
        cls.product_a = cls.env['product.product'].create({
            'name': 'اشتراك ريستا', 'type': 'service', 'lst_price': 499.0, 'taxes_id': [(6, 0, [])]})
        cls.partner_a = cls.env['res.partner'].create({'name': 'مطعم تجربة', 'email': 'test@kaffa.sa'})
        cls.env.user.write({'group_ids': [(4, cls.env.ref('resta_platform.group_resta_manager').id)]})
        icp = cls.env['ir.config_parameter'].sudo()
        for key, value in {
            'resta.base_domain': 'restaerp.com',
            'resta.platform_internal_url': 'http://10.0.0.2:8069',
            'resta.restic_password': 'test-restic-password',
            'resta.modules_dinein': 'contacts,point_of_sale',
            'resta.modules_fastfood': '',
            'resta.backup_gcs_bucket': 'resta-backups-test',
            'resta.gcp_project': 'resta-prod',
            'resta.load_threshold': '70',
            'resta.grace_days': '7',
            'resta.trial_days': '14',
        }.items():
            icp.set_param(key, value)
        cls.cell = cls.env['resta.server'].create({
            'name': 'cell-01', 'kind': 'cell', 'provider': 'manual', 'state': 'ready',
            'internal_ip': '10.0.0.5', 'external_ip': '34.1.1.1', 'agent_secret': 'a' * 64,
            'registered': True, 'capacity': 10,
        })
        cls.plan = cls.env.ref('resta_platform.plan_saas_dinein')
        cls.plan.product_id = cls.product_a
        cls.customer = cls.partner_a

    def _project(self, **vals):
        values = {'name': 'مطعم كفة', 'code': 'kaffa', 'project_type': 'saas', 'plan_id': self.plan.id,
                  'partner_id': self.customer.id, 'admin_email': 'owner@kaffa.sa'}
        values.update(vals)
        return self.env['resta.project'].create(values)

    @contextmanager
    def fake_infra(self, fail_agent=False, health=None):
        calls = []

        def fake_call(model, server, method, path, payload=None, timeout=20):
            calls.append((server.name, method, path, payload))
            if fail_agent:
                from ..models.agent_client import AgentError as AE
                raise AE('agent unreachable: test')
            if path == '/health':
                return health or {}
            if method == 'POST' and path == '/jobs':
                return {'id': 'job-' + payload['operation_id'], 'operation_id': payload['operation_id'],
                        'state': 'queued'}
            return {'state': 'running'}

        with patch(AGENT + '.call', fake_call), \
                patch(CF + '.upsert', lambda *a, **k: True), \
                patch(CF + '.remove', lambda *a, **k: True):
            yield calls

    def _finish(self, op, result=None, state='done', error=None):
        return self.env['resta.operation'].handle_callback(op.server_id, {
            'operation_id': op.name, 'id': 'job-' + op.name, 'state': state,
            'result': result or {}, 'error': error})
