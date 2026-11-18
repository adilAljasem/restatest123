"""تنفيذ الأوامر على السيرفر.

في وضع docker: أدوات PostgreSQL تشتغل داخل حاوية resta-db، وأودو داخل حاوية الطبقة (stack).
في وضع local: نفس الأوامر تشتغل مباشرة على الجهاز (للاختبارات فقط).
"""
import logging
import os
import subprocess
from pathlib import Path

from .config import Settings, check_db_name, check_stack_name

_logger = logging.getLogger(__name__)

DB_CONTAINER = "resta-db"
NGINX_CONTAINER = "resta-nginx"


class CommandError(Exception):
    def __init__(self, cmd, returncode, stderr):
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = (stderr or "")[-4000:]
        super().__init__(f"command failed ({returncode}): {' '.join(map(str, cmd))[:300]}\n{self.stderr}")


def _redact(cmd):
    return [("***" if "x-access-token" in str(c) else str(c)) for c in cmd]


class Runner:
    def __init__(self, settings: Settings):
        self.s = settings

    # ------------------------------------------------------------ generic
    def run(self, cmd, *, input_bytes=None, stdout_path=None, stdin_path=None,
            env=None, check=True, timeout=6 * 3600, cwd=None) -> subprocess.CompletedProcess:
        full_env = os.environ.copy()
        if env:
            full_env.update(env)
        _logger.info("run: %s", " ".join(_redact(cmd))[:500])
        stdout = subprocess.PIPE
        stdin = None
        fh_out = fh_in = None
        try:
            if stdout_path:
                fh_out = open(stdout_path, "wb")
                stdout = fh_out
            if stdin_path:
                fh_in = open(stdin_path, "rb")
                stdin = fh_in
            proc = subprocess.run(
                [str(c) for c in cmd], input=input_bytes, stdin=stdin if input_bytes is None else None,
                stdout=stdout, stderr=subprocess.PIPE, env=full_env, timeout=timeout, cwd=cwd,
            )
        finally:
            if fh_out:
                fh_out.close()
            if fh_in:
                fh_in.close()
        if check and proc.returncode != 0:
            raise CommandError(_redact(cmd), proc.returncode, proc.stderr.decode(errors="replace"))
        return proc

    # ------------------------------------------------------------ postgres
    def pg_cmd(self, tool: str, *args) -> list:
        if self.s.mode == "local":
            return [tool, *args]
        return ["docker", "exec", "-i", "-e", f"PGPASSWORD={self.s.db_password}", DB_CONTAINER,
                tool, "-U", self.s.db_user, "-h", "127.0.0.1", *args]

    def pg_env(self):
        return dict(self.s.pg_env) if self.s.mode == "local" else None

    def pg(self, tool: str, *args, **kw):
        return self.run(self.pg_cmd(tool, *args), env=self.pg_env(), **kw)

    def sql(self, query: str, db: str = "postgres") -> list[list[str]]:
        proc = self.pg("psql", "-X", "-At", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-d", db, "-c", query)
        out = proc.stdout.decode().strip()
        return [line.split("\t") for line in out.splitlines()] if out else []

    def db_exists(self, db: str) -> bool:
        check_db_name(db)
        return bool(self.sql(f"SELECT 1 FROM pg_database WHERE datname = '{db}'"))

    def terminate(self, db: str):
        check_db_name(db)
        self.sql(f"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                 f"WHERE datname = '{db}' AND pid <> pg_backend_pid()")

    def allow_connections(self, db: str, allow: bool):
        check_db_name(db)
        self.sql(f'ALTER DATABASE "{db}" ALLOW_CONNECTIONS {"true" if allow else "false"}')
        if not allow:
            self.terminate(db)

    def connections_allowed(self, db: str) -> bool:
        check_db_name(db)
        rows = self.sql(f"SELECT datallowconn FROM pg_database WHERE datname = '{db}'")
        return bool(rows) and rows[0][0] == "t"

    # ------------------------------------------------------------ odoo
    def stack_dir(self, stack: str) -> Path:
        return self.s.stacks_dir / check_stack_name(stack)

    def odoo_oneoff(self, stack: str, *args) -> list:
        """أمر أودو يشتغل في حاوية مؤقتة (تثبيت/تحديث/تحييد).

        أول معامل ممكن يكون أمر فرعي مثل shell أو neutralize.
        """
        args = [str(a) for a in args]
        sub = [args[0]] if args and not args[0].startswith("-") else []
        rest = args[len(sub):]
        if self.s.mode == "local":
            return [*self.s.odoo_cmd, *sub, "-c", self.s.odoo_conf, *rest]
        d = self.stack_dir(stack)
        cmd = ["docker", "compose", "-p", f"resta-{stack}", "-f", str(d / "compose.yml"),
               "run", "--rm", "--no-deps", "-T", "odoo", "odoo"]
        return [*cmd, *sub, "-c", "/etc/odoo/odoo.conf", *rest]

    def odoo_shell(self, stack: str, db: str, script: str):
        check_db_name(db)
        cmd = self.odoo_oneoff(stack, "shell", "-d", db, "--no-http", "--log-level=warn")
        return self.run(cmd, input_bytes=script.encode())

    def compose(self, stack: str, *args):
        if self.s.mode == "local":
            return None
        d = self.stack_dir(stack)
        return self.run(["docker", "compose", "-p", f"resta-{stack}", "-f", str(d / "compose.yml"), *args], cwd=d)

    def base_compose(self, *args):
        if self.s.mode == "local":
            return None
        f = Path(self.s.root) / "base" / "compose.yml"
        return self.run(["docker", "compose", "-p", "resta-base", "-f", str(f), *args])

    def nginx_reload(self):
        if self.s.mode == "local":
            return None
        return self.run(["docker", "exec", NGINX_CONTAINER, "nginx", "-s", "reload"], check=False)

    def container_health(self, name: str) -> str:
        if self.s.mode == "local":
            return "unknown"
        proc = self.run(["docker", "inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}", name], check=False)
        return proc.stdout.decode().strip() or "missing"

    # ------------------------------------------------------------ git
    def git_url(self, url: str) -> str:
        token = self.s.git_token
        if token and url.startswith("https://github.com/"):
            return url.replace("https://", f"https://x-access-token:{token}@", 1)
        return url

    def git_checkout(self, url: str, branch_or_ref: str, path: Path):
        path = Path(path)
        if not (path / ".git").exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            self.run(["git", "clone", "--depth", "1", "--branch", branch_or_ref, self.git_url(url), str(path)])
        else:
            self.run(["git", "-C", str(path), "remote", "set-url", "origin", self.git_url(url)])
            self.run(["git", "-C", str(path), "fetch", "--depth", "1", "origin", branch_or_ref])
            self.run(["git", "-C", str(path), "reset", "--hard", "FETCH_HEAD"])
        proc = self.run(["git", "-C", str(path), "rev-parse", "HEAD"])
        return proc.stdout.decode().strip()

    # ------------------------------------------------------------ restic
    def restic(self, repo: str, *args, **kw):
        env = dict(self.s.backup_env or {})
        env["RESTIC_PASSWORD"] = self.s.restic_password
        env["RESTIC_REPOSITORY"] = repo
        return self.run(["restic", *args], env=env, **kw)
