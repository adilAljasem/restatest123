"""ملفات Docker وnginx وأودو تُكتب بدون قيم ناقصة."""
import yaml

from resta_agent.config import Settings, StackConfig
from resta_agent.ops import Operations
from resta_agent.setup import write_base

from .conftest import SECRET


def test_render_server_files(tmp_path):
    s = Settings.from_dict({
        "server_id": 3, "server_name": "cell-03", "agent_secret": SECRET, "callback_url": "http://10.0.0.2:8069/cb",
        "mode": "docker", "root": str(tmp_path), "db_password": "dbpass123", "master_password": "master123",
        "origin_cert": "CERT", "origin_key": "KEY", "base_domain": "restaerp.com", "workers": 8,
        "stacks": [{"name": "main", "repo_url": "https://github.com/walsoodi/resta.git", "branch": "main",
                    "routes": ["*"]}],
    })
    write_base(s, ram_gb=16)
    base = yaml.safe_load((tmp_path / "base" / "compose.yml").read_text())
    assert base["services"]["db"]["environment"]["POSTGRES_PASSWORD"] == "dbpass123"
    assert "shared_buffers=4GB" in base["services"]["db"]["command"]
    nginx = (tmp_path / "nginx" / "conf.d" / "resta.conf").read_text()
    assert r"\.restaerp\.com$" in nginx and "{{" not in nginx
    assert (tmp_path / "nginx" / "resta" / "resta-suspended.html").exists()
    assert (tmp_path / "nginx" / "certs" / "origin.key").read_text() == "KEY"

    ops = Operations(s)
    ops.write_stack_files(StackConfig(name="staging", repo_url="x", branch="staging"))
    d = tmp_path / "stacks" / "staging"
    compose = yaml.safe_load((d / "compose.yml").read_text())
    assert compose["services"]["odoo"]["container_name"] == "odoo-staging"
    conf = (d / "odoo.conf").read_text()
    assert "db_password = dbpass123" in conf and "workers = 2" in conf and "dbfilter = ^%d$" in conf
    assert "FROM odoo:19.0" in (d / "Dockerfile").read_text()
    ops.write_stack_files(StackConfig(name="main", repo_url="x", branch="main"))
    assert "workers = 8" in (tmp_path / "stacks" / "main" / "odoo.conf").read_text()


def test_docker_commands(tmp_path):
    s = Settings.from_dict({"server_id": 1, "server_name": "c", "agent_secret": SECRET, "callback_url": "x",
                            "mode": "docker", "root": str(tmp_path), "db_password": "p"})
    r = Operations(s).r
    assert r.pg_cmd("psql", "-c", "select 1")[:6] == ["docker", "exec", "-i", "-e", "PGPASSWORD=p", "resta-db"]
    cmd = r.odoo_oneoff("main", "shell", "-d", "kaffa")
    assert cmd[-6:] == ["shell", "-c", "/etc/odoo/odoo.conf", "-d", "kaffa"][-6:] or "shell" in cmd
    assert cmd.index("shell") < cmd.index("-c")
    assert r.git_url("https://github.com/walsoodi/resta.git") == "https://github.com/walsoodi/resta.git"
    s.git_token = "tok"
    assert r.git_url("https://github.com/walsoodi/resta.git").startswith("https://x-access-token:tok@github.com/")


def test_source_stack_uses_resta_image(tmp_path):
    """مستودع فيه أودو كامل + Dockerfile: تُبنى صورة ريستا منه بدل صورة أودو الرسمية."""
    s = Settings.from_dict({"server_id": 1, "server_name": "c", "agent_secret": SECRET, "callback_url": "x",
                            "mode": "docker", "root": str(tmp_path), "db_password": "p"})
    ops = Operations(s)
    st = StackConfig(name="main", repo_url="x", branch="staging")
    ops.write_stack_files(st)
    d = tmp_path / "stacks" / "main"
    assert not ops.is_source_stack("main") and (d / "Dockerfile").exists()
    # بعد سحب مشروع ريستا الكامل
    (d / "addons" / "odoo").mkdir(parents=True)
    (d / "addons" / "odoo" / "odoo-bin").write_text("#!/usr/bin/env python3\n")
    (d / "addons" / "Dockerfile").write_text("FROM ubuntu:24.04\n")
    ops.write_stack_files(st)
    assert ops.is_source_stack("main")
    compose = yaml.safe_load((d / "compose.yml").read_text())
    svc = compose["services"]["odoo"]
    assert svc["build"] == "./addons" and svc["entrypoint"] == ["/usr/bin/env"]
    assert svc["command"] == ["odoo", "-c", "/etc/odoo/odoo.conf"]
    assert not any("/mnt/addons" in v for v in svc["volumes"])
    assert not (d / "Dockerfile").exists()
    conf = (d / "odoo.conf").read_text()
    assert "addons_path = /opt/odoo/odoo/addons,/opt/odoo/resta_addons,/opt/odoo/bs_addons" in conf
    s.source_enterprise = True
    ops.write_stack_files(st)
    assert "/opt/odoo/odoo/addons,/opt/odoo/enterprise," in (d / "odoo.conf").read_text()
    # أمر تشغيل مؤقت لأودو يمر عبر نفس الخدمة
    cmd = ops.r.odoo_oneoff("main", "-d", "x", "-i", "base")
    assert cmd[cmd.index("run"):][:6] == ["run", "--rm", "--no-deps", "-T", "odoo", "odoo"]
