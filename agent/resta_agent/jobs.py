"""طابور المهام.

- كل أمر من المنصة يصير مهمة محفوظة في SQLite (ما تضيع لو انطفأ السيرفر).
- المهام تتنفذ وحدة وحدة بالترتيب (عشان ما تتعارض العمليات على نفس القواعد).
- رقم العملية من المنصة (operation_id) فريد: لو وصل نفس الأمر مرتين ما يتنفذ مرتين.
- بعد كل مهمة يرسل الوكيل النتيجة للمنصة بتوقيع آمن، ويعيد المحاولة لو فشل الإرسال.
"""
import json
import logging
import sqlite3
import threading
import time
import traceback
import uuid

import httpx

from .security import signed_headers

_logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    operation_id TEXT UNIQUE,
    op TEXT NOT NULL,
    params TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT,
    error TEXT,
    created REAL NOT NULL,
    started REAL,
    finished REAL,
    callback_state TEXT DEFAULT 'none',
    callback_attempts INTEGER DEFAULT 0,
    callback_next REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, created);
"""

FIELDS = ("id", "operation_id", "op", "params", "state", "result", "error", "created",
          "started", "finished", "callback_state", "callback_attempts", "callback_next")


class JobStore:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            # أي مهمة كانت شغالة وقت انطفاء السيرفر ترجع للانتظار
            c.execute("UPDATE jobs SET state='queued', started=NULL WHERE state='running'")

    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    def _row(self, row) -> dict | None:
        if row is None:
            return None
        data = {k: row[k] for k in FIELDS}
        data["params"] = json.loads(data["params"] or "{}")
        data["result"] = json.loads(data["result"]) if data["result"] else None
        return data

    def submit(self, op: str, params: dict, operation_id: str | None) -> dict:
        with self._lock, self._conn() as c:
            if operation_id:
                existing = c.execute("SELECT * FROM jobs WHERE operation_id=?", (operation_id,)).fetchone()
                if existing:
                    return self._row(existing)
            job_id = uuid.uuid4().hex
            c.execute("INSERT INTO jobs (id, operation_id, op, params, state, created) VALUES (?,?,?,?,?,?)",
                      (job_id, operation_id, op, json.dumps(params or {}), "queued", time.time()))
            return self._row(c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())

    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            return self._row(c.execute("SELECT * FROM jobs WHERE id=? OR operation_id=?", (job_id, job_id)).fetchone())

    def next_queued(self) -> dict | None:
        with self._lock, self._conn() as c:
            row = c.execute("SELECT * FROM jobs WHERE state='queued' ORDER BY created LIMIT 1").fetchone()
            if row:
                c.execute("UPDATE jobs SET state='running', started=? WHERE id=?", (time.time(), row["id"]))
            return self._row(row)

    def finish(self, job_id: str, state: str, result=None, error=None, callback=True):
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET state=?, result=?, error=?, finished=?, callback_state=?, callback_next=0 "
                      "WHERE id=?", (state, json.dumps(result) if result is not None else None, error,
                                     time.time(), "pending" if callback else "none", job_id))

    def pending_callbacks(self, now: float) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM jobs WHERE callback_state='pending' AND callback_next<=? "
                             "ORDER BY finished LIMIT 20", (now,)).fetchall()
            return [self._row(r) for r in rows]

    def callback_result(self, job_id: str, ok: bool, attempts: int):
        delay = min(3600, 30 * (2 ** min(attempts, 7)))
        with self._lock, self._conn() as c:
            c.execute("UPDATE jobs SET callback_state=?, callback_attempts=?, callback_next=? WHERE id=?",
                      ("sent" if ok else ("failed" if attempts >= 30 else "pending"), attempts,
                       time.time() + delay, job_id))

    def recent(self, limit=50) -> list[dict]:
        with self._conn() as c:
            return [self._row(r) for r in c.execute("SELECT * FROM jobs ORDER BY created DESC LIMIT ?", (limit,))]


def public_job(job: dict) -> dict:
    return {k: job[k] for k in ("id", "operation_id", "op", "state", "result", "error",
                                "created", "started", "finished")}


class Worker(threading.Thread):
    def __init__(self, settings, operations, store: JobStore, poll=2.0):
        super().__init__(daemon=True, name="resta-worker")
        self.s = settings
        self.ops = operations
        self.store = store
        self.poll = poll
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run_one(self) -> bool:
        job = self.store.next_queued()
        if not job:
            return False
        _logger.info("job %s start: %s", job["id"], job["op"])
        try:
            result = self.ops.dispatch(job["op"], job["params"])
            self.store.finish(job["id"], "done", result=result)
            _logger.info("job %s done", job["id"])
        except Exception as exc:
            _logger.exception("job %s failed", job["id"])
            self.store.finish(job["id"], "failed", error=f"{exc}\n{traceback.format_exc()[-3000:]}")
        return True

    def send_callbacks(self):
        for job in self.store.pending_callbacks(time.time()):
            attempts = (job["callback_attempts"] or 0) + 1
            body = json.dumps({"server_id": self.s.server_id, "job": public_job(job)}).encode()
            ok = False
            try:
                resp = httpx.post(self.s.callback_url, content=body,
                                  headers=signed_headers(self.s.agent_secret, body), timeout=20)
                ok = resp.status_code == 200
                if not ok:
                    _logger.warning("callback %s -> %s %s", job["id"], resp.status_code, resp.text[:200])
            except Exception as exc:
                _logger.warning("callback %s failed: %s", job["id"], exc)
            self.store.callback_result(job["id"], ok, attempts)

    def run(self):
        while not self._stop.is_set():
            try:
                worked = self.run_one()
                self.send_callbacks()
            except Exception:
                _logger.exception("worker loop error")
                worked = False
            if not worked:
                self._stop.wait(self.poll)
