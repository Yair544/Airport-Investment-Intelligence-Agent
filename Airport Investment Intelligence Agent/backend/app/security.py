"""Password hashing, session tokens and in-memory rate limiting."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

# argon2-cffi defaults to Argon2id with a random salt per hash.
_hasher = PasswordHasher()
# Verifying against a dummy hash keeps the timing of "unknown user" close to "wrong password".
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_token() -> str:
    """Random URL-safe token for session ids and CSRF tokens (256 bits)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Only this digest of a session token is stored server side."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def origin_allowed(origin: str | None, host: str | None, allowed_origins: list[str]) -> bool:
    """Accept configured origins, or an origin whose host matches the request Host header."""
    if not origin:
        return False
    normalized = origin.rstrip("/").lower()
    if normalized in {item.rstrip("/").lower() for item in allowed_origins}:
        return True
    parts = urlsplit(normalized)
    return bool(host) and parts.scheme in {"http", "https"} and parts.netloc == (host or "").lower()


class RateLimiter:
    """Sliding-window limiter kept in process memory (the deployment runs a single process)."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str) -> float | None:
        """Seconds to wait if `key` is at its limit, without recording a hit."""
        now = time.monotonic()
        with self._lock:
            window = self._hits.get(key)
            if not window:
                return None
            while window and now - window[0] >= self.window_seconds:
                window.popleft()
            if len(window) >= self.limit:
                return max(1.0, self.window_seconds - (now - window[0]))
            return None

    def hit(self, key: str) -> float | None:
        """Record a hit. Returns None when allowed, or the seconds to wait before retrying."""
        now = time.monotonic()
        with self._lock:
            window = self._hits.setdefault(key, deque())
            while window and now - window[0] >= self.window_seconds:
                window.popleft()
            if len(window) >= self.limit:
                return max(1.0, self.window_seconds - (now - window[0]))
            window.append(now)
            if len(self._hits) > 10_000:
                self._prune(now)
            return None

    def _prune(self, now: float) -> None:
        stale = [key for key, hits in self._hits.items() if not hits or now - hits[-1] >= self.window_seconds]
        for key in stale:
            del self._hits[key]
