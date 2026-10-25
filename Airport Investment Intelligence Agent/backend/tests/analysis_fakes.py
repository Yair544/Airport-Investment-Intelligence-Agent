"""In-memory test doubles for the data-layer protocols used by the analysis layer.

`FakeRepository`, `FakeReferenceCatalog` and `FakeDataService` implement the protocols in
`app.data.service` over small pandas DataFrames, so analysis tests run without SQLite, files or
network. The row builders keep test data short and readable.

`load_anc_t100_2025()` reads `fixtures/analysis/t100_segment_anc_2025.csv`: every ANC-origin row
of the BTS T-100 Segment (All Carriers) sample for Alaska, calendar year 2025
(`tmp/source-review/t100-alaska-2025.zip`), summed by month, destination, service class,
aircraft configuration and distance. Only the carrier and data-source columns were dropped, so all
departure, seat and passenger totals equal the sample. Columns the sample lacks (BTS airport ids,
countries, city names, aircraft type, freight, distance group) are filled with the ANC BTS id,
"US" for the origin country and missing values otherwise.
"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.analysis.models import (
    ALL_MONTHS,
    AirportRef,
    ConstraintRecord,
    ConstraintType,
    EvidenceRecord,
    NationalReference,
    ResolutionOutcome,
    ResolvedAirport,
    SourceId,
    SourceRef,
)
from app.data.service import DataCoverage, DataUnavailableError, TaxiKind

FIXTURES = Path(__file__).parent / "fixtures" / "analysis"
ANC_BTS_AIRPORT_ID = 10299
RETRIEVED_AT = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

TAF_COLUMNS = [
    "faa_locid",
    "fiscal_year",
    "is_forecast",
    "total_enplanements",
    "commercial_operations",
]
T100_COLUMNS = [
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
]
DEPARTURE_COLUMNS = [
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
ARRIVAL_COLUMNS = [
    "airport_id",
    "year",
    "month",
    "arrivals",
    "delay_carrier",
    "delay_weather",
    "delay_nas",
    "delay_security",
    "delay_late_aircraft",
]
TAXI_COLUMNS = ["airport_id", "year", "month", "kind", "minutes", "flights"]
SOURCE_NAMES: dict[str, tuple[str, str]] = {
    "faa_taf": ("FAA Terminal Area Forecast", "https://taf.faa.gov/"),
    "bts_t100_segment": (
        "BTS T-100 Segment (All Carriers)",
        "https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FMG",
    ),
    "bts_ontime": (
        "BTS Reporting Carrier On-Time Performance",
        "https://www.transtats.bts.gov/Fields.asp?gnoyr_VQ=FGJ",
    ),
    "bts_t100_summary": (
        "BTS T-100 summary API",
        "https://data.transportation.gov/resource/r495-tyji.json",
    ),
}


def frame(rows: Iterable[Mapping[str, Any]], columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame(list(rows), columns=list(columns))


def coverage(
    version_id: str = "v-test",
    *,
    t100_years: Iterable[int] = (),
    ontime_years: Iterable[int] = (),
    taf_edition: str | None = "TAF 2025",
    taf_base_year: int | None = 2024,
    taf_last_year: int | None = 2045,
) -> DataCoverage:
    return DataCoverage(
        version_id=version_id,
        t100_months={year: list(ALL_MONTHS) for year in t100_years},
        ontime_months={year: list(ALL_MONTHS) for year in ontime_years},
        taf_edition=taf_edition,
        taf_base_year=taf_base_year,
        taf_last_year=taf_last_year,
    )


class FakeRepository:
    """`DataRepository` over in-memory frames; records national-reference fallbacks in `calls`."""

    def __init__(
        self,
        version_id: str = "v-test",
        *,
        data_coverage: DataCoverage | None = None,
        taf: pd.DataFrame | None = None,
        t100: pd.DataFrame | None = None,
        departures: pd.DataFrame | None = None,
        arrivals: pd.DataFrame | None = None,
        taxi: pd.DataFrame | None = None,
        national_references: Iterable[NationalReference] = (),
    ) -> None:
        self._version_id = version_id
        self._coverage = data_coverage or coverage(version_id)
        self._taf = taf if taf is not None else frame([], TAF_COLUMNS)
        self._t100 = t100 if t100 is not None else frame([], T100_COLUMNS)
        self._departures = departures if departures is not None else frame([], DEPARTURE_COLUMNS)
        self._arrivals = arrivals if arrivals is not None else frame([], ARRIVAL_COLUMNS)
        self._taxi = taxi if taxi is not None else frame([], TAXI_COLUMNS)
        self._references = {(ref.metric, ref.period_key): ref for ref in national_references}
        self.calls: list[str] = []

    @property
    def version_id(self) -> str:
        return self._version_id

    def coverage(self) -> DataCoverage:
        return self._coverage

    def taf_annual(self, faa_locids: Sequence[str] | None = None) -> pd.DataFrame:
        if faa_locids is None:
            self.calls.append("taf_annual(all)")
            return self._taf.copy()
        return self._taf[self._taf["faa_locid"].isin(list(faa_locids))].copy()

    def t100_segments(
        self,
        origin_airport_ids: Sequence[int],
        year: int,
        months: Sequence[int],
        service_classes: Sequence[str] | None = None,
    ) -> pd.DataFrame:
        rows = self._t100_period(year, months, service_classes)
        return rows[rows["origin_airport_id"].isin(list(origin_airport_ids))].reset_index(drop=True)

    def t100_origin_totals(
        self,
        year: int,
        months: Sequence[int],
        service_classes: Sequence[str] | None = None,
        origin_country: str | None = None,
    ) -> pd.DataFrame:
        self.calls.append(f"t100_origin_totals({year})")
        rows = self._t100_period(year, months, service_classes)
        if origin_country is not None:
            rows = rows[rows["origin_country"] == origin_country]
        return rows.groupby(["origin_airport_id", "origin"], as_index=False).agg(
            departures_performed=("departures_performed", "sum"),
            seats=("seats", "sum"),
            passengers=("passengers", "sum"),
            months_present=("month", "nunique"),
        )

    def _t100_period(
        self, year: int, months: Sequence[int], service_classes: Sequence[str] | None
    ) -> pd.DataFrame:
        rows = self._t100[(self._t100["year"] == year) & self._t100["month"].isin(list(months))]
        if service_classes is not None:
            rows = rows[rows["service_class"].isin(list(service_classes))]
        return rows

    def ontime_departures(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> pd.DataFrame:
        return self._ontime(self._departures, airport_ids, year, months)

    def ontime_arrivals(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> pd.DataFrame:
        return self._ontime(self._arrivals, airport_ids, year, months)

    def ontime_taxi_histogram(
        self, airport_ids: Sequence[int] | None, year: int, months: Sequence[int], kind: TaxiKind
    ) -> pd.DataFrame:
        if airport_ids is None:
            self.calls.append(f"ontime_taxi_histogram(all, {year})")
        rows = self._ontime(self._taxi, airport_ids, year, months)
        rows = rows[rows["kind"] == kind]
        return rows.groupby(["airport_id", "minutes"], as_index=False)["flights"].sum()

    @staticmethod
    def _ontime(
        table: pd.DataFrame, airport_ids: Sequence[int] | None, year: int, months: Sequence[int]
    ) -> pd.DataFrame:
        rows = table[(table["year"] == year) & table["month"].isin(list(months))]
        if airport_ids is not None:
            rows = rows[rows["airport_id"].isin(list(airport_ids))]
        return rows.reset_index(drop=True)

    def national_reference(self, metric: str, period_key: str) -> NationalReference | None:
        return self._references.get((metric, period_key))

    def source_ref(self, source_id: SourceId, period_label: str | None = None) -> SourceRef:
        name, url = SOURCE_NAMES.get(source_id, (source_id, "https://example.test/"))
        return SourceRef(
            source_id=source_id,
            name=name,
            url=url,
            period=period_label,
            retrieved_at=RETRIEVED_AT,
            dataset_version=f"{source_id} test file",
            data_version_id=self._version_id,
        )


class FakeReferenceCatalog:
    """`ReferenceCatalog` over explicit airport, constraint and evidence records."""

    def __init__(
        self,
        airports: Iterable[AirportRef],
        *,
        constraints: Iterable[ConstraintRecord] = (),
        evidence: Iterable[EvidenceRecord] = (),
        reference_version: str = "ref-test",
    ) -> None:
        self._airports = {airport.airport_id: airport for airport in airports}
        self._constraints = list(constraints)
        self._evidence = list(evidence)
        self._reference_version = reference_version

    @property
    def reference_version(self) -> str:
        return self._reference_version

    def airport(self, airport_id: str) -> AirportRef | None:
        return self._airports.get(airport_id)

    def airports(self) -> list[AirportRef]:
        return list(self._airports.values())

    def resolve(self, query: str, context_airport_ids: Sequence[str] = ()) -> ResolutionOutcome:
        matches = [a for a in self._airports.values() if a.code.upper() == query.strip().upper()]
        if not matches:
            return ResolutionOutcome(query=query, status="not_found")
        return ResolutionOutcome(
            query=query,
            status="matched",
            kind="airport",
            airports=[
                ResolvedAirport(
                    airport_id=a.airport_id,
                    code=a.code,
                    faa_locid=a.faa_locid,
                    name=a.name,
                    city=a.city,
                    state=a.state,
                    matched_on="code",
                )
                for a in matches
            ],
        )

    def constraints_for(self, airport_id: str) -> list[ConstraintRecord]:
        return [record for record in self._constraints if record.airport_id == airport_id]

    def evidence_for(self, airport_id: str) -> list[EvidenceRecord]:
        return [record for record in self._evidence if record.airport_id == airport_id]


class FakeDataService:
    """`DataService` with explicit versions; `active_version_id=None` means no data yet."""

    def __init__(
        self,
        reference: FakeReferenceCatalog,
        repositories: Iterable[FakeRepository] = (),
        active_version_id: str | None = None,
    ) -> None:
        self._reference = reference
        self.repositories = {repo.version_id: repo for repo in repositories}
        self._active = active_version_id

    @property
    def reference(self) -> FakeReferenceCatalog:
        return self._reference

    def active_version_id(self) -> str | None:
        return self._active

    def repository(self, version_id: str | None = None) -> FakeRepository:
        key = self._active if version_id is None else version_id
        if key is None or key not in self.repositories:
            raise DataUnavailableError(f"Data version {key} is not available.")
        return self.repositories[key]


# ---------------------------------------------------------------------------------------------
# Record and row builders
# ---------------------------------------------------------------------------------------------


def airport_ref(
    code: str, bts_airport_id: int | None, *, state: str = "MA", city: str | None = None
) -> AirportRef:
    return AirportRef(
        airport_id=f"apt_{code.lower()}",
        faa_locid=code,
        iata_code=code,
        bts_airport_id=bts_airport_id,
        name=f"{code} Test Airport",
        city=city or f"{code} City",
        state=state,
        mapping_status="verified" if bts_airport_id is not None else "faa_only",
    )


def constraint(airport_id: str, constraint_type: ConstraintType = "curfew") -> ConstraintRecord:
    return ConstraintRecord(
        airport_id=airport_id,
        constraint_type=constraint_type,
        description="Night-time operating restriction.",
        source_title=f"{airport_id} noise rules",
        source_url=f"https://example.test/{airport_id}/noise",
        checked_on=date(2026, 10, 20),
    )


def evidence(airport_id: str, component: str | None = "terminal") -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=f"{airport_id}-{component}",
        airport_id=airport_id,
        topic="capacity",
        infrastructure_component=component,
        scenario="existing",
        statement="Terminal capacity is documented in the airport master plan.",
        stance_subject="constraint",
        stance="supports",
        source_title=f"{airport_id} master plan",
        source_url=f"https://example.test/{airport_id}/master-plan",
        source_locator="Table 4-2",
        checked_on=date(2026, 10, 21),
    )


def taf_rows(
    faa_locid: str, values: Mapping[int, tuple[float | None, float | None]], *, base_year: int = 2024
) -> list[dict[str, Any]]:
    """values: fiscal year -> (total enplanements, commercial operations)."""
    return [
        {
            "faa_locid": faa_locid,
            "fiscal_year": year,
            "is_forecast": year > base_year,
            "total_enplanements": enplanements,
            "commercial_operations": operations,
        }
        for year, (enplanements, operations) in values.items()
    ]


def t100_rows(
    origin_airport_id: int,
    origin: str,
    year: int,
    *,
    passengers: float,
    seats: float,
    departures: float = 100.0,
    months: Iterable[int] = ALL_MONTHS,
    service_class: str = "F",
    origin_country: str = "US",
    dest: str = "XYZ",
    distance: float | None = 500.0,
    aircraft_config: int = 1,
) -> list[dict[str, Any]]:
    """One segment row per month with the given monthly values."""
    return [
        {
            "year": year,
            "month": month,
            "carrier": "ZZ",
            "origin_airport_id": origin_airport_id,
            "origin": origin,
            "origin_country": origin_country,
            "dest_airport_id": None,
            "dest": dest,
            "dest_country": "US",
            "dest_city_name": f"{dest} City",
            "service_class": service_class,
            "aircraft_config": aircraft_config,
            "aircraft_type": None,
            "departures_scheduled": departures,
            "departures_performed": departures,
            "seats": seats,
            "passengers": passengers,
            "freight": 0.0,
            "distance": distance,
            "distance_group": None,
        }
        for month in months
    ]


def segment(
    *,
    departures: float,
    distance: float | None,
    aircraft_config: int | None = 1,
    dest: str = "XYZ",
    dest_city_name: str | None = None,
    distance_group: int | None = None,
    year: int = 2025,
    month: int = 1,
) -> dict[str, Any]:
    """A single T-100 segment row for long-haul unit tests."""
    row = t100_rows(1, "ORG", year, passengers=0.0, seats=0.0, departures=departures, months=[month])[0]
    row.update(
        dest=dest,
        dest_city_name=dest_city_name,
        distance=distance,
        aircraft_config=aircraft_config,
        distance_group=distance_group,
    )
    return row


def taxi_rows(
    airport_id: int,
    year: int,
    histogram: Mapping[int, int],
    *,
    kind: str = "taxi_out",
    month: int = 1,
) -> list[dict[str, Any]]:
    return [
        {
            "airport_id": airport_id,
            "year": year,
            "month": month,
            "kind": kind,
            "minutes": minute,
            "flights": flights,
        }
        for minute, flights in histogram.items()
    ]


def departure_rows(
    airport_id: int,
    year: int,
    *,
    scheduled: int,
    cancelled: int,
    del15_valid: int,
    del15_count: int,
    diverted: int = 0,
    months: Iterable[int] = ALL_MONTHS,
) -> list[dict[str, Any]]:
    """One aggregate row per month with the given monthly values."""
    return [
        {
            "airport_id": airport_id,
            "year": year,
            "month": month,
            "days_in_month": calendar.monthrange(year, month)[1],
            "scheduled": scheduled,
            "cancelled": cancelled,
            "diverted": diverted,
            "del15_valid": del15_valid,
            "del15_count": del15_count,
        }
        for month in months
    ]


def arrival_rows(
    airport_id: int,
    year: int,
    *,
    arrivals: int,
    carrier: float,
    weather: float,
    nas: float,
    security: float,
    late_aircraft: float,
    months: Iterable[int] = ALL_MONTHS,
) -> list[dict[str, Any]]:
    return [
        {
            "airport_id": airport_id,
            "year": year,
            "month": month,
            "arrivals": arrivals,
            "delay_carrier": carrier,
            "delay_weather": weather,
            "delay_nas": nas,
            "delay_security": security,
            "delay_late_aircraft": late_aircraft,
        }
        for month in months
    ]


def load_anc_t100_2025() -> pd.DataFrame:
    """The ANC-origin T-100 fixture with every column `DataRepository.t100_segments` returns."""
    raw = pd.read_csv(FIXTURES / "t100_segment_anc_2025.csv")
    filled = raw.assign(
        carrier=None,
        origin_airport_id=float(ANC_BTS_AIRPORT_ID),
        origin_country="US",
        dest_airport_id=float("nan"),
        dest_country=None,
        dest_city_name=None,
        aircraft_type=float("nan"),
        freight=float("nan"),
        distance_group=float("nan"),
    )
    numeric = [
        "aircraft_config",
        "departures_scheduled",
        "departures_performed",
        "seats",
        "passengers",
        "distance",
    ]
    return filled.astype({column: float for column in numeric})[T100_COLUMNS]
