"""Deterministic guardrails for untrusted LLM interpretation output.

Problem Statement Section 08: LLM output is untrusted structured data until
these checks pass. The guardrails normalize what can be normalized
(note order, hour uniqueness/ascending order, applies semantics) and reject
what cannot (unsupported directive types, out-of-range hours or values),
falling back per-note to the deterministic interpreter before ever failing
in a controlled way.
"""

from collections.abc import Callable
from typing import Any

from pydantic import ValidationError

from app.core.errors import InterpretationError
from app.core.logging import get_logger
from app.schemas.directives import ADJUSTMENT_FIELDS_FOR_TYPE, DirectiveInterpretation
from app.schemas.enums import HORIZON_HOURS
from app.schemas.request import BatterySpec

logger = get_logger(__name__)

FallbackInterpreter = Callable[[int], dict[str, Any] | None]

_VALID_TYPES = set(ADJUSTMENT_FIELDS_FOR_TYPE) | {"no_op"}


def _normalize_hours(raw: Any) -> list[int] | None:
    """Coerce to unique ascending integers within 0..23; None when invalid."""
    if not isinstance(raw, list) or not raw:
        return None
    hours: list[int] = []
    for item in raw:
        if isinstance(item, bool):
            return None
        if isinstance(item, float) and item.is_integer():
            item = int(item)
        if not isinstance(item, int):
            return None
        if not 0 <= item <= HORIZON_HOURS - 1:
            return None
        hours.append(item)
    return sorted(set(hours))


def _normalize_factor(raw: Any) -> float | None:
    """Usable fraction in [0, 1]; repairs percent-like values (e.g. 20 -> 0.2)."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if 0.0 <= value <= 1.0:
        return value
    if 1.0 < value <= 100.0:  # model emitted "20" meaning 20%
        return round(value / 100.0, 6)
    return None


def _normalize_non_negative(raw: Any) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if value != value or value == float("inf") or value < 0:  # NaN/inf/negative
        return None
    return value


def _normalize_candidate(
    raw: dict[str, Any],
    battery: BatterySpec,
    note_index: int | None = None,
) -> dict[str, Any] | None:
    """Normalize one raw candidate into a schema-valid candidate, or None.

    `note_index` (the caller's authoritative loop position) overrides whatever
    index the candidate claims — single-note fallback calls always report 0,
    so without this the emitted entries could carry duplicate indices.
    """
    directive_type = raw.get("directive_type")
    if not isinstance(directive_type, str) or directive_type not in _VALID_TYPES:
        return None

    resolved_index = note_index if note_index is not None else raw.get("note_index")

    if directive_type == "no_op":
        # Section 5.1: no_op forces applies=false and null adjustment.
        return {
            "note_index": resolved_index,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": _clean_explanation(raw.get("explanation")),
        }

    adjustment = raw.get("structured_adjustment")
    if not isinstance(adjustment, dict):
        return None
    hours = _normalize_hours(adjustment.get("hours"))
    if hours is None:
        return None

    normalized: dict[str, Any] | None = None
    if directive_type == "solar_reduction":
        factor = _normalize_factor(adjustment.get("factor"))
        if factor is not None:
            normalized = {"hours": hours, "factor": factor}
    elif directive_type == "minimum_battery_reserve":
        reserve = _normalize_non_negative(adjustment.get("minimum_energy_kwh"))
        if reserve is not None and reserve <= battery.capacity_kwh:
            normalized = {"hours": hours, "minimum_energy_kwh": reserve}
    elif directive_type in ("no_charge_window", "no_discharge_window"):
        normalized = {"hours": hours}
    elif directive_type == "max_grid_window":
        cap = _normalize_non_negative(adjustment.get("max_grid_kwh"))
        if cap is not None:
            normalized = {"hours": hours, "max_grid_kwh": cap}

    if normalized is None:
        return None
    return {
        "note_index": resolved_index,
        "applies": True,
        "directive_type": directive_type,
        "structured_adjustment": normalized,
        "explanation": _clean_explanation(raw.get("explanation")),
    }


def _clean_explanation(raw: Any) -> str:
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:512]
    return "Interpreted from the operator note."


def _materialize(candidate: dict[str, Any]) -> DirectiveInterpretation | None:
    try:
        return DirectiveInterpretation.model_validate(candidate)
    except ValidationError:
        return None


def validate_interpretations(
    raw_candidates: list[dict[str, Any]],
    note_count: int,
    battery: BatterySpec,
    fallback: FallbackInterpreter,
) -> list[DirectiveInterpretation]:
    """Return exactly one validated interpretation per note, in note order.

    Per-note flow: LLM candidate -> normalize -> strict schema -> on failure,
    deterministic fallback candidate -> normalize -> strict schema -> on
    failure, controlled InterpretationError (never an invented directive).
    """
    by_index: dict[int, dict[str, Any]] = {}
    for raw in raw_candidates:
        index = raw.get("note_index")
        if isinstance(index, bool) or not isinstance(index, int):
            continue
        if 0 <= index < note_count and index not in by_index:
            by_index[index] = raw

    validated: list[DirectiveInterpretation] = []
    for index in range(note_count):
        candidate = _normalize_candidate(by_index.get(index, {}), battery)
        interpretation = _materialize(candidate) if candidate else None

        if interpretation is None:
            logger.warning("guardrail_fallback_for_note", extra={"note_index": index})
            raw_fallback = fallback(index)
            fallback_candidate = (
                _normalize_candidate(raw_fallback, battery, note_index=index)
                if raw_fallback
                else None
            )
            interpretation = _materialize(fallback_candidate) if fallback_candidate else None

        if interpretation is None:
            raise InterpretationError(
                f"note {index} could not be interpreted into a supported directive"
            )
        validated.append(interpretation)

    return validated
