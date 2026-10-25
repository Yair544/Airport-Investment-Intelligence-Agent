"""OpenAI client wrapper: response parsing and the single retry layer (no network)."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest

from app.agent.client import ModelError, OpenAIResponsesClient
from app.config import Settings


def response(*items: Any, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        output=list(items), output_text=text, usage=SimpleNamespace(input_tokens=10, output_tokens=5)
    )


FUNCTION_CALL = SimpleNamespace(
    type="function_call", call_id="call_1", name="resolve_airports", arguments='{"queries":["SFO"]}'
)
REASONING = SimpleNamespace(type="reasoning", id="rs_1", summary=[], encrypted_content="opaque")
MESSAGE = SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="Hello")])


class FakeResponses:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes = outcomes
        self.kwargs: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.kwargs.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def client_with(settings: Settings, outcomes: list[Any]) -> tuple[OpenAIResponsesClient, FakeResponses]:
    client = OpenAIResponsesClient(settings)
    fake = FakeResponses(outcomes)
    client._client = SimpleNamespace(responses=fake)  # type: ignore[assignment]
    return client, fake


def status_error(
    cls: type[openai.APIStatusError], status: int, headers: dict[str, str] | None = None
) -> openai.APIStatusError:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    return cls("error", response=httpx2.Response(status, request=request, headers=headers or {}), body=None)


def test_unconfigured_client_raises_not_configured(settings: Settings) -> None:
    with pytest.raises(ModelError) as error:
        OpenAIResponsesClient(settings).respond(
            instructions="", input_items=[], tools=[], deadline=time.monotonic() + 30
        )
    assert error.value.kind == "not_configured"


def test_parses_tool_calls_and_replayable_items(settings: Settings) -> None:
    client, fake = client_with(settings, [response(REASONING, FUNCTION_CALL, MESSAGE, text="Hello")])
    turn = client.respond(instructions="x", input_items=[], tools=[], deadline=time.monotonic() + 30)
    assert turn.tool_calls[0].name == "resolve_airports" and turn.tool_calls[0].call_id == "call_1"
    assert turn.output_items == [
        {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "opaque"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "resolve_airports",
            "arguments": '{"queries":["SFO"]}',
        },
        {"role": "assistant", "content": "Hello"},
    ]
    sent = fake.kwargs[0]
    assert sent["store"] is False and sent["include"] == ["reasoning.encrypted_content"]
    assert sent["model"] == settings.openai_model


def test_retries_transient_errors_with_retry_after(settings: Settings) -> None:
    settings.openai_max_retries = 2
    client, fake = client_with(
        settings,
        [
            status_error(openai.RateLimitError, 429, {"retry-after": "0"}),
            status_error(openai.InternalServerError, 500, {"retry-after": "0"}),
            response(MESSAGE, text="Hello"),
        ],
    )
    turn = client.respond(instructions="x", input_items=[], tools=[], deadline=time.monotonic() + 30)
    assert turn.text == "Hello" and len(fake.kwargs) == 3


def test_does_not_retry_auth_or_bad_requests(settings: Settings) -> None:
    client, fake = client_with(settings, [status_error(openai.AuthenticationError, 401)])
    with pytest.raises(ModelError) as error:
        client.respond(instructions="x", input_items=[], tools=[], deadline=time.monotonic() + 30)
    assert error.value.kind == "unavailable" and len(fake.kwargs) == 1

    client, fake = client_with(settings, [status_error(openai.BadRequestError, 400)])
    with pytest.raises(ModelError) as error:
        client.respond(instructions="x", input_items=[], tools=[], deadline=time.monotonic() + 30)
    assert error.value.kind == "rejected" and len(fake.kwargs) == 1


def test_no_call_starts_without_enough_budget(settings: Settings) -> None:
    client, fake = client_with(settings, [response(MESSAGE, text="late")])
    with pytest.raises(ModelError) as error:
        client.respond(instructions="x", input_items=[], tools=[], deadline=time.monotonic() + 1)
    assert error.value.kind == "timeout" and fake.kwargs == []


def test_exhausted_quota_fails_fast_without_retry(settings: Settings) -> None:
    request = httpx2.Request("POST", "https://api.openai.com/v1/responses")
    quota = openai.RateLimitError(
        "no credits",
        response=httpx2.Response(429, request=request),
        body={"error": {"type": "insufficient_quota", "code": "credit_balance_exhausted"}},
    )
    client, fake = client_with(settings, [quota, response(MESSAGE, text="never reached")])
    with pytest.raises(ModelError) as error:
        client.respond(instructions="x", input_items=[], tools=[], deadline=time.monotonic() + 30)
    assert error.value.kind == "quota_exhausted" and len(fake.kwargs) == 1


def test_incomplete_responses_are_not_returned_as_answers(settings: Settings) -> None:
    partial = SimpleNamespace(
        output=[MESSAGE],
        output_text="the larg",
        usage=None,
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    client, _ = client_with(settings, [partial])
    with pytest.raises(ModelError) as error:
        client.respond(instructions="x", input_items=[], tools=[], deadline=time.monotonic() + 30)
    assert error.value.kind == "incomplete" and "max_output_tokens" in str(error.value)
