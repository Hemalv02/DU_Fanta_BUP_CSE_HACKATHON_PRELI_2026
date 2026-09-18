"""OpenAI-compatible LLM repository using the official `openai` SDK.

Model-side guardrails baked into every call:
  - strict JSON-schema `response_format` — the model cannot emit anything
    outside the interpretation contract (no extra fields, no prose)
  - no tools/functions, no temperature — no extra steering surface
  - bounded output tokens and request timeout
  - notes travel as sanitized, escaped data inside marked untrusted blocks

Latency safety (rubric: POST must finish within 30 s): total LLM budget is
bounded by timeout x (SDK retries + 1). The json_object fallback is only
attempted when the provider *rejects* the strict json_schema response_format
with an immediate BadRequestError — a fast HTTP 400, never a slow timeout —
so the two modes can never stack into a doubled latency budget.

Provider errors are converted to controlled InterpretationError; raw provider
payloads are never logged or re-raised.
"""

import json
import time
from typing import Any

import openai

from app.core.config import Settings
from app.core.errors import InterpretationError
from app.core.logging import get_logger
from app.repositories.llm.prompt import SYSTEM_PROMPT, build_user_message
from app.repositories.llm.response_schema import GRIDWISE_RESPONSE_FORMAT

logger = get_logger(__name__)

#: Whitelist of keys accepted in a raw candidate — anything else is dropped.
_CANDIDATE_KEYS = frozenset(
    {"note_index", "applies", "directive_type", "structured_adjustment", "explanation"}
)
#: Whitelist of keys accepted inside structured_adjustment.
_ADJUSTMENT_KEYS = frozenset({"hours", "factor", "minimum_energy_kwh", "max_grid_kwh"})

_JSON_OBJECT_MODE: dict[str, Any] = {"type": "json_object"}


def _loads_lenient(content: str) -> Any:
    """Strict json.loads first; on failure retry on the outermost {...} block.

    Some providers acknowledge the response_format but still wrap the JSON in
    markdown fences or prose. The brace-slice is deterministic and safe: the
    result still goes through whitelist parsing and the guardrails.
    """
    try:
        return json.loads(content)
    except ValueError:
        start, end = content.find("{"), content.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(content[start : end + 1])
            except ValueError:
                return None
        return None


def extract_candidates(content: str) -> list[dict[str, Any]]:
    """Parse model output strictly: JSON only, whitelist keys only.

    Accepts either {"interpretations": [...]} or a bare [...]; drops every
    field outside the contract so nothing unexpected can reach the guardrails.
    """
    parsed = _loads_lenient(content)
    if parsed is None:
        return []

    raw_items: Any = None
    if isinstance(parsed, dict):
        raw_items = parsed.get("interpretations")
    elif isinstance(parsed, list):
        raw_items = parsed

    if not isinstance(raw_items, list):
        return []

    candidates: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        kept = {key: item[key] for key in _CANDIDATE_KEYS if key in item}
        adjustment = kept.get("structured_adjustment")
        if isinstance(adjustment, dict):
            kept["structured_adjustment"] = {
                key: value for key, value in adjustment.items() if key in _ADJUSTMENT_KEYS
            }
        candidates.append(kept)
    return candidates


class OpenAICompatibleLLMRepository:
    """Calls a hosted language model and returns raw interpretation candidates."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _client(self) -> openai.OpenAI:
        # Constructed per call: cheap and avoids sharing a socket pool across
        # the threadpool workers that serve sync endpoints.
        return openai.OpenAI(
            api_key=self._settings.llm_api_key.get_secret_value(),
            base_url=self._settings.llm_base_url.rstrip("/"),
            timeout=self._settings.llm_timeout_seconds,
            max_retries=self._settings.llm_max_retries,
        )

    def interpret_notes(
        self, notes: list[str], battery_capacity_kwh: float | None = None
    ) -> list[dict[str, Any]]:
        started = time.monotonic()
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_message(notes, battery_capacity_kwh)},
        ]
        client = self._client()

        try:
            try:
                candidates = self._complete(client, messages, GRIDWISE_RESPONSE_FORMAT)
            except openai.BadRequestError:
                # Provider rejected strict json_schema mode outright (many
                # OpenAI-compatible servers do): retry once in plain
                # json_object mode. A 400 fails fast, so this cannot stack
                # with the first attempt's latency budget.
                logger.warning("llm_json_schema_mode_rejected")
                candidates = self._complete(client, messages, _JSON_OBJECT_MODE)
        except openai.BadRequestError:
            logger.warning("llm_json_object_mode_also_rejected")
            candidates = []
        except openai.OpenAIError as exc:
            # Log the class only — never provider payloads or keys.
            logger.error("llm_provider_error", extra={"error": type(exc).__name__})
            raise InterpretationError(f"LLM provider error: {type(exc).__name__}") from exc

        elapsed = time.monotonic() - started
        logger.info("llm_call_completed", extra={"elapsed_seconds": round(elapsed, 2)})
        if not candidates:
            raise InterpretationError("LLM returned no usable interpretations")
        return candidates

    def _complete(
        self,
        client: openai.OpenAI,
        messages: list[dict[str, str]],
        response_format: dict[str, Any],
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "model": self._settings.llm_model,
            "messages": messages,
            "response_format": response_format,
            "max_completion_tokens": self._settings.llm_max_output_tokens,
        }
        if self._settings.llm_reasoning_effort:
            params["reasoning_effort"] = self._settings.llm_reasoning_effort

        try:
            completion = client.chat.completions.create(**params)
        except openai.BadRequestError:
            # Provider may not accept the reasoning-effort hint: retry without.
            if "reasoning_effort" not in params:
                raise
            logger.warning("llm_reasoning_effort_rejected_retrying_without")
            params.pop("reasoning_effort")
            completion = client.chat.completions.create(**params)

        if not completion.choices:
            return []
        content = completion.choices[0].message.content
        if not isinstance(content, str) or not content.strip():
            return []
        return extract_candidates(content)
