from __future__ import annotations

from app.security import (
    RateLimiter,
    hash_password,
    hash_token,
    new_token,
    origin_allowed,
    tokens_equal,
    verify_password,
)


def test_password_hash_is_argon2id_and_verifies() -> None:
    hashed = hash_password("correct horse battery staple")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "correct horse battery staple")
    assert not verify_password(hashed, "wrong password")


def test_missing_hash_never_verifies() -> None:
    assert not verify_password(None, "anything")
    assert not verify_password("not-a-hash", "anything")


def test_tokens_are_random_and_only_digests_are_comparable() -> None:
    first, second = new_token(), new_token()
    assert first != second and len(first) >= 40
    assert hash_token(first) == hash_token(first)
    assert hash_token(first) != first
    assert tokens_equal(first, first) and not tokens_equal(first, second)


def test_origin_allowed_by_list_or_matching_host() -> None:
    allowed = ["http://localhost:5173"]
    assert origin_allowed("http://localhost:5173/", "api.local", allowed)
    assert origin_allowed("https://app.example.com", "app.example.com", allowed)
    assert not origin_allowed("https://evil.example.com", "app.example.com", allowed)
    assert not origin_allowed(None, "app.example.com", allowed)


def test_rate_limiter_blocks_after_limit_per_key() -> None:
    limiter = RateLimiter(limit=2, window_seconds=60)
    assert limiter.hit("a") is None
    assert limiter.hit("a") is None
    retry_after = limiter.hit("a")
    assert retry_after is not None and retry_after >= 1
    assert limiter.hit("b") is None
