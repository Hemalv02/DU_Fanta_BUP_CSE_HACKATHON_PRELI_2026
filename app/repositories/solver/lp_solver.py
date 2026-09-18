"""Linear-programming solver repository (scipy HiGHS backend).

The GridWise scheduling problem is a pure LP over 24 hours with five variable
families per hour:

    grid[h] >= 0            grid energy purchased
    solar_used[h] in [0, S_eff(h)]
    charge[h] in [0, C_max(h)]
    discharge[h] in [0, D_max(h)]
    energy[h] in [R_min(h), capacity]

Subject to (Problem Statement Section 09):
    grid[h] + solar_used[h] + discharge[h] - charge[h] = demand[h]   (9.5)
    energy[h] = energy[h-1] + charge[h] - discharge[h]               (9.1)
    energy[23] = initial_energy                                       (9.6)

Objective: minimize SUM(tariff[h] * grid[h])                       (5.2)

Directive effects (5.3) enter as tightened per-hour bounds:
    solar_reduction        -> S_eff(h) = solar(h) * factor
    minimum_battery_reserve-> R_min(h) = max(base_min, directive_min)
    no_charge_window       -> C_max(h) = 0
    no_discharge_window    -> D_max(h) = 0
    max_grid_window        -> grid upper bound = max_grid_kwh
"""

import math
from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog

from app.core.errors import OptimizationError
from app.core.logging import get_logger
from app.schemas.enums import HORIZON_HOURS

logger = get_logger(__name__)

_INF = float(np.inf)


@dataclass(frozen=True)
class OptimizationProblem:
    """Fully materialized per-hour bounds after directives are applied."""

    demand_kwh: tuple[float, ...]
    effective_solar_kwh: tuple[float, ...]
    tariff_bdt_per_kwh: tuple[float, ...]
    capacity_kwh: float
    initial_energy_kwh: float
    base_minimum_energy_kwh: float
    minimum_energy_by_hour: tuple[float, ...]
    max_charge_by_hour: tuple[float, ...]
    max_discharge_by_hour: tuple[float, ...]
    max_grid_by_hour: tuple[float, ...]


@dataclass(frozen=True)
class SolverSchedule:
    """Raw optimal solution; post-processed by the optimization service."""

    grid_kwh: tuple[float, ...]
    solar_used_kwh: tuple[float, ...]
    charge_kwh: tuple[float, ...]
    discharge_kwh: tuple[float, ...]
    energy_after_kwh: tuple[float, ...]
    total_cost_bdt: float


class LPSolverRepository:
    """Solves the materialized LP; raises OptimizationError on infeasibility."""

    def solve(self, problem: OptimizationProblem) -> SolverSchedule:
        n = HORIZON_HOURS
        # Variable layout: [grid(n), solar_used(n), charge(n), discharge(n), energy(n)]
        size = 5 * n
        offset_grid, offset_solar, offset_charge, offset_discharge, offset_energy = (
            0, n, 2 * n, 3 * n, 4 * n,
        )

        cost = np.zeros(size)
        cost[offset_grid : offset_grid + n] = problem.tariff_bdt_per_kwh

        bounds: list[tuple[float, float]] = []
        bounds += [(0.0, problem.max_grid_by_hour[h]) for h in range(n)]
        bounds += [(0.0, problem.effective_solar_kwh[h]) for h in range(n)]
        bounds += [(0.0, problem.max_charge_by_hour[h]) for h in range(n)]
        bounds += [(0.0, problem.max_discharge_by_hour[h]) for h in range(n)]
        bounds += [
            (problem.minimum_energy_by_hour[h], problem.capacity_kwh) for h in range(n)
        ]

        rows: list[dict[int, float]] = []
        rhs: list[float] = []

        # 9.5 energy balance: grid + solar_used + discharge - charge = demand
        for h in range(n):
            row = {
                offset_grid + h: 1.0,
                offset_solar + h: 1.0,
                offset_discharge + h: 1.0,
                offset_charge + h: -1.0,
            }
            rows.append(row)
            rhs.append(problem.demand_kwh[h])

        # 9.1 battery transition: energy[h] - energy[h-1] - charge[h] + discharge[h] = 0
        for h in range(n):
            row = {
                offset_energy + h: 1.0,
                offset_charge + h: -1.0,
                offset_discharge + h: 1.0,
            }
            if h > 0:
                row[offset_energy + h - 1] = -1.0
                rhs.append(0.0)
            else:
                rhs.append(problem.initial_energy_kwh)
            rows.append(row)

        # 9.6 end-of-day neutrality: energy[23] = initial
        rows.append({offset_energy + n - 1: 1.0})
        rhs.append(problem.initial_energy_kwh)

        a_eq = np.zeros((len(rows), size))
        for i, row in enumerate(rows):
            for j, value in row.items():
                a_eq[i, j] = value
        b_eq = np.array(rhs)

        result = linprog(
            c=cost,
            A_eq=a_eq,
            b_eq=b_eq,
            bounds=bounds,
            method="highs",
        )
        if not result.success or result.x is None:
            logger.error("lp_solve_failed", extra={"status": result.message})
            raise OptimizationError("no feasible schedule exists for this scenario")

        x = result.x
        return SolverSchedule(
            grid_kwh=tuple(float(v) for v in x[offset_grid : offset_grid + n]),
            solar_used_kwh=tuple(float(v) for v in x[offset_solar : offset_solar + n]),
            charge_kwh=tuple(float(v) for v in x[offset_charge : offset_charge + n]),
            discharge_kwh=tuple(float(v) for v in x[offset_discharge : offset_discharge + n]),
            energy_after_kwh=tuple(float(v) for v in x[offset_energy : offset_energy + n]),
            total_cost_bdt=float(result.fun) if result.fun is not None else 0.0,
        )


def infinity() -> float:
    """Positive infinity bound (kept explicit for readability)."""
    return _INF


def is_finite(value: float) -> bool:
    return math.isfinite(value)
