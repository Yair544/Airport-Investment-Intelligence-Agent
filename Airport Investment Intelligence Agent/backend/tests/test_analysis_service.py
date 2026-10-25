"""AnalysisService on a small fake region with fixed expected values (no database, network or model).

Region design (TAF 2025: base FY2024, horizon FY2029; stored national references: TAF CAGR 2.0%,
load factor 80.0% in CY2025, taxi-out excess 3.0 minutes in CY2025):

| Airport | ΔE      | CAGR  | LF CY2025 | Taxi-out excess   | Other                | Expected group     |
|---------|---------|-------|-----------|-------------------|----------------------|--------------------|
| BOS     | 150,000 | 2.83% | 85%       | 3.5               | terminal evidence    | strong candidate   |
| BDL     | 30,000  | 0.98% | 82%       | 0.8               |                      | watch              |
| PVD     | 60,000  | 2.83% | 85%       | 3.5               | curfew on file       | external constraint|
| MHT     | 45,000  | 2.83% | 70%       | sample of 500     |                      | information needed |
| PWM     | 5,000   | 0.49% | 70%       | 0.8               |                      | low pressure       |
| BTV     | 10,000  | 1.30% | no BTS mapping                                    | information needed |
| LEB     | 500     |       |           |                   | 9,000 enplanements   | excluded           |
| ZZV     |         |       |           |                   | not in TAF           | excluded           |
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pytest

from app.analysis.models import RULES_VERSION, NationalReference
from app.analysis.service import (
    LONG_HAUL_SERVICE_CLASS_LIMITATION,
    NAS_LIMITATION,
    ONTIME_COVERAGE_LIMITATION,
    RANKING_LIMITATIONS,
    UNMET_LIMITATIONS,
    AnalysisInputError,
    AnalysisService,
    NoDataError,
    compute_national_references,
)
from app.config import Settings
from tests.analysis_fakes import (
    ANC_BTS_AIRPORT_ID,
    ARRIVAL_COLUMNS,
    DEPARTURE_COLUMNS,
    T100_COLUMNS,
    TAF_COLUMNS,
    TAXI_COLUMNS,
    FakeDataService,
    FakeReferenceCatalog,
    FakeRepository,
    airport_ref,
    arrival_rows,
    constraint,
    coverage,
    departure_rows,
    evidence,
    frame,
    load_anc_t100_2025,
    t100_rows,
    taf_rows,
    taxi_rows,
)

H1 = {8: 100, 9: 100, 12: 600, 15: 200}  # 1,000 departures: P10 8.5, median 12.0, excess 3.5
H2 = {10: 500, 11: 500}  # 1,000 departures: P10 9.7, median 10.5, excess 0.8
SMALL = {10: 300, 12: 200}  # 500 departures: below the 1,000 minimum for S3

# code: (BTS id, TAF (E FY2024, E FY2029, ops FY2024, ops FY2029),
#        monthly class F passengers per 1,000 seats (CY2024, CY2025), CY2025 taxi-out histogram)
REGION: dict[str, tuple[int | None, tuple[int, int, int, int], tuple[int, int] | None, Any]] = {
    "BOS": (10721, (1_000_000, 1_150_000, 100_000, 110_000), (800, 850), H1),
    "BDL": (10529, (600_000, 630_000, 50_000, 51_000), (800, 820), H2),
    "PVD": (14307, (400_000, 460_000, 40_000, 44_000), (800, 850), H1),
    "MHT": (13296, (300_000, 345_000, 30_000, 33_000), (650, 700), SMALL),
    "PWM": (14252, (200_000, 205_000, 20_000, 20_500), (720, 700), H2),
    "BTV": (None, (150_000, 160_000, 15_000, 15_500), None, None),
    "ORH": (13873, (100_000, 103_000, 10_000, 10_300), (800, 850), SMALL),
    "ACK": (10154, (250_000, 262_500, 25_000, 26_000), (1_000, 1_050), None),
    "LEB": (12568, (9_000, 9_500, 900, 950), None, None),
}
NOT_IN_TAF = {"ZZV": 99999, "ZZU": None}
RANKED_IDS = [f"apt_{code}" for code in ("bos", "bdl", "pvd", "mht", "pwm", "btv", "leb", "zzv")]
STORED_REFERENCES = [
    NationalReference(metric="taf_cagr", period_key="FY2024-FY2029", value=2.0, sample_size=3_000),
    NationalReference(metric="t100_load_factor", period_key="CY2024", value=79.0),
    NationalReference(metric="t100_load_factor", period_key="CY2025", value=80.0),
    NationalReference(
        metric="ontime_taxi_out_excess_median",
        period_key="CY2024",
        value=3.0,
        details={"min_departures": 1_000},
    ),
    NationalReference(
        metric="ontime_taxi_out_excess_median",
        period_key="CY2025",
        value=3.0,
        details={"min_departures": 1_000},
    ),
]


def make_repository(
    version_id: str = "v-2025",
    *,
    t100_years: tuple[int, ...] = (2024, 2025),
    ontime_years: tuple[int, ...] = (2024, 2025),
    stored_references: bool = True,
) -> FakeRepository:
    taf: list[dict[str, Any]] = []
    t100: list[dict[str, Any]] = []
    taxi: list[dict[str, Any]] = []
    arrivals: list[dict[str, Any]] = []
    for code, (bts_id, (e_base, e_horizon, ops_base, ops_horizon), lf, histogram) in REGION.items():
        taf += taf_rows(code, {2024: (e_base, ops_base), 2029: (e_horizon, ops_horizon)})
        if bts_id is not None and lf is not None:
            t100 += t100_rows(bts_id, code, 2024, passengers=lf[0], seats=1_000)
            t100 += t100_rows(bts_id, code, 2025, passengers=lf[1], seats=1_000)
            if code not in ("BOS", "BDL"):
                arrivals += arrival_rows(
                    bts_id,
                    2025,
                    arrivals=500,
                    carrier=100,
                    weather=100,
                    nas=100,
                    security=0,
                    late_aircraft=100,
                )
        if bts_id is not None and histogram is not None:
            taxi += taxi_rows(bts_id, 2025, histogram)
    # Never part of a class F load factor of a US airport: a foreign origin and an all-cargo class.
    t100 += t100_rows(16271, "YYZ", 2025, passengers=100, seats=1_000, origin_country="CA")
    t100 += t100_rows(10721, "BOS", 2025, passengers=0, seats=500, service_class="G")
    taxi += taxi_rows(10721, 2025, {5: 400, 6: 600}, kind="taxi_in")
    taxi += taxi_rows(10529, 2025, {7: 300}, kind="taxi_in")
    departures = departure_rows(
        10721, 2025, scheduled=3_000, cancelled=30, diverted=5, del15_valid=2_970, del15_count=594
    ) + departure_rows(10529, 2025, scheduled=1_000, cancelled=10, del15_valid=990, del15_count=99)
    arrivals += arrival_rows(
        10721, 2025, arrivals=3_000, carrier=1_000, weather=200, nas=600, security=0, late_aircraft=200
    )
    arrivals += arrival_rows(
        10529, 2025, arrivals=1_000, carrier=300, weather=100, nas=400, security=0, late_aircraft=200
    )
    return FakeRepository(
        version_id,
        data_coverage=coverage(version_id, t100_years=t100_years, ontime_years=ontime_years),
        taf=frame(taf, TAF_COLUMNS),
        t100=frame(t100, T100_COLUMNS),
        departures=frame(departures, DEPARTURE_COLUMNS),
        arrivals=frame(arrivals, ARRIVAL_COLUMNS),
        taxi=frame(taxi, TAXI_COLUMNS),
        national_references=STORED_REFERENCES if stored_references else (),
    )


def make_catalog() -> FakeReferenceCatalog:
    airports = [airport_ref(code, spec[0]) for code, spec in REGION.items()]
    airports += [airport_ref(code, bts_id) for code, bts_id in NOT_IN_TAF.items()]
    return FakeReferenceCatalog(
        airports,
        constraints=[constraint("apt_pvd", "curfew")],
        evidence=[evidence("apt_bos", "terminal")],
    )


def make_service(
    settings: Settings, *repositories: FakeRepository, active: str | None = "v-2025"
) -> AnalysisService:
    repos = repositories or (make_repository(),)
    return AnalysisService(FakeDataService(make_catalog(), repos, active), settings)


@pytest.fixture
def service(settings: Settings) -> AnalysisService:
    return make_service(settings)


# -- ranking -------------------------------------------------------------------------------------


def test_ranking_groups_order_and_exclusions(service: AnalysisService) -> None:
    result = service.rank_airports(RANKED_IDS, region_label="New England")
    assert [(r.rank, r.airport.code, r.group, r.rule_id) for r in result.data.rows] == [
        (1, "BOS", "strong_candidate", "R2"),
        (2, "BDL", "watch", "R3"),
        (3, "PVD", "external_constraint", "R1"),
        (4, "MHT", "information_needed", "UNKNOWN"),  # ΔE 45,000 before BTV's 10,000
        (5, "BTV", "information_needed", "UNKNOWN"),
        (6, "PWM", "low_pressure", "R4"),
    ]
    assert [(item.airport.code, item.reason) for item in result.data.excluded] == [
        ("LEB", "FY2024 enplanements below 10,000 (9,000 in TAF 2025)."),
        ("ZZV", "Not in the TAF 2025 edition."),
    ]


def test_ranking_row_values_and_flags(service: AnalysisService) -> None:
    rows = {row.airport.code: row for row in service.rank_airports(RANKED_IDS).data.rows}
    bos = rows["BOS"]
    assert (bos.base_year_enplanements, bos.horizon_enplanements, bos.delta_enplanements) == (
        1_000_000,
        1_150_000,
        150_000,
    )
    assert bos.cagr_5 == pytest.approx(100 * (1.15**0.2 - 1))
    assert (
        bos.base_year_commercial_operations,
        bos.horizon_commercial_operations,
        bos.delta_commercial_operations,
    ) == (100_000, 110_000, 10_000)
    s1, s2, s3 = bos.signals
    assert (s1.signal, s1.status, s1.reference) == ("S1", "pass", 2.0)
    assert (s2.signal, s2.value, s2.reference, s2.status, s2.sample_size) == ("S2", 85.0, 80.0, "pass", 1_200)
    assert (s3.signal, s3.value, s3.reference, s3.status, s3.sample_size) == ("S3", 3.5, 3.0, "pass", 1_000)
    assert (bos.evidence_status, bos.further_review, bos.notes) == ("available", False, [])
    assert bos.group_label == "Strong candidate"

    bdl = rows["BDL"]
    assert (bdl.evidence_status, bdl.further_review) == ("missing", True)
    assert bdl.notes == ["Candidate for further review: no terminal capacity evidence on file"]

    pvd = rows["PVD"]
    assert [record.constraint_type for record in pvd.constraints] == ["curfew"]
    assert (pvd.further_review, pvd.possible_groups) == (False, [])

    mht = rows["MHT"]
    assert mht.possible_groups == ["strong_candidate", "watch"]
    assert mht.rule.endswith("S3 unknown: pass → Strong candidate, fail → Watch.")
    assert (mht.signals[2].status, mht.signals[2].sample_size) == ("unknown", 500)

    btv = rows["BTV"]
    assert [signal.status for signal in btv.signals] == ["fail", "unknown", "unknown"]
    assert btv.possible_groups == ["watch", "low_pressure"]
    assert btv.signals[1].note == "No verified BTS airport mapping, so T-100 data cannot be joined."


def test_ranking_metadata_periods_sources_and_limitations(service: AnalysisService) -> None:
    result = service.rank_airports(RANKED_IDS, region_label="New England")
    assert result.analysis_type == "ranking"
    assert result.title == "Expansion candidate ranking: New England"
    assert (result.data_version_id, result.reference_version, result.rules_version) == (
        "v-2025",
        "ref-test",
        RULES_VERSION,
    )
    assert result.defaults_applied == ["year", "horizon_years"]
    parameters = result.parameters
    assert (parameters["year"], parameters["horizon_years"], parameters["base_year"]) == (2025, 5, 2024)
    assert (parameters["horizon_year"], parameters["region_label"]) == (2029, "New England")
    assert parameters["airport_codes"] == ["BOS", "BDL", "PVD", "MHT", "PWM", "BTV", "LEB", "ZZV"]
    assert parameters["primary_airport_min_enplanements"] == 10_000
    assert parameters["s3_min_departures"] == 1_000
    assert [(p.label, p.kind, p.nature, p.source_id, p.role) for p in result.periods] == [
        ("CY2025", "calendar_year", "historical", "bts_t100_segment", "analysis year"),
        ("CY2025", "calendar_year", "historical", "bts_ontime", "analysis year"),
        ("FY2024", "fiscal_year", "historical", "faa_taf", "base year"),
        ("FY2029", "fiscal_year", "forecast", "faa_taf", "forecast horizon"),
    ]
    assert (result.periods[2].start, result.periods[3].end) == (date(2023, 10, 1), date(2029, 9, 30))
    references = result.data.national_references
    assert {key: ref.value for key, ref in references.items()} == {"S1": 2.0, "S2": 80.0, "S3": 3.0}
    data = result.data
    assert (data.taf_edition, data.base_year, data.horizon_year) == ("TAF 2025", 2024, 2029)
    assert [(source.source_id, source.period) for source in result.sources] == [
        ("faa_taf", "FY2024-FY2029"),
        ("bts_t100_segment", "CY2025"),
        ("bts_ontime", "CY2025"),
        ("constraints_table", None),
        ("evidence_records", None),
    ]
    assert (result.sources[3].name, result.sources[3].dataset_version) == (
        "apt_pvd noise rules",
        "checked 2026-10-20",
    )
    assert result.completeness == "partial"
    limitations = result.limitations
    assert (
        "Some pressure signals are unknown because data is missing or the sample is too small: "
        "BTV (S2, S3), MHT (S3). Unknown signals are not counted for or against an airport."
    ) in limitations
    assert "Not ranked because TAF data is missing: ZZV is not in the TAF 2025 edition." in limitations
    assert (
        "BDL: no terminal capacity evidence is on file, so it is a candidate for further review, "
        "not a proven expansion need."
    ) in limitations
    assert all(text in limitations for text in RANKING_LIMITATIONS)
    assert len(limitations) == len(set(limitations))
    assert not any(text.startswith("Data version changed") for text in limitations)


def test_adding_an_airport_does_not_change_other_airports(service: AnalysisService) -> None:
    full = service.rank_airports(RANKED_IDS)
    subset = service.rank_airports(["apt_pwm", "apt_bdl"])
    expected = {row.airport.code: row.model_dump(exclude={"rank"}) for row in full.data.rows}
    assert [row.airport.code for row in subset.data.rows] == ["BDL", "PWM"]
    for row in subset.data.rows:
        assert row.model_dump(exclude={"rank"}) == expected[row.airport.code]
    assert subset.data.national_references == full.data.national_references
    assert subset.completeness == "complete"


def test_stored_national_references_are_used_without_recomputing(settings: Settings) -> None:
    repo = make_repository()
    make_service(settings, repo).rank_airports(RANKED_IDS)
    assert repo.calls == []


def test_missing_national_references_are_computed_from_the_full_source(settings: Settings) -> None:
    repo = make_repository(stored_references=False)
    result = make_service(settings, repo).rank_airports(["apt_bos"])
    references = result.data.national_references
    # Class F departures from US origins only: the YYZ origin and the class G rows are left out.
    assert references["S2"].value == pytest.approx(100 * 69_840 / 84_000)
    # Airports with at least 1,000 taxi-out departures: excesses 0.8, 0.8, 3.5 and 3.5.
    assert references["S3"].value == pytest.approx((0.8 + 3.5) / 2)
    assert references["S3"].sample_size == 4
    # Every TAF airport with both years, including LEB below the primary-airport threshold.
    assert references["S1"].value == pytest.approx(100 * ((3_325_000 / 3_009_000) ** 0.2 - 1))
    assert references["S1"].sample_size == 9
    assert {"taf_annual(all)", "t100_origin_totals(2025)", "ontime_taxi_histogram(all, 2025)"} <= set(
        repo.calls
    )


def test_stored_taxi_reference_is_recomputed_for_another_sample_minimum(settings: Settings) -> None:
    repo = make_repository()
    service = make_service(settings.model_copy(update={"s3_min_departures": 500}), repo)
    result = service.rank_airports(["apt_mht"])
    reference = result.data.national_references["S3"]
    assert "ontime_taxi_histogram(all, 2025)" in repo.calls
    assert (reference.details["min_departures"], reference.sample_size) == (500, 6)
    assert reference.value == pytest.approx(0.8)  # middle pair of 2/3, 2/3, 0.8, 0.8, 3.5, 3.5
    assert result.data.rows[0].signals[2].status == "fail"  # MHT: 2/3 minute with 500 departures


def test_load_factor_above_100_percent_keeps_its_status_and_is_flagged(service: AnalysisService) -> None:
    result = service.rank_airports(["apt_ack"])
    row = result.data.rows[0]
    assert (row.signals[1].value, row.signals[1].status) == (105.0, "pass")
    assert row.signals[1].note == "Load factor above 100% — flagged for review."
    assert (row.group, row.rule_id) == ("watch", "R3")  # holds whether S3 passes or fails
    assert "ACK: the CY2025 seat load factor of 105.0% is above 100% and is flagged for review." in (
        result.limitations
    )


def test_non_default_horizon_uses_a_national_cagr_for_that_horizon(settings: Settings) -> None:
    repo = FakeRepository(
        "v-h",
        data_coverage=coverage("v-h", t100_years=(2025,), ontime_years=(2025,), taf_last_year=2030),
        taf=frame(
            taf_rows("AAA", {2024: (1_000_000, 0), 2027: (1_157_625, 0)})  # 5% a year
            + taf_rows("BBB", {2024: (500_000, 0), 2027: (500_000, 0)}),
            TAF_COLUMNS,
        ),
    )
    catalog = FakeReferenceCatalog([airport_ref("AAA", 1), airport_ref("BBB", 2)])
    service = AnalysisService(FakeDataService(catalog, [repo], "v-h"), settings)
    result = service.rank_airports(["apt_aaa"], horizon_years=3)
    assert result.defaults_applied == ["year"]
    assert (result.parameters["horizon_years"], result.parameters["horizon_year"]) == (3, 2027)
    reference = result.data.national_references["S1"]
    assert reference.period_key == "FY2024-FY2027"
    assert reference.value == pytest.approx(100 * ((1_657_625 / 1_500_000) ** (1 / 3) - 1))
    row = result.data.rows[0]
    assert row.cagr_5 == pytest.approx(5.0)
    assert row.signals[0].status == "pass"
    assert result.periods[-1].label == "FY2027"
    with pytest.raises(AnalysisInputError) as error:
        service.rank_airports(["apt_aaa"], horizon_years=7)  # the edition ends in FY2030
    assert (error.value.code, error.value.details["maximum"]) == ("invalid_parameter", 6)


# -- data versions and periods ---------------------------------------------------------------------


def test_follow_up_keeps_its_data_version_or_discloses_the_switch(settings: Settings) -> None:
    old = make_repository("v-2024", t100_years=(2024,), ontime_years=(2024,))
    service = make_service(settings, old, make_repository("v-2025"), active="v-2025")

    pinned = service.rank_airports(["apt_bos"], year=2024, data_version_id="v-2024")
    assert pinned.data_version_id == "v-2024"
    assert not any(text.startswith("Data version changed") for text in pinned.limitations)
    default_year = service.rank_airports(["apt_bos"], data_version_id="v-2024")
    assert (default_year.data_version_id, default_year.parameters["year"]) == ("v-2024", 2024)

    switched = service.rank_airports(["apt_bos"], year=2025, data_version_id="v-2024")
    assert switched.data_version_id == "v-2025"
    assert switched.limitations[0] == (
        "Data version changed from v-2024 to v-2025 because v-2024 does not cover T-100 Segment "
        "for CY2025 and On-Time Performance for CY2025."
    )
    gone = service.compare_airports(["apt_bos", "apt_bdl"], data_version_id="v-gone")
    assert gone.data_version_id == "v-2025"
    assert gone.limitations[0] == (
        "Data version changed from v-gone to v-2025 because v-gone is no longer available."
    )


def test_a_period_without_full_coverage_lists_the_available_years(service: AnalysisService) -> None:
    with pytest.raises(NoDataError) as ranking:
        service.rank_airports(["apt_bos"], year=2023)
    assert ranking.value.code == "period_unavailable"
    assert ranking.value.details["available_years"] == [2024, 2025]
    assert "CY2023" in str(ranking.value)

    with pytest.raises(NoDataError) as unmet:
        service.assess_unmet_demand("apt_bos", year=2024)  # growth needs CY2023 as well
    assert unmet.value.details["available_years"] == [2025]
    assert unmet.value.details["missing"] == ["T-100 Segment for CY2023"]


def test_no_active_data_version(settings: Settings) -> None:
    service = AnalysisService(FakeDataService(make_catalog(), [], None), settings)
    with pytest.raises(NoDataError) as error:
        service.compare_airports(["apt_bos", "apt_bdl"])
    assert error.value.code == "no_data_version"


INVALID_REQUESTS: list[tuple[str, Callable[[AnalysisService], object], str]] = [
    ("no airports", lambda s: s.rank_airports([]), "too_few_airports"),
    ("301 airports", lambda s: s.rank_airports([f"apt_{i}" for i in range(301)]), "too_many_airports"),
    ("ids as a string", lambda s: s.rank_airports("apt_bos"), "invalid_parameter"),
    ("unknown id", lambda s: s.rank_airports(["apt_bos", "apt_nope"]), "unknown_airport"),
    ("year 0", lambda s: s.rank_airports(["apt_bos"], year=0), "invalid_year"),
    ("year bool", lambda s: s.rank_airports(["apt_bos"], year=True), "invalid_year"),
    ("year text", lambda s: s.rank_airports(["apt_bos"], year="2025"), "invalid_year"),
    ("horizon 0", lambda s: s.rank_airports(["apt_bos"], horizon_years=0), "invalid_parameter"),
    ("horizon 21", lambda s: s.rank_airports(["apt_bos"], horizon_years=21), "invalid_parameter"),
    ("compare one", lambda s: s.compare_airports(["apt_bos"]), "too_few_airports"),
    ("compare duplicate", lambda s: s.compare_airports(["apt_bos", "apt_bos"]), "too_few_airports"),
    (
        "compare seven",
        lambda s: s.compare_airports([f"apt_{c}" for c in ("bos", "bdl", "pvd", "mht", "pwm", "btv", "orh")]),
        "too_many_airports",
    ),
    ("long-haul unknown", lambda s: s.long_haul_share("apt_nope"), "unknown_airport"),
    ("threshold 0", lambda s: s.long_haul_share("apt_bos", threshold_miles=0), "invalid_parameter"),
    ("threshold < 0", lambda s: s.long_haul_share("apt_bos", threshold_miles=-5), "invalid_parameter"),
    ("threshold big", lambda s: s.long_haul_share("apt_bos", threshold_miles=12_001), "invalid_parameter"),
    (
        "threshold nan",
        lambda s: s.long_haul_share("apt_bos", threshold_miles=float("nan")),
        "invalid_parameter",
    ),
    ("threshold bool", lambda s: s.long_haul_share("apt_bos", threshold_miles=True), "invalid_parameter"),
    ("unmet year", lambda s: s.assess_unmet_demand("apt_bos", year=99_999), "invalid_year"),
]


@pytest.mark.parametrize(
    ("call", "code"),
    [(call, code) for _, call, code in INVALID_REQUESTS],
    ids=[name for name, _, _ in INVALID_REQUESTS],
)
def test_invalid_requests_raise_structured_errors(
    service: AnalysisService, call: Callable[[AnalysisService], object], code: str
) -> None:
    with pytest.raises(AnalysisInputError) as error:
        call(service)
    assert error.value.code == code
    assert str(error.value) == error.value.message


def test_unknown_airport_error_lists_the_unknown_ids(service: AnalysisService) -> None:
    with pytest.raises(AnalysisInputError) as error:
        service.rank_airports(["apt_bos", "apt_nope", "apt_other"])
    assert error.value.details == {"airport_ids": ["apt_nope", "apt_other"]}


# -- comparison ----------------------------------------------------------------------------------


def test_comparison_side_by_side_values(service: AnalysisService) -> None:
    result = service.compare_airports(["apt_bos", "apt_bdl"])
    assert result.analysis_type == "comparison"
    assert result.title == "Congestion comparison: BOS vs BDL (CY2025)"
    assert (result.completeness, result.defaults_applied) == ("complete", ["year"])
    bos, bdl = result.data.airports
    assert (bos.days_covered, bos.departures_scheduled, bos.departures_performed) == (365, 36_000, 35_640)
    assert bos.departures_performed_per_day == pytest.approx(35_640 / 365)
    assert (bos.departure_delay_rate, bos.departure_delay_base) == (pytest.approx(20.0), 35_640)
    assert bos.cancellation_rate == pytest.approx(1.0)
    assert (bos.median_taxi_out, bos.taxi_out_p10, bos.taxi_out_sample) == (12.0, 8.5, 1_000)
    assert bos.taxi_out_excess == pytest.approx(3.5)
    assert (bos.median_taxi_in, bos.taxi_in_sample) == (pytest.approx(5.5 + 100 / 600), 1_000)
    assert bos.attributed_delay_minutes == 24_000
    assert {cause.cause: cause.share for cause in bos.delay_causes} == {
        "carrier": 50.0,
        "weather": 10.0,
        "nas": 30.0,
        "security": 0.0,
        "late_aircraft": 10.0,
    }
    assert bos.notes == []
    assert (bdl.departure_delay_rate, bdl.cancellation_rate) == (pytest.approx(10.0), pytest.approx(1.0))
    assert (bdl.median_taxi_in, bdl.taxi_in_sample) == (7.0, 300)
    assert bdl.notes == ["Taxi-in sample of 300 arrivals is below 1,000; interpret taxi-in times with care."]
    assert result.limitations == [
        "Taxi-time samples below 1,000 flights for BDL; interpret those taxi times with care.",
        NAS_LIMITATION,
        ONTIME_COVERAGE_LIMITATION,
    ]
    assert [(source.source_id, source.period) for source in result.sources] == [("bts_ontime", "CY2025")]
    assert [(p.label, p.source_id) for p in result.periods] == [("CY2025", "bts_ontime")]
    assert result.parameters == {
        "airport_ids": ["apt_bos", "apt_bdl"],
        "airport_codes": ["BOS", "BDL"],
        "year": 2025,
        "s3_min_departures": 1_000,
    }


def test_comparison_with_missing_data_is_partial(service: AnalysisService) -> None:
    result = service.compare_airports(["apt_pvd", "apt_btv"], year=2025)
    assert (result.completeness, result.defaults_applied) == ("partial", [])
    pvd, btv = result.data.airports
    assert [record.constraint_type for record in pvd.constraints] == ["curfew"]
    assert pvd.departures_scheduled is None  # no departure aggregates: missing, not zero
    assert pvd.median_taxi_out == 12.0
    assert pvd.notes[0].startswith("No On-Time departures are reported for CY2025")
    assert (btv.departures_scheduled, btv.median_taxi_out, btv.taxi_out_sample) == (None, None, None)
    assert btv.notes == ["No verified BTS airport mapping, so On-Time data cannot be joined."]
    assert "Some metrics could not be computed for PVD, BTV; see the airport notes." in result.limitations
    assert [source.source_id for source in result.sources] == ["bts_ontime", "constraints_table"]
    with pytest.raises(NoDataError) as error:
        service.compare_airports(["apt_btv", "apt_zzu"])
    assert error.value.code == "airport_not_covered"


# -- long-haul share ----------------------------------------------------------------------------


@pytest.fixture
def anc_service(settings: Settings) -> AnalysisService:
    repo = FakeRepository(
        "v-anc", data_coverage=coverage("v-anc", t100_years=(2025,)), t100=load_anc_t100_2025()
    )
    catalog = FakeReferenceCatalog(
        [
            airport_ref("ANC", ANC_BTS_AIRPORT_ID, state="AK", city="Anchorage"),
            airport_ref("FAI", 10630, state="AK", city="Fairbanks"),
            airport_ref("BTV", None, state="VT"),
        ]
    )
    return AnalysisService(FakeDataService(catalog, [repo], "v-anc"), settings)


def test_long_haul_share_for_anchorage_matches_the_golden_values(anc_service: AnalysisService) -> None:
    result = anc_service.long_haul_share("apt_anc")
    data = result.data
    assert (result.analysis_type, result.completeness) == ("long_haul_share", "complete")
    assert result.defaults_applied == ["year", "threshold_miles"]
    assert (data.year, data.period_label, data.threshold_miles) == (2025, "CY2025", 3_452.0)
    assert data.threshold_nautical_miles == pytest.approx(2_999.706, abs=1e-3)
    assert (data.total_departures, data.valid_departures, data.long_haul_departures) == (
        87_988,
        87_746,
        15_089,
    )
    assert (data.excluded_departures, round(data.excluded_share or 0, 3)) == (242, 0.275)
    assert round(data.long_haul_share or 0, 3) == 17.196
    assert [(entry.label, round(entry.share or 0, 3)) for entry in data.by_aircraft_config] == [
        ("Passenger", 1.288),
        ("Freight", 29.662),
        ("Passenger/freight combination", 16.54),
    ]
    assert data.top_long_haul_destinations[0].dest_code == "HKG"
    assert result.parameters == {
        "airport_id": "apt_anc",
        "airport_code": "ANC",
        "year": 2025,
        "threshold_miles": 3_452.0,
        "service_classes": "all",
    }
    assert result.limitations == [
        "Long-haul means a segment longer than 3,452 statute miles (about 3,000 nautical miles); "
        "this threshold is a system choice, not a universal definition, so see the distance-group "
        "distribution.",
        "242 departures (0.28% of all departures performed) have a missing or zero distance, for "
        "example flights that return to the origin airport, and are excluded from the share.",
        LONG_HAUL_SERVICE_CLASS_LIMITATION,
    ]
    assert [(source.source_id, source.period) for source in result.sources] == [
        ("bts_t100_segment", "CY2025")
    ]
    assert [(p.label, p.source_id, p.nature) for p in result.periods] == [
        ("CY2025", "bts_t100_segment", "historical")
    ]


def test_long_haul_share_with_a_requested_threshold(anc_service: AnalysisService) -> None:
    result = anc_service.long_haul_share("apt_anc", year=2025, threshold_miles=2_500)
    assert result.defaults_applied == []
    assert result.data.threshold_miles == 2_500.0
    assert result.data.long_haul_departures == 37_969
    assert result.data.long_haul_share == pytest.approx(100 * 37_969 / 87_746)
    assert result.limitations[0] == (
        "Long-haul means a segment longer than 2,500 statute miles (about 2,172 nautical miles), "
        "as requested instead of the default 3,452 statute miles; any threshold is a choice, not a "
        "universal definition, so see the distance-group distribution."
    )


def test_long_haul_share_without_data_for_the_airport(anc_service: AnalysisService) -> None:
    with pytest.raises(NoDataError) as unmapped:
        anc_service.long_haul_share("apt_btv")
    assert unmapped.value.code == "airport_not_covered"
    with pytest.raises(NoDataError) as no_rows:
        anc_service.long_haul_share("apt_fai")
    assert no_rows.value.code == "airport_not_covered"
    assert str(no_rows.value) == "No T-100 departures are reported from FAI in CY2025."
    with pytest.raises(NoDataError) as no_year:
        anc_service.long_haul_share("apt_anc", year=2024)
    assert (no_year.value.code, no_year.value.details["available_years"]) == ("period_unavailable", [2025])


# -- unmet demand -------------------------------------------------------------------------------


def test_unmet_demand_with_constrained_demand_signs(service: AnalysisService) -> None:
    result = service.assess_unmet_demand("apt_bos")
    data = result.data
    assert result.analysis_type == "unmet_demand"
    assert (data.analysis_year, data.comparison_year) == (2025, 2024)
    assert (data.classification, data.classification_label) == (
        "constrained_demand_signs",
        "Signs of constrained demand",
    )
    assert (data.passengers, data.passengers_previous, data.seats, data.seats_previous) == (
        10_200,
        9_600,
        12_000,
        12_000,
    )
    assert (data.passenger_growth, data.seat_growth) == (pytest.approx(6.25), 0.0)
    assert [(c.condition_id, c.status, c.detail) for c in data.conditions] == [
        ("S2", "pass", "Load factor 85.0% ≥ national 80.0%."),
        ("PAX_VS_SEATS", "pass", "Passengers +6.2% vs seats +0.0% (CY2024 to CY2025)."),
        (
            "S3_OR_CONSTRAINT",
            "pass",
            "Taxi-out excess 3.5 min ≥ national 3.0 min. No documented constraint outside the "
            "terminal is on file.",
        ),
    ]
    assert (data.s2.status, data.s3.status) == ("pass", "pass")
    assert (data.evidence_status, [record.evidence_id for record in data.evidence]) == (
        "available",
        ["apt_bos-terminal"],
    )
    assert [entry.month for entry in data.monthly_delay_causes] == list(range(1, 13))
    assert all(
        (entry.arrivals, entry.attributed_delay_minutes) == (3_000, 2_000)
        for entry in data.monthly_delay_causes
    )
    assert (result.completeness, result.defaults_applied) == ("complete", ["year"])
    assert result.limitations == list(UNMET_LIMITATIONS)
    assert [(source.source_id, source.period) for source in result.sources] == [
        ("bts_t100_segment", "CY2024-CY2025"),
        ("bts_ontime", "CY2025"),
        ("evidence_records", None),
    ]
    assert [(p.label, p.source_id, p.role) for p in result.periods] == [
        ("CY2025", "bts_t100_segment", "analysis year"),
        ("CY2024", "bts_t100_segment", "comparison year"),
        ("CY2025", "bts_ontime", "analysis year"),
    ]


def test_unmet_demand_with_weak_demand_signs(service: AnalysisService) -> None:
    data = service.assess_unmet_demand("apt_pwm").data
    assert data.classification == "weak_demand_signs"
    assert data.passenger_growth == pytest.approx(-100 * 240 / 8_640)
    assert data.seat_growth == 0.0
    assert [condition.status for condition in data.conditions] == ["fail", "fail", "fail"]


def test_unmet_demand_names_the_input_that_blocks_the_decision(service: AnalysisService) -> None:
    result = service.assess_unmet_demand("apt_orh")
    assert result.data.classification == "undetermined"
    assert result.data.rule == (
        "Cannot be determined because the result depends on unknown inputs. "
        "S3 unknown: pass → signs of constrained demand, fail → neither pattern."
    )
    assert [condition.status for condition in result.data.conditions] == ["pass", "pass", "unknown"]
    assert result.completeness == "partial"
    assert (
        "The classification is undetermined because S3 (taxi-out excess) is unknown; see the conditions."
    ) in result.limitations


def test_unmet_demand_with_a_constraint_does_not_need_s3(service: AnalysisService) -> None:
    data = service.assess_unmet_demand("apt_pvd").data
    assert data.classification == "constrained_demand_signs"
    assert data.conditions[2].detail.endswith("Documented constraint outside the terminal: curfew.")


def test_unmet_demand_without_a_bts_mapping(service: AnalysisService) -> None:
    with pytest.raises(NoDataError) as error:
        service.assess_unmet_demand("apt_btv")
    assert error.value.code == "airport_not_covered"


# -- national references for a data version ---------------------------------------------------


def national_repository(
    *,
    aaa_passengers: float = 800.0,
    aaa_horizon: float = 1_100_000.0,
    taxi_airports: int = 101,
    taf_base_year: int | None = 2024,
) -> FakeRepository:
    taf = (
        taf_rows("AAA", {2024: (1_000_000, 0), 2029: (aaa_horizon, 0)})
        + taf_rows("BBB", {2024: (0, 0), 2029: (50_000, 0)})
        + taf_rows("CCC", {2024: (500_000, 0)})
    )
    t100 = (
        t100_rows(1, "AAA", 2025, passengers=aaa_passengers, seats=1_000)
        + t100_rows(2, "BBB", 2025, passengers=90, seats=100)
        + t100_rows(3, "YYZ", 2025, passengers=10, seats=1_000, origin_country="CA")
        + t100_rows(1, "AAA", 2025, passengers=0, seats=1_000, service_class="G")
    )
    # Airport i: {10: 100, 11 + i % 10: 900} has an excess of (1 + i % 10) - 5/9 minutes.
    taxi = [
        row for i in range(taxi_airports) for row in taxi_rows(100 + i, 2025, {10: 100, 11 + i % 10: 900})
    ] + taxi_rows(999, 2025, {10: 999})
    return FakeRepository(
        "v-new",
        data_coverage=coverage(
            "v-new", t100_years=(2025,), ontime_years=(2025,), taf_base_year=taf_base_year
        ),
        taf=frame(taf, TAF_COLUMNS),
        t100=frame(t100, T100_COLUMNS),
        taxi=frame(taxi, TAXI_COLUMNS),
    )


def test_compute_national_references() -> None:
    references = compute_national_references(national_repository())
    by_key = {(ref.metric, ref.period_key): ref for ref in references}
    assert list(by_key) == [
        ("taf_cagr", "FY2024-FY2029"),
        ("t100_load_factor", "CY2025"),
        ("ontime_taxi_out_excess_median", "CY2025"),
    ]
    cagr = by_key[("taf_cagr", "FY2024-FY2029")]
    assert (cagr.value, cagr.sample_size) == (pytest.approx(100 * (1.15**0.2 - 1)), 2)
    load_factor = by_key[("t100_load_factor", "CY2025")]
    assert (load_factor.value, load_factor.sample_size) == (pytest.approx(100 * 10_680 / 13_200), 2)
    taxi = by_key[("ontime_taxi_out_excess_median", "CY2025")]
    assert (taxi.value, taxi.sample_size) == (pytest.approx(40 / 9), 101)  # the 51st of 101 values
    assert taxi.details["min_departures"] == 1_000


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"aaa_passengers": 300.0}, "t100_load_factor CY2025"),  # 35.5% is below 50%
        ({"aaa_horizon": 2_000_000.0}, "taf_cagr FY2024-FY2029"),  # 15.4% a year
        ({"taxi_airports": 50}, "at least 100"),
        ({"taf_base_year": None}, "no TAF edition"),
    ],
)
def test_compute_national_references_rejects_implausible_values(
    overrides: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        compute_national_references(national_repository(**overrides))


def test_compute_national_references_needs_both_taf_years() -> None:
    with pytest.raises(ValueError, match="could not be computed"):
        compute_national_references(national_repository(), horizon_years=3)  # no FY2027 rows
