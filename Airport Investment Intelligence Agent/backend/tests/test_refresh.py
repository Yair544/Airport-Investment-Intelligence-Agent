"""Refresh job: versions, reuse of unchanged files, failures and leases (fake sources, real SQLite)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.analysis.models import NationalReference
from app.config import Settings
from app.data.download import DownloadError, RemoteMeta
from app.data.providers import ontime, t100, t100_summary, taf
from app.data.refresh import FetchedFile, cleanup_incomplete_files, run_refresh_job, summary_fingerprint
from app.data.resolution import load_reference_catalog
from app.data.service import DataRepository, DataUnavailableError, SqliteDataService
from app.storage import RefreshJobRecord, Storage, to_iso
from tests.test_providers import ontime_month_csv, summary_rows, t100_year_csv, taf_members, write_zip

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def clock() -> datetime:
    return NOW


class FakeGateway:
    """In-memory "remote" sources that write files the way the HTTP gateway does."""

    def __init__(self, raw_dir: Path) -> None:
        self.raw_dir = raw_dir
        self.taf_editions = {2025: "etag-taf-2025"}
        self.taf_scale = 1.0
        self.summary_latest = (2026, 4)
        self.summary_departures = {2024: 30, 2025: 30}
        self.t100_departures = {2024: 30, 2025: 30}
        self.ontime = {(year, month): "etag-1" for year in (2024, 2025) for month in range(1, 13)}
        self.ontime_taxi: dict[tuple[int, int], float] = {}
        self.failures: dict[str, Exception] = {}
        self.downloads: list[str] = []

    def _store(self, source_id: str, stem: str, content: bytes, fingerprint: str | None) -> FetchedFile:
        sha = hashlib.sha256(content).hexdigest()
        path = self.raw_dir / source_id / f"{stem}-{sha[:12]}.zip"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.downloads.append(f"{source_id}:{stem}")
        return FetchedFile(
            path, sha, len(content), f"https://example.test/{stem}", datetime.now(UTC), fingerprint
        )

    @staticmethod
    def _zip(members: dict[str, bytes], tmp: Path) -> bytes:
        return write_zip(tmp, members).read_bytes()

    def _meta(self, etag: str) -> RemoteMeta:
        return RemoteMeta("https://example.test", etag, None, None, "application/zip")

    def taf_edition_meta(self, edition: int) -> RemoteMeta | None:
        etag = self.taf_editions.get(edition)
        return None if etag is None else self._meta(etag)

    def download_taf(self, edition: int) -> FetchedFile:
        content = self._zip(taf_members(scale=self.taf_scale), self.raw_dir.parent / "taf-build.zip")
        fetched = self._store(
            taf.SOURCE_ID, str(edition), content, self._meta(self.taf_editions[edition]).fingerprint()
        )
        return FetchedFile(**{**fetched.__dict__, "dataset_version": taf.dataset_version(edition)})

    def summary_latest_month(self) -> tuple[int, int]:
        return self.summary_latest

    def fetch_summary_year(self, year: int) -> FetchedFile:
        frame = t100_summary.normalize_rows(summary_rows(year, self.summary_departures[year]), year)
        summary = t100_summary.SummaryYear(year, frame, t100_summary.canonical_content(frame, year), "")
        sha = hashlib.sha256(summary.content).hexdigest()
        summary = t100_summary.SummaryYear(year, frame, summary.content, sha)
        path = t100_summary.write_year_file(
            summary, self.raw_dir / t100_summary.SOURCE_ID, self.raw_dir / ".tmp"
        )
        return FetchedFile(
            path, sha, len(summary.content), t100_summary.API_URL, datetime.now(UTC), summary_fingerprint(sha)
        )

    def download_t100_year(self, year: int) -> FetchedFile:
        if failure := self.failures.get(f"t100:{year}"):
            raise failure
        content = self._zip(
            {t100.CSV_MEMBER: t100_year_csv(year, self.t100_departures[year])},
            self.raw_dir.parent / "t100-build.zip",
        )
        return self._store(t100.SOURCE_ID, str(year), content, None)

    def ontime_month_meta(self, year: int, month: int) -> RemoteMeta | None:
        etag = self.ontime.get((year, month))
        return None if etag is None else self._meta(etag)

    def download_ontime_month(self, year: int, month: int) -> FetchedFile:
        csv_bytes = ontime_month_csv(year, month, self.ontime_taxi.get((year, month), 12.0))
        content = self._zip(
            {ontime.member_name(year, month): csv_bytes}, self.raw_dir.parent / "otp-build.zip"
        )
        return self._store(
            ontime.SOURCE_ID,
            ontime.period_key(year, month),
            content,
            self._meta(self.ontime[(year, month)]).fingerprint(),
        )

    def close(self) -> None:
        pass


def builder(repo: DataRepository) -> list[NationalReference]:
    coverage = repo.coverage()
    base = coverage.taf_base_year or 0
    taf_rows = repo.taf_annual()
    return [
        NationalReference(
            metric="taf_cagr",
            period_key=f"FY{base}-FY{base + 5}",
            value=4.0,
            sample_size=taf_rows["faa_locid"].nunique(),
        )
    ]


def refresh(
    storage: Storage,
    settings: Settings,
    gateway: FakeGateway,
    reference_builder: Callable[[DataRepository], list[NationalReference]] = builder,
) -> RefreshJobRecord:
    return run_refresh_job(storage, settings, reference_builder, gateway=gateway, clock=clock)


@pytest.fixture
def gateway(settings: Settings) -> FakeGateway:
    return FakeGateway(settings.raw_data_dir)


def count(storage: Storage, sql: str) -> int:
    with storage.connection() as conn:
        return int(conn.execute(sql).fetchone()[0])


def test_first_refresh_builds_validates_and_activates_a_version(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    record = refresh(storage, settings, gateway)

    assert record.status == "completed" and record.changed is True and record.error is None
    active = storage.get_active_data_version_id()
    assert active is not None and record.version_id == active
    assert count(storage, "SELECT COUNT(*) FROM data_files WHERE status = 'ready'") == 1 + 2 + 2 + 24
    version = storage.get_data_version(active)
    assert version is not None and version.status == "active"
    assert version.manifest["targets"] == {"taf_edition": 2025, "calendar_years": [2024, 2025]}
    cross_check = version.manifest["quality"]["t100_summary_cross_check"]["2025"]
    assert cross_check["value_mismatches"] == 0 and cross_check["compared_origin_months"] == 24
    assert cross_check["foreign_origin_months"] == 12  # YVR departures: outside the summary's scope
    assert cross_check["us_origin_months_missing_from_summary"] == 0

    repo = SqliteDataService(storage, load_reference_catalog()).repository()
    coverage = repo.coverage()
    assert coverage.t100_months == {2024: list(range(1, 13)), 2025: list(range(1, 13))}
    assert coverage.ontime_months[2025] == list(range(1, 13))
    assert coverage.t100_summary_months[2024] == list(range(1, 13))
    assert (coverage.taf_edition, coverage.taf_base_year, coverage.taf_last_year) == ("TAF 2025", 2024, 2030)
    assert coverage.latest_full_year() == 2025 and coverage.activated_at is not None
    reference = repo.national_reference("taf_cagr", "FY2024-FY2029")
    assert reference is not None and reference.value == 4.0 and reference.sample_size == 3
    totals = repo.t100_origin_totals(2025, list(range(1, 13))).set_index("origin_airport_id")
    assert totals.loc[10299, "departures_performed"] == 34 * 12  # summary numbers are never added

    sources = {source["source_id"]: source for source in record.sources}
    assert set(sources) == {"faa_taf", "bts_t100_summary", "bts_t100_segment", "bts_ontime"}
    assert all(source["status"] == "updated" for source in sources.values())
    assert sources["bts_ontime"]["periods"] == ["CY2024", "CY2025"]
    assert sources["faa_taf"]["periods"] == ["FY2022-FY2030"]
    assert all(source["checked_at"] and source["retrieved_at"] for source in sources.values())
    assert "0 US origin-month value mismatches" in sources["bts_t100_summary"]["message"]


def test_unchanged_sources_are_reused_without_download_or_reprocessing(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    first = refresh(storage, settings, gateway)
    downloads, files = len(gateway.downloads), count(storage, "SELECT COUNT(*) FROM data_files")
    rows = count(storage, "SELECT COUNT(*) FROM otp_taxi_hist")

    second = refresh(storage, settings, gateway)

    assert second.status == "completed" and second.changed is False
    assert second.version_id == first.version_id == storage.get_active_data_version_id()
    assert len(gateway.downloads) == downloads  # fingerprints matched: nothing fetched again
    assert count(storage, "SELECT COUNT(*) FROM data_files") == files
    assert count(storage, "SELECT COUNT(*) FROM data_versions") == 1
    assert count(storage, "SELECT COUNT(*) FROM otp_taxi_hist") == rows
    assert all(source["status"] == "unchanged" for source in second.sources)


def test_identical_content_with_new_fingerprint_is_not_reprocessed(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    refresh(storage, settings, gateway)
    gateway.ontime[(2025, 5)] = "etag-2"  # file re-published with the same bytes
    record = refresh(storage, settings, gateway)
    assert record.changed is False
    assert gateway.downloads.count("bts_ontime:2025-05") == 2
    assert count(storage, "SELECT COUNT(*) FROM data_files WHERE period_key = '2025-05'") == 1


def test_changed_file_creates_a_new_version_and_old_version_stays_readable(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    first = refresh(storage, settings, gateway)
    gateway.ontime[(2025, 3)] = "etag-2"
    gateway.ontime_taxi[(2025, 3)] = 30.0
    second = refresh(storage, settings, gateway)

    assert second.status == "completed" and second.changed is True
    assert second.version_id != first.version_id
    assert storage.get_active_data_version_id() == second.version_id
    assert count(storage, "SELECT COUNT(*) FROM data_files") == 30  # only the changed month was added
    assert {s["source_id"]: s["status"] for s in second.sources}["bts_ontime"] == "updated"
    old = storage.get_data_version(first.version_id or "")
    assert old is not None and old.status == "superseded"

    service = SqliteDataService(storage, load_reference_catalog())
    old_hist = service.repository(first.version_id).ontime_taxi_histogram([12892], 2025, [3], "taxi_out")
    new_hist = service.repository().ontime_taxi_histogram([12892], 2025, [3], "taxi_out")
    assert dict(zip(old_hist["minutes"], old_hist["flights"], strict=True)) == {12: 1, 20: 1}
    assert dict(zip(new_hist["minutes"], new_hist["flights"], strict=True)) == {20: 1, 30: 1}


def test_failure_keeps_the_previous_version_active(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    first = refresh(storage, settings, gateway)
    gateway.summary_departures[2025] = 31  # BTS revised 2025, so the T-100 year must be fetched again
    gateway.failures["t100:2025"] = DownloadError("www.transtats.bts.gov returned HTTP 503.")

    record = refresh(storage, settings, gateway)

    assert record.status == "failed" and record.changed is False
    assert storage.get_active_data_version_id() == first.version_id
    assert record.error == {
        "code": "refresh_failed",
        "message": "www.transtats.bts.gov returned HTTP 503. The previous data version stays active.",
    }
    statuses = {source["source_id"]: source["status"] for source in record.sources}
    assert statuses["bts_t100_segment"] == "failed"
    assert count(storage, "SELECT COUNT(*) FROM data_versions") == 1
    assert count(storage, "SELECT COUNT(*) FROM data_files WHERE status = 'ingesting'") == 0


def test_summary_cross_check_mismatches_are_recorded_not_fatal(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    gateway.summary_departures[2024] = 33  # summary says 37 departures per month, segments sum to 34
    record = refresh(storage, settings, gateway)
    assert record.status == "completed"
    version = storage.get_data_version(record.version_id or "")
    assert version is not None
    check = version.manifest["quality"]["t100_summary_cross_check"]["2024"]
    assert check["value_mismatches"] == 12 and check["examples"][0]["departures_segment"] == 34.0
    file = storage.latest_ready_file("bts_t100_segment", "2024")
    assert file is not None and any("12 with different departures" in note for note in file.notes)


def test_reference_failure_and_missing_month_fail_without_activation(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    def implausible(repo: DataRepository) -> list[NationalReference]:
        raise ValueError("national load factor 140% is outside 30-100%")

    record = refresh(storage, settings, gateway, implausible)
    assert record.status == "failed"
    assert (
        record.error is not None and "could not be computed: national load factor" in record.error["message"]
    )
    assert storage.get_active_data_version_id() is None
    assert count(storage, "SELECT COUNT(*) FROM data_versions WHERE status = 'failed'") == 1
    with pytest.raises(DataUnavailableError):
        SqliteDataService(storage, load_reference_catalog()).repository()

    del gateway.ontime[(2025, 7)]
    record = refresh(storage, settings, gateway)
    assert record.status == "failed"
    assert record.error is not None
    assert "has not published On-Time Performance data for 2025-07" in record.error["message"]


def test_job_that_lost_its_lease_cannot_activate(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    def hijack(repo: DataRepository) -> list[NationalReference]:
        with storage.transaction() as conn:  # another worker takes the job over
            conn.execute("UPDATE refresh_jobs SET lease_owner = 'other-worker'")
        return builder(repo)

    record = refresh(storage, settings, gateway, hijack)

    assert record.status == "running" and record.lease_owner == "other-worker"
    assert storage.get_active_data_version_id() is None
    assert count(storage, "SELECT COUNT(*) FROM data_versions WHERE status = 'failed'") == 1

    job, _ = storage.create_or_get_active_refresh_job(NOW)
    with storage.transaction() as conn:
        conn.execute(
            "INSERT INTO data_versions(version_id, status, created_at, manifest_json) "
            "VALUES ('v-x', 'ready', ?, '{}')",
            (to_iso(NOW),),
        )
    assert not storage.activate_data_version("v-x", job.job_id, "not-the-owner", NOW)


def test_run_without_job_id_returns_the_already_active_job(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    queued, created = storage.create_or_get_active_refresh_job(NOW)
    assert created
    record = run_refresh_job(storage, settings, builder, gateway=gateway, clock=clock)
    assert record.job_id == queued.job_id and record.status == "queued"
    assert gateway.downloads == []


def test_cleanup_incomplete_files_removes_partial_rows(storage: Storage, settings: Settings) -> None:
    storage.ensure_aviation_schema()
    file_id = storage.begin_data_file(
        source_id="bts_ontime",
        period_key="2025-01",
        url="https://example.test",
        sha256="ab" * 32,
        size_bytes=10,
        local_path="raw/bts_ontime/x.zip",
        dataset_version=None,
        retrieved_at=NOW,
        remote_fingerprint=None,
    )
    storage.insert_file_rows(
        "otp_taxi_hist",
        file_id,
        ["airport_id", "year", "month", "kind", "minutes", "flights"],
        [(12892, 2025, 1, "taxi_out", 12, 3)],
    )
    part = settings.raw_data_dir / ".tmp" / "2025-01.123.part"
    part.parent.mkdir(parents=True, exist_ok=True)
    part.write_bytes(b"partial")

    job, _ = storage.create_or_get_active_refresh_job(datetime.now(UTC))
    assert storage.claim_refresh_job(job.job_id, "live-worker", 300, datetime.now(UTC))
    assert cleanup_incomplete_files(storage) == 0  # a live worker owns its in-progress files
    assert part.exists()

    storage.finish_refresh_job(job.job_id, "live-worker", status="failed", now=datetime.now(UTC))
    assert cleanup_incomplete_files(storage) == 1
    record = storage.get_data_file(file_id)
    assert record is not None and record.status == "failed"
    assert count(storage, "SELECT COUNT(*) FROM otp_taxi_hist") == 0
    assert not part.exists()


def test_lease_lost_during_a_run_marks_the_job_failed(
    storage: Storage, settings: Settings, gateway: FakeGateway
) -> None:
    def expire_lease(repo: DataRepository) -> list[NationalReference]:
        with storage.transaction() as conn:  # the lease runs out while the worker is busy
            conn.execute("UPDATE refresh_jobs SET lease_expires_at = '2000-01-01T00:00:00.000Z'")
        return builder(repo)

    record = refresh(storage, settings, gateway, expire_lease)

    assert record.status == "failed" and record.error is not None
    assert record.error["code"] == "lease_lost"
    assert storage.get_active_data_version_id() is None


def test_slow_download_keeps_its_lease_alive(storage: Storage, settings: Settings) -> None:
    import time as real_time

    class SlowGateway(FakeGateway):
        slept = False

        def download_t100_year(self, year: int) -> FetchedFile:
            if not self.slept:  # one download clearly longer than the 3 s lease below
                self.slept = True
                real_time.sleep(4.5)
            return super().download_t100_year(year)

    # The keep-alive renews every second, so two late renewals in a row would still be tolerated.
    settings.refresh_lease_seconds = 3.0
    record = run_refresh_job(storage, settings, builder, gateway=SlowGateway(settings.raw_data_dir))
    assert record.status == "completed" and record.changed is True
