"""العمليات: كل أمر للسيرفرات يُحفظ هنا أولاً ثم يُرسل (نفس فكرة Outbox في RestaSH).

- لو السيرفر ما رد، تُعاد المحاولة تلقائياً بفواصل متزايدة.
- نفس رقم العملية يُرسل كل مرة، والوكيل ما ينفذها مرتين.
- النتيجة تصل بإشعار موقّع من الوكيل، ولو ما وصل تسأل المنصة عنها بنفسها.
"""
import json
import logging
import uuid
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .agent_client import AgentError

_logger = logging.getLogger(__name__)

OP_TYPES = [
    ('create_db', 'إنشاء قاعدة'),
    ('build_template', 'بناء قالب'),
    ('suspend', 'إيقاف'),
    ('resume', 'تشغيل'),
    ('backup', 'نسخة احتياطية'),
    ('backup_all', 'نسخ ليلي لكل القواعد'),
    ('restore', 'استرجاع'),
    ('snapshots', 'قائمة النسخ'),
    ('duplicate', 'نسخة تجربة'),
    ('drop', 'حذف قاعدة'),
    ('deploy', 'تحديث الكود'),
    ('create_stack', 'إنشاء طبقة أودو'),
]
OP_STATES = [
    ('pending', 'بالانتظار'),
    ('running', 'قيد التنفيذ'),
    ('done', 'تمت'),
    ('failed', 'فشلت'),
    ('cancelled', 'ملغاة'),
]
RETRY_MINUTES = [1, 2, 5, 10, 30, 60]


class RestaOperation(models.Model):
    _name = 'resta.operation'
    _description = 'عملية على سيرفر'
    _order = 'id desc'

    name = fields.Char('الرقم', required=True, readonly=True, copy=False,
                       default=lambda self: self._next_name())
    op_type = fields.Selection(OP_TYPES, 'العملية', required=True)
    state = fields.Selection(OP_STATES, 'الحالة', default='pending', required=True, index=True)
    server_id = fields.Many2one('resta.server', 'السيرفر', required=True, ondelete='cascade', index=True)
    project_id = fields.Many2one('resta.project', 'المشروع', ondelete='set null', index=True)
    depends_on_id = fields.Many2one('resta.operation', 'تنتظر العملية')
    payload = fields.Text('المعطيات', default='{}')
    result = fields.Text('النتيجة', readonly=True)
    error = fields.Text('الخطأ', readonly=True)
    job_id = fields.Char('رقم المهمة في الوكيل', readonly=True)
    attempts = fields.Integer('محاولات الإرسال', readonly=True)
    next_try = fields.Datetime('المحاولة القادمة', default=fields.Datetime.now)
    date_sent = fields.Datetime('أُرسلت', readonly=True)
    date_done = fields.Datetime('انتهت', readonly=True)
    duration = fields.Float('المدة (دقائق)', compute='_compute_duration')
    requested_by = fields.Many2one('res.users', 'بطلب من', default=lambda self: self.env.user, readonly=True)

    _name_unique = models.Constraint('unique(name)', 'رقم العملية مكرر.')

    @api.model
    def _next_name(self):
        name = self.env['ir.sequence'].sudo().with_company(False).next_by_code('resta.operation')
        return name or 'OP-' + uuid.uuid4().hex[:12].upper()

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if 'payload_dict' in vals:
                vals['payload'] = json.dumps(vals.pop('payload_dict'), ensure_ascii=False)
        return super().create(vals_list)

    @api.depends('date_sent', 'date_done')
    def _compute_duration(self):
        for op in self:
            if op.date_sent and op.date_done:
                op.duration = round((op.date_done - op.date_sent).total_seconds() / 60.0, 1)
            else:
                op.duration = 0.0

    def params(self):
        self.ensure_one()
        return json.loads(self.payload or '{}')

    def result_dict(self):
        self.ensure_one()
        try:
            return json.loads(self.result or '{}')
        except ValueError:
            return {}

    # ------------------------------------------------------------ sending
    def _can_send(self):
        self.ensure_one()
        server = self.server_id
        if not server.registered or not server.internal_ip:
            return False
        if server.state in ('retired',):
            return False
        if self.op_type != 'build_template' and server.state not in ('ready', 'full', 'maintenance'):
            return False
        dep = self.depends_on_id
        if dep and dep.state != 'done':
            if dep.state in ('failed', 'cancelled'):
                self._mark('failed', error=_('العملية السابقة %s لم تنجح', dep.name))
            return False
        return True

    def _send(self):
        agent = self.env['resta.agent']
        for op in self:
            try:
                job = agent.submit(op)
            except AgentError as exc:
                attempts = op.attempts + 1
                if attempts >= len(RETRY_MINUTES):
                    op._mark('failed', error=str(exc), attempts=attempts)
                else:
                    op.write({'attempts': attempts, 'error': str(exc),
                              'next_try': fields.Datetime.now() + timedelta(minutes=RETRY_MINUTES[attempts - 1])})
                continue
            op.write({'state': 'running', 'job_id': job.get('id'), 'date_sent': fields.Datetime.now(),
                      'error': False})
            if job.get('state') in ('done', 'failed'):  # كانت منفذة من قبل (تكرار)
                op._apply_job(job)

    @api.model
    def _cron_process(self):
        now = fields.Datetime.now()
        pending = self.search([('state', '=', 'pending'), ('next_try', '<=', now)], order='id')
        for op in pending:
            if op._can_send():
                op._send()
                self.env['resta.server']._commit()
        # المهام اللي ما وصلنا إشعارها: نسأل عنها
        stale = self.search([('state', '=', 'running'), ('date_sent', '<', now - timedelta(minutes=3))])
        for op in stale:
            try:
                job = self.env['resta.agent'].job(op)
            except AgentError:
                continue
            op._apply_job(job)
            self.env['resta.server']._commit()

    # ------------------------------------------------------------ results
    def _mark(self, state, result=None, error=None, attempts=None):
        vals = {'state': state, 'date_done': fields.Datetime.now() if state in ('done', 'failed') else False}
        if result is not None:
            vals['result'] = json.dumps(result, ensure_ascii=False, indent=1)
        if error is not None:
            vals['error'] = error
        if attempts is not None:
            vals['attempts'] = attempts
        self.write(vals)
        if state == 'done':
            self._on_done()
        elif state == 'failed':
            self._on_failed()

    def _apply_job(self, job):
        self.ensure_one()
        if self.state not in ('pending', 'running'):
            return False
        state = job.get('state')
        if state == 'done':
            self._mark('done', result=job.get('result') or {})
        elif state == 'failed':
            self._mark('failed', error=(job.get('error') or '')[-6000:])
        return True

    @api.model
    def handle_callback(self, server, job):
        op = self.search([('name', '=', job.get('operation_id')), ('server_id', '=', server.id)], limit=1)
        if not op:
            _logger.warning('callback for unknown operation %s from %s', job.get('operation_id'), server.name)
            return False
        if job.get('id') and not op.job_id:
            op.job_id = job['id']
        return op._apply_job(job)

    def _on_done(self):
        for op in self:
            res = op.result_dict()
            if op.project_id:
                op.project_id._on_operation_done(op, res)
            server = op.server_id
            if op.op_type == 'build_template':
                server.message_post(body=_('القالب %(t)s جاهز (%(m)s موديول، %(s)s ثانية).',
                                           t=res.get('template'), m=res.get('modules', '-'), s=res.get('seconds', '-')))
            elif op.op_type == 'backup_all':
                server.last_backup_all = fields.Datetime.now()
                for db in res.get('done', []):
                    server.project_ids.filtered(lambda p: p.db_name == db).write(
                        {'last_backup': fields.Datetime.now()})
                if res.get('failed'):
                    server.activity_schedule('mail.mail_activity_data_todo',
                                             summary=_('فشل النسخ الليلي لبعض القواعد'),
                                             note=json.dumps(res['failed'], ensure_ascii=False)[:2000],
                                             user_id=self.env.ref('base.user_admin').id)
            elif op.op_type == 'deploy' and not op.project_id:
                server.message_post(body=_('تم التحديث إلى %(c)s: %(n)s قاعدة نجحت، %(f)s فشلت.',
                                           c=(res.get('commit') or '')[:10], n=len(res.get('updated', [])),
                                           f=len(res.get('failed', {}))))

    def _on_failed(self):
        for op in self:
            if op.project_id:
                op.project_id._on_operation_failed(op)
            target = op.project_id or op.server_id
            target.activity_schedule(
                'mail.mail_activity_data_todo',
                summary=_('فشلت العملية %(n)s (%(t)s)', n=op.name, t=dict(OP_TYPES).get(op.op_type)),
                note=(op.error or '')[-1500:],
                user_id=(op.project_id.account_manager_id or self.env.ref('base.user_admin')).id,
            )

    # ------------------------------------------------------------ buttons
    def action_retry(self):
        for op in self:
            if op.state not in ('failed', 'cancelled'):
                raise UserError(_('إعادة المحاولة متاحة للعمليات الفاشلة فقط.'))
        # رقم جديد عشان الوكيل يعتبرها عملية جديدة
        for op in self:
            op.write({'state': 'pending', 'attempts': 0, 'next_try': fields.Datetime.now(), 'error': False,
                      'result': False, 'job_id': False, 'date_done': False,
                      'name': self._next_name()})
        return True

    def action_cancel(self):
        self.filtered(lambda o: o.state == 'pending').write({'state': 'cancelled'})
        return True

    def action_send_now(self):
        for op in self.filtered(lambda o: o.state == 'pending'):
            if not op._can_send():
                raise UserError(_('السيرفر غير جاهز أو العملية تنتظر عملية سابقة.'))
            op._send()
        return True
