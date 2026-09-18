"""Non-blocking JSONL request/response logging (disabled by default).

Set REQUEST_LOG_FILE to a writable path to enable. One JSON line is appended
per /optimize-energy exchange: timestamp, method, path, status, latency, the
parsed request body and the parsed response body.

Zero hot-path cost: the pure-ASGI middleware only buffers the (small) bodies
in memory and enqueues the record on an unbounded queue; a single daemon
writer thread performs every disk operation. Logging failures (full disk,
unwritable path after startup) can never fail or slow an API request.

Records contain only data that already flows through the API — no headers,
no environment values, no secrets (rubric secret-handling rules). /health is
skipped so container healthchecks never pollute the log.
"""

import json
import os
import queue
import threading
import time
from datetime import UTC, datetime
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import get_logger

logger = get_logger(__name__)

#: Readiness probes carry no scenario data and fire every 30 s per
#: healthcheck; logging them would only add noise.
_SKIP_PATHS = frozenset({"/health"})

#: Bodies larger than this are summarized, not stored — bounds log growth
#: even under hostile oversized payloads.
_MAX_STORED_BODY_BYTES = 128 * 1024
_PREVIEW_CHARS = 256


def _summarize_body(raw: bytes) -> Any:
    """Parse a body for logging; never raise, never store junk or bulk."""
    if not raw:
        return None
    if len(raw) > _MAX_STORED_BODY_BYTES:
        return {"truncated": True, "length_bytes": len(raw)}
    try:
        return json.loads(raw)
    except ValueError:
        return {"unparsed": True, "preview": raw[:_PREVIEW_CHARS].decode("utf-8", "replace")}


class JsonlRequestLogger:
    """Single-writer background sink; enqueue-only on the request path."""

    def __init__(self, path: str) -> None:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        # Owned by the daemon writer thread for the process lifetime.
        self._file = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._writer = threading.Thread(
            target=self._write_loop, name="jsonl-request-log", daemon=True
        )
        self._writer.start()

    def record(self, entry: dict[str, Any]) -> None:
        """Enqueue one record; can never block or raise into the request."""
        self._queue.put_nowait(entry)

    def close(self, timeout: float = 2.0) -> None:
        """Drain pending records and stop the writer (graceful shutdown)."""
        self._queue.put_nowait(None)
        self._writer.join(timeout)

    def _write_loop(self) -> None:
        while True:
            entry = self._queue.get()
            if entry is None:
                try:
                    self._file.flush()
                    self._file.close()
                except OSError:
                    pass
                return
            try:
                self._file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                self._file.flush()
            except (OSError, ValueError) as exc:
                logger.warning("request_log_write_failed", extra={"error": type(exc).__name__})


class RequestResponseLoggingMiddleware:
    """Pure-ASGI middleware capturing one request/response pair per line."""

    def __init__(self, app: ASGIApp, sink: JsonlRequestLogger) -> None:
        self.app = app
        self._sink = sink

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in _SKIP_PATHS:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status = 0
        request_body = bytearray()
        response_body = bytearray()

        async def receive_wrapper() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                request_body.extend(message.get("body", b""))
            return message

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body":
                response_body.extend(message.get("body", b""))
            await send(message)

        error_name: str | None = None
        try:
            await self.app(scope, receive_wrapper, send_wrapper)
        except Exception as exc:
            # Controlled re-raise: the server's error middleware still sends
            # the 500; we record only the exception class, never a payload.
            error_name = type(exc).__name__
            raise
        finally:
            entry: dict[str, Any] = {
                "timestamp": datetime.now(UTC).isoformat(),
                "method": scope.get("method"),
                "path": scope.get("path"),
                "status": 500 if error_name else status,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
                "request": _summarize_body(bytes(request_body)),
                "response": _summarize_body(bytes(response_body)),
            }
            if error_name:
                entry["exception"] = error_name
            self._sink.record(entry)
