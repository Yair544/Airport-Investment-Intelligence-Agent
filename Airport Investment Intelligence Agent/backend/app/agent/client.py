"""Model provider access. All OpenAI-specific calls live in this module.

The SDK's built-in retries are disabled so retries happen in one place, inside the request time
budget, with exponential backoff, jitter and Retry-After support (spec section 9).
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import openai

from app.config import Settings

logger = logging.getLogger("app.agent.client")

ModelErrorKind = Literal[
    "not_configured",
    "timeout",
    "rate_limited",
    "quota_exhausted",
    "unavailable",
    "rejected",
    "incomplete",
]
MIN_CALL_SECONDS = 3.0


class ModelError(Exception):
    def __init__(self, kind: ModelErrorKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    arguments: str


@dataclass
class ModelTurn:
    text: str
    tool_calls: list[ToolCall]
    # Items to append to the next request's input so the model keeps its own context.
    output_items: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)


class ModelClient(Protocol):
    def respond(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        deadline: float,
    ) -> ModelTurn: ...


class OpenAIResponsesClient:
    """Responses API client with function tools, stateless (`store=False`) conversations."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
        self._client = openai.OpenAI(api_key=key, max_retries=0) if key else None

    def respond(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        deadline: float,
    ) -> ModelTurn:
        if self._client is None:
            raise ModelError("not_configured", "The language model is not configured.")
        attempts = 1 + max(0, self._settings.openai_max_retries)
        last_error: ModelError | None = None
        for attempt in range(attempts):
            remaining = deadline - time.monotonic()
            if remaining < MIN_CALL_SECONDS:
                raise last_error or ModelError("timeout", "The time budget for this request ran out.")
            try:
                response = self._client.responses.create(  # type: ignore[call-overload]
                    model=self._settings.openai_model,
                    instructions=instructions,
                    input=input_items,
                    tools=tools,
                    tool_choice="auto",
                    parallel_tool_calls=True,
                    reasoning={"effort": self._settings.openai_reasoning_effort},
                    store=False,
                    include=["reasoning.encrypted_content"],
                    max_output_tokens=self._settings.openai_max_output_tokens,
                    timeout=min(self._settings.openai_timeout_seconds, remaining),
                )
                return _to_turn(response)
            except (openai.APITimeoutError, openai.APIConnectionError) as exc:
                last_error = ModelError("timeout", "The language model did not respond in time.")
                retry_after = None
                logger.warning("model_call_retryable", extra={"error_type": type(exc).__name__})
            except openai.RateLimitError as exc:
                if _provider_error_type(exc) == "insufficient_quota":
                    # Billing, not a transient limit: retrying cannot succeed.
                    logger.error("model_call_quota_exhausted", extra={"status": exc.status_code})
                    raise ModelError(
                        "quota_exhausted", "The language model account has no remaining credits."
                    ) from exc
                last_error = ModelError("rate_limited", "The language model is rate limited.")
                retry_after = _retry_after_seconds(exc)
                logger.warning("model_call_rate_limited", extra={"attempt": attempt + 1})
            except openai.InternalServerError as exc:
                last_error = ModelError("unavailable", "The language model service is unavailable.")
                retry_after = _retry_after_seconds(exc)
                logger.warning("model_call_server_error", extra={"status": exc.status_code})
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                logger.error("model_call_auth_failed", extra={"status": exc.status_code})
                raise ModelError(
                    "unavailable", "The language model rejected the configured credentials."
                ) from exc
            except openai.APIStatusError as exc:
                logger.error("model_call_rejected", extra={"status": exc.status_code})
                raise ModelError("rejected", "The language model rejected the request.") from exc
            if attempt + 1 < attempts:
                delay = retry_after if retry_after is not None else random.uniform(0, 0.5 * 2**attempt)
                if time.monotonic() + delay + MIN_CALL_SECONDS > deadline:
                    break
                time.sleep(delay)
        raise last_error or ModelError("unavailable", "The language model is unavailable.")


def _provider_error_type(exc: openai.APIStatusError) -> str | None:
    """The provider's error `type` (e.g. "insufficient_quota"), if the body carries one."""
    body = exc.body
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict) and isinstance(error.get("type"), str):
            return str(error["type"])
    value = getattr(exc, "type", None)
    return value if isinstance(value, str) else None


def _retry_after_seconds(exc: openai.APIStatusError) -> float | None:
    value = exc.response.headers.get("retry-after") if exc.response is not None else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 30 else None


def _to_turn(response: Any) -> ModelTurn:
    if getattr(response, "status", None) == "incomplete":
        # Partial text (for example cut by max_output_tokens) must never be stored as an answer.
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None) or "unknown reason"
        raise ModelError("incomplete", f"The language model stopped before finishing ({reason}).")
    tool_calls: list[ToolCall] = []
    items: list[dict[str, Any]] = []
    for item in response.output or []:
        kind = getattr(item, "type", None)
        if kind == "function_call":
            tool_calls.append(ToolCall(call_id=item.call_id, name=item.name, arguments=item.arguments))
            items.append(
                {
                    "type": "function_call",
                    "call_id": item.call_id,
                    "name": item.name,
                    "arguments": item.arguments,
                }
            )
        elif kind == "reasoning" and getattr(item, "encrypted_content", None):
            items.append(
                {
                    "type": "reasoning",
                    "id": item.id,
                    "summary": [part.model_dump(exclude_none=True) for part in item.summary or []],
                    "encrypted_content": item.encrypted_content,
                }
            )
        elif kind == "message":
            text = "".join(
                getattr(part, "text", "") for part in item.content or [] if part.type == "output_text"
            )
            if text:
                items.append({"role": "assistant", "content": text})
    usage: dict[str, int] = {}
    if getattr(response, "usage", None) is not None:
        usage = {
            "input_tokens": int(response.usage.input_tokens or 0),
            "output_tokens": int(response.usage.output_tokens or 0),
        }
    return ModelTurn(
        text=(response.output_text or "").strip(),
        tool_calls=tool_calls,
        output_items=items,
        usage=usage,
    )
