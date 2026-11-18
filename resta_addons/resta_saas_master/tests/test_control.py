import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.tests import HttpCase, TransactionCase, tagged


@tagged("post_install", "-at_install")
class TestTenantControl(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plan = cls.env["resta.plan"].create({
            "name": "النمو", "price_monthly": 499, "price_yearly": 4990, "max_branches": 2,
            "growth_plan": "paid_full", "max_users": 10, "max_pos": 3, "app_names": "hr_attendance, pos_loyalty"})
        cls.tenant = cls.env["resta.tenant"].create({
            "name": "مطعم الاختبار", "business_type": "dinein", "owner_name": "المالك",
            "owner_email": "owner@example.com", "owner_phone": "0500000000"})
        cls.tenant.write({"state": "trial"})

    def test_trial_entitlements(self):
        ent = self.tenant._entitlements()
        self.assertEqual(ent["growth_plan"], "trial")
        self.assertEqual(ent["max_users"], 0)

    def test_subscription_gives_plan_and_queues_apps(self):
        today = fields.Date.today()
        self.env["resta.subscription"].create({"tenant_id": self.tenant.id, "plan_id": self.plan.id, "branches": 2,
                                               "date_start": today, "date_end": today + timedelta(days=365)})
        self.tenant.write({"state": "active", "plan_id": self.plan.id})
        ent = self.tenant._entitlements()
        self.assertEqual((ent["growth_plan"], ent["max_users"], ent["max_pos"]), ("paid_full", 10, 3))
        command = self.tenant.command_ids.filtered(lambda c: c.kind == "install_modules")
        self.assertEqual(json.loads(command.payload)["modules"], ["hr_attendance", "pos_loyalty"])

    def test_orders_travel_with_the_sync(self):
        wizard = self.env["resta.tenant.command.wizard"].create({
            "tenant_id": self.tenant.id, "kind": "unlock_features", "features": "accounting", "sync_now": False})
        wizard.action_send()
        command = self.tenant.command_ids
        self.assertEqual(command.state, "pending")
        payload = self.tenant._sync_response(0)
        self.assertEqual(payload["commands"], [{"id": command.id, "kind": "unlock_features",
                                                "payload": {"keys": ["accounting"]}}])
        self.assertEqual(command.state, "sent")
        self.assertIn("entitlements", payload)
        self.tenant._record_acks([{"id": command.id, "state": "done", "result": "فُتح"}])
        self.assertEqual(command.state, "done")
        # another restaurant cannot report on this order
        other = self.env["resta.tenant"].create({"name": "آخر", "business_type": "qsr", "owner_name": "x",
                                                 "owner_email": "x@example.com"})
        command.write({"state": "sent"})
        other._record_acks([{"id": command.id, "state": "failed"}])
        self.assertEqual(command.state, "sent")

    def test_override_per_restaurant(self):
        self.tenant.write({"growth_plan_override": "standard", "max_users_override": 4})
        ent = self.tenant._entitlements()
        self.assertEqual((ent["growth_plan"], ent["max_users"]), ("standard", 4))

    # ------------------------------------------------------------ waking the restaurant system
    def test_sync_now_returns_at_once(self):
        """«مزامنة الآن» never waits on the restaurant: it only asks to be woken after the save."""
        self.tenant.write({"db_name": "r_test_tenant"})
        with patch("odoo.addons.resta_saas_master.models.resta_control.requests.post",
                   side_effect=AssertionError("no HTTP inside the request")):
            action = self.tenant.action_sync_now()
        self.assertEqual(action["tag"], "display_notification")
        self.assertIn(self.tenant.id, self.env.cr.postcommit.data.get("resta_wake_tenants", set()))

    def _fake_tenant_db(self):
        """The test database plays the restaurant's: its sync job and a cursor that is not committed."""
        cron = self.env["ir.cron"].create({
            "name": "Resta sync (test)", "model_id": self.env.ref("base.model_res_partner").id,
            "state": "code", "code": "pass", "interval_number": 30, "interval_type": "minutes"})
        self.env["ir.model.data"].create({"module": "resta_saas_tenant", "name": "ir_cron_resta_sync",
                                          "model": "ir.cron", "res_id": cron.id})
        cr = self.env.cr

        class Borrowed:
            def __enter__(self):
                return cr

            def __exit__(self, *exc):
                return False
        return cron, Borrowed

    def test_wake_same_server_adds_a_job_trigger(self):
        """Same database server: a trigger in the restaurant's database, no HTTP, no registry."""
        self.tenant.write({"db_name": "r_test_tenant"})
        cron, borrowed = self._fake_tenant_db()
        Tenant = type(self.tenant)
        with patch.object(Tenant, "_pg_databases", return_value={"r_test_tenant"}), \
                patch.object(Tenant, "_tenant_cursor", lambda rec: borrowed()), \
                patch.object(Tenant, "_notify_cron_workers") as notify, \
                patch("odoo.addons.resta_saas_master.models.resta_control.requests.post",
                      side_effect=AssertionError("no HTTP on the same server")):
            result = self.tenant._wake_now()
        self.assertEqual(result, {self.tenant.id: "direct"})
        self.assertTrue(self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)]))
        notify.assert_called_once_with("r_test_tenant")

    def test_wake_other_server_pings_briefly(self):
        """Restaurant system on another server: a signed ping with a short timeout."""
        self.tenant.write({"db_name": "r_elsewhere"})
        Tenant = type(self.tenant)
        with patch.object(Tenant, "_pg_databases", return_value=set()), \
                patch("odoo.addons.resta_saas_master.models.resta_control.requests.post") as post:
            post.return_value.raise_for_status.return_value = None
            result = self.tenant._wake_now()
        self.assertEqual(result, {self.tenant.id: "ping"})
        self.assertTrue(post.call_args.args[0].endswith("/resta/tenant/ping"))
        self.assertEqual(post.call_args.kwargs["timeout"], 5)
        self.assertEqual(post.call_args.kwargs["json"]["account"], self.tenant.account_number)
        self.assertTrue(post.call_args.kwargs["json"]["sig"])
        # not reachable: no error for the account manager, the scheduled sync delivers later
        with patch.object(Tenant, "_pg_databases", return_value=set()), \
                patch("odoo.addons.resta_saas_master.models.resta_control.requests.post",
                      side_effect=OSError("timeout")):
            self.assertEqual(self.tenant._wake_now(), {self.tenant.id: ""})

    def test_renewal_wakes_the_restaurant_without_asking_again_for_apps(self):
        today = fields.Date.today()
        Sub = self.env["resta.subscription"]
        vals = {"tenant_id": self.tenant.id, "plan_id": self.plan.id, "date_start": today,
                "date_end": today + timedelta(days=365)}
        Sub.create(vals)
        Sub.create(dict(vals, date_start=today + timedelta(days=366), date_end=today + timedelta(days=730)))
        installs = self.tenant.command_ids.filtered(lambda c: c.kind == "install_modules")
        self.assertEqual(len(installs), 1, "a renewal of the same plan installs nothing again")
        self.plan.app_names = "hr_attendance, pos_loyalty, hr_holidays"
        Sub.create(dict(vals, date_start=today + timedelta(days=731), date_end=today + timedelta(days=1095)))
        installs = self.tenant.command_ids.filtered(lambda c: c.kind == "install_modules").sorted("id")
        self.assertEqual(json.loads(installs[-1].payload)["modules"], ["hr_holidays"])
        self.assertIn(self.tenant.id, self.env.cr.postcommit.data.get("resta_wake_tenants", set()))

    def test_extending_the_trial_wakes_the_restaurant(self):
        self.tenant.write({"db_name": "r_test_tenant"})
        self.env.cr.postcommit.data.pop("resta_wake_tenants", None)
        self.tenant.action_extend_trial()
        self.assertIn(self.tenant.id, self.env.cr.postcommit.data.get("resta_wake_tenants", set()))


@tagged("post_install", "-at_install")
class TestUnifiedSignInPage(HttpCase):

    def test_sign_in_page(self):
        html = self.url_open("/resta/login?account=100245").text
        self.assertIn('name="account"', html)
        self.assertIn('value="100245"', html)
        self.assertIn('name="login"', html)
        self.assertIn("اسم المستخدم", html)
        self.assertIn("rs_auth", html)
        self.assertNotIn("www.odoo.com", html)

    def test_short_link(self):
        resp = self.url_open("/login?account=100245", allow_redirects=False)
        self.assertIn(resp.status_code, (301, 302, 303))
        self.assertTrue(resp.headers["Location"].endswith("/resta/login?account=100245"))
