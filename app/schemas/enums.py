"""Enums and literals shared across the GridWise contract.

Canonical source: Problem Statement Sections 04, 07 and 10.
"""

from enum import StrEnum

#: Planning horizon: 24 hourly intervals, hours 0 through 23.
HORIZON_HOURS = 24

#: Absolute tolerance used by the judge for kWh / BDT comparisons.
NUMERIC_TOLERANCE = 0.01


class DirectiveType(StrEnum):
    """Supported operator-note directive types (Section 4.1)."""

    SOLAR_REDUCTION = "solar_reduction"
    MINIMUM_BATTERY_RESERVE = "minimum_battery_reserve"
    NO_CHARGE_WINDOW = "no_charge_window"
    NO_DISCHARGE_WINDOW = "no_discharge_window"
    MAX_GRID_WINDOW = "max_grid_window"
    NO_OP = "no_op"


class BatteryAction(StrEnum):
    """Allowed battery_action values in hourly_plan entries (Section 10.3)."""

    CHARGE = "charge"
    DISCHARGE = "discharge"
    IDLE = "idle"
