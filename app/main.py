"""GridWise LLM service — FastAPI application entrypoint.

Run locally:
    uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

Error contract (Problem Statement Section 6.1):
    400 -> malformed JSON or structurally invalid request
    500 -> controlled internal error (no secrets, no raw stack traces)
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import routers
from app.core.config import get_settings
from app.core.errors import GridWiseError
from app.core.logging import configure_logging, get_logger
from app.core.request_logging import (
    JsonlRequestLogger,
    RequestResponseLoggingMiddleware,
)

logger = get_logger(__name__)


def create_app() -> FastAPI:
    """Application factory."""
    configure_logging()
    settings = get_settings()

    # Optional JSONL request/response logging (off unless REQUEST_LOG_FILE
    # is set). A bad path disables logging with an error log line instead of
    # ever taking the service down.
    request_sink: JsonlRequestLogger | None = None
    if settings.request_log_file:
        try:
            request_sink = JsonlRequestLogger(settings.request_log_file)
        except OSError as exc:
            logger.error(
                "request_logging_disabled_unwritable_path",
                extra={"error": type(exc).__name__},
            )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        if request_sink is not None:
            request_sink.close()

    app = FastAPI(
        title="GridWise LLM",
        description=(
            "LLM-assisted operator directive interpretation and 24-hour campus "
            "energy optimization (BUP CSE Fest 2026 preliminary)."
        ),
        version="0.1.0",
        # The judge harness only uses /health and /optimize-energy; docs stay
        # enabled for humans but add nothing to the contract.
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    for router in routers:
        app.include_router(router)

    if request_sink is not None:
        app.add_middleware(RequestResponseLoggingMiddleware, sink=request_sink)

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Section 6.1: malformed JSON / structurally invalid request -> 400.
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "invalid request payload"},
        )

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        _request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # 404/405/etc. keep their status but use the same controlled envelope.
        return JSONResponse(
            status_code=exc.status_code,
            content={"status": "error", "error": str(exc.detail)},
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(GridWiseError)
    async def handle_gridwise_error(
        _request: Request, exc: GridWiseError
    ) -> JSONResponse:
        logger.error("controlled_failure", extra={"error": type(exc).__name__})
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "internal processing failure"},
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(_request: Request, exc: Exception) -> JSONResponse:
        # Never leak stack traces or provider details to the client.
        logger.exception("unhandled_exception")
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": "internal server error"},
        )

    return app


app = create_app()
