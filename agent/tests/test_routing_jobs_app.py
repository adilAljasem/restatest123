import json

import pytest
from fastapi.testclient import TestClient

from resta_agent.app import create_app
from resta_agent.config import ConfigError, check_db_name
from resta_agent.jobs import JobStore, Worker
from resta_agent.routing import Routing
from resta_agent.security import signed_headers, verify

from .conftest import SECRET


def test_db_name_validation():
    assert check_db_name("kaffa") == "kaffa"
    assert check_db_name("abc-staging") == "abc-staging"
    for bad in ("", "A", "x", "drop;db", "../etc", "a" * 70, "Kaffa"):
        with pytest.raises(ConfigError):
            check_db_name(bad)


def test_routing_maps(settings):
    r = Routing(settings)
    r.set_route("acme", "main")
    r.set_route("acme-staging", "staging")
    r.set_suspended("acme", True)
    routes = (settings.nginx_dir / "resta" / "routes.map").read_text()
    suspended = (settings.nginx_dir / "resta" / "suspended.map").read_text()
    assert '"acme-staging" staging;' in routes
    assert '"acme" 1;' in suspended
    r.set_suspended("acme", False)
    assert (settings.nginx_dir / "resta" / "suspended.map").read_text() == ""
    r.remove("acme-staging")
    assert "acme-staging" not in (settings.nginx_dir / "resta" / "routes.map").read_text()


class FakeOps:
    def __init__(self):
        self.calls = []

    def dispatch(self, op, params):
        self.calls.append((op, params))
        if op == "drop":
            raise RuntimeError("boom")
        return {"ok": True, "op": op}

    def health(self):
        return {"cpu_percent": 1}


def test_job_idempotent_and_callback(settings, monkeypatch):
    store = JobStore(settings.jobs_db)
    a = store.submit("suspend", {"db": "acme"}, "OP-1")
    b = store.submit("suspend", {"db": "acme"}, "OP-1")
    assert a["id"] == b["id"]
    sent = []

    class Resp:
        status_code = 200
        text = "ok"

    def fake_post(url, content, headers, timeout):
        assert verify(SECRET, headers["X-Resta-Timestamp"], headers["X-Resta-Signature"], content)
        sent.append(json.loads(content))
        return Resp()

    monkeypatch.setattr("resta_agent.jobs.httpx.post", fake_post)
    ops = FakeOps()
    w = Worker(settings, ops, store)
    assert w.run_one() is True
    w.send_callbacks()
    assert sent and sent[0]["job"]["operation_id"] == "OP-1" and sent[0]["job"]["state"] == "done"
    store.submit("drop", {"db": "x"}, "OP-2")
    w.run_one()
    assert store.get("OP-2")["state"] == "failed"


def test_api_requires_signature(settings):
    app = create_app(settings, operations=FakeOps(), start_worker=False)
    client = TestClient(app)
    assert client.get("/v1/health").status_code == 401
    body = json.dumps({"operation_id": "OP-9", "op": "suspend", "params": {"db": "acme"}}).encode()
    r = client.post("/v1/jobs", content=body, headers=signed_headers(SECRET, body))
    assert r.status_code == 202, r.text
    job = r.json()
    r2 = client.get(f"/v1/jobs/{job['id']}", headers=signed_headers(SECRET, b""))
    assert r2.status_code == 200 and r2.json()["state"] == "queued"
    bad = json.dumps({"op": "rm -rf"}).encode()
    assert client.post("/v1/jobs", content=bad, headers=signed_headers(SECRET, bad)).status_code == 400
    assert client.get("/v1/health", headers=signed_headers(SECRET, b"")).json() == {"cpu_percent": 1}
