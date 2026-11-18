"""العمليات اللي تنفذها المنصة على السيرفر.

كل عملية تستقبل معاملات (dict) وترجع نتيجة (dict). أي خطأ يرفع استثناء
ويُسجّل في المهمة ويُرسل للمنصة.

القاعدة = اسم الرابط الفرعي (مثال: القاعدة kaffa ← kaffa.restaerp.com)
القوالب تبدأ بـ tpl_ وتكون مقفلة عن الاتصال (ما تدخلها مهام أودو المجدولة).
"""
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from . import templates
from .config import Settings, check_db_name, check_stack_name, StackConfig
from .routing import Routing
from .runner import Runner
from . import metrics

_logger = logging.getLogger(__name__)

POST_CONFIG_SCRIPT = """
import json
cfg = json.loads({payload!r})
ICP = env['ir.config_parameter'].sudo()
if cfg.get('reset_uuid'):
    ICP.init(force=True)
if cfg.get('base_url'):
    ICP.set_param('web.base.url', cfg['base_url'])
    ICP.set_param('web.base.url.freeze', 'True')
lang = cfg.get('lang')
if lang:
    env['res.lang']._activate_lang(lang)
company = env['res.company'].sudo().search([], order='id', limit=1)
if cfg.get('country_code'):
    country = env['res.country'].search([('code', '=', cfg['country_code'])], limit=1)
    if country:
        company.country_id = country
        company.partner_id.country_id = country
# العملة تتغير فقط قبل تثبيت المحاسبة (مرحلة بناء القالب)
if cfg.get('currency_code') and 'account.move' not in env:
    cur = env['res.currency'].with_context(active_test=False).search([('name', '=', cfg['currency_code'])], limit=1)
    if cur:
        cur.active = True
        company.currency_id = cur
if cfg.get('company_name'):
    company.name = cfg['company_name']
    company.partner_id.name = cfg['company_name']
admin = env.ref('base.user_admin')
if cfg.get('admin_login'):
    admin.login = cfg['admin_login']
    if '@' in cfg['admin_login']:
        admin.partner_id.email = cfg['admin_login']
if cfg.get('admin_name'):
    admin.name = cfg['admin_name']
if cfg.get('admin_password'):
    admin.password = cfg['admin_password']
if lang:
    env['res.users'].with_context(active_test=False).search([]).write({{'lang': lang}})
    company.partner_id.lang = lang
if cfg.get('timezone'):
    admin.tz = cfg['timezone']
env.cr.commit()
print('RESTA-OK')
"""


class OperationError(Exception):
    pass


class Operations:
    ALLOWED = (
        "create_db", "build_template", "suspend", "resume", "backup", "restore",
        "duplicate", "drop", "deploy", "create_stack", "snapshots", "backup_all",
    )

    def __init__(self, settings: Settings, runner: Runner | None = None):
        self.s = settings
        self.r = runner or Runner(settings)
        self.routing = Routing(settings)

    # ------------------------------------------------------------ dispatch
    def dispatch(self, op: str, params: dict) -> dict:
        if op not in self.ALLOWED:
            raise OperationError(f"unknown operation: {op}")
        return getattr(self, op)(**(params or {}))

    # ------------------------------------------------------------ helpers
    def _post_config(self, stack: str, db: str, **cfg):
        cfg = {k: v for k, v in cfg.items() if v not in (None, "")}
        script = POST_CONFIG_SCRIPT.format(payload=json.dumps(cfg))
        proc = self.r.odoo_shell(stack, db, script)
        if b"RESTA-OK" not in proc.stdout:
            raise OperationError("post configuration did not complete: "
                                 + proc.stdout.decode(errors="replace")[-1500:]
                                 + proc.stderr.decode(errors="replace")[-1500:])

    def _copy_filestore(self, source: str, target: str):
        src = self.s.filestore_path(source)
        dst = self.s.filestore_path(target)
        if dst.exists():
            shutil.rmtree(dst)
        if src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src, dst, symlinks=True)
            shutil.copystat(src, dst)
            self._chown_like(src, dst)

    def _chown_like(self, src: Path, dst: Path):
        try:
            st = src.stat()
            for path in [dst, *dst.rglob("*")]:
                shutil.chown(path, st.st_uid, st.st_gid)
        except (PermissionError, LookupError, OSError):
            pass

    def _init_db(self, stack: str, db: str, modules: list, lang: str | None):
        args = ["-d", db, "-i", ",".join(modules), "--stop-after-init", "--no-http"]
        if lang:
            args.append(f"--load-language={lang}")
        self.r.run(self.r.odoo_oneoff(stack, *args))

    def _size_mb(self, db: str) -> float:
        rows = self.r.sql(f"SELECT pg_database_size('{check_db_name(db)}')")
        return round(int(rows[0][0]) / 1048576, 1) if rows else 0.0

    def _dump(self, db: str, path: Path):
        """نسخة كاملة من القاعدة حتى لو كانت موقوفة."""
        was_allowed = self.r.connections_allowed(db)
        if not was_allowed:
            self.r.allow_connections(db, True)
        try:
            self.r.pg("pg_dump", "-Fc", "-Z", "6", "--no-owner", "-d", db, stdout_path=path)
        finally:
            if not was_allowed:
                self.r.allow_connections(db, False)

    def _restore_dump(self, path: Path, target: str):
        self.r.pg("createdb", target)
        self.r.pg("pg_restore", "--no-owner", "--no-privileges", "-d", target, stdin_path=path)

    def _tmp(self, label: str) -> Path:
        d = self.s.work_dir / f"{label}-{int(time.time())}-{uuid.uuid4().hex[:6]}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ------------------------------------------------------------ create
    def create_db(self, db, template=None, modules=None, stack="main", company_name=None,
                  admin_login=None, admin_password=None, admin_name=None, lang=None,
                  base_url=None, country_code="SA", timezone="Asia/Riyadh", if_exists="error"):
        check_db_name(db)
        check_stack_name(stack)
        lang = lang or self.s.default_lang
        if self.r.db_exists(db):
            if if_exists == "reuse":
                return {"db": db, "reused": True, "size_mb": self._size_mb(db)}
            raise OperationError(f"database {db} already exists")
        if template:
            check_db_name(template)
            if not self.r.db_exists(template):
                raise OperationError(f"template {template} not found on this server")
            self.r.terminate(template)
            self.r.pg("createdb", "-T", template, db)
            self._copy_filestore(template, db)
        else:
            self._init_db(stack, db, ["base"], lang)
            self._post_config(stack, db, country_code=country_code, currency_code="SAR", lang=lang)
            if modules and modules != ["base"]:
                self._init_db(stack, db, modules, lang)
        self.r.allow_connections(db, True)
        self._post_config(
            stack, db, reset_uuid=True, base_url=base_url or f"https://{db}.{self.s.base_domain}",
            company_name=company_name, admin_login=admin_login, admin_password=admin_password,
            admin_name=admin_name, lang=lang, timezone=timezone,
        )
        if stack != "main" or self.s.kind == "dedicated":
            self.routing.set_route(db, stack)
        self.routing.set_suspended(db, False)
        self.r.nginx_reload()
        return {"db": db, "size_mb": self._size_mb(db), "url": base_url or f"https://{db}.{self.s.base_domain}"}

    def build_template(self, name, modules, stack="main", lang=None, country_code="SA", rebuild=False):
        check_db_name(name)
        if not name.startswith("tpl_"):
            raise OperationError("template names must start with tpl_")
        lang = lang or self.s.default_lang
        if self.r.db_exists(name):
            if not rebuild:
                return {"template": name, "exists": True}
            self.r.sql(f'DROP DATABASE "{name}" WITH (FORCE)')
            shutil.rmtree(self.s.filestore_path(name), ignore_errors=True)
        started = time.time()
        self._init_db(stack, name, ["base"], lang)
        self._post_config(stack, name, country_code=country_code, currency_code="SAR", lang=lang)
        if modules:
            self._init_db(stack, name, list(modules), lang)
        self._post_config(stack, name, lang=lang, timezone="Asia/Riyadh")
        self.r.allow_connections(name, False)
        return {"template": name, "modules": len(modules or []), "seconds": int(time.time() - started),
                "size_mb": self._size_mb(name)}

    # ------------------------------------------------------------ suspend / resume
    def suspend(self, db):
        check_db_name(db)
        if not self.r.db_exists(db):
            raise OperationError(f"database {db} not found")
        self.r.allow_connections(db, False)
        self.routing.set_suspended(db, True)
        self.r.nginx_reload()
        return {"db": db, "suspended": True}

    def resume(self, db):
        check_db_name(db)
        if not self.r.db_exists(db):
            raise OperationError(f"database {db} not found")
        self.r.allow_connections(db, True)
        self.routing.set_suspended(db, False)
        self.r.nginx_reload()
        return {"db": db, "suspended": False}

    # ------------------------------------------------------------ backups
    def backup(self, db, tag="manual"):
        check_db_name(db)
        if not self.s.backup_repos:
            raise OperationError("no backup repositories configured")
        tmp = self._tmp(f"bk-{db}")
        dump = tmp / "db.dump"
        try:
            self._dump(db, dump)
            paths = [str(dump)]
            fs = self.s.filestore_path(db)
            if fs.exists():
                paths.append(str(fs))
            results, errors = {}, {}
            for repo in self.s.backup_repos:
                try:
                    proc = self.r.restic(
                        repo, "backup", "--json", "--host", self.s.server_name,
                        "--tag", f"db:{db}", "--tag", f"kind:{tag}", *paths,
                    )
                    snap = None
                    for line in proc.stdout.decode().splitlines():
                        try:
                            msg = json.loads(line)
                        except ValueError:
                            continue
                        if msg.get("message_type") == "summary":
                            snap = msg.get("snapshot_id")
                    results[repo] = snap
                except Exception as exc:  # نكمل لباقي الأماكن
                    errors[repo] = str(exc)[-500:]
            if not results:
                raise OperationError("backup failed in every location: " + json.dumps(errors)[:2000])
            return {"db": db, "snapshots": results, "errors": errors,
                    "dump_mb": round(dump.stat().st_size / 1048576, 1), "copies": len(results)}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def _snapshot_args(self, repo, db, snapshot):
        """latest تعني آخر نسخة لهذه القاعدة فقط، وأي رقم آخر لازم يكون من نسخ نفس القاعدة."""
        snapshot = str(snapshot or "").strip()
        if snapshot == "latest":
            return ["latest", "--tag", f"db:{db}"]
        if not snapshot.isalnum():
            raise OperationError("invalid snapshot id")
        proc = self.r.restic(repo, "snapshots", "--json", "--tag", f"db:{db}")
        ids = {i["id"] for i in json.loads(proc.stdout.decode() or "[]")}
        if not any(i.startswith(snapshot) for i in ids):
            raise OperationError(f"snapshot {snapshot} does not belong to {db}")
        return [snapshot]

    def snapshots(self, db, repo=None):
        check_db_name(db)
        repo = repo or self.s.backup_repos[0]
        proc = self.r.restic(repo, "snapshots", "--json", "--tag", f"db:{db}")
        items = json.loads(proc.stdout.decode() or "[]")
        return {"db": db, "repo": repo, "snapshots": [
            {"id": i["short_id"], "time": i["time"], "tags": i.get("tags", [])} for i in items]}

    def backup_all(self, tag="nightly", forget=True):
        rows = self.r.sql("SELECT datname FROM pg_database WHERE datname NOT IN "
                          "('postgres','template0','template1') AND datname NOT LIKE 'tpl\\_%' ORDER BY datname")
        done, failed = [], {}
        for (db,) in rows:
            try:
                self.backup(db, tag=tag)
                done.append(db)
            except Exception as exc:
                failed[db] = str(exc)[-500:]
        pruned = {}
        if forget:
            for repo in self.s.backup_repos:
                try:
                    self.r.restic(repo, "forget", "--host", self.s.server_name, "--group-by", "host,tags",
                                  "--keep-daily", "7", "--keep-weekly", "4", "--keep-monthly", "3", "--prune")
                    pruned[repo] = "ok"
                except Exception as exc:
                    pruned[repo] = str(exc)[-300:]
        return {"done": done, "failed": failed, "retention": pruned}

    def restore(self, db, snapshot, target=None, repo=None, swap=False, neutralize=None,
                base_url=None, stack="main"):
        """استرجاع نسخة.

        - target مختلف عن db: نسخة جديدة جنب الأصلية (للفحص) ومحيّدة افتراضياً.
        - swap=True و target=db: الأصلية تتسمّى db__old_<وقت> وتحل النسخة مكانها.
        """
        check_db_name(db)
        target = check_db_name(target or db)
        repo = repo or self.s.backup_repos[0]
        neutralize = (target != db) if neutralize is None else neutralize
        old_name = None
        if self.r.db_exists(target):
            if not (swap and target == db):
                raise OperationError(f"database {target} already exists")
            old_name = f"{db}__old_{time.strftime('%Y%m%d%H%M')}"[:63]
            self.r.allow_connections(db, False)
            self.r.sql(f'ALTER DATABASE "{db}" RENAME TO "{old_name}"')
            fs = self.s.filestore_path(db)
            if fs.exists():
                fs.rename(self.s.filestore_path(old_name))
        tmp = self._tmp(f"rs-{target}")
        try:
            self.r.restic(repo, "restore", *self._snapshot_args(repo, db, snapshot), "--target", str(tmp))
            dumps = list(tmp.rglob("db.dump"))
            if not dumps:
                raise OperationError("snapshot has no database dump")
            self._restore_dump(dumps[0], target)
            src_fs_rel = str(self.s.filestore_path(db)).lstrip("/")
            restored_fs = tmp / src_fs_rel
            dst = self.s.filestore_path(target)
            if restored_fs.exists():
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(restored_fs, dst, symlinks=True)
                self._chown_like(dst.parent, dst)
            if target != db:
                self._post_config(stack, target, reset_uuid=True,
                                  base_url=base_url or f"https://{target}.{self.s.base_domain}")
            if neutralize:
                self.r.run(self.r.odoo_oneoff(stack, "neutralize", "-d", target))
            if stack != "main" or self.s.kind == "dedicated":
                self.routing.set_route(target, stack)
            self.r.nginx_reload()
            return {"db": target, "from_snapshot": snapshot, "old_db": old_name,
                    "neutralized": bool(neutralize), "size_mb": self._size_mb(target)}
        except Exception:
            if old_name and not self.r.db_exists(db):
                self.r.sql(f'ALTER DATABASE "{old_name}" RENAME TO "{db}"')
                self.r.allow_connections(db, True)
                old_fs = self.s.filestore_path(old_name)
                if old_fs.exists():
                    old_fs.rename(self.s.filestore_path(db))
            raise
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def duplicate(self, db, target, neutralize=True, base_url=None, stack="main"):
        """نسخة تجربة من قاعدة شغالة بدون ما توقفها."""
        check_db_name(db)
        check_db_name(target)
        if self.r.db_exists(target):
            raise OperationError(f"database {target} already exists")
        tmp = self._tmp(f"dup-{db}")
        try:
            dump = tmp / "db.dump"
            self._dump(db, dump)
            self._restore_dump(dump, target)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self._copy_filestore(db, target)
        self._post_config(stack, target, reset_uuid=True,
                          base_url=base_url or f"https://{target}.{self.s.base_domain}")
        if neutralize:
            self.r.run(self.r.odoo_oneoff(stack, "neutralize", "-d", target))
        if stack != "main" or self.s.kind == "dedicated":
            self.routing.set_route(target, stack)
        self.r.nginx_reload()
        return {"db": target, "from": db, "neutralized": bool(neutralize), "size_mb": self._size_mb(target)}

    def drop(self, db, backup_first=True, confirm=None):
        check_db_name(db)
        if confirm != db:
            raise OperationError("confirm must equal the database name")
        result = {"db": db}
        if not self.r.db_exists(db):
            raise OperationError(f"database {db} not found")
        if backup_first:
            result["backup"] = self.backup(db, tag="before-drop")
        self.r.sql(f'DROP DATABASE "{db}" WITH (FORCE)')
        shutil.rmtree(self.s.filestore_path(db), ignore_errors=True)
        self.routing.remove(db)
        self.r.nginx_reload()
        result["dropped"] = True
        return result

    # ------------------------------------------------------------ code
    def _stack_databases(self, stack: str) -> list[str]:
        rows = self.r.sql("SELECT datname FROM pg_database WHERE datname NOT IN "
                          "('postgres','template0','template1') ORDER BY datname")
        names = [r[0] for r in rows]
        routes = self.routing.routes()
        if stack == "main":
            return [n for n in names if routes.get(n, "main") == "main"]
        return [n for n in names if routes.get(n) == stack]

    def deploy(self, stack="main", ref=None, modules="all", databases=None, stop_during_update=True):
        """تحديث الكود من GitHub ثم تحديث موديولات القواعد (والقوالب)."""
        st = self.s.stack(check_stack_name(stack))
        ref = ref or st.branch
        d = self.r.stack_dir(stack)
        commit = self.r.git_checkout(st.repo_url, ref, d / "addons")
        if not self.is_source_stack(stack):
            self._sync_requirements(stack)
        self.r.compose(stack, "build", "--pull")
        dbs = databases or self._stack_databases(stack)
        mods = modules if isinstance(modules, str) else ",".join(modules)
        if stop_during_update:
            self.r.compose(stack, "stop")
        updated, failed = [], {}
        try:
            for db in dbs:
                check_db_name(db)
                locked = not self.r.connections_allowed(db)
                try:
                    if locked:
                        self.r.allow_connections(db, True)
                    self.r.run(self.r.odoo_oneoff(stack, "-d", db, "-u", mods, "--stop-after-init", "--no-http"))
                    updated.append(db)
                except Exception as exc:
                    failed[db] = str(exc)[-800:]
                finally:
                    if locked:
                        self.r.allow_connections(db, False)
        finally:
            self.r.compose(stack, "up", "-d")
        if failed and not updated:
            raise OperationError("update failed on every database: " + json.dumps(failed)[:2000])
        return {"stack": stack, "commit": commit, "updated": updated, "failed": failed}

    def _sync_requirements(self, stack: str):
        d = self.r.stack_dir(stack)
        req = d / "addons" / "requirements.txt"
        target = d / "requirements.txt"
        target.write_text(req.read_text() if req.exists() else "")

    # نسخ ريستا مصممة على Community فقط (resta_account و resta_payroll و resta_barcode بدل Enterprise)
    SOURCE_ADDONS_PATH = "/opt/odoo/odoo/addons,/opt/odoo/resta_addons,/opt/odoo/bs_addons"

    def is_source_stack(self, stack: str) -> bool:
        """المستودع فيه أودو كامل مع Dockerfile (مشروع ريستا) بدل موديولات فقط."""
        a = self.r.stack_dir(stack) / "addons"
        return (a / "Dockerfile").is_file() and (a / "odoo" / "odoo-bin").is_file()

    def write_stack_files(self, st: StackConfig):
        d = self.r.stack_dir(st.name)
        (d / "addons").mkdir(parents=True, exist_ok=True)
        (d / "enterprise").mkdir(parents=True, exist_ok=True)
        source = self.is_source_stack(st.name)
        if source:
            addons_path = self.SOURCE_ADDONS_PATH
            if self.s.source_enterprise:
                addons_path = addons_path.replace("/opt/odoo/odoo/addons,", "/opt/odoo/odoo/addons,/opt/odoo/enterprise,")
        else:
            addons_path = "/mnt/addons"
            if self.s.enterprise_repo_url:
                addons_path = "/mnt/enterprise," + addons_path
        values = {
            "STACK": st.name, "ROOT": str(self.s.root),
            "DB_USER": self.s.db_user, "DB_PASSWORD": self.s.db_password,
            "MASTER_PASSWORD": self.s.master_password, "ADDONS_PATH": addons_path,
            "WORKERS": self.s.workers if st.name == "main" else 2,
            "CRON": self.s.max_cron_threads if st.name == "main" else 1,
        }
        if source:
            templates.write("stack_compose_source.yml", d / "compose.yml", values)
            (d / "Dockerfile").unlink(missing_ok=True)
        else:
            templates.write("stack_compose.yml", d / "compose.yml", values)
            templates.write("stack_Dockerfile", d / "Dockerfile", values)
            if not (d / "requirements.txt").exists():
                (d / "requirements.txt").write_text("")
        templates.write("odoo.conf", d / "odoo.conf", values, mode=0o644)

    def _fix_data_owner(self, stack: str):
        """مجلد ملفات أودو يكون ملك مستخدم odoo داخل صورة هذه الطبقة."""
        self.r.compose(stack, "run", "--rm", "--no-deps", "-T", "--user", "root", "--entrypoint", "chown",
                       "odoo", "-R", "odoo:odoo", "/var/lib/odoo")

    def create_stack(self, name, repo_url, branch="main", routes=None):
        check_stack_name(name)
        st = StackConfig(name=name, repo_url=repo_url, branch=branch, routes=list(routes or []))
        self.s.stacks = [x for x in self.s.stacks if x.name != name] + [st]
        self.write_stack_files(st)
        d = self.r.stack_dir(name)
        commit = self.r.git_checkout(repo_url, branch, d / "addons")
        self.write_stack_files(st)  # بعد سحب الكود نعرف هل هو مشروع كامل أو موديولات فقط
        if not self.is_source_stack(name):
            if self.s.enterprise_repo_url:
                self.r.git_checkout(self.s.enterprise_repo_url, self.s.enterprise_branch, d / "enterprise")
            self._sync_requirements(name)
        self.r.compose(name, "build", "--pull")
        self._fix_data_owner(name)
        self.r.compose(name, "up", "-d")
        for sub in st.routes:
            if sub != "*":
                self.routing.set_route(sub, name)
        self.r.nginx_reload()
        if self.s.mode != "local":
            self.s.save()
        return {"stack": name, "commit": commit}

    # ------------------------------------------------------------ health
    def health(self) -> dict:
        return metrics.collect(self.s, self.r, self.routing)
