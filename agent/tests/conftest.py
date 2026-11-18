import os
import subprocess
from pathlib import Path

import pytest

from resta_agent.config import Settings
from resta_agent.ops import Operations

ODOO_ROOT = os.environ.get("RESTA_TEST_ODOO", "/home/claude/env/odoo19")
PG_ENV = {"PGHOST": os.environ.get("PGHOST", "/tmp"), "PGPORT": "5432", "PGUSER": "odoo", "PGPASSWORD": "odoo"}
SECRET = "s" * 40


def pg_available():
    try:
        subprocess.run(["psql", "-X", "-At", "-c", "select 1", "-d", "postgres"], env={**os.environ, **PG_ENV},
                       check=True, capture_output=True, timeout=5)
        return True
    except Exception:
        return False


@pytest.fixture
def settings(tmp_path):
    data_dir = tmp_path / "data"
    conf = tmp_path / "odoo.conf"
    conf.write_text(
        "[options]\n"
        f"db_host = {PG_ENV['PGHOST']}\n"
        "db_port = 5432\n"
        "db_user = odoo\ndb_password = odoo\n"
        f"addons_path = {ODOO_ROOT}/addons,{ODOO_ROOT}/odoo/addons\n"
        f"data_dir = {data_dir}\n"
        "log_level = warn\n"
    )
    s = Settings.from_dict({
        "server_id": 1, "server_name": "test-cell", "agent_secret": SECRET,
        "callback_url": "http://127.0.0.1:9/cb", "mode": "local", "root": str(tmp_path / "root"),
        "pg_env": PG_ENV, "odoo_cmd": ["python3", f"{ODOO_ROOT}/odoo-bin"], "odoo_conf": str(conf),
        "filestore_root": str(data_dir / "filestore"), "restic_password": "test-pass",
        "backup_repos": [str(tmp_path / "repo-local"), str(tmp_path / "repo-remote")],
        "stacks": [{"name": "main", "repo_url": "", "branch": "main", "routes": ["*"]}],
    })
    s.agent_dir.mkdir(parents=True, exist_ok=True)
    return s


@pytest.fixture
def ops(settings):
    return Operations(settings)


requires_pg = pytest.mark.skipif(not pg_available(), reason="PostgreSQL not available")
requires_odoo = pytest.mark.skipif(not Path(ODOO_ROOT, "odoo-bin").exists(), reason="Odoo source not available")
