"""POST /optimize-energy — interpretation + optimization endpoint (Section 06)."""

from fastapi import APIRouter, Depends, status

from app.dependencies import get_interpretation_service, get_optimization_service
from app.schemas.request import OptimizeEnergyRequest
from app.schemas.response import OptimizeEnergyResponse
from app.services.interpretation_service import InterpretationService
from app.services.optimization_service import OptimizationService

router = APIRouter(tags=["optimize"])


@router.post(
    "/optimize-energy",
    response_model=OptimizeEnergyResponse,
    status_code=status.HTTP_200_OK,
)
def optimize_energy(
    request: OptimizeEnergyRequest,
    interpretation_service: InterpretationService = Depends(get_interpretation_service),
    optimization_service: OptimizationService = Depends(get_optimization_service),
) -> OptimizeEnergyResponse:
    """One scenario in -> one interpretation + optimization plan out."""
    directives = interpretation_service.interpret(request)
    plan, totals, summary = optimization_service.optimize(request, directives)

    return OptimizeEnergyResponse(
        scenario_id=request.scenario_id,
        directive_interpretation=directives,
        hourly_plan=plan,
        total_grid_kwh=totals.total_grid_kwh,
        total_cost_bdt=totals.total_cost_bdt,
        peak_grid_kwh=totals.peak_grid_kwh,
        plan_summary=summary,
    )
