"""Repository protocol for the operator-note interpretation model.

The LLM repository is the *only* component that talks to a language model.
Its output is intentionally untyped (`list[dict[str, object]]`) because LLM
output is untrusted until the deterministic guardrails validate it
(Problem Statement Section 08).
"""

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class LLMRepository(Protocol):
    """Interpret operator notes into raw directive candidates.

    Implementations must return one candidate dict per note whenever possible,
    each with keys: note_index, applies, directive_type, structured_adjustment,
    explanation. The guardrail layer is responsible for validating/normalizing.

    `battery_capacity_kwh` is supplied so reserves expressed as a percentage
    of capacity (e.g. "keep at least 50% of capacity") can be resolved from
    scenario data instead of being invented.
    """

    def interpret_notes(
        self, notes: list[str], battery_capacity_kwh: float | None = None
    ) -> list[dict[str, Any]]:
        ...
