"""Deterministic rule-based fallback interpreter.

This is NOT the primary interpretation path — the language model is (per the
Problem Statement's LLM REQUIREMENT). This repository exists so the service
degrades safely when no provider key is configured (local development, tests)
or when the hosted model fails; it never invents unsupported directives and
its output goes through the same deterministic guardrails as LLM output.
"""

import re
from typing import Any

from app.core.logging import get_logger

logger = get_logger(__name__)

_FRACTION_WORDS: dict[str, float] = {
    "half": 0.5,
    "one-half": 0.5,
    "quarter": 0.25,
    "one-quarter": 0.25,
    "fourth": 0.25,
    "one-fourth": 0.25,
    "third": 1 / 3,
    "one-third": 1 / 3,
    "two-thirds": 2 / 3,
    "three-quarters": 0.75,
    "three-fourths": 0.75,
    "fifth": 0.2,
    "one-fifth": 0.2,
    "two-fifths": 0.4,
    "three-fifths": 0.6,
    "four-fifths": 0.8,
    "tenth": 0.1,
    "one-tenth": 0.1,
}

_TIME_12H = re.compile(r"\b(\d{1,2})\s*(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE)
_TIME_24H = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_RANGE_DASH = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\s*(a\.?m\.?|p\.?m\.?)\b", re.IGNORECASE)

# Word-number windows: "from one until three", "between eight and ten".
_HOUR_WORDS = (
    "one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve"
)
_WORD_WINDOW = re.compile(
    rf"\b({_HOUR_WORDS})\s+(?:o'?clock\s+)?(?:until|to|through)\s+(?:o'?clock\s+)?({_HOUR_WORDS})\b"
    rf"|\b(?:from|between)\s+({_HOUR_WORDS})\s+(?:o'?clock\s+)?(?:until|to|through|and)\s+"
    rf"(?:o'?clock\s+)?({_HOUR_WORDS})\b",
    re.IGNORECASE,
)
_HOUR_WORD_MAP = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_KWH = re.compile(r"(\d+(?:\.\d+)?)\s*kwh", re.IGNORECASE)

_SOLAR_KW = re.compile(r"solar|pv\b|panel|photovoltaic", re.IGNORECASE)
_SOLAR_EVENT = re.compile(
    r"drop|reduc|wash|clean|maintenance|fog|cloud|overcast|shade|dust|curtail|degrad|snow"
    r"|inspection|inverter|less|lower|fewer|unreliab|unstable|interrupt",
    re.IGNORECASE,
)

_NEGATION = (
    r"(?:no|do\s*not|don['’]t|must\s*not|mustn['’]t|may\s*not|shall\s*not|avoid|suspend"
    r"|stop(?:ped)?|halt|cannot|can['’]t|prohibit(?:ed)?|disallow(?:ed)?|block(?:ed)?|without)"
)
_OUTAGE = (
    r"(?:unavailable|disabled|isolated|offline|out\s+of\s+service|suspended|interrupted"
    r"|blocked|prohibited|bypassed|locked\s*out)"
)
# "do not charge ..." / "no charging ..." — but never "discharge" (lookbehind).
_NO_CHARGE = re.compile(
    rf"{_NEGATION}[^.]{{0,60}}(?<!dis)charg|(?<!dis)charg\w*[^.]{{0,60}}{_OUTAGE}",
    re.IGNORECASE,
)
_NO_DISCHARGE = re.compile(
    rf"{_NEGATION}[^.]{{0,60}}discharg|discharg\w*[^.]{{0,60}}{_OUTAGE}",
    re.IGNORECASE,
)
_RESERVE = re.compile(
    r"(?:keep|maintain|hold|retain|ensure|require[sd]?|reserve|minimum|at\s*least"
    r"|must\s+(?:stay|remain|be)|\bfull\b)"
    r"[^.]{0,80}?(\d+(?:\.\d+)?)\s*kwh"
    r"|reserve[^.]{0,40}?(\d+(?:\.\d+)?)\s*kwh",
    re.IGNORECASE,
)
_MAX_GRID = re.compile(
    r"grid[^.]{0,60}?(?:not\s*exceed|no\s*more\s*than|no\s*greater\s*than|at\s*most"
    r"|at\s*or\s*below|below|cap(?:ped)?\s*(?:at|to)?|limit(?:ed)?\s*(?:to|at)?"
    r"|maximum\s*(?:of)?|up\s*to|more\s*than)[^.]{0,30}?(\d+(?:\.\d+)?)\s*kwh"
    r"|(?:not\s*exceed|no\s*more\s*than|no\s*greater\s*than|at\s*most|at\s*or\s*below"
    r"|more\s*than|cap(?:ped)?\s*at|limit(?:ed)?\s*(?:to|at)?|maximum)"
    r"[^.]{0,30}?(\d+(?:\.\d+)?)\s*kwh[^.]{0,30}?grid",
    re.IGNORECASE,
)
_RESERVE_PREFIX = re.compile(
    r"\b(?:keep|maintain|hold|retain|ensure|require[sd]?|reserve|minimum|at\s*least"
    r"|must\s+(?:stay|remain|be)|full)\b",
    re.IGNORECASE,
)


def _find_times(text: str) -> list[int]:
    """Extract clock hours (0-23) mentioned in the text, in order of appearance."""
    spans: list[tuple[int, int]] = []  # (position_in_text, hour)

    # Word-number windows: "from one until three". Without a meridiem, small
    # hours (1-7) on both endpoints are read as PM (daytime operations).
    for match in _WORD_WINDOW.finditer(text):
        pair = [g for g in match.groups() if g is not None]
        hours = [_HOUR_WORD_MAP[pair[0].lower()], _HOUR_WORD_MAP[pair[1].lower()]]
        if all(1 <= h <= 7 for h in hours):
            hours = [h + 12 for h in hours]
        spans.append((match.start(), hours[0]))
        spans.append((match.start(), hours[1]))

    consumed = [(m.start(), m.end()) for m in _RANGE_DASH.finditer(text)]
    for match in _RANGE_DASH.finditer(text):
        start_h, end_h, meridiem = int(match.group(1)), int(match.group(2)), match.group(3)
        spans.append((match.start(), _to_24h(start_h, meridiem)))
        spans.append((match.start(), _to_24h(end_h, meridiem)))

    for match in _TIME_12H.finditer(text):
        if any(s <= match.start() < e for s, e in consumed):
            continue
        hour, meridiem = int(match.group(1)), match.group(3)
        spans.append((match.start(), _to_24h(hour, meridiem)))

    for match in _TIME_24H.finditer(text):
        spans.append((match.start(), int(match.group(1))))

    lowered = text.lower()
    for word, hour in (("noon", 12), ("midnight", 0)):
        for match in re.finditer(word, lowered):
            spans.append((match.start(), hour))

    spans.sort(key=lambda item: item[0])
    return [hour for _, hour in spans]


def _to_24h(hour: int, meridiem: str) -> int:
    meridiem = meridiem.lower().replace(".", "")
    if meridiem == "am":
        return 0 if hour == 12 else hour % 12
    return 12 if hour == 12 else (hour + 12) % 24


def _window_hours(times: list[int]) -> list[int] | None:
    """Whole-hour window: start inclusive, end exclusive (Section 5.1)."""
    if not times:
        return None
    if len(times) == 1:
        return [times[0] % 24]
    start, end = times[0] % 24, times[1] % 24
    if end == start:
        return [start]
    if end > start:
        return list(range(start, end))
    # Crosses midnight (e.g. 11 PM to 2 AM).
    return [h % 24 for h in range(start, end + 24)]


def _solar_factor(text: str) -> float | None:
    """Usable fraction remaining, from percentages or fraction words."""
    lowered = text.lower()

    pct = _PERCENT.search(text)
    if pct:
        value = float(pct.group(1)) / 100.0
        before = lowered[: pct.start()]
        after = lowered[pct.end() :]
        # "drop to 20%", "at 25% of forecast" -> fraction remaining.
        # "80% reduction", "reduce by 30%" -> complement.
        reduction_context = bool(
            re.search(r"(reduc|drop|loss|cut|degrad|decrease|less)[^.]{0,25}$", before)
            or re.search(r"^\s*(reduction|less|lower|fewer)", after)
        )
        remaining_context = bool(
            re.search(
                r"(to|at|about|around|only|roughly|approximately|treated as)[^.]{0,25}$", before
            )
            or re.search(r"^\s*(of|remains?|remaining|of the|of normal|of forecast)", after)
        )
        if reduction_context and not remaining_context:
            return round(max(0.0, 1.0 - value), 6)
        return round(min(1.0, value), 6)

    for word, fraction in _FRACTION_WORDS.items():
        if re.search(rf"\b{re.escape(word)}\b", lowered):
            # "leave one-fifth of output" -> remaining; "lose half" -> complement.
            if re.search(rf"(lose|loss|reduc|cut|drop)[^.]{{0,30}}{re.escape(word)}", lowered):
                return round(max(0.0, 1.0 - fraction), 6)
            return fraction
    return None


def _interpret_one(note: str, battery_capacity_kwh: float | None) -> dict[str, Any]:
    times = _find_times(note)
    hours = _window_hours(times)

    def candidate(
        directive_type: str,
        adjustment: dict[str, Any] | None,
        explanation: str,
    ) -> dict[str, Any]:
        return {
            "note_index": 0,  # rebound by the caller for multi-note batches
            "applies": directive_type != "no_op",
            "directive_type": directive_type,
            "structured_adjustment": adjustment,
            "explanation": explanation,
        }

    if _NO_CHARGE.search(note) and hours:
        return candidate(
            "no_charge_window", {"hours": hours}, "Battery charging is unavailable in this window."
        )

    if _NO_DISCHARGE.search(note) and hours:
        return candidate(
            "no_discharge_window",
            {"hours": hours},
            "Battery discharging is blocked in this window.",
        )

    if _SOLAR_KW.search(note) and _SOLAR_EVENT.search(note):
        factor = _solar_factor(note)
        if hours and factor is not None:
            return candidate(
                "solar_reduction",
                {"hours": hours, "factor": factor},
                f"Usable solar falls to {factor:.0%} of forecast in this window.",
            )

    reserve_match = _RESERVE.search(note)
    if hours and (reserve_match or _RESERVE_PREFIX.search(note)) and not _MAX_GRID.search(note):
        value: float | None = None
        if reserve_match:
            value = float(next(g for g in reserve_match.groups() if g is not None))
        else:
            # "at least 50% of the battery capacity" — resolve against capacity.
            pct = _PERCENT.search(note)
            if pct and battery_capacity_kwh and re.search(
                r"%[^.]{0,40}(capacity|stored)", note, re.IGNORECASE
            ):
                value = round(float(pct.group(1)) / 100.0 * battery_capacity_kwh, 6)
        if value is not None:
            return candidate(
                "minimum_battery_reserve",
                {"hours": hours, "minimum_energy_kwh": value},
                f"Battery must stay at or above {value:g} kWh in this window.",
            )

    grid_match = _MAX_GRID.search(note)
    if grid_match and hours:
        value = float(next(g for g in grid_match.groups() if g is not None))
        return candidate(
            "max_grid_window",
            {"hours": hours, "max_grid_kwh": value},
            f"Grid import is capped at {value:g} kWh in this window.",
        )

    return candidate("no_op", None, "This note does not affect today's 24-hour energy schedule.")


class RuleBasedLLMRepository:
    """Deterministic fallback interpreter (development / safe-failure path)."""

    def interpret_notes(
        self, notes: list[str], battery_capacity_kwh: float | None = None
    ) -> list[dict[str, Any]]:
        result = [
            {**_interpret_one(note, battery_capacity_kwh), "note_index": index}
            for index, note in enumerate(notes)
        ]
        logger.info("rule_based_interpretation_used", extra={"notes": len(notes)})
        return result
