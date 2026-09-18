"""Pydantic schemas implementing the exact GridWise API contract."""

from app.schemas.directives import (
    ADJUSTMENT_FIELDS_FOR_TYPE,
    DirectiveInterpretation,
    MaxGridWindowAdjustment,
    MinimumBatteryReserveAdjustment,
    NoChargeWindowAdjustment,
    NoDischargeWindowAdjustment,
    SolarReductionAdjustment,
    StructuredAdjustment,
)
from app.schemas.enums import HORIZON_HOURS, NUMERIC_TOLERANCE, BatteryAction, DirectiveType
from app.schemas.request import BatterySpec, HourEntry, OptimizeEnergyRequest
from app.schemas.response import (
    ErrorResponse,
    HealthResponse,
    HourPlanEntry,
    OptimizeEnergyResponse,
)

__all__ = [
    "ADJUSTMENT_FIELDS_FOR_TYPE",
    "BatteryAction",
    "BatterySpec",
    "DirectiveInterpretation",
    "DirectiveType",
    "ErrorResponse",
    "HORIZON_HOURS",
    "HealthResponse",
    "HourEntry",
    "HourPlanEntry",
    "MaxGridWindowAdjustment",
    "MinimumBatteryReserveAdjustment",
    "NUMERIC_TOLERANCE",
    "NoChargeWindowAdjustment",
    "NoDischargeWindowAdjustment",
    "OptimizeEnergyRequest",
    "OptimizeEnergyResponse",
    "SolarReductionAdjustment",
    "StructuredAdjustment",
]
