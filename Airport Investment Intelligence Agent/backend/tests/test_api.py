"""HTTP contract, authentication and chat lifecycle with a scripted agent (no model, no data)."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.service import AgentError, TurnInput, TurnOutput
from app.analysis.models import RULES_VERSION
from app.api.deps import AppContainer
from app.config import Settings
from app.main import create_app
from app.security import hash_password
from app.storage import Storage, utcnow

ORIGIN = {"Origin": "http://testserver"}
PASSWORD = "a-long-test-password"


class ScriptedAgent:
    """Returns canned answers; can fail or block to exercise the lifecycle."""

    def __init__(self) -> None:
        self.calls: list[TurnInput] = []
        self.fail_with: AgentError | None = None
        self.block: threading.Event | None = None

    def run_turn(self, turn: TurnInput) -> TurnOutput:
        self.calls.append(turn)
        if self.block is not None:
            self.block.wait(timeout=5)
        if self.fail_with is not None:
            raise self.fail_with
        return TurnOutput(
            answer=f"answer to: {turn.message}",
            results=[],
            sources=[],
            limitations=["example limitation"],
            context={"airports": [{"airport_id": "apt_lax", "code": "LAX", "name": "Los Angeles"}]},
            data_version_id="v1",
            rules_version=RULES_VERSION,
        )


@pytest.fixture
def agent() -> ScriptedAgent:
    return ScriptedAgent()


@pytest.fixture
def submitted() -> list[str]:
    return []


@pytest.fixture
def client(
    settings: Settings, storage: Storage, agent: ScriptedAgent, submitted: list[str]
) -> Iterator[TestClient]:
    container = AppContainer(
        settings=settings, storage=storage, agent=agent, data=None, submit_refresh=submitted.append
    )
    with TestClient(create_app(settings, container=container)) as test_client:
        yield test_client


def make_user(storage: Storage, username: str = "analyst") -> str:
    return storage.create_user(username, hash_password(PASSWORD))


def login(client: TestClient, username: str = "analyst") -> str:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": PASSWORD}, headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    return str(response.json()["csrf_token"])


def chat(
    client: TestClient,
    csrf: str,
    message: str,
    request_id: str | None = None,
    conversation_id: str | None = None,
) -> Any:
    return client.post(
        "/api/chat",
        json={
            "request_id": request_id or str(uuid.uuid4()),
            "message": message,
            "conversation_id": conversation_id,
        },
        headers={**ORIGIN, "X-CSRF-Token": csrf},
    )


# -- auth -------------------------------------------------------------------------------------


def test_login_sets_protected_cookie_and_me_returns_user(client: TestClient, storage: Storage) -> None:
    make_user(storage)
    response = client.post(
        "/api/auth/login", json={"username": "analyst", "password": PASSWORD}, headers=ORIGIN
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["username"] == "analyst" and body["csrf_token"]
    assert "password" not in response.text and "argon2" not in response.text
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    me = client.get("/api/auth/me")
    assert me.status_code == 200 and me.json()["user"]["username"] == "analyst"


def test_login_failure_is_uniform(client: TestClient, storage: Storage) -> None:
    make_user(storage)
    wrong = client.post(
        "/api/auth/login", json={"username": "analyst", "password": "nope-nope"}, headers=ORIGIN
    )
    unknown = client.post(
        "/api/auth/login", json={"username": "ghost", "password": "nope-nope"}, headers=ORIGIN
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


def test_login_requires_allowed_origin_and_json(client: TestClient, storage: Storage) -> None:
    make_user(storage)
    payload = {"username": "analyst", "password": PASSWORD}
    assert client.post("/api/auth/login", json=payload).status_code == 403
    assert (
        client.post("/api/auth/login", json=payload, headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    form = client.post("/api/auth/login", data=payload, headers=ORIGIN)
    assert form.status_code == 415 and form.json()["error"]["code"] == "unsupported_media_type"


def test_logout_requires_csrf_and_revokes_session(client: TestClient, storage: Storage) -> None:
    make_user(storage)
    csrf = login(client)
    assert client.post("/api/auth/logout", headers=ORIGIN).status_code == 403
    response = client.post("/api/auth/logout", headers={**ORIGIN, "X-CSRF-Token": csrf})
    assert response.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_register_creates_account_and_signs_in(client: TestClient, storage: Storage) -> None:
    response = client.post(
        "/api/auth/register", json={"username": "new.analyst", "password": PASSWORD}, headers=ORIGIN
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["username"] == "new.analyst" and body["csrf_token"]
    assert "httponly" in response.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").json()["user"]["username"] == "new.analyst"
    user = storage.get_user_by_username("new.analyst")
    assert user is not None and user.password_hash != PASSWORD


def test_register_rejects_taken_username_and_weak_input(client: TestClient, storage: Storage) -> None:
    make_user(storage)
    taken = client.post(
        "/api/auth/register", json={"username": "ANALYST", "password": PASSWORD}, headers=ORIGIN
    )
    assert taken.status_code == 409 and taken.json()["error"]["code"] == "username_taken"
    for payload in (
        {"username": "ok-name", "password": "short"},
        {"username": "has space", "password": PASSWORD},
        {"username": "ab", "password": PASSWORD},
    ):
        response = client.post("/api/auth/register", json=payload, headers=ORIGIN)
        assert response.status_code == 422, payload
        assert payload["password"] not in response.text
    assert client.get("/api/auth/me").status_code == 401


def test_register_requires_origin_and_is_rate_limited(
    client: TestClient, storage: Storage, settings: Settings
) -> None:
    assert (
        client.post("/api/auth/register", json={"username": "someone", "password": PASSWORD}).status_code
        == 403
    )
    statuses = [
        client.post(
            "/api/auth/register", json={"username": f"user{i}", "password": PASSWORD}, headers=ORIGIN
        ).status_code
        for i in range(settings.rate_limit_register_per_hour + 1)
    ]
    assert statuses[:-1] == [200] * settings.rate_limit_register_per_hour
    assert statuses[-1] == 429


def test_unauthenticated_requests_get_401_envelope(client: TestClient) -> None:
    response = client.get("/api/conversations")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


# -- chat -------------------------------------------------------------------------------------


def test_chat_returns_full_envelope_and_retry_returns_saved_answer(
    client: TestClient, storage: Storage, agent: ScriptedAgent
) -> None:
    make_user(storage)
    csrf = login(client)
    request_id = str(uuid.uuid4())
    first = chat(client, csrf, "Compare LAX and SNA", request_id)
    assert first.status_code == 200
    body = first.json()
    assert set(body) == {"conversation_id", "answer", "results", "sources", "limitations"}
    assert body["results"] == [] and body["sources"] == []
    retry = chat(client, csrf, "Compare LAX and SNA", request_id)
    assert retry.status_code == 200 and retry.json() == body
    assert len(agent.calls) == 1
    detail = client.get(f"/api/conversations/{body['conversation_id']}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["request"]["status"] == "completed"
    assert detail["messages"][1]["response"] == body


def test_follow_up_receives_history_and_context(
    client: TestClient, storage: Storage, agent: ScriptedAgent
) -> None:
    make_user(storage)
    csrf = login(client)
    conversation_id = chat(client, csrf, "Compare LAX and SNA").json()["conversation_id"]
    follow_up = chat(client, csrf, "And the previous year?", conversation_id=conversation_id)
    assert follow_up.status_code == 200
    turn = agent.calls[-1]
    assert [item["role"] for item in turn.history] == ["user", "assistant"]
    assert turn.analysis_context == {
        "airports": [{"airport_id": "apt_lax", "code": "LAX", "name": "Los Angeles"}]
    }
    assert turn.context_data_version_id == "v1"


def test_chat_conflicts_and_not_found(client: TestClient, storage: Storage) -> None:
    make_user(storage)
    csrf = login(client)
    request_id = str(uuid.uuid4())
    chat(client, csrf, "first", request_id)
    mismatch = chat(client, csrf, "different", request_id)
    assert mismatch.status_code == 409 and mismatch.json()["error"]["code"] == "request_id_conflict"
    missing = chat(client, csrf, "hello", conversation_id="does-not-exist")
    assert missing.status_code == 404


def test_chat_requires_csrf_and_valid_body(client: TestClient, storage: Storage) -> None:
    make_user(storage)
    csrf = login(client)
    no_csrf = client.post("/api/chat", json={"request_id": str(uuid.uuid4()), "message": "x"}, headers=ORIGIN)
    assert no_csrf.status_code == 403
    blank = chat(client, csrf, "   ")
    assert blank.status_code == 422 and blank.json()["error"]["code"] == "invalid_request"
    bad_id = client.post(
        "/api/chat", json={"request_id": "nope", "message": "x"}, headers={**ORIGIN, "X-CSRF-Token": csrf}
    )
    assert bad_id.status_code == 422
    huge = chat(client, csrf, "x" * 70_000)
    assert huge.status_code == 413


def test_agent_failure_is_recorded_and_retry_does_not_duplicate_message(
    client: TestClient, storage: Storage, agent: ScriptedAgent
) -> None:
    make_user(storage)
    csrf = login(client)
    request_id = str(uuid.uuid4())
    agent.fail_with = AgentError("model_unavailable", 503, "The language model is temporarily unavailable.")
    failed = chat(client, csrf, "Rank New England", request_id)
    assert failed.status_code == 503
    error = failed.json()["error"]
    assert (
        error["code"] == "model_unavailable"
        and error["request_id"] == request_id
        and error["conversation_id"]
    )
    status = client.get(f"/api/chat/requests/{request_id}").json()
    assert (
        status["status"] == "failed"
        and status["response"] is None
        and status["error"]["code"] == "model_unavailable"
    )

    agent.fail_with = None
    retried = chat(client, csrf, "Rank New England", request_id)
    assert retried.status_code == 200
    messages = client.get(f"/api/conversations/{error['conversation_id']}").json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_stale_retry_after_new_message(client: TestClient, storage: Storage, agent: ScriptedAgent) -> None:
    make_user(storage)
    csrf = login(client)
    request_id = str(uuid.uuid4())
    agent.fail_with = AgentError("model_timeout", 504, "Timed out.")
    conversation_id = chat(client, csrf, "first", request_id).json()["error"]["conversation_id"]
    agent.fail_with = None
    assert chat(client, csrf, "second", conversation_id=conversation_id).status_code == 200
    stale = chat(client, csrf, "first", request_id)
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "stale_request"


def test_duplicate_while_processing_returns_202(client: TestClient, storage: Storage) -> None:
    owner = make_user(storage)
    csrf = login(client)
    request_id = str(uuid.uuid4())
    storage.accept_chat_request(
        owner_id=owner,
        request_id=request_id,
        message="slow question",
        sent_conversation_id=None,
        lease_seconds=300,
        now=utcnow(),
        max_active_per_user=2,
        max_active_total=8,
        max_attempts=3,
    )
    response = chat(client, csrf, "slow question", request_id)
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "processing" and body["status_url"] == f"/api/chat/requests/{request_id}"
    busy = chat(client, csrf, "another", conversation_id=body["conversation_id"])
    assert busy.status_code == 409 and busy.json()["error"]["code"] == "conversation_busy"


def test_expired_processing_request_reads_as_interrupted(client: TestClient, storage: Storage) -> None:
    owner = make_user(storage)
    login(client)
    request_id = str(uuid.uuid4())
    storage.accept_chat_request(
        owner_id=owner,
        request_id=request_id,
        message="question",
        sent_conversation_id=None,
        lease_seconds=60,
        now=utcnow() - timedelta(hours=1),
        max_active_per_user=2,
        max_active_total=8,
        max_attempts=3,
    )
    status = client.get(f"/api/chat/requests/{request_id}")
    assert status.status_code == 200 and status.json()["status"] == "interrupted"


def test_private_data_of_other_users_is_404(client: TestClient, storage: Storage) -> None:
    make_user(storage, "alice")
    make_user(storage, "bob")
    alice_csrf = login(client, "alice")
    request_id = str(uuid.uuid4())
    conversation_id = chat(client, alice_csrf, "Alice's question", request_id).json()["conversation_id"]
    login(client, "bob")
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 404
    assert client.get(f"/api/chat/requests/{request_id}").status_code == 404
    assert client.get("/api/conversations").json() == {"conversations": []}


# -- refresh, health, misc --------------------------------------------------------------------


def test_refresh_returns_single_active_job(
    client: TestClient, storage: Storage, submitted: list[str]
) -> None:
    make_user(storage)
    csrf = login(client)
    headers = {**ORIGIN, "X-CSRF-Token": csrf}
    first = client.post("/api/data/refresh", headers=headers)
    second = client.post("/api/data/refresh", headers=headers)
    assert first.status_code == second.status_code == 202
    job_id = first.json()["job_id"]
    assert second.json()["job_id"] == job_id and submitted == [job_id]
    status = client.get(f"/api/data/refresh/{job_id}").json()
    assert status["status"] == "queued" and status["status_url"] == f"/api/data/refresh/{job_id}"
    assert client.get("/api/data/refresh/unknown").status_code == 404


def test_health_unknown_routes_and_security_headers(client: TestClient) -> None:
    health = client.get("/api/health")
    assert health.status_code == 200 and health.json()["status"] == "ok"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert "frame-ancestors 'none'" in health.headers["content-security-policy"]
    assert health.headers["cache-control"] == "no-store"
    missing = client.get("/api/nothing-here")
    assert missing.status_code == 404 and missing.json()["error"]["code"] == "not_found"
    assert client.get("/api/docs").status_code == 404
