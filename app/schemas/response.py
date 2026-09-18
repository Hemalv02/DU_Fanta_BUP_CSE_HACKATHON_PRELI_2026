"""Response schemas for GET /health and POST /optimize-energy (Section 10)."""

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from app.schemas.directives import DirectiveInterpretation
from app.schemas.enums import HORIZON_HOURS
from app.schemas.request import NonNegativeFloat


class HealthResponse(BaseModel):
    """GET /health readiness payload (Section 6.2)."""

    status: str = "ok"


class HourPlanEntry(BaseModel):
    """One hour of the final 24-hour schedule (Section 10.3)."""

    hour: Annotated[int, Field(ge=0, le=23)]
    grid_kwh: NonNegativeFloat
    solar_used_kwh: NonNegativeFloat
    battery_action: Annotated[str, Field(pattern="^(charge|discharge|idle)$")]
    battery_kwh: NonNegativeFloat
    battery_energy_after_kwh: NonNegativeFloat


class OptimizeEnergyResponse(BaseModel):
    """Successful POST /optimize-energy payload (Section 10.1)."""

    scenario_id: str
    directive_interpretation: Annotated[list[DirectiveInterpretation], Field(min_length=1)]
    hourly_plan: Annotated[
        list[HourPlanEntry], Field(min_length=HORIZON_HOURS, max_length=HORIZON_HOURS)
    ]
    total_grid_kwh: NonNegativeFloat
    total_cost_bdt: NonNegativeFloat
    peak_grid_kwh: NonNegativeFloat
    plan_summary: Annotated[str, Field(min_length=1, max_length=2048)]

    @model_validator(mode="after")
    def _plan_hours_ordered(self) -> "OptimizeEnergyResponse":
        if [entry.hour for entry in self.hourly_plan] != list(range(HORIZON_HOURS)):
            raise ValueError("hourly_plan must contain hours 0..23 in order")
        return self


class ErrorResponse(BaseModel):
    """Controlled error payload — never contains secrets or stack traces."""

    status: str = "error"
    error: str
