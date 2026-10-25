"""Data refresh job (spec section 8): discover, fetch or reuse, ingest, validate, reference, activate.

Entry points:
- `run_refresh_job(storage, settings, reference_builder, job_id=None, worker_id=None)` claims a
  queued job (or creates one when `job_id` is None), builds a new data version when the target
  file set changed, and activates it in one short transaction that also completes the job.
- `cleanup_incomplete_files(storage)` runs at startup and removes the partial rows of files that
  a crashed refresh left in `ingesting`.

A data version is a set of source files (exactly one per source and period) plus coverage and
the national references computed from it by the injected `reference_builder`, so this module
never imports the analysis layer. A file whose remote fingerprint (ETag/Last-Modified for files,
the summary API totals for a T-100 year) or sha256 is unchanged is reused without reprocessing.
Rows are inserted in batches of short transactions. A keep-alive thread extends the job lease
while long downloads and parsing run, the lease is also checked on every stage, file and batch,
and a job that lost its lease stops without activating anything and is marked failed. Any failure
leaves the previously active version in place.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import partial
from pathlib import Path
from typing import Any, Protocol, cast

import pandas as pd

from app.analysis.models import NationalReference
from app.config import Settings
from app.data.download import DownloadError, RemoteMeta, SafeHttpClient, safe_child
from app.data.providers import ParsedFile, SourceFormatError, ontime, t100, t100_summary, taf
from app.data.service import DataCoverage, DataRepository, SqliteDataRepository
from app.storage import DataFileRecord, RefreshJobRecord, Storage, StorageError, new_id, to_iso, utcnow

logger = logging.getLogger("app.data.refresh")

ReferenceBuilder = Callable[[DataRepository], list[NationalReference]]
INSERT_BATCH_ROWS = 20_000
SOURCE_ORDER = ("faa_taf", "bts_t100_summary", "bts_t100_segment", "bts_ontime")
TMP_DIR_NAME = ".tmp"


class RefreshError(Exception):
    """An expected refresh failure whose message is safe to show to users."""


class CoverageError(RefreshError):
    """The fetched data does not cover the periods a data version needs."""


class LeaseLostError(Exception):
    """Another worker took over the job, or its lease expired."""


@dataclass(frozen=True)
class FetchedFile:
    path: Path
    sha256: str
    size_bytes: int
    url: str
    retrieved_at: datetime
    remote_fingerprint: str | None
    dataset_version: str | None = None


class SourceGateway(Protocol):
    """Remote access used by the refresh job (tests substitute a fake)."""

    def taf_edition_meta(self, edition: int) -> RemoteMeta | None: ...

    def download_taf(self, edition: int) -> FetchedFile: ...

    def summary_latest_month(self) -> tuple[int, int]: ...

    def fetch_summary_year(self, year: int) -> FetchedFile: ...

    def download_t100_year(self, year: int) -> FetchedFile: ...

    def ontime_month_meta(self, year: int, month: int) -> RemoteMeta | None: ...

    def download_ontime_month(self, year: int, month: int) -> FetchedFile: ...

    def close(self) -> None: ...


class HttpSourceGateway:
    """Real gateway: one safe HTTP client and the raw-file directory layout under `data/raw/`."""

    def __init__(self, client: SafeHttpClient, raw_dir: Path) -> None:
        self._client = client
        self._raw_dir = raw_dir
        self._tmp_dir = raw_dir / TMP_DIR_NAME

    def _dest(self, source_id: str) -> Path:
        return safe_child(self._raw_dir, source_id)

    def _fetched(
        self, result: Any, fingerprint: str | None, dataset_version: str | None = None
    ) -> FetchedFile:
        return FetchedFile(
            path=result.path,
            sha256=result.sha256,
            size_bytes=result.size_bytes,
            url=result.url,
            retrieved_at=result.retrieved_at,
            remote_fingerprint=fingerprint,
            dataset_version=dataset_version,
        )

    def taf_edition_meta(self, edition: int) -> RemoteMeta | None:
        return self._client.head(taf.edition_url(edition))

    def download_taf(self, edition: int) -> FetchedFile:
        result = self._client.download(
            taf.edition_url(edition),
            dest_dir=self._dest(taf.SOURCE_ID),
            tmp_dir=self._tmp_dir,
            stem=str(edition),
        )
        return self._fetched(result, result.meta.fingerprint(), taf.dataset_version(edition))

    def summary_latest_month(self) -> tuple[int, int]:
        return t100_summary.latest_month(self._client)

    def fetch_summary_year(self, year: int) -> FetchedFile:
        summary = t100_summary.fetch_year(self._client, year)
        path = t100_summary.write_year_file(summary, self._dest(t100_summary.SOURCE_ID), self._tmp_dir)
        return FetchedFile(
            path=path,
            sha256=summary.sha256,
            size_bytes=len(summary.content),
            url=t100_summary.API_URL,
            retrieved_at=utcnow(),
            remote_fingerprint=summary_fingerprint(summary.sha256),
        )

    def download_t100_year(self, year: int) -> FetchedFile:
        result = t100.download_year(
            self._client, year, dest_dir=self._dest(t100.SOURCE_ID), tmp_dir=self._tmp_dir
        )
        return self._fetched(result, None)

    def ontime_month_meta(self, year: int, month: int) -> RemoteMeta | None:
        return ontime.month_meta(self._client, year, month)

    def download_ontime_month(self, year: int, month: int) -> FetchedFile:
        result = self._client.download(
            ontime.month_url(year, month),
            dest_dir=self._dest(ontime.SOURCE_ID),
            tmp_dir=self._tmp_dir,
            stem=ontime.period_key(year, month),
        )
        return self._fetched(result, result.meta.fingerprint())

    def close(self) -> None:
        self._client.close()


def summary_fingerprint(sha256: str) -> str:
    """T-100 year fingerprint: sha256 of that year's summary totals per origin and month."""
    return f"summary-sha256:{sha256}"


# ---------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------


def run_refresh_job(
    storage: Storage,
    settings: Settings,
    reference_builder: ReferenceBuilder,
    job_id: str | None = None,
    worker_id: str | None = None,
    *,
    gateway: SourceGateway | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> RefreshJobRecord:
    """Run one refresh job to completion and return its final record.

    With `job_id=None` a job is created (or the already active job is returned untouched).
    The job must be `queued`; if another worker claimed it, its current record is returned.
    """
    storage.ensure_aviation_schema()
    if job_id is None:
        job, created = storage.create_or_get_active_refresh_job(clock())
        if not created:
            return job
        job_id = job.job_id
    worker = worker_id or f"refresh-{new_id()}"
    if storage.claim_refresh_job(job_id, worker, settings.refresh_lease_seconds, clock()):
        deadline = time.monotonic() + settings.refresh_budget_seconds
        own_gateway = gateway is None
        active_gateway = gateway or HttpSourceGateway(
            SafeHttpClient.from_settings(settings, deadline=deadline), settings.raw_data_dir
        )
        try:
            _RefreshRun(
                storage, settings, active_gateway, reference_builder, job_id, worker, clock, deadline
            ).run()
        finally:
            if own_gateway:
                active_gateway.close()
    record = storage.get_refresh_job(job_id)
    if record is None:
        raise StorageError("refresh_job_not_found", "The refresh job does not exist.")
    return record


def cleanup_incomplete_files(storage: Storage, raw_dir: Path | None = None) -> int:
    """Startup cleanup: fail files stuck in `ingesting` (deleting their rows) and stale temp files.

    Does nothing while a refresh job holds a valid lease. Returns the number of files cleaned.
    """
    storage.ensure_aviation_schema()
    now = utcnow()
    if storage.has_running_refresh_job(now):
        return 0  # a live worker owns its in-progress files
    cleaned = storage.cleanup_incomplete_data(now)
    tmp_dir = (raw_dir or storage.database_path.parent / "raw") / TMP_DIR_NAME
    if tmp_dir.is_dir():
        for part in tmp_dir.glob("*.part"):
            try:
                part.unlink()
            except OSError:
                logger.warning("refresh_tmp_cleanup_failed", extra={"file": part.name})
    if cleaned:
        logger.info("refresh_incomplete_files_cleaned", extra={"files": len(cleaned)})
    return len(cleaned)


# ---------------------------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------------------------


@dataclass
class _SourceStatus:
    source_id: str
    status: str = "pending"
    periods: list[str] = field(default_factory=list)
    checked_at: datetime | None = None
    retrieved_at: datetime | None = None
    message: str | None = None
    updated: int = 0
    unchanged: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "status": self.status,
            "periods": self.periods,
            "checked_at": None if self.checked_at is None else to_iso(self.checked_at),
            "retrieved_at": None if self.retrieved_at is None else to_iso(self.retrieved_at),
            "message": self.message,
        }


@dataclass(frozen=True)
class _Plan:
    taf_edition: int
    taf_fingerprint: str | None
    years: list[int]
    summary_latest: tuple[int, int]


class _RefreshRun:
    def __init__(
        self,
        storage: Storage,
        settings: Settings,
        gateway: SourceGateway,
        reference_builder: ReferenceBuilder,
        job_id: str,
        worker_id: str,
        clock: Callable[[], datetime],
        deadline: float,
    ) -> None:
        self.storage = storage
        self.settings = settings
        self.gateway = gateway
        self.reference_builder = reference_builder
        self.job_id = job_id
        self.worker_id = worker_id
        self.clock = clock
        self.deadline = deadline
        self.sources = {source_id: _SourceStatus(source_id) for source_id in SOURCE_ORDER}
        self.current_source: str | None = None
        self.version_id: str | None = None
        self.lease_lost = threading.Event()

    # -- progress ------------------------------------------------------------------------------

    def sources_payload(self) -> list[dict[str, Any]]:
        return [status.as_dict() for status in self.sources.values()]

    def beat(self, stage: str | None = None) -> None:
        if self.lease_lost.is_set() or not self.storage.heartbeat_refresh_job(
            self.job_id,
            self.worker_id,
            self.settings.refresh_lease_seconds,
            self.clock(),
            stage=stage,
            sources=self.sources_payload(),
        ):
            raise LeaseLostError("The refresh job lost its lease.")
        if time.monotonic() > self.deadline:
            raise RefreshError("The refresh exceeded its time budget.")

    # -- main flow -------------------------------------------------------------------------------

    def keep_alive(self, stop: threading.Event) -> None:
        """Extend the lease while a single download or parse outlasts the time between beats."""
        interval = max(1.0, self.settings.refresh_lease_seconds / 3)
        while not stop.wait(interval):
            try:
                renewed = self.storage.heartbeat_refresh_job(
                    self.job_id, self.worker_id, self.settings.refresh_lease_seconds, self.clock()
                )
            except Exception:
                logger.warning("refresh_keep_alive_failed", extra={"job_id": self.job_id})
                continue
            if not renewed:
                self.lease_lost.set()
                return

    def run(self) -> None:
        stop = threading.Event()
        keeper = threading.Thread(target=self.keep_alive, args=(stop,), name="refresh-lease", daemon=True)
        keeper.start()
        try:
            self._run_stages()
        finally:
            stop.set()
            keeper.join(timeout=5)

    def _run_stages(self) -> None:
        try:
            # Only one job can be active, so files still `ingesting` now belong to a dead run.
            self.storage.cleanup_incomplete_data(self.clock(), ignore_job_id=self.job_id)
            self.beat("discovering")
            plan = self.discover()
            self.beat("fetching")
            files = self.fetch_all(plan)
            self.current_source = None
            self.beat("validating")
            coverage, quality = self.validate(plan, files)
            file_ids = sorted(file.file_id for file in files)
            active = self.storage.get_active_data_version_id()
            if active is not None and {f.file_id for f in self.storage.data_version_files(active)} == set(
                file_ids
            ):
                self.finish_unchanged(active)
                return
            self.build_and_activate(plan, files, coverage, quality)
        except LeaseLostError:
            logger.warning("refresh_lease_lost", extra={"job_id": self.job_id})
            self.fail_version()
            # Finishing only succeeds while this worker still owns the job; otherwise it is a no-op.
            self.storage.finish_refresh_job(
                self.job_id,
                self.worker_id,
                status="failed",
                now=self.clock(),
                changed=False,
                sources=self.sources_payload(),
                error={
                    "code": "lease_lost",
                    "message": "The refresh stopped because it lost ownership of the job. "
                    "The previous data version stays active.",
                },
            )
        except Exception as exc:
            self.fail(exc)

    def finish_unchanged(self, active: str) -> None:
        self.storage.finish_refresh_job(
            self.job_id,
            self.worker_id,
            status="completed",
            now=self.clock(),
            changed=False,
            version_id=active,
            sources=self.sources_payload(),
        )
        logger.info("refresh_unchanged", extra={"job_id": self.job_id, "version_id": active})

    def build_and_activate(
        self, plan: _Plan, files: list[DataFileRecord], coverage: dict[str, Any], quality: dict[str, Any]
    ) -> None:
        self.beat("computing_references")
        now = self.clock()
        version_id = f"dv-{now:%Y%m%d%H%M%S}-{new_id()[:8]}"
        coverage["version_id"] = version_id
        manifest = {
            "job_id": self.job_id,
            "created_at": to_iso(now),
            "targets": {"taf_edition": plan.taf_edition, "calendar_years": plan.years},
            "files": [
                {
                    "file_id": f.file_id,
                    "source_id": f.source_id,
                    "period_key": f.period_key,
                    "sha256": f.sha256,
                    "dataset_version": f.dataset_version,
                    "retrieved_at": to_iso(f.retrieved_at),
                    "row_count": f.row_count,
                    "local_path": f.local_path,
                    "notes": f.notes,
                }
                for f in sorted(files, key=lambda f: (f.source_id, f.period_key))
            ],
            "quality": quality,
        }
        self.storage.create_data_version(
            version_id=version_id,
            job_id=self.job_id,
            file_ids=[f.file_id for f in files],
            manifest=manifest,
            coverage=coverage,
            now=now,
        )
        self.version_id = version_id
        repository = cast(
            DataRepository, _HeartbeatRepository(SqliteDataRepository(self.storage, version_id), self.beat)
        )
        try:
            references = self.reference_builder(repository)
        except (ValueError, ArithmeticError) as exc:  # e.g. a benchmark outside its plausible range
            raise RefreshError(f"The national references could not be computed: {exc}") from exc
        self.storage.save_national_references(
            version_id, [reference.model_dump(mode="json") for reference in references]
        )
        if not self.storage.set_data_version_status(version_id, "ready"):
            raise RefreshError("The new data version could not be prepared for activation.")
        self.beat("activating")
        if not self.storage.activate_data_version(
            version_id, self.job_id, self.worker_id, self.clock(), sources=self.sources_payload()
        ):
            raise LeaseLostError("The job lost its lease before activation.")
        logger.info("refresh_activated", extra={"job_id": self.job_id, "version_id": version_id})

    def fail_version(self) -> None:
        if self.version_id is not None:
            self.storage.set_data_version_status(self.version_id, "failed")

    def fail(self, exc: BaseException) -> None:
        if isinstance(exc, RefreshError | DownloadError | SourceFormatError):
            message = str(exc)
            logger.warning("refresh_failed", extra={"job_id": self.job_id, "reason": message})
        else:
            message = "An unexpected error stopped the refresh."
            logger.exception("refresh_crashed", extra={"job_id": self.job_id})
        if self.current_source is not None:
            status = self.sources[self.current_source]
            status.status = "failed"
            status.checked_at = self.clock()
            status.message = message
        self.fail_version()
        outcome = (
            "The previous data version stays active."
            if self.storage.get_active_data_version_id() is not None
            else "No data version is available yet."
        )
        self.storage.finish_refresh_job(
            self.job_id,
            self.worker_id,
            status="failed",
            now=self.clock(),
            changed=False,
            sources=self.sources_payload(),
            error={"code": "refresh_failed", "message": f"{message} {outcome}"},
        )

    # -- discovery ---------------------------------------------------------------------------------

    def discover(self) -> _Plan:
        today: date = self.clock().date()
        self.current_source = taf.SOURCE_ID
        found: tuple[int, RemoteMeta] | None = None
        for edition in (today.year + 1, today.year, today.year - 1):
            meta = self.gateway.taf_edition_meta(edition)
            if meta is not None:
                found = (edition, meta)
                break
        if found is None:
            raise RefreshError("No FAA TAF edition was found for the current or previous year.")
        self.current_source = t100_summary.SOURCE_ID
        latest = self.gateway.summary_latest_month()
        t100_year = latest[0] if latest[1] == 12 else latest[0] - 1
        self.current_source = ontime.SOURCE_ID
        ontime_year = next(
            (
                year
                for year in range(t100_year, t100_year - 3, -1)
                if self.gateway.ontime_month_meta(year, 12)
            ),
            None,
        )
        if ontime_year is None:
            raise RefreshError("No complete calendar year of On-Time Performance data was found.")
        target = min(t100_year, ontime_year)
        years = list(range(target - max(1, self.settings.data_history_years) + 1, target + 1))
        self.current_source = None
        logger.info(
            "refresh_plan",
            extra={"job_id": self.job_id, "taf_edition": found[0], "years": years, "summary_latest": latest},
        )
        return _Plan(
            taf_edition=found[0], taf_fingerprint=found[1].fingerprint(), years=years, summary_latest=latest
        )

    # -- fetching and ingestion ----------------------------------------------------------------------

    def fetch_all(self, plan: _Plan) -> list[DataFileRecord]:
        """One file per source and period. The slow T-100 form downloads run last, so a failure
        there leaves every other file ingested and reusable by the next run."""
        files: list[DataFileRecord] = []
        labels = [f"CY{year}" for year in plan.years]

        self.current_source = taf.SOURCE_ID
        self.sources[taf.SOURCE_ID].periods = [taf.dataset_version(plan.taf_edition)]
        files.append(
            self.ensure_file(
                taf.SOURCE_ID,
                str(plan.taf_edition),
                plan.taf_fingerprint,
                partial(self.gateway.download_taf, plan.taf_edition),
                partial(taf.parse_taf_zip, edition_year=plan.taf_edition),
            )
        )

        self.current_source = t100_summary.SOURCE_ID
        self.sources[t100_summary.SOURCE_ID].periods = labels
        summaries: dict[int, FetchedFile] = {}
        for year in plan.years:
            summaries[year] = self.gateway.fetch_summary_year(year)
            files.append(
                self.ensure_file(
                    t100_summary.SOURCE_ID,
                    str(year),
                    summaries[year].remote_fingerprint,
                    partial(_same, summaries[year]),
                    partial(t100_summary.parse_summary_file, year=year),
                )
            )

        self.current_source = ontime.SOURCE_ID
        self.sources[ontime.SOURCE_ID].periods = labels
        for year in plan.years:
            for month in range(1, 13):
                meta = self.gateway.ontime_month_meta(year, month)
                if meta is None:
                    raise CoverageError(
                        f"BTS has not published On-Time Performance data for {year}-{month:02d}."
                    )
                files.append(
                    self.ensure_file(
                        ontime.SOURCE_ID,
                        ontime.period_key(year, month),
                        meta.fingerprint(),
                        partial(self.gateway.download_ontime_month, year, month),
                        partial(ontime.parse_ontime_zip, year=year, month=month),
                    )
                )

        self.current_source = t100.SOURCE_ID
        self.sources[t100.SOURCE_ID].periods = labels
        for year in plan.years:
            fingerprint = summaries[year].remote_fingerprint
            files.append(
                self.ensure_file(
                    t100.SOURCE_ID,
                    str(year),
                    fingerprint,
                    partial(self._download_t100, year, fingerprint),
                    partial(t100.parse_t100_zip, year=year),
                )
            )
        for status in self.sources.values():
            if status.status == "pending":
                status.status = "updated" if status.updated else "unchanged"
        return files

    def ensure_file(
        self,
        source_id: str,
        period_key: str,
        fingerprint: str | None,
        fetch: Callable[[], FetchedFile],
        parse: Callable[[Path], ParsedFile],
    ) -> DataFileRecord:
        """Reuse the current file when unchanged; otherwise download, deduplicate by sha256 and ingest."""
        self.beat()
        existing = self.storage.latest_ready_file(source_id, period_key)
        if (
            existing is not None
            and fingerprint is not None
            and existing.remote_fingerprint == fingerprint
            and (self.settings.data_dir / existing.local_path).is_file()
        ):
            self.storage.touch_data_file(existing.file_id, self.clock())
            return self._record(source_id, existing, updated=False)

        fetched = fetch()
        self.beat()
        match = self.storage.find_data_file(source_id, period_key, fetched.sha256)
        if match is not None and match.status == "ready":
            self.storage.touch_data_file(
                match.file_id, self.clock(), fetched.remote_fingerprint or fingerprint
            )
            return self._record(source_id, self._reload(match.file_id), updated=False)

        file_id = self.storage.begin_data_file(
            source_id=source_id,
            period_key=period_key,
            url=fetched.url,
            sha256=fetched.sha256,
            size_bytes=fetched.size_bytes,
            local_path=self._relative(fetched.path),
            dataset_version=fetched.dataset_version,
            retrieved_at=fetched.retrieved_at,
            remote_fingerprint=fetched.remote_fingerprint or fingerprint,
        )
        try:
            parsed = parse(fetched.path)
            self.beat()
            rows = self._insert(file_id, parsed)
            self.storage.finish_data_file(file_id, row_count=rows, notes=parsed.notes)
        except BaseException:
            self.storage.fail_data_file(file_id)
            raise
        logger.info(
            "refresh_file_ingested",
            extra={"job_id": self.job_id, "source_id": source_id, "period": period_key, "rows": rows},
        )
        return self._record(source_id, self._reload(file_id), updated=True)

    def _download_t100(self, year: int, fingerprint: str | None) -> FetchedFile:
        return _with_fingerprint(self.gateway.download_t100_year(year), fingerprint)

    def _insert(self, file_id: str, parsed: ParsedFile) -> int:
        total = 0
        for table in parsed.tables:
            frame = table.frame
            columns = [str(column) for column in frame.columns]
            for start in range(0, len(frame), INSERT_BATCH_ROWS):
                rows = _records(frame.iloc[start : start + INSERT_BATCH_ROWS])
                total += self.storage.insert_file_rows(table.table, file_id, columns, rows)
                self.beat()
        return total

    def _reload(self, file_id: str) -> DataFileRecord:
        record = self.storage.get_data_file(file_id)
        if record is None:
            raise RefreshError("A source file record disappeared during the refresh.")
        return record

    def _relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.settings.data_dir.resolve()).as_posix()
        except ValueError as exc:
            raise RefreshError("A downloaded file was stored outside the data directory.") from exc

    def _record(self, source_id: str, file: DataFileRecord, *, updated: bool) -> DataFileRecord:
        status = self.sources[source_id]
        status.checked_at = self.clock()
        if status.retrieved_at is None or file.retrieved_at > status.retrieved_at:
            status.retrieved_at = file.retrieved_at
        if updated:
            status.updated += 1
        else:
            status.unchanged += 1
        total = status.updated + status.unchanged
        status.message = f"{total} file(s) checked: {status.updated} updated, {status.unchanged} unchanged."
        return file

    # -- validation ---------------------------------------------------------------------------------

    def validate(self, plan: _Plan, files: list[DataFileRecord]) -> tuple[dict[str, Any], dict[str, Any]]:
        by_source: dict[str, dict[str, DataFileRecord]] = {}
        for file in files:
            by_source.setdefault(file.source_id, {})[file.period_key] = file
        coverage = DataCoverage(version_id="pending")
        quality: dict[str, Any] = {"t100_summary_cross_check": {}}

        for year in plan.years:
            segment_file = by_source[t100.SOURCE_ID][str(year)]
            months = self.storage.file_year_months("t100_segments", segment_file.file_id).get(year, [])
            if months != list(range(1, 13)):
                raise CoverageError(f"T-100 Segment data for {year} covers months {months} instead of 1-12.")
            coverage.t100_months[year] = months
            summary_file = by_source[t100_summary.SOURCE_ID][str(year)]
            summary_months = self.storage.file_year_months("t100_summary_month", summary_file.file_id).get(
                year, []
            )
            if summary_months != list(range(1, 13)):
                raise CoverageError(
                    f"The T-100 summary for {year} covers months {summary_months} instead of 1-12."
                )
            coverage.t100_summary_months[year] = summary_months
            check = cross_check_t100(
                self.storage.t100_origin_month_sums(segment_file.file_id),
                self.storage.t100_summary_rows(summary_file.file_id),
            )
            quality["t100_summary_cross_check"][str(year)] = check
            self.beat()

            ontime_months = []
            for month in range(1, 13):
                file = by_source[ontime.SOURCE_ID][ontime.period_key(year, month)]
                if self.storage.file_year_months("otp_departures_month", file.file_id).get(year) != [month]:
                    raise CoverageError(
                        f"The On-Time file for {year}-{month:02d} does not contain that month."
                    )
                ontime_months.append(month)
            coverage.ontime_months[year] = ontime_months

        taf_file = by_source[taf.SOURCE_ID][str(plan.taf_edition)]
        fiscal = self.storage.taf_fiscal_years(taf_file.file_id)
        historical = [year for year, forecast, _ in fiscal if not forecast]
        forecast_years = {year for year, forecast, _ in fiscal if forecast}
        if not historical:
            raise CoverageError("The TAF edition has no historical fiscal years.")
        base_year = max(historical)
        needed = set(range(base_year + 1, base_year + self.settings.forecast_horizon_years + 1))
        if not needed <= forecast_years:
            raise CoverageError(f"The TAF edition does not forecast FY{base_year + 1}-FY{max(needed)}.")
        coverage.taf_edition = taf.edition_label(plan.taf_edition)
        coverage.taf_base_year = base_year
        coverage.taf_last_year = max(year for year, _, _ in fiscal)

        self.sources[taf.SOURCE_ID].periods = [f"FY{min(historical)}-FY{coverage.taf_last_year}"]
        taf_status = self.sources[taf.SOURCE_ID]
        taf_status.message = (
            f"{taf.dataset_version(plan.taf_edition)}, base year FY{base_year}; "
            f"{'updated' if taf_status.updated else 'unchanged'}."
        )
        summary_status = self.sources[t100_summary.SOURCE_ID]
        mismatches = sum(
            int(check["value_mismatches"]) for check in quality["t100_summary_cross_check"].values()
        )
        summary_status.message = (
            f"Latest published month {plan.summary_latest[0]}-{plan.summary_latest[1]:02d}; "
            f"cross-check with T-100 Segment: {mismatches} US origin-month value mismatches."
        )
        for year, check in quality["t100_summary_cross_check"].items():
            segment_file = by_source[t100.SOURCE_ID][year]
            notes = [note for note in segment_file.notes if not note.startswith(CROSS_CHECK_PREFIX)]
            notes.append(cross_check_note(check))
            self.storage.update_data_file_notes(segment_file.file_id, notes)
        return coverage.model_dump(mode="json"), quality


CROSS_CHECK_PREFIX = "Summary cross-check:"


def cross_check_t100(segment_rows: list[Any], summary_rows: list[Any]) -> dict[str, Any]:
    """Compare T-100 Segment sums per origin and month with the BTS summary API totals.

    The summary API covers US origin airports only, so segment rows departing foreign airports
    are counted separately instead of as missing from the summary.
    """
    metrics = ["departures", "passengers", "seats"]
    segments = pd.DataFrame(
        [tuple(row) for row in segment_rows], columns=["airport_id", "month", *metrics, "origin_country"]
    )
    summary = pd.DataFrame(
        [tuple(row) for row in summary_rows], columns=["airport_id", "code", "month", *metrics]
    )
    merged = segments.merge(
        summary, on=["airport_id", "month"], how="outer", suffixes=("_segment", "_summary"), indicator=True
    )
    both = merged[merged["_merge"] == "both"]
    differs = pd.Series(False, index=both.index)
    for metric in metrics:
        left = pd.to_numeric(both[f"{metric}_segment"], errors="coerce")
        right = pd.to_numeric(both[f"{metric}_summary"], errors="coerce")
        differs |= ((left - right).abs() > 0.5) | (left.isna() != right.isna())
    only_segments = merged[merged["_merge"] == "left_only"]
    foreign = only_segments["origin_country"].fillna("") != "US"
    examples = [
        {
            "airport_id": int(row["airport_id"]),
            "code": row["code"],
            "month": int(row["month"]),
            **{f"{metric}_segment": _plain(row[f"{metric}_segment"]) for metric in metrics},
            **{f"{metric}_summary": _plain(row[f"{metric}_summary"]) for metric in metrics},
        }
        for row in both[differs].head(5).to_dict("records")
    ]
    return {
        "compared_origin_months": len(both),
        "value_mismatches": int(differs.sum()),
        "foreign_origin_months": int(foreign.sum()),  # outside the summary's scope
        "us_origin_months_missing_from_summary": int((~foreign).sum()),
        "only_in_summary": int((merged["_merge"] == "right_only").sum()),
        "examples": examples,
    }


def cross_check_note(check: dict[str, Any]) -> str:
    return (
        f"{CROSS_CHECK_PREFIX} {check['compared_origin_months']:,} US origin-months compared with the BTS "
        f"summary API, {check['value_mismatches']:,} with different departures, passengers or seats; "
        f"{check['us_origin_months_missing_from_summary']:,} US origin-months missing from the summary, "
        f"{check['only_in_summary']:,} only in the summary; "
        f"{check['foreign_origin_months']:,} foreign-origin months are outside the summary's scope."
    )


def _plain(value: Any) -> float | None:
    number = pd.to_numeric(value, errors="coerce")
    return None if pd.isna(number) else float(number)


def _with_fingerprint(fetched: FetchedFile, fingerprint: str | None) -> FetchedFile:
    return FetchedFile(
        path=fetched.path,
        sha256=fetched.sha256,
        size_bytes=fetched.size_bytes,
        url=fetched.url,
        retrieved_at=fetched.retrieved_at,
        remote_fingerprint=fingerprint,
        dataset_version=fetched.dataset_version,
    )


def _same(value: FetchedFile) -> FetchedFile:
    return value


def _records(frame: pd.DataFrame) -> list[tuple[Any, ...]]:
    """Rows of plain Python values for sqlite3 (numpy scalars converted, NaN/NA as None)."""
    columns = [
        frame[column].astype(object).where(frame[column].notna(), None).tolist() for column in frame.columns
    ]
    return list(zip(*columns, strict=True))


class _HeartbeatRepository:
    """Delegating repository that extends the job lease before every query of the reference builder."""

    def __init__(self, repository: DataRepository, beat: Callable[[], None]) -> None:
        self._repository = repository
        self._beat = beat

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._repository, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            self._beat()
            return attribute(*args, **kwargs)

        return call
