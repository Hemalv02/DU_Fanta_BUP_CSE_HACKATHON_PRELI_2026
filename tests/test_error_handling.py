"""Error and exception handling (Problem Statement Sections 6.1, 08 SAFE FAILURE).

Covers every failure path end to end:
  - 404 / 405 keep the controlled JSON envelope
  - infeasible optimization -> controlled 500, no stack trace
  - LLM provider crash / garbage -> deterministic fallback, still 200
  - latency misconfiguration -> auto-clamped inside the 30 s judge budget
"""

import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from app.core.config import Settings
from app.core.errors import InterpretationError
from app.dependencies import reset_container
from app.repositories.llm.base import LLMRepository

SAMPLE_FILE = Path(__file__).parent / "fixtures/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


def _payload() -> dict[str, Any]:
    with SAMPLE_FILE.open() as handle:
        return json.load(handle)["cases"][0]["input"]


class _CrashingLLM:
    """Simulates a provider that always fails (timeout / outage)."""

    def interpret_notes(
        self, notes: list[str], battery_capacity_kwh: float | None = None
    ) -> list[dict[str, Any]]:
        raise InterpretationError("LLM provider error: APITimeoutError")


class _GarbageLLM:
    """Simulates a model returning unusable structured output."""

    def interpret_notes(
        self, notes: list[str], battery_capacity_kwh: float | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "note_index": 99,  # out of range -> dropped by guardrails
                "applies": True,
                "directive_type": "self_destruct",  # unsupported -> dropped
                "structured_adjustment": {"hours": [77]},
                "explanation": "garbage",
            }
        ]


@pytest.fixture()
def llm_swap(
    monkeypatch: MonkeyPatch,
) -> Iterator[tuple[TestClient, Callable[[LLMRepository], None]]]:
    """Client whose LLM repository can be swapped to simulate provider faults."""
    monkeypatch.setenv("LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-only")

    from app import dependencies as deps
    from app.main import app

    original_builder = deps.build_llm_repository
    broken: list[LLMRepository] = []

    def build(_settings: Settings) -> LLMRepository:
        return broken[-1]

    def use(repo: LLMRepository) -> None:
        broken.append(repo)
        reset_container()

    deps.build_llm_repository = build
    reset_container()
    with TestClient(app) as client:
        yield client, use
    deps.build_llm_repository = original_builder
    reset_container()


def test_unknown_route_uses_controlled_envelope(client: TestClient) -> None:
    response = client.get("/nope")
    assert response.status_code == 404
    body = response.json()
    assert body["status"] == "error"
    assert "error" in body


def test_wrong_method_uses_controlled_envelope(client: TestClient) -> None:
    response = client.get("/optimize-energy")
    assert response.status_code == 405
    assert response.json()["status"] == "error"


def test_infeasible_scenario_returns_controlled_500(client: TestClient) -> None:
    payload = _payload()
    payload["operator_notes"] = ["Grid intake must stay at or below 1 kWh from 12 AM until 11 PM."]
    payload["hours"] = [
        {"hour": h, "demand_kwh": 150, "solar_kwh": 0, "tariff_bdt_per_kwh": 10}
        for h in range(24)
    ]
    payload["battery"] = {
        "capacity_kwh": 10,
        "initial_energy_kwh": 5,
        "minimum_energy_kwh": 0,
        "max_charge_kwh_per_hour": 5,
        "max_discharge_kwh_per_hour": 5,
    }
    response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 500
    assert response.json() == {"status": "error", "error": "internal processing failure"}
    assert "Traceback" not in response.text


def test_provider_crash_degrades_to_rule_based(
    llm_swap: tuple[TestClient, Callable[[LLMRepository], None]],
) -> None:
    client, use = llm_swap
    use(_CrashingLLM())
    response = client.post("/optimize-energy", json=_payload())
    assert response.status_code == 200
    entries = response.json()["directive_interpretation"]
    # Whole-set fallback still interprets SAMPLE-01 note 0 correctly.
    assert entries[0]["directive_type"] == "solar_reduction"
    assert entries[1]["directive_type"] == "no_op"


def test_garbage_llm_output_degrades_per_note(
    llm_swap: tuple[TestClient, Callable[[LLMRepository], None]],
) -> None:
    client, use = llm_swap
    use(_GarbageLLM())
    response = client.post("/optimize-energy", json=_payload())
    assert response.status_code == 200
    entries = response.json()["directive_interpretation"]
    assert [e["note_index"] for e in entries] == [0, 1]
    assert all(e["directive_type"] in {"solar_reduction", "no_op"} for e in entries)


def test_latency_budget_is_auto_clamped() -> None:
    settings = Settings(llm_timeout_seconds=25.0, llm_max_retries=2)
    # 26 s LLM budget (30 s judge limit minus margin) split across 3 attempts.
    assert settings.llm_timeout_seconds * (settings.llm_max_retries + 1) <= 26.0
    assert settings.llm_timeout_seconds <= 9.0


def test_sane_latency_config_untouched() -> None:
    settings = Settings()
    assert settings.llm_timeout_seconds * (settings.llm_max_retries + 1) <= 26.0
