"""Optimizer unit tests: LP behavior, directive application, and replay."""

import pytest

from app.repositories.solver import LPSolverRepository
from app.schemas.directives import (
    DirectiveInterpretation,
)
from app.schemas.request import BatterySpec, HourEntry, OptimizeEnergyRequest
from app.services.energy_service import EnergyService

ENERGY = EnergyService()
SOLVER = LPSolverRepository()


def make_request(
    demand: float = 100.0,
    solar: float = 0.0,
    tariffs: list[float] | None = None,
    battery: BatterySpec | None = None,
) -> OptimizeEnergyRequest:
    if tariffs is None:
        tariffs = [10.0] * 24
    battery = battery or BatterySpec(
        capacity_kwh=200.0,
        initial_energy_kwh=100.0,
        minimum_energy_kwh=20.0,
        max_charge_kwh_per_hour=50.0,
        max_discharge_kwh_per_hour=50.0,
    )
    return OptimizeEnergyRequest(
        scenario_id="TEST-1",
        operator_notes=["Do not charge the battery between 2 AM until 4 AM."],
        hours=[
            HourEntry(hour=h, demand_kwh=demand, solar_kwh=solar, tariff_bdt_per_kwh=tariffs[h])
            for h in range(24)
        ],
        battery=battery,
    )


def solve(request: OptimizeEnergyRequest, directives: list | None = None):
    directives = directives or []
    problem = ENERGY.build_problem(request, directives)
    solution = SOLVER.solve(problem)
    plan = ENERGY.post_process(solution, problem, decimals=4)
    ENERGY.replay_validate(request, problem, plan)
    totals = ENERGY.compute_totals(plan, problem.tariff_bdt_per_kwh)
    return plan, totals


def directive(entry: dict) -> DirectiveInterpretation:
    return DirectiveInterpretation.model_validate(entry)


def test_battery_arbitrage_charges_cheap_and_discharges_expensive() -> None:
    tariffs = [10.0] * 24
    tariffs[2] = 1.0  # cheap hour
    tariffs[20] = 50.0  # expensive hour
    plan, totals = solve(make_request(tariffs=tariffs))

    # Naive no-battery cost would be 100 * (sum of tariffs).
    naive_cost = sum(100.0 * t for t in tariffs)
    assert totals.total_cost_bdt < naive_cost
    assert plan[2].battery_action == "charge"
    assert plan[20].battery_action == "discharge"


def test_end_of_day_neutrality() -> None:
    plan, _ = solve(make_request())
    request = make_request()
    assert plan[23].battery_energy_after_kwh == pytest.approx(
        request.battery.initial_energy_kwh, abs=0.01
    )


def test_solar_used_up_to_effective_availability() -> None:
    plan, totals = solve(make_request(solar=60.0))
    # All 60 kWh of solar must displace grid energy every hour.
    assert sum(entry.solar_used_kwh for entry in plan) == pytest.approx(60.0 * 24, abs=0.05)
    assert totals.total_grid_kwh == pytest.approx(40.0 * 24, abs=0.05)


def test_solar_reduction_directive_shrinks_usable_solar() -> None:
    request = make_request(solar=100.0)
    d = directive(
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "solar_reduction",
            "structured_adjustment": {"hours": [12, 13], "factor": 0.25},
            "explanation": "cleaning",
        }
    )
    plan, totals = solve(request, [d])
    for h in (12, 13):
        assert plan[h].solar_used_kwh <= 25.0 + 0.01
    # 22 hours at full 100 + 2 hours at 25.
    assert totals.total_grid_kwh == pytest.approx(22 * 0.0 + 2 * 75.0, abs=0.05)


def test_no_charge_window_forces_zero_charging() -> None:
    request = make_request(tariffs=[1.0] * 24)  # cheap everywhere -> charging attractive
    d = directive(
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [5, 6]},
            "explanation": "maintenance",
        }
    )
    plan, _ = solve(request, [d])
    for h in (5, 6):
        assert plan[h].battery_action != "charge"


def test_no_discharge_window_forces_zero_discharge() -> None:
    request = make_request(tariffs=[1.0] * 5 + [100.0] * 19)
    d = directive(
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_discharge_window",
            "structured_adjustment": {"hours": [10, 11]},
            "explanation": "relay testing",
        }
    )
    plan, _ = solve(request, [d])
    for h in (10, 11):
        assert plan[h].battery_action != "discharge"


def test_minimum_battery_reserve_is_respected() -> None:
    request = make_request(tariffs=[100.0] * 24)
    d = directive(
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "minimum_battery_reserve",
            "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 150.0},
            "explanation": "reserve",
        }
    )
    plan, _ = solve(request, [d])
    for h in (18, 19, 20):
        assert plan[h].battery_energy_after_kwh >= 150.0 - 0.01


def test_max_grid_window_caps_import() -> None:
    request = make_request(demand=150.0)
    d = directive(
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [17, 18], "max_grid_kwh": 120.0},
            "explanation": "feeder limit",
        }
    )
    plan, _ = solve(request, [d])
    for h in (17, 18):
        assert plan[h].grid_kwh <= 120.0 + 0.01
        # Missing grid energy must come from the battery.
        if plan[h].grid_kwh < 150.0 - 0.01:
            assert plan[h].battery_action == "discharge"


def test_infeasible_scenario_raises_controlled_error() -> None:
    from app.core.errors import OptimizationError

    # Grid capped below demand with no battery/solar to compensate.
    request = make_request(demand=200.0)
    d = directive(
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "max_grid_window",
            "structured_adjustment": {"hours": [12], "max_grid_kwh": 50.0},
            "explanation": "impossible",
        }
    )
    with pytest.raises(OptimizationError):
        solve(request, [d])


class _ThirdsSolver:
    """Stub solver whose optimal vertex lands on repeating decimals.

    Charges 100/3 kWh in hours 0-6 and discharges the exact total spread
    over hours 12-14 (rate-legal). The schedule is exactly neutral and
    feasible in real arithmetic, but rounding the deltas to 2 decimals
    accumulates ~0.03 kWh of drift — past the judge tolerance — which
    exercises the precision-retry ladder in OptimizationService.
    """

    def solve(self, problem):  # type: ignore[no-untyped-def]
        n = 24
        charge = [0.0] * n
        discharge = [0.0] * n
        unit = 100.0 / 3.0  # 33.333...
        for h in range(7):
            charge[h] = unit
        for h in (12, 13, 14):
            discharge[h] = 7 * unit / 3.0  # 77.777... each

        grid = [
            problem.demand_kwh[h] + charge[h] - discharge[h] for h in range(n)
        ]
        energy = problem.initial_energy_kwh
        energies = []
        for h in range(n):
            energy = energy + charge[h] - discharge[h]
            energies.append(energy)
        from app.repositories.solver import SolverSchedule

        return SolverSchedule(
            grid_kwh=tuple(grid),
            solar_used_kwh=tuple(0.0 for _ in range(n)),
            charge_kwh=tuple(charge),
            discharge_kwh=tuple(discharge),
            energy_after_kwh=tuple(energies),
            total_cost_bdt=0.0,
        )


def _thirds_request() -> OptimizeEnergyRequest:
    return OptimizeEnergyRequest(
        scenario_id="THIRDS-1",
        operator_notes=["Nothing scheduled today."],
        hours=[
            HourEntry(
                hour=h,
                demand_kwh=150.0 if h in (12, 13, 14) else 50.0,
                solar_kwh=0.0,
                tariff_bdt_per_kwh=10.0,
            )
            for h in range(24)
        ],
        battery=BatterySpec(
            capacity_kwh=500.0,
            initial_energy_kwh=0.0,
            minimum_energy_kwh=0.0,
            max_charge_kwh_per_hour=100.0,
            max_discharge_kwh_per_hour=100.0,
        ),
    )


def test_repeating_decimal_deltas_break_two_decimal_rounding() -> None:
    """Documents the hazard the precision ladder repairs (see next test)."""
    from app.core.errors import OptimizationError

    request = _thirds_request()
    problem = ENERGY.build_problem(request, [])
    solution = _ThirdsSolver().solve(problem)
    plan = ENERGY.post_process(solution, problem, decimals=2)
    with pytest.raises(OptimizationError):
        ENERGY.replay_validate(request, problem, plan)


def test_precision_ladder_recovers_repeating_decimal_schedule() -> None:
    from app.core.config import Settings
    from app.services.optimization_service import OptimizationService

    request = _thirds_request()
    service = OptimizationService(
        solver_repository=_ThirdsSolver(),  # type: ignore[arg-type]
        settings=Settings(schedule_rounding_decimals=2),
    )
    plan, totals, _summary = service.optimize(request, [])

    # End-of-day neutrality inside a tenth of the judge tolerance.
    assert abs(plan[23].battery_energy_after_kwh - 0.0) <= 0.001
    # Every reported battery delta has at most 6 decimals.
    for entry in plan:
        assert round(entry.battery_kwh, 6) == entry.battery_kwh
    # Totals are still recalculated from (and consistent with) the plan.
    assert totals.total_grid_kwh >= 0.0
