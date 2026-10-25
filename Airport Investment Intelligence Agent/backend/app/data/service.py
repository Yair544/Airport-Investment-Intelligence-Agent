"""Normalized data access contracts.

The analysis layer depends only on the protocols in this module. A `DataRepository` is pinned to
one immutable data version, so a refresh never changes data in the middle of a calculation.
BTS tables are joined on the numeric BTS airport id from the verified reference mapping and TAF
tables on the FAA location identifier; analysis code never matches airports by code similarity.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Literal, Protocol

import pandas as pd
from pydantic import BaseModel, Field

from app.analysis.models import (
    AirportRef,
    ConstraintRecord,
    EvidenceRecord,
    NationalReference,
    ResolutionOutcome,
    ResolvedRegion,
    SourceId,
    SourceRef,
)
from app.storage import T100_SEGMENT_COLUMNS, DataFileRecord, Storage

TaxiKind = Literal["taxi_out", "taxi_in"]


class DataUnavailableError(Exception):
    """Raised when no usable data version exists or a requested version is unknown."""


class DataCoverage(BaseModel):
    """What one data version contains. Serialized into `data_versions.coverage_json`."""

    version_id: str
    activated_at: datetime | None = None
    t100_months: dict[int, list[int]] = Field(default_factory=dict)  # calendar year -> months
    ontime_months: dict[int, list[int]] = Field(default_factory=dict)  # calendar year -> months
    t100_summary_months: dict[int, list[int]] = Field(default_factory=dict)
    taf_edition: str | None = None  # e.g. "TAF 2025"
    taf_base_year: int | None = None  # last historical fiscal year in the edition
    taf_last_year: int | None = None  # last forecast fiscal year in the edition

    def has_months(
        self, source: Literal["bts_t100_segment", "bts_ontime"], year: int, months: Sequence[int]
    ) -> bool:
        available = (self.t100_months if source == "bts_t100_segment" else self.ontime_months).get(year, [])
        return set(months).issubset(available)

    def full_years(self, source: Literal["bts_t100_segment", "bts_ontime"]) -> list[int]:
        table = self.t100_months if source == "bts_t100_segment" else self.ontime_months
        return sorted(year for year, months in table.items() if len(set(months)) == 12)

    def latest_full_year(self) -> int | None:
        """Latest calendar year with all 12 months in both T-100 and On-Time (spec section 4.1)."""
        common = set(self.full_years("bts_t100_segment")) & set(self.full_years("bts_ontime"))
        return max(common) if common else None


class DataRepository(Protocol):
    """Read access to one immutable data version. All DataFrames use the documented columns."""

    @property
    def version_id(self) -> str: ...

    def coverage(self) -> DataCoverage: ...

    def taf_annual(self, faa_locids: Sequence[str] | None = None) -> pd.DataFrame:
        """TAF rows for the given airports, or for every airport in the edition when None.

        Columns: faa_locid (str), fiscal_year (int), is_forecast (bool),
        total_enplanements (float), commercial_operations (float).
        total_enplanements = air carrier + air taxi + commuter + US flag + foreign flag enplanements;
        commercial_operations = itinerant air carrier + itinerant air taxi/commuter operations.
        """
        ...

    def t100_segments(
        self,
        origin_airport_ids: Sequence[int],
        year: int,
        months: Sequence[int],
        service_classes: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        """T-100 segment rows departing the given BTS airport ids.

        Columns: year, month, carrier, origin_airport_id, origin, origin_country,
        dest_airport_id, dest, dest_country, dest_city_name, service_class, aircraft_config,
        aircraft_type,
        departures_scheduled, departures_performed, seats, passengers, freight, distance,
        distance_group. Numeric columns are floats; a missing distance stays NaN.
        """
        ...

    def t100_origin_totals(
        self,
        year: int,
        months: Sequence[int],
        service_classes: Sequence[str] | None = None,
        origin_country: str | None = None,
    ) -> pd.DataFrame:
        """Sums per origin airport across the whole source (used for national references).

        Columns: origin_airport_id, origin, departures_performed, seats, passengers,
        months_present (number of distinct months with rows).
        """
        ...

    def ontime_departures(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> pd.DataFrame:
        """Monthly departure aggregates per origin airport (None = all airports).

        Columns: airport_id, year, month, days_in_month, scheduled, cancelled, diverted,
        del15_valid (non-cancelled departures with a valid DepDel15), del15_count.
        """
        ...

    def ontime_arrivals(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> pd.DataFrame:
        """Monthly arrival aggregates per destination airport (None = all airports).

        Columns: airport_id, year, month, arrivals, delay_carrier, delay_weather, delay_nas,
        delay_security, delay_late_aircraft (attributed delay minutes, summed).
        """
        ...

    def ontime_taxi_histogram(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int], kind: TaxiKind
    ) -> pd.DataFrame:
        """Integer-minute histogram summed over the requested months.

        taxi_out counts non-cancelled departures with a valid TaxiOut at the origin airport;
        taxi_in counts arrivals with a valid TaxiIn at the destination airport.
        Columns: airport_id, minutes (int), flights (int).
        """
        ...

    def national_reference(self, metric: str, period_key: str) -> NationalReference | None:
        """National benchmark stored with this data version, or None when it was not computed."""
        ...

    def source_ref(self, source_id: SourceId, period_label: str | None = None) -> SourceRef:
        """Provenance for a source in this version: URL, latest retrieval time, dataset version."""
        ...


class ReferenceCatalog(Protocol):
    """Version-controlled reference records: airports, aliases, regions, constraints, evidence."""

    @property
    def reference_version(self) -> str:
        """Content signature of the reference files, carried with analysis results."""
        ...

    def airport(self, airport_id: str) -> AirportRef | None: ...

    def airports(self) -> list[AirportRef]: ...

    def resolve(self, query: str, context_airport_ids: Sequence[str] = ()) -> ResolutionOutcome:
        """Resolve a code, name, alias (Hebrew or English), city, state or region."""
        ...

    def region(self, region_id: str) -> ResolvedRegion | None:
        """A region or state by the id that `resolve` returns."""
        ...

    def airports_in_states(self, states: Iterable[str]) -> list[AirportRef]: ...

    def constraints_for(self, airport_id: str) -> list[ConstraintRecord]: ...

    def evidence_for(self, airport_id: str) -> list[EvidenceRecord]: ...


class DataService(Protocol):
    """Entry point used by the analysis layer and the API."""

    @property
    def reference(self) -> ReferenceCatalog: ...

    def active_version_id(self) -> str | None: ...

    def repository(self, version_id: str | None = None) -> DataRepository:
        """Repository pinned to `version_id`, or to the active version when None.

        Raises DataUnavailableError when there is no active version or the id is unknown.
        """
        ...


# ---------------------------------------------------------------------------------------------
# SQLite implementation
# ---------------------------------------------------------------------------------------------

SOURCE_INFO: dict[str, tuple[str, str]] = {
    "faa_taf": ("FAA Terminal Area Forecast (TAF)", "https://taf.faa.gov/"),
    "bts_t100_segment": (
        "BTS T-100 Segment (All Carriers)",
        "https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FMG",
    ),
    "bts_ontime": (
        "BTS Reporting Carrier On-Time Performance",
        "https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ",
    ),
    "bts_t100_summary": (
        "BTS T-100 Segment Summary by Origin Airport (API)",
        "https://data.bts.gov/resource/r495-tyji.json",
    ),
}
CURATED_SOURCES: dict[str, str] = {
    "constraints_table": "Documented constraints outside the terminal (curated reference table)",
    "evidence_records": "Official demand and infrastructure evidence (curated reference records)",
}
SERVABLE_VERSION_STATUSES = ("active", "superseded")
_CALENDAR_YEAR = re.compile(r"CY(\d{4})")
_YEAR_MONTH = re.compile(r"(\d{4})-(\d{2})")


def _period_keys(source_id: str, period_label: str | None) -> set[str] | None:
    """Period keys of the files a period label refers to (None means every file of the source)."""
    if period_label is None or source_id == "faa_taf":
        return None
    months = [(int(year), int(month)) for year, month in _YEAR_MONTH.findall(period_label)]
    years = [int(year) for year in _CALENDAR_YEAR.findall(period_label)]
    if months and source_id == "bts_ontime":
        (first_year, first_month), (last_year, last_month) = months[0], months[-1]
        first = first_year * 12 + first_month - 1
        last = last_year * 12 + last_month - 1
        return {f"{index // 12:04d}-{index % 12 + 1:02d}" for index in range(first, last + 1)}
    if months:
        years = [year for year, _ in months]
    if not years:
        return None
    span = range(min(years), max(years) + 1)
    if source_id == "bts_ontime":
        return {f"{year:04d}-{month:02d}" for year in span for month in range(1, 13)}
    return {str(year) for year in span}


def _frame(
    rows: Sequence[sqlite3.Row],
    columns: Sequence[str],
    *,
    ints: Sequence[str] = (),
    floats: Sequence[str] = (),
    bools: Sequence[str] = (),
) -> pd.DataFrame:
    """Measures become float64 (missing = NaN); integer codes int64, or float64 if any is missing."""
    frame = pd.DataFrame.from_records([tuple(row) for row in rows], columns=list(columns))
    for column in floats:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").astype("float64")
    for column in ints:
        values = pd.to_numeric(frame[column], errors="coerce")
        frame[column] = values.astype("int64") if values.notna().all() else values.astype("float64")
    for column in bools:
        frame[column] = frame[column].astype(bool)
    return frame


class SqliteDataRepository:
    """DataRepository pinned to one data version; every query is scoped to that version's files."""

    def __init__(self, storage: Storage, version_id: str) -> None:
        record = storage.get_data_version(version_id)
        if record is None:
            raise DataUnavailableError(f"Data version {version_id} does not exist.")
        self._storage = storage
        self._version = record
        self._files: dict[str, list[DataFileRecord]] = {}
        for file in storage.data_version_files(version_id):
            self._files.setdefault(file.source_id, []).append(file)

    @property
    def version_id(self) -> str:
        return self._version.version_id

    def _file_ids(self, source_id: str, period_keys: set[str] | None = None) -> list[str]:
        return [
            file.file_id
            for file in self._files.get(source_id, [])
            if period_keys is None or file.period_key in period_keys
        ]

    def _ontime_file_ids(self, year: int, months: Sequence[int]) -> list[str]:
        return self._file_ids("bts_ontime", {f"{int(year):04d}-{int(month):02d}" for month in months})

    def coverage(self) -> DataCoverage:
        payload = dict(self._version.coverage or {})
        payload["version_id"] = self.version_id
        payload["activated_at"] = self._version.activated_at
        return DataCoverage.model_validate(payload)

    def taf_annual(self, faa_locids: Sequence[str] | None = None) -> pd.DataFrame:
        locids = None if faa_locids is None else [str(locid) for locid in faa_locids]
        rows = self._storage.query_taf_annual(self._file_ids("faa_taf"), locids)
        return _frame(
            rows,
            ["faa_locid", "fiscal_year", "is_forecast", "total_enplanements", "commercial_operations"],
            ints=["fiscal_year"],
            floats=["total_enplanements", "commercial_operations"],
            bools=["is_forecast"],
        )

    def t100_segments(
        self,
        origin_airport_ids: Sequence[int],
        year: int,
        months: Sequence[int],
        service_classes: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        rows: list[sqlite3.Row] = []
        if months:
            rows = self._storage.query_t100_segments(
                self._file_ids("bts_t100_segment", {str(year)}),
                [int(airport_id) for airport_id in origin_airport_ids],
                int(year),
                [int(month) for month in months],
                None if service_classes is None else list(service_classes),
            )
        return _frame(
            rows,
            T100_SEGMENT_COLUMNS,
            ints=[
                "year",
                "month",
                "origin_airport_id",
                "dest_airport_id",
                "aircraft_config",
                "aircraft_type",
                "distance_group",
            ],
            floats=[
                "departures_scheduled",
                "departures_performed",
                "seats",
                "passengers",
                "freight",
                "distance",
            ],
        )

    def t100_origin_totals(
        self,
        year: int,
        months: Sequence[int],
        service_classes: Sequence[str] | None = None,
        origin_country: str | None = None,
    ) -> pd.DataFrame:
        rows: list[sqlite3.Row] = []
        if months:
            rows = self._storage.query_t100_origin_totals(
                self._file_ids("bts_t100_segment", {str(year)}),
                int(year),
                [int(month) for month in months],
                None if service_classes is None else list(service_classes),
                origin_country,
            )
        return _frame(
            rows,
            ["origin_airport_id", "origin", "departures_performed", "seats", "passengers", "months_present"],
            ints=["origin_airport_id", "months_present"],
            floats=["departures_performed", "seats", "passengers"],
        )

    def ontime_departures(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> pd.DataFrame:
        ids = None if airport_ids is None else [int(airport_id) for airport_id in airport_ids]
        rows = self._storage.query_ontime_departures(
            self._ontime_file_ids(year, months), ids, int(year), [int(month) for month in months]
        )
        columns = [
            "airport_id",
            "year",
            "month",
            "days_in_month",
            "scheduled",
            "cancelled",
            "diverted",
            "del15_valid",
            "del15_count",
        ]
        return _frame(rows, columns, ints=columns)

    def ontime_arrivals(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> pd.DataFrame:
        ids = None if airport_ids is None else [int(airport_id) for airport_id in airport_ids]
        rows = self._storage.query_ontime_arrivals(
            self._ontime_file_ids(year, months), ids, int(year), [int(month) for month in months]
        )
        delays = ["delay_carrier", "delay_weather", "delay_nas", "delay_security", "delay_late_aircraft"]
        return _frame(
            rows,
            ["airport_id", "year", "month", "arrivals", *delays],
            ints=["airport_id", "year", "month", "arrivals"],
            floats=delays,
        )

    def ontime_taxi_histogram(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int], kind: TaxiKind
    ) -> pd.DataFrame:
        if kind not in ("taxi_out", "taxi_in"):
            raise ValueError(f"Unknown taxi histogram kind: {kind}")
        ids = None if airport_ids is None else [int(airport_id) for airport_id in airport_ids]
        rows = self._storage.query_ontime_taxi_histogram(
            self._ontime_file_ids(year, months), ids, int(year), [int(month) for month in months], kind
        )
        return _frame(rows, ["airport_id", "minutes", "flights"], ints=["airport_id", "minutes", "flights"])

    def national_reference(self, metric: str, period_key: str) -> NationalReference | None:
        row = self._storage.get_national_reference(self.version_id, metric, period_key)
        return None if row is None else NationalReference.model_validate(row)

    def source_ref(self, source_id: SourceId, period_label: str | None = None) -> SourceRef:
        if source_id in CURATED_SOURCES:
            return SourceRef(
                source_id=source_id,
                name=CURATED_SOURCES[source_id],
                url="",
                period=period_label,
                data_version_id=self.version_id,
            )
        name, url = SOURCE_INFO[source_id]
        keys = _period_keys(source_id, period_label)
        files = [f for f in self._files.get(source_id, []) if keys is None or f.period_key in keys]
        dataset_version: str | None = None
        if source_id == "faa_taf":
            dataset_version = next((f.dataset_version for f in files if f.dataset_version), None)
        elif len(files) == 1:
            dataset_version = f"file sha256 {files[0].sha256[:12]}"
        elif files:
            combined = hashlib.sha256("".join(sorted(f.sha256 for f in files)).encode("ascii")).hexdigest()
            dataset_version = f"{len(files)} files, combined sha256 {combined[:12]}"
        return SourceRef(
            source_id=source_id,
            name=name,
            url=url,
            period=period_label,
            retrieved_at=max((f.retrieved_at for f in files), default=None),
            dataset_version=dataset_version,
            data_version_id=self.version_id,
        )


class SqliteDataService:
    """DataService over the SQLite store; repositories are pinned to one immutable version."""

    def __init__(self, storage: Storage, reference: ReferenceCatalog) -> None:
        self._storage = storage
        self._reference = reference

    @property
    def reference(self) -> ReferenceCatalog:
        return self._reference

    def active_version_id(self) -> str | None:
        return self._storage.get_active_data_version_id()

    def repository(self, version_id: str | None = None) -> DataRepository:
        target = version_id or self.active_version_id()
        if target is None:
            raise DataUnavailableError("No aviation data version is available yet. Run a data refresh first.")
        record = self._storage.get_data_version(target)
        if record is None or record.status not in SERVABLE_VERSION_STATUSES:
            raise DataUnavailableError(f"Data version {target} is not available.")
        return SqliteDataRepository(self._storage, target)
