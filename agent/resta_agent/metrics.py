"""أرقام الأداء: المعالج والذاكرة والقرص وحجم كل قاعدة وحالة أودو."""
import os
import shutil
import time
from pathlib import Path

import psutil

from . import __version__


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def collect(settings, runner, routing) -> dict:
    disk_path = Path(settings.root) if Path(settings.root).exists() else Path("/")
    du = shutil.disk_usage(disk_path)
    mem = psutil.virtual_memory()
    load1, load5, load15 = os.getloadavg()
    cpus = psutil.cpu_count() or 1
    cpu = psutil.cpu_percent(interval=1)

    rows = runner.sql(
        "SELECT datname, pg_database_size(datname), datallowconn FROM pg_database "
        "WHERE datname NOT IN ('postgres','template0','template1') ORDER BY datname"
    )
    suspended = set(routing._load()["suspended"])
    databases = []
    for name, size, allowconn in rows:
        databases.append({
            "name": name,
            "db_size_mb": round(int(size) / 1048576, 1),
            "filestore_mb": round(_dir_size(settings.filestore_path(name)) / 1048576, 1)
            if not name.startswith("tpl_") else 0,
            "template": name.startswith("tpl_"),
            "suspended": name in suspended,
            "connections_allowed": allowconn == "t",
        })

    stacks = {st.name: runner.container_health(f"odoo-{st.name}") for st in settings.stacks}
    disk_percent = round(du.used * 100 / du.total, 1)
    return {
        "server_id": settings.server_id,
        "agent_version": __version__,
        "time": int(time.time()),
        "cpu_percent": cpu,
        "load_1": round(load1, 2),
        "load_5": round(load5, 2),
        "load_percent": round(min(100.0, load5 * 100 / cpus), 1),
        "cpu_count": cpus,
        "ram_percent": mem.percent,
        "ram_total_gb": round(mem.total / 1073741824, 1),
        "disk_percent": disk_percent,
        "disk_total_gb": round(du.total / 1073741824, 1),
        "disk_free_gb": round(du.free / 1073741824, 1),
        "databases": databases,
        "stacks": stacks,
    }
