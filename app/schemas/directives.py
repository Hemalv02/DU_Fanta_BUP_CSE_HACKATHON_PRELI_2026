"""Directive interpretation schemas (Problem Statement Sections 04, 05, 08, 10.2).

These models are the machine-checkable output of the LLM interpretation path.
They are constructed only *after* deterministic guardrails have normalized the
raw model output; the schema itself stays strict so an invalid combination
(directive_type vs. structured_adjustment shape, applies semantics, hour
ranges) can never be represented.
"""

from typing import Annotated

from pydantic import BaseModel, Field, model_validator

from app.schemas.enums import HORIZON_HOURS

#: Hours list: unique integers 0..23 in ascending order, at least one hour.
HoursList = Annotated[
    list[Annotated[int, Field(ge=0, le=HORIZON_HOURS - 1)]],
    Field(min_length=1, max_length=HORIZON_HOURS),
]

FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]


def _validate_hours_ascending_unique(hours: list[int]) -> list[int]:
    if len(set(hours)) != len(hours):
        raise ValueError("hours must be unique")
    if hours != sorted(hours):
        raise ValueError("hours must be in ascending order")
    return hours


class SolarReductionAdjustment(BaseModel):
    """Reduce usable solar during specific hours; factor = fraction remaining."""

    hours: HoursList
    factor: Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _check_hours(self) -> "SolarReductionAdjustment":
        _validate_hours_ascending_unique(self.hours)
        return self


class MinimumBatteryReserveAdjustment(BaseModel):
    """Keep battery energy at or above a required level during specific hours."""

    hours: HoursList
    minimum_energy_kwh: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _check_hours(self) -> "MinimumBatteryReserveAdjustment":
        _validate_hours_ascending_unique(self.hours)
        return self


class NoChargeWindowAdjustment(BaseModel):
    """Battery charging is unavailable during specific hours."""

    hours: HoursList

    @model_validator(mode="after")
    def _check_hours(self) -> "NoChargeWindowAdjustment":
        _validate_hours_ascending_unique(self.hours)
        return self


class NoDischargeWindowAdjustment(BaseModel):
    """Battery discharging is unavailable during specific hours."""

    hours: HoursList

    @model_validator(mode="after")
    def _check_hours(self) -> "NoDischargeWindowAdjustment":
        _validate_hours_ascending_unique(self.hours)
        return self


class MaxGridWindowAdjustment(BaseModel):
    """Grid import may not exceed max_grid_kwh during specific hours."""

    hours: HoursList
    max_grid_kwh: Annotated[float, Field(ge=0.0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def _check_hours(self) -> "MaxGridWindowAdjustment":
        _validate_hours_ascending_unique(self.hours)
        return self


StructuredAdjustment = (
    SolarReductionAdjustment
    | MinimumBatteryReserveAdjustment
    | NoChargeWindowAdjustment
    | NoDischargeWindowAdjustment
    | MaxGridWindowAdjustment
)

#: Discriminator for the (directive_type -> required adjustment fields) pairing.
#: no_charge_window and no_discharge_window are structurally identical, so the
#: Union cannot distinguish them; directive_type is the discriminator and the
#: required field set is validated against it.
ADJUSTMENT_FIELDS_FOR_TYPE: dict[str, set[str]] = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}


class DirectiveInterpretation(BaseModel):
    """One machine-checkable interpretation entry for one operator note."""

    note_index: Annotated[int, Field(ge=0)]
    applies: bool
    directive_type: str
    structured_adjustment: StructuredAdjustment | None
    explanation: Annotated[str, Field(min_length=1, max_length=512)]

    @model_validator(mode="after")
    def _check_applies_semantics(self) -> "DirectiveInterpretation":
        if self.directive_type == "no_op":
            if self.applies:
                raise ValueError("no_op requires applies=false")
            if self.structured_adjustment is not None:
                raise ValueError("no_op requires structured_adjustment=null")
            return self

        expected_fields = ADJUSTMENT_FIELDS_FOR_TYPE.get(self.directive_type)
        if expected_fields is None:
            raise ValueError(f"unsupported directive_type: {self.directive_type!r}")
        if not self.applies:
            raise ValueError("non-no_op directives require applies=true")
        if self.structured_adjustment is None:
            raise ValueError(
                f"directive_type {self.directive_type!r} requires structured_adjustment"
            )
        actual_fields = set(type(self.structured_adjustment).model_fields.keys())
        if actual_fields != expected_fields:
            raise ValueError(
                f"directive_type {self.directive_type!r} requires structured_adjustment "
                f"with fields {sorted(expected_fields)}, got {sorted(actual_fields)}"
            )
        return self
