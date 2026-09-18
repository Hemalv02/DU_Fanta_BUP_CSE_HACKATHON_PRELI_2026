"""Request schemas for POST /optimize-energy (Problem Statement Section 07).

Every numeric field is constrained to be finite and non-negative so that
structurally invalid payloads are rejected with a controlled 400 before any
LLM call or solver work happens.
"""

from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.enums import HORIZON_HOURS

#: A finite, non-negative float used for energy quantities and tariffs.
NonNegativeFloat = Annotated[float, Field(ge=0.0, allow_inf_nan=False)]


class HourEntry(BaseModel):
    """One hourly interval of demand, solar availability and grid tariff."""

    hour: Annotated[int, Field(ge=0, le=23)]
    demand_kwh: NonNegativeFloat
    solar_kwh: NonNegativeFloat
    tariff_bdt_per_kwh: NonNegativeFloat


class BatterySpec(BaseModel):
    """Battery energy storage parameters (Section 7.3).

    Rates and capacity are maxima/levels, not required to be strictly
    positive: a judge scenario may legitimately lock the battery with
    zero charge/discharge rates. Cross-field consistency is enforced below.
    """

    capacity_kwh: NonNegativeFloat
    initial_energy_kwh: NonNegativeFloat
    minimum_energy_kwh: NonNegativeFloat
    max_charge_kwh_per_hour: NonNegativeFloat
    max_discharge_kwh_per_hour: NonNegativeFloat

    @model_validator(mode="after")
    def _check_energy_bounds(self) -> "BatterySpec":
        if not (self.minimum_energy_kwh <= self.initial_energy_kwh <= self.capacity_kwh):
            raise ValueError(
                "battery.initial_energy_kwh must lie between minimum_energy_kwh and capacity_kwh"
            )
        if self.minimum_energy_kwh > self.capacity_kwh:
            raise ValueError("battery.minimum_energy_kwh must not exceed capacity_kwh")
        return self


class OptimizeEnergyRequest(BaseModel):
    """The single JSON object accepted by POST /optimize-energy."""

    #: Length cap is an abuse guard only — the spec puts no bound on
    #: scenario_id, so keep it generous to never 400 a valid judge payload.
    scenario_id: Annotated[str, Field(min_length=1, max_length=1024)]
    operator_notes: Annotated[list[str], Field(min_length=1, max_length=3)]
    hours: Annotated[list[HourEntry], Field(min_length=HORIZON_HOURS, max_length=HORIZON_HOURS)]
    battery: BatterySpec

    @field_validator("operator_notes")
    @classmethod
    def _notes_non_empty(cls, value: list[str]) -> list[str]:
        for note in value:
            if not note.strip():
                raise ValueError("operator_notes entries must be non-empty strings")
        return value

    @field_validator("hours")
    @classmethod
    def _hours_complete_and_unique(cls, value: list[HourEntry]) -> list[HourEntry]:
        seen = [entry.hour for entry in value]
        if len(set(seen)) != len(seen):
            raise ValueError("hours must contain unique hour integers")
        if sorted(seen) != list(range(HORIZON_HOURS)):
            raise ValueError("hours must contain exactly the integers 0 through 23")
        return sorted(value, key=lambda entry: entry.hour)
