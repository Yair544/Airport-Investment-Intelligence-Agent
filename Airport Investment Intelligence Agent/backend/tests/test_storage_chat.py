"""Chat request lifecycle at the storage level (spec sections 6 and 8)."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.storage import ConflictError, LimitExceededError, NotFoundError, Storage, utcnow

LIMITS = {"max_active_per_user": 2, "max_active_total": 8, "max_attempts": 3}


def accept(
    storage: Storage,
    owner: str,
    request_id: str,
    message: str = "Compare LAX and SNA",
    conversation_id: str | None = None,
    now: datetime | None = None,
    lease_seconds: float = 120,
):  # type: ignore[no-untyped-def]
    return storage.accept_chat_request(
        owner_id=owner,
        request_id=request_id,
        message=message,
        sent_conversation_id=conversation_id,
        lease_seconds=lease_seconds,
        now=now or utcnow(),
        **LIMITS,
    )


def complete(storage: Storage, owner: str, record, answer: str = "done") -> bool:  # type: ignore[no-untyped-def]
    return storage.complete_chat_request(
        owner_id=owner,
        request_id=record.request_id,
        attempt_id=record.attempt_id,
        answer=answer,
        response={"conversation_id": record.conversation_id, "answer": answer},
        context={"airports": ["apt_lax"]},
        data_version_id="v1",
        rules_version="r1",
        now=utcnow(),
    )


@pytest.fixture
def owner(storage: Storage) -> str:
    return storage.create_user("analyst", "hash")


def test_first_message_creates_conversation_and_retry_returns_saved_answer(
    storage: Storage, owner: str
) -> None:
    started = accept(storage, owner, "r1")
    assert started.kind == "started" and started.created_conversation
    assert complete(storage, owner, started.request)

    retry = accept(storage, owner, "r1")
    assert retry.kind == "completed"
    assert retry.request.response == {"conversation_id": started.request.conversation_id, "answer": "done"}
    messages = storage.get_messages(owner, started.request.conversation_id)
    assert [m.role for m in messages] == ["user", "assistant"]
    assert len(storage.list_conversations(owner)) == 1
    context = storage.get_analysis_context(owner, started.request.conversation_id)
    assert context is not None and context.data_version_id == "v1"


def test_same_request_id_with_different_content_conflicts(storage: Storage, owner: str) -> None:
    accept(storage, owner, "r1")
    with pytest.raises(ConflictError) as error:
        accept(storage, owner, "r1", message="Something else")
    assert error.value.code == "request_id_conflict"


def test_processing_request_reports_in_progress_and_blocks_new_messages(storage: Storage, owner: str) -> None:
    first = accept(storage, owner, "r1")
    assert accept(storage, owner, "r1").kind == "in_progress"
    with pytest.raises(ConflictError) as error:
        accept(storage, owner, "r2", message="next", conversation_id=first.request.conversation_id)
    assert error.value.code == "conversation_busy"


def test_unknown_or_foreign_conversation_is_not_found(storage: Storage, owner: str) -> None:
    other = storage.create_user("other", "hash")
    started = accept(storage, other, "x1")
    with pytest.raises(NotFoundError):
        accept(storage, owner, "r1", conversation_id=started.request.conversation_id)
    with pytest.raises(NotFoundError):
        accept(storage, owner, "r2", conversation_id="missing")
    assert storage.get_chat_request(owner, "x1", utcnow()) is None
    assert storage.get_messages(owner, started.request.conversation_id) == []


def test_failed_request_retries_without_duplicate_message(storage: Storage, owner: str) -> None:
    first = accept(storage, owner, "r1")
    assert storage.fail_chat_request(
        owner_id=owner,
        request_id="r1",
        attempt_id=first.request.attempt_id,  # type: ignore[arg-type]
        error={"code": "model_unavailable", "message": "down"},
        now=utcnow(),
    )
    retry = accept(storage, owner, "r1")
    assert retry.kind == "started" and retry.request.attempt_count == 2
    assert retry.request.attempt_id != first.request.attempt_id
    assert [m.role for m in storage.get_messages(owner, first.request.conversation_id)] == ["user"]
    # The superseded attempt can no longer save.
    assert not complete(storage, owner, first.request)
    assert complete(storage, owner, retry.request)


def test_retry_after_conversation_moved_on_is_stale(storage: Storage, owner: str) -> None:
    first = accept(storage, owner, "r1")
    storage.fail_chat_request(
        owner_id=owner,
        request_id="r1",
        attempt_id=first.request.attempt_id,  # type: ignore[arg-type]
        error={"code": "x", "message": "x"},
        now=utcnow(),
    )
    second = accept(
        storage, owner, "r2", message="new question", conversation_id=first.request.conversation_id
    )
    assert second.kind == "started"
    with pytest.raises(ConflictError) as error:
        accept(storage, owner, "r1")
    assert error.value.code == "stale_request"


def test_expired_lease_becomes_interrupted_and_can_retry(storage: Storage, owner: str) -> None:
    past = utcnow() - timedelta(hours=1)
    first = accept(storage, owner, "r1", now=past, lease_seconds=60)
    status = storage.get_chat_request(owner, "r1", utcnow())
    assert status is not None and status.status == "interrupted"
    assert status.error is not None and status.error["code"] == "interrupted"
    retry = accept(storage, owner, "r1")
    assert retry.kind == "started" and retry.request.conversation_id == first.request.conversation_id
    # An expired lease cannot save either.
    assert not complete(storage, owner, first.request)


def test_startup_sweep_leaves_valid_leases_alone(storage: Storage, owner: str) -> None:
    accept(storage, owner, "r1", now=utcnow() - timedelta(hours=1), lease_seconds=60)
    live = accept(storage, owner, "r2", message="other conversation")
    assert storage.interrupt_expired_requests(utcnow()) == 1
    current = storage.get_chat_request(owner, "r2", utcnow())
    assert current is not None and current.status == "processing"
    assert live.request.attempt_id == current.attempt_id


def test_attempt_limit(storage: Storage, owner: str) -> None:
    record = accept(storage, owner, "r1").request
    for _ in range(2):
        storage.fail_chat_request(
            owner_id=owner,
            request_id="r1",
            attempt_id=record.attempt_id,
            error={"code": "x", "message": "x"},
            now=utcnow(),  # type: ignore[arg-type]
        )
        record = accept(storage, owner, "r1").request
    storage.fail_chat_request(
        owner_id=owner,
        request_id="r1",
        attempt_id=record.attempt_id,
        error={"code": "x", "message": "x"},
        now=utcnow(),  # type: ignore[arg-type]
    )
    with pytest.raises(LimitExceededError) as error:
        accept(storage, owner, "r1")
    assert error.value.code == "attempt_limit_reached"


def test_active_request_cap_per_user(storage: Storage, owner: str) -> None:
    accept(storage, owner, "r1", message="a")
    accept(storage, owner, "r2", message="b")
    with pytest.raises(LimitExceededError) as error:
        accept(storage, owner, "r3", message="c")
    assert error.value.code == "too_many_active_requests"


def test_history_contains_only_completed_exchanges(storage: Storage, owner: str) -> None:
    first = accept(storage, owner, "r1", message="first question")
    complete(storage, owner, first.request, answer="first answer")
    accept(storage, owner, "r2", message="second question", conversation_id=first.request.conversation_id)
    history = storage.load_history(
        owner, first.request.conversation_id, exclude_request_id="r2", max_messages=10, max_chars=10_000
    )
    assert history == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]


def test_single_active_refresh_job_and_lease(storage: Storage) -> None:
    now = utcnow()
    job, created = storage.create_or_get_active_refresh_job(now)
    again, created_again = storage.create_or_get_active_refresh_job(now)
    assert created and not created_again and again.job_id == job.job_id
    assert storage.claim_refresh_job(job.job_id, "worker-1", 60, now)
    assert not storage.claim_refresh_job(job.job_id, "worker-2", 60, now)
    assert storage.heartbeat_refresh_job(job.job_id, "worker-1", 60, now, stage="fetching")
    assert not storage.heartbeat_refresh_job(job.job_id, "worker-2", 60, now)
    assert storage.finish_refresh_job(job.job_id, "worker-1", status="completed", changed=False, now=now)
    finished = storage.get_refresh_job(job.job_id)
    assert finished is not None and finished.status == "completed" and finished.changed is False
    _, created_new = storage.create_or_get_active_refresh_job(now)
    assert created_new


def test_expired_refresh_lease_is_interrupted(storage: Storage) -> None:
    past = utcnow() - timedelta(hours=1)
    job, _ = storage.create_or_get_active_refresh_job(past)
    storage.claim_refresh_job(job.job_id, "worker-1", 60, past)
    replacement, created = storage.create_or_get_active_refresh_job(utcnow())
    assert created and replacement.job_id != job.job_id
    old = storage.get_refresh_job(job.job_id)
    assert old is not None and old.status == "interrupted"
    assert not storage.activate_data_version("missing", job.job_id, "worker-1", utcnow())


def test_sweep_without_queued_jobs_only_interrupts_expired_running_jobs(storage: Storage) -> None:
    now = utcnow()
    queued, _ = storage.create_or_get_active_refresh_job(now)
    assert storage.interrupt_stale_refresh_jobs(now, include_queued=False) == 0
    still_queued = storage.get_refresh_job(queued.job_id)
    assert still_queued is not None and still_queued.status == "queued"
    storage.claim_refresh_job(queued.job_id, "worker-1", 60, now - timedelta(hours=1))
    assert storage.interrupt_stale_refresh_jobs(now, include_queued=False) == 1
    swept = storage.get_refresh_job(queued.job_id)
    assert swept is not None and swept.status == "interrupted"


def test_password_reset_revokes_sessions_but_rehash_does_not(storage: Storage, owner: str) -> None:
    future = utcnow() + timedelta(hours=1)
    storage.create_session(owner, "session-a", "csrf", future)
    storage.update_password(owner, "rehashed", revoke_sessions=False)
    assert storage.get_active_session("session-a", utcnow()) is not None
    storage.update_password(owner, "new-hash")
    assert storage.get_active_session("session-a", utcnow()) is None


def test_reading_an_expired_refresh_job_reports_it_interrupted(storage: Storage) -> None:
    past = utcnow() - timedelta(hours=1)
    job, _ = storage.create_or_get_active_refresh_job(past)
    storage.claim_refresh_job(job.job_id, "worker-1", 60, past)
    raw = storage.get_refresh_job(job.job_id)
    assert raw is not None and raw.status == "running"
    current = storage.get_refresh_job(job.job_id, utcnow())
    assert current is not None and current.status == "interrupted"
    assert current.error is not None and current.error["code"] == "interrupted"


def test_conversation_scoped_sweep_leaves_other_conversations(storage: Storage, owner: str) -> None:
    past = utcnow() - timedelta(hours=1)
    first = accept(storage, owner, "r1", message="a", now=past, lease_seconds=60)
    accept(storage, owner, "r2", message="b", now=past, lease_seconds=60)
    assert storage.interrupt_expired_requests(utcnow(), conversation_id=first.request.conversation_id) == 1
    assert storage.interrupt_expired_requests(utcnow(), conversation_id=first.request.conversation_id) == 0
    assert storage.interrupt_expired_requests(utcnow()) == 1
