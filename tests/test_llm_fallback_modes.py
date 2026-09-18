"""Response-format fallback behavior of the hosted LLM repository.

Many OpenAI-compatible providers reject the strict `json_schema`
response_format with an immediate 400. The repository must then retry once
in plain `json_object` mode (fast failure, no latency stacking) and only
give up on the hosted model if that mode is rejected too — in which case the
service layer degrades to the deterministic interpreter.
"""

import json
from types import SimpleNamespace
from typing import Any

import httpx
import openai
import pytest
from pytest import MonkeyPatch

from app.core.config import Settings
from app.core.errors import InterpretationError
from app.repositories.llm.openai_compatible import (
    OpenAICompatibleLLMRepository,
    extract_candidates,
)

_NOTE = "Do not charge the battery between 2 PM and 4 PM."

_VALID_INTERPRETATIONS = {
    "interpretations": [
        {
            "note_index": 0,
            "applies": True,
            "directive_type": "no_charge_window",
            "structured_adjustment": {"hours": [14, 15]},
            "explanation": "charging blocked",
        }
    ]
}


def _bad_request() -> openai.BadRequestError:
    request = httpx.Request("POST", "http://test")
    return openai.BadRequestError(
        "response_format not supported", response=httpx.Response(400, request=request),
        body=None,
    )


def _completion(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
    )


class _FakeCompletions:
    def __init__(self, script: list[Any]) -> None:
        self._script = script
        self.calls: list[dict[str, Any]] = []

    def create(self, **params: Any) -> Any:
        self.calls.append(params)
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class _FakeChat:
    def __init__(self, completions: _FakeCompletions) -> None:
        self.completions = completions


class _FakeOpenAI:
    def __init__(self, script: list[Any], **_kwargs: Any) -> None:
        self.chat = _FakeChat(_FakeCompletions(script))


def test_json_schema_rejection_retries_json_object(monkeypatch: MonkeyPatch) -> None:
    holder: dict[str, _FakeOpenAI] = {}

    def factory(**kwargs: Any) -> _FakeOpenAI:
        client = _FakeOpenAI(
            [_bad_request(), _completion(json.dumps(_VALID_INTERPRETATIONS))]
        )
        holder["client"] = client
        return client

    monkeypatch.setattr(
        "app.repositories.llm.openai_compatible.openai.OpenAI", factory
    )
    repo = OpenAICompatibleLLMRepository(
        Settings(llm_api_key="sk-test-only", llm_reasoning_effort="")
    )
    candidates = repo.interpret_notes([_NOTE], 500.0)

    client = holder["client"]
    formats = [c["response_format"] for c in client.chat.completions.calls]
    assert len(formats) == 2, "json_object retry must happen exactly once"
    assert formats[0]["type"] == "json_schema"
    assert formats[1]["type"] == "json_object"
    assert candidates[0]["directive_type"] == "no_charge_window"
    assert candidates[0]["structured_adjustment"]["hours"] == [14, 15]


def test_both_modes_rejected_raises_controlled_error(monkeypatch: MonkeyPatch) -> None:
    def factory(**kwargs: Any) -> _FakeOpenAI:
        return _FakeOpenAI([_bad_request(), _bad_request()])

    monkeypatch.setattr(
        "app.repositories.llm.openai_compatible.openai.OpenAI", factory
    )
    repo = OpenAICompatibleLLMRepository(
        Settings(llm_api_key="sk-test-only", llm_reasoning_effort="")
    )
    with pytest.raises(InterpretationError):
        repo.interpret_notes([_NOTE], 500.0)


def test_json_schema_success_never_calls_json_object(monkeypatch: MonkeyPatch) -> None:
    holder: dict[str, _FakeOpenAI] = {}

    def factory(**kwargs: Any) -> _FakeOpenAI:
        client = _FakeOpenAI([_completion(json.dumps(_VALID_INTERPRETATIONS))])
        holder["client"] = client
        return client

    monkeypatch.setattr(
        "app.repositories.llm.openai_compatible.openai.OpenAI", factory
    )
    repo = OpenAICompatibleLLMRepository(
        Settings(llm_api_key="sk-test-only", llm_reasoning_effort="")
    )
    candidates = repo.interpret_notes([_NOTE], 500.0)

    client = holder["client"]
    assert len(client.chat.completions.calls) == 1
    assert client.chat.completions.calls[0]["response_format"]["type"] == "json_schema"
    assert candidates[0]["directive_type"] == "no_charge_window"


def test_markdown_fenced_json_is_parsed() -> None:
    fenced = f"```json\n{json.dumps(_VALID_INTERPRETATIONS)}\n```"
    candidates = extract_candidates(fenced)
    assert len(candidates) == 1
    assert candidates[0]["directive_type"] == "no_charge_window"


def test_prose_without_json_object_yields_nothing() -> None:
    assert extract_candidates("no braces here at all") == []
