"""Request validation: structurally invalid payloads must yield 400 (Section 6.1)."""


import json

from tests.conftest import SAMPLE_CASES_FILE


def _base_payload() -> dict:
    with SAMPLE_CASES_FILE.open() as handle:
        return json.load(handle)["cases"][0]["input"]


def test_malformed_json_returns_400(client) -> None:
    response = client.post(
        "/optimize-energy",
        content="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["status"] == "error"


def test_missing_top_level_field_returns_400(client) -> None:
    payload = _base_payload()
    del payload["battery"]
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_too_many_notes_returns_400(client) -> None:
    payload = _base_payload()
    payload["operator_notes"] = ["a", "b", "c", "d"]
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_empty_note_string_returns_400(client) -> None:
    payload = _base_payload()
    payload["operator_notes"] = ["   "]
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_wrong_hour_count_returns_400(client) -> None:
    payload = _base_payload()
    payload["hours"] = payload["hours"][:23]
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_duplicate_hours_returns_400(client) -> None:
    payload = _base_payload()
    payload["hours"][5]["hour"] = 4
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_out_of_range_hour_returns_400(client) -> None:
    payload = _base_payload()
    payload["hours"][0]["hour"] = 24
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_negative_demand_returns_400(client) -> None:
    payload = _base_payload()
    payload["hours"][0]["demand_kwh"] = -5
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_battery_initial_above_capacity_returns_400(client) -> None:
    payload = _base_payload()
    payload["battery"]["initial_energy_kwh"] = payload["battery"]["capacity_kwh"] + 1
    assert client.post("/optimize-energy", json=payload).status_code == 400


def test_unrelated_endpoint_returns_404_not_500(client) -> None:
    assert client.post("/nope", json={}).status_code == 404


def test_valid_payload_round_trip(client) -> None:
    payload = _base_payload()
    payload["operator_notes"] = ["The cafeteria menu changes tomorrow."]
    response = client.post("/optimize-energy", json=payload)
    assert response.status_code == 200
    assert response.json()["scenario_id"] == payload["scenario_id"]
