"""Optimization service: guardrailed directives -> valid low-cost schedule.

Pipeline (Problem Statement Sections 03, 05, 09):
    directives -> materialized LP (per-hour bounds)
               -> scipy/HiGHS solver repository
               -> post-processed hourly plan
               -> final deterministic replay (guardrail)
               -> totals recalculated from the plan
"""

from pydantic import ValidationError

from app.core.config import Settings
from app.core.errors import OptimizationError
from app.core.logging import get_logger
from app.repositories.solver import LPSolverRepository
from app.schemas.directives import DirectiveInterpretation
from app.schemas.request import OptimizeEnergyRequest
from app.schemas.response import HourPlanEntry
from app.services.energy_service import (
    EnergyService,
    ScheduleTotals,
    build_plan_summary,
)

logger = get_logger(__name__)

#: Finer rounding precisions tried (in order) when the replay of a rounded
#: plan violates the judge tolerance. Rounding battery deltas to `d` decimals
#: can accumulate up to 24 x 0.5 * 10^-d kWh of drift against the LP's exact
#: neutrality; 4 decimals bound that drift at ~0.001 kWh, safely inside the
#: 0.01 tolerance, so every schedule that is valid in exact arithmetic
#: survives rounding at some rung of this ladder.
_FINER_PRECISIONS: tuple[int, ...] = (4, 6)


class OptimizationService:
    """Runs the deterministic scheduling pipeline for one scenario."""

    def __init__(self, solver_repository: LPSolverRepository, settings: Settings) -> None:
        self._solver = solver_repository
        self._settings = settings
        self._energy = EnergyService()

    def _precision_ladder(self) -> list[int]:
        """Configured rounding first, then finer rungs (deduplicated)."""
        configured = self._settings.schedule_rounding_decimals
        ladder = [configured]
        ladder.extend(d for d in _FINER_PRECISIONS if d > configured)
        return ladder

    def optimize(
        self,
        request: OptimizeEnergyRequest,
        directives: list[DirectiveInterpretation],
    ) -> tuple[list[HourPlanEntry], ScheduleTotals, str]:
        problem = self._energy.build_problem(request, directives)
        solution = self._solver.solve(problem)

        # Section 08 "Final replay": verify every directive was actually
        # applied. A plan that only fails replay because of rounding drift
        # (repeating-decimal LP vertices) is re-rounded one rung finer
        # instead of failing the request; genuinely invalid plans fail at
        # every precision and still raise the controlled error.
        plan: list[HourPlanEntry] | None = None
        replay_error: OptimizationError | None = None
        for decimals in self._precision_ladder():
            try:
                candidate = self._energy.post_process(solution, problem, decimals)
                self._energy.replay_validate(request, problem, candidate)
            except OptimizationError as error:
                replay_error = error
            except ValidationError as error:
                # Defensive: post_process should already clamp drift-induced
                # negatives; anything else is still retried finer and finally
                # surfaced as a controlled optimization failure.
                replay_error = OptimizationError(f"invalid rounded plan: {error.error_count()}")
            else:
                plan, replay_error = candidate, None
                break
            logger.warning(
                "replay_retry_at_finer_precision",
                extra={"decimals": decimals},
            )
        if plan is None:
            assert replay_error is not None
            raise replay_error

        totals = self._energy.compute_totals(plan, problem.tariff_bdt_per_kwh)
        summary = build_plan_summary(directives, totals, request.battery, request.hours)

        logger.info(
            "optimization_completed",
            extra={
                "scenario_id": request.scenario_id,
                "total_cost_bdt": totals.total_cost_bdt,
            },
        )
        return plan, totals, summary
