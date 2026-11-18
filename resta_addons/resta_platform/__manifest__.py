{
    'name': 'منصة ريستا',
    'summary': 'منصة واحدة لمشاريع الساس والكاستم: سيرفرات Google Cloud، إنشاء المطاعم، '
               'الفوترة والإيقاف التلقائي، النسخ الاحتياطي في 3 أماكن، التحديثات، والأداء',
    'description': """
منصة ريستا - Resta Platform (Basic Solution)
============================================
* المشاريع: ساس (قاعدة على سيرفر مشترك من قالب) وكاستم (سيرفر خاص من مستودع العميل).
* السيرفرات: إنشاء سيرفرات Google Cloud تلقائياً، وتسجيلها، ومراقبتها.
* الربط: كل سيرفر عليه وكيل (resta-agent) يستقبل أوامر موقّعة.
* الفوترة: فواتير شهرية من الباقة، وإيقاف تلقائي عند التأخر، وتشغيل بعد السداد.
* النسخ الاحتياطي: على السيرفر + Google Cloud Storage + مزوّد ثالث.
* الأداء: المعالج والذاكرة والقرص وحجم كل قاعدة، وفتح سيرفر جديد عند الامتلاء.
* التسجيل الذاتي: واجهة لموقع resta.sa تنشئ المطعم مباشرة.
""",
    'version': '19.0.1.0.0',
    'category': 'Services/Resta',
    'author': 'Basic Solution',
    'website': 'https://resta.sa',
    'license': 'LGPL-3',
    'depends': ['mail', 'account'],
    'data': [
        'security/resta_security.xml',
        'security/ir.model.access.csv',
        'data/ir_sequence_data.xml',
        'data/ir_cron_data.xml',
        'data/mail_template_data.xml',
        'data/resta_plan_data.xml',
        'views/resta_operation_views.xml',
        'views/resta_server_views.xml',
        'views/resta_project_views.xml',
        'views/resta_plan_views.xml',
        'views/account_move_views.xml',
        'views/res_config_settings_views.xml',
        'wizard/resta_restore_wizard_views.xml',
        'views/menus.xml',
    ],
    'installable': True,
    'application': True,
}
