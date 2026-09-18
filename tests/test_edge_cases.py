"""Adversarial edge cases through the full API (rule-based provider).

The same pack is also run against the live LLM endpoint by
scripts/run_public_samples.py --cases tests/fixtures/edge_cases.json — this
test pins the deterministic safety net so the service stays correct even when
the hosted model is unavailable.
"""

import json
from pathlib import Path

import pytest

from tests.test_public_samples import _judge_replay

EDGE_FILE = Path(__file__).parent / "fixtures/edge_cases.json"
TOL = 0.01


@pytest.fixture(scope="module")
def edge_cases() -> list[dict]:
    with EDGE_FILE.open() as handle:
        return json.load(handle)["cases"]


def test_edge_pack_shape(edge_cases) -> None:
    assert len(edge_cases) >= 15


def test_edge_cases_rule_based(client, edge_cases) -> None:
    failures: list[str] = []
    for case in edge_cases:
        case_id = case["id"]
        response = client.post("/optimize-energy", json=case["input"])
        if response.status_code != 200:
            failures.append(f"{case_id}: HTTP {response.status_code}")
            continue
        body = response.json()
        expected = case["expected_output"]["directive_interpretation"]

        for exp, act in zip(expected, body["directive_interpretation"], strict=False):
            if act["directive_type"] != exp["directive_type"]:
                failures.append(
                    f"{case_id} note {exp['note_index']}: "
                    f"{act['directive_type']} != {exp['directive_type']}"
                )
                continue
            exp_adj, act_adj = exp["structured_adjustment"], act["structured_adjustment"]
            if (exp_adj is None) != (act_adj is None):
                failures.append(f"{case_id} note {exp['note_index']}: adjustment nullness")
            elif exp_adj is not None and act_adj is not None:
                if act_adj.get("hours") != exp_adj.get("hours"):
                    failures.append(
                        f"{case_id} note {exp['note_index']}: hours "
                        f"{act_adj.get('hours')} != {exp_adj.get('hours')}"
                    )
                for key, value in exp_adj.items():
                    if key != "hours" and abs(float(act_adj.get(key, -1)) - float(value)) > TOL:
                        failures.append(f"{case_id} note {exp['note_index']}: {key} mismatch")

        _judge_replay(case["input"], body["hourly_plan"], body)
        reference_cost = case["expected_output"]["total_cost_bdt"]
        if body["total_cost_bdt"] > reference_cost + 1.0:
            failures.append(
                f"{case_id}: cost {body['total_cost_bdt']} worse than optimal {reference_cost}"
            )

    assert not failures, "\n".join(failures)
