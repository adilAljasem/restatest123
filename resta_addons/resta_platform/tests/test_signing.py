from odoo.tests import TransactionCase, tagged

from ..models.signing import sign, signed_headers, verify


@tagged('post_install', '-at_install', 'resta')
class TestSigning(TransactionCase):

    def test_same_vector_as_agent(self):
        # نفس القيمة في agent/tests/test_security.py — أي اختلاف يعني الطرفين ما يتفاهمون
        self.assertEqual(sign('k' * 32, '1700000000', b'{}'),
                         '0b3b35525c10b3757ffa78df69037021fc3168d8390a51a4fcb4a8d56a25b557')

    def test_roundtrip_and_tamper(self):
        h = signed_headers('s' * 40, b'{"a":1}', now=1_700_000_000)
        self.assertTrue(verify('s' * 40, h['X-Resta-Timestamp'], h['X-Resta-Signature'], b'{"a":1}', now=1_700_000_005))
        self.assertFalse(verify('s' * 40, h['X-Resta-Timestamp'], h['X-Resta-Signature'], b'{"a":2}', now=1_700_000_005))
        self.assertFalse(verify('s' * 40, h['X-Resta-Timestamp'], h['X-Resta-Signature'], b'{"a":1}', now=1_700_009_000))
