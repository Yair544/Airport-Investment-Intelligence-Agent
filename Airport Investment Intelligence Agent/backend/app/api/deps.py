"""Request dependencies: application services, authentication, CSRF, origin and rate limits."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Request

from app.api.errors import ApiError
from app.config import Settings
from app.security import RateLimiter, hash_token, origin_allowed, tokens_equal
from app.storage import SessionRecord, Storage, utcnow


@dataclass
class RateLimits:
    login: RateLimiter
    register: RateLimiter
    chat: RateLimiter
    status: RateLimiter
    refresh: RateLimiter

    @classmethod
    def from_settings(cls, settings: Settings) -> RateLimits:
        return cls(
            login=RateLimiter(settings.rate_limit_login_per_minute, 60.0),
            register=RateLimiter(settings.rate_limit_register_per_hour, 3600.0),
            chat=RateLimiter(settings.rate_limit_chat_per_minute, 60.0),
            status=RateLimiter(settings.rate_limit_status_per_minute, 60.0),
            refresh=RateLimiter(settings.rate_limit_refresh_per_hour, 3600.0),
        )


@dataclass
class AppContainer:
    """Long-lived services shared by all requests (built once in app.main)."""

    settings: Settings
    storage: Storage
    agent: Any  # AgentService, or a test double with the same run_turn method
    data: Any  # DataService (None in tests that do not touch data)
    submit_refresh: Callable[[str], None]  # schedules a refresh job id on the background worker
    rate_limits: RateLimits = field(init=False)

    def __post_init__(self) -> None:
        self.rate_limits = RateLimits.from_settings(self.settings)


def get_container(request: Request) -> AppContainer:
    container: AppContainer = request.app.state.container
    return container


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def enforce_rate_limit(limiter: RateLimiter, key: str) -> None:
    retry_after = limiter.hit(key)
    if retry_after is not None:
        raise ApiError(
            429,
            "rate_limited",
            "Too many requests. Please wait and try again.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )


def require_origin(request: Request, container: AppContainer = Depends(get_container)) -> None:
    """State-changing requests must come from an allowed browser origin."""
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer:
            parts = referer.split("/")
            origin = "/".join(parts[:3]) if len(parts) >= 3 else None
    if not origin_allowed(origin, request.headers.get("host"), container.settings.allowed_origins):
        raise ApiError(403, "origin_not_allowed", "This request origin is not allowed.")


def current_session(request: Request, container: AppContainer = Depends(get_container)) -> SessionRecord:
    token = request.cookies.get(container.settings.session_cookie_name)
    if not token:
        raise ApiError(401, "not_authenticated", "Sign in to continue.")
    session = container.storage.get_active_session(hash_token(token), utcnow())
    if session is None:
        raise ApiError(401, "session_expired", "Your session has ended. Sign in again.")
    return session


def require_csrf(
    request: Request,
    session: SessionRecord = Depends(current_session),
    _: None = Depends(require_origin),
) -> SessionRecord:
    header = request.headers.get("x-csrf-token")
    if not header or not tokens_equal(header, session.csrf_token):
        raise ApiError(403, "csrf_failed", "The security token is missing or invalid. Reload the page.")
    return session
