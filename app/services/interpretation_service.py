"""Interpretation service: operator notes -> guardrailed directives.

Pipeline (Problem Statement Sections 02-03, 08):
    operator_notes
        -> LLM repository (language-capable model, mandatory path)
        -> deterministic guardrails (validate + normalize + per-note fallback)
        -> list[DirectiveInterpretation] consumed by the optimizer
"""

from collections.abc import Callable
from typing import Any

from app.core.errors import InterpretationError
from app.core.logging import get_logger
from app.guardrails import validate_interpretations
from app.repositories.llm import LLMRepository
from app.schemas.directives import DirectiveInterpretation
from app.schemas.request import OptimizeEnergyRequest

logger = get_logger(__name__)


class InterpretationService:
    """Orchestrates the LLM path and its deterministic safety net."""

    def __init__(self, llm_repository: LLMRepository, fallback_repository: LLMRepository) -> None:
        self._llm = llm_repository
        self._fallback = fallback_repository

    def interpret(self, request: OptimizeEnergyRequest) -> list[DirectiveInterpretation]:
        """Exactly one validated interpretation per note, in note_index order."""
        notes = request.operator_notes
        capacity = request.battery.capacity_kwh

        try:
            raw = self._llm.interpret_notes(notes, capacity)
        except InterpretationError:
            # Hosted model unavailable after retries: degrade to the
            # deterministic interpreter for the whole set (controlled).
            logger.warning("llm_provider_failed_fallback_used")
            raw = self._fallback.interpret_notes(notes, capacity)

        return validate_interpretations(
            raw_candidates=raw,
            note_count=len(notes),
            battery=request.battery,
            fallback=self._fallback_for_note(notes, capacity),
        )

    def _fallback_for_note(
        self, notes: list[str], capacity: float
    ) -> Callable[[int], dict[str, Any] | None]:
        def fallback(note_index: int) -> dict[str, Any] | None:
            candidates = self._fallback.interpret_notes([notes[note_index]], capacity)
            return candidates[0] if candidates else None

        return fallback
