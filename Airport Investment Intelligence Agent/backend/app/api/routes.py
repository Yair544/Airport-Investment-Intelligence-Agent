"""HTTP handlers. Ownership always comes from the authenticated session, never from the body."""

from __future__ import annotations

import logging
import time
import uuid
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.agent.service import AgentError, TurnInput, TurnOutput
from app.api.deps import (
    AppContainer,
    client_ip,
    current_session,
    enforce_rate_limit,
    get_container,
    require_csrf,
    require_origin,
)
from app.api.errors import ApiError
from app.api.schemas import (
    AuthResponse,
    ChatAccepted,
    ChatRequest,
    ChatRequestStatus,
    ChatResponse,
    ConversationDetail,
    ConversationList,
    ConversationSummary,
    ErrorBody,
    LoginRequest,
    MessageOut,
    MessageRequestInfo,
    RefreshJob,
    RegisterRequest,
    SourceRefreshResult,
    UserOut,
)
from app.security import hash_password, hash_token, new_token, password_needs_rehash, verify_password
from app.storage import (
    ChatRequestRecord,
    ConflictError,
    LimitExceededError,
    NotFoundError,
    RefreshJobRecord,
    SessionRecord,
    utcnow,
)

logger = logging.getLogger("app.api")

router = APIRouter(prefix="/api")

LOGIN_FAILED = "Invalid username or password."


# -- auth ------------------------------------------------------------------------------------


@router.post("/auth/login", response_model=AuthResponse, dependencies=[Depends(require_origin)])
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> AuthResponse:
    # Every attempt counts per client address; only failures count per username, so nobody can
    # lock a known user out with a burst of requests that never needed the password.
    enforce_rate_limit(container.rate_limits.login, f"ip:{client_ip(request)}")
    user_key = f"user:{body.username.casefold()}"
    retry_after = container.rate_limits.login.blocked(user_key)
    if retry_after is not None:
        raise ApiError(
            429,
            "rate_limited",
            "Too many failed sign-in attempts. Please wait and try again.",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )
    storage = container.storage
    user = storage.get_user_by_username(body.username)
    password_hash = user.password_hash if user and user.is_active else None
    if user is None or not user.is_active or not verify_password(password_hash, body.password):
        container.rate_limits.login.hit(user_key)
        raise ApiError(401, "invalid_credentials", LOGIN_FAILED)
    if password_needs_rehash(user.password_hash):
        storage.update_password(user.user_id, hash_password(body.password), revoke_sessions=False)

    logger.info("login_succeeded", extra={"user_id": user.user_id})
    return _start_session(container, request, response, user.user_id, user.username)


@router.post("/auth/register", response_model=AuthResponse, dependencies=[Depends(require_origin)])
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    container: AppContainer = Depends(get_container),
) -> AuthResponse:
    """Self-service sign-up; the new account is signed in straight away."""
    enforce_rate_limit(container.rate_limits.register, f"ip:{client_ip(request)}")
    try:
        user_id = container.storage.create_user(body.username, hash_password(body.password))
    except ConflictError as exc:
        raise ApiError(409, exc.code, str(exc)) from exc
    logger.info("user_registered", extra={"user_id": user_id})
    return _start_session(container, request, response, user_id, body.username)


def _start_session(
    container: AppContainer, request: Request, response: Response, user_id: str, username: str
) -> AuthResponse:
    """Replace any session this browser already holds and set the new session cookie."""
    storage = container.storage
    previous = request.cookies.get(container.settings.session_cookie_name)
    if previous:
        storage.revoke_session(hash_token(previous))
    token, csrf_token = new_token(), new_token()
    expires_at = utcnow() + timedelta(hours=container.settings.session_ttl_hours)
    storage.create_session(user_id, hash_token(token), csrf_token, expires_at)
    response.set_cookie(
        container.settings.session_cookie_name,
        token,
        max_age=int(container.settings.session_ttl_hours * 3600),
        httponly=True,
        samesite="lax",
        secure=container.settings.secure_cookies,
        path="/",
    )
    return AuthResponse(user=UserOut(user_id=user_id, username=username), csrf_token=csrf_token)


@router.get("/auth/me", response_model=AuthResponse)
def me(session: SessionRecord = Depends(current_session)) -> AuthResponse:
    return AuthResponse(
        user=UserOut(user_id=session.user_id, username=session.username), csrf_token=session.csrf_token
    )


@router.post("/auth/logout", status_code=204)
def logout(
    session: SessionRecord = Depends(require_csrf),
    container: AppContainer = Depends(get_container),
) -> Response:
    container.storage.revoke_session(session.session_hash)
    response = Response(status_code=204)
    response.delete_cookie(
        container.settings.session_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=container.settings.secure_cookies,
    )
    return response


# -- chat ------------------------------------------------------------------------------------


def _status_url(request_id: str) -> str:
    return f"/api/chat/requests/{request_id}"


def _accepted(record: ChatRequestRecord) -> JSONResponse:
    body = ChatAccepted(
        request_id=record.request_id,
        conversation_id=record.conversation_id,
        status=record.status,
        status_url=_status_url(record.request_id),
    )
    return JSONResponse(status_code=202, content=body.model_dump(mode="json"))


@router.post(
    "/chat",
    response_model=ChatResponse,
    responses={202: {"model": ChatAccepted}},
)
def chat(
    body: ChatRequest,
    session: SessionRecord = Depends(require_csrf),
    container: AppContainer = Depends(get_container),
) -> Any:
    settings = container.settings
    enforce_rate_limit(container.rate_limits.chat, f"user:{session.user_id}")
    message = body.message.strip()
    if len(message) > settings.max_message_chars:
        raise ApiError(
            422,
            "message_too_long",
            f"The message is longer than {settings.max_message_chars} characters.",
        )
    request_id = str(body.request_id)
    storage = container.storage
    try:
        outcome = storage.accept_chat_request(
            owner_id=session.user_id,
            request_id=request_id,
            message=message,
            sent_conversation_id=body.conversation_id,
            lease_seconds=settings.lease_seconds,
            now=utcnow(),
            max_active_per_user=settings.max_active_requests_per_user,
            max_active_total=settings.max_active_requests_total,
            max_attempts=settings.chat_max_attempts,
        )
    except ConflictError as exc:
        raise ApiError(
            409,
            exc.code,
            str(exc),
            request_id=request_id,
            conversation_id=exc.conversation_id or body.conversation_id,
        ) from exc
    except NotFoundError as exc:
        raise ApiError(404, exc.code, str(exc)) from exc
    except LimitExceededError as exc:
        # attempt_limit_reached never clears, so only the transient limits advertise a retry delay.
        headers = None if exc.code == "attempt_limit_reached" else {"Retry-After": "5"}
        raise ApiError(429, exc.code, str(exc), request_id=request_id, headers=headers) from exc

    record = outcome.request
    if outcome.kind == "completed":
        return JSONResponse(status_code=200, content=record.response)
    if outcome.kind == "in_progress":
        return _accepted(record)
    return _process(container, session, record)


def _process(container: AppContainer, session: SessionRecord, record: ChatRequestRecord) -> Any:
    """Run the agent outside any database transaction, then persist answer and context together."""
    settings = container.settings
    storage = container.storage
    assert record.attempt_id is not None
    ids = {"request_id": record.request_id, "conversation_id": record.conversation_id}
    context_record = storage.get_analysis_context(session.user_id, record.conversation_id)
    turn = TurnInput(
        owner_id=session.user_id,
        conversation_id=record.conversation_id,
        request_id=record.request_id,
        attempt_id=record.attempt_id,
        message=record.message,
        history=storage.load_history(
            session.user_id,
            record.conversation_id,
            exclude_request_id=record.request_id,
            max_messages=settings.history_max_messages,
            max_chars=settings.history_max_chars,
        ),
        analysis_context=context_record.context if context_record else None,
        context_data_version_id=context_record.data_version_id if context_record else None,
        deadline=time.monotonic() + settings.chat_request_budget_seconds,
    )
    started = time.perf_counter()
    try:
        output: TurnOutput = container.agent.run_turn(turn)
    except AgentError as exc:
        storage.fail_chat_request(
            owner_id=session.user_id,
            request_id=record.request_id,
            attempt_id=record.attempt_id,
            error={"code": exc.code, "message": exc.public_message},
            now=utcnow(),
        )
        raise ApiError(
            exc.status,
            exc.code,
            exc.public_message,
            request_id=record.request_id,
            conversation_id=record.conversation_id,
        ) from exc
    except Exception as exc:
        logger.exception("chat_turn_failed", extra=ids)
        storage.fail_chat_request(
            owner_id=session.user_id,
            request_id=record.request_id,
            attempt_id=record.attempt_id,
            error={"code": "internal_error", "message": "The request failed unexpectedly. You can retry."},
            now=utcnow(),
        )
        raise ApiError(
            500,
            "internal_error",
            "The request failed unexpectedly. You can retry.",
            request_id=record.request_id,
            conversation_id=record.conversation_id,
        ) from exc

    envelope = ChatResponse(
        conversation_id=record.conversation_id,
        answer=output.answer,
        results=output.results,
        sources=output.sources,
        limitations=output.limitations,
    )
    payload = envelope.model_dump(mode="json")
    saved = storage.complete_chat_request(
        owner_id=session.user_id,
        request_id=record.request_id,
        attempt_id=record.attempt_id,
        answer=output.answer,
        response=payload,
        context=output.context,
        data_version_id=output.data_version_id,
        rules_version=output.rules_version,
        now=utcnow(),
    )
    logger.info(
        "chat_request_finished",
        extra={
            **ids,
            "attempt_id": record.attempt_id,
            "saved": saved,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "model_calls": output.model_calls,
            "tool_calls": output.tool_calls,
        },
    )
    if saved:
        return JSONResponse(status_code=200, content=payload)
    current = storage.get_chat_request(session.user_id, record.request_id, utcnow())
    if current is not None and current.status == "completed":
        return JSONResponse(status_code=200, content=current.response)
    if current is not None and current.status == "processing":
        return _accepted(current)
    raise ApiError(
        504,
        "processing_timeout",
        "Processing took longer than allowed and the answer was not saved. You can retry.",
        request_id=record.request_id,
        conversation_id=record.conversation_id,
    )


@router.get("/chat/requests/{request_id}", response_model=ChatRequestStatus)
def chat_request_status(
    request_id: str,
    session: SessionRecord = Depends(current_session),
    container: AppContainer = Depends(get_container),
) -> ChatRequestStatus:
    enforce_rate_limit(container.rate_limits.status, f"user:{session.user_id}")
    try:
        normalized = str(uuid.UUID(request_id))
    except ValueError as exc:
        raise ApiError(404, "request_not_found", "Request not found.") from exc
    record = container.storage.get_chat_request(session.user_id, normalized, utcnow())
    if record is None:
        raise ApiError(404, "request_not_found", "Request not found.")
    error = (
        ErrorBody(**record.error, request_id=record.request_id, conversation_id=record.conversation_id)
        if (record.error and record.status in ("failed", "interrupted"))
        else None
    )
    return ChatRequestStatus(
        request_id=record.request_id,
        conversation_id=record.conversation_id,
        status=record.status,
        created_at=record.created_at,
        updated_at=record.updated_at,
        response=record.response if record.status == "completed" else None,
        error=error,
    )


# -- conversations ---------------------------------------------------------------------------


@router.get("/conversations", response_model=ConversationList)
def list_conversations(
    session: SessionRecord = Depends(current_session),
    container: AppContainer = Depends(get_container),
) -> ConversationList:
    records = container.storage.list_conversations(session.user_id)
    return ConversationList(
        conversations=[
            ConversationSummary(conversation_id=r.conversation_id, title=r.title, updated_at=r.updated_at)
            for r in records
        ]
    )


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str,
    session: SessionRecord = Depends(current_session),
    container: AppContainer = Depends(get_container),
) -> ConversationDetail:
    storage = container.storage
    conversation = storage.get_conversation(session.user_id, conversation_id)
    if conversation is None:
        raise ApiError(404, "conversation_not_found", "Conversation not found.")
    storage.interrupt_expired_requests(utcnow(), conversation_id=conversation_id)
    messages = [
        MessageOut(
            seq=m.seq,
            role=m.role,
            content=m.content,
            created_at=m.created_at,
            request=MessageRequestInfo(
                request_id=m.request_id,
                status=m.request_status,
                sent_conversation_id=m.sent_conversation_id,
            )
            if m.role == "user" and m.request_status
            else None,
            response=m.response if m.role == "assistant" and m.response else None,
        )
        for m in storage.get_messages(session.user_id, conversation_id)
    ]
    return ConversationDetail(
        conversation_id=conversation.conversation_id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=messages,
    )


# -- data refresh ----------------------------------------------------------------------------


def _job_out(container: AppContainer, job: RefreshJobRecord) -> RefreshJob:
    error = None
    if job.error:
        error = ErrorBody(
            code=str(job.error.get("code", "refresh_failed")), message=str(job.error.get("message", ""))
        )
    sources = []
    for item in job.sources:
        try:
            sources.append(SourceRefreshResult.model_validate(item))
        except ValueError:
            logger.warning("refresh_source_unreadable", extra={"job_id": job.job_id})
    return RefreshJob(
        job_id=job.job_id,
        status=job.status,
        stage=job.stage,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
        sources=sources,
        changed=job.changed,
        active_version_id=container.storage.get_active_data_version_id(),
        error=error,
        status_url=f"/api/data/refresh/{job.job_id}",
    )


@router.post("/data/refresh", status_code=202, response_model=RefreshJob)
def start_refresh(
    session: SessionRecord = Depends(require_csrf),
    container: AppContainer = Depends(get_container),
) -> RefreshJob:
    enforce_rate_limit(container.rate_limits.refresh, f"user:{session.user_id}")
    job, created = container.storage.create_or_get_active_refresh_job(utcnow())
    if created:
        container.submit_refresh(job.job_id)
        logger.info("refresh_requested", extra={"job_id": job.job_id})
    return _job_out(container, job)


@router.get("/data/refresh/{job_id}", response_model=RefreshJob)
def refresh_status(
    job_id: str,
    session: SessionRecord = Depends(current_session),
    container: AppContainer = Depends(get_container),
) -> RefreshJob:
    enforce_rate_limit(container.rate_limits.status, f"user:{session.user_id}")
    job = container.storage.get_refresh_job(job_id, utcnow())
    if job is None:
        raise ApiError(404, "job_not_found", "Refresh job not found.")
    return _job_out(container, job)


# -- health ----------------------------------------------------------------------------------


@router.get("/health")
def health(container: AppContainer = Depends(get_container)) -> dict[str, Any]:
    return {"status": "ok", "data_version_id": container.storage.get_active_data_version_id()}


@router.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
def api_not_found(path: str) -> None:
    raise ApiError(404, "not_found", "Not found.")
