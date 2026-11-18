import hmac
import logging
import secrets
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import config as odoo_config
from odoo.tools.misc import file_open

from .agent_client import AgentError

_logger = logging.getLogger(__name__)

SERVER_STATES = [
    ('draft', 'مسودة'),
    ('provisioning', 'قيد الإنشاء'),
    ('ready', 'جاهز'),
    ('full', 'ممتلئ'),
    ('maintenance', 'صيانة'),
    ('error', 'لا يستجيب'),
    ('retired', 'متوقف نهائياً'),
]


def _bootstrap_script():
    with file_open('resta_platform/data/bootstrap_cell.sh') as fh:
        return fh.read()


class RestaServer(models.Model):
    _name = 'resta.server'
    _description = 'سيرفر'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'kind, name'

    name = fields.Char('الاسم', required=True, tracking=True, help='مثال: cell-01')
    kind = fields.Selection([('cell', 'خلية مشتركة (ساس)'), ('dedicated', 'سيرفر خاص (كاستم)')],
                            'النوع', required=True, default='cell', tracking=True)
    provider = fields.Selection([('gcp', 'Google Cloud'), ('manual', 'يدوي')], 'المزوّد',
                                required=True, default='gcp')
    state = fields.Selection(SERVER_STATES, 'الحالة', default='draft', required=True, tracking=True)
    zone = fields.Char('النطاق', default=lambda self: self.env['resta.config'].get('gcp_zone'))
    machine_type = fields.Char('نوع السيرفر', default=lambda self: self.env['resta.config'].get('cell_machine_type'))
    disk_gb = fields.Integer('القرص (GB)', default=lambda self: self.env['resta.config'].get_int('cell_disk_gb', 200))
    instance_name = fields.Char('اسم السيرفر في Google', compute='_compute_instance_name', store=True, readonly=False)
    external_ip = fields.Char('العنوان الخارجي', tracking=True)
    internal_ip = fields.Char('العنوان الداخلي', tracking=True)
    agent_port = fields.Integer('منفذ الوكيل', default=8090)
    agent_secret = fields.Char('سر الوكيل', groups='base.group_system', copy=False)
    register_token = fields.Char(groups='base.group_system', copy=False)
    db_password = fields.Char('كلمة سر قاعدة البيانات', groups='base.group_system', copy=False)
    master_password = fields.Char('كلمة سر أودو الرئيسية', groups='base.group_system', copy=False)
    registered = fields.Boolean('مسجّل', readonly=True, copy=False)
    capacity = fields.Integer('السعة (علامات)', default=lambda self: self.env['resta.config'].get_int('cell_capacity', 10))
    project_ids = fields.One2many('resta.project', 'server_id', 'المشاريع')
    project_count = fields.Integer('عدد المشاريع', compute='_compute_project_count')
    operation_ids = fields.One2many('resta.operation', 'server_id', 'العمليات')
    metric_ids = fields.One2many('resta.metric', 'server_id', 'الأداء')
    templates_ready = fields.Char('القوالب الجاهزة', readonly=True, copy=False)
    # الأداء
    cpu_percent = fields.Float('المعالج %', readonly=True)
    ram_percent = fields.Float('الذاكرة %', readonly=True)
    disk_percent = fields.Float('القرص %', readonly=True)
    load_percent = fields.Float('الحمل %', readonly=True, help='أعلى قيمة بين المعالج والذاكرة والقرص')
    db_count = fields.Integer('عدد القواعد', readonly=True)
    agent_version = fields.Char('نسخة الوكيل', readonly=True)
    last_seen = fields.Datetime('آخر اتصال', readonly=True)
    online = fields.Boolean('متصل', compute='_compute_online')
    last_backup_all = fields.Datetime('آخر نسخ ليلي', readonly=True)
    notes = fields.Html('ملاحظات')

    _name_unique = models.Constraint('unique(name)', 'اسم السيرفر مستخدم.')

    @api.depends('name')
    def _compute_instance_name(self):
        for s in self:
            if not s.instance_name and s.name:
                s.instance_name = 'resta-' + s.name.lower().replace('_', '-').replace(' ', '-')

    def _compute_project_count(self):
        data = self.env['resta.project']._read_group(
            [('server_id', 'in', self.ids), ('state', 'not in', ('archived', 'draft'))], ['server_id'], ['__count'])
        counts = {srv.id: c for srv, c in data}
        for s in self:
            s.project_count = counts.get(s.id, 0)

    def _compute_online(self):
        limit = fields.Datetime.now() - timedelta(minutes=10)
        for s in self:
            s.online = bool(s.last_seen and s.last_seen >= limit)

    # ------------------------------------------------------------ provisioning
    def _platform_url(self):
        url = self.env['resta.config'].get('platform_internal_url')
        if not url:
            raise UserError(_('حدد رابط المنصة الداخلي في الإعدادات (مثال http://10.212.0.2:8069).'))
        return url.rstrip('/')

    def action_provision(self):
        """إنشاء السيرفر في Google Cloud: عنوان ثابت + سيرفر بسكربت إقلاع يسجّل نفسه في المنصة."""
        gcp = self.env['resta.gcp']
        for s in self:
            if s.provider != 'gcp':
                raise UserError(_('السيرفر %s يدوي: ثبّت الوكيل عليه بالسكربت ثم سجّله.', s.name))
            if s.state not in ('draft', 'error'):
                raise UserError(_('السيرفر %s ليس في حالة تسمح بالإنشاء.', s.name))
            s.sudo().write({
                'agent_secret': secrets.token_hex(32),
                'register_token': secrets.token_urlsafe(32),
                'registered': False,
            })
            ip = gcp.reserve_address(s.instance_name + '-ip')
            metadata = {
                'startup-script': _bootstrap_script(),
                'resta-platform-url': s._platform_url(),
                'resta-register-token': s.sudo().register_token,
                'resta-server-id': s.id,
                'resta-agent-repo': self.env['resta.config'].get('agent_repo'),
                'resta-agent-branch': self.env['resta.config'].get('agent_branch'),
                'resta-git-token': self.env['resta.config'].get('git_token') or '',
            }
            gcp.create_instance(s.instance_name, s.zone, s.machine_type, s.disk_gb, ip, metadata,
                                kind=s.kind, labels={'resta-server': str(s.id)})
            s.write({'external_ip': ip, 'state': 'provisioning'})
            s.message_post(body=_('بدأ إنشاء السيرفر في Google Cloud (%(zone)s، %(type)s، العنوان %(ip)s). '
                                  'سيسجّل نفسه خلال 5 إلى 15 دقيقة.', zone=s.zone, type=s.machine_type, ip=ip))
        return True

    def _workers(self):
        try:
            cpus = int((self.machine_type or '').rsplit('-', 1)[-1])
        except ValueError:
            cpus = 2
        return max(2, cpus * 2)

    def _stacks(self):
        cfg = self.env['resta.config']
        if self.kind == 'cell':
            return [{'name': 'main', 'repo_url': cfg.get('saas_repo'), 'branch': cfg.get('saas_branch'),
                     'routes': ['*']}]
        project = self.project_ids[:1]
        if not project or not project.repo_url:
            raise UserError(_('السيرفر الخاص %s لازم يكون مربوط بمشروع كاستم له مستودع.', self.name))
        return [
            {'name': 'main', 'repo_url': project.repo_url, 'branch': project.branch or 'main',
             'routes': [project.code]},
            {'name': 'staging', 'repo_url': project.repo_url, 'branch': project.staging_branch or 'staging',
             'routes': [project.code + '-staging']},
        ]

    def _backup_repos(self):
        cfg = self.env['resta.config']
        repos = ['/opt/resta/backups/restic']
        if cfg.get('backup_gcs_bucket'):
            repos.append(f"gs:{cfg.get('backup_gcs_bucket')}:/{self.name}")
        if cfg.get('backup_s3_url'):
            repos.append(f"{cfg.get('backup_s3_url').rstrip('/')}/{self.name}")
        return repos

    def _registration_payload(self, internal_ip):
        """الإعدادات اللي يستلمها السيرفر مرة واحدة عند أول تشغيل."""
        self.ensure_one()
        cfg = self.env['resta.config']
        s = self.sudo()
        if not s.db_password:
            s.db_password = secrets.token_urlsafe(24)
        if not s.master_password:
            s.master_password = secrets.token_urlsafe(24)
        backup_env = {}
        if cfg.get('gcp_project'):
            backup_env['GOOGLE_PROJECT_ID'] = cfg.get('gcp_project')
        if cfg.get('backup_s3_key'):
            backup_env['AWS_ACCESS_KEY_ID'] = cfg.get('backup_s3_key')
            backup_env['AWS_SECRET_ACCESS_KEY'] = cfg.get('backup_s3_secret') or ''
        return {
            'server_id': s.id,
            'server_name': s.name,
            'kind': s.kind,
            'agent_secret': s.agent_secret,
            'callback_url': f'{self._platform_url()}/resta/agent/callback',
            'bind_host': internal_ip,
            'bind_port': s.agent_port,
            'base_domain': cfg.get('base_domain'),
            'db_password': s.db_password,
            'master_password': s.master_password,
            'workers': s._workers(),
            'max_cron_threads': 2,
            'git_token': cfg.get('git_token') or '',
            'enterprise_repo_url': cfg.get('enterprise_repo') or '',
            'enterprise_branch': cfg.get('enterprise_branch'),
            'stacks': s._stacks(),
            'restic_password': cfg.get('restic_password') or '',
            'backup_repos': s._backup_repos(),
            'backup_env': backup_env,
            'origin_cert': cfg.get('origin_cert') or '',
            'origin_key': cfg.get('origin_key') or '',
            'default_lang': cfg.get('default_lang'),
        }

    def register_from_agent(self, token, internal_ip):
        """يستدعيه السيرفر الجديد مرة واحدة. الرمز يُمسح بعد الاستخدام."""
        self.ensure_one()
        s = self.sudo()
        if not s.register_token or not token or not hmac.compare_digest(s.register_token, token):
            raise UserError(_('رمز تسجيل غير صالح'))
        if not s.restic_ready():
            raise UserError(_('أكمل إعدادات النسخ الاحتياطي (كلمة سر التشفير) قبل تسجيل السيرفرات.'))
        payload = s._registration_payload(internal_ip)
        s.write({'internal_ip': internal_ip, 'register_token': False, 'registered': True})
        s.message_post(body=_('سجّل السيرفر نفسه من العنوان الداخلي %s وبدأ التجهيز.', internal_ip))
        if s.kind == 'cell':
            s._queue_templates()
        return payload

    def restic_ready(self):
        return bool(self.env['resta.config'].get('restic_password'))

    def _queue_templates(self, rebuild=False):
        cfg = self.env['resta.config']
        ops = self.env['resta.operation']
        for s in self:
            for edition in ('dinein', 'fastfood'):
                modules = cfg.modules_for(edition)
                if not modules:
                    continue
                ops.create({
                    'server_id': s.id, 'op_type': 'build_template',
                    'payload_dict': {'name': f'tpl_{edition}', 'modules': modules, 'rebuild': rebuild,
                                     'lang': cfg.get('default_lang')},
                })
        return True

    def action_rebuild_templates(self):
        return self._queue_templates(rebuild=True)

    def template_op(self, template):
        self.ensure_one()
        return self.env['resta.operation'].search([
            ('server_id', '=', self.id), ('op_type', '=', 'build_template'),
            ('state', 'in', ('pending', 'running')), ('payload', 'like', f'"{template}"')], limit=1)

    # ------------------------------------------------------------ actions
    def action_refresh(self):
        for s in self:
            s._refresh_health(raise_errors=True)
        return True

    def action_deploy(self):
        for s in self:
            stack = 'main'
            s.env['resta.operation'].create({
                'server_id': s.id, 'op_type': 'deploy',
                'payload_dict': {'stack': stack, 'modules': 'all'},
            })
            s.message_post(body=_('طُلب تحديث الكود وتحديث كل القواعد على هذا السيرفر.'))
        return True

    def action_backup_all(self):
        for s in self:
            s.env['resta.operation'].create({'server_id': s.id, 'op_type': 'backup_all', 'payload_dict': {}})
        return True

    def action_maintenance(self):
        self.write({'state': 'maintenance'})

    def action_set_ready(self):
        self.write({'state': 'ready'})

    def action_retire(self):
        gcp = self.env['resta.gcp']
        for s in self:
            if s.project_ids.filtered(lambda p: p.state not in ('archived', 'draft')):
                raise UserError(_('انقل أو أرشف مشاريع السيرفر %s قبل إيقافه نهائياً.', s.name))
            if s.provider == 'gcp' and s.instance_name:
                gcp.delete_instance(s.instance_name, s.zone)
                gcp.release_address(s.instance_name + '-ip')
            s.write({'state': 'retired'})
            s.message_post(body=_('تم حذف السيرفر من Google Cloud وإيقافه نهائياً.'))
        return True

    # ------------------------------------------------------------ selection & capacity
    @api.model
    def select_cell(self):
        threshold = self.env['resta.config'].get_int('load_threshold', 70)
        cells = self.search([('kind', '=', 'cell'), ('state', '=', 'ready')])
        cells = cells.filtered(lambda c: c.project_count < c.capacity and c.load_percent < threshold)
        return cells.sorted(lambda c: (c.load_percent, c.project_count))[:1]

    @api.model
    def _next_cell_name(self):
        names = self.with_context(active_test=False).search([('kind', '=', 'cell')]).mapped('name')
        n = 1
        while f'cell-{n:02d}' in names:
            n += 1
        return f'cell-{n:02d}'

    @api.model
    def ensure_capacity(self):
        """لو ما فيه خلية فيها مكان، يفتح خلية جديدة (إذا مفعّل) أو ينبّه المدراء."""
        if self.select_cell():
            return False
        if self.search_count([('kind', '=', 'cell'), ('state', '=', 'provisioning')]):
            return False
        cfg = self.env['resta.config']
        if cfg.get_bool('auto_provision') and self.env['resta.gcp'].is_configured():
            cell = self.create({'name': self._next_cell_name(), 'kind': 'cell'})
            cell.action_provision()
            return cell
        cell = self.search([('kind', '=', 'cell')], order='id desc', limit=1)
        if cell:
            group = self.env.ref('resta_platform.group_resta_manager', raise_if_not_found=False)
            user = (group.user_ids[:1] if group else False) or self.env.ref('base.user_admin')
            summary = _('كل الخلايا ممتلئة — افتح خلية جديدة')
            if not cell.activity_ids.filtered(lambda a: a.summary == summary):
                cell.activity_schedule('mail.mail_activity_data_todo', summary=summary, user_id=user.id)
        return False

    # ------------------------------------------------------------ health
    def _refresh_health(self, raise_errors=False):
        self.ensure_one()
        agent = self.env['resta.agent']
        threshold = self.env['resta.config'].get_int('load_threshold', 70)
        try:
            data = agent.health(self)
        except AgentError as exc:
            if raise_errors:
                raise UserError(_('السيرفر %(name)s لا يستجيب: %(err)s', name=self.name, err=exc))
            limit = fields.Datetime.now() - timedelta(minutes=10)
            if self.state in ('ready', 'full') and (not self.last_seen or self.last_seen < limit):
                self.state = 'error'
                self.activity_schedule('mail.mail_activity_data_todo',
                                       summary=_('السيرفر لا يستجيب منذ 10 دقائق'),
                                       user_id=self.env.ref('base.user_admin').id)
            return False
        load = max(data.get('cpu_percent', 0), data.get('ram_percent', 0), data.get('disk_percent', 0),
                   data.get('load_percent', 0))
        vals = {
            'cpu_percent': data.get('cpu_percent', 0), 'ram_percent': data.get('ram_percent', 0),
            'disk_percent': data.get('disk_percent', 0), 'load_percent': load,
            'db_count': len([d for d in data.get('databases', []) if not d.get('template')]),
            'agent_version': data.get('agent_version'), 'last_seen': fields.Datetime.now(),
            'templates_ready': ','.join(sorted(d['name'] for d in data.get('databases', []) if d.get('template'))),
        }
        if self.state in ('provisioning', 'error') and self.registered:
            vals['state'] = 'ready'
        state = vals.get('state', self.state)
        if state in ('ready', 'full') and self.kind == 'cell':
            vals['state'] = 'full' if (self.project_count >= self.capacity or load >= threshold) else 'ready'
        self.write(vals)
        self.env['resta.metric'].create({
            'server_id': self.id, 'cpu_percent': vals['cpu_percent'], 'ram_percent': vals['ram_percent'],
            'disk_percent': vals['disk_percent'], 'load_percent': load, 'db_count': vals['db_count'],
        })
        sizes = {d['name']: d for d in data.get('databases', [])}
        for project in self.project_ids:
            info = sizes.get(project.db_name)
            if info:
                project.write({'db_size_mb': info.get('db_size_mb', 0), 'filestore_mb': info.get('filestore_mb', 0)})
        return True

    @api.model
    def _cron_health(self):
        servers = self.search([('registered', '=', True), ('state', 'not in', ('retired', 'maintenance'))])
        for s in servers:
            try:
                s._refresh_health()
                self._commit()
            except Exception:
                _logger.exception('health check failed for %s', s.name)
                self.env.cr.rollback()
        self.env['resta.project']._provision_queued()
        self.ensure_capacity()

    @api.model
    def _commit(self):
        if not odoo_config['test_enable']:
            self.env.cr.commit()


class RestaMetric(models.Model):
    _name = 'resta.metric'
    _description = 'قراءة أداء'
    _order = 'create_date desc'

    server_id = fields.Many2one('resta.server', required=True, ondelete='cascade', index=True)
    cpu_percent = fields.Float('المعالج %')
    ram_percent = fields.Float('الذاكرة %')
    disk_percent = fields.Float('القرص %')
    load_percent = fields.Float('الحمل %')
    db_count = fields.Integer('القواعد')

    @api.model
    def _cron_cleanup(self, days=30):
        limit = fields.Datetime.now() - timedelta(days=days)
        self.search([('create_date', '<', limit)]).unlink()
