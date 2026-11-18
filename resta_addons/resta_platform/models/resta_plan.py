from odoo import api, fields, models

EDITIONS = [('dinein', 'مطاعم الصالة'), ('fastfood', 'وجبات سريعة ومقاهي')]
PROJECT_TYPES = [('saas', 'ساس'), ('custom', 'كاستم')]


class RestaPlan(models.Model):
    _name = 'resta.plan'
    _description = 'باقة'
    _order = 'project_type, sequence, id'

    name = fields.Char('الباقة', required=True, translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    project_type = fields.Selection(PROJECT_TYPES, 'النوع', required=True, default='saas')
    edition = fields.Selection(EDITIONS, 'النسخة', help='للساس: أي قالب تُنشأ منه قاعدة المطعم')
    is_default = fields.Boolean('افتراضية للتسجيل الذاتي')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    product_id = fields.Many2one('product.product', 'منتج الفاتورة', domain=[('type', '=', 'service')],
                                 help='يحدد الحساب والضريبة في الفاتورة')
    base_price = fields.Monetary('السعر الشهري الأساسي', currency_field='currency_id')
    included_branches = fields.Integer('فروع مشمولة', default=1)
    price_per_branch = fields.Monetary('سعر الفرع الإضافي', currency_field='currency_id')
    included_storage_gb = fields.Float('مساحة مشمولة (GB)', default=10)
    price_per_gb = fields.Monetary('سعر الـ GB الإضافي', currency_field='currency_id')
    machine_type = fields.Char('نوع السيرفر الخاص', default='e2-standard-2',
                               help='للكاستم: حجم السيرفر الخاص في Google Cloud')
    disk_gb = fields.Integer('قرص السيرفر الخاص (GB)', default=100)
    project_count = fields.Integer(compute='_compute_project_count')

    def _compute_project_count(self):
        data = self.env['resta.project']._read_group([('plan_id', 'in', self.ids)], ['plan_id'], ['__count'])
        counts = {plan.id: count for plan, count in data}
        for plan in self:
            plan.project_count = counts.get(plan.id, 0)

    def invoice_lines(self, project):
        """بنود الفاتورة الشهرية للمشروع حسب الباقة والاستهلاك الفعلي."""
        self.ensure_one()
        base = {'product_id': self.product_id.id} if self.product_id else {}
        period = project._billing_period_label()
        lines = [dict(base, name=f'{self.name} — {project.name} ({period})', quantity=1,
                      price_unit=self.base_price)]
        extra_branches = max(0, (project.branches or 0) - self.included_branches)
        if extra_branches and self.price_per_branch:
            lines.append(dict(base, name=f'فروع إضافية — {project.name}', quantity=extra_branches,
                              price_unit=self.price_per_branch))
        used_gb = (project.db_size_mb + project.filestore_mb) / 1024.0
        extra_gb = round(max(0.0, used_gb - self.included_storage_gb), 2)
        if extra_gb and self.price_per_gb:
            lines.append(dict(base, name=f'مساحة إضافية (GB) — {project.name}', quantity=extra_gb,
                              price_unit=self.price_per_gb))
        return lines

    def monthly_amount(self, project):
        return sum(line['quantity'] * line['price_unit'] for line in self.invoice_lines(project))
