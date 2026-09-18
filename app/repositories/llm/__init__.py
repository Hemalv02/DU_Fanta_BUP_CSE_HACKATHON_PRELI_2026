"""LLM repository factory.

The interpretation path is pluggable: hosted OpenAI-compatible model by
default (official `openai` SDK), deterministic rule-based fallback when no
key is configured.
"""

from app.core.config import Settings
from app.core.logging import get_logger
from app.repositories.llm.base import LLMRepository
from app.repositories.llm.openai_compatible import OpenAICompatibleLLMRepository
from app.repositories.llm.rule_based import RuleBasedLLMRepository

logger = get_logger(__name__)

__all__ = ["LLMRepository", "build_llm_repository"]


def build_llm_repository(settings: Settings) -> LLMRepository:
    """Choose the interpretation repository from configuration."""
    provider = settings.effective_llm_provider
    logger.info(
        "llm_provider_selected",
        extra={"provider": provider, "model": settings.llm_model},
    )
    if provider == "openai_compatible":
        return OpenAICompatibleLLMRepository(settings)
    return RuleBasedLLMRepository()
