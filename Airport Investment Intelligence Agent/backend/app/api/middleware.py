"""ASGI middleware: body size limit, security headers and request logging without content."""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Mapping
from typing import Any

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.http")

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)


class _BodyTooLarge(Exception):
    pass


async def _send_json(send: Send, status: int, payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class BodySizeLimitMiddleware:
    """Reject request bodies above `max_bytes` with 413, using Content-Length and a stream guard."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        too_large = {"error": {"code": "payload_too_large", "message": "The request body is too large."}}
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = 0
                if declared > self.max_bytes:
                    await _send_json(send, 413, too_large)
                    return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not started:
                await _send_json(send, 413, too_large)


class JsonContentTypeMiddleware:
    """API requests that carry a body must declare application/json (415 otherwise)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and scope.get("method") in {"POST", "PUT", "PATCH"}
            and scope.get("path", "").startswith("/api/")
        ):
            headers = dict(scope.get("headers", []))
            has_body = (
                headers.get(b"content-length", b"0") not in (b"", b"0") or b"transfer-encoding" in headers
            )
            content_type = headers.get(b"content-type", b"").split(b";")[0].strip().lower()
            if has_body and content_type != b"application/json":
                await _send_json(
                    send,
                    415,
                    {
                        "error": {
                            "code": "unsupported_media_type",
                            "message": "Send the body as application/json.",
                        }
                    },
                )
                return
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Adds conservative security headers; API responses are never cached.

    `hsts` adds Strict-Transport-Security for deployments served over HTTPS.
    """

    def __init__(self, app: ASGIApp, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope.get("path", "").startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers += [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", CONTENT_SECURITY_POLICY.encode()),
                ]
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                if self.hsts:
                    headers.append((b"strict-transport-security", b"max-age=31536000; includeSubDomains"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


class RequestLogMiddleware:
    """One structured line per request: id, method, path, status, duration. No bodies or cookies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        http_request_id = uuid.uuid4().hex[:12]
        scope.setdefault("state", {})["http_request_id"] = http_request_id
        started = time.perf_counter()
        status_holder = {"status": 500}

        async def logging_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", http_request_id.encode()))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, logging_send)
        finally:
            logger.info(
                "http_request",
                extra={
                    "http_request_id": http_request_id,
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_holder["status"],
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
