"""الاتصال بوكيل السيرفر (agent) عبر الشبكة الداخلية بطلبات موقّعة."""
import json
import logging

import requests

from odoo import api, models

from .signing import signed_headers

_logger = logging.getLogger(__name__)


class AgentError(Exception):
    pass


class RestaAgent(models.AbstractModel):
    _name = 'resta.agent'
    _description = 'وكيل السيرفر'

    @api.model
    def call(self, server, method, path, payload=None, timeout=20):
        server = server.sudo()
        if not server.internal_ip or not server.agent_secret:
            raise AgentError('server is not registered yet')
        body = json.dumps(payload).encode() if payload is not None else b''
        url = f'http://{server.internal_ip}:{server.agent_port}/v1{path}'
        try:
            resp = requests.request(method, url, data=body, timeout=timeout,
                                    headers=signed_headers(server.agent_secret, body))
        except requests.RequestException as exc:
            raise AgentError(f'agent unreachable: {exc}') from exc
        if resp.status_code >= 400:
            raise AgentError(f'agent error {resp.status_code}: {resp.text[:400]}')
        return resp.json()

    @api.model
    def health(self, server):
        return self.call(server, 'GET', '/health', timeout=30)

    @api.model
    def submit(self, operation):
        return self.call(operation.server_id, 'POST', '/jobs', {
            'operation_id': operation.name,
            'op': operation.op_type,
            'params': json.loads(operation.payload or '{}'),
        })

    @api.model
    def job(self, operation):
        return self.call(operation.server_id, 'GET', f'/jobs/{operation.job_id or operation.name}')
