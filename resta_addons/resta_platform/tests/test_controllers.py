import json

from odoo.tests import HttpCase, tagged

from ..models.signing import signed_headers


@tagged('post_install', '-at_install', 'resta')
class TestRestaControllers(HttpCase):

    def setUp(self):
        super().setUp()
        self.server = self.env['resta.server'].create({
            'name': 'cell-77', 'kind': 'cell', 'provider': 'manual', 'state': 'ready', 'registered': True,
            'internal_ip': '10.0.0.77', 'agent_secret': 'z' * 64})
        self.op = self.env['resta.operation'].create({
            'server_id': self.server.id, 'op_type': 'backup_all', 'payload_dict': {}, 'state': 'running'})

    def _post(self, path, payload, headers):
        body = json.dumps(payload).encode()
        return self.url_open(path, data=body, headers=dict(headers or {}, **{'Content-Type': 'application/json'}))

    def test_callback_signature(self):
        payload = {'server_id': self.server.id,
                   'job': {'operation_id': self.op.name, 'id': 'j1', 'state': 'done', 'result': {'done': []}}}
        body = json.dumps(payload).encode()
        bad = self._post('/resta/agent/callback', payload, signed_headers('y' * 64, body))
        self.assertEqual(bad.status_code, 401)
        self.assertEqual(self.op.state, 'running')
        good = self.url_open('/resta/agent/callback', data=body, headers=signed_headers('z' * 64, body))
        self.assertEqual(good.status_code, 200, good.text)
        self.op.invalidate_recordset()
        self.assertEqual(self.op.state, 'done')

    def test_register_and_signup_keys(self):
        self.server.sudo().write({'register_token': 'once', 'registered': False})
        self.env['ir.config_parameter'].sudo().set_param('resta.restic_password', 'p')
        self.env['ir.config_parameter'].sudo().set_param('resta.platform_internal_url', 'http://10.0.0.2:8069')
        r = self._post('/resta/agent/register', {'server_id': self.server.id, 'token': 'nope',
                                                 'internal_ip': '10.0.0.77'}, {})
        self.assertEqual(r.status_code, 403)
        r = self._post('/resta/agent/register', {'server_id': self.server.id, 'token': 'once',
                                                 'internal_ip': '10.0.0.77'}, {})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()['agent_secret'], 'z' * 64)
        r = self._post('/resta/api/signup', {'brand_name': 'x'}, {'X-Resta-Key': 'wrong'})
        self.assertEqual(r.status_code, 401)
