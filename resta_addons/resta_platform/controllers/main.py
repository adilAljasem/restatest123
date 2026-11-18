"""نقاط الاتصال:

  POST /resta/agent/register      السيرفر الجديد يسجّل نفسه (رمز لمرة واحدة) ويستلم إعداداته
  POST /resta/agent/callback      نتائج العمليات من الوكيل (موقّعة بسر السيرفر)
  POST /resta/api/signup          التسجيل الذاتي من موقع resta.sa (مفتاح X-Resta-Key)
  GET  /resta/api/project/<code>  حالة المشروع لموقع resta.sa
"""
import hmac
import json
import logging

from odoo import http
from odoo.exceptions import UserError, ValidationError
from odoo.http import request

from ..models.signing import verify

_logger = logging.getLogger(__name__)


def _json(data, status=200):
    return request.make_json_response(data, status=status)


def _body():
    raw = request.httprequest.get_data() or b''
    try:
        return raw, json.loads(raw or b'{}')
    except ValueError:
        return raw, None


class RestaPlatformController(http.Controller):

    @http.route('/resta/agent/register', type='http', auth='public', methods=['POST'], csrf=False,
                save_session=False)
    def agent_register(self, **_kw):
        _raw, data = _body()
        if not isinstance(data, dict):
            return _json({'error': 'invalid json'}, 400)
        try:
            server = request.env['resta.server'].sudo().browse(int(data.get('server_id') or 0)).exists()
        except (TypeError, ValueError):
            server = None
        if not server:
            return _json({'error': 'unknown server'}, 404)
        try:
            payload = server.register_from_agent(data.get('token'), data.get('internal_ip'))
        except UserError as exc:
            _logger.warning('registration refused for server %s: %s', server.id, exc)
            return _json({'error': str(exc)}, 403)
        return _json(payload)

    @http.route('/resta/agent/callback', type='http', auth='public', methods=['POST'], csrf=False,
                save_session=False)
    def agent_callback(self, **_kw):
        raw, data = _body()
        if not isinstance(data, dict):
            return _json({'error': 'invalid json'}, 400)
        try:
            server = request.env['resta.server'].sudo().browse(int(data.get('server_id') or 0)).exists()
        except (TypeError, ValueError):
            server = None
        if not server or not verify(server.agent_secret, request.httprequest.headers.get('X-Resta-Timestamp'),
                                    request.httprequest.headers.get('X-Resta-Signature'), raw):
            return _json({'error': 'invalid signature'}, 401)
        request.env['resta.operation'].sudo().handle_callback(server, data.get('job') or {})
        return _json({'ok': True})

    def _check_key(self):
        expected = request.env['resta.config'].sudo().get('signup_key')
        given = request.httprequest.headers.get('X-Resta-Key') or ''
        return bool(expected) and hmac.compare_digest(expected, given)

    @http.route('/resta/api/signup', type='http', auth='public', methods=['POST'], csrf=False,
                save_session=False)
    def api_signup(self, **_kw):
        if not self._check_key():
            return _json({'error': 'invalid key'}, 401)
        _raw, data = _body()
        if not isinstance(data, dict):
            return _json({'error': 'invalid json'}, 400)
        try:
            result = request.env['resta.project'].sudo().signup(data)
        except (ValidationError, UserError) as exc:
            request.env.cr.rollback()
            return _json({'error': str(exc)}, 400)
        return _json(result, 201)

    @http.route('/resta/api/project/<string:code>', type='http', auth='public', methods=['GET'], csrf=False,
                save_session=False)
    def api_project(self, code, **_kw):
        if not self._check_key():
            return _json({'error': 'invalid key'}, 401)
        project = request.env['resta.project'].sudo().search([('code', '=', code)], limit=1)
        if not project:
            return _json({'error': 'not found'}, 404)
        return _json({'code': project.code, 'state': project.state, 'url': project.url,
                      'trial_end': project.trial_end and str(project.trial_end)})
