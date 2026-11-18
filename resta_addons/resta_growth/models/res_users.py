import secrets

from odoo import SUPERUSER_ID, Command, api, fields, models

# Business rights of a restaurant owner. Deliberately NOT base.group_system.
OWNER_GROUPS = [
    "base.group_user",
    "base.group_erp_manager",          # users and access rights
    "base.group_partner_manager",
    "resta_growth.group_resta_owner",
    "point_of_sale.group_pos_manager",
    "stock.group_stock_manager",
    "account.group_account_manager",
    "purchase.group_purchase_manager",
    "sales_team.group_sale_manager",
    "mrp.group_mrp_manager",
    "hr.group_hr_manager",
]
SUPPORT_LOGIN = "resta-support"
# Kept for the Resta team (support account): technical administration only.
TECHNICAL_GROUPS = [
    "base.group_system",
    "base.group_no_one",
    "base.group_sanitize_override",
    "resta_growth.group_resta_support",
    "base.group_portal",
    "base.group_public",
]


class ResUsers(models.Model):
    _inherit = "res.users"

    @api.model
    def _resta_technical_group_ids(self):
        refs = (self.env.ref(xmlid, raise_if_not_found=False) for xmlid in TECHNICAL_GROUPS)
        return {group.id for group in refs if group}

    @api.model
    def _resta_owner_group_ids(self):
        """Every business right of the installed apps, never the technical administration.

        Besides the listed groups, the owner gets each right the administrator account holds
        (create products, leaves, attendance, payroll, expenses, dashboards, export...): a right
        missing here is a screen or a button the restaurant cannot use.
        """
        ids = set()
        for xmlid in OWNER_GROUPS:
            group = self.env.ref(xmlid, raise_if_not_found=False)
            if group:
                ids.add(group.id)
        admin = self.env.ref("base.user_admin", raise_if_not_found=False)
        if admin:
            ids |= set(admin.sudo().all_group_ids.ids)
        return sorted(ids - self._resta_technical_group_ids())

    @api.model
    def _resta_sync_owner_groups(self):
        """Owners get the rights of apps added after provisioning (and any right they were missing)."""
        owner_group = self.env.ref("resta_growth.group_resta_owner", raise_if_not_found=False)
        if not owner_group:
            return self.browse()
        technical = self._resta_technical_group_ids()
        wanted = set(self._resta_owner_group_ids())
        updated = self.browse()
        for owner in owner_group.sudo().user_ids.filtered(lambda u: u.active and not u.share):
            if technical & set(owner.group_ids.ids):
                continue  # a technical administrator (legacy owner): already has everything
            missing = wanted - set(owner.all_group_ids.ids)
            if missing:
                owner.sudo().write({"group_ids": [Command.link(gid) for gid in sorted(missing)]})
                updated |= owner
        return updated

    @api.model
    def _resta_growth_setup_owner(self, business_type=None):
        """Called by the platform when a restaurant database is created from a template.

        The template's administrator (already renamed to the owner, with the signup
        password) is split in two:
          * a new owner user with business rights, without technical administration;
          * the administrator, renamed «دعم ريستا», kept for the Basic Solution team.
        """
        env = self.env
        admin = env.ref("base.user_admin")
        if admin.login == SUPPORT_LOGIN:
            return False  # already done
        env.cr.execute("SELECT password FROM res_users WHERE id = %s", [admin.id])
        pw_hash = (env.cr.fetchone() or [None])[0]
        owner_vals = {
            "name": admin.name, "login": admin.login, "email": admin.email,
            "lang": admin.lang, "tz": admin.tz or "Asia/Riyadh",
            "company_id": admin.company_id.id, "company_ids": [Command.set(admin.company_ids.ids)],
        }
        phone = admin.partner_id.phone
        admin.write({"login": SUPPORT_LOGIN, "name": "دعم ريستا", "email": False})
        admin.partner_id.phone = False
        # free the login in the database before the owner takes it (unique login)
        admin.flush_recordset(["login"])
        owner = self.with_context(no_reset_password=True).create(owner_vals)
        owner.write({"group_ids": [Command.set(self._resta_owner_group_ids())]})
        owner.partner_id.phone = phone
        if pw_hash:
            owner._set_encrypted_password(owner.id, pw_hash)
        admin._change_password(secrets.token_urlsafe(24))
        support = env.ref("resta_growth.group_resta_support")
        admin.write({"group_ids": [Command.link(support.id)]})
        env["resta.growth.profile"]._reset_for_new_tenant(business_type)
        return owner.id


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    resta_growth_w_sales = fields.Integer("وزن انتظام المبيعات", default=300,
                                          config_parameter="resta_growth.w_sales",
                                          groups="resta_growth.group_resta_support")
    resta_growth_w_inventory = fields.Integer("وزن دقة الجرد", default=300,
                                              config_parameter="resta_growth.w_inventory",
                                              groups="resta_growth.group_resta_support")
    resta_growth_w_invoices = fields.Integer("وزن سلامة الفواتير", default=200,
                                             config_parameter="resta_growth.w_invoices",
                                             groups="resta_growth.group_resta_support")
    resta_growth_w_margin = fields.Integer("وزن الهامش والنمو", default=200,
                                           config_parameter="resta_growth.w_margin",
                                           groups="resta_growth.group_resta_support")
    resta_growth_band_1 = fields.Integer("بداية «في الطريق»", default=400,
                                         config_parameter="resta_growth.band_1",
                                         groups="resta_growth.group_resta_support")
    resta_growth_band_2 = fields.Integer("بداية «منضبط»", default=600,
                                         config_parameter="resta_growth.band_2",
                                         groups="resta_growth.group_resta_support")
    resta_growth_band_3 = fields.Integer("بداية «جاهز لفرع ثانٍ»", default=800,
                                         config_parameter="resta_growth.band_3",
                                         groups="resta_growth.group_resta_support")
    resta_growth_quality_threshold = fields.Integer("حد الشهر المجاني (جودة الإدخالات)", default=85,
                                                    config_parameter="resta_growth.quality_threshold",
                                                    groups="resta_growth.group_resta_support")
    resta_growth_forming_days = fields.Integer("أيام تكوين المؤشر", default=14,
                                               config_parameter="resta_growth.forming_days",
                                               groups="resta_growth.group_resta_support")
    resta_growth_simulator_enabled = fields.Boolean("تفعيل المحاكي (ليس لقاعدة إنتاج)",
                                                    config_parameter="resta_growth.simulator_enabled",
                                                    groups="resta_growth.group_resta_support")

    def execute(self):
        """The owner is not a technical administrator but may save the business settings."""
        user = self.env.user
        if not self.env.su and not user.has_group("base.group_system") \
                and user.has_group("resta_growth.group_resta_owner"):
            self.check_access("write")
            if hasattr(self, "_resta_keep_modules_as_installed"):
                # apps are added by the Resta platform only (resta_saas_tenant)
                self._resta_keep_modules_as_installed()
            # some set_values() overrides drop sudo(); run as the superuser instead
            return super(ResConfigSettings, self.with_user(SUPERUSER_ID)).execute()
        return super().execute()
