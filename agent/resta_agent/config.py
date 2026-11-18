"""إعدادات الوكيل.

تُكتب مرة واحدة عند إقلاع السيرفر من رد المنصة على طلب التسجيل،
وتُحفظ في /etc/resta/config.json بصلاحية 600.
"""
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = "/etc/resta/config.json"
DB_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
STACK_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")


class ConfigError(Exception):
    pass


def check_db_name(name: str) -> str:
    if not isinstance(name, str) or not DB_NAME_RE.match(name):
        raise ConfigError(f"invalid database name: {name!r}")
    return name


def check_stack_name(name: str) -> str:
    if not isinstance(name, str) or not STACK_NAME_RE.match(name):
        raise ConfigError(f"invalid stack name: {name!r}")
    return name


@dataclass
class StackConfig:
    name: str
    repo_url: str = ""
    branch: str = "main"
    routes: list = field(default_factory=list)


@dataclass
class Settings:
    server_id: int
    server_name: str
    agent_secret: str
    callback_url: str
    kind: str = "cell"                    # cell | dedicated
    mode: str = "docker"                  # docker | local (للاختبار)
    root: Path = Path("/opt/resta")
    bind_host: str = "0.0.0.0"
    bind_port: int = 8090
    base_domain: str = "restaerp.com"
    db_user: str = "odoo"
    db_password: str = ""
    master_password: str = ""
    workers: int = 8
    max_cron_threads: int = 2
    git_token: str = ""
    enterprise_repo_url: str = ""
    enterprise_branch: str = "19.0"
    source_enterprise: bool = False  # مشروع ريستا الكامل: إضافة مجلد enterprise (معطّل افتراضياً)
    stacks: list = field(default_factory=list)
    restic_password: str = ""
    backup_repos: list = field(default_factory=list)
    backup_env: dict = field(default_factory=dict)
    origin_cert: str = ""
    origin_key: str = ""
    default_lang: str = "ar_001"
    # وضع الاختبار المحلي
    pg_env: dict = field(default_factory=dict)
    odoo_cmd: list = field(default_factory=list)
    odoo_conf: str = ""
    filestore_root: str = ""

    # ---------------------------------------------------------------- paths
    @property
    def agent_dir(self) -> Path:
        return Path(self.root) / "agent"

    @property
    def jobs_db(self) -> Path:
        return self.agent_dir / "jobs.db"

    @property
    def work_dir(self) -> Path:
        return Path(self.root) / "backups" / "work"

    @property
    def nginx_dir(self) -> Path:
        return Path(self.root) / "nginx"

    @property
    def stacks_dir(self) -> Path:
        return Path(self.root) / "stacks"

    def filestore_path(self, db: str) -> Path:
        check_db_name(db)
        if self.filestore_root:
            return Path(self.filestore_root) / db
        return Path(self.root) / "data" / "odoo" / "filestore" / db

    def stack(self, name: str) -> StackConfig:
        for st in self.stacks:
            if st.name == name:
                return st
        raise ConfigError(f"unknown stack: {name}")

    # ---------------------------------------------------------------- io
    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        data = dict(data)
        stacks = [StackConfig(**s) if isinstance(s, dict) else s for s in data.pop("stacks", [])]
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        for key in unknown:
            data.pop(key)
        if "root" in data:
            data["root"] = Path(data["root"])
        settings = cls(**data)
        settings.stacks = stacks
        for st in settings.stacks:
            check_stack_name(st.name)
        if not settings.agent_secret or len(settings.agent_secret) < 32:
            raise ConfigError("agent_secret must be at least 32 characters")
        return settings

    def to_dict(self) -> dict:
        out = {}
        for key in self.__dataclass_fields__:
            value = getattr(self, key)
            if key == "root":
                value = str(value)
            if key == "stacks":
                value = [s.__dict__ for s in value]
            out[key] = value
        return out

    def save(self, path: str = DEFAULT_CONFIG_PATH) -> None:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        os.chmod(tmp, 0o600)
        tmp.replace(p)


def load(path: str | None = None) -> Settings:
    path = path or os.environ.get("RESTA_AGENT_CONFIG", DEFAULT_CONFIG_PATH)
    with open(path, encoding="utf-8") as fh:
        return Settings.from_dict(json.load(fh))
