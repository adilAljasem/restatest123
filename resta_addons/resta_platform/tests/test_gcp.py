from unittest.mock import MagicMock, patch

from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from ..models import gcp as gcp_module


@tagged('post_install', '-at_install', 'resta')
class TestGcpAuth(TransactionCase):

    def setUp(self):
        super().setUp()
        gcp_module._TOKENS.clear()
        icp = self.env['ir.config_parameter'].sudo()
        icp.set_param('resta.gcp_credentials', '')
        icp.set_param('resta.gcp_project', 'resta-prod')

    def test_metadata_token_without_key(self):
        """على سيرفر Google: الرمز من حساب خدمة السيرفر بدون مفتاح JSON."""
        def fake_get(url, headers=None, timeout=None):
            self.assertEqual(headers, {'Metadata-Flavor': 'Google'})
            r = MagicMock(status_code=200)
            r.json.return_value = {'access_token': 'meta-token', 'expires_in': 3000}
            return r
        calls = []

        def fake_request(method, url, json=None, params=None, timeout=None, headers=None):
            calls.append((method, url, headers))
            return MagicMock(status_code=200, content=b'{}', json=lambda: {})
        gcp = self.env['resta.gcp']
        self.assertTrue(gcp.is_configured())
        with patch.object(gcp_module.requests, 'get', fake_get), \
                patch.object(gcp_module.requests, 'request', fake_request):
            gcp._api('GET', 'zones/me-central2-a/instances/x')
        self.assertIn('/projects/resta-prod/', calls[0][1])
        self.assertEqual(calls[0][2]['Authorization'], 'Bearer meta-token')

    def test_clear_error_outside_google(self):
        def fail_get(*a, **k):
            raise gcp_module.requests.ConnectionError('no metadata')
        with patch.object(gcp_module.requests, 'get', fail_get), self.assertRaises(UserError):
            self.env['resta.gcp']._token()

    def test_bad_json_key(self):
        self.env['ir.config_parameter'].sudo().set_param('resta.gcp_credentials', '{"x": 1}')
        with self.assertRaises(UserError):
            self.env['resta.gcp']._credentials()
