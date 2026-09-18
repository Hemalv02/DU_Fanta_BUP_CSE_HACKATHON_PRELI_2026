"""Run the public sample pack against a live GridWise endpoint.

Usage:
    uv run python scripts/run_public_samples.py [--base-url http://127.0.0.1:8000]

For each case it checks (mirroring the judge):
  - HTTP 200 and the full response schema shape
  - interpretation semantics vs. the public reference
  - an independent hour-by-hour replay of hourly_plan against the
    interpreted directives (effective solar, battery rules, energy balance,
    end-of-day neutrality, directive application)
  - totals recalculated from hourly_plan
  - cost quality vs. the reference optimum

Exit code 0 = all cases passed.
"""

import argparse
import json
import pathlib
import sys
import time

import httpx

DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_CASES = pathlib.Path(__file__).resolve().parents[1] / (
    "tests/fixtures/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)
TOL = 0.01


def check_interpretation(case: dict, body: dict) -> list[str]:
    errors: list[str] = []
    expected = case["expected_output"]["directive_interpretation"]
    actual = body.get("directive_interpretation")
    notes_count = len(case["input"]["operator_notes"])

    if not isinstance(actual, list) or len(actual) != notes_count:
        return [f"expected {notes_count} interpretation entries, got {len(actual or [])}"]
    if [e.get("note_index") for e in actual] != list(range(notes_count)):
        errors.append("interpretation entries not in note_index order 0..N-1")

    for exp, act in zip(expected, actual, strict=False):
        if act.get("applies") != exp["applies"]:
            errors.append(
                f"note {exp['note_index']}: applies {act.get('applies')} != {exp['applies']}"
            )
        if act.get("directive_type") != exp["directive_type"]:
            errors.append(
                f"note {exp['note_index']}: type "
                f"{act.get('directive_type')} != {exp['directive_type']}"
            )
            continue
        exp_adj, act_adj = exp["structured_adjustment"], act.get("structured_adjustment")
        if exp_adj is None:
            if act_adj is not None:
                errors.append(f"note {exp['note_index']}: expected null adjustment")
            continue
        if not isinstance(act_adj, dict):
            errors.append(f"note {exp['note_index']}: missing structured_adjustment")
            continue
        if act_adj.get("hours") != exp_adj.get("hours"):
            errors.append(
                f"note {exp['note_index']}: hours {act_adj.get('hours')} != {exp_adj.get('hours')}"
            )
        for key, value in exp_adj.items():
            if key == "hours":
                continue
            if key not in act_adj or abs(float(act_adj[key]) - float(value)) > TOL:
                errors.append(f"note {exp['note_index']}: {key} mismatch")
    return errors


def judge_replay(case_input: dict, body: dict) -> list[str]:
    """Independent replay of hourly_plan against the interpreted directives."""
    errors: list[str] = []
    battery = case_input["battery"]
    hours = {h["hour"]: h for h in case_input["hours"]}
    plan = body.get("hourly_plan", [])

    if [e.get("hour") for e in plan] != list(range(24)):
        return ["hourly_plan must contain hours 0..23 in order"]

    effective_solar = {h: entry["solar_kwh"] for h, entry in hours.items()}
    reserve_floor: dict[int, float] = {h: battery["minimum_energy_kwh"] for h in range(24)}
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    grid_cap: dict[int, float] = {}
    for d in body["directive_interpretation"]:
        adj = d.get("structured_adjustment")
        if not adj:
            continue
        for h in adj["hours"]:
            t = d["directive_type"]
            if t == "solar_reduction":
                effective_solar[h] = hours[h]["solar_kwh"] * adj["factor"]
            elif t == "minimum_battery_reserve":
                reserve_floor[h] = max(reserve_floor[h], adj["minimum_energy_kwh"])
            elif t == "no_charge_window":
                no_charge.add(h)
            elif t == "no_discharge_window":
                no_discharge.add(h)
            elif t == "max_grid_window":
                grid_cap[h] = min(grid_cap.get(h, float("inf")), adj["max_grid_kwh"])

    energy = battery["initial_energy_kwh"]
    total_grid = total_cost = 0.0
    peak = 0.0

    def fail(msg: str) -> None:
        errors.append(msg)

    for entry in plan:
        h = entry["hour"]
        charge = entry["battery_kwh"] if entry["battery_action"] == "charge" else 0.0
        discharge = entry["battery_kwh"] if entry["battery_action"] == "discharge" else 0.0

        lhs = entry["grid_kwh"] + entry["solar_used_kwh"] + discharge
        rhs = hours[h]["demand_kwh"] + charge
        if abs(lhs - rhs) > TOL:
            fail(f"h{h}: energy balance {lhs:.3f} != {rhs:.3f}")
        if not -TOL <= entry["solar_used_kwh"] <= effective_solar[h] + TOL:
            fail(
                f"h{h}: solar_used {entry['solar_used_kwh']} "
                f"outside [0, {effective_solar[h]:.2f}]"
            )
        if charge > battery["max_charge_kwh_per_hour"] + TOL:
            fail(f"h{h}: charge rate")
        if discharge > battery["max_discharge_kwh_per_hour"] + TOL:
            fail(f"h{h}: discharge rate")
        if h in no_charge and charge > TOL:
            fail(f"h{h}: no_charge_window violated")
        if h in no_discharge and discharge > TOL:
            fail(f"h{h}: no_discharge_window violated")
        if h in grid_cap and entry["grid_kwh"] > grid_cap[h] + TOL:
            fail(f"h{h}: max_grid_window violated ({entry['grid_kwh']} > {grid_cap[h]})")

        energy += charge - discharge
        if abs(entry["battery_energy_after_kwh"] - energy) > TOL:
            fail(f"h{h}: battery transition")
        if not (
            reserve_floor[h] - TOL
            <= entry["battery_energy_after_kwh"]
            <= battery["capacity_kwh"] + TOL
        ):
            fail(f"h{h}: battery bounds")
        energy = entry["battery_energy_after_kwh"]

        total_grid += entry["grid_kwh"]
        total_cost += entry["grid_kwh"] * hours[h]["tariff_bdt_per_kwh"]
        peak = max(peak, entry["grid_kwh"])

    if abs(energy - battery["initial_energy_kwh"]) > TOL:
        fail(f"end-of-day neutrality: {energy} != {battery['initial_energy_kwh']}")
    if abs(body["total_grid_kwh"] - total_grid) > TOL:
        fail("total_grid_kwh mismatch")
    if abs(body["total_cost_bdt"] - total_cost) > TOL:
        fail("total_cost_bdt mismatch")
    if abs(body["peak_grid_kwh"] - peak) > TOL:
        fail("peak_grid_kwh mismatch")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--cases", type=pathlib.Path, default=DEFAULT_CASES)
    args = parser.parse_args()

    with args.cases.open() as handle:
        cases = json.load(handle)["cases"]

    health = httpx.get(f"{args.base_url}/health", timeout=10)
    if health.status_code != 200 or health.json().get("status") != "ok":
        print(f"HEALTH CHECK FAILED: {health.status_code} {health.text}")
        return 2
    print(f"health OK: {health.json()}")

    passed = 0
    for case in cases:
        case_id = case["id"]
        started = time.perf_counter()
        response = httpx.post(
            f"{args.base_url}/optimize-energy", json=case["input"], timeout=30
        )
        elapsed = time.perf_counter() - started

        if response.status_code != 200:
            print(f"FAIL {case_id}: HTTP {response.status_code} {response.text[:200]}")
            continue
        body = response.json()

        errors = []
        if body.get("scenario_id") != case["input"]["scenario_id"]:
            errors.append("scenario_id echo mismatch")
        errors += check_interpretation(case, body)
        errors += judge_replay(case["input"], body)

        reference_cost = case["expected_output"]["total_cost_bdt"]
        if body["total_cost_bdt"] > reference_cost + 1.0:
            errors.append(
                f"cost {body['total_cost_bdt']} worse than reference {reference_cost}"
            )

        if errors:
            print(f"FAIL {case_id} ({elapsed:.2f}s):")
            for error in errors:
                print(f"   - {error}")
        else:
            passed += 1
            ratio = body["total_cost_bdt"] / reference_cost if reference_cost else 1.0
            print(
                f"PASS {case_id} ({elapsed:.2f}s) "
                f"cost={body['total_cost_bdt']:.1f} ref={reference_cost:.1f} ratio={ratio:.4f}"
            )

    print(f"\n{passed}/{len(cases)} cases passed")
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
