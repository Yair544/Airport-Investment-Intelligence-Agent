"""Uniform error envelope: {"error": {"code", "message", "request_id"?, "conversation_id"?}}."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.schemas import ErrorBody, ErrorEnvelope

logger = logging.getLogger("app.api")

_STATUS_CODES = {
    400: "bad_request",
    401: "not_authenticated",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "invalid_request",
    429: "rate_limited",
    500: "internal_error",
    503: "service_unavailable",
    504: "timeout",
}


class ApiError(Exception):
    """An expected failure that maps to an HTTP status and a stable error code."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        *,
        request_id: str | None = None,
        conversation_id: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.request_id = request_id
        self.conversation_id = conversation_id
        self.headers = headers or {}


def error_response(
    status: int,
    code: str,
    message: str,
    *,
    request_id: str | None = None,
    conversation_id: str | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorEnvelope(
        error=ErrorBody(code=code, message=message, request_id=request_id, conversation_id=conversation_id)
    )
    return JSONResponse(
        status_code=status,
        content=body.model_dump(mode="json", exclude_none=True),
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return error_response(
            exc.status,
            exc.code,
            exc.message,
            request_id=exc.request_id,
            conversation_id=exc.conversation_id,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Describe where validation failed without echoing the submitted values.
        details = []
        for error in exc.errors()[:5]:
            location = ".".join(str(part) for part in error.get("loc", ()) if part != "body")
            details.append(f"{location}: {error.get('msg', 'invalid value')}" if location else error["msg"])
        message = "Invalid request. " + "; ".join(details) if details else "Invalid request."
        return error_response(422, "invalid_request", message)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_CODES.get(exc.status_code, "error")
        message = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return error_response(exc.status_code, code, message, headers=getattr(exc, "headers", None))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_error",
            extra={"path": request.url.path, "error_type": type(exc).__name__},
        )
        return error_response(500, "internal_error", "An unexpected error occurred.")
