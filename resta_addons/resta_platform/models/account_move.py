from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'

    resta_project_id = fields.Many2one('resta.project', 'مشروع ريستا', index=True, copy=False,
                                       ondelete='set null')

    def _invoice_paid_hook(self):
        """أول ما تنسدّ فاتورة مشروع موقوف بسبب السداد، يرجع يشتغل بدون انتظار الجدولة اليومية."""
        res = super()._invoice_paid_hook()
        projects = self.mapped('resta_project_id').filtered(
            lambda p: p.state == 'suspended' and p.suspend_reason == 'billing')
        if projects:
            projects._check_overdue()
        return res
