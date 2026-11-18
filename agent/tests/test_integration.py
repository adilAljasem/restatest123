"""اختبار حقيقي: أودو 19 + PostgreSQL + restic (بدون Docker)."""
import uuid

import pytest

from resta_agent.ops import OperationError

from .conftest import requires_odoo, requires_pg

pytestmark = [requires_pg, requires_odoo]


def _q(ops, sql, db):
    return ops.r.sql(sql, db=db)


def test_full_lifecycle(ops, settings):
    tag = uuid.uuid4().hex[:6]
    tpl = f"tpl_t{tag}"
    db = f"rest{tag}"
    copy_db = f"rest{tag}-copy"
    stg = f"rest{tag}-staging"
    try:
        # 1) قالب
        res = ops.build_template(tpl, ["contacts"])
        assert res["template"] == tpl
        assert not ops.r.connections_allowed(tpl)

        # 2) مطعم جديد من القالب
        res = ops.create_db(db, template=tpl, company_name="مطعم الاختبار", admin_login="owner@test.sa",
                            admin_password="Pass#12345", base_url=f"https://{db}.restaerp.com")
        assert res["db"] == db
        assert _q(ops, "select name from res_company order by id limit 1", db)[0][0] == "مطعم الاختبار"
        assert _q(ops, "select login from res_users where id=2", db)[0][0] == "owner@test.sa"
        uuid_tpl = None
        uuid_db = _q(ops, "select value from ir_config_parameter where key='database.uuid'", db)[0][0]
        assert uuid_db
        with pytest.raises(OperationError):
            ops.create_db(db, template=tpl)

        # 3) إيقاف وتشغيل
        ops.suspend(db)
        assert not ops.r.connections_allowed(db)
        assert ops.routing.is_suspended(db)
        ops.resume(db)
        assert ops.r.connections_allowed(db)

        # 4) نسخ احتياطي في مكانين (حتى لو القاعدة موقوفة)
        for repo in settings.backup_repos:
            ops.r.restic(repo, "init")
        ops.suspend(db)
        bk = ops.backup(db, tag="test")
        assert bk["copies"] == 2 and not bk["errors"]
        assert not ops.r.connections_allowed(db)
        ops.resume(db)
        snaps = ops.snapshots(db)["snapshots"]
        assert len(snaps) == 1

        # 5) استرجاع كنسخة جديدة (محيّدة)
        snap = list(bk["snapshots"].values())[0]
        rs = ops.restore(db, snap, target=copy_db)
        assert rs["neutralized"] is True
        uuid_copy = _q(ops, "select value from ir_config_parameter where key='database.uuid'", copy_db)[0][0]
        assert uuid_copy != uuid_db

        # 6) نسخة تجربة من قاعدة شغالة
        dp = ops.duplicate(db, stg)
        assert dp["neutralized"] is True
        url = _q(ops, "select value from ir_config_parameter where key='web.base.url'", stg)[0][0]
        assert url == f"https://{stg}.restaerp.com"

        # 7) استرجاع بالاستبدال (حالة كارثة)
        rs2 = ops.restore(db, "latest", swap=True)
        assert rs2["old_db"] and ops.r.db_exists(rs2["old_db"])
        assert _q(ops, "select value from ir_config_parameter where key='database.uuid'", db)[0][0] == uuid_db
        ops.drop(rs2["old_db"], backup_first=False, confirm=rs2["old_db"])

        # 8) حذف مع نسخة قبلها
        with pytest.raises(OperationError):
            ops.drop(copy_db, confirm="wrong")
        dr = ops.drop(copy_db, confirm=copy_db)
        assert dr["dropped"] and dr["backup"]["copies"] == 2
        assert not ops.r.db_exists(copy_db)
        # نسخة مطعم آخر لا تُسترجع على هذا المطعم
        foreign = list(dr["backup"]["snapshots"].values())[0]
        with pytest.raises(OperationError):
            ops.restore(db, foreign, target=f"{db}-bad")
        with pytest.raises(OperationError):
            ops.restore(db, "abc;rm", target=f"{db}-bad")
        assert not ops.r.db_exists(f"{db}-bad")

        # 9) أرقام الأداء
        h = ops.health()
        names = {d["name"] for d in h["databases"]}
        assert db in names and tpl in names
        assert 0 <= h["disk_percent"] <= 100
    finally:
        for name in (copy_db, stg, db, tpl):
            ops.r.sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
