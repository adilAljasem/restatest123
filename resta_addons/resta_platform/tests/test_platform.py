from datetime import timedelta

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import tagged

from .common import RestaCommon


@tagged('post_install', '-at_install', 'resta')
class TestRestaPlatform(RestaCommon):

    # ------------------------------------------------------------ قواعد الرابط
    def test_code_validation(self):
        for bad in ('ab', 'Kaffa', 'admin', 'tpl_x', 'x' * 40, 'a b', 'shop-staging'):
            with self.assertRaises(ValidationError, msg=bad):
                with self.env.cr.savepoint():
                    self._project(code=bad)
        self.assertEqual(self._project(code='kaffa-2').url, 'https://kaffa-2.restaerp.com')

    # ------------------------------------------------------------ اختيار الخلية
    def test_select_cell_least_loaded(self):
        busy = self.cell
        self.env['resta.server'].search([('id', '!=', busy.id)]).write({'state': 'retired'})
        busy.load_percent = 60
        free = self.cell.copy({'name': 'cell-02', 'state': 'ready', 'registered': True,
                               'internal_ip': '10.0.0.6', 'load_percent': 20})
        self.assertEqual(self.env['resta.server'].select_cell(), free)
        free.load_percent = 90  # فوق حد الامتلاء
        self.assertEqual(self.env['resta.server'].select_cell(), busy)
        busy.capacity = 0
        self.assertFalse(self.env['resta.server'].select_cell())

    # ------------------------------------------------------------ إنشاء مطعم ساس كامل
    def test_saas_provision_flow(self):
        project = self._project()
        with self.fake_infra() as calls:
            project.action_provision()
            self.assertEqual(project.state, 'provisioning')
            self.assertEqual(project.server_id, self.cell)
            self.assertTrue(project.trial_end)
            op = project.operation_ids
            self.assertEqual(op.op_type, 'create_db')
            params = op.params()
            self.assertEqual(params['template'], 'tpl_dinein')
            self.assertEqual(params['db'], 'kaffa')
            self.assertEqual(params['base_url'], 'https://kaffa.restaerp.com')
            self.assertTrue(params['admin_password'])
            self.env['resta.operation']._cron_process()
            self.assertEqual(op.state, 'running')
            self.assertEqual(calls[-1][2], '/jobs')
            self._finish(op, {'db': 'kaffa', 'size_mb': 42.5})
        self.assertEqual(project.state, 'active')
        self.assertEqual(project.db_size_mb, 42.5)
        self.assertFalse(project.sudo().initial_password, 'كلمة المرور تُمسح بعد إرسال الإيميل')
        mail = self.env['mail.mail'].sudo().search([('model', '=', 'resta.project'), ('res_id', '=', project.id)],
                                                   limit=1)
        self.assertTrue(mail, 'إيميل الترحيب')
        self.assertIn('kaffa.restaerp.com', mail.body_html)
        self.assertIn('كلمة المرور المؤقتة', mail.body_html)

    def test_create_waits_for_template(self):
        tpl_op = self.env['resta.operation'].create({
            'server_id': self.cell.id, 'op_type': 'build_template',
            'payload_dict': {'name': 'tpl_dinein', 'modules': ['contacts']}})
        project = self._project()
        with self.fake_infra():
            project.action_provision()
            create_op = project.operation_ids
            self.assertEqual(create_op.depends_on_id, tpl_op)
            self.assertFalse(create_op._can_send())
            self.env['resta.operation']._cron_process()  # يرسل القالب فقط
            self.assertEqual(tpl_op.state, 'running')
            self.assertEqual(create_op.state, 'pending')
            self._finish(tpl_op, {'template': 'tpl_dinein'})
            self.assertEqual(tpl_op.state, 'done')
            self.assertNotEqual(tpl_op.name, create_op.name)
            self.assertTrue(create_op._can_send())

    def test_retry_then_fail(self):
        project = self._project()
        with self.fake_infra(fail_agent=True):
            project.action_provision()
            op = project.operation_ids
            op.next_try = fields.Datetime.now() - timedelta(minutes=1)
            self.env['resta.operation']._cron_process()
            self.assertEqual(op.state, 'pending')
            self.assertEqual(op.attempts, 1)
            self.assertGreater(op.next_try, fields.Datetime.now())
            for _i in range(6):
                op.next_try = fields.Datetime.now() - timedelta(minutes=1)
                self.env['resta.operation']._cron_process()
        self.assertEqual(op.state, 'failed')
        self.assertEqual(project.state, 'error')
        op.action_retry()
        self.assertEqual(op.state, 'pending')

    # ------------------------------------------------------------ تسجيل السيرفر
    def test_registration(self):
        server = self.env['resta.server'].create({'name': 'cell-09', 'kind': 'cell', 'provider': 'manual'})
        server.sudo().write({'register_token': 'tok-123', 'agent_secret': 'b' * 64})
        with self.assertRaises(UserError):
            server.register_from_agent('wrong', '10.0.0.9')
        payload = server.register_from_agent('tok-123', '10.0.0.9')
        self.assertEqual(payload['agent_secret'], 'b' * 64)
        self.assertEqual(payload['bind_host'], '10.0.0.9')
        self.assertEqual(payload['callback_url'], 'http://10.0.0.2:8069/resta/agent/callback')
        self.assertEqual(payload['stacks'][0]['name'], 'main')
        self.assertIn('gs:resta-backups-test:/cell-09', payload['backup_repos'])
        self.assertEqual(payload['workers'], 8)
        self.assertFalse(server.sudo().register_token, 'الرمز لمرة واحدة')
        self.assertTrue(server.registered)
        tpl_ops = server.operation_ids.filtered(lambda o: o.op_type == 'build_template')
        self.assertEqual(len(tpl_ops), 2, 'قالب لكل نسخة')
        modules = {op.params()['name']: op.params()['modules'] for op in tpl_ops}
        self.assertEqual(modules['tpl_dinein'], ['contacts', 'point_of_sale'])
        self.assertEqual(modules['tpl_fastfood'], ['resta_edition_qsr'], 'الافتراضي: نسخة ريستا للوجبات السريعة')
        with self.assertRaises(UserError):
            server.register_from_agent('tok-123', '10.0.0.9')

    # ------------------------------------------------------------ الأداء والامتلاء
    def test_health_marks_full_and_updates_sizes(self):
        project = self._project(server_id=self.cell.id, state='active')
        health = {'cpu_percent': 50, 'ram_percent': 82, 'disk_percent': 30, 'load_percent': 40,
                  'agent_version': '1.0.0',
                  'databases': [{'name': 'kaffa', 'db_size_mb': 120.5, 'filestore_mb': 33},
                                {'name': 'tpl_dinein', 'template': True}]}
        with self.fake_infra(health=health):
            self.cell._refresh_health()
        self.assertEqual(self.cell.load_percent, 82)
        self.assertEqual(self.cell.state, 'full')
        self.assertEqual(self.cell.templates_ready, 'tpl_dinein')
        self.assertEqual(project.db_size_mb, 120.5)
        self.assertEqual(len(self.cell.metric_ids), 1)
        health.update({'ram_percent': 40})
        with self.fake_infra(health=health):
            self.cell._refresh_health()
        self.assertEqual(self.cell.state, 'ready')

    # ------------------------------------------------------------ الفوترة والإيقاف التلقائي
    def test_invoice_amount(self):
        project = self._project(server_id=self.cell.id, state='active', branches=3,
                                next_invoice_date=fields.Date.today())
        lines = self.plan.invoice_lines(project)
        self.assertEqual(sum(l['quantity'] * l['price_unit'] for l in lines), 499 + 2 * 299)
        move = project._create_invoice()
        self.assertEqual(move.resta_project_id, project)
        self.assertAlmostEqual(move.amount_untaxed, 1097)
        self.assertEqual(project.next_invoice_date, fields.Date.today() + timedelta(days=0) + _month())

    def test_overdue_suspends_and_payment_resumes(self):
        project = self._project(server_id=self.cell.id, state='active', next_invoice_date=fields.Date.today())
        move = project._create_invoice()
        old = fields.Date.today() - timedelta(days=20)
        move.write({'invoice_date': old, 'invoice_date_due': old})
        move.action_post()
        with self.fake_infra():
            self.env['resta.project']._check_overdue()
            suspend = project.operation_ids.filtered(lambda o: o.op_type == 'suspend')
            self.assertTrue(suspend)
            self.assertEqual(project.suspend_reason, 'billing')
            self.env['resta.operation']._cron_process()
            self._finish(suspend, {'db': 'kaffa', 'suspended': True})
            self.assertEqual(project.state, 'suspended')
            # السداد ← تشغيل تلقائي
            self.env['account.payment.register'].with_context(
                active_model='account.move', active_ids=move.ids).create({})._create_payments()
            self.assertIn(move.payment_state, ('paid', 'in_payment'))
            resume = project.operation_ids.filtered(lambda o: o.op_type == 'resume')
            if not resume:  # بعض الإعدادات تخلي الحالة in_payment بدون استدعاء الخطاف
                self.env['resta.project']._check_overdue()
                resume = project.operation_ids.filtered(lambda o: o.op_type == 'resume')
            self.assertTrue(resume)
            self.env['resta.operation']._cron_process()
            self._finish(resume, {'db': 'kaffa'})
        self.assertEqual(project.state, 'active')
        self.assertFalse(project.suspend_reason)

    # ------------------------------------------------------------ التسجيل الذاتي
    def test_signup(self):
        with self.fake_infra():
            res = self.env['resta.project'].signup({
                'brand_name': 'برجر الحي', 'code': 'burger-hay', 'email': 'Owner@Burger.sa',
                'contact_name': 'أبو فهد', 'phone': '+966500000000', 'edition': 'dinein', 'branches': 2})
        self.assertEqual(res['url'], 'https://burger-hay.restaerp.com')
        self.assertEqual(res['state'], 'provisioning')
        project = self.env['resta.project'].browse(res['project_id'])
        self.assertEqual(project.admin_email, 'owner@burger.sa')
        self.assertEqual(project.plan_id, self.plan)
        with self.assertRaises(ValidationError):
            self.env['resta.project'].signup({'brand_name': 'x', 'code': 'burger-hay', 'email': 'a@b.sa'})

    # ------------------------------------------------------------ الكاستم
    def test_custom_requires_repo_and_creates_dedicated_server(self):
        plan = self.env.ref('resta_platform.plan_custom_standard')
        project = self._project(code='emkan', project_type='custom', plan_id=plan.id)
        with self.assertRaises(UserError):
            project.action_provision()
        project.repo_url = 'https://github.com/walsoodi/emkan.git'
        server = self.env['resta.server'].create({'name': 'emkan', 'kind': 'dedicated', 'provider': 'manual',
                                                 'state': 'ready', 'registered': True,
                                                 'internal_ip': '10.0.0.8', 'agent_secret': 'c' * 64})
        project.server_id = server
        with self.fake_infra():
            project.action_provision()
        op = project.operation_ids
        self.assertEqual(op.params()['modules'], ['base'])
        stacks = server._stacks()
        self.assertEqual([s['name'] for s in stacks], ['main', 'staging'])
        self.assertEqual(stacks[1]['routes'], ['emkan-staging'])

    def test_snapshots_and_restore(self):
        project = self._project()
        with self.fake_infra():
            project.action_provision()
            self.env['resta.operation']._cron_process()
            self._finish(project.operation_ids, {'db': 'kaffa', 'size_mb': 10})
            # قائمة النسخ
            project.action_list_snapshots()
            op = project.operation_ids.filtered(lambda o: o.op_type == 'snapshots')
            self.env['resta.operation']._cron_process()
            self._finish(op, {'db': 'kaffa', 'snapshots': [
                {'id': 'a1b2c3d4', 'time': '2026-11-17T23:30:05Z', 'tags': ['db:kaffa', 'nightly']}]})
            self.assertEqual(project.snapshots_json[0]['id'], 'a1b2c3d4')
            Wizard = self.env['resta.restore.wizard']
            w = Wizard.create({'project_id': project.id, 'mode': 'staging', 'snapshot': 'a1b2c3d4'})
            self.assertIn('a1b2c3d4', w.available_html)
            # كنسخة تجربة
            w.action_restore()
            op = project.operation_ids.filtered(lambda o: o.op_type == 'restore')
            self.assertEqual(op.params()['target'], 'kaffa-staging')
            self.assertTrue(op.params()['neutralize'])
            self.env['resta.operation']._cron_process()
            self._finish(op, {'db': 'kaffa-staging', 'from_snapshot': 'a1b2c3d4', 'old_db': None})
            self.assertEqual(project.staging_db, 'kaffa-staging')
            with self.assertRaises(UserError):
                Wizard.create({'project_id': project.id, 'mode': 'staging'}).action_restore()
            # الاستبدال يحتاج تأكيد بالكود
            with self.assertRaises(UserError):
                Wizard.create({'project_id': project.id, 'mode': 'replace', 'confirm_code': 'x'}).action_restore()
            with self.assertRaises(UserError):
                Wizard.create({'project_id': project.id, 'mode': 'replace', 'snapshot': 'a;b',
                               'confirm_code': 'kaffa'}).action_restore()
            Wizard.create({'project_id': project.id, 'mode': 'replace', 'confirm_code': 'kaffa'}).action_restore()
            op = project.operation_ids.filtered(lambda o: o.op_type == 'restore' and o.state == 'pending')
            self.assertEqual(op.params()['snapshot'], 'latest')
            self.assertTrue(op.params()['swap'])


    def test_deploy_waits_for_backup(self):
        project = self._project()
        with self.fake_infra():
            project.action_provision()
            self.env['resta.operation']._cron_process()
            self._finish(project.operation_ids, {'db': 'kaffa', 'size_mb': 10})
            project.action_deploy()
            backup = project.operation_ids.filtered(lambda o: o.op_type == 'backup')
            deploy = project.operation_ids.filtered(lambda o: o.op_type == 'deploy')
            self.assertEqual(deploy.depends_on_id, backup)
            self.env['resta.operation']._cron_process()
            self.assertEqual(deploy.state, 'pending', 'التحديث ينتظر النسخة')
            self._finish(backup, state='failed', error='disk full')
            self.env['resta.operation']._cron_process()
            self.assertEqual(deploy.state, 'failed', 'لا تحديث بدون نسخة')

    def test_queued_when_cells_full(self):
        self.env['resta.server'].search([('id', '!=', self.cell.id)]).write({'state': 'retired'})
        self.cell.capacity = 0
        project = self._project()
        with self.fake_infra():
            project.action_provision()
            self.assertEqual(project.state, 'queued', 'العميل ما يُرفض، ينتظر سيرفر')
            self.assertFalse(project.operation_ids)
            self.assertFalse(project.trial_end, 'التجربة تبدأ مع التجهيز الفعلي')
            self.assertTrue(self.cell.activity_ids, 'تنبيه للمدير لفتح خلية')
            self.cell.capacity = 10
            self.env['resta.project']._provision_queued()
        self.assertEqual(project.state, 'provisioning')
        self.assertEqual(project.operation_ids.op_type, 'create_db')
        self.assertTrue(project.trial_end)

def _month():
    from dateutil.relativedelta import relativedelta
    return relativedelta(months=1)
