"""SqliteDataRepository / SqliteDataService: queries scoped to one data version's files."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from typing import Any

import pandas as pd
import pytest

from app.data.resolution import load_reference_catalog
from app.data.service import DataCoverage, DataUnavailableError, SqliteDataRepository, SqliteDataService
from app.storage import T100_SEGMENT_COLUMNS, Storage

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
T100_COLUMNS = [
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
]


def add_file(
    storage: Storage,
    source_id: str,
    period_key: str,
    tables: dict[str, tuple[list[str], list[tuple[Any, ...]]]],
    *,
    retrieved_at: datetime = NOW,
    dataset_version: str | None = None,
    tag: str = "",
) -> str:
    sha = hashlib.sha256(f"{source_id}/{period_key}/{tag}".encode()).hexdigest()
    file_id = storage.begin_data_file(
        source_id=source_id,
        period_key=period_key,
        url="https://example.test/file",
        sha256=sha,
        size_bytes=100,
        local_path=f"raw/{source_id}/{period_key}-{sha[:12]}.zip",
        dataset_version=dataset_version,
        retrieved_at=retrieved_at,
        remote_fingerprint=None,
    )
    total = 0
    for table, (columns, rows) in tables.items():
        total += storage.insert_file_rows(table, file_id, columns, rows)
    storage.finish_data_file(file_id, row_count=total, notes=[])
    return file_id


def segment(
    month: int,
    origin_id: int,
    origin: str,
    dest_id: int,
    dest: str,
    departures: float,
    seats: float | None,
    passengers: float | None,
    distance: float | None,
    *,
    service_class: str = "F",
    country: str = "US",
    config: int = 1,
) -> tuple[Any, ...]:
    return (
        2025,
        month,
        "AS",
        origin_id,
        origin,
        country,
        "AK" if country == "US" else None,
        dest_id,
        dest,
        "US",
        "WA",
        "Seattle, WA",
        service_class,
        config,
        614,
        "DU",
        departures,
        departures,
        seats,
        passengers,
        None,
        distance,
        None if distance is None else 3,
    )


def activate(storage: Storage, version_id: str) -> None:
    job, _ = storage.create_or_get_active_refresh_job(NOW)
    assert storage.claim_refresh_job(job.job_id, "worker", 300, NOW)
    assert storage.activate_data_version(version_id, job.job_id, "worker", NOW)


@pytest.fixture
def versions(storage: Storage) -> dict[str, str]:
    storage.ensure_aviation_schema()
    taf = add_file(
        storage,
        "faa_taf",
        "2025",
        {
            "taf_annual": (
                ["faa_locid", "fiscal_year", "is_forecast", "total_enplanements", "commercial_operations"],
                [
                    ("LAX", 2024, 0, 37_806_366.0, 567_181.0),
                    ("LAX", 2029, 1, 41_017_660.0, None),
                    ("SNA", 2024, 0, 5_424_464.0, 100_000.0),
                ],
            )
        },
        dataset_version="TAF 2025 edition",
    )
    t100_rows = [
        segment(1, 10299, "ANC", 14747, "SEA", 30, 5400, 4500, 1448),
        segment(2, 10299, "ANC", 14747, "SEA", 20, 3600, 3000, None),
        segment(1, 10299, "ANC", 10391, "SIN", 18, 0, 0, 6665, service_class="G", config=2),
        segment(1, 16217, "YVR", 10299, "ANC", 5, 900, 800, 1300, country="CA"),
    ]
    t100_a = add_file(
        storage, "bts_t100_segment", "2025", {"t100_segments": (T100_COLUMNS, t100_rows)}, tag="a"
    )
    revised = [segment(1, 10299, "ANC", 14747, "SEA", 60, 10800, 9000, 1448), *t100_rows[1:]]
    t100_b = add_file(
        storage,
        "bts_t100_segment",
        "2025",
        {"t100_segments": (T100_COLUMNS, revised)},
        tag="b",
        retrieved_at=NOW + timedelta(days=1),
    )
    otp: list[str] = []
    for month, retrieved in ((1, NOW - timedelta(days=2)), (2, NOW - timedelta(days=1))):
        otp.append(
            add_file(
                storage,
                "bts_ontime",
                f"2025-{month:02d}",
                {
                    "otp_departures_month": (
                        [
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
                        ],
                        [
                            (12892, "LAX", 2025, month, 31 if month == 1 else 28, 100 * month, 2, 1, 97, 20),
                            (14908, "SNA", 2025, month, 31 if month == 1 else 28, 10, 0, 0, 10, 1),
                        ],
                    ),
                    "otp_arrivals_month": (
                        [
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
                        ],
                        [
                            (12892, "LAX", 2025, month, 99, 50.0, 10.0, 30.0, 0.0, 70.0),
                            (14908, "SNA", 2025, month, 10, None, None, None, None, None),
                        ],
                    ),
                    "otp_taxi_hist": (
                        ["airport_id", "year", "month", "kind", "minutes", "flights"],
                        [
                            (12892, 2025, month, "taxi_out", 15, 60),
                            (12892, 2025, month, "taxi_out", 20, 37),
                            (12892, 2025, month, "taxi_in", 8, 99),
                            (14908, 2025, month, "taxi_out", 11, 10),
                        ],
                    ),
                },
                retrieved_at=retrieved,
            )
        )
    coverage = DataCoverage(
        version_id="x",
        t100_months={2025: [1, 2]},
        ontime_months={2025: [1, 2]},
        taf_edition="TAF 2025",
        taf_base_year=2024,
        taf_last_year=2029,
    ).model_dump(mode="json")
    for version_id, t100_file in (("v1", t100_a), ("v2", t100_b)):
        storage.create_data_version(
            version_id=version_id,
            job_id=None,
            file_ids=[taf, t100_file, *otp],
            manifest={},
            coverage={**coverage, "version_id": version_id},
            now=NOW,
        )
        storage.save_national_references(
            version_id,
            [
                {
                    "metric": "t100_load_factor",
                    "period_key": "CY2025",
                    "value": 83.3,
                    "sample_size": 2,
                    "details": {"airports": 2},
                }
            ],
        )
        storage.set_data_version_status(version_id, "ready")
    activate(storage, "v1")
    activate(storage, "v2")
    return {"taf": taf, "t100_a": t100_a, "t100_b": t100_b}


@pytest.fixture
def service(storage: Storage) -> SqliteDataService:
    return SqliteDataService(storage, load_reference_catalog())


def test_repositories_are_pinned_to_their_version(
    service: SqliteDataService, versions: dict[str, str]
) -> None:
    assert service.active_version_id() == "v2"
    current, previous = service.repository(), service.repository("v1")
    assert current.version_id == "v2" and previous.version_id == "v1"
    jan_current = current.t100_segments([10299], 2025, [1])
    jan_previous = previous.t100_segments([10299], 2025, [1])
    assert jan_current.loc[jan_current["dest"] == "SEA", "departures_performed"].tolist() == [60.0]
    assert jan_previous.loc[jan_previous["dest"] == "SEA", "departures_performed"].tolist() == [30.0]
    assert list(jan_current.columns) == list(T100_SEGMENT_COLUMNS)
    assert jan_current["departures_performed"].dtype == "float64"
    assert jan_current["origin_airport_id"].dtype == "int64"
    assert service.reference is load_reference_catalog()


def test_t100_queries_filter_months_classes_and_keep_missing_values(
    service: SqliteDataService, versions: dict[str, str]
) -> None:
    repo = service.repository("v1")
    rows = repo.t100_segments([10299], 2025, [1, 2], service_classes=["F"])
    assert sorted(rows["month"].tolist()) == [1, 2] and set(rows["service_class"]) == {"F"}
    feb = rows[rows["month"] == 2].iloc[0]
    assert pd.isna(feb["distance"]) and pd.isna(feb["distance_group"]) and pd.isna(feb["freight"])
    assert repo.t100_segments([10299], 2024, [1]).empty
    assert list(repo.t100_segments([10299], 2025, []).columns) == list(T100_SEGMENT_COLUMNS)

    totals = repo.t100_origin_totals(2025, [1, 2], service_classes=["F"], origin_country="US")
    assert totals["origin_airport_id"].tolist() == [10299]  # Canadian origin and class G excluded
    anc = totals.iloc[0]
    assert (anc["departures_performed"], anc["seats"], anc["passengers"], anc["months_present"]) == (
        50.0,
        9000.0,
        7500.0,
        2,
    )
    everything = repo.t100_origin_totals(2025, [1, 2]).set_index("origin_airport_id")
    assert everything.loc[10299, "departures_performed"] == 68.0 and 16217 in everything.index


def test_ontime_queries(service: SqliteDataService, versions: dict[str, str]) -> None:
    repo = service.repository()
    departures = repo.ontime_departures(None, 2025, [1, 2])
    assert len(departures) == 4 and departures["scheduled"].dtype == "int64"
    lax = repo.ontime_departures([12892], 2025, [1])
    assert lax[["scheduled", "cancelled", "del15_valid", "days_in_month"]].values.tolist() == [
        [100, 2, 97, 31]
    ]

    arrivals = repo.ontime_arrivals([14908, 12892], 2025, [2]).set_index("airport_id")
    assert arrivals.loc[12892, "delay_nas"] == 30.0
    assert arrivals.loc[14908, ["delay_carrier", "delay_late_aircraft"]].isna().all()  # missing stays missing

    hist = repo.ontime_taxi_histogram([12892], 2025, [1, 2], "taxi_out")
    assert hist.values.tolist() == [[12892, 15, 120], [12892, 20, 74]]  # summed over the two months
    all_out = repo.ontime_taxi_histogram(None, 2025, [1], "taxi_out")
    assert set(all_out["airport_id"]) == {12892, 14908}
    assert repo.ontime_taxi_histogram([12892], 2025, [3], "taxi_in").empty
    with pytest.raises(ValueError):
        repo.ontime_taxi_histogram([12892], 2025, [1], "taxi_sideways")  # type: ignore[arg-type]


def test_taf_reference_and_coverage(service: SqliteDataService, versions: dict[str, str]) -> None:
    repo = service.repository()
    taf = repo.taf_annual()
    assert list(taf.columns) == [
        "faa_locid",
        "fiscal_year",
        "is_forecast",
        "total_enplanements",
        "commercial_operations",
    ]
    assert taf["is_forecast"].dtype == bool and taf["fiscal_year"].dtype == "int64"
    lax = repo.taf_annual(["LAX"]).set_index("fiscal_year")
    assert lax.loc[2024, "total_enplanements"] == 37_806_366.0 and not lax.loc[2024, "is_forecast"]
    assert pd.isna(lax.loc[2029, "commercial_operations"]) and lax.loc[2029, "is_forecast"]

    reference = repo.national_reference("t100_load_factor", "CY2025")
    assert reference is not None and reference.value == 83.3 and reference.details == {"airports": 2}
    assert repo.national_reference("t100_load_factor", "CY2024") is None

    coverage = repo.coverage()
    assert coverage.version_id == "v2" and coverage.t100_months == {2025: [1, 2]}
    assert coverage.taf_edition == "TAF 2025" and coverage.activated_at is not None
    assert coverage.latest_full_year() is None  # only two months: no full calendar year


def test_source_refs_carry_names_urls_retrieval_and_dataset_version(
    service: SqliteDataService, versions: dict[str, str]
) -> None:
    repo = service.repository("v1")
    taf = repo.source_ref("faa_taf", "FY2024-FY2029")
    assert (taf.name, taf.url) == ("FAA Terminal Area Forecast (TAF)", "https://taf.faa.gov/")
    assert taf.dataset_version == "TAF 2025 edition" and taf.period == "FY2024-FY2029"
    assert taf.retrieved_at == NOW and taf.data_version_id == "v1"

    t100 = repo.source_ref("bts_t100_segment", "CY2025")
    assert t100.name == "BTS T-100 Segment (All Carriers)"
    assert t100.url == "https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FMG"
    assert t100.dataset_version is not None and t100.dataset_version.startswith("file sha256 ")

    january = repo.source_ref("bts_ontime", "2025-01 to 2025-01")
    whole_year = repo.source_ref("bts_ontime", "CY2025")
    assert january.url == "https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ"
    assert january.retrieved_at == NOW - timedelta(days=2)
    assert whole_year.retrieved_at == NOW - timedelta(days=1)  # latest retrieval among the files used
    assert whole_year.dataset_version is not None and whole_year.dataset_version.startswith("2 files")
    assert repo.source_ref("bts_ontime", "CY2023").retrieved_at is None  # not in this version

    summary = repo.source_ref("bts_t100_summary")
    assert summary.url == "https://data.bts.gov/resource/r495-tyji.json" and summary.retrieved_at is None
    assert service.repository("v2").source_ref("bts_t100_segment").retrieved_at == NOW + timedelta(days=1)


def test_unavailable_versions_raise(storage: Storage, service: SqliteDataService) -> None:
    with pytest.raises(DataUnavailableError, match="Run a data refresh"):
        service.repository()
    storage.create_data_version(
        version_id="building", job_id=None, file_ids=[], manifest={}, coverage={}, now=NOW
    )
    with pytest.raises(DataUnavailableError):
        service.repository("building")  # not activated: not served to analyses
    assert SqliteDataRepository(storage, "building").version_id == "building"  # the refresh may read it
    with pytest.raises(DataUnavailableError):
        service.repository("missing")
    with pytest.raises(DataUnavailableError):
        SqliteDataRepository(storage, "missing")
