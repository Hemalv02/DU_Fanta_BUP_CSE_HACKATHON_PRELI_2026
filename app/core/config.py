"""Application configuration via environment variables (12-factor).

Secrets (LLM_API_KEY) are loaded as SecretStr and are never logged or echoed
back in API responses.
"""

import logging
import math
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Module-local logger avoids a circular import with app.core.logging.
logger = logging.getLogger(__name__)

#: Hard wall-clock budget for the whole /optimize-energy request (judge limit).
_MAX_REQUEST_BUDGET_SECONDS = 30.0
#: Room left for the deterministic fallback, solver, and serialization.
_LLM_BUDGET_MARGIN_SECONDS = 4.0


class Settings(BaseSettings):
    """Runtime settings; every value can be overridden by environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Service -----------------------------------------------------------
    app_name: str = "gridwise-llm"
    environment: Literal["local", "production"] = "local"
    log_level: str = "INFO"

    # --- LLM interpretation path ------------------------------------------
    #: auto = openai_compatible when LLM_API_KEY is set, otherwise rule_based.
    llm_provider: Literal["auto", "openai_compatible", "rule_based"] = "auto"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-5.6-sol"
    llm_timeout_seconds: float = Field(default=12.0, gt=0.0, le=30.0)
    llm_max_retries: int = Field(default=1, ge=0, le=3)
    llm_max_output_tokens: int = Field(default=1024, ge=256, le=16384)
    #: Optional reasoning-effort hint for reasoning models ("low"/"medium"/
    #: "high"). Empty string omits the parameter (safest across providers).
    llm_reasoning_effort: Literal["", "minimal", "low", "medium", "high"] = "low"

    #: Hard wall-clock budget note — see _clamp_llm_latency_budget below.
    @model_validator(mode="after")
    def _clamp_llm_latency_budget(self) -> "Settings":
        """Keep timeout x (retries + 1) inside the judge's 30 s request limit.

        Misconfiguration is auto-clamped (with a warning) rather than refusing
        to start, so a bad env var can never take the service down.
        """
        budget = _MAX_REQUEST_BUDGET_SECONDS - _LLM_BUDGET_MARGIN_SECONDS
        attempts = self.llm_max_retries + 1
        worst_case = self.llm_timeout_seconds * attempts
        if worst_case > budget:
            # Floor (not round) so attempts x timeout always fits the budget.
            clamped = math.floor(budget / attempts * 10) / 10
            logger.warning(
                "llm_timeout_clamped_to_respect_request_budget",
                extra={"requested": self.llm_timeout_seconds, "clamped": clamped},
            )
            self.llm_timeout_seconds = clamped
        return self

    # --- Optimizer ----------------------------------------------------------
    #: Decimal places used when rounding the LP solution into the response.
    schedule_rounding_decimals: int = Field(default=2, ge=0, le=6)

    # --- Request logging -----------------------------------------------------
    #: JSONL file receiving every /optimize-energy request+response pair.
    #: Empty (default) = disabled; enable by setting REQUEST_LOG_FILE in
    #: deployment. Writes happen on a background thread only.
    request_log_file: str = ""

    @property
    def effective_llm_provider(self) -> Literal["openai_compatible", "rule_based"]:
        if self.llm_provider == "openai_compatible":
            return "openai_compatible"
        if self.llm_provider == "rule_based":
            return "rule_based"
        return "openai_compatible" if self.llm_api_key.get_secret_value() else "rule_based"


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor used across the service layer."""
    return Settings()
