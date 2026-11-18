"""What each restaurant may use, and the orders the platform sends to its system.

Restaurants never install apps themselves. Their system opens features through the growth
levels, or when the Resta team sends an order from here:
  - unlock features of the growth journey, or reach a level;
  - install an app included in the plan;
  - switch the journey to «paid in full» (everything open).
Orders are delivered at the next synchronisation, run in the restaurant system and reported back.

Waking a restaurant system («مزامنة الآن», a new order, a renewal) never waits on it:
  - after the platform has saved the change, a job trigger is written in the restaurant's
    database (same database server) and its cron workers are notified. No HTTP round trip,
    no loading of the restaurant's system in the platform's worker;
  - a restaurant on another server gets a short signed ping instead (5 s, answered at once).
The restaurant system then synchronises in the background (cron), runs the orders and reports.
"""
import hashlib
import hmac
import json
import logging
import time

import requests

import odoo
from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

GROWTH_PLANS = [("trial", "تجربة (المستويات)"), ("standard", "قياسي (المستويات)"),
                ("paid_full", "مدفوع بالكامل (كل الميزات مفتوحة)")]
COMMAND_KINDS = [
    ("unlock_features", "فتح ميزات"),
    ("achieve_level", "الوصول لمستوى"),
    ("install_modules", "تثبيت تطبيقات"),
    ("set_growth_plan", "خطة الرحلة"),
]
# growth journey catalogue (resta_growth): keys of the lockable features and the levels
GROWTH_FEATURES = [("accounting", "المحاسبة والدفاتر (مفتوحة من البداية)"), ("inventory", "إدارة المخزون"),
                   ("financial_analysis", "تحليل الربحية")]
GROWTH_LEVELS = [("1", "1 - التأسيس"), ("2", "2 - الانضباط"), ("3", "3 - الربحية"), ("4", "4 - النمو"),
                 ("5", "5 - التوسع")]
# apps the Resta team may add to a restaurant (technical name: label)
APP_CATALOG = [
    ("hr_attendance", "الحضور والانصراف"),
    ("hr_holidays", "الإجازات"),
    ("hr_expense", "النفقات"),
    ("pos_loyalty", "الولاء والعروض"),
    ("pos_online_payment_self_order", "الدفع الإلكتروني في الطلب الذاتي"),
    ("stock_landed_costs", "تكاليف الشحن على المخزون"),
    ("mrp_subcontracting", "التصنيع لدى الغير"),
    ("purchase_requisition", "اتفاقيات الشراء"),
    ("project", "المشاريع"),
    ("maintenance", "الصيانة"),
    ("survey", "الاستبيانات"),
]


class RestaPlan(models.Model):
    _inherit = "resta.plan"

    growth_plan = fields.Selection(GROWTH_PLANS, "رحلة النمو", default="standard", required=True,
                                   help="قياسي: الميزات تُفتح بالمستويات. مدفوع بالكامل: كل الميزات مفتوحة.")
    max_users = fields.Integer("أقصى عدد مستخدمين", default=0, help="0 = بلا حد")
    max_pos = fields.Integer("أقصى عدد نقاط بيع", default=0, help="0 = بلا حد")
    app_names = fields.Char("تطبيقات مشمولة", help="أسماء تقنية مفصولة بفواصل، تُثبّت تلقائياً عند الاشتراك. "
                                                    "مثال: hr_attendance, pos_loyalty")

    def _app_list(self):
        self.ensure_one()
        return [a.strip() for a in (self.app_names or "").replace("\n", ",").split(",") if a.strip()]


class RestaTenantCommand(models.Model):
    _name = "resta.tenant.command"
    _description = "أمر للمطعم"
    _order = "id desc"

    tenant_id = fields.Many2one("resta.tenant", "المطعم", required=True, ondelete="cascade", index=True)
    kind = fields.Selection(COMMAND_KINDS, "الأمر", required=True)
    payload = fields.Text("البيانات", default="{}")
    summary = fields.Char("الوصف")
    state = fields.Selection([("pending", "بانتظار المزامنة"), ("sent", "أُرسل"), ("done", "نُفّذ"),
                              ("failed", "فشل")], "الحالة", default="pending", required=True, index=True)
    result = fields.Text("النتيجة")
    sent_date = fields.Datetime("أُرسل في", readonly=True)
    done_date = fields.Datetime("نُفّذ في", readonly=True)

    def _payload_dict(self):
        try:
            return json.loads(self.payload or "{}")
        except ValueError:
            return {}

    def _to_tenant(self):
        return [{"id": c.id, "kind": c.kind, "payload": c._payload_dict()} for c in self]

    def action_retry(self):
        self.write({"state": "pending", "result": False})


class RestaTenant(models.Model):
    _inherit = "resta.tenant"

    command_ids = fields.One2many("resta.tenant.command", "tenant_id", "الأوامر")
    pending_commands = fields.Integer("أوامر معلقة", compute="_compute_pending_commands")
    growth_plan_override = fields.Selection(GROWTH_PLANS, "رحلة النمو (استثناء)",
                                            help="يتجاوز ما تحدده الباقة لهذا المطعم فقط.")
    max_users_override = fields.Integer("أقصى مستخدمين (استثناء)", help="0 = حسب الباقة")
    max_pos_override = fields.Integer("أقصى نقاط بيع (استثناء)", help="0 = حسب الباقة")

    @api.depends("command_ids.state")
    def _compute_pending_commands(self):
        for t in self:
            t.pending_commands = len(t.command_ids.filtered(lambda c: c.state in ("pending", "sent")))

    # ------------------------------------------------------------------ entitlements
    def _entitlements(self):
        """What the restaurant system enforces: journey plan and limits."""
        self.ensure_one()
        plan = self.plan_id if self.state == "active" else self.env["resta.plan"]
        growth = self.growth_plan_override or (plan.growth_plan if plan else "trial")
        branches = max(self.subscription_ids.filtered(lambda s: s.state == "active").mapped("branches") or [0])
        max_pos = self.max_pos_override or plan.max_pos or 0
        if plan and branches and plan.max_pos:
            max_pos = self.max_pos_override or max(plan.max_pos, branches)
        return {
            "growth_plan": growth,
            "max_users": self.max_users_override or plan.max_users or 0,
            "max_pos": max_pos,
            "apps": plan._app_list() if plan else [],
            "state": self.state,
        }

    def _queue(self, kind, payload, summary, wake=True):
        self.ensure_one()
        command = self.env["resta.tenant.command"].sudo().create({
            "tenant_id": self.id, "kind": kind, "payload": json.dumps(payload, ensure_ascii=False),
            "summary": summary})
        if wake:
            self._wake_after_commit()
        return command

    def _requested_apps(self):
        """Apps already asked for (waiting, sent or installed): a renewal does not ask again."""
        self.ensure_one()
        apps = set()
        for command in self.command_ids.filtered(lambda c: c.kind == "install_modules" and c.state != "failed"):
            apps.update(command._payload_dict().get("modules") or [])
        return apps

    # ------------------------------------------------------------------ wake the restaurant system
    def _wake_after_commit(self):
        """Ask these restaurant systems to synchronise once this transaction is saved
        (so they see the new orders and the new state). Never blocks the current request."""
        tenants = self.filtered(lambda t: t.db_name or t.url)
        if not tenants:
            return
        data = self.env.cr.postcommit.data
        pending = data.setdefault("resta_wake_tenants", set())
        if not pending:
            self.env.cr.postcommit.add(self._wake_pending)
        pending.update(tenants.ids)

    def _wake_pending(self):
        ids = self.env.cr.postcommit.data.pop("resta_wake_tenants", set())
        tenants = self.env["resta.tenant"].sudo().browse(sorted(ids)).exists()
        try:
            tenants._wake_now()
        except Exception:  # noqa: BLE001 - after commit: the next scheduled sync delivers anyway
            _logger.exception("Resta: could not wake restaurant systems %s", tenants.ids)

    def _wake_now(self):
        """Wake each restaurant system now. Returns {tenant id: "direct" | "ping" | "" (not reached)}."""
        result = {}
        local = set(self._pg_databases()) if any(self.mapped("db_name")) else set()
        for tenant in self:
            how = ""
            if tenant.db_name and tenant.db_name in local:
                how = "direct" if tenant._trigger_tenant_cron() else ""
            elif tenant.url:
                how = "ping" if tenant._ping_tenant() else ""
            result[tenant.id] = how
        return result

    def _tenant_cursor(self):
        return odoo.sql_db.db_connect(self.db_name).cursor()

    def _trigger_tenant_cron(self):
        """Same database server: add a trigger to the restaurant's sync job and notify the cron workers."""
        self.ensure_one()
        with self._tenant_cursor() as cr:
            cr.execute("""
                INSERT INTO ir_cron_trigger (cron_id, call_at, create_uid, write_uid, create_date, write_date)
                SELECT c.id, now() AT TIME ZONE 'UTC', 1, 1, now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC'
                  FROM ir_model_data d
                  JOIN ir_cron c ON c.id = d.res_id
                 WHERE d.model = 'ir.cron' AND d.module = 'resta_saas_tenant' AND d.name = 'ir_cron_resta_sync'
                   AND c.active
             RETURNING id
            """)
            created = bool(cr.fetchall())
        if created:
            self._notify_cron_workers(self.db_name)
        return created

    @api.model
    def _notify_cron_workers(self, dbname):
        with odoo.sql_db.db_connect("postgres").cursor() as cr:
            cr.execute("SELECT pg_notify('cron_trigger', %s)", [dbname])

    def _ping_tenant(self):
        """Restaurant system on another server: signed ping, answered at once (it syncs in the background)."""
        self.ensure_one()
        ts = str(int(time.time()))
        sig = hmac.new(self.sudo().token.encode(), ("%s:%s" % (self.account_number, ts)).encode(),
                       hashlib.sha256).hexdigest()
        try:
            resp = requests.post(self.url.rstrip("/") + "/resta/tenant/ping",
                                 json={"account": self.account_number, "ts": ts, "sig": sig}, timeout=5)
            resp.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001 - the scheduled sync delivers anyway
            _logger.info("Resta: restaurant %s not reachable now (%s)", self.account_number, exc)
            return False

    def _commands_for_sync(self):
        self.ensure_one()
        commands = self.command_ids.filtered(lambda c: c.state in ("pending", "sent")).sorted("id")
        commands.filtered(lambda c: c.state == "pending").write({"state": "sent", "sent_date": fields.Datetime.now()})
        return commands._to_tenant()

    def _record_acks(self, acks):
        self.ensure_one()
        Command = self.env["resta.tenant.command"].sudo()
        for ack in acks or []:
            command = Command.browse(int(ack.get("id") or 0)).exists()
            if not command or command.tenant_id != self:
                continue
            state = ack.get("state") if ack.get("state") in ("done", "failed") else "failed"
            command.write({"state": state, "result": (ack.get("result") or "")[:2000],
                           "done_date": fields.Datetime.now()})
            if state == "failed":
                self.message_post(body=_("فشل تنفيذ الأمر «%(c)s»: %(r)s", c=command.summary or command.kind,
                                         r=ack.get("result") or ""))

    # ------------------------------------------------------------------ actions
    def action_open_command_wizard(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "resta.tenant.command.wizard", "view_mode": "form",
                "target": "new", "name": _("إدارة الميزات"), "context": {"default_tenant_id": self.id}}

    def action_sync_now(self):
        """Ask the restaurant system to synchronise now. Returns at once: the restaurant system
        synchronises in the background and the orders' results appear here within a minute."""
        self._wake_after_commit()
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"type": "success", "message": _("طُلبت المزامنة. تظهر نتيجة الأوامر خلال دقيقة.")}}

    # the restaurant system sees the change at once (state, plan, limits, trial or subscription end)
    WAKE_FIELDS = {"state", "plan_id", "trial_end", "growth_plan_override", "max_users_override", "max_pos_override"}

    def write(self, vals):
        res = super().write(vals)
        if self.WAKE_FIELDS & set(vals):
            self._wake_after_commit()
        return res

    def _sync_response(self, last_id=0):
        res = super()._sync_response(last_id)
        res["entitlements"] = self._entitlements()
        res["commands"] = self._commands_for_sync()
        return res


class RestaSubscription(models.Model):
    _inherit = "resta.subscription"

    @api.model_create_multi
    def create(self, vals_list):
        subs = super().create(vals_list)
        for sub in subs.filtered(lambda s: s.state == "active"):
            tenant = sub.tenant_id
            # a renewal of the same plan asks for nothing: only apps not requested yet
            apps = [a for a in sub.plan_id._app_list() if a not in tenant._requested_apps()]
            if apps:
                tenant._queue("install_modules", {"modules": apps}, _("تطبيقات باقة %s", sub.plan_id.name))
            tenant._wake_after_commit()
        return subs


class RestaTenantCommandWizard(models.TransientModel):
    _name = "resta.tenant.command.wizard"
    _description = "إدارة ميزات المطعم"

    tenant_id = fields.Many2one("resta.tenant", "المطعم", required=True)
    kind = fields.Selection(COMMAND_KINDS, "الإجراء", required=True, default="unlock_features")
    features = fields.Selection(GROWTH_FEATURES, "الميزة")
    level = fields.Selection(GROWTH_LEVELS, "المستوى")
    app = fields.Selection(APP_CATALOG, "التطبيق")
    app_other = fields.Char("تطبيق آخر (اسم تقني)")
    growth_plan = fields.Selection(GROWTH_PLANS, "خطة الرحلة")
    sync_now = fields.Boolean("مزامنة الآن", default=True)

    def action_send(self):
        self.ensure_one()
        tenant = self.tenant_id
        if self.kind == "unlock_features":
            if not self.features:
                raise UserError(_("اختر الميزة."))
            label = dict(GROWTH_FEATURES)[self.features]
            tenant._queue("unlock_features", {"keys": [self.features]}, _("فتح: %s", label), wake=False)
        elif self.kind == "achieve_level":
            if not self.level:
                raise UserError(_("اختر المستوى."))
            tenant._queue("achieve_level", {"level": int(self.level)}, _("الوصول للمستوى %s", self.level), wake=False)
        elif self.kind == "install_modules":
            name = (self.app_other or self.app or "").strip()
            if not name:
                raise UserError(_("اختر التطبيق."))
            tenant._queue("install_modules", {"modules": [name]}, _("تثبيت: %s", dict(APP_CATALOG).get(name, name)), wake=False)
        else:
            if not self.growth_plan:
                raise UserError(_("اختر خطة الرحلة."))
            tenant.growth_plan_override = self.growth_plan
            tenant._queue("set_growth_plan", {"growth_plan": self.growth_plan},
                          _("خطة الرحلة: %s", dict(GROWTH_PLANS)[self.growth_plan]), wake=False)
        tenant.message_post(body=_("أمر جديد للمطعم: %s", tenant.command_ids[:1].summary))
        if self.sync_now:
            tenant._wake_after_commit()
        return {"type": "ir.actions.act_window_close"}
