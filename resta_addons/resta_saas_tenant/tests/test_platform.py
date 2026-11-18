import hashlib
import hmac
import json
import re
import time
from unittest.mock import patch

from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.resta_saas_tenant.models.resta_client import RestaSaasClient


class LinkedCase(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        icp = cls.env["ir.config_parameter"].sudo()
        for key, value in {"resta.account_number": "100777", "resta.tenant_token": "secret-token",
                           "resta.master_url": "https://platform.example", "resta.subscription_state": "active",
                           "resta.account_manager": json.dumps({"name": "سارة", "phone": "0500000000"})}.items():
            icp.set_param(key, value)
        cls.owner = new_test_user(cls.env, login="tenant_owner", name="صاحب المطعم",
                                  groups="base.group_user,base.group_erp_manager,base.group_partner_manager,"
                                         "point_of_sale.group_pos_manager")
        cls.Command = cls.env["resta.platform.command"].sudo()

    def set_entitlements(self, **vals):
        self.env["ir.config_parameter"].sudo().set_param("resta.entitlements", json.dumps(vals))


@tagged("post_install", "-at_install")
class TestTenantLockdown(LinkedCase):

    def test_owner_cannot_install_or_remove_apps(self):
        module = self.env["ir.module.module"].search([("state", "=", "uninstalled")], limit=1)
        with self.assertRaises(AccessError):
            module.with_user(self.owner).button_immediate_install()
        installed = self.env["ir.module.module"].search([("name", "=", "resta_saas_tenant")])
        with self.assertRaises(AccessError):
            installed.with_user(self.owner).button_immediate_uninstall()

    def test_owner_cannot_open_the_apps_catalogue(self):
        apps = self.env.ref("base.open_module_tree")
        with self.assertRaises(AccessError):
            apps.with_user(self.owner).sudo()._get_action_dict()
        # the technical administrator (Resta support) still can
        self.assertEqual(apps.with_user(self.env.ref("base.user_admin")).sudo()._get_action_dict()["res_model"],
                         "ir.module.module")
        # other screens are not affected
        self.assertTrue(self.env.ref("base.action_partner_form").with_user(self.owner).sudo()._get_action_dict())

    def test_settings_hide_app_switches_from_the_owner(self):
        arch, _view = self.env["res.config.settings"]._get_view(view_type="form")
        for setting in arch.iter("setting"):
            if any((f.get("name") or "").startswith("module_") for f in setting.iter("field")):
                self.assertTrue(setting.get("invisible") == "1" or setting.get("groups") == "base.group_system",
                                setting.get("id"))

    def test_owner_cannot_grant_technical_admin(self):
        other = new_test_user(self.env, login="tenant_cashier", groups="base.group_user")
        system = self.env.ref("base.group_system")
        with self.assertRaises(AccessError):
            other.with_user(self.owner).write({"group_ids": [(4, system.id)]})
        with self.assertRaises(AccessError):
            system.with_user(self.owner).write({"user_ids": [(4, other.id)]})
        # the support account (technical admin) may
        other.write({"group_ids": [(4, system.id)]})
        self.assertTrue(other.has_group("base.group_system"))

    def test_user_limit_of_the_plan(self):
        Users = self.env["res.users"]
        current = Users.search_count([("share", "=", False), ("active", "=", True),
                                      ("id", "not in", Users._resta_support_user_ids())])
        self.set_entitlements(max_users=current)
        with self.assertRaises(UserError):
            self.env["res.users"].with_user(self.owner).create({"name": "مستخدم زائد", "login": "extra_user"})
        self.set_entitlements(max_users=0)
        self.env["res.users"].with_user(self.owner).create({"name": "مستخدم", "login": "extra_user2"})

    def test_pos_limit_of_the_plan(self):
        self.set_entitlements(max_pos=self.env["pos.config"].search_count([]))
        with self.assertRaises(UserError):
            self.env["pos.config"].with_user(self.owner).create({"name": "فرع زائد"})

    def test_lock_screen_state(self):
        Client = self.env["resta.saas.client"]
        self.assertFalse(Client.with_user(self.owner).get_lock_state()["blocked"])
        self.env["ir.config_parameter"].sudo().set_param("resta.subscription_state", "expired")
        state = Client.with_user(self.owner).get_lock_state()
        self.assertTrue(state["blocked"])
        self.assertEqual(state["manager"]["name"], "سارة")
        self.assertIn("100777", state["renew_url"])
        self.assertFalse(Client.get_lock_state()["blocked"], "the support account keeps working")


@tagged("post_install", "-at_install")
class TestPlatformOrders(LinkedCase):

    def test_receive_run_and_report(self):
        self.Command._receive([{"id": 11, "kind": "install_modules", "payload": {"modules": ["base", "no_such_app"]}},
                               {"id": 12, "kind": "unknown_order", "payload": {}}])
        self.Command._receive([{"id": 11, "kind": "install_modules", "payload": {}}])  # sent twice: kept once
        self.assertEqual(self.Command.search_count([("remote_id", "in", (11, 12))]), 2)
        with patch.object(RestaSaasClient, "sync", return_value=True):
            self.Command._cron_run()
        done = self.Command.search([("remote_id", "=", 11)])
        self.assertEqual(done.state, "done")
        self.assertIn("no_such_app", done.result)
        self.assertEqual(self.Command.search([("remote_id", "=", 12)]).state, "failed")
        _records, acks = self.Command._acks()
        self.assertEqual({a["id"]: a["state"] for a in acks}, {11: "done", 12: "failed"})

    def test_sync_applies_entitlements_and_orders(self):
        calls = []

        def fake_post(self_, path, payload, timeout=8):
            calls.append(payload)
            return {"state": "active", "days_left": 200, "plan": "النمو", "announcements": [],
                    "entitlements": {"growth_plan": "standard", "max_users": 7, "max_pos": 3},
                    "commands": [{"id": 21, "kind": "install_modules", "payload": {"modules": ["base"]}}]}

        with patch.object(RestaSaasClient, "_post", fake_post):
            self.env["resta.saas.client"].sync()
            ent = json.loads(self.env["ir.config_parameter"].sudo().get_param("resta.entitlements"))
            self.assertEqual(ent["max_users"], 7)
            self.assertEqual(self.Command.search([("remote_id", "=", 21)]).state, "pending")
            self.Command.search([("remote_id", "=", 21)]).write({"state": "done", "result": "ok"})
            self.env["resta.saas.client"].sync()
        self.assertEqual(calls[-1]["acks"], [{"id": 21, "state": "done", "result": "ok"}])
        self.assertTrue(self.Command.search([("remote_id", "=", 21)]).acked)


@tagged("post_install", "-at_install")
class TestPing(HttpCase):

    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("resta.account_number", "100778")
        icp.set_param("resta.tenant_token", "ping-token")

    def _post(self, sig_token="ping-token"):
        ts = str(int(time.time()))
        sig = hmac.new(sig_token.encode(), ("100778:%s" % ts).encode(), hashlib.sha256).hexdigest()
        return self.url_open("/resta/tenant/ping", data=json.dumps({"account": "100778", "ts": ts, "sig": sig}),
                             headers={"Content-Type": "application/json"})

    def test_ping_signature(self):
        with patch.object(RestaSaasClient, "sync", side_effect=AssertionError("never inside the request")):
            self.assertEqual(self._post("wrong").status_code, 401)
            resp = self._post()
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["ok"])
            self.assertTrue(resp.json()["queued"])
        # the synchronisation runs in the background job
        cron = self.env.ref("resta_saas_tenant.ir_cron_resta_sync")
        self.assertTrue(self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)]))


@tagged("post_install", "-at_install")
class TestUnifiedSignIn(HttpCase):
    """Sign-in like Foodics: account number + username + password."""

    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("resta.account_number", "100779")
        icp.set_param("resta.master_url", "https://platform.example")
        icp.set_param("resta.subscription_state", "active")
        self.user = new_test_user(self.env, login="cashier.one", password="cashier-pass-1", name="كاشير")

    def _login(self, account, login="cashier.one", password="cashier-pass-1"):
        page = self.url_open("/web/login")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        return self.url_open("/web/login", data={"csrf_token": csrf, "account": account, "login": login,
                                                 "password": password}, allow_redirects=False)

    def test_page_asks_for_account_number_and_username(self):
        html = self.url_open("/web/login").text
        self.assertIn('name="account"', html)
        self.assertIn('value="100779"', html)
        self.assertIn("اسم المستخدم", html)
        self.assertIn("rs_auth", html)
        self.assertNotIn("www.odoo.com", html)
        self.assertNotIn("/web/signup", html)

    def test_sign_in_with_own_account_number(self):
        resp = self._login("100779")
        self.assertEqual(resp.status_code, 303)
        self.assertNotIn("/web/login", resp.headers["Location"])

    def test_other_account_number_goes_to_the_platform(self):
        resp = self._login("100800")
        self.assertEqual(resp.status_code, 303)
        self.assertTrue(resp.headers["Location"].startswith("https://platform.example/resta/login?account=100800"))

    def test_wrong_password_stays_here(self):
        resp = self._login("100779", password="wrong")
        self.assertEqual(resp.status_code, 200)
        self.assertIn('name="account"', resp.text)


@tagged("post_install", "-at_install")
class TestSyncNeverBlocks(TransactionCase):
    """What used to hold the server: no call to the platform inside a request, no heavy work for nothing."""

    def setUp(self):
        super().setUp()
        icp = self.env["ir.config_parameter"].sudo()
        icp.set_param("resta.account_number", "100781")
        icp.set_param("resta.tenant_token", "tok")
        icp.set_param("resta.master_url", "https://platform.example")
        self.Command = self.env["resta.platform.command"].sudo()
        self.Command.search([("state", "=", "pending")]).write({"state": "done"})

    def test_panel_does_not_wait_for_the_platform(self):
        self.env["ir.config_parameter"].sudo().set_param("resta.last_sync", "2020-01-01 00:00:00")
        cron = self.env.ref("resta_saas_tenant.ir_cron_resta_sync")
        before = self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)])
        with patch.object(RestaSaasClient, "_post", side_effect=AssertionError("no HTTP in a user request")):
            data = self.env["resta.saas.client"].get_panel_data()
        self.assertTrue(data["linked"])
        self.assertEqual(self.env["ir.cron.trigger"].search_count([("cron_id", "=", cron.id)]), before + 1)

    def test_orders_job_without_orders_stays_quiet(self):
        with patch.object(RestaSaasClient, "sync", side_effect=AssertionError("nothing to report")):
            self.Command._cron_run()

    def test_installed_apps_do_not_reload_anything(self):
        Module = type(self.env["ir.module.module"])
        with patch.object(Module, "update_list", side_effect=AssertionError("no apps list scan")), \
                patch.object(Module, "button_immediate_install", side_effect=AssertionError("no install")):
            result = self.Command._run_install_modules({"modules": ["base", "web"]})
        self.assertEqual(result, "مثبتة مسبقاً")

    def test_unknown_app_reads_the_apps_list_once(self):
        Module = type(self.env["ir.module.module"])
        with patch.object(Module, "update_list", return_value=(0, 0)) as update_list, \
                patch.object(Module, "button_immediate_install", side_effect=AssertionError("no install")):
            result = self.Command._run_install_modules({"modules": ["base", "resta_app_that_does_not_exist"]})
        update_list.assert_called_once()
        self.assertIn("resta_app_that_does_not_exist", result)
