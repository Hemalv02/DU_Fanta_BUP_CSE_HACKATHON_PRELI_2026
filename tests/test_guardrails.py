"""Deterministic guardrails over untrusted interpretation output (Section 08)."""

import pytest

from app.core.errors import InterpretationError
from app.guardrails.directive_guardrails import validate_interpretations
from app.schemas.request import BatterySpec

BATTERY = BatterySpec(
    capacity_kwh=200.0,
    initial_energy_kwh=100.0,
    minimum_energy_kwh=40.0,
    max_charge_kwh_per_hour=50.0,
    max_discharge_kwh_per_hour=50.0,
)

NO_OP_FALLBACK = lambda index: {  # noqa: E731 - test stub
    "note_index": index,
    "applies": False,
    "directive_type": "no_op",
    "structured_adjustment": None,
    "explanation": "fallback",
}


def validate(raw: list[dict], note_count: int = 2) -> list:
    return validate_interpretations(raw, note_count, BATTERY, NO_OP_FALLBACK)


def test_hours_are_normalized_sorted_and_deduped() -> None:
    result = validate(
        [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [16, 14, 15, 14]},
                "explanation": "x",
            },
            {
                "note_index": 1,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "y",
            },
        ]
    )
    assert result[0].structured_adjustment is not None
    assert result[0].structured_adjustment.hours == [14, 15, 16]


def test_percent_like_factor_is_repaired() -> None:
    result = validate(
        [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "solar_reduction",
                "structured_adjustment": {"hours": [13], "factor": 20},
                "explanation": "x",
            }
        ],
        note_count=1,
    )
    assert result[0].structured_adjustment is not None
    assert result[0].structured_adjustment.factor == pytest.approx(0.2)


def test_unsupported_directive_type_falls_back_per_note() -> None:
    result = validate(
        [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "rename_campus",  # invented type
                "structured_adjustment": {"hours": [1]},
                "explanation": "x",
            }
        ],
        note_count=1,
    )
    assert result[0].directive_type == "no_op"
    assert result[0].applies is False


def test_out_of_range_hours_fall_back() -> None:
    result = validate(
        [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "no_discharge_window",
                "structured_adjustment": {"hours": [7, 24]},
                "explanation": "x",
            }
        ],
        note_count=1,
    )
    assert result[0].directive_type == "no_op"


def test_reserve_above_capacity_falls_back() -> None:
    result = validate(
        [
            {
                "note_index": 0,
                "applies": True,
                "directive_type": "minimum_battery_reserve",
                "structured_adjustment": {"hours": [5], "minimum_energy_kwh": 999.0},
                "explanation": "x",
            }
        ],
        note_count=1,
    )
    assert result[0].directive_type == "no_op"


def test_applies_semantics_are_forced() -> None:
    result = validate(
        [
            {
                "note_index": 0,
                "applies": True,  # contradictory: no_op must be applies=false
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "x",
            },
            {
                "note_index": 1,
                "applies": False,  # contradictory: real directive must be applies=true
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [3]},
                "explanation": "y",
            },
        ]
    )
    assert result[0].applies is False
    assert result[1].applies is True


def test_missing_note_mapping_is_filled_in_order() -> None:
    result = validate(
        [
            {
                "note_index": 1,
                "applies": True,
                "directive_type": "no_charge_window",
                "structured_adjustment": {"hours": [3]},
                "explanation": "y",
            }
            # note 0 missing entirely -> fallback fills it
        ]
    )
    assert [entry.note_index for entry in result] == [0, 1]
    assert result[0].directive_type == "no_op"
    assert result[1].directive_type == "no_charge_window"


def test_duplicate_note_mapping_kept_once() -> None:
    raw = [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [3]},
            "explanation": "first",
        },
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [4]},
            "explanation": "second",
        },
    ]
    result = validate(raw, note_count=1)
    assert len(result) == 1
    assert result[0].structured_adjustment is not None
    assert result[0].structured_adjustment.hours == [3]  # first wins


def test_controlled_error_when_nothing_valid_and_no_fallback() -> None:
    with pytest.raises(InterpretationError):
        validate_interpretations(
            [
                {
                    "note_index": 0,
                    "applies": True,
                    "directive_type": "unsupported_directive",
                    "structured_adjustment": {"hours": [1]},
                    "explanation": "x",
                }
            ],
            note_count=1,
            battery=BATTERY,
            fallback=lambda index: None,
        )
