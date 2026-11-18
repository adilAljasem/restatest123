"""Accounting and journals open from the first day; level 2 opens the profitability analysis;
the owner gets every business right of the installed apps (creating products was refused).

Texts are changed only where the Resta team kept the original ones.
"""
import json

from odoo import SUPERUSER_ID, api

ACCOUNTING = {
    "name": ("المحاسبة والتقارير المالية", "المحاسبة والدفاتر"),
    "teaser": ("شوف ربحك الحقيقي: أرباح وخسائر، مصروفات، وهامش.",
               "مفتوحة من أول يوم: الدفاتر وطرق الدفع والضرائب والفواتير لنقاط البيع."),
}
DISCIPLINE = {
    "reward": ("فتح المحاسبة والتقارير المالية", "فتح تحليل الربحية: ربح كل فرع، التدفق النقدي، والموازنات"),
    "celebration": (
        "مطعمك صار منضبط: جرد كل أسبوع، هدر تحت السيطرة، وكاشير مطابق. فتحنا لك المحاسبة، الحين تشوف ربحك الحقيقي.",
        "مطعمك صار منضبط: جرد كل أسبوع، هدر تحت السيطرة، وكاشير مطابق. فتحنا لك تحليل الربحية، الحين تشوف ربحك "
        "الحقيقي لكل فرع."),
}


def _replace_texts(cr, table, rec_id, changes):
    for column, (old, new) in changes.items():
        cr.execute("SELECT %s FROM %s WHERE id = %%s" % (column, table), [rec_id])  # noqa: S608 (fixed names)
        row = cr.fetchone()
        value = row and row[0]
        if not isinstance(value, dict) or any(v != old for v in value.values()):
            continue
        cr.execute("UPDATE %s SET %s = %%s::jsonb WHERE id = %%s" % (table, column),  # noqa: S608
                   [json.dumps({lang: new for lang in value}), rec_id])


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    accounting = env.ref("resta_growth.feature_accounting", raise_if_not_found=False)
    foundation = env.ref("resta_growth.level_foundation", raise_if_not_found=False)
    discipline = env.ref("resta_growth.level_discipline", raise_if_not_found=False)
    if accounting and foundation:
        _replace_texts(cr, "resta_growth_feature", accounting.id, ACCOUNTING)
        accounting.invalidate_recordset()
        accounting.write({"open_from_start": True, "level_id": foundation.id, "sequence": 0})
    if discipline:
        _replace_texts(cr, "resta_growth_level", discipline.id, DISCIPLINE)
        discipline.invalidate_recordset()
    Feature = env["resta.growth.feature"]
    Feature._link_optional_menus()
    Feature._open_from_start()
    env["res.users"]._resta_sync_owner_groups()
