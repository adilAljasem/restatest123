from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

RESTORE_MODES = [
    ('staging', 'استرجاع كنسخة تجربة للفحص (الإنتاج لا يتأثر)'),
    ('replace', 'استبدال قاعدة الإنتاج بالنسخة'),
]


class RestaRestoreWizard(models.TransientModel):
    _name = 'resta.restore.wizard'
    _description = 'استرجاع نسخة احتياطية'

    project_id = fields.Many2one('resta.project', 'المشروع', required=True, readonly=True)
    mode = fields.Selection(RESTORE_MODES, 'طريقة الاسترجاع', required=True, default='staging')
    snapshot = fields.Char('رقم النسخة', required=True, default='latest',
                           help='اكتب latest لآخر نسخة، أو رقم نسخة من القائمة')
    confirm_code = fields.Char('اكتب كود المشروع للتأكيد')
    available_html = fields.Html('النسخ المتاحة', compute='_compute_available', sanitize=False)

    @api.depends('project_id.snapshots_json')
    def _compute_available(self):
        for w in self:
            items = w.project_id.snapshots_json or []
            if not items:
                w.available_html = Markup('<p>%s</p>') % _('لا توجد قائمة بعد. اضغط «تحديث قائمة النسخ» في المشروع ثم ارجع هنا.')
                continue
            rows = Markup('').join(
                Markup('<tr><td><code>%s</code></td><td>%s</td><td>%s</td></tr>') % (
                    i.get('id', ''), (i.get('time') or '')[:16].replace('T', ' '),
                    ', '.join(t for t in i.get('tags', []) if not t.startswith('db:')))
                for i in reversed(items[-30:]))
            w.available_html = Markup(
                '<table class="table table-sm"><thead><tr><th>%s</th><th>%s</th><th>%s</th></tr></thead>'
                '<tbody>%s</tbody></table>') % (_('الرقم'), _('الوقت (UTC)'), _('النوع'), rows)

    def action_restore(self):
        self.ensure_one()
        p = self.project_id
        p._require_server()
        snapshot = (self.snapshot or '').strip()
        if not snapshot or not all(c.isalnum() for c in snapshot):
            raise UserError(_('رقم النسخة غير صحيح.'))
        domain = self.env['resta.config'].get('base_domain')
        if self.mode == 'staging':
            if p.staging_db:
                raise UserError(_('احذف نسخة التجربة الحالية أولاً.'))
            target = f'{p.db_name}-staging'
            if p.server_id.external_ip:
                self.env['resta.cloudflare'].upsert(target, p.server_id.external_ip)
            p._new_operation('restore', {
                'db': p.db_name, 'snapshot': snapshot, 'target': target, 'neutralize': True,
                'stack': 'staging' if p.project_type == 'custom' else 'main',
                'base_url': f'https://{target}.{domain}',
            })
            p.message_post(body=_('طُلب استرجاع النسخة %s كنسخة تجربة محيّدة.', escape(snapshot)))
        else:
            if p.state != 'active':
                raise UserError(_('الاستبدال متاح للأنظمة العاملة فقط. شغّل النظام أولاً.'))
            if (self.confirm_code or '').strip().lower() != p.code:
                raise UserError(_('اكتب كود المشروع (%s) للتأكيد.', p.code))
            p._new_operation('restore', {
                'db': p.db_name, 'snapshot': snapshot, 'target': p.db_name, 'swap': True, 'stack': 'main',
            })
            p.message_post(body=_('طُلب استبدال قاعدة الإنتاج بالنسخة %s. القاعدة الحالية ستُحفظ باسم جانبي.',
                                  escape(snapshot)))
        return {'type': 'ir.actions.act_window_close'}
