"""Non-blocking JSONL request/response logging (off by default).

Uses a private FastAPI app so the global app (logging disabled under the
default empty REQUEST_LOG_FILE) stays untouched; middleware and sink are
exercised directly against a temp file.
"""

import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.request_logging import (
    JsonlRequestLogger,
    RequestResponseLoggingMiddleware,
)


def _build_app(sink: JsonlRequestLogger) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestResponseLoggingMiddleware, sink=sink)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/optimize-energy")
    def optimize(payload: dict[str, Any]) -> dict[str, Any]:
        return {"scenario_id": payload["scenario_id"], "hourly_plan": list(range(24))}

    return app


def _read_records(path: Path, expected: int, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Poll the JSONL file until the background writer catches up."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            lines = [line for line in path.read_text().splitlines() if line.strip()]
            if len(lines) >= expected:
                return [json.loads(line) for line in lines[:expected]]
        time.sleep(0.02)
    return []


def test_request_and_response_are_logged(tmp_path: Path) -> None:
    log_file = tmp_path / "requests.jsonl"
    sink = JsonlRequestLogger(str(log_file))
    client = TestClient(_build_app(sink))

    response = client.post(
        "/optimize-energy", json={"scenario_id": "LOG-1", "operator_notes": ["x"]}
    )
    assert response.status_code == 200
    sink.close()

    records = _read_records(log_file, expected=1)
    assert len(records) == 1
    entry = records[0]
    assert entry["method"] == "POST"
    assert entry["path"] == "/optimize-energy"
    assert entry["status"] == 200
    assert isinstance(entry["elapsed_ms"], float)
    assert entry["request"]["scenario_id"] == "LOG-1"
    assert entry["response"]["scenario_id"] == "LOG-1"
    assert len(entry["response"]["hourly_plan"]) == 24


def test_malformed_body_is_summarized_not_stored(tmp_path: Path) -> None:
    log_file = tmp_path / "requests.jsonl"
    sink = JsonlRequestLogger(str(log_file))
    client = TestClient(_build_app(sink))

    response = client.post(
        "/optimize-energy", content="{broken", headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 422  # FastAPI's own validation on this app
    sink.close()

    records = _read_records(log_file, expected=1)
    assert records[0]["request"]["unparsed"] is True
    assert "preview" in records[0]["request"]


def test_health_is_not_logged(tmp_path: Path) -> None:
    log_file = tmp_path / "requests.jsonl"
    sink = JsonlRequestLogger(str(log_file))
    client = TestClient(_build_app(sink))

    assert client.get("/health").json() == {"status": "ok"}
    client.post("/optimize-energy", json={"scenario_id": "LOG-2"})
    sink.close()

    records = _read_records(log_file, expected=1)
    assert len(records) == 1  # /health produced no line
    assert records[0]["request"]["scenario_id"] == "LOG-2"


def test_oversized_body_is_truncated_but_request_succeeds(tmp_path: Path) -> None:
    log_file = tmp_path / "requests.jsonl"
    sink = JsonlRequestLogger(str(log_file))
    client = TestClient(_build_app(sink))

    huge_note = "x" * 300_000
    response = client.post(
        "/optimize-energy", json={"scenario_id": "BIG", "operator_notes": [huge_note]}
    )
    assert response.status_code == 200
    sink.close()

    records = _read_records(log_file, expected=1)
    assert records[0]["request"]["truncated"] is True
    assert records[0]["request"]["length_bytes"] > 128 * 1024


def test_logging_disabled_by_default() -> None:
    from app.core.config import Settings

    assert Settings().request_log_file == ""
