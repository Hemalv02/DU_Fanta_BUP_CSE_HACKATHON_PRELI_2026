"""Composition root: builds the service graph once per process.

API layer -> services -> repositories; nothing below the API layer knows
about FastAPI, which keeps the pipeline unit-testable.
"""

from functools import lru_cache

from app.core.config import get_settings
from app.repositories.llm import LLMRepository, build_llm_repository
from app.repositories.llm.rule_based import RuleBasedLLMRepository
from app.repositories.solver import LPSolverRepository
from app.services.interpretation_service import InterpretationService
from app.services.optimization_service import OptimizationService


@lru_cache
def get_llm_repository() -> LLMRepository:
    settings = get_settings()
    return build_llm_repository(settings)


@lru_cache
def get_interpretation_service() -> InterpretationService:
    return InterpretationService(
        llm_repository=get_llm_repository(),
        fallback_repository=RuleBasedLLMRepository(),
    )


@lru_cache
def get_optimization_service() -> OptimizationService:
    return OptimizationService(
        solver_repository=LPSolverRepository(),
        settings=get_settings(),
    )


def reset_container() -> None:
    """Clear cached wiring (used by tests that override configuration)."""
    get_llm_repository.cache_clear()
    get_interpretation_service.cache_clear()
    get_optimization_service.cache_clear()
    get_settings.cache_clear()


__all__ = [
    "get_interpretation_service",
    "get_llm_repository",
    "get_optimization_service",
    "reset_container",
]
