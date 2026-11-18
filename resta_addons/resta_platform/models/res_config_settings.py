from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # Google Cloud
    resta_gcp_project = fields.Char('مشروع Google Cloud', config_parameter='resta.gcp_project')
    resta_gcp_region = fields.Char('المنطقة', config_parameter='resta.gcp_region', default='me-central2')
    resta_gcp_zone = fields.Char('النطاق', config_parameter='resta.gcp_zone', default='me-central2-a')
    resta_gcp_network = fields.Char('الشبكة', config_parameter='resta.gcp_network',
                                    default='global/networks/default')
    resta_gcp_credentials = fields.Char('مفتاح حساب الخدمة (JSON)', config_parameter='resta.gcp_credentials',
                                        help='اتركه فارغاً إذا المنصة على سيرفر Google مربوط بحساب resta-platform')
    resta_cell_sa_email = fields.Char('حساب خدمة السيرفرات', config_parameter='resta.cell_sa_email')
    resta_cell_machine_type = fields.Char('نوع سيرفر الخلية', config_parameter='resta.cell_machine_type',
                                          default='e2-standard-4')
    resta_cell_disk_gb = fields.Integer('قرص الخلية (GB)', config_parameter='resta.cell_disk_gb', default=200)
    resta_cell_capacity = fields.Integer('علامات في الخلية', config_parameter='resta.cell_capacity', default=10)

    # الربط
    resta_platform_internal_url = fields.Char('رابط المنصة الداخلي', config_parameter='resta.platform_internal_url',
                                              help='مثال: http://10.212.0.2:8069 — السيرفرات تتواصل مع المنصة عليه')
    resta_base_domain = fields.Char('نطاق المطاعم', config_parameter='resta.base_domain', default='restaerp.com')
    resta_cf_token = fields.Char('مفتاح Cloudflare', config_parameter='resta.cf_token')
    resta_cf_zone_id = fields.Char('Zone ID', config_parameter='resta.cf_zone_id')
    resta_origin_cert = fields.Char('شهادة Origin', config_parameter='resta.origin_cert')
    resta_origin_key = fields.Char('مفتاح شهادة Origin', config_parameter='resta.origin_key')

    # الكود
    resta_git_token = fields.Char('مفتاح GitHub (قراءة فقط)', config_parameter='resta.git_token')
    resta_saas_repo = fields.Char('مستودع نسخة ريستا', config_parameter='resta.saas_repo',
                                  default='https://github.com/walsoodi/restash.git')
    resta_saas_branch = fields.Char('فرع الإنتاج', config_parameter='resta.saas_branch', default='staging',
                                    help='يتغير إلى main بعد اعتماد الإصدار')
    resta_agent_repo = fields.Char('مستودع الوكيل', config_parameter='resta.agent_repo',
                                   default='https://github.com/walsoodi/restash.git')
    resta_agent_branch = fields.Char('فرع الوكيل', config_parameter='resta.agent_branch', default='staging')
    resta_enterprise_repo = fields.Char('مستودع Enterprise', config_parameter='resta.enterprise_repo')
    resta_enterprise_branch = fields.Char('فرع Enterprise', config_parameter='resta.enterprise_branch', default='19.0')
    resta_modules_dinein = fields.Char('موديولات نسخة الصالة', config_parameter='resta.modules_dinein',
                                       default='resta_edition_dinein')
    resta_modules_fastfood = fields.Char('موديولات نسخة الوجبات السريعة', config_parameter='resta.modules_fastfood',
                                         default='resta_edition_qsr')

    # النسخ الاحتياطي
    resta_restic_password = fields.Char('كلمة سر التشفير', config_parameter='resta.restic_password')
    resta_backup_gcs_bucket = fields.Char('Bucket في Google Cloud', config_parameter='resta.backup_gcs_bucket')
    resta_backup_s3_url = fields.Char('تخزين المزوّد الثالث (S3)', config_parameter='resta.backup_s3_url',
                                      help='مثال: s3:https://eu2.contabostorage.com/resta-backups')
    resta_backup_s3_key = fields.Char('مفتاح التخزين الثالث (Access Key)', config_parameter='resta.backup_s3_key')
    resta_backup_s3_secret = fields.Char('سر التخزين الثالث (Secret Key)', config_parameter='resta.backup_s3_secret')

    # التشغيل والفوترة
    resta_load_threshold = fields.Integer('حد الامتلاء %', config_parameter='resta.load_threshold', default=70)
    resta_auto_provision = fields.Boolean('افتح سيرفر جديد تلقائياً عند الامتلاء',
                                          config_parameter='resta.auto_provision')
    resta_trial_days = fields.Integer('أيام التجربة المجانية', config_parameter='resta.trial_days', default=14)
    resta_grace_days = fields.Integer('مهلة السداد قبل الإيقاف (أيام)', config_parameter='resta.grace_days',
                                      default=7)
    resta_auto_post_invoices = fields.Boolean('ترحيل الفواتير تلقائياً', config_parameter='resta.auto_post_invoices')
    resta_signup_key = fields.Char('مفتاح التسجيل لموقع resta.sa', config_parameter='resta.signup_key')
