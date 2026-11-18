"""Configurable reference data of the growth journey: restaurant types, levels, features.

Every number here is a starting value that the support team can change from
Configuration without touching the code.
"""
from odoo import api, fields, models, tools
from odoo.exceptions import ValidationError


class RestaGrowthRestaurantType(models.Model):
    _name = "resta.growth.restaurant.type"
    _description = "نوع المطعم"
    _order = "sequence, id"

    name = fields.Char("النوع", required=True, translate=True)
    code = fields.Char(required=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    max_waste_pct = fields.Float("الحد الأعلى للهدر %", default=4.0,
                                 help="قيمة الإتلاف ÷ قيمة الاستهلاك. يُستخدم في شرط المستوى 2.")
    min_margin_pct = fields.Float("الحد الأدنى للهامش الإجمالي %", default=60.0,
                                  help="يُستخدم في شرط المستوى 3 (المرحلة 2).")
    avg_ticket = fields.Float("متوسط الفاتورة المعتاد (للمحاكي)", default=45.0)
    orders_per_day = fields.Integer("الطلبات اليومية المعتادة (للمحاكي)", default=120)

    _code_uniq = models.Constraint("UNIQUE(code)", "رمز النوع مستخدم.")


class RestaGrowthLevel(models.Model):
    _name = "resta.growth.level"
    _description = "مستوى رحلة النمو"
    _order = "sequence"

    sequence = fields.Integer("الرقم", required=True)
    code = fields.Char(required=True)
    name = fields.Char("المستوى", required=True, translate=True)
    subtitle = fields.Char("الوصف المختصر", translate=True)
    color = fields.Char("اللون", default="#0E7C66")
    icon = fields.Char("الأيقونة", default="fa-flag")
    reward = fields.Char("الجائزة", translate=True)
    reward_note = fields.Text("ملاحظة الجائزة", translate=True,
                              help="نص صادق يظهر تحت الجائزة (مثلاً: القرار النهائي للتمويل للشريك).")
    celebration = fields.Text("رسالة الاحتفال", translate=True,
                              help="{days} تُستبدل بعدد أيام البيانات الجاهزة.")
    learning = fields.Text("التعلم", translate=True)
    condition_ids = fields.One2many("resta.growth.condition", "level_id", "الشروط")
    feature_ids = fields.One2many("resta.growth.feature", "level_id", "الميزات التي تُفتح")

    _code_uniq = models.Constraint("UNIQUE(code)", "رمز المستوى مستخدم.")
    _seq_uniq = models.Constraint("UNIQUE(sequence)", "رقم المستوى مستخدم.")


class RestaGrowthFeature(models.Model):
    """A feature unlocked by a level. Locking is UI-only: menus + direct action links.

    Model access rights are never touched: the point of sale keeps creating stock
    moves and journal entries in the background while the menus are hidden.
    """
    _name = "resta.growth.feature"
    _description = "ميزة تُفتح في رحلة النمو"
    _order = "level_sequence, sequence, id"

    key = fields.Char(required=True)
    name = fields.Char("الميزة", required=True, translate=True)
    sequence = fields.Integer(default=10)
    level_id = fields.Many2one("resta.growth.level", "تُفتح في المستوى", required=True, ondelete="restrict")
    level_sequence = fields.Integer(related="level_id.sequence", store=True)
    menu_ids = fields.Many2many("ir.ui.menu", string="القوائم الجذرية المقفلة",
                                help="تُقفل هذه القوائم وكل ما تحتها، والإجراءات المرتبطة بها.")
    icon = fields.Char(default="fa-cubes")
    teaser = fields.Char("نص تشويقي", translate=True)
    entry_action_xmlid = fields.Char("الإجراء الذي يُفتح بعد الفتح")
    open_from_start = fields.Boolean(
        "مفتوحة من البداية",
        help="مفتوحة من أول يوم ولا تُقفل أبداً (مثل المحاسبة والدفاتر التي تحتاجها نقاط البيع). "
             "تظهر في رحلة النمو ضمن مستواها.")
    tour_name = fields.Char("جولة الميزة")
    unlocked = fields.Boolean("مفتوحة", readonly=True, copy=False)
    unlocked_date = fields.Datetime("تاريخ الفتح", readonly=True, copy=False)

    _key_uniq = models.Constraint("UNIQUE(key)", "مفتاح الميزة مستخدم.")

    # ------------------------------------------------------------------ locking
    @api.model
    @tools.ormcache()
    def _locked_ids(self):
        """(menu_ids, action refs) hidden for everybody except the support team."""
        features = self.sudo().with_context(active_test=False).search([("unlocked", "=", False)])
        if not features:
            return frozenset(), frozenset()
        Menu = self.env["ir.ui.menu"].sudo().with_context(active_test=False)
        roots = features.menu_ids
        menus = Menu.search([("id", "child_of", roots.ids)]) if roots else Menu
        actions = {(m.action._name, m.action.id) for m in menus if m.action}
        # A screen that an open menu also leads to stays open: product categories, attributes and units
        # (point of sale, purchase, sales), customers, taxes... are never locked by a feature's menu.
        open_menus = Menu.with_context(active_test=True).search([("id", "not in", menus.ids), ("action", "!=", False)])
        actions -= {(m.action._name, m.action.id) for m in open_menus}
        return frozenset(menus.ids), frozenset(actions)

    @api.model
    def _user_bypasses_lock(self):
        return self.env.su or self.env.user.has_group("resta_growth.group_resta_support")

    @api.model
    def _feature_for_action(self, action_model, action_id):
        """The locked feature that hides this action, if any (for the friendly message)."""
        for feature in self.sudo().search([("unlocked", "=", False)]):
            menus = self.env["ir.ui.menu"].sudo().search([("id", "child_of", feature.menu_ids.ids)])
            if any(m.action and m.action._name == action_model and m.action.id == action_id for m in menus):
                return feature
        return self.browse()

    def _unlock(self):
        to_open = self.filtered(lambda f: not f.unlocked)
        if to_open:
            to_open.sudo().write({"unlocked": True, "unlocked_date": fields.Datetime.now()})
            self.env.registry.clear_cache()
        return to_open

    def _relock_for_simulator(self):
        """Only the simulator may lock a feature again (real restaurants: never)."""
        if not self.env.context.get("resta_growth_simulator"):
            raise ValidationError("لا يمكن قفل ميزة بعد فتحها.")
        self.filtered(lambda f: not f.open_from_start).sudo().write({"unlocked": False, "unlocked_date": False})
        self.env.registry.clear_cache()

    @api.model
    def _open_from_start(self):
        """Features open from the first day (accounting and journals for the point of sale)."""
        return self.sudo().search([("open_from_start", "=", True), ("unlocked", "=", False)])._unlock()

    @api.model_create_multi
    def create(self, vals_list):
        features = super().create(vals_list)
        features.filtered("open_from_start")._unlock()
        return features

    def write(self, vals):
        if vals.get("unlocked") is False and not self.env.context.get("resta_growth_simulator") \
                and any(self.mapped("unlocked")):
            raise ValidationError("لا يمكن قفل ميزة بعد فتحها: قد يتعطل تشغيل المطعم.")
        if vals.get("unlocked") is False and any(self.mapped("open_from_start")):
            raise ValidationError("هذه الميزة مفتوحة من البداية ولا تُقفل.")
        res = super().write(vals)
        if vals.get("open_from_start"):
            self._unlock()
        if "menu_ids" in vals or "unlocked" in vals:
            self.env.registry.clear_cache()
        return res

    OPTIONAL_MENUS = {
        # other apps that belong to the same feature, linked only when installed
        "inventory": ["resta_barcode.menu_barcode_root", "stock_barcode.stock_barcode_menu",
                      "stock_request.menu_stock_request_root"],
        "accounting": ["accountant.menu_accounting"],
        # profitability analysis (Resta accounting): profit per branch, cash flow, budgets
        "financial_analysis": ["resta_account.menu_report_pl_branch", "resta_account.menu_report_cf",
                               "resta_account.menu_report_budget", "resta_account.menu_budgets"],
    }

    @api.model
    def _link_optional_menus(self):
        for key, xmlids in self.OPTIONAL_MENUS.items():
            feature = self.sudo().search([("key", "=", key)], limit=1)
            if not feature:
                continue
            for xmlid in xmlids:
                menu = self.env.ref(xmlid, raise_if_not_found=False)
                if menu and menu not in feature.menu_ids:
                    feature.menu_ids = [(4, menu.id)]
        self.env.registry.clear_cache()

    def ui_data(self):
        return [{
            "key": f.key, "name": f.name, "icon": f.icon, "teaser": f.teaser or "",
            "level": f.level_id.sequence, "level_name": f.level_id.name, "unlocked": f.unlocked,
            "open_from_start": f.open_from_start,
        } for f in self]
