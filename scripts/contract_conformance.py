"""Section-by-section contract conformance check against a live endpoint.

Walks the Problem Statement PDF item by item — endpoints (06), request
validation (07), response schema (10), battery/energy rules (09), and the
judge consistency checks (11.3) — and prints PASS/FAIL per numbered check.

Usage: uv run python scripts/contract_conformance.py [--base-url http://127.0.0.1:8000]
"""

import argparse
import copy
import json
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

DEFAULT_URL = "http://127.0.0.1:8000"
CASES_FILE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "tests/fixtures/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"
)
TOL = 0.01

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str) -> None:
    results.append((ok, label))
    print(f"{'PASS' if ok else 'FAIL'}  {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=DEFAULT_URL)
    args = parser.parse_args()
    client = httpx.Client(base_url=args.base_url, timeout=30)

    with CASES_FILE.open() as handle:
        base = json.load(handle)["cases"][0]["input"]

    # ---------------- Section 06: API contract --------------------------
    health = client.get("/health")
    check(health.status_code == 200, "06: GET /health returns 200")
    check(health.json() == {"status": "ok"}, '06: health body is exactly {"status":"ok"}')

    ok_resp = client.post("/optimize-energy", json=base)
    check(ok_resp.status_code == 200, "06: valid scenario returns 200")
    body = ok_resp.json()

    # ---------------- Section 07: request validation --------------------
    def post_expect_400(label: str, mutate) -> None:
        payload = copy.deepcopy(base)
        mutate(payload)
        response = client.post("/optimize-energy", json=payload)
        check(response.status_code == 400, f"07: {label} -> 400 (got {response.status_code})")

    post_expect_400("missing scenario_id", lambda p: p.pop("scenario_id"))
    post_expect_400("empty scenario_id", lambda p: p.update(scenario_id=""))
    post_expect_400("missing operator_notes", lambda p: p.pop("operator_notes"))
    post_expect_400("zero notes", lambda p: p.update(operator_notes=[]))
    post_expect_400("four notes", lambda p: p.update(operator_notes=["a", "b", "c", "d"]))
    post_expect_400("blank note", lambda p: p.update(operator_notes=["   "]))
    post_expect_400("note is not a string", lambda p: p.update(operator_notes=[42]))
    post_expect_400("missing hours", lambda p: p.pop("hours"))
    post_expect_400("23 hours", lambda p: p.update(hours=p["hours"][:23]))
    post_expect_400("25 hours", lambda p: p.update(hours=p["hours"] + [p["hours"][0]]))
    post_expect_400("duplicate hour", lambda p: p["hours"][5].update(hour=4))
    post_expect_400("hour 24", lambda p: p["hours"][0].update(hour=24))
    post_expect_400("hour -1", lambda p: p["hours"][0].update(hour=-1))
    post_expect_400("missing demand_kwh", lambda p: p["hours"][0].pop("demand_kwh"))
    post_expect_400("negative demand", lambda p: p["hours"][0].update(demand_kwh=-1))
    post_expect_400("negative solar", lambda p: p["hours"][1].update(solar_kwh=-0.5))
    post_expect_400("negative tariff", lambda p: p["hours"][1].update(tariff_bdt_per_kwh=-2))
    post_expect_400("string number", lambda p: p["hours"][2].update(demand_kwh="lots"))
    post_expect_400("missing battery", lambda p: p.pop("battery"))
    post_expect_400("battery missing field", lambda p: p["battery"].pop("capacity_kwh"))
    post_expect_400(
        "initial above capacity",
        lambda p: p["battery"].update(initial_energy_kwh=p["battery"]["capacity_kwh"] + 1),
    )
    post_expect_400(
        "initial below minimum",
        lambda p: p["battery"].update(initial_energy_kwh=p["battery"]["minimum_energy_kwh"] - 1),
    )
    post_expect_400("negative capacity", lambda p: p["battery"].update(capacity_kwh=-10))

    malformed = client.post(
        "/optimize-energy", content="{broken", headers={"Content-Type": "application/json"}
    )
    check(malformed.status_code == 400, "06.1/07: malformed JSON -> 400")
    err = malformed.json()
    check(
        isinstance(err, dict) and "error" in err and "Traceback" not in str(err),
        "06.1: error body is controlled (no stack trace)",
    )
    # But a zero-rate battery must be ACCEPTED (spec defines maxima, not >0).
    zero_rate = copy.deepcopy(base)
    zero_rate["battery"]["max_charge_kwh_per_hour"] = 0
    zero_rate["operator_notes"] = ["The cafeteria menu changes tomorrow."]
    resp = client.post("/optimize-energy", json=zero_rate)
    check(resp.status_code == 200, "07.3: zero charge rate accepted (legal maximum)")

    # ---------------- Section 10: response schema -----------------------
    notes_count = len(base["operator_notes"])
    check(body.get("scenario_id") == base["scenario_id"], "10.1: scenario_id echoes request")
    interp = body.get("directive_interpretation", [])
    check(isinstance(interp, list) and len(interp) == notes_count, "10.2: one entry per note")
    check(
        [e.get("note_index") for e in interp] == list(range(notes_count)),
        "05.1: note_index order 0..N-1, no gaps/duplicates",
    )
    shape_ok = True
    for e in interp:
        keys = set(e.keys())
        adj = e.get("structured_adjustment")
        if e["directive_type"] == "no_op":
            shape_ok &= e["applies"] is False and adj is None
        else:
            shape_ok &= e["applies"] is True and isinstance(adj, dict) and bool(adj.get("hours"))
            if isinstance(adj, dict) and adj.get("hours") is not None:
                hours = adj["hours"]
                shape_ok &= bool(
                    hours == sorted(set(hours))
                    and all(isinstance(h, int) and 0 <= h <= 23 for h in hours)
                )
        shape_ok &= keys == {
            "note_index", "applies", "directive_type", "structured_adjustment", "explanation"
        }
        shape_ok &= bool(isinstance(e.get("explanation"), str) and e["explanation"])
    check(shape_ok, "10.2/08: applies semantics + exact adjustment shapes + hour rules")

    plan = body.get("hourly_plan", [])
    check(isinstance(plan, list) and len(plan) == 24, "10.1: hourly_plan has exactly 24 entries")
    check([e["hour"] for e in plan] == list(range(24)), "11.3: hours 0..23 unique in order")

    entries_ok = action_ok = True
    for e in plan:
        values = (
            e["grid_kwh"], e["solar_used_kwh"], e["battery_kwh"], e["battery_energy_after_kwh"]
        )
        entries_ok &= all(
            isinstance(v, (int, float)) and v == v and abs(v) != float("inf") and v >= 0
            for v in values
        )
        action_ok &= e["battery_action"] in ("charge", "discharge", "idle")
        if e["battery_action"] == "idle":
            action_ok &= e["battery_kwh"] == 0
        else:
            action_ok &= e["battery_kwh"] > 0
    check(entries_ok, "11.3: all hourly numbers finite and non-negative")
    check(action_ok, "10.3: battery_action enum + battery_kwh consistency (0 iff idle)")
    check(
        bool(isinstance(body.get("plan_summary"), str) and body["plan_summary"]),
        "10.1: plan_summary is a non-empty string",
    )

    # ---------------- Section 09 + 11.3: physics replay ------------------
    battery = base["battery"]
    hours = {h["hour"]: h for h in base["hours"]}
    effective_solar = {h: x["solar_kwh"] for h, x in hours.items()}
    reserve = {h: battery["minimum_energy_kwh"] for h in range(24)}
    no_charge: set[int] = set()
    no_discharge: set[int] = set()
    grid_cap: dict[int, float] = {}
    for d in interp:
        adj = d.get("structured_adjustment")
        if not adj:
            continue
        for h in adj["hours"]:
            t = d["directive_type"]
            if t == "solar_reduction":
                effective_solar[h] = hours[h]["solar_kwh"] * adj["factor"]
            elif t == "minimum_battery_reserve":
                reserve[h] = max(reserve[h], adj["minimum_energy_kwh"])
            elif t == "no_charge_window":
                no_charge.add(h)
            elif t == "no_discharge_window":
                no_discharge.add(h)
            elif t == "max_grid_window":
                grid_cap[h] = min(grid_cap.get(h, float("inf")), adj["max_grid_kwh"])

    energy = battery["initial_energy_kwh"]
    physics_ok = True
    total_grid = total_cost = 0.0
    peak = 0.0
    for e in plan:
        h = e["hour"]
        charge = e["battery_kwh"] if e["battery_action"] == "charge" else 0.0
        discharge = e["battery_kwh"] if e["battery_action"] == "discharge" else 0.0
        physics_ok &= abs(
            (e["grid_kwh"] + e["solar_used_kwh"] + discharge)
            - (hours[h]["demand_kwh"] + charge)
        ) <= TOL                                                    # 9.5
        physics_ok &= 0 <= e["solar_used_kwh"] <= effective_solar[h] + TOL   # 9.4
        physics_ok &= charge <= battery["max_charge_kwh_per_hour"] + TOL     # 9.3
        physics_ok &= discharge <= battery["max_discharge_kwh_per_hour"] + TOL
        physics_ok &= not (h in no_charge and charge > TOL)                  # 5.3
        physics_ok &= not (h in no_discharge and discharge > TOL)
        if h in grid_cap:
            physics_ok &= e["grid_kwh"] <= grid_cap[h] + TOL
        energy += charge - discharge
        physics_ok &= abs(e["battery_energy_after_kwh"] - energy) <= TOL     # 9.1
        physics_ok &= reserve[h] - TOL <= e["battery_energy_after_kwh"] <= battery[
            "capacity_kwh"
        ] + TOL                                                             # 9.2
        energy = e["battery_energy_after_kwh"]
        total_grid += e["grid_kwh"]
        total_cost += e["grid_kwh"] * hours[h]["tariff_bdt_per_kwh"]
        peak = max(peak, e["grid_kwh"])
    physics_ok &= abs(energy - battery["initial_energy_kwh"]) <= TOL        # 9.6
    check(physics_ok, "09/5.3: full physics replay (balance, solar, rates, windows, bounds)")

    check(abs(body["total_grid_kwh"] - total_grid) <= TOL, "11.3: total_grid_kwh matches plan")
    check(abs(body["total_cost_bdt"] - total_cost) <= TOL, "11.3: total_cost_bdt matches plan")
    check(abs(body["peak_grid_kwh"] - peak) <= TOL, "11.3: peak_grid_kwh matches plan")

    failed = [label for ok, label in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} contract checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
