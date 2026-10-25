"""Application composition: configuration, services, routes, static frontend and lifecycle."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.deps import AppContainer
from app.api.errors import install_error_handlers
from app.api.middleware import (
    BodySizeLimitMiddleware,
    JsonContentTypeMiddleware,
    RequestLogMiddleware,
    SecurityHeadersMiddleware,
)
from app.api.routes import router
from app.config import Settings, get_settings
from app.storage import Storage, utcnow

logger = logging.getLogger("app")

_STANDARD_LOG_FIELDS = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line, including structured `extra` fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        payload.update({k: v for k, v in record.__dict__.items() if k not in _STANDARD_LOG_FIELDS})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(debug: bool) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    root = logging.getLogger("app")
    root.handlers = [handler]
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    root.propagate = False


class RefreshWorker:
    """Runs refresh jobs on one background thread so ingestion never blocks chat requests."""

    def __init__(self, storage: Storage, settings: Settings, data_service: Any) -> None:
        self._storage = storage
        self._settings = settings
        self._data_service = data_service
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="refresh")

    def submit(self, job_id: str) -> None:
        self._executor.submit(self._run, job_id)

    def _run(self, job_id: str) -> None:
        from app.analysis.service import compute_national_references
        from app.data.refresh import run_refresh_job

        try:
            run_refresh_job(
                self._storage,
                self._settings,
                lambda repo: compute_national_references(
                    repo,
                    horizon_years=self._settings.forecast_horizon_years,
                    min_departures=self._settings.s3_min_departures,
                ),
                job_id=job_id,
            )
        except Exception:
            logger.exception("refresh_job_crashed", extra={"job_id": job_id})

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def build_container(settings: Settings) -> tuple[AppContainer, RefreshWorker]:
    """Wire the production services: storage → data → analysis → tools → agent."""
    from app.agent.client import OpenAIResponsesClient
    from app.agent.service import AgentService
    from app.agent.tools import ToolExecutor
    from app.analysis.service import AnalysisService
    from app.data.resolution import load_reference_catalog
    from app.data.service import SqliteDataService

    storage = Storage(settings.database_path)
    if settings.auto_bootstrap:
        from app.bootstrap import bootstrap

        bootstrap(storage)
    else:
        storage.initialize()
    reference = load_reference_catalog()
    data_service = SqliteDataService(storage, reference)
    analysis = AnalysisService(data_service, settings)
    executor = ToolExecutor(analysis, reference, settings.tool_output_max_chars)
    agent = AgentService(OpenAIResponsesClient(settings), executor, data_service, settings)
    worker = RefreshWorker(storage, settings, data_service)
    container = AppContainer(
        settings=settings, storage=storage, agent=agent, data=data_service, submit_refresh=worker.submit
    )
    return container, worker


def create_app(settings: Settings | None = None, container: AppContainer | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.debug)
    worker: RefreshWorker | None = None
    if container is None:
        container, worker = build_container(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        storage = container.storage
        storage.initialize()
        now = utcnow()
        interrupted = storage.interrupt_expired_requests(now)
        stale_jobs = storage.interrupt_stale_refresh_jobs(now, include_queued=True)
        storage.delete_expired_sessions(now)
        if worker is not None:
            from app.data.refresh import cleanup_incomplete_files

            cleanup_incomplete_files(storage)
        logger.info(
            "startup",
            extra={
                "interrupted_requests": interrupted,
                "interrupted_refresh_jobs": stale_jobs,
                "active_data_version": storage.get_active_data_version_id(),
                "model": settings.openai_model,
                "model_configured": settings.openai_api_key is not None,
            },
        )
        yield
        if worker is not None:
            worker.shutdown()

    app = FastAPI(
        title="Airport Investment Intelligence Agent",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.container = container
    install_error_handlers(app)
    app.include_router(router)
    if settings.frontend_dist.is_dir():
        # Only the built frontend is served; the database, data files and .env live elsewhere.
        app.mount("/", StaticFiles(directory=settings.frontend_dist, html=True), name="frontend")
    app.add_middleware(JsonContentTypeMiddleware)
    app.add_middleware(SecurityHeadersMiddleware, hsts=settings.secure_cookies)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_body_bytes)
    app.add_middleware(RequestLogMiddleware)
    return app


def app_factory() -> FastAPI:
    """Entry point for `uvicorn app.main:app_factory --factory`."""
    return create_app()
