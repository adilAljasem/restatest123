"""تجهيز السيرفر أول مرة (يشغّله سكربت الإقلاع في Google Cloud).

1) يحفظ الإعدادات القادمة من المنصة.
2) يكتب ملفات Docker وnginx والشهادة.
3) يشغّل قاعدة البيانات وnginx.
4) يجهّز طبقات أودو (main، وstaging للسيرفرات الخاصة).
5) يجهّز مستودعات النسخ الاحتياطي الثلاثة.
بناء القوالب يتم بعدها كعمليات من المنصة عشان تظهر لك في الشاشة.
"""
import logging
import secrets
import time
from pathlib import Path

from . import templates
from .config import Settings
from .ops import Operations
from .routing import Routing
from .runner import Runner, DB_CONTAINER

_logger = logging.getLogger(__name__)


def _pg_tuning(total_ram_gb: float) -> dict:
    shared = max(1, int(total_ram_gb * 0.25))
    cache = max(2, int(total_ram_gb * 0.5))
    return {"SHARED_BUFFERS": f"{shared}GB", "EFFECTIVE_CACHE": f"{cache}GB",
            "MAX_CONNECTIONS": 300}


def write_base(settings: Settings, ram_gb: float):
    root = Path(settings.root)
    for sub in ("base", "stacks", "data/postgres", "data/odoo", "backups/work", "backups/restic",
                "nginx/resta", "nginx/certs", "agent"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    values = {"ROOT": str(root), "DB_USER": settings.db_user, "DB_PASSWORD": settings.db_password}
    values.update(_pg_tuning(ram_gb))
    templates.write("base_compose.yml", root / "base" / "compose.yml", values, mode=0o600)
    templates.write("nginx_resta.conf", root / "nginx" / "conf.d" / "resta.conf",
                    {"BASE_DOMAIN": settings.base_domain.replace(".", r"\.")})
    templates.write("suspended.html", root / "nginx" / "resta" / "resta-suspended.html", {})
    certs = root / "nginx" / "certs"
    (certs / "origin.pem").write_text(settings.origin_cert)
    (certs / "origin.key").write_text(settings.origin_key)
    (certs / "origin.key").chmod(0o600)
    Routing(settings).render()


def wait_db(runner: Runner, timeout=180):
    start = time.time()
    while time.time() - start < timeout:
        proc = runner.run(["docker", "exec", DB_CONTAINER, "pg_isready", "-U", "odoo", "-h", "127.0.0.1"], check=False)
        if proc.returncode == 0:
            return True
        time.sleep(3)
    raise RuntimeError("database container did not become ready")


def init_backup_repos(settings: Settings, runner: Runner) -> dict:
    status = {}
    for repo in settings.backup_repos:
        proc = runner.restic(repo, "cat", "config", check=False)
        if proc.returncode == 0:
            status[repo] = "exists"
            continue
        proc = runner.restic(repo, "init", check=False)
        status[repo] = "created" if proc.returncode == 0 else proc.stderr.decode(errors="replace")[-300:]
    return status


def run_setup(data: dict, config_path: str, ram_gb: float) -> dict:
    data.setdefault("db_password", secrets.token_urlsafe(24))
    data.setdefault("master_password", secrets.token_urlsafe(24))
    settings = Settings.from_dict(data)
    settings.save(config_path)
    runner = Runner(settings)
    write_base(settings, ram_gb)
    runner.base_compose("up", "-d")
    wait_db(runner)
    (Path(settings.root) / "data" / "odoo").mkdir(parents=True, exist_ok=True)
    ops = Operations(settings, runner)
    stacks = []
    for st in list(settings.stacks):
        res = ops.create_stack(st.name, st.repo_url, st.branch, st.routes)
        stacks.append(res)
    repos = init_backup_repos(settings, runner)
    settings.save(config_path)
    return {"stacks": stacks, "backup_repos": repos}
