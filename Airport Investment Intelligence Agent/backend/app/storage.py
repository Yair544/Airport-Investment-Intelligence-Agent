"""SQLite persistence: schema, transactions and every database query in the application.

Conventions:
- One short-lived connection per operation (safe across FastAPI worker threads).
- Writes that must be atomic run inside `Storage.transaction()`, which issues BEGIN IMMEDIATE.
- Timestamps are stored as ISO 8601 UTC strings with a fixed format so they compare as text.
- Every query on private data filters by the authenticated owner.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Literal

SCHEMA_VERSION = "1"

# Only these public-data tables may cross into the distributable starter snapshot.
SEED_TABLES = (
    "data_files",
    "data_versions",
    "data_version_files",
    "national_references",
    "taf_airports",
    "taf_annual",
    "t100_segments",
    "otp_departures_month",
    "otp_arrivals_month",
    "otp_taxi_hist",
    "t100_summary_month",
    "active_data_version",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Accounts and sessions -------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

-- Conversations, messages, requests and analysis context ----------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    conversation_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL REFERENCES users(user_id),
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_owner ON conversations(owner_id, updated_at);

CREATE TABLE IF NOT EXISTS chat_requests (
    owner_id TEXT NOT NULL REFERENCES users(user_id),
    request_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    sent_conversation_id TEXT,
    content_hash TEXT NOT NULL,
    message TEXT NOT NULL,
    seq INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'completed', 'failed', 'interrupted')),
    attempt_id TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TEXT,
    error_json TEXT,
    response_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (owner_id, request_id),
    UNIQUE (conversation_id, seq)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_requests_one_active
    ON chat_requests(conversation_id) WHERE status = 'processing';
CREATE INDEX IF NOT EXISTS idx_chat_requests_status ON chat_requests(status, lease_expires_at);

CREATE TABLE IF NOT EXISTS messages (
    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id),
    seq INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    request_id TEXT NOT NULL,
    response_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, seq),
    UNIQUE (conversation_id, request_id, role)
);

CREATE TABLE IF NOT EXISTS analysis_context (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(conversation_id),
    owner_id TEXT NOT NULL REFERENCES users(user_id),
    context_json TEXT NOT NULL,
    data_version_id TEXT,
    rules_version TEXT,
    last_request_id TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Data refresh jobs --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS refresh_jobs (
    job_id TEXT PRIMARY KEY,
    singleton INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL
        CHECK (status IN ('queued', 'running', 'completed', 'failed', 'interrupted')),
    stage TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    updated_at TEXT NOT NULL,
    sources_json TEXT,
    changed INTEGER,
    version_id TEXT,
    error_json TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_refresh_jobs_one_active
    ON refresh_jobs(singleton) WHERE status IN ('queued', 'running');

-- Aviation data: files, versions and national references -----------------------------------
CREATE TABLE IF NOT EXISTS data_files (
    file_id TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    period_key TEXT NOT NULL,
    url TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    local_path TEXT NOT NULL,
    dataset_version TEXT,
    retrieved_at TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    row_count INTEGER,
    status TEXT NOT NULL CHECK (status IN ('ingesting', 'ready', 'failed')),
    remote_fingerprint TEXT,
    notes_json TEXT,
    UNIQUE (source_id, period_key, sha256)
);

CREATE TABLE IF NOT EXISTS data_versions (
    version_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'active', 'superseded', 'failed')),
    job_id TEXT,
    created_at TEXT NOT NULL,
    activated_at TEXT,
    manifest_json TEXT NOT NULL,
    coverage_json TEXT
);

CREATE TABLE IF NOT EXISTS data_version_files (
    version_id TEXT NOT NULL REFERENCES data_versions(version_id),
    file_id TEXT NOT NULL REFERENCES data_files(file_id),
    PRIMARY KEY (version_id, file_id)
);
CREATE INDEX IF NOT EXISTS idx_data_version_files_file ON data_version_files(file_id);

CREATE TABLE IF NOT EXISTS active_data_version (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version_id TEXT NOT NULL REFERENCES data_versions(version_id),
    activated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS national_references (
    version_id TEXT NOT NULL REFERENCES data_versions(version_id),
    metric TEXT NOT NULL,
    period_key TEXT NOT NULL,
    value REAL,
    sample_size INTEGER,
    details_json TEXT,
    PRIMARY KEY (version_id, metric, period_key)
);

-- Aviation data: normalized rows, always scoped to the source file they came from -----------
CREATE TABLE IF NOT EXISTS taf_airports (
    file_id TEXT NOT NULL REFERENCES data_files(file_id),
    faa_locid TEXT NOT NULL,
    name TEXT,
    city TEXT,
    state TEXT,
    faa_region TEXT,
    hub_size INTEGER,
    PRIMARY KEY (file_id, faa_locid)
);

CREATE TABLE IF NOT EXISTS taf_annual (
    file_id TEXT NOT NULL REFERENCES data_files(file_id),
    faa_locid TEXT NOT NULL,
    fiscal_year INTEGER NOT NULL,
    is_forecast INTEGER NOT NULL,
    enpl_air_carrier REAL,
    enpl_air_taxi REAL,
    enpl_commuter REAL,
    enpl_intl_us_flag REAL,
    enpl_intl_foreign_flag REAL,
    total_enplanements REAL,
    ops_itinerant_air_carrier REAL,
    ops_itinerant_air_taxi REAL,
    commercial_operations REAL,
    PRIMARY KEY (file_id, faa_locid, fiscal_year)
);

CREATE TABLE IF NOT EXISTS t100_segments (
    file_id TEXT NOT NULL REFERENCES data_files(file_id),
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    carrier TEXT,
    origin_airport_id INTEGER,
    origin TEXT,
    origin_country TEXT,
    origin_state TEXT,
    dest_airport_id INTEGER,
    dest TEXT,
    dest_country TEXT,
    dest_state TEXT,
    dest_city_name TEXT,
    service_class TEXT,
    aircraft_config INTEGER,
    aircraft_type INTEGER,
    data_source TEXT,
    departures_scheduled REAL,
    departures_performed REAL,
    seats REAL,
    passengers REAL,
    freight REAL,
    distance REAL,
    distance_group INTEGER
);
CREATE INDEX IF NOT EXISTS idx_t100_origin ON t100_segments(origin_airport_id, year, month);
CREATE INDEX IF NOT EXISTS idx_t100_file ON t100_segments(file_id, year, month);

CREATE TABLE IF NOT EXISTS otp_departures_month (
    file_id TEXT NOT NULL REFERENCES data_files(file_id),
    airport_id INTEGER NOT NULL,
    code TEXT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    days_in_month INTEGER NOT NULL,
    scheduled INTEGER NOT NULL,
    cancelled INTEGER NOT NULL,
    diverted INTEGER NOT NULL,
    del15_valid INTEGER NOT NULL,
    del15_count INTEGER NOT NULL,
    PRIMARY KEY (file_id, airport_id, year, month)
);

CREATE TABLE IF NOT EXISTS otp_arrivals_month (
    file_id TEXT NOT NULL REFERENCES data_files(file_id),
    airport_id INTEGER NOT NULL,
    code TEXT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    arrivals INTEGER NOT NULL,
    delay_carrier REAL,
    delay_weather REAL,
    delay_nas REAL,
    delay_security REAL,
    delay_late_aircraft REAL,
    PRIMARY KEY (file_id, airport_id, year, month)
);

CREATE TABLE IF NOT EXISTS otp_taxi_hist (
    file_id TEXT NOT NULL REFERENCES data_files(file_id),
    airport_id INTEGER NOT NULL,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('taxi_out', 'taxi_in')),
    minutes INTEGER NOT NULL,
    flights INTEGER NOT NULL,
    PRIMARY KEY (file_id, airport_id, year, month, kind, minutes)
);

CREATE TABLE IF NOT EXISTS t100_summary_month (
    file_id TEXT NOT NULL REFERENCES data_files(file_id),
    airport_id INTEGER NOT NULL,
    code TEXT,
    year INTEGER NOT NULL,
    month INTEGER NOT NULL,
    departures REAL,
    passengers REAL,
    seats REAL,
    PRIMARY KEY (file_id, airport_id, year, month)
);
"""

ChatStatus = Literal["processing", "completed", "failed", "interrupted"]
JobStatus = Literal["queued", "running", "completed", "failed", "interrupted"]


# ---------------------------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def from_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)


def new_id() -> str:
    return str(uuid.uuid4())


def _dump(value: Any) -> str | None:
    return None if value is None else json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load(value: str | None) -> Any:
    return None if value is None else json.loads(value)


# ---------------------------------------------------------------------------------------------
# Errors and records
# ---------------------------------------------------------------------------------------------


class StorageError(Exception):
    """Base class for expected storage outcomes that the API maps to HTTP errors."""

    def __init__(self, code: str, message: str = "", conversation_id: str | None = None) -> None:
        super().__init__(message or code)
        self.code = code
        self.conversation_id = conversation_id


class ConflictError(StorageError):
    """`request_id_conflict`, `conversation_busy`, `stale_request` or `username_taken`."""


class NotFoundError(StorageError):
    """A private resource does not exist for this owner."""


class LimitExceededError(StorageError):
    """Too many active requests for the user or for the whole system."""


@dataclass(frozen=True)
class UserRecord:
    user_id: str
    username: str
    password_hash: str
    is_active: bool


@dataclass(frozen=True)
class SessionRecord:
    session_hash: str
    user_id: str
    username: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True)
class ConversationRecord:
    conversation_id: str
    owner_id: str
    title: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class ChatRequestRecord:
    owner_id: str
    request_id: str
    conversation_id: str
    sent_conversation_id: str | None
    message: str
    seq: int
    status: ChatStatus
    attempt_id: str | None
    attempt_count: int
    lease_expires_at: datetime | None
    error: dict[str, Any] | None
    response: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MessageRecord:
    message_id: int
    conversation_id: str
    seq: int
    role: Literal["user", "assistant"]
    content: str
    request_id: str
    response: dict[str, Any] | None
    created_at: datetime
    request_status: ChatStatus | None  # for user messages
    sent_conversation_id: str | None  # for user messages


@dataclass(frozen=True)
class AnalysisContextRecord:
    conversation_id: str
    context: dict[str, Any]
    data_version_id: str | None
    rules_version: str | None
    last_request_id: str
    updated_at: datetime


@dataclass(frozen=True)
class AcceptOutcome:
    kind: Literal["started", "completed", "in_progress"]
    request: ChatRequestRecord
    created_conversation: bool = False


@dataclass(frozen=True)
class RefreshJobRecord:
    job_id: str
    status: JobStatus
    stage: str | None
    lease_owner: str | None
    lease_expires_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    updated_at: datetime
    sources: list[dict[str, Any]]
    changed: bool | None
    version_id: str | None
    error: dict[str, Any] | None


def content_hash(message: str, sent_conversation_id: str | None) -> str:
    payload = json.dumps(
        {"message": message, "conversation_id": sent_conversation_id},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _chat_request(row: sqlite3.Row) -> ChatRequestRecord:
    return ChatRequestRecord(
        owner_id=row["owner_id"],
        request_id=row["request_id"],
        conversation_id=row["conversation_id"],
        sent_conversation_id=row["sent_conversation_id"],
        message=row["message"],
        seq=row["seq"],
        status=row["status"],
        attempt_id=row["attempt_id"],
        attempt_count=row["attempt_count"],
        lease_expires_at=from_iso(row["lease_expires_at"]),
        error=_load(row["error_json"]),
        response=_load(row["response_json"]),
        created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
        updated_at=from_iso(row["updated_at"]),  # type: ignore[arg-type]
    )


def _refresh_job(row: sqlite3.Row) -> RefreshJobRecord:
    return RefreshJobRecord(
        job_id=row["job_id"],
        status=row["status"],
        stage=row["stage"],
        lease_owner=row["lease_owner"],
        lease_expires_at=from_iso(row["lease_expires_at"]),
        created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
        started_at=from_iso(row["started_at"]),
        finished_at=from_iso(row["finished_at"]),
        updated_at=from_iso(row["updated_at"]),  # type: ignore[arg-type]
        sources=_load(row["sources_json"]) or [],
        changed=None if row["changed"] is None else bool(row["changed"]),
        version_id=row["version_id"],
        error=_load(row["error_json"]),
    )


INTERRUPTED_ERROR = {
    "code": "interrupted",
    "message": "Processing stopped before an answer was saved. You can retry the same request.",
}


class Storage:
    """Owns the SQLite file. Create one instance per process and share it."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    # -- connections and schema ---------------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.database_path, timeout=10.0, isolation_level=None, check_same_thread=False, uri=True
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE ... COMMIT; rolls back on any exception."""
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def initialize(self) -> None:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO schema_meta(key, value) VALUES ('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (SCHEMA_VERSION,),
            )

    def backup(self, destination: Path) -> None:
        """Consistent online copy using the SQLite backup API."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()

    def export_seed(self, destination: Path) -> dict[str, Any]:
        """Build a new database from an allowlist, never by copying private database pages."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            raise FileExistsError(destination)
        with closing(sqlite3.connect(destination, uri=True)) as conn:
            conn.executescript(SCHEMA)
            conn.execute("ATTACH DATABASE ? AS source", (self.database_path.resolve().as_uri() + "?mode=ro",))
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT version_id FROM source.active_data_version WHERE singleton = 1"
            ).fetchone()
            if row is None:
                raise ValueError("Load aviation data before building the starter snapshot.")
            version_id = str(row[0])
            conn.execute("INSERT INTO schema_meta VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
            for table in SEED_TABLES:
                if table in (
                    "data_versions",
                    "data_version_files",
                    "national_references",
                    "active_data_version",
                ):
                    condition = "version_id = ?"
                else:
                    condition = (
                        "file_id IN (SELECT file_id FROM source.data_version_files WHERE version_id = ?)"
                    )
                conn.execute(
                    f"INSERT INTO main.{table} SELECT * FROM source.{table} WHERE {condition}", (version_id,)
                )
            # Refresh execution records are local; retain only the public provenance in the manifest.
            manifest_row = conn.execute(
                "SELECT manifest_json, coverage_json FROM data_versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            manifest = json.loads(manifest_row[0])
            manifest.pop("job_id", None)
            conn.execute(
                "UPDATE data_versions SET job_id = NULL, manifest_json = ? WHERE version_id = ?",
                (_dump(manifest), version_id),
            )
            conn.commit()
            if conn.execute("PRAGMA foreign_key_check").fetchone():
                raise ValueError("Starter snapshot has inconsistent foreign keys.")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Starter snapshot failed its integrity check.")
            return {"version_id": version_id, "coverage": json.loads(manifest_row[1])}

    @staticmethod
    def _validate_seed(conn: sqlite3.Connection, namespace: str) -> None:
        # Namespace is internal, never a filename or a value supplied by the caller.
        if namespace not in ("main", "seed"):
            raise ValueError("Invalid snapshot namespace.")
        schema = conn.execute(
            f"SELECT value FROM {namespace}.schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if schema is None or schema[0] != SCHEMA_VERSION:
            raise ValueError("Starter snapshot schema does not match this application.")
        if conn.execute(f"PRAGMA {namespace}.integrity_check").fetchone()[0] != "ok":
            raise ValueError("Starter snapshot failed its integrity check.")
        if conn.execute(f"PRAGMA {namespace}.foreign_key_check").fetchone():
            raise ValueError("Starter snapshot has inconsistent foreign keys.")
        version = conn.execute(
            f"SELECT a.version_id FROM {namespace}.active_data_version a "
            f"JOIN {namespace}.data_versions v ON v.version_id = a.version_id "
            "WHERE a.singleton = 1 AND v.status = 'active'"
        ).fetchone()
        if version is None:
            raise ValueError("Starter snapshot has no active aviation data.")
        for table in (
            "users",
            "sessions",
            "conversations",
            "messages",
            "chat_requests",
            "analysis_context",
            "refresh_jobs",
        ):
            if conn.execute(f"SELECT 1 FROM {namespace}.{table} LIMIT 1").fetchone():
                raise ValueError("Starter snapshots must not contain private data.")

    def restore_seed_if_missing(self, source: Path) -> bool:
        """Publish a fully validated fresh database atomically, without overwriting any existing file.

        Validate on local temporary storage, then copy sequentially before publishing with a hard link.
        """
        with closing(sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
            self._validate_seed(conn, "main")
        with TemporaryDirectory(prefix="starter-install-", dir=self.database_path.parent) as directory:
            prepared = Path(directory) / "app.db"
            shutil.copyfile(source, prepared)
            try:
                os.link(prepared, self.database_path)
            except FileExistsError:
                return False
        return True

    def import_seed(self, source: Path) -> bool:
        """Atomically install public starter data; preserve all accounts, history and active data."""
        with self.connection() as conn:
            conn.execute("ATTACH DATABASE ? AS seed", (source.resolve().as_uri() + "?mode=ro",))
            self._validate_seed(conn, "seed")
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM main.active_data_version WHERE singleton = 1").fetchone():
                    conn.execute("ROLLBACK")
                    return False
                # Remember existing complete files so unkeyed segment rows are never duplicated.
                conn.execute(
                    "CREATE TEMP TABLE seed_files AS SELECT file_id FROM seed.data_files "
                    "WHERE file_id NOT IN (SELECT file_id FROM main.data_files WHERE status = 'ready')"
                )
                for table in SEED_TABLES:
                    if table in _FILE_ROW_TABLES:
                        conn.execute(
                            f"DELETE FROM main.{table} WHERE file_id IN (SELECT file_id FROM seed_files)"
                        )
                        conn.execute(
                            f"INSERT INTO main.{table} SELECT * FROM seed.{table} "
                            "WHERE file_id IN (SELECT file_id FROM seed_files)"
                        )
                    elif table == "data_files":
                        columns = [row[1] for row in conn.execute("PRAGMA main.table_info(data_files)")]
                        updates = ", ".join(
                            f"{column}=excluded.{column}" for column in columns if column != "file_id"
                        )
                        conn.execute(
                            "INSERT INTO main.data_files SELECT * FROM seed.data_files WHERE true "
                            f"ON CONFLICT(file_id) DO UPDATE SET {updates}"
                        )
                    else:
                        conn.execute(f"INSERT OR IGNORE INTO main.{table} SELECT * FROM seed.{table}")
                if conn.execute("PRAGMA main.foreign_key_check").fetchone():
                    raise ValueError("Starter data could not be installed consistently.")
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        return True

    # -- users ----------------------------------------------------------------------------------

    def create_user(self, username: str, password_hash: str) -> str:
        user_id = new_id()
        now = to_iso(utcnow())
        try:
            with self.transaction() as conn:
                conn.execute(
                    "INSERT INTO users(user_id, username, password_hash, is_active, created_at, "
                    "updated_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (user_id, username, password_hash, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("username_taken", "A user with this username already exists.") from exc
        return user_id

    def get_user_by_username(self, username: str) -> UserRecord | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return None if row is None else self._user(row)

    def get_user(self, user_id: str) -> UserRecord | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return None if row is None else self._user(row)

    def update_password(self, user_id: str, password_hash: str, *, revoke_sessions: bool = True) -> None:
        """Store a new hash. A reset revokes existing sessions; a transparent rehash at login does not."""
        now = to_iso(utcnow())
        with self.transaction() as conn:
            conn.execute(
                "UPDATE users SET password_hash = ?, updated_at = ? WHERE user_id = ?",
                (password_hash, now, user_id),
            )
            if revoke_sessions:
                conn.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (now, user_id),
                )

    def set_user_active(self, user_id: str, active: bool) -> None:
        now = to_iso(utcnow())
        with self.transaction() as conn:
            conn.execute(
                "UPDATE users SET is_active = ?, updated_at = ? WHERE user_id = ?",
                (1 if active else 0, now, user_id),
            )
            if not active:
                conn.execute(
                    "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
                    (now, user_id),
                )

    @staticmethod
    def _user(row: sqlite3.Row) -> UserRecord:
        return UserRecord(
            user_id=row["user_id"],
            username=row["username"],
            password_hash=row["password_hash"],
            is_active=bool(row["is_active"]),
        )

    # -- sessions -------------------------------------------------------------------------------

    def create_session(self, user_id: str, session_hash: str, csrf_token: str, expires_at: datetime) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO sessions(session_hash, user_id, csrf_token, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (session_hash, user_id, csrf_token, to_iso(utcnow()), to_iso(expires_at)),
            )

    def get_active_session(self, session_hash: str, now: datetime) -> SessionRecord | None:
        """Session that is not revoked, not expired and belongs to an active user."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT s.session_hash, s.user_id, s.csrf_token, s.expires_at, u.username "
                "FROM sessions s JOIN users u ON u.user_id = s.user_id "
                "WHERE s.session_hash = ? AND s.revoked_at IS NULL AND s.expires_at > ? "
                "AND u.is_active = 1",
                (session_hash, to_iso(now)),
            ).fetchone()
        if row is None:
            return None
        return SessionRecord(
            session_hash=row["session_hash"],
            user_id=row["user_id"],
            username=row["username"],
            csrf_token=row["csrf_token"],
            expires_at=from_iso(row["expires_at"]),  # type: ignore[arg-type]
        )

    def revoke_session(self, session_hash: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "UPDATE sessions SET revoked_at = ? WHERE session_hash = ? AND revoked_at IS NULL",
                (to_iso(utcnow()), session_hash),
            )

    def delete_expired_sessions(self, now: datetime) -> int:
        with self.transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM sessions WHERE expires_at <= ? OR revoked_at IS NOT NULL",
                (to_iso(now - timedelta(days=1)),),
            )
        return cursor.rowcount

    # -- chat requests --------------------------------------------------------------------------

    def accept_chat_request(
        self,
        *,
        owner_id: str,
        request_id: str,
        message: str,
        sent_conversation_id: str | None,
        lease_seconds: float,
        now: datetime,
        max_active_per_user: int,
        max_active_total: int,
        max_attempts: int,
    ) -> AcceptOutcome:
        """Register a new request or a retry atomically (spec sections 6 and 8).

        Returns `started` with a fresh attempt id when the caller must process the request,
        `completed` with the stored response, or `in_progress` when another attempt holds a
        valid lease. Raises ConflictError, NotFoundError or LimitExceededError.
        """
        now_iso = to_iso(now)
        lease_iso = to_iso(now + timedelta(seconds=lease_seconds))
        digest = content_hash(message, sent_conversation_id)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM chat_requests WHERE owner_id = ? AND request_id = ?",
                (owner_id, request_id),
            ).fetchone()
            if row is not None:
                existing = _chat_request(row)
                if row["content_hash"] != digest:
                    raise ConflictError(
                        "request_id_conflict",
                        "This request id was already used with different content.",
                    )
                if existing.status == "completed":
                    return AcceptOutcome("completed", existing)
                if existing.status == "processing":
                    if row["lease_expires_at"] is not None and row["lease_expires_at"] > now_iso:
                        return AcceptOutcome("in_progress", existing)
                    self._interrupt(conn, owner_id, request_id, now_iso)
                later = conn.execute(
                    "SELECT 1 FROM chat_requests WHERE conversation_id = ? AND seq > ? LIMIT 1",
                    (existing.conversation_id, existing.seq),
                ).fetchone()
                if later is not None:
                    raise ConflictError(
                        "stale_request",
                        "The conversation has moved on since this request. Send a new message.",
                        conversation_id=existing.conversation_id,
                    )
                if existing.attempt_count >= max_attempts:
                    raise LimitExceededError(
                        "attempt_limit_reached",
                        "This request reached its retry limit. Send it again as a new message.",
                    )
                self._ensure_conversation_idle(conn, existing.conversation_id, now_iso)
                self._ensure_capacity(conn, owner_id, now_iso, max_active_per_user, max_active_total)
                attempt_id = new_id()
                conn.execute(
                    "UPDATE chat_requests SET status = 'processing', attempt_id = ?, "
                    "attempt_count = attempt_count + 1, lease_expires_at = ?, error_json = NULL, "
                    "updated_at = ? WHERE owner_id = ? AND request_id = ?",
                    (attempt_id, lease_iso, now_iso, owner_id, request_id),
                )
                updated = conn.execute(
                    "SELECT * FROM chat_requests WHERE owner_id = ? AND request_id = ?",
                    (owner_id, request_id),
                ).fetchone()
                return AcceptOutcome("started", _chat_request(updated))

            created = False
            if sent_conversation_id is not None:
                conversation = conn.execute(
                    "SELECT conversation_id FROM conversations WHERE conversation_id = ? AND owner_id = ?",
                    (sent_conversation_id, owner_id),
                ).fetchone()
                if conversation is None:
                    raise NotFoundError("conversation_not_found", "Conversation not found.")
                conversation_id = sent_conversation_id
                self._ensure_conversation_idle(conn, conversation_id, now_iso)
            else:
                conversation_id = new_id()
                created = True
            self._ensure_capacity(conn, owner_id, now_iso, max_active_per_user, max_active_total)
            if created:
                conn.execute(
                    "INSERT INTO conversations(conversation_id, owner_id, title, created_at, "
                    "updated_at) VALUES (?, ?, ?, ?, ?)",
                    (conversation_id, owner_id, _title(message), now_iso, now_iso),
                )
            request_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM chat_requests WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            message_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO messages(conversation_id, seq, role, content, request_id, created_at) "
                "VALUES (?, ?, 'user', ?, ?, ?)",
                (conversation_id, message_seq, message, request_id, now_iso),
            )
            attempt_id = new_id()
            conn.execute(
                "INSERT INTO chat_requests(owner_id, request_id, conversation_id, "
                "sent_conversation_id, content_hash, message, seq, status, attempt_id, "
                "attempt_count, lease_expires_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', ?, 1, ?, ?, ?)",
                (
                    owner_id,
                    request_id,
                    conversation_id,
                    sent_conversation_id,
                    digest,
                    message,
                    request_seq,
                    attempt_id,
                    lease_iso,
                    now_iso,
                    now_iso,
                ),
            )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now_iso, conversation_id),
            )
            inserted = conn.execute(
                "SELECT * FROM chat_requests WHERE owner_id = ? AND request_id = ?",
                (owner_id, request_id),
            ).fetchone()
            return AcceptOutcome("started", _chat_request(inserted), created_conversation=created)

    def _ensure_conversation_idle(self, conn: sqlite3.Connection, conversation_id: str, now_iso: str) -> None:
        active = conn.execute(
            "SELECT owner_id, request_id, lease_expires_at FROM chat_requests "
            "WHERE conversation_id = ? AND status = 'processing'",
            (conversation_id,),
        ).fetchone()
        if active is None:
            return
        if active["lease_expires_at"] is not None and active["lease_expires_at"] > now_iso:
            raise ConflictError(
                "conversation_busy",
                "Another request in this conversation is still processing.",
                conversation_id=conversation_id,
            )
        self._interrupt(conn, active["owner_id"], active["request_id"], now_iso)

    @staticmethod
    def _ensure_capacity(
        conn: sqlite3.Connection,
        owner_id: str,
        now_iso: str,
        max_active_per_user: int,
        max_active_total: int,
    ) -> None:
        user_active = conn.execute(
            "SELECT COUNT(*) FROM chat_requests WHERE owner_id = ? AND status = 'processing' "
            "AND lease_expires_at > ?",
            (owner_id, now_iso),
        ).fetchone()[0]
        if user_active >= max_active_per_user:
            raise LimitExceededError(
                "too_many_active_requests", "Too many requests are processing for this account."
            )
        total_active = conn.execute(
            "SELECT COUNT(*) FROM chat_requests WHERE status = 'processing' AND lease_expires_at > ?",
            (now_iso,),
        ).fetchone()[0]
        if total_active >= max_active_total:
            raise LimitExceededError("system_busy", "The service is busy. Please retry shortly.")

    @staticmethod
    def _interrupt(conn: sqlite3.Connection, owner_id: str, request_id: str, now_iso: str) -> None:
        conn.execute(
            "UPDATE chat_requests SET status = 'interrupted', attempt_id = NULL, "
            "lease_expires_at = NULL, error_json = ?, updated_at = ? "
            "WHERE owner_id = ? AND request_id = ? AND status = 'processing'",
            (_dump(INTERRUPTED_ERROR), now_iso, owner_id, request_id),
        )

    def complete_chat_request(
        self,
        *,
        owner_id: str,
        request_id: str,
        attempt_id: str,
        answer: str,
        response: dict[str, Any],
        context: dict[str, Any] | None,
        data_version_id: str | None,
        rules_version: str | None,
        now: datetime,
    ) -> bool:
        """Persist the answer, results and analysis context together.

        Returns False (and writes nothing) when this attempt no longer owns a valid lease.
        """
        now_iso = to_iso(now)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT conversation_id, status, attempt_id, lease_expires_at FROM chat_requests "
                "WHERE owner_id = ? AND request_id = ?",
                (owner_id, request_id),
            ).fetchone()
            if (
                row is None
                or row["status"] != "processing"
                or row["attempt_id"] != attempt_id
                or row["lease_expires_at"] is None
                or row["lease_expires_at"] <= now_iso
            ):
                return False
            conversation_id = row["conversation_id"]
            message_seq = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            response_json = _dump(response)
            conn.execute(
                "INSERT INTO messages(conversation_id, seq, role, content, request_id, "
                "response_json, created_at) VALUES (?, ?, 'assistant', ?, ?, ?, ?)",
                (conversation_id, message_seq, answer, request_id, response_json, now_iso),
            )
            conn.execute(
                "UPDATE chat_requests SET status = 'completed', response_json = ?, "
                "lease_expires_at = NULL, error_json = NULL, completed_at = ?, updated_at = ? "
                "WHERE owner_id = ? AND request_id = ?",
                (response_json, now_iso, now_iso, owner_id, request_id),
            )
            if context is not None:
                conn.execute(
                    "INSERT INTO analysis_context(conversation_id, owner_id, context_json, "
                    "data_version_id, rules_version, last_request_id, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(conversation_id) DO UPDATE SET context_json = excluded.context_json, "
                    "data_version_id = excluded.data_version_id, "
                    "rules_version = excluded.rules_version, "
                    "last_request_id = excluded.last_request_id, updated_at = excluded.updated_at",
                    (
                        conversation_id,
                        owner_id,
                        _dump(context),
                        data_version_id,
                        rules_version,
                        request_id,
                        now_iso,
                    ),
                )
            conn.execute(
                "UPDATE conversations SET updated_at = ? WHERE conversation_id = ?",
                (now_iso, conversation_id),
            )
        return True

    def fail_chat_request(
        self,
        *,
        owner_id: str,
        request_id: str,
        attempt_id: str,
        error: dict[str, Any],
        now: datetime,
    ) -> bool:
        """Mark a known failure. Never touches the last completed analysis context."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE chat_requests SET status = 'failed', attempt_id = NULL, "
                "lease_expires_at = NULL, error_json = ?, updated_at = ? "
                "WHERE owner_id = ? AND request_id = ? AND status = 'processing' AND attempt_id = ?",
                (_dump(error), to_iso(now), owner_id, request_id, attempt_id),
            )
        return cursor.rowcount == 1

    def get_chat_request(self, owner_id: str, request_id: str, now: datetime) -> ChatRequestRecord | None:
        """Owner-scoped status read. A processing request whose lease expired becomes interrupted.

        Reads first and takes a write lock only when the status actually has to change, so frequent
        polling never queues behind other writes.
        """
        now_iso = to_iso(now)
        query = "SELECT * FROM chat_requests WHERE owner_id = ? AND request_id = ?"
        with self.connection() as conn:
            row = conn.execute(query, (owner_id, request_id)).fetchone()
        if row is None:
            return None
        if row["status"] == "processing" and (
            row["lease_expires_at"] is None or row["lease_expires_at"] <= now_iso
        ):
            with self.transaction() as conn:
                self._interrupt(conn, owner_id, request_id, now_iso)
                row = conn.execute(query, (owner_id, request_id)).fetchone()
        return _chat_request(row)

    def interrupt_expired_requests(self, now: datetime, conversation_id: str | None = None) -> int:
        """Mark processing requests whose lease expired as interrupted (valid leases untouched).

        With `conversation_id`, only that conversation is checked. The write lock is taken only when
        an expired request exists.
        """
        now_iso = to_iso(now)
        condition = "status = 'processing' AND (lease_expires_at IS NULL OR lease_expires_at <= ?)"
        params: tuple[Any, ...] = (now_iso,)
        if conversation_id is not None:
            condition += " AND conversation_id = ?"
            params += (conversation_id,)
        with self.connection() as conn:
            expired = conn.execute(
                f"SELECT 1 FROM chat_requests WHERE {condition} LIMIT 1", params
            ).fetchone()
        if expired is None:
            return 0
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE chat_requests SET status = 'interrupted', attempt_id = NULL, "
                f"lease_expires_at = NULL, error_json = ?, updated_at = ? WHERE {condition}",
                (_dump(INTERRUPTED_ERROR), now_iso, *params),
            )
        return cursor.rowcount

    # -- conversations --------------------------------------------------------------------------

    def list_conversations(self, owner_id: str, limit: int = 200) -> list[ConversationRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM conversations WHERE owner_id = ? ORDER BY updated_at DESC LIMIT ?",
                (owner_id, limit),
            ).fetchall()
        return [self._conversation(row) for row in rows]

    def get_conversation(self, owner_id: str, conversation_id: str) -> ConversationRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM conversations WHERE conversation_id = ? AND owner_id = ?",
                (conversation_id, owner_id),
            ).fetchone()
        return None if row is None else self._conversation(row)

    def get_messages(self, owner_id: str, conversation_id: str) -> list[MessageRecord]:
        """Messages in order; user messages carry their request status for retry UIs."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT m.*, r.status AS request_status, "
                "r.sent_conversation_id AS sent_conversation_id "
                "FROM messages m JOIN conversations c ON c.conversation_id = m.conversation_id "
                "LEFT JOIN chat_requests r ON r.owner_id = c.owner_id "
                "AND r.request_id = m.request_id AND m.role = 'user' "
                "WHERE m.conversation_id = ? AND c.owner_id = ? ORDER BY m.seq",
                (conversation_id, owner_id),
            ).fetchall()
        return [
            MessageRecord(
                message_id=row["message_id"],
                conversation_id=row["conversation_id"],
                seq=row["seq"],
                role=row["role"],
                content=row["content"],
                request_id=row["request_id"],
                response=_load(row["response_json"]),
                created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
                request_status=row["request_status"],
                sent_conversation_id=row["sent_conversation_id"],
            )
            for row in rows
        ]

    def load_history(
        self,
        owner_id: str,
        conversation_id: str,
        exclude_request_id: str,
        max_messages: int,
        max_chars: int,
    ) -> list[dict[str, str]]:
        """Bounded recent history of completed exchanges, oldest first, for the model."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT m.role, m.content FROM messages m "
                "JOIN chat_requests r ON r.conversation_id = m.conversation_id "
                "AND r.request_id = m.request_id "
                "WHERE m.conversation_id = ? AND r.owner_id = ? AND r.status = 'completed' "
                "AND m.request_id != ? ORDER BY m.seq DESC LIMIT ?",
                (conversation_id, owner_id, exclude_request_id, max_messages),
            ).fetchall()
        history: list[dict[str, str]] = []
        used = 0
        for row in rows:
            content = row["content"]
            if used + len(content) > max_chars:
                break
            used += len(content)
            history.append({"role": row["role"], "content": content})
        history.reverse()
        return history

    def get_analysis_context(self, owner_id: str, conversation_id: str) -> AnalysisContextRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM analysis_context WHERE conversation_id = ? AND owner_id = ?",
                (conversation_id, owner_id),
            ).fetchone()
        if row is None:
            return None
        return AnalysisContextRecord(
            conversation_id=row["conversation_id"],
            context=_load(row["context_json"]),
            data_version_id=row["data_version_id"],
            rules_version=row["rules_version"],
            last_request_id=row["last_request_id"],
            updated_at=from_iso(row["updated_at"]),  # type: ignore[arg-type]
        )

    @staticmethod
    def _conversation(row: sqlite3.Row) -> ConversationRecord:
        return ConversationRecord(
            conversation_id=row["conversation_id"],
            owner_id=row["owner_id"],
            title=row["title"],
            created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
            updated_at=from_iso(row["updated_at"]),  # type: ignore[arg-type]
        )

    # -- refresh jobs ---------------------------------------------------------------------------

    def create_or_get_active_refresh_job(self, now: datetime) -> tuple[RefreshJobRecord, bool]:
        """Return the active job, or create a queued one. Only one job may be active at a time."""
        now_iso = to_iso(now)
        with self.transaction() as conn:
            conn.execute(
                "UPDATE refresh_jobs SET status = 'interrupted', lease_owner = NULL, "
                "lease_expires_at = NULL, finished_at = ?, updated_at = ?, error_json = ? "
                "WHERE status = 'running' AND lease_expires_at <= ?",
                (now_iso, now_iso, _dump(_JOB_INTERRUPTED), now_iso),
            )
            row = conn.execute("SELECT * FROM refresh_jobs WHERE status IN ('queued', 'running')").fetchone()
            if row is not None:
                return _refresh_job(row), False
            job_id = new_id()
            conn.execute(
                "INSERT INTO refresh_jobs(job_id, status, stage, created_at, updated_at) "
                "VALUES (?, 'queued', 'queued', ?, ?)",
                (job_id, now_iso, now_iso),
            )
            created = conn.execute("SELECT * FROM refresh_jobs WHERE job_id = ?", (job_id,)).fetchone()
            return _refresh_job(created), True

    def claim_refresh_job(self, job_id: str, worker_id: str, lease_seconds: float, now: datetime) -> bool:
        now_iso = to_iso(now)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE refresh_jobs SET status = 'running', stage = 'starting', lease_owner = ?, "
                "lease_expires_at = ?, started_at = ?, updated_at = ? "
                "WHERE job_id = ? AND status = 'queued'",
                (
                    worker_id,
                    to_iso(now + timedelta(seconds=lease_seconds)),
                    now_iso,
                    now_iso,
                    job_id,
                ),
            )
        return cursor.rowcount == 1

    def heartbeat_refresh_job(
        self,
        job_id: str,
        worker_id: str,
        lease_seconds: float,
        now: datetime,
        stage: str | None = None,
        sources: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Extend the lease and record progress. False means the worker lost ownership."""
        now_iso = to_iso(now)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE refresh_jobs SET lease_expires_at = ?, updated_at = ?, "
                "stage = COALESCE(?, stage), sources_json = COALESCE(?, sources_json) "
                "WHERE job_id = ? AND status = 'running' AND lease_owner = ? "
                "AND lease_expires_at > ?",
                (
                    to_iso(now + timedelta(seconds=lease_seconds)),
                    now_iso,
                    stage,
                    _dump(sources),
                    job_id,
                    worker_id,
                    now_iso,
                ),
            )
        return cursor.rowcount == 1

    def finish_refresh_job(
        self,
        job_id: str,
        worker_id: str,
        *,
        status: Literal["completed", "failed"],
        now: datetime,
        changed: bool | None = None,
        version_id: str | None = None,
        sources: list[dict[str, Any]] | None = None,
        error: dict[str, Any] | None = None,
    ) -> bool:
        """Finish a job without activating a version (no change, or failure)."""
        now_iso = to_iso(now)
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE refresh_jobs SET status = ?, stage = ?, lease_owner = NULL, "
                "lease_expires_at = NULL, finished_at = ?, updated_at = ?, changed = ?, "
                "version_id = COALESCE(?, version_id), sources_json = COALESCE(?, sources_json), "
                "error_json = ? WHERE job_id = ? AND status = 'running' AND lease_owner = ?",
                (
                    status,
                    status,
                    now_iso,
                    now_iso,
                    None if changed is None else int(changed),
                    version_id,
                    _dump(sources),
                    _dump(error),
                    job_id,
                    worker_id,
                ),
            )
        return cursor.rowcount == 1

    def activate_data_version(
        self,
        version_id: str,
        job_id: str,
        worker_id: str,
        now: datetime,
        sources: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Switch the active data version and complete the job in one short transaction.

        Only a worker that still holds a valid lease may activate. Returns False otherwise.
        """
        now_iso = to_iso(now)
        with self.transaction() as conn:
            job = conn.execute(
                "SELECT 1 FROM refresh_jobs WHERE job_id = ? AND status = 'running' "
                "AND lease_owner = ? AND lease_expires_at > ?",
                (job_id, worker_id, now_iso),
            ).fetchone()
            version = conn.execute(
                "SELECT status FROM data_versions WHERE version_id = ?", (version_id,)
            ).fetchone()
            if job is None or version is None or version["status"] != "ready":
                return False
            conn.execute("UPDATE data_versions SET status = 'superseded' WHERE status = 'active'")
            conn.execute(
                "UPDATE data_versions SET status = 'active', activated_at = ? WHERE version_id = ?",
                (now_iso, version_id),
            )
            conn.execute(
                "INSERT INTO active_data_version(singleton, version_id, activated_at) "
                "VALUES (1, ?, ?) ON CONFLICT(singleton) DO UPDATE SET "
                "version_id = excluded.version_id, activated_at = excluded.activated_at",
                (version_id, now_iso),
            )
            conn.execute(
                "UPDATE refresh_jobs SET status = 'completed', stage = 'completed', "
                "lease_owner = NULL, lease_expires_at = NULL, finished_at = ?, updated_at = ?, "
                "changed = 1, version_id = ?, sources_json = COALESCE(?, sources_json), "
                "error_json = NULL WHERE job_id = ?",
                (now_iso, now_iso, version_id, _dump(sources), job_id),
            )
        return True

    def get_refresh_job(self, job_id: str, now: datetime | None = None) -> RefreshJobRecord | None:
        """Read a job. With `now`, a running job whose lease expired is reported as interrupted."""
        query = "SELECT * FROM refresh_jobs WHERE job_id = ?"
        with self.connection() as conn:
            row = conn.execute(query, (job_id,)).fetchone()
        if (
            row is not None
            and now is not None
            and row["status"] == "running"
            and (row["lease_expires_at"] is None or row["lease_expires_at"] <= to_iso(now))
        ):
            now_iso = to_iso(now)
            with self.transaction() as conn:
                conn.execute(
                    "UPDATE refresh_jobs SET status = 'interrupted', lease_owner = NULL, "
                    "lease_expires_at = NULL, finished_at = ?, updated_at = ?, error_json = ? "
                    "WHERE job_id = ? AND status = 'running' "
                    "AND (lease_expires_at IS NULL OR lease_expires_at <= ?)",
                    (now_iso, now_iso, _dump(_JOB_INTERRUPTED), job_id, now_iso),
                )
                row = conn.execute(query, (job_id,)).fetchone()
        return None if row is None else _refresh_job(row)

    def interrupt_stale_refresh_jobs(self, now: datetime, include_queued: bool) -> int:
        """Running jobs with an expired lease (and, at startup, queued jobs) become interrupted."""
        now_iso = to_iso(now)
        statuses = "('queued', 'running')" if include_queued else "('running')"
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE refresh_jobs SET status = 'interrupted', lease_owner = NULL, "
                "lease_expires_at = NULL, finished_at = ?, updated_at = ?, error_json = ? "
                f"WHERE status IN {statuses} AND (status = 'queued' OR lease_expires_at <= ?)",
                (now_iso, now_iso, _dump(_JOB_INTERRUPTED), now_iso),
            )
        return cursor.rowcount

    # -- aviation data --------------------------------------------------------------------------

    def get_active_data_version_id(self) -> str | None:
        with self.connection() as conn:
            row = conn.execute("SELECT version_id FROM active_data_version WHERE singleton = 1").fetchone()
        return None if row is None else str(row["version_id"])

    # The remaining aviation-data queries (file registration, row inserts, version manifests,
    # national references and the read queries behind DataRepository) are added below.

    # -- aviation data: schema upgrade -----------------------------------------------------------

    def ensure_aviation_schema(self) -> None:
        """Add `data_files` columns that databases created before they existed lack (idempotent)."""
        with self.connection() as conn:
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(data_files)")}
        if {"remote_fingerprint", "notes_json"} <= existing:
            return
        with self.transaction() as conn:
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(data_files)")}
            for column in ("remote_fingerprint", "notes_json"):
                if column not in existing:
                    conn.execute(f"ALTER TABLE data_files ADD COLUMN {column} TEXT")

    # -- aviation data: source files ---------------------------------------------------------------

    def latest_ready_file(self, source_id: str, period_key: str) -> DataFileRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM data_files WHERE source_id = ? AND period_key = ? AND status = 'ready' "
                "ORDER BY retrieved_at DESC, file_id DESC LIMIT 1",
                (source_id, period_key),
            ).fetchone()
        return None if row is None else _data_file(row)

    def find_data_file(self, source_id: str, period_key: str, sha256: str) -> DataFileRecord | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT * FROM data_files WHERE source_id = ? AND period_key = ? AND sha256 = ?",
                (source_id, period_key, sha256),
            ).fetchone()
        return None if row is None else _data_file(row)

    def get_data_file(self, file_id: str) -> DataFileRecord | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM data_files WHERE file_id = ?", (file_id,)).fetchone()
        return None if row is None else _data_file(row)

    def list_data_files(self, status: str | None = None) -> list[dict[str, Any]]:
        """Every source file row (or those with `status`) as a plain dict, e.g. for backups.

        Keys: file_id, source_id, period_key, url, sha256, size_bytes, local_path (relative to the
        data directory), dataset_version, retrieved_at, checked_at, row_count and status.
        """
        with self.connection() as conn:
            if status is None:
                rows = conn.execute("SELECT * FROM data_files ORDER BY source_id, period_key").fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM data_files WHERE status = ? ORDER BY source_id, period_key", (status,)
                ).fetchall()
        return [_data_file(row).as_dict() for row in rows]

    def touch_data_file(
        self, file_id: str, checked_at: datetime, remote_fingerprint: str | None = None
    ) -> None:
        """Record that an existing file was confirmed current (optionally with a new fingerprint)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE data_files SET checked_at = ?, "
                "remote_fingerprint = COALESCE(?, remote_fingerprint) WHERE file_id = ?",
                (to_iso(checked_at), remote_fingerprint, file_id),
            )

    def begin_data_file(
        self,
        *,
        source_id: str,
        period_key: str,
        url: str,
        sha256: str,
        size_bytes: int,
        local_path: str,
        dataset_version: str | None,
        retrieved_at: datetime,
        remote_fingerprint: str | None,
    ) -> str:
        """Register a file as `ingesting`. A failed earlier attempt with the same content is reset."""
        now_iso = to_iso(retrieved_at)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT file_id, status FROM data_files WHERE source_id = ? AND period_key = ? "
                "AND sha256 = ?",
                (source_id, period_key, sha256),
            ).fetchone()
            values = (url, size_bytes, local_path, dataset_version, now_iso, now_iso, remote_fingerprint)
            if row is None:
                file_id = new_id()
                conn.execute(
                    "INSERT INTO data_files(file_id, source_id, period_key, sha256, url, size_bytes, "
                    "local_path, dataset_version, retrieved_at, checked_at, remote_fingerprint, status) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ingesting')",
                    (file_id, source_id, period_key, sha256, *values),
                )
                return file_id
            if row["status"] == "ready":
                raise StorageError("file_already_ready", "This source file is already ingested.")
            if row["status"] == "ingesting":
                raise StorageError("file_ingesting", "This source file is being ingested by another job.")
            file_id = str(row["file_id"])
            _delete_file_rows(conn, file_id)
            conn.execute(
                "UPDATE data_files SET url = ?, size_bytes = ?, local_path = ?, dataset_version = ?, "
                "retrieved_at = ?, checked_at = ?, remote_fingerprint = ?, row_count = NULL, "
                "notes_json = NULL, status = 'ingesting' WHERE file_id = ?",
                (*values, file_id),
            )
            return file_id

    def insert_file_rows(
        self, table: str, file_id: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]
    ) -> int:
        """Insert one batch of normalized rows for a file that is being ingested (one transaction)."""
        allowed = _FILE_ROW_TABLES.get(table)
        if allowed is None or not set(columns) <= set(allowed) or not columns:
            raise ValueError(f"Unsupported table or columns for file rows: {table}")
        sql = (
            f"INSERT INTO {table}(file_id, {', '.join(columns)}) "
            f"VALUES (?, {', '.join('?' for _ in columns)})"
        )
        with self.transaction() as conn:
            status = conn.execute("SELECT status FROM data_files WHERE file_id = ?", (file_id,)).fetchone()
            if status is None or status["status"] != "ingesting":
                raise StorageError("file_not_ingesting", "Rows can only be added while a file is ingesting.")
            conn.executemany(sql, ((file_id, *row) for row in rows))
        return len(rows)

    def finish_data_file(self, file_id: str, *, row_count: int, notes: Sequence[str]) -> None:
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE data_files SET status = 'ready', row_count = ?, notes_json = ? "
                "WHERE file_id = ? AND status = 'ingesting'",
                (row_count, _dump(list(notes)), file_id),
            )
            if cursor.rowcount != 1:
                raise StorageError("file_not_ingesting", "The file is no longer being ingested.")

    def update_data_file_notes(self, file_id: str, notes: Sequence[str]) -> None:
        """Replace the quality notes of a file (for example after the summary cross-check)."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE data_files SET notes_json = ? WHERE file_id = ?", (_dump(list(notes)), file_id)
            )

    def fail_data_file(self, file_id: str) -> None:
        """Mark an incomplete file failed and remove its partial rows."""
        with self.transaction() as conn:
            conn.execute(
                "UPDATE data_files SET status = 'failed', row_count = NULL "
                "WHERE file_id = ? AND status = 'ingesting'",
                (file_id,),
            )
            row = conn.execute("SELECT status FROM data_files WHERE file_id = ?", (file_id,)).fetchone()
            if row is not None and row["status"] == "failed":
                _delete_file_rows(conn, file_id)

    def has_running_refresh_job(self, now: datetime, ignore_job_id: str | None = None) -> bool:
        """True while a refresh job (other than `ignore_job_id`) holds a valid lease."""
        with self.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM refresh_jobs WHERE status = 'running' AND lease_expires_at > ? "
                "AND job_id IS NOT ?",
                (to_iso(now), ignore_job_id),
            ).fetchone()
        return row is not None

    def cleanup_incomplete_data(self, now: datetime, ignore_job_id: str | None = None) -> list[str]:
        """Fail files left `ingesting` and versions left `building` by a refresh that crashed.

        Nothing is touched while another refresh job still holds a valid lease, because that job
        owns its in-progress files. Returns the ids of the files that were cleaned up.
        """
        now_iso = to_iso(now)
        with self.transaction() as conn:
            busy = conn.execute(
                "SELECT 1 FROM refresh_jobs WHERE status = 'running' AND lease_expires_at > ? "
                "AND job_id IS NOT ?",
                (now_iso, ignore_job_id),
            ).fetchone()
            if busy is not None:
                return []
            file_ids = [
                str(row["file_id"])
                for row in conn.execute("SELECT file_id FROM data_files WHERE status = 'ingesting'")
            ]
            conn.execute(
                "UPDATE data_files SET status = 'failed', row_count = NULL WHERE status = 'ingesting'"
            )
            conn.execute("UPDATE data_versions SET status = 'failed' WHERE status = 'building'")
        for file_id in file_ids:
            self.fail_data_file(file_id)  # deletes the partial rows (status is already 'failed')
        return file_ids

    # -- aviation data: versions and national references -------------------------------------------

    def create_data_version(
        self,
        *,
        version_id: str,
        job_id: str | None,
        file_ids: Sequence[str],
        manifest: dict[str, Any],
        coverage: dict[str, Any],
        now: datetime,
    ) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO data_versions(version_id, status, job_id, created_at, manifest_json, "
                "coverage_json) VALUES (?, 'building', ?, ?, ?, ?)",
                (version_id, job_id, to_iso(now), _dump(manifest), _dump(coverage)),
            )
            conn.executemany(
                "INSERT INTO data_version_files(version_id, file_id) VALUES (?, ?)",
                [(version_id, file_id) for file_id in file_ids],
            )

    def set_data_version_status(self, version_id: str, status: Literal["ready", "failed"]) -> bool:
        """Move a version that is not active yet to `ready` or `failed`."""
        with self.transaction() as conn:
            cursor = conn.execute(
                "UPDATE data_versions SET status = ? "
                "WHERE version_id = ? AND status IN ('building', 'ready')",
                (status, version_id),
            )
        return cursor.rowcount == 1

    def save_national_references(self, version_id: str, references: Sequence[dict[str, Any]]) -> None:
        with self.transaction() as conn:
            conn.execute("DELETE FROM national_references WHERE version_id = ?", (version_id,))
            conn.executemany(
                "INSERT INTO national_references(version_id, metric, period_key, value, sample_size, "
                "details_json) VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (
                        version_id,
                        ref["metric"],
                        ref["period_key"],
                        ref.get("value"),
                        ref.get("sample_size"),
                        _dump(ref.get("details") or {}),
                    )
                    for ref in references
                ],
            )

    def get_national_reference(self, version_id: str, metric: str, period_key: str) -> dict[str, Any] | None:
        with self.connection() as conn:
            row = conn.execute(
                "SELECT metric, period_key, value, sample_size, details_json FROM national_references "
                "WHERE version_id = ? AND metric = ? AND period_key = ?",
                (version_id, metric, period_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "metric": row["metric"],
            "period_key": row["period_key"],
            "value": row["value"],
            "sample_size": row["sample_size"],
            "details": _load(row["details_json"]) or {},
        }

    def get_data_version(self, version_id: str) -> DataVersionRecord | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM data_versions WHERE version_id = ?", (version_id,)).fetchone()
        if row is None:
            return None
        return DataVersionRecord(
            version_id=row["version_id"],
            status=row["status"],
            job_id=row["job_id"],
            created_at=from_iso(row["created_at"]),  # type: ignore[arg-type]
            activated_at=from_iso(row["activated_at"]),
            manifest=_load(row["manifest_json"]) or {},
            coverage=_load(row["coverage_json"]),
        )

    def data_version_files(self, version_id: str) -> list[DataFileRecord]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT f.* FROM data_files f JOIN data_version_files v ON v.file_id = f.file_id "
                "WHERE v.version_id = ? ORDER BY f.source_id, f.period_key",
                (version_id,),
            ).fetchall()
        return [_data_file(row) for row in rows]

    # -- aviation data: ingestion checks ------------------------------------------------------------

    def file_year_months(self, table: str, file_id: str) -> dict[int, list[int]]:
        """Distinct (year -> months) present in one file's rows of a monthly table."""
        if table not in ("t100_segments", "t100_summary_month", "otp_departures_month"):
            raise ValueError(f"Unsupported monthly table: {table}")
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT DISTINCT year, month FROM {table} WHERE file_id = ? ORDER BY year, month",
                (file_id,),
            ).fetchall()
        result: dict[int, list[int]] = {}
        for row in rows:
            result.setdefault(int(row["year"]), []).append(int(row["month"]))
        return result

    def taf_fiscal_years(self, file_id: str) -> list[tuple[int, bool, int]]:
        """(fiscal_year, is_forecast, airports) for one TAF file."""
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT fiscal_year, is_forecast, COUNT(*) AS airports FROM taf_annual WHERE file_id = ? "
                "GROUP BY fiscal_year, is_forecast ORDER BY fiscal_year",
                (file_id,),
            ).fetchall()
        return [(int(r["fiscal_year"]), bool(r["is_forecast"]), int(r["airports"])) for r in rows]

    def t100_origin_month_sums(self, file_id: str) -> list[sqlite3.Row]:
        """Sums over all rows of one T-100 file per origin airport and month (summary cross-check)."""
        with self.connection() as conn:
            return conn.execute(
                "SELECT origin_airport_id AS airport_id, month, SUM(departures_performed) AS departures, "
                "SUM(passengers) AS passengers, SUM(seats) AS seats, MAX(origin_country) AS origin_country "
                "FROM t100_segments WHERE file_id = ? AND origin_airport_id IS NOT NULL "
                "GROUP BY origin_airport_id, month",
                (file_id,),
            ).fetchall()

    def t100_summary_rows(self, file_id: str) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                "SELECT airport_id, code, month, departures, passengers, seats FROM t100_summary_month "
                "WHERE file_id = ?",
                (file_id,),
            ).fetchall()

    # -- aviation data: repository reads (always scoped to explicit file ids) ----------------------

    def query_taf_annual(
        self, file_ids: Sequence[str], faa_locids: Sequence[str] | None
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT faa_locid, fiscal_year, is_forecast, total_enplanements, commercial_operations "
            "FROM taf_annual WHERE file_id IN ({files})"
        )
        return self._scoped_query(sql, file_ids, "faa_locid", faa_locids, order="faa_locid, fiscal_year")

    def query_taf_airports(self, file_ids: Sequence[str]) -> list[sqlite3.Row]:
        return self._scoped_query(
            "SELECT faa_locid, name, city, state, faa_region, hub_size FROM taf_airports "
            "WHERE file_id IN ({files})",
            file_ids,
            order="faa_locid",
        )

    def query_t100_segments(
        self,
        file_ids: Sequence[str],
        origin_airport_ids: Sequence[int],
        year: int,
        months: Sequence[int],
        service_classes: Sequence[str] | None,
    ) -> list[sqlite3.Row]:
        sql = (
            f"SELECT {', '.join(T100_SEGMENT_COLUMNS)} FROM t100_segments "
            "WHERE file_id IN ({files}) AND year = ? AND month IN ({months})"
        )
        params: list[Any] = [year, *months]
        if service_classes is not None:
            sql += f" AND service_class IN ({_marks(len(service_classes))})"
            params += list(service_classes)
        return self._scoped_query(
            sql,
            file_ids,
            "origin_airport_id",
            origin_airport_ids,
            params=params,
            months=months,
            order="origin_airport_id, month, carrier, dest_airport_id",
        )

    def query_t100_origin_totals(
        self,
        file_ids: Sequence[str],
        year: int,
        months: Sequence[int],
        service_classes: Sequence[str] | None,
        origin_country: str | None,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT origin_airport_id, MAX(origin) AS origin, "
            "SUM(departures_performed) AS departures_performed, "
            "SUM(seats) AS seats, SUM(passengers) AS passengers, COUNT(DISTINCT month) AS months_present "
            "FROM t100_segments WHERE file_id IN ({files}) AND year = ? AND month IN ({months}) "
            "AND origin_airport_id IS NOT NULL"
        )
        params: list[Any] = [year, *months]
        if service_classes is not None:
            sql += f" AND service_class IN ({_marks(len(service_classes))})"
            params += list(service_classes)
        if origin_country is not None:
            sql += " AND origin_country = ?"
            params.append(origin_country)
        return self._scoped_query(
            sql, file_ids, params=params, months=months, group="origin_airport_id", order="origin_airport_id"
        )

    def query_ontime_departures(
        self, file_ids: Sequence[str], airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT airport_id, year, month, days_in_month, scheduled, cancelled, diverted, del15_valid, "
            "del15_count FROM otp_departures_month WHERE file_id IN ({files}) AND year = ? "
            "AND month IN ({months})"
        )
        return self._scoped_query(
            sql,
            file_ids,
            "airport_id",
            airport_ids,
            params=[year, *months],
            months=months,
            order="airport_id, year, month",
        )

    def query_ontime_arrivals(
        self, file_ids: Sequence[str], airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT airport_id, year, month, arrivals, delay_carrier, delay_weather, delay_nas, "
            "delay_security, delay_late_aircraft FROM otp_arrivals_month WHERE file_id IN ({files}) "
            "AND year = ? AND month IN ({months})"
        )
        return self._scoped_query(
            sql,
            file_ids,
            "airport_id",
            airport_ids,
            params=[year, *months],
            months=months,
            order="airport_id, year, month",
        )

    def query_ontime_taxi_histogram(
        self,
        file_ids: Sequence[str],
        airport_ids: Sequence[int] | None,
        year: int,
        months: Sequence[int],
        kind: str,
    ) -> list[sqlite3.Row]:
        sql = (
            "SELECT airport_id, minutes, SUM(flights) AS flights FROM otp_taxi_hist "
            "WHERE file_id IN ({files}) AND kind = ? AND year = ? AND month IN ({months})"
        )
        return self._scoped_query(
            sql,
            file_ids,
            "airport_id",
            airport_ids,
            params=[kind, year, *months],
            months=months,
            group="airport_id, minutes",
            order="airport_id, minutes",
        )

    def bts_airport_codes(self, t100_file_ids: Sequence[str]) -> list[sqlite3.Row]:
        """Distinct BTS airport id / code / state / country seen in T-100 rows (origins and destinations)."""
        if not t100_file_ids:
            return []
        files = _marks(len(t100_file_ids))
        sql = (
            "SELECT airport_id, code, state, country, MAX(city_name) AS city_name, "
            "SUM(departures) AS departures, COUNT(*) AS segment_rows FROM ("
            "SELECT origin_airport_id AS airport_id, origin AS code, origin_state AS state, "
            "origin_country AS country, NULL AS city_name, departures_performed AS departures "
            f"FROM t100_segments WHERE file_id IN ({files}) "
            "UNION ALL SELECT dest_airport_id, dest, dest_state, dest_country, dest_city_name, 0 "
            f"FROM t100_segments WHERE file_id IN ({files})"
            ") WHERE airport_id IS NOT NULL GROUP BY airport_id, code, state, country "
            "ORDER BY code, airport_id"
        )
        with self.connection() as conn:
            return conn.execute(sql, [*t100_file_ids, *t100_file_ids]).fetchall()

    def ontime_airport_codes(self, otp_file_ids: Sequence[str]) -> list[sqlite3.Row]:
        """Distinct BTS airport id / code pairs seen in On-Time departures."""
        return self._scoped_query(
            "SELECT airport_id, code, SUM(scheduled) AS flights FROM otp_departures_month "
            "WHERE file_id IN ({files})",
            otp_file_ids,
            group="airport_id, code",
            order="code, airport_id",
        )

    def _scoped_query(
        self,
        sql: str,
        file_ids: Sequence[str],
        filter_column: str | None = None,
        filter_values: Sequence[Any] | None = None,
        *,
        params: Sequence[Any] = (),
        months: Sequence[int] = (),
        group: str | None = None,
        order: str | None = None,
    ) -> list[sqlite3.Row]:
        """Run `sql` ({files}/{months} placeholders) for explicit file ids, chunking long IN lists."""
        if not file_ids or (filter_values is not None and not filter_values):
            return []
        base = sql.format(files=_marks(len(file_ids)), months=_marks(len(months)))
        suffix = (f" GROUP BY {group}" if group else "") + (f" ORDER BY {order}" if order else "")
        rows: list[sqlite3.Row] = []
        with self.connection() as conn:
            if filter_column is None or filter_values is None:
                return conn.execute(base + suffix, [*file_ids, *params]).fetchall()
            values = list(dict.fromkeys(filter_values))
            for start in range(0, len(values), _IN_CHUNK):
                part = values[start : start + _IN_CHUNK]
                query = f"{base} AND {filter_column} IN ({_marks(len(part))}){suffix}"
                rows.extend(conn.execute(query, [*file_ids, *params, *part]).fetchall())
        return rows


_JOB_INTERRUPTED = {
    "code": "interrupted",
    "message": "The refresh stopped before completing. The previous data version stays active.",
}


def _title(message: str, limit: int = 80) -> str:
    text = " ".join(message.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def chunks(rows: Sequence[Any], size: int) -> Iterator[Sequence[Any]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


# ---------------------------------------------------------------------------------------------
# Aviation data records and helpers
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DataFileRecord:
    file_id: str
    source_id: str
    period_key: str
    url: str
    sha256: str
    size_bytes: int
    local_path: str  # relative to the data directory
    dataset_version: str | None
    retrieved_at: datetime
    checked_at: datetime
    row_count: int | None
    status: Literal["ingesting", "ready", "failed"]
    remote_fingerprint: str | None
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "source_id": self.source_id,
            "period_key": self.period_key,
            "url": self.url,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "local_path": self.local_path,
            "dataset_version": self.dataset_version,
            "retrieved_at": to_iso(self.retrieved_at),
            "checked_at": to_iso(self.checked_at),
            "row_count": self.row_count,
            "status": self.status,
        }


@dataclass(frozen=True)
class DataVersionRecord:
    version_id: str
    status: Literal["building", "ready", "active", "superseded", "failed"]
    job_id: str | None
    created_at: datetime
    activated_at: datetime | None
    manifest: dict[str, Any]
    coverage: dict[str, Any] | None


# Tables whose rows belong to one source file, with the columns the ingestion may write.
_FILE_ROW_TABLES: dict[str, tuple[str, ...]] = {
    "taf_airports": ("faa_locid", "name", "city", "state", "faa_region", "hub_size"),
    "taf_annual": (
        "faa_locid",
        "fiscal_year",
        "is_forecast",
        "enpl_air_carrier",
        "enpl_air_taxi",
        "enpl_commuter",
        "enpl_intl_us_flag",
        "enpl_intl_foreign_flag",
        "total_enplanements",
        "ops_itinerant_air_carrier",
        "ops_itinerant_air_taxi",
        "commercial_operations",
    ),
    "t100_segments": (
        "year",
        "month",
        "carrier",
        "origin_airport_id",
        "origin",
        "origin_country",
        "origin_state",
        "dest_airport_id",
        "dest",
        "dest_country",
        "dest_state",
        "dest_city_name",
        "service_class",
        "aircraft_config",
        "aircraft_type",
        "data_source",
        "departures_scheduled",
        "departures_performed",
        "seats",
        "passengers",
        "freight",
        "distance",
        "distance_group",
    ),
    "otp_departures_month": (
        "airport_id",
        "code",
        "year",
        "month",
        "days_in_month",
        "scheduled",
        "cancelled",
        "diverted",
        "del15_valid",
        "del15_count",
    ),
    "otp_arrivals_month": (
        "airport_id",
        "code",
        "year",
        "month",
        "arrivals",
        "delay_carrier",
        "delay_weather",
        "delay_nas",
        "delay_security",
        "delay_late_aircraft",
    ),
    "otp_taxi_hist": ("airport_id", "year", "month", "kind", "minutes", "flights"),
    "t100_summary_month": ("airport_id", "code", "year", "month", "departures", "passengers", "seats"),
}

# Column order of DataRepository.t100_segments().
T100_SEGMENT_COLUMNS: tuple[str, ...] = (
    "year",
    "month",
    "carrier",
    "origin_airport_id",
    "origin",
    "origin_country",
    "dest_airport_id",
    "dest",
    "dest_country",
    "dest_city_name",
    "service_class",
    "aircraft_config",
    "aircraft_type",
    "departures_scheduled",
    "departures_performed",
    "seats",
    "passengers",
    "freight",
    "distance",
    "distance_group",
)

_IN_CHUNK = 500


def _marks(count: int) -> str:
    return ", ".join("?" for _ in range(count))


def _data_file(row: sqlite3.Row) -> DataFileRecord:
    keys = set(row.keys())
    return DataFileRecord(
        file_id=row["file_id"],
        source_id=row["source_id"],
        period_key=row["period_key"],
        url=row["url"],
        sha256=row["sha256"],
        size_bytes=row["size_bytes"],
        local_path=row["local_path"],
        dataset_version=row["dataset_version"],
        retrieved_at=from_iso(row["retrieved_at"]),  # type: ignore[arg-type]
        checked_at=from_iso(row["checked_at"]),  # type: ignore[arg-type]
        row_count=row["row_count"],
        status=row["status"],
        remote_fingerprint=row["remote_fingerprint"] if "remote_fingerprint" in keys else None,
        notes=(_load(row["notes_json"]) or []) if "notes_json" in keys else [],
    )


def _delete_file_rows(conn: sqlite3.Connection, file_id: str) -> None:
    for table in _FILE_ROW_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))
