"""Energy accounting service: problem materialization, schedule post-processing,
final replay validation, and totals (Problem Statement Sections 05, 09, 11).

All money/energy math is deterministic; the LLM never touches this module.
"""

import math
from dataclasses import dataclass
from typing import cast

from app.core.errors import OptimizationError
from app.core.logging import get_logger
from app.repositories.solver import OptimizationProblem, SolverSchedule
from app.schemas.directives import (
    DirectiveInterpretation,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    NoChargeWindowAdjustment,
    NoDischargeWindowAdjustment,
    SolarReductionAdjustment,
)
from app.schemas.enums import HORIZON_HOURS, NUMERIC_TOLERANCE, BatteryAction
from app.schemas.request import BatterySpec, HourEntry, OptimizeEnergyRequest
from app.schemas.response import HourPlanEntry

logger = get_logger(__name__)

_EPS = 1e-9


@dataclass(frozen=True)
class ScheduleTotals:
    """Top-level aggregates recalculated from the hourly plan (Section 10.1)."""

    total_grid_kwh: float
    total_cost_bdt: float
    peak_grid_kwh: float


class EnergyService:
    """Deterministic energy accounting for the 24-hour horizon."""

    # ------------------------------------------------------------------
    # Directive materialization (Section 5.3)
    # ------------------------------------------------------------------
    def build_problem(
        self,
        request: OptimizeEnergyRequest,
        directives: list[DirectiveInterpretation],
    ) -> OptimizationProblem:
        battery = request.battery
        solar = [entry.solar_kwh for entry in request.hours]
        demand = [entry.demand_kwh for entry in request.hours]
        tariff = [entry.tariff_bdt_per_kwh for entry in request.hours]

        min_energy = [battery.minimum_energy_kwh] * HORIZON_HOURS
        charge_cap = [battery.max_charge_kwh_per_hour] * HORIZON_HOURS
        discharge_cap = [battery.max_discharge_kwh_per_hour] * HORIZON_HOURS
        grid_cap: list[float] = [math.inf] * HORIZON_HOURS

        for directive in directives:
            adjustment = directive.structured_adjustment
            if adjustment is None:
                continue
            # Dispatch on directive_type (the authoritative discriminator):
            # no_charge/no_discharge adjustments are structurally identical,
            # so union type alone cannot distinguish them. The schema
            # validator guarantees the adjustment shape matches the type,
            # which makes these casts sound.
            type_name = directive.directive_type
            if type_name == "solar_reduction":
                reduction = cast(SolarReductionAdjustment, adjustment)
                for h in reduction.hours:
                    solar[h] = solar[h] * reduction.factor
            elif type_name == "minimum_battery_reserve":
                reserve = cast(MinimumBatteryReserveAdjustment, adjustment)
                for h in reserve.hours:
                    min_energy[h] = max(min_energy[h], reserve.minimum_energy_kwh)
            elif type_name == "no_charge_window":
                for h in cast(NoChargeWindowAdjustment, adjustment).hours:
                    charge_cap[h] = 0.0
            elif type_name == "no_discharge_window":
                for h in cast(NoDischargeWindowAdjustment, adjustment).hours:
                    discharge_cap[h] = 0.0
            elif type_name == "max_grid_window":
                cap_adjust = cast(MaxGridWindowAdjustment, adjustment)
                for h in cap_adjust.hours:
                    grid_cap[h] = min(grid_cap[h], cap_adjust.max_grid_kwh)

        return OptimizationProblem(
            demand_kwh=tuple(demand),
            effective_solar_kwh=tuple(round(v, 6) for v in solar),
            tariff_bdt_per_kwh=tuple(tariff),
            capacity_kwh=battery.capacity_kwh,
            initial_energy_kwh=battery.initial_energy_kwh,
            base_minimum_energy_kwh=battery.minimum_energy_kwh,
            minimum_energy_by_hour=tuple(min_energy),
            max_charge_by_hour=tuple(charge_cap),
            max_discharge_by_hour=tuple(discharge_cap),
            max_grid_by_hour=tuple(grid_cap),
        )

    # ------------------------------------------------------------------
    # Post-processing into the response schedule (Section 10.3)
    # ------------------------------------------------------------------
    def post_process(
        self,
        solution: SolverSchedule,
        problem: OptimizationProblem,
        decimals: int,
    ) -> list[HourPlanEntry]:
        """Turn the raw LP vertex into a self-consistent hourly plan.

        Values are rounded to `decimals` places, simultaneous charge/discharge
        is cancelled (energy-neutral), battery energy is recomputed exactly
        from the reported deltas, and grid is recomputed from the Section 9.5
        balance so the judge's replay matches our numbers by construction.
        """
        n = HORIZON_HOURS

        def r(value: float) -> float:
            return round(max(value, 0.0), decimals)

        solar_used = [
            min(r(solution.solar_used_kwh[h]), problem.effective_solar_kwh[h]) for h in range(n)
        ]
        charge = [r(solution.charge_kwh[h]) for h in range(n)]
        discharge = [r(solution.discharge_kwh[h]) for h in range(n)]

        energy = problem.initial_energy_kwh
        entries: list[HourPlanEntry] = []
        for h in range(n):
            # Cancel any simultaneous charge/discharge (energy-neutral).
            overlap = min(charge[h], discharge[h])
            charge[h] = round(charge[h] - overlap, decimals)
            discharge[h] = round(discharge[h] - overlap, decimals)
            charge[h] = min(charge[h], problem.max_charge_by_hour[h])
            discharge[h] = min(discharge[h], problem.max_discharge_by_hour[h])

            # Clamp at zero so rounding drift can never produce a negative
            # reported energy (the response schema forbids it). If the clamp
            # actually engaged, the transition/neutrality checks in
            # replay_validate flag it and the caller retries at finer
            # precision — a clamped value can never reach the client.
            energy = round(max(energy + charge[h] - discharge[h], 0.0), decimals)

            if charge[h] > _EPS:
                action, battery_kwh = BatteryAction.CHARGE, charge[h]
            elif discharge[h] > _EPS:
                action, battery_kwh = BatteryAction.DISCHARGE, discharge[h]
            else:
                action, battery_kwh = BatteryAction.IDLE, 0.0

            grid = r(
                problem.demand_kwh[h] + charge[h] - solar_used[h] - discharge[h]
            )

            entries.append(
                HourPlanEntry(
                    hour=h,
                    grid_kwh=grid,
                    solar_used_kwh=solar_used[h],
                    battery_action=action.value,
                    battery_kwh=round(battery_kwh, decimals),
                    battery_energy_after_kwh=energy,
                )
            )
        return entries

    # ------------------------------------------------------------------
    # Final replay guardrail (Sections 08 "Final replay", 09, 11.3)
    # ------------------------------------------------------------------
    def replay_validate(
        self,
        request: OptimizeEnergyRequest,
        problem: OptimizationProblem,
        plan: list[HourPlanEntry],
    ) -> None:
        """Replay the completed schedule hour by hour; raise on any violation."""
        tol = NUMERIC_TOLERANCE
        energy = problem.initial_energy_kwh

        if [entry.hour for entry in plan] != list(range(HORIZON_HOURS)):
            raise OptimizationError("hourly_plan must cover hours 0..23 in order")

        for h, entry in enumerate(plan):
            # 11.3 finite & non-negative values
            values = (
                entry.grid_kwh,
                entry.solar_used_kwh,
                entry.battery_kwh,
                entry.battery_energy_after_kwh,
            )
            if any(not math.isfinite(v) or v < -tol for v in values):
                raise OptimizationError(f"hour {h}: negative or non-finite value")

            # 9.5 energy balance
            balance = (
                entry.grid_kwh
                + entry.solar_used_kwh
                + (entry.battery_kwh if entry.battery_action == "discharge" else 0.0)
                - (entry.battery_kwh if entry.battery_action == "charge" else 0.0)
                - problem.demand_kwh[h]
            )
            if abs(balance) > tol:
                raise OptimizationError(f"hour {h}: energy balance violated")

            # 9.4 solar usage within effective solar
            if entry.solar_used_kwh > problem.effective_solar_kwh[h] + tol:
                raise OptimizationError(f"hour {h}: solar usage exceeds effective solar")

            # 10.3 action consistency + 9.3 hourly rate limits + 5.3 windows
            charge = entry.battery_kwh if entry.battery_action == "charge" else 0.0
            discharge = entry.battery_kwh if entry.battery_action == "discharge" else 0.0
            if entry.battery_action == "idle" and entry.battery_kwh > tol:
                raise OptimizationError(f"hour {h}: idle action with nonzero battery_kwh")
            if charge > problem.max_charge_by_hour[h] + tol:
                raise OptimizationError(f"hour {h}: charge exceeds hourly rate limit")
            if discharge > problem.max_discharge_by_hour[h] + tol:
                raise OptimizationError(f"hour {h}: discharge exceeds hourly rate limit")

            # 5.3 max_grid_window
            if entry.grid_kwh > problem.max_grid_by_hour[h] + tol:
                raise OptimizationError(f"hour {h}: grid import exceeds directive cap")

            # 9.1 battery transition
            expected_energy = energy + charge - discharge
            if abs(entry.battery_energy_after_kwh - expected_energy) > tol:
                raise OptimizationError(f"hour {h}: battery transition mismatch")

            # 9.2 battery bounds (incl. directive reserve floor)
            if not (
                problem.minimum_energy_by_hour[h] - tol
                <= entry.battery_energy_after_kwh
                <= problem.capacity_kwh + tol
            ):
                raise OptimizationError(f"hour {h}: battery energy outside allowed bounds")

            energy = entry.battery_energy_after_kwh

        # 9.6 end-of-day neutrality
        if abs(energy - problem.initial_energy_kwh) > tol:
            raise OptimizationError("end-of-day battery energy must equal initial energy")

        logger.info(
            "final_replay_passed",
            extra={"scenario_id": request.scenario_id},
        )

    # ------------------------------------------------------------------
    # Totals (Section 10.1 / 11.3 recalculated-from-plan rule)
    # ------------------------------------------------------------------
    def compute_totals(
        self, plan: list[HourPlanEntry], tariff: tuple[float, ...]
    ) -> ScheduleTotals:
        total_grid = round(sum(entry.grid_kwh for entry in plan), 2)
        total_cost = round(
            sum(entry.grid_kwh * tariff[entry.hour] for entry in plan), 2
        )
        peak_grid = round(max(entry.grid_kwh for entry in plan), 2)
        return ScheduleTotals(
            total_grid_kwh=total_grid,
            total_cost_bdt=total_cost,
            peak_grid_kwh=peak_grid,
        )


def build_plan_summary(
    directives: list[DirectiveInterpretation],
    totals: ScheduleTotals,
    battery: BatterySpec,
    hours: list[HourEntry],
) -> str:
    """Deterministic human-readable strategy summary (Section 10.1)."""
    applied = [d for d in directives if d.applies]
    parts: list[str] = []
    if applied:
        described = ", ".join(d.directive_type.replace("_", " ") for d in applied)
        parts.append(f"Applied {len(applied)} operator directive(s): {described}.")
    else:
        parts.append("No operator directive changed the base schedule.")

    peak_tariff_hour = max(hours, key=lambda entry: entry.tariff_bdt_per_kwh).hour
    low_tariff_hour = min(hours, key=lambda entry: entry.tariff_bdt_per_kwh).hour
    parts.append(
        "Solar is consumed first; the battery charges in cheaper hours "
        f"(lowest tariff at hour {low_tariff_hour}) and discharges in expensive hours "
        f"(peak tariff at hour {peak_tariff_hour}), returning to its initial "
        f"{battery.initial_energy_kwh:g} kWh by end of day."
    )
    parts.append(
        f"Grid import totals {totals.total_grid_kwh:g} kWh at {totals.total_cost_bdt:g} BDT "
        f"with a peak of {totals.peak_grid_kwh:g} kWh per hour."
    )
    return " ".join(parts)
