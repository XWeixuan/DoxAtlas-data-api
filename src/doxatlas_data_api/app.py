from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from cn_hk_collector.db import (
    TaskAlreadyPulledError,
    TaskNotFoundError,
    TaskNotReadyError,
    connect,
    create_crawler_task,
    get_crawler_task,
    init_schema,
    mark_crawler_task_failed,
    mark_crawler_task_running,
    mark_crawler_task_succeeded,
    pull_crawler_task_once,
)
from cn_hk_collector.market import normalize_ticker
from cn_hk_collector.runner import collect_ticker
from doxatlas_data_api.config import get_settings
from doxatlas_data_api.schemas import (
    CrawlTaskCreate,
    CrawlTaskCreateResponse,
    CrawlTaskPullResponse,
    CrawlTaskSummary,
)

logger = logging.getLogger(__name__)
settings = get_settings()


def _client_ip(request: Request) -> str:
    if settings.trust_proxy_headers:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip.strip()
    return request.client.host if request.client else ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.init_db_on_startup:
        with connect() as conn:
            init_schema(conn)
    yield


app = FastAPI(
    title="DoxAtlas Mainland Data API",
    version="0.1.0",
    lifespan=lifespan,
)


@app.middleware("http")
async def enforce_ip_allowlist(request: Request, call_next):
    if not settings.disable_ip_allowlist:
        client_ip = _client_ip(request)
        if client_ip not in settings.allowed_client_ips:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "client_ip_not_allowed",
                    "client_ip": client_ip,
                },
            )
    return await call_next(request)


def _task_url(task_id: str, suffix: str = "") -> str:
    return f"/v1/crawl-tasks/{task_id}{suffix}"


def _run_task(task_id: str) -> None:
    with connect() as conn:
        task = get_crawler_task(conn, task_id)
    if not task:
        logger.error("crawler task disappeared before execution: %s", task_id)
        return

    try:
        with connect() as conn:
            mark_crawler_task_running(conn, task_id)
        result = collect_ticker(
            market=task["market"],
            ticker=task["ticker"],
            lookback_days=int(task["lookback_days"]),
            window_start=task.get("window_start"),
            window_end=task.get("window_end"),
            task_id=task_id,
            collect_media=bool(task.get("collect_media")),
            collect_social=bool(task.get("collect_social")),
        )
        with connect() as conn:
            mark_crawler_task_succeeded(conn, result)
    except Exception as exc:
        logger.exception("crawler task failed task_id=%s", task_id)
        with connect() as conn:
            mark_crawler_task_failed(conn, task_id, str(exc))


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "doxatlas_data_api"}


@app.post("/v1/crawl-tasks", response_model=CrawlTaskCreateResponse, status_code=202)
def create_task(payload: CrawlTaskCreate, background_tasks: BackgroundTasks) -> CrawlTaskCreateResponse:
    if payload.window_start and payload.window_end and payload.window_end <= payload.window_start:
        raise HTTPException(status_code=422, detail="window_end must be later than window_start")
    ticker = normalize_ticker(payload.ticker, payload.market)
    with connect() as conn:
        task = create_crawler_task(
            conn,
            market=payload.market,
            ticker=ticker,
            lookback_days=payload.lookback_days,
            window_start=payload.window_start.isoformat() if payload.window_start else None,
            window_end=payload.window_end.isoformat() if payload.window_end else None,
            collect_media=payload.collect_media,
            collect_social=payload.collect_social,
        )
    background_tasks.add_task(_run_task, task["task_id"])
    return CrawlTaskCreateResponse(
        task_id=task["task_id"],
        status=task["status"],
        status_url=_task_url(task["task_id"]),
        pull_url=_task_url(task["task_id"], "/pull"),
    )


@app.get("/v1/crawl-tasks/{task_id}", response_model=CrawlTaskSummary)
def get_task(task_id: str) -> CrawlTaskSummary:
    with connect() as conn:
        task = get_crawler_task(conn, task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task_not_found")
    return CrawlTaskSummary(**task)


@app.post("/v1/crawl-tasks/{task_id}/pull", response_model=CrawlTaskPullResponse)
def pull_task(task_id: str) -> CrawlTaskPullResponse:
    try:
        with connect() as conn:
            payload = pull_crawler_task_once(conn, task_id)
    except TaskNotFoundError:
        raise HTTPException(status_code=404, detail="task_not_found") from None
    except TaskNotReadyError:
        raise HTTPException(status_code=409, detail="task_not_succeeded") from None
    except TaskAlreadyPulledError:
        raise HTTPException(status_code=409, detail="task_already_pulled") from None
    return CrawlTaskPullResponse(
        task=CrawlTaskSummary(**payload["task"]),
        raw_media=payload["raw_media"],
        raw_social=payload["raw_social"],
    )
