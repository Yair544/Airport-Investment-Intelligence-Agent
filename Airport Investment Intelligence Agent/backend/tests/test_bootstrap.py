"""Starter data privacy, first-run usability, restart persistence and interrupted imports."""

from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.bootstrap import DEMO_PASSWORD, DEMO_USERNAME, bootstrap
from app.config import Settings
from app.data.resolution import load_reference_catalog
from app.data.service import SqliteDataService
from app.main import create_app
from app.security import hash_password, verify_password
from app.storage import Storage, utcnow
from tests.test_data_repository import versions as versions


@pytest.fixture
def starter(storage: Storage, versions: dict[str, str], tmp_path: Path) -> Path:
    directory = tmp_path / "bundle"
    directory.mkdir()
    database = directory / "public.db"
    metadata = storage.export_seed(database)
    payload = database.read_bytes()
    (directory / "aviation.db.gz").write_bytes(gzip.compress(payload))
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                **metadata,
                "database_bytes": len(payload),
                "database_sha256": hashlib.sha256(payload).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    return directory


@pytest.fixture
def target(tmp_path: Path) -> Storage:
    result = Storage(tmp_path / "installation" / "app.db")
    result.initialize()
    return result


def add_conversation(storage: Storage, username: str) -> str:
    user = storage.get_user_by_username(username)
    assert user is not None
    outcome = storage.accept_chat_request(
        owner_id=user.user_id,
        request_id=str(uuid.uuid4()),
        message="Private discussion: do not distribute this text",
        sent_conversation_id=None,
        lease_seconds=150,
        now=utcnow(),
        max_active_per_user=2,
        max_active_total=16,
        max_attempts=3,
    )
    return outcome.request.conversation_id


def test_export_contains_only_active_public_data(
    storage: Storage, versions: dict[str, str], tmp_path: Path
) -> None:
    storage.create_user("private-account-never-distribute", hash_password("private-password"))
    add_conversation(storage, "private-account-never-distribute")
    database = tmp_path / "clean.db"
    metadata = storage.export_seed(database)
    clean = Storage(database)
    assert metadata["version_id"] == clean.get_active_data_version_id() == "v2"
    with clean.connection() as conn:
        for table in (
            "users",
            "sessions",
            "conversations",
            "messages",
            "chat_requests",
            "analysis_context",
            "refresh_jobs",
        ):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM data_versions").fetchone()[0] == 1
        assert (
            conn.execute("SELECT 1 FROM data_files WHERE file_id = ?", (versions["t100_a"],)).fetchone()
            is None
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None
    assert b"private-account-never-distribute" not in database.read_bytes()
    assert b"Private discussion" not in database.read_bytes()


def test_first_run_provides_real_queries_and_demo_login(starter: Path, target: Storage) -> None:
    bootstrap(target, starter)
    user = target.get_user_by_username(DEMO_USERNAME)
    assert user is not None and verify_password(user.password_hash, DEMO_PASSWORD)
    repository = SqliteDataService(target, load_reference_catalog()).repository()
    rows = repository.t100_segments([10299], 2025, [1])
    assert rows.loc[rows["dest"] == "SEA", "departures_performed"].tolist() == [60.0]
    assert not list(target.database_path.parent.glob("starter-*"))


def test_fresh_restore_never_replaces_an_existing_database(starter: Path, target: Storage) -> None:
    owner_id = target.create_user("owner", hash_password("owner-password"))
    assert not target.restore_seed_if_missing(starter / "public.db")
    owner = target.get_user_by_username("owner")
    assert owner is not None and owner.user_id == owner_id
    assert target.get_active_data_version_id() is None


def test_fresh_restore_rejects_a_snapshot_with_private_records(starter: Path, tmp_path: Path) -> None:
    source = Storage(starter / "public.db")
    source.create_user("must-not-be-shipped", hash_password("private-password"))
    destination = tmp_path / "must-not-be-created.db"
    with pytest.raises(ValueError, match="private data"):
        Storage(destination).restore_seed_if_missing(source.database_path)
    assert not destination.exists()


def test_restarts_preserve_password_account_state_history_and_data(starter: Path, target: Storage) -> None:
    bootstrap(target, starter)
    user = target.get_user_by_username(DEMO_USERNAME)
    assert user is not None
    conversation_id = add_conversation(target, DEMO_USERNAME)
    target.update_password(user.user_id, hash_password("my-changed-password"))
    target.set_user_active(user.user_id, False)
    # No archive access or network is needed after a successful initialization.
    bootstrap(target, starter / "does-not-exist")
    current = target.get_user_by_username(DEMO_USERNAME)
    assert current is not None and current.user_id == user.user_id and not current.is_active
    assert verify_password(current.password_hash, "my-changed-password")
    assert target.get_conversation(user.user_id, conversation_id) is not None
    with target.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM t100_segments").fetchone()[0] == 4


def test_existing_accounts_survive_data_initialization(starter: Path, target: Storage) -> None:
    target.create_user("owner", hash_password("owner-password"))
    conversation_id = add_conversation(target, "owner")
    bootstrap(target, starter)
    owner = target.get_user_by_username("owner")
    assert owner is not None and verify_password(owner.password_hash, "owner-password")
    assert target.get_conversation(owner.user_id, conversation_id) is not None
    assert target.get_active_data_version_id() == "v2"


@pytest.mark.parametrize("damage", ["checksum", "truncated"])
def test_damaged_archive_leaves_live_data_untouched_and_retry_works(
    starter: Path, target: Storage, damage: str
) -> None:
    archive = starter / "aviation.db.gz"
    original = archive.read_bytes()
    if damage == "truncated":
        archive.write_bytes(original[:20])
    else:
        archive.write_bytes(gzip.compress(b"wrong database"))
    with pytest.raises((ValueError, EOFError, OSError)):
        bootstrap(target, starter)
    assert target.get_active_data_version_id() is None
    assert target.get_user_by_username(DEMO_USERNAME) is None
    assert not list(target.database_path.parent.glob("starter-*"))
    archive.write_bytes(original)
    bootstrap(target, starter)
    assert target.get_active_data_version_id() == "v2"


def test_failed_import_rolls_back_and_can_be_retried(starter: Path, target: Storage) -> None:
    with target.connection() as conn:
        conn.execute(
            "CREATE TRIGGER interrupt_import BEFORE INSERT ON taf_annual "
            "BEGIN SELECT RAISE(ABORT, 'simulated interruption'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated interruption"):
        bootstrap(target, starter)
    with target.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM data_files").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM data_versions").fetchone()[0] == 0
        conn.execute("DROP TRIGGER interrupt_import")
    bootstrap(target, starter)
    assert target.get_active_data_version_id() == "v2"


def test_startup_is_ready_for_login_and_preserves_session_after_restart(
    starter: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.bootstrap

    monkeypatch.setattr(app.bootstrap, "bootstrap", lambda storage: bootstrap(storage, starter))
    settings = settings.model_copy(
        update={"auto_bootstrap": True, "data_dir": starter.parent / "client-data"}
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json()["data_version_id"] == "v2"
        response = client.post(
            "/api/auth/login",
            json={"username": DEMO_USERNAME, "password": DEMO_PASSWORD},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 200
        cookies = client.cookies
    with TestClient(create_app(settings)) as restarted:
        restarted.cookies.update(cookies)
        assert restarted.get("/api/auth/me").status_code == 200
        assert restarted.get("/api/health").json()["data_version_id"] == "v2"
