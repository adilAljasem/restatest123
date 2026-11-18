"""The platform asks this restaurant system to synchronise now (after an order or a plan change).

Signed like the sign-in: HMAC of (account, timestamp) with this restaurant's token.
Answered at once: the synchronisation runs in the background (cron), never inside this request
(it calls the platform back, and the platform may be waiting for this answer).
On the same database server the platform does not even call this: it triggers the job directly.
"""
import hashlib
import hmac
import json
import time

from odoo import http
from odoo.http import request

MAX_AGE = 300


class RestaPing(http.Controller):

    @http.route("/resta/tenant/ping", type="http", auth="none", methods=["POST"], csrf=False,
                save_session=False, sitemap=False, readonly=False)
    def ping(self, **kw):
        try:
            data = json.loads(request.httprequest.get_data() or b"{}")
        except ValueError:
            data = {}
        icp = request.env["ir.config_parameter"].sudo()
        token = icp.get_param("resta.tenant_token") or ""
        account = icp.get_param("resta.account_number") or ""
        ts, sig = str(data.get("ts") or ""), str(data.get("sig") or "")
        try:
            fresh = abs(time.time() - int(ts)) <= MAX_AGE
        except ValueError:
            fresh = False
        expected = hmac.new(token.encode(), ("%s:%s" % (account, ts)).encode(), hashlib.sha256).hexdigest()
        if not (token and data.get("account") == account and fresh and hmac.compare_digest(expected, sig)):
            return request.make_json_response({"error": "unauthorized"}, status=401)
        request.env["resta.saas.client"].sudo()._request_sync()
        return request.make_json_response({"ok": True, "queued": True})
