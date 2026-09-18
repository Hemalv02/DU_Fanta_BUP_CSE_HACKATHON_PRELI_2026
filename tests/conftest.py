"""Shared fixtures: rule-based provider (no network) + sample pack loader."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_CASES_FILE = FIXTURES / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


@pytest.fixture()
def client() -> TestClient:
    """API client with the deterministic interpreter (no LLM key needed)."""
    monkeypatch = MonkeyPatch()
    monkeypatch.setenv("LLM_PROVIDER", "rule_based")
    from app.dependencies import reset_container

    reset_container()
    from app.main import app

    with TestClient(app) as test_client:
        yield test_client
    reset_container()
    monkeypatch.undo()


@pytest.fixture(scope="session")
def sample_cases() -> list[dict[str, Any]]:
    with SAMPLE_CASES_FILE.open() as handle:
        return json.load(handle)["cases"]
