"""واجهة الوكيل (API v1).

كل الطلبات لازم تكون موقّعة بسر السيرفر. الوكيل يسمع على العنوان الداخلي فقط،
والجدار الناري في Google Cloud يسمح للمنصة فقط.

  GET  /v1/health          أرقام الأداء وحالة القواعد
  POST /v1/jobs            {operation_id, op, params}  ← تنفيذ عملية (غير متزامن)
  GET  /v1/jobs/{id}       حالة عملية
  GET  /v1/jobs            آخر العمليات
"""
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import API_VERSION, __version__
from .jobs import JobStore, Worker, public_job
from .ops import Operations
from .security import verify

_logger = logging.getLogger(__name__)


def create_app(settings, operations: Operations | None = None, start_worker: bool = True) -> FastAPI:
    settings.agent_dir.mkdir(parents=True, exist_ok=True)
    ops = operations or Operations(settings)
    store = JobStore(settings.jobs_db)
    worker = Worker(settings, ops, store)

    @asynccontextmanager
    async def lifespan(_app):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Resta Agent", version=__version__, docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.worker = worker
    app.state.ops = ops

    async def signed(request: Request) -> bytes:
        body = await request.body()
        if len(body) > 1_000_000:
            raise HTTPException(413, "body too large")
        ok = verify(settings.agent_secret, request.headers.get("X-Resta-Timestamp"),
                    request.headers.get("X-Resta-Signature"), body)
        if not ok:
            raise HTTPException(401, "invalid signature")
        return body

    @app.get(f"/{API_VERSION}/health")
    def health(_: bytes = Depends(signed)):
        return ops.health()

    @app.post(f"/{API_VERSION}/jobs", status_code=202)
    async def submit(request: Request, body: bytes = Depends(signed)):
        try:
            data = await request.json()
        except Exception:
            raise HTTPException(400, "invalid json")
        op = data.get("op")
        if op not in Operations.ALLOWED:
            raise HTTPException(400, f"unknown operation: {op}")
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise HTTPException(400, "params must be an object")
        job = store.submit(op, params, data.get("operation_id"))
        return JSONResponse(public_job(job), status_code=202)

    @app.get(f"/{API_VERSION}/jobs/{{job_id}}")
    def job_status(job_id: str, _: bytes = Depends(signed)):
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return public_job(job)

    @app.get(f"/{API_VERSION}/jobs")
    def jobs(_: bytes = Depends(signed)):
        return [public_job(j) for j in store.recent()]

    return app
