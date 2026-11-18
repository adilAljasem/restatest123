import logging
import re
import secrets
from datetime import timedelta

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .resta_plan import EDITIONS, PROJECT_TYPES

_logger = logging.getLogger(__name__)

CODE_RE = re.compile(r'^[a-z0-9][a-z0-9-]{2,29}$')
RESERVED = {'www', 'admin', 'api', 'app', 'mail', 'control', 'platform', 'resta', 'staging', 'tpl', 'test',
            'support', 'help', 'status', 'dashboard', 'cdn', 'static', 'ftp', 'smtp', 'ns1', 'ns2'}
PROJECT_STATES = [
    ('draft', 'مسودة'),
    ('queued', 'بانتظار سيرفر'),
    ('provisioning', 'قيد الإنشاء'),
    ('active', 'يعمل'),
    ('suspended', 'موقوف'),
    ('archived', 'مؤرشف'),
    ('error', 'خطأ'),
]


class RestaProject(models.Model):
    _name = 'resta.project'
    _description = 'مشروع (مطعم ساس أو عميل كاستم)'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'create_date desc'

    name = fields.Char('العلامة / العميل', required=True, tracking=True)
    code = fields.Char('الرابط', required=True, tracking=True, copy=False,
                       help='يصير رابط النظام: <الرابط>.restaerp.com — أحرف إنجليزية صغيرة وأرقام وشرطة')
    project_type = fields.Selection(PROJECT_TYPES, 'النوع', required=True, default='saas', tracking=True)
    plan_id = fields.Many2one('resta.plan', 'الباقة', tracking=True,
                              domain="[('project_type', '=', project_type)]")
    edition = fields.Selection(EDITIONS, 'النسخة', compute='_compute_edition', store=True, readonly=False)
    state = fields.Selection(PROJECT_STATES, 'الحالة', default='draft', required=True, tracking=True, index=True)
    suspend_reason = fields.Selection([('billing', 'عدم السداد'), ('manual', 'يدوي')], 'سبب الإيقاف', tracking=True)
    partner_id = fields.Many2one('res.partner', 'العميل', required=True, tracking=True)
    admin_email = fields.Char('إيميل مدير النظام', help='يدخل فيه صاحب المطعم أول مرة')
    admin_name = fields.Char('اسم مدير النظام')
    account_manager_id = fields.Many2one('res.users', 'مدير الحساب', default=lambda self: self.env.user,
                                         tracking=True)
    branches = fields.Integer('عدد الفروع', default=1, tracking=True)
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    currency_id = fields.Many2one(related='company_id.currency_id')
    # التشغيل
    server_id = fields.Many2one('resta.server', 'السيرفر', tracking=True, index=True)
    db_name = fields.Char('القاعدة', compute='_compute_db_name', store=True)
    url = fields.Char('الرابط الكامل', compute='_compute_urls')
    staging_db = fields.Char('قاعدة التجربة', readonly=True, copy=False)
    staging_url = fields.Char('رابط التجربة', compute='_compute_urls')
    db_size_mb = fields.Float('حجم القاعدة (MB)', readonly=True)
    filestore_mb = fields.Float('حجم الملفات (MB)', readonly=True)
    last_backup = fields.Datetime('آخر نسخة احتياطية', readonly=True)
    deployed_commit = fields.Char('آخر نسخة كود', readonly=True)
    snapshots_json = fields.Json('النسخ المتاحة', readonly=True, copy=False)
    snapshots_checked = fields.Datetime('آخر تحديث لقائمة النسخ', readonly=True, copy=False)
    initial_password = fields.Char(groups='base.group_system', copy=False)
    # الكاستم
    repo_url = fields.Char('مستودع GitHub', help='https://github.com/<حساب>/<مستودع>.git')
    branch = fields.Char('فرع الإنتاج', default='main')
    staging_branch = fields.Char('فرع التجربة', default='staging')
    init_modules = fields.Char('موديولات التثبيت الأولي', help='مفصولة بفواصل. للكاستم فقط')
    # الفوترة
    trial_end = fields.Date('نهاية التجربة')
    start_date = fields.Date('بداية الاشتراك')
    next_invoice_date = fields.Date('الفاتورة القادمة', tracking=True)
    monthly_amount = fields.Monetary('الاشتراك الشهري', compute='_compute_monthly_amount',
                                     currency_field='currency_id')
    invoice_ids = fields.One2many('account.move', 'resta_project_id', 'الفواتير')
    invoice_count = fields.Integer(compute='_compute_invoice_count')
    overdue_amount = fields.Monetary('متأخرات', compute='_compute_invoice_count', currency_field='currency_id')
    operation_ids = fields.One2many('resta.operation', 'project_id', 'العمليات')
    operation_count = fields.Integer(compute='_compute_operation_count')
    notes = fields.Html('ملاحظات')

    _code_unique = models.Constraint('unique(code)', 'هذا الرابط مستخدم لمشروع آخر.')

    # ------------------------------------------------------------ computes
    @api.depends('plan_id')
    def _compute_edition(self):
        for p in self:
            if p.plan_id.edition:
                p.edition = p.plan_id.edition

    @api.depends('code')
    def _compute_db_name(self):
        for p in self:
            p.db_name = p.code

    @api.depends('code', 'staging_db')
    def _compute_urls(self):
        domain = self.env['resta.config'].get('base_domain')
        for p in self:
            p.url = f'https://{p.code}.{domain}' if p.code else False
            p.staging_url = f'https://{p.staging_db}.{domain}' if p.staging_db else False

    @api.depends('plan_id', 'branches', 'db_size_mb', 'filestore_mb')
    def _compute_monthly_amount(self):
        for p in self:
            p.monthly_amount = p.plan_id.monthly_amount(p) if p.plan_id else 0.0

    def _compute_invoice_count(self):
        today = fields.Date.context_today(self)
        for p in self:
            p.invoice_count = len(p.invoice_ids)
            overdue = p.invoice_ids.filtered(
                lambda m: m.state == 'posted' and m.payment_state in ('not_paid', 'partial')
                and m.invoice_date_due and m.invoice_date_due < today)
            p.overdue_amount = sum(overdue.mapped('amount_residual'))

    def _compute_operation_count(self):
        for p in self:
            p.operation_count = len(p.operation_ids)

    @api.constrains('code')
    def _check_code(self):
        for p in self:
            if not CODE_RE.match(p.code or '') or p.code in RESERVED or p.code.startswith('tpl'):
                raise ValidationError(_('الرابط "%s" غير صالح: من 3 إلى 30 حرف إنجليزي صغير أو رقم أو شرطة، '
                                        'ولا يكون محجوزاً.', p.code))
            if p.code.endswith('-staging'):
                raise ValidationError(_('لا تنهي الرابط بـ -staging (محجوزة لنسخ التجربة).'))

    @api.onchange('name')
    def _onchange_name(self):
        if self.name and not self.code:
            guess = re.sub(r'[^a-z0-9-]+', '-', self.name.lower()).strip('-')[:30]
            if CODE_RE.match(guess or ''):
                self.code = guess

    @api.model
    def _billing_period_label(self):
        today = fields.Date.context_today(self)
        return today.strftime('%Y-%m')

    # ------------------------------------------------------------ provisioning
    def _new_operation(self, op_type, payload, server=None, depends_on=None):
        self.ensure_one()
        return self.env['resta.operation'].create({
            'server_id': (server or self.server_id).id, 'project_id': self.id, 'op_type': op_type,
            'payload_dict': payload, 'depends_on_id': depends_on.id if depends_on else False,
        })

    def action_provision(self):
        for p in self:
            if p.state not in ('draft', 'error', 'queued'):
                raise UserError(_('المشروع %s تم إنشاؤه من قبل.', p.name))
            if not p.plan_id:
                raise UserError(_('اختر باقة للمشروع %s.', p.name))
            if not p.admin_email:
                raise UserError(_('أدخل إيميل مدير النظام للمشروع %s.', p.name))
            if p.project_type == 'saas':
                p._provision_saas()
            else:
                p._provision_custom()
            if p.state == 'queued':
                continue
            if not p.start_date:
                trial = self.env['resta.config'].get_int('trial_days', 14) if p.project_type == 'saas' else 0
                today = fields.Date.context_today(p)
                p.write({'start_date': today, 'trial_end': today + timedelta(days=trial) if trial else False,
                         'next_invoice_date': today + timedelta(days=trial)})
        return True

    def _provision_saas(self):
        self.ensure_one()
        servers = self.env['resta.server']
        server = self.server_id or servers.select_cell()
        if not server:
            # ما نرفض العميل: يدخل الانتظار، والمنصة تجهزه أول ما تتوفر خلية
            try:
                with self.env.cr.savepoint():
                    new_cell = servers.ensure_capacity()
            except Exception:
                _logger.exception('auto provisioning a new cell failed')
                new_cell = False
            if self.state != 'queued':
                self.state = 'queued'
                self.message_post(body=_('كل الخلايا ممتلئة؛ النظام بانتظار سيرفر. %s', _(
                    'جاري فتح خلية جديدة تلقائياً.') if new_cell else _(
                    'فعّل "افتح سيرفر جديد تلقائياً" أو أنشئ خلية من شاشة السيرفرات.')))
            return
        edition = self.edition or 'dinein'
        template = f'tpl_{edition}'
        password = secrets.token_urlsafe(10)
        self.sudo().initial_password = password
        self.write({'server_id': server.id, 'state': 'provisioning', 'suspend_reason': False})
        self.env['resta.cloudflare'].upsert(self.code, server.external_ip)
        self._new_operation('create_db', {
            'db': self.db_name, 'template': template, 'company_name': self.name,
            'admin_login': self.admin_email, 'admin_password': password,
            'admin_name': self.admin_name or self.partner_id.name, 'base_url': self.url,
            'lang': self.env['resta.config'].get('default_lang'),
        }, server=server, depends_on=server.template_op(template))
        self.message_post(body=_('بدأ إنشاء النظام على %(s)s من قالب %(t)s.', s=server.name, t=template))

    def _provision_custom(self):
        self.ensure_one()
        if not self.repo_url:
            raise UserError(_('مشروع الكاستم يحتاج مستودع GitHub.'))
        server = self.server_id
        if not server:
            plan = self.plan_id
            server = self.env['resta.server'].create({
                'name': self.code, 'kind': 'dedicated', 'capacity': 1,
                'machine_type': plan.machine_type or 'e2-standard-2', 'disk_gb': plan.disk_gb or 100,
            })
            self.server_id = server
            server.action_provision()
        password = secrets.token_urlsafe(10)
        self.sudo().initial_password = password
        self.write({'state': 'provisioning', 'suspend_reason': False})
        if server.external_ip:
            self.env['resta.cloudflare'].upsert(self.code, server.external_ip)
            self.env['resta.cloudflare'].upsert(self.code + '-staging', server.external_ip)
        modules = [m.strip() for m in (self.init_modules or 'base').split(',') if m.strip()]
        self._new_operation('create_db', {
            'db': self.db_name, 'modules': modules, 'stack': 'main', 'company_name': self.name,
            'admin_login': self.admin_email, 'admin_password': password,
            'admin_name': self.admin_name or self.partner_id.name, 'base_url': self.url,
            'lang': self.env['resta.config'].get('default_lang'),
        }, server=server)
        self.message_post(body=_('بدأ تجهيز سيرفر خاص %s للمشروع من المستودع.', server.name))

    # ------------------------------------------------------------ actions
    def _require_server(self):
        for p in self:
            if not p.server_id:
                raise UserError(_('المشروع %s ليس على سيرفر.', p.name))

    def action_suspend(self, reason='manual'):
        self._require_server()
        for p in self.filtered(lambda x: x.state == 'active'):
            p.suspend_reason = reason
            p._new_operation('suspend', {'db': p.db_name})
            p.message_post(body=_('طُلب إيقاف النظام (%s).', dict(self._fields['suspend_reason'].selection)[reason]))
        return True

    def action_suspend_manual(self):
        return self.action_suspend('manual')

    def action_resume(self):
        self._require_server()
        for p in self.filtered(lambda x: x.state == 'suspended'):
            p._new_operation('resume', {'db': p.db_name})
            p.message_post(body=_('طُلب تشغيل النظام.'))
        return True

    def action_backup(self):
        self._require_server()
        for p in self:
            p._new_operation('backup', {'db': p.db_name, 'tag': 'manual'})
        return True

    def action_create_staging(self):
        self._require_server()
        for p in self:
            target = f'{p.db_name}-staging'
            stack = 'staging' if p.project_type == 'custom' else 'main'
            if p.server_id.external_ip:
                self.env['resta.cloudflare'].upsert(target, p.server_id.external_ip)
            p._new_operation('duplicate', {'db': p.db_name, 'target': target, 'neutralize': True,
                                           'stack': stack, 'base_url': f"https://{target}.{self.env['resta.config'].get('base_domain')}"})
            p.message_post(body=_('طُلب إنشاء نسخة تجربة محيّدة (بدون إيميلات أو مهام مجدولة).'))
        return True

    def action_drop_staging(self):
        for p in self.filtered('staging_db'):
            p._new_operation('drop', {'db': p.staging_db, 'confirm': p.staging_db, 'backup_first': False})
        return True

    def action_deploy(self):
        self._require_server()
        for p in self:
            # نسخة احتياطية أولاً؛ إذا فشلت لا يبدأ التحديث
            backup = p._new_operation('backup', {'db': p.db_name, 'tag': 'pre-deploy'})
            if p.project_type == 'custom':
                p._new_operation('deploy', {'stack': 'main', 'ref': p.branch, 'databases': [p.db_name]},
                                 depends_on=backup)
            else:
                p._new_operation('deploy', {'stack': 'main', 'databases': [p.db_name], 'modules': 'all'},
                                 depends_on=backup)
            p.message_post(body=_('طُلب تحديث كود الإنتاج بعد نسخة احتياطية (يتوقف النظام دقائق أثناء التحديث).'))
        return True

    def action_deploy_staging(self):
        self._require_server()
        for p in self.filtered(lambda x: x.project_type == 'custom'):
            if not p.staging_db:
                raise UserError(_('أنشئ نسخة تجربة أولاً.'))
            p._new_operation('deploy', {'stack': 'staging', 'ref': p.staging_branch, 'databases': [p.staging_db],
                                        'stop_during_update': True})
        return True

    @api.model
    def _provision_queued(self):
        for p in self.search([('state', '=', 'queued')], order='id'):
            if not self.env['resta.server'].select_cell():
                break
            try:
                p.action_provision()
                self.env['resta.server']._commit()
            except Exception:
                _logger.exception('queued provisioning failed for %s', p.code)
                self.env.cr.rollback()

    def action_list_snapshots(self):
        self._require_server()
        for p in self:
            p._new_operation('snapshots', {'db': p.db_name})
        return True

    def action_open_restore(self):
        self.ensure_one()
        self._require_server()
        return {
            'type': 'ir.actions.act_window', 'res_model': 'resta.restore.wizard', 'view_mode': 'form',
            'target': 'new', 'name': _('استرجاع نسخة'), 'context': {'default_project_id': self.id},
        }

    def action_archive_project(self):
        self._require_server()
        for p in self:
            p._new_operation('drop', {'db': p.db_name, 'confirm': p.db_name, 'backup_first': True})
            p.message_post(body=_('طُلب أرشفة النظام: نسخة احتياطية أخيرة ثم حذف القاعدة من السيرفر.'))
        return True

    def action_open_url(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_url', 'url': self.url, 'target': 'new'}

    def action_view_invoices(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('account.action_move_out_invoice_type')
        action['domain'] = [('resta_project_id', '=', self.id)]
        action['context'] = {'default_resta_project_id': self.id, 'default_partner_id': self.partner_id.id,
                             'default_move_type': 'out_invoice'}
        return action

    def action_view_operations(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': _('العمليات'), 'res_model': 'resta.operation',
            'view_mode': 'list,form', 'domain': [('project_id', '=', self.id)],
        }

    def action_send_welcome(self):
        template = self.env.ref('resta_platform.mail_template_welcome', raise_if_not_found=False)
        for p in self:
            if template:
                template.sudo().send_mail(p.id, force_send=False)
            p.sudo().initial_password = False
        return True

    # ------------------------------------------------------------ operation results
    def _on_operation_done(self, op, res):
        self.ensure_one()
        t = op.op_type
        if t == 'create_db' and res.get('db') == self.db_name:
            self.write({'state': 'active', 'db_size_mb': res.get('size_mb', 0)})
            self.message_post(body=_('النظام جاهز على %s', self.url))
            self.action_send_welcome()
        elif t == 'suspend':
            self.state = 'suspended'
            self.message_post(body=_('تم إيقاف النظام. البيانات محفوظة.'))
        elif t == 'resume':
            self.write({'state': 'active', 'suspend_reason': False})
            self.message_post(body=_('تم تشغيل النظام.'))
        elif t == 'backup':
            self.last_backup = fields.Datetime.now()
            copies = res.get('copies', 0)
            msg = _('نسخة احتياطية في %s أماكن.', copies)
            if res.get('errors'):
                msg += ' ' + _('تنبيه: فشلت في %s مكان.', len(res['errors']))
            self.message_post(body=msg)
        elif t == 'duplicate':
            self.staging_db = res.get('db')
            self.message_post(body=_('نسخة التجربة جاهزة: %s', self.staging_url))
        elif t == 'drop':
            if res.get('db') == self.db_name:
                self.state = 'archived'
                self.env['resta.cloudflare'].remove(self.code)
                self.message_post(body=_('تمت الأرشفة. آخر نسخة محفوظة في أماكن النسخ الاحتياطي.'))
            elif res.get('db') == self.staging_db:
                self.env['resta.cloudflare'].remove(self.staging_db)
                self.staging_db = False
        elif t == 'deploy':
            self.deployed_commit = (res.get('commit') or '')[:12]
            failed = res.get('failed') or {}
            if failed:
                self.message_post(body=_('التحديث فشل على: %s', ', '.join(failed)))
            else:
                self.message_post(body=_('تم التحديث إلى %s', self.deployed_commit))
        elif t == 'snapshots':
            self.write({'snapshots_json': res.get('snapshots') or [], 'snapshots_checked': fields.Datetime.now()})
            self.message_post(body=_('تم تحديث قائمة النسخ: %s نسخة.', len(res.get('snapshots') or [])))
        elif t == 'restore':
            if res.get('db') == f'{self.db_name}-staging':
                self.staging_db = res.get('db')
                self.message_post(body=_('النسخة المسترجعة جاهزة للفحص على %s', self.staging_url))
            elif res.get('old_db'):
                self.message_post(body=_('تم استرجاع النسخة %(s)s مكان قاعدة الإنتاج. القاعدة السابقة محفوظة باسم %(o)s.',
                                         s=res.get('from_snapshot'), o=res.get('old_db')))
            else:
                self.message_post(body=_('تم الاسترجاع إلى القاعدة %s', res.get('db')))

    def _on_operation_failed(self, op):
        self.ensure_one()
        if op.op_type == 'create_db' and self.state == 'provisioning':
            self.state = 'error'
        if op.op_type == 'suspend' and self.state == 'active':
            self.suspend_reason = False
        self.message_post(body=_('فشلت العملية %(n)s: %(e)s', n=op.name, e=(op.error or '')[:500]))

    # ------------------------------------------------------------ billing
    def _create_invoice(self):
        self.ensure_one()
        lines = self.plan_id.invoice_lines(self)
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': self.partner_id.id,
            'invoice_date': fields.Date.context_today(self),
            'invoice_origin': self.code,
            'resta_project_id': self.id,
            'invoice_line_ids': [(0, 0, line) for line in lines],
        })
        if self.env['resta.config'].get_bool('auto_post_invoices'):
            move.action_post()
        base = self.next_invoice_date or fields.Date.context_today(self)
        self.next_invoice_date = base + relativedelta(months=1)
        self.message_post(body=_('أُنشئت فاتورة الاشتراك %s', move.name or move.id))
        return move

    def action_invoice_now(self):
        for p in self:
            if not p.plan_id:
                raise UserError(_('المشروع %s بدون باقة.', p.name))
            p._create_invoice()
        return True

    def _overdue_invoices(self, limit_date):
        self.ensure_one()
        return self.invoice_ids.filtered(
            lambda m: m.state == 'posted' and m.payment_state in ('not_paid', 'partial')
            and m.invoice_date_due and m.invoice_date_due < limit_date)

    @api.model
    def _cron_billing(self):
        today = fields.Date.context_today(self)
        due = self.search([('state', 'in', ('active', 'suspended')), ('plan_id', '!=', False),
                           ('next_invoice_date', '!=', False), ('next_invoice_date', '<=', today)])
        for p in due:
            try:
                p._create_invoice()
                self.env['resta.server']._commit()
            except Exception:
                _logger.exception('invoice failed for %s', p.code)
                self.env.cr.rollback()
        self._check_overdue()

    @api.model
    def _check_overdue(self):
        grace = self.env['resta.config'].get_int('grace_days', 7)
        limit = fields.Date.context_today(self) - timedelta(days=grace)
        for p in self.search([('state', '=', 'active'), ('server_id', '!=', False)]):
            if p._overdue_invoices(limit) and not p.operation_ids.filtered(
                    lambda o: o.op_type == 'suspend' and o.state in ('pending', 'running')):
                p.action_suspend('billing')
        for p in self.search([('state', '=', 'suspended'), ('suspend_reason', '=', 'billing')]):
            if not p._overdue_invoices(limit) and not p.operation_ids.filtered(
                    lambda o: o.op_type == 'resume' and o.state in ('pending', 'running')):
                p.action_resume()

    # ------------------------------------------------------------ self signup (resta.sa)
    @api.model
    def signup(self, data):
        """تسجيل ذاتي من موقع resta.sa. يرجع بيانات المشروع."""
        email = (data.get('email') or '').strip().lower()
        brand = (data.get('brand_name') or '').strip()
        code = (data.get('code') or '').strip().lower()
        edition = data.get('edition') or 'dinein'
        if not email or '@' not in email or not brand or not code:
            raise ValidationError(_('الاسم والإيميل والرابط مطلوبة.'))
        if edition not in dict(EDITIONS):
            raise ValidationError(_('النسخة غير معروفة.'))
        if self.search_count([('code', '=', code)]):
            raise ValidationError(_('هذا الرابط محجوز، اختر رابطاً آخر.'))
        plan = self.env['resta.plan'].search([('project_type', '=', 'saas'), ('edition', '=', edition),
                                              ('is_default', '=', True)], limit=1) or \
            self.env['resta.plan'].search([('project_type', '=', 'saas'), ('edition', '=', edition)], limit=1)
        if not plan:
            raise ValidationError(_('ما فيه باقة متاحة لهذه النسخة.'))
        partner = self.env['res.partner'].search([('email', '=ilike', email)], limit=1) or \
            self.env['res.partner'].create({'name': data.get('contact_name') or brand, 'email': email,
                                            'phone': data.get('phone'), 'is_company': False})
        project = self.create({
            'name': brand, 'code': code, 'project_type': 'saas', 'plan_id': plan.id, 'edition': edition,
            'partner_id': partner.id, 'admin_email': email, 'admin_name': data.get('contact_name'),
            'branches': max(1, int(data.get('branches') or 1)),
        })
        project.message_post(body=_('تسجيل ذاتي من موقع ريستا.'))
        project.action_provision()
        return {'project_id': project.id, 'code': project.code, 'url': project.url, 'state': project.state,
                'trial_end': fields.Date.to_string(project.trial_end) if project.trial_end else None}
