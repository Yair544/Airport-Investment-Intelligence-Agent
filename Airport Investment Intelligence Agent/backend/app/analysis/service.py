"""Analysis orchestration: validated inputs -> pinned data version -> metrics -> structured results.

The service is deterministic and runs without a language model. Each public method validates its
inputs, pins one immutable data version (spec section 8), reads normalized data through the
`DataService` protocol, computes metrics and classifications with `metrics` and `scoring`, and
returns a result that carries the effective parameters, periods, sources, limitations and
versions needed to reproduce it.

Periods: this version analyses full calendar years only. Without a requested year, the latest
full calendar year covered by every source the analysis needs is used and "year" is listed in
`defaults_applied`. TAF values are FAA fiscal years and are always reported as separate periods.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import pandas as pd

from app.analysis import metrics, scoring
from app.analysis.metrics import SCHEDULED_PASSENGER_CLASS, Histogram, TafGrowth
from app.analysis.models import (
    ALL_MONTHS,
    RANKING_GROUP_LABELS,
    AirportLabel,
    AirportRef,
    ComparisonAirport,
    ComparisonData,
    ComparisonResult,
    ConditionResult,
    ConstraintRecord,
    EvidenceRecord,
    ExcludedAirport,
    LongHaulData,
    LongHaulResult,
    NationalReference,
    PeriodInfo,
    RankingData,
    RankingResult,
    RankingRow,
    SignalResult,
    SignalStatus,
    SourceId,
    SourceRef,
    UnmetDemandData,
    UnmetDemandResult,
    calendar_label,
    fiscal_label,
)
from app.analysis.scoring import join_words
from app.config import Settings
from app.data.service import DataCoverage, DataRepository, DataService, DataUnavailableError

ParamValue = str | int | float | bool | list[str] | None
SignalKey = Literal["S1", "S2", "S3"]

MAX_RANKING_AIRPORTS = 300
MIN_COMPARE_AIRPORTS = 2
MAX_COMPARE_AIRPORTS = 6
MAX_HORIZON_YEARS = 20
MAX_THRESHOLD_MILES = 12_000.0
EARLIEST_YEAR = 1900
LATEST_YEAR = 2100
NATIONAL_TAXI_MIN_DEPARTURES = 1_000  # spec section 4.6 default for S3 and its national reference
MIN_AIRPORTS_FOR_TAXI_REFERENCE = 100
PLAUSIBLE_RANGES: dict[str, tuple[float, float, str]] = {
    "taf_cagr": (-10.0, 10.0, "percent per year"),
    "t100_load_factor": (50.0, 100.0, "percent"),
    "ontime_taxi_out_excess_median": (0.5, 20.0, "minutes"),
}

RANKING_LIMITATIONS = (
    "This ranking is an initial screening based on public data, not an engineering, regulatory "
    "or economic assessment.",
    "At airports that were constrained in the past, the TAF forecast starts from a constrained "
    "base and may understate demand.",
    "Seat load factor reflects airline supply decisions, not terminal utilization.",
    "On-Time Performance data covers domestic flights of reporting carriers only, so S3 may be "
    "unknown at smaller airports.",
    "Signal thresholds are relative to national references; being above a national reference "
    "is a sign of pressure, not a proven need.",
    "A demand-capacity gap model is deferred to a later version because comparable public "
    "terminal capacity data is not available.",
)
NAS_LIMITATION = (
    "NAS delay includes weather and air traffic control effects, so it is not evidence of a "
    "terminal shortage."
)
ONTIME_COVERAGE_LIMITATION = (
    "On-Time data covers domestic flights of reporting carriers only, not all airport activity."
)
LOAD_FACTOR_LIMITATION = "Seat load factor reflects airline supply decisions, not terminal utilization."
UNMET_LIMITATIONS = (
    "This classifies signs of constrained demand; it does not count missing flights or passengers.",
    "A delay is not by itself proof of a missing flight.",
    NAS_LIMITATION,
    LOAD_FACTOR_LIMITATION,
    ONTIME_COVERAGE_LIMITATION,
)
LONG_HAUL_SERVICE_CLASS_LIMITATION = (
    "The share counts departures performed in every T-100 service class: scheduled and "
    "non-scheduled, passenger and freight."
)

_EMPTY = pd.DataFrame()
_UNMET_INPUT_NAMES: dict[str, str] = {
    "S2": "S2 (seat load factor)",
    "G": "passenger vs seat growth",
    "S3": "S3 (taxi-out excess)",
}
_SOURCE_LABELS: dict[str, str] = {
    "bts_t100_segment": "T-100 Segment",
    "bts_ontime": "On-Time Performance",
}


# ---------------------------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------------------------


class AnalysisInputError(ValueError):
    """Invalid analysis input, reported as a structured error with a stable `code`.

    Codes: unknown_airport, invalid_year, invalid_parameter, too_few_airports, too_many_airports.
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class NoDataError(Exception):
    """A valid request that the available data cannot answer; reported apart from failures.

    Codes: no_data_version, period_unavailable, airport_not_covered.
    """

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


# ---------------------------------------------------------------------------------------------
# Periods and data-version selection
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _YearNeeds:
    """Full calendar years an analysis needs from each BTS source, relative to its year t."""

    t100_offsets: tuple[int, ...] = ()
    ontime_offsets: tuple[int, ...] = ()
    taf: bool = False

    def missing(self, coverage: DataCoverage, year: int) -> list[str]:
        gaps = [
            f"{_SOURCE_LABELS['bts_t100_segment']} for {calendar_label(year + offset)}"
            for offset in self.t100_offsets
            if not coverage.has_months("bts_t100_segment", year + offset, ALL_MONTHS)
        ]
        gaps += [
            f"{_SOURCE_LABELS['bts_ontime']} for {calendar_label(year + offset)}"
            for offset in self.ontime_offsets
            if not coverage.has_months("bts_ontime", year + offset, ALL_MONTHS)
        ]
        return gaps

    def available_years(self, coverage: DataCoverage) -> list[int]:
        candidates = set(coverage.full_years("bts_t100_segment")) | set(coverage.full_years("bts_ontime"))
        return sorted(year for year in candidates if not self.missing(coverage, year))

    def describe(self) -> str:
        parts: list[str] = []
        if self.t100_offsets:
            consecutive = " for two consecutive years" if len(self.t100_offsets) > 1 else ""
            parts.append(f"{_SOURCE_LABELS['bts_t100_segment']}{consecutive}")
        if self.ontime_offsets:
            parts.append(_SOURCE_LABELS["bts_ontime"])
        return join_words(parts)


_RANKING_NEEDS = _YearNeeds(t100_offsets=(0,), ontime_offsets=(0,), taf=True)
_COMPARISON_NEEDS = _YearNeeds(ontime_offsets=(0,))
_LONG_HAUL_NEEDS = _YearNeeds(t100_offsets=(0,))
_UNMET_NEEDS = _YearNeeds(t100_offsets=(0, -1), ontime_offsets=(0,))


@dataclass(frozen=True)
class _Gap:
    """Why a data version cannot serve a request."""

    reason: str  # completes "<version id> ..." in the version-change limitation
    message: str
    details: dict[str, Any]


def _plan_year(version_id: str, coverage: DataCoverage, needs: _YearNeeds, year: int | None) -> int | _Gap:
    """The analysis year this version can serve, or the gap that prevents it."""
    if needs.taf and (coverage.taf_base_year is None or coverage.taf_last_year is None):
        return _Gap(
            reason="has no TAF edition",
            message=f"Data version {version_id} has no TAF edition.",
            details={"year": year, "available_years": [], "data_version_id": version_id},
        )
    available = needs.available_years(coverage)
    if year is None:
        if available:
            return available[-1]
        return _Gap(
            reason=f"has no full calendar year of {needs.describe()}",
            message=f"Data version {version_id} has no full calendar year of {needs.describe()}.",
            details={"year": None, "available_years": [], "data_version_id": version_id},
        )
    missing = needs.missing(coverage, year)
    if not missing:
        return year
    listed = join_words(missing)
    years_text = ", ".join(str(item) for item in available) or "none"
    return _Gap(
        reason=f"does not cover {listed}",
        message=(
            f"{calendar_label(year)} cannot be analysed: data version {version_id} does not "
            f"cover {listed}. Full calendar years available for this analysis: {years_text}."
        ),
        details={
            "year": year,
            "available_years": available,
            "missing": missing,
            "data_version_id": version_id,
        },
    )


# ---------------------------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------------------------


def _validate_year(year: object) -> None:
    if year is None:
        return
    if isinstance(year, bool) or not isinstance(year, int) or not EARLIEST_YEAR <= year <= LATEST_YEAR:
        raise AnalysisInputError(
            "invalid_year",
            f"year must be a calendar year between {EARLIEST_YEAR} and {LATEST_YEAR}, such as 2025.",
            {"year": repr(year)},
        )


def _validate_horizon(horizon_years: object) -> int:
    if (
        isinstance(horizon_years, bool)
        or not isinstance(horizon_years, int)
        or not 1 <= horizon_years <= MAX_HORIZON_YEARS
    ):
        raise AnalysisInputError(
            "invalid_parameter",
            f"horizon_years must be a whole number of years between 1 and {MAX_HORIZON_YEARS}.",
            {"parameter": "horizon_years", "value": repr(horizon_years)},
        )
    return horizon_years


def _validate_threshold(threshold_miles: object) -> float:
    if (
        isinstance(threshold_miles, bool)
        or not isinstance(threshold_miles, int | float)
        or not math.isfinite(threshold_miles)
        or not 0 < threshold_miles <= MAX_THRESHOLD_MILES
    ):
        raise AnalysisInputError(
            "invalid_parameter",
            f"threshold_miles must be greater than 0 and at most {MAX_THRESHOLD_MILES:,.0f} statute miles.",
            {"parameter": "threshold_miles", "value": repr(threshold_miles)},
        )
    return float(threshold_miles)


def _label(airport: AirportRef) -> AirportLabel:
    return AirportLabel(
        airport_id=airport.airport_id,
        code=airport.code,
        name=airport.name,
        city=airport.city,
        state=airport.state,
    )


def _unique(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _unique_sources(sources: Iterable[SourceRef]) -> list[SourceRef]:
    seen: set[str] = set()
    result: list[SourceRef] = []
    for source in sources:
        key = source.model_dump_json()
        if key not in seen:
            seen.add(key)
            result.append(source)
    return result


def _reference_sources(
    constraints: Iterable[ConstraintRecord], evidence: Iterable[EvidenceRecord]
) -> list[SourceRef]:
    """One source entry per constraint or evidence record used (deduplicated by the caller)."""
    sources = [
        SourceRef(
            source_id="constraints_table",
            name=record.source_title,
            url=record.source_url,
            dataset_version=f"checked {record.checked_on.isoformat()}",
        )
        for record in constraints
    ]
    sources += [
        SourceRef(
            source_id="evidence_records",
            name=record.source_title,
            url=record.source_url,
            dataset_version=f"checked {record.checked_on.isoformat()}",
        )
        for record in evidence
    ]
    return sources


def _calendar_period(year: int, source_id: SourceId, role: str) -> PeriodInfo:
    return PeriodInfo(
        label=calendar_label(year),
        kind="calendar_year",
        nature="historical",
        source_id=source_id,
        start=date(year, 1, 1),
        end=date(year, 12, 31),
        role=role,
    )


def _fiscal_period(year: int, nature: Literal["historical", "forecast"], role: str) -> PeriodInfo:
    """FAA fiscal year: October 1 of the previous calendar year to September 30."""
    return PeriodInfo(
        label=fiscal_label(year),
        kind="fiscal_year",
        nature=nature,
        source_id="faa_taf",
        start=date(year - 1, 10, 1),
        end=date(year, 9, 30),
        role=role,
    )


def _constraint_kinds(constraints: Sequence[ConstraintRecord]) -> str:
    return ", ".join(
        sorted({scoring.CONSTRAINT_TYPE_LABELS[record.constraint_type] for record in constraints})
    )


def _load_factor_flag(code: str, signal: SignalResult, period: str) -> str | None:
    if signal.value is None or signal.value <= 100:
        return None
    return (
        f"{code}: the {period} seat load factor of {signal.value:.1f}% is above 100% and is "
        "flagged for review."
    )


def _subject(region_label: str | None, codes: Sequence[str]) -> str:
    if region_label:
        return region_label
    return ", ".join(codes) if len(codes) <= 6 else f"{len(codes)} airports"


# ---------------------------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------------------------


class AnalysisService:
    """Deterministic analyses over one pinned data version; no language model is involved."""

    def __init__(self, data: DataService, settings: Settings) -> None:
        self._data = data
        self._settings = settings

    # -- ranking (spec section 4.4) -------------------------------------------------------------

    def rank_airports(
        self,
        airport_ids: Sequence[str],
        *,
        region_label: str | None = None,
        year: int | None = None,
        horizon_years: int | None = None,
        data_version_id: str | None = None,
    ) -> RankingResult:
        """Classify the requested primary airports by capacity pressure and rank them.

        Airports below the primary-airport threshold in the TAF base year, or missing from the
        TAF edition, are listed in `excluded`. National references come from the full source data,
        so adding an airport to the request never changes the other airports' results.
        """
        airports = self._resolve_airports(airport_ids, minimum=1, maximum=MAX_RANKING_AIRPORTS)
        _validate_year(year)
        settings = self._settings
        horizon = (
            settings.forecast_horizon_years if horizon_years is None else _validate_horizon(horizon_years)
        )
        repo, analysis_year, limitations = self._open(data_version_id, _RANKING_NEEDS, year)
        coverage = repo.coverage()
        base_year, last_year = coverage.taf_base_year, coverage.taf_last_year
        if base_year is None or last_year is None:  # excluded by _RANKING_NEEDS; kept for typing
            raise NoDataError("period_unavailable", "The data version has no TAF edition.")
        edition = coverage.taf_edition or "TAF"
        max_horizon = min(MAX_HORIZON_YEARS, last_year - base_year)
        if horizon > max_horizon:
            raise AnalysisInputError(
                "invalid_parameter",
                f"horizon_years must be between 1 and {max_horizon} for {edition}.",
                {"parameter": "horizon_years", "value": horizon, "maximum": max_horizon},
            )
        horizon_year = base_year + horizon
        period = calendar_label(analysis_year)
        taf_period = metrics.taf_period_key(base_year, horizon_year)
        references: dict[SignalKey, NationalReference] = {
            "S1": self._taf_reference(repo, base_year, horizon_year),
            "S2": self._load_factor_reference(repo, analysis_year),
            "S3": self._taxi_out_reference(repo, analysis_year),
        }

        taf = repo.taf_annual(sorted({airport.faa_locid for airport in airports}))
        bts_ids = sorted({a.bts_airport_id for a in airports if a.bts_airport_id is not None})
        segments = (
            repo.t100_segments(bts_ids, analysis_year, ALL_MONTHS, [SCHEDULED_PASSENGER_CLASS])
            if bts_ids
            else _EMPTY
        )
        taxi = (
            metrics.histograms_by_airport(
                repo.ontime_taxi_histogram(bts_ids, analysis_year, ALL_MONTHS, "taxi_out")
            )
            if bts_ids
            else {}
        )

        rows: list[RankingRow] = []
        excluded: list[ExcludedAirport] = []
        missing_taf: list[str] = []
        min_enplanements = settings.primary_airport_min_enplanements
        for airport in airports:
            airport_taf = metrics.rows_where(taf, "faa_locid", airport.faa_locid)
            growth = metrics.taf_growth(airport_taf, base_year, horizon_year)
            reason: str | None = None
            if airport_taf.empty:
                reason = f"Not in the {edition} edition."
                missing_taf.append(f"{airport.code} is not in the {edition} edition")
            elif growth.base_enplanements is None:
                reason = f"{fiscal_label(base_year)} enplanements are not available in {edition}."
                missing_taf.append(
                    f"{airport.code} has no {fiscal_label(base_year)} enplanements in {edition}"
                )
            elif growth.base_enplanements < min_enplanements:
                reason = (
                    f"{fiscal_label(base_year)} enplanements below {min_enplanements:,} "
                    f"({growth.base_enplanements:,} in {edition})."
                )
            if reason is not None:
                excluded.append(ExcludedAirport(airport=_label(airport), reason=reason))
                continue
            rows.append(self._ranking_row(airport, growth, segments, taxi, references, period, taf_period))
        rows = scoring.order_ranking_rows(rows)
        excluded.sort(key=lambda item: item.airport.code)

        unknown = [
            f"{row.airport.code} ({', '.join(s.signal for s in row.signals if s.status == 'unknown')})"
            for row in sorted(rows, key=lambda item: item.airport.code)
            if any(signal.status == "unknown" for signal in row.signals)
        ]
        flags = [_load_factor_flag(row.airport.code, row.signals[1], period) for row in rows]
        limitations += [flag for flag in flags if flag]
        if unknown:
            limitations.append(
                "Some pressure signals are unknown because data is missing or the sample is too "
                f"small: {', '.join(unknown)}. Unknown signals are not counted for or against an "
                "airport."
            )
        missing_delta = [row.airport.code for row in rows if row.delta_enplanements is None]
        if missing_delta:
            limitations.append(
                f"TAF {fiscal_label(horizon_year)} forecast enplanements are missing for "
                f"{', '.join(missing_delta)}, so ΔE is unknown and the airport is listed last in "
                "its group."
            )
        if missing_taf:
            limitations.append(f"Not ranked because TAF data is missing: {'; '.join(missing_taf)}.")
        limitations += [
            f"{row.airport.code}: no terminal capacity evidence is on file, so it is a candidate "
            "for further review, not a proven expansion need."
            for row in rows
            if row.further_review
        ]
        limitations += RANKING_LIMITATIONS
        limitations.append(
            f"TAF values are FAA fiscal years (October to September) and are shown separately "
            f"from the calendar-year BTS data for {period}."
        )

        codes = [airport.code for airport in airports]
        parameters: dict[str, ParamValue] = {
            "airport_ids": [airport.airport_id for airport in airports],
            "airport_codes": codes,
            "region_label": region_label,
            "year": analysis_year,
            "horizon_years": horizon,
            "taf_edition": coverage.taf_edition,
            "base_year": base_year,
            "horizon_year": horizon_year,
            "primary_airport_min_enplanements": min_enplanements,
            "s3_min_departures": settings.s3_min_departures,
            "load_factor_service_class": SCHEDULED_PASSENGER_CLASS,
        }
        sources = [
            repo.source_ref("faa_taf", taf_period),
            repo.source_ref("bts_t100_segment", period),
            repo.source_ref("bts_ontime", period),
            *_reference_sources(
                [record for row in rows for record in row.constraints],
                [record for row in rows for record in row.evidence],
            ),
        ]
        return RankingResult(
            title=f"Expansion candidate ranking: {_subject(region_label, codes)}",
            completeness="partial" if unknown or missing_delta or missing_taf else "complete",
            units={
                "base_year_enplanements": "enplaned passengers per fiscal year",
                "horizon_enplanements": "enplaned passengers per fiscal year",
                "delta_enplanements": "enplaned passengers per fiscal year",
                "cagr_5": f"percent per year over {taf_period}",
                "commercial_operations": "commercial operations per fiscal year",
                "S1": scoring.SIGNAL_UNITS["S1"],
                "S2": "percent of seats filled",
                "S3": scoring.SIGNAL_UNITS["S3"],
            },
            parameters=parameters,
            defaults_applied=[
                name for name, value in (("year", year), ("horizon_years", horizon_years)) if value is None
            ],
            periods=[
                _calendar_period(analysis_year, "bts_t100_segment", "analysis year"),
                _calendar_period(analysis_year, "bts_ontime", "analysis year"),
                _fiscal_period(base_year, "historical", "base year"),
                _fiscal_period(horizon_year, "forecast", "forecast horizon"),
            ],
            reference_version=self._data.reference.reference_version,
            data_version_id=repo.version_id,
            sources=_unique_sources(sources),
            limitations=_unique(limitations),
            data=RankingData(
                region_label=region_label,
                analysis_year=analysis_year,
                taf_edition=edition,
                base_year=base_year,
                horizon_year=horizon_year,
                national_references=references,
                rows=rows,
                excluded=excluded,
            ),
        )

    def _ranking_row(
        self,
        airport: AirportRef,
        growth: TafGrowth,
        segments: pd.DataFrame,
        taxi: dict[int, Histogram],
        references: dict[SignalKey, NationalReference],
        period: str,
        taf_period: str,
    ) -> RankingRow:
        bts_id = airport.bts_airport_id
        load_factor = (
            None
            if bts_id is None
            else metrics.load_factor(metrics.rows_where(segments, "origin_airport_id", bts_id))
        )
        stats = None if bts_id is None else metrics.taxi_stats(taxi.get(bts_id, {}))
        signals = [
            scoring.evaluate_s1(growth.cagr, references["S1"], period=taf_period),
            scoring.evaluate_s2(load_factor, references["S2"], period=period),
            scoring.evaluate_s3(
                stats,
                references["S3"],
                period=period,
                min_departures=self._settings.s3_min_departures,
            ),
        ]
        constraints = self._data.reference.constraints_for(airport.airport_id)
        evidence = self._data.reference.evidence_for(airport.airport_id)
        decision = scoring.classify_airport(
            signals[0].status, signals[1].status, signals[2].status, constraints
        )
        further_review = decision.group in (
            "strong_candidate",
            "watch",
        ) and not scoring.has_terminal_evidence(evidence)
        return RankingRow(
            rank=0,
            airport=_label(airport),
            group=decision.group,
            group_label=RANKING_GROUP_LABELS[decision.group],
            rule_id=decision.rule_id,
            rule=decision.rule,
            possible_groups=decision.possible_groups,
            base_year_enplanements=growth.base_enplanements,
            horizon_enplanements=growth.horizon_enplanements,
            delta_enplanements=growth.delta_enplanements,
            cagr_5=growth.cagr,
            base_year_commercial_operations=growth.base_operations,
            horizon_commercial_operations=growth.horizon_operations,
            delta_commercial_operations=growth.delta_operations,
            signals=signals,
            constraints=constraints,
            evidence=evidence,
            evidence_status="available" if evidence else "missing",
            further_review=further_review,
            notes=[scoring.FURTHER_REVIEW_NOTE] if further_review else [],
        )

    # -- congestion comparison (spec section 4.5) -----------------------------------------------

    def compare_airports(
        self,
        airport_ids: Sequence[str],
        *,
        year: int | None = None,
        data_version_id: str | None = None,
    ) -> ComparisonResult:
        """On-Time activity and congestion metrics side by side for one year, with no score."""
        airports = self._resolve_airports(
            airport_ids, minimum=MIN_COMPARE_AIRPORTS, maximum=MAX_COMPARE_AIRPORTS
        )
        _validate_year(year)
        repo, analysis_year, limitations = self._open(data_version_id, _COMPARISON_NEEDS, year)
        period = calendar_label(analysis_year)
        bts_ids = sorted({a.bts_airport_id for a in airports if a.bts_airport_id is not None})
        departures, arrivals = _EMPTY, _EMPTY
        taxi_out: dict[int, Histogram] = {}
        taxi_in: dict[int, Histogram] = {}
        if bts_ids:
            departures = repo.ontime_departures(bts_ids, analysis_year, ALL_MONTHS)
            arrivals = repo.ontime_arrivals(bts_ids, analysis_year, ALL_MONTHS)
            taxi_out = metrics.histograms_by_airport(
                repo.ontime_taxi_histogram(bts_ids, analysis_year, ALL_MONTHS, "taxi_out")
            )
            taxi_in = metrics.histograms_by_airport(
                repo.ontime_taxi_histogram(bts_ids, analysis_year, ALL_MONTHS, "taxi_in")
            )

        entries: list[ComparisonAirport] = []
        covered = False
        for airport in airports:
            entry, has_data = self._comparison_airport(
                airport, departures, arrivals, taxi_out, taxi_in, period
            )
            entries.append(entry)
            covered = covered or has_data
        if not covered:
            raise NoDataError(
                "airport_not_covered",
                f"None of the requested airports has On-Time Performance data for {period}.",
                {
                    "airport_ids": [airport.airport_id for airport in airports],
                    "year": analysis_year,
                    "data_version_id": repo.version_id,
                },
            )

        min_sample = self._settings.s3_min_departures
        small_samples = [
            entry.airport.code
            for entry in entries
            if (entry.taxi_out_sample is not None and entry.taxi_out_sample < min_sample)
            or (entry.taxi_in_sample is not None and entry.taxi_in_sample < min_sample)
        ]
        if small_samples:
            limitations.append(
                f"Taxi-time samples below {min_sample:,} flights for {', '.join(small_samples)}; "
                "interpret those taxi times with care."
            )
        incomplete = [entry.airport.code for entry in entries if _comparison_gaps(entry)]
        if incomplete:
            limitations.append(
                f"Some metrics could not be computed for {', '.join(incomplete)}; see the airport notes."
            )
        limitations += [NAS_LIMITATION, ONTIME_COVERAGE_LIMITATION]

        codes = [airport.code for airport in airports]
        constraints = [record for entry in entries for record in entry.constraints]
        parameters: dict[str, ParamValue] = {
            "airport_ids": [airport.airport_id for airport in airports],
            "airport_codes": codes,
            "year": analysis_year,
            "s3_min_departures": min_sample,
        }
        return ComparisonResult(
            title=f"Congestion comparison: {' vs '.join(codes)} ({period})",
            completeness="partial" if incomplete else "complete",
            units={
                "departures_performed_per_day": "departures per day",
                "departure_delay_rate": "percent of non-cancelled departures with a valid DepDel15",
                "cancellation_rate": "percent of scheduled departures",
                "median_taxi_out": "minutes",
                "taxi_out_p10": "minutes",
                "taxi_out_excess": "minutes",
                "median_taxi_in": "minutes",
                "attributed_delay_minutes": "minutes at arrivals",
                "delay_causes.share": "percent of attributed delay minutes at arrivals",
            },
            parameters=parameters,
            defaults_applied=["year"] if year is None else [],
            periods=[_calendar_period(analysis_year, "bts_ontime", "analysis year")],
            reference_version=self._data.reference.reference_version,
            data_version_id=repo.version_id,
            sources=_unique_sources(
                [repo.source_ref("bts_ontime", period), *_reference_sources(constraints, [])]
            ),
            limitations=_unique(limitations),
            data=ComparisonData(year=analysis_year, period_label=period, airports=entries),
        )

    def _comparison_airport(
        self,
        airport: AirportRef,
        departures: pd.DataFrame,
        arrivals: pd.DataFrame,
        taxi_out: dict[int, Histogram],
        taxi_in: dict[int, Histogram],
        period: str,
    ) -> tuple[ComparisonAirport, bool]:
        bts_id = airport.bts_airport_id
        notes: list[str] = []
        if bts_id is None:
            notes.append("No verified BTS airport mapping, so On-Time data cannot be joined.")
            dep_rows, arr_rows = _EMPTY, _EMPTY
            out_hist: Histogram = {}
            in_hist: Histogram = {}
        else:
            dep_rows = metrics.rows_where(departures, "airport_id", bts_id)
            arr_rows = metrics.rows_where(arrivals, "airport_id", bts_id)
            out_hist = taxi_out.get(bts_id, {})
            in_hist = taxi_in.get(bts_id, {})
        dep = metrics.departure_summary(dep_rows)
        delays = metrics.delay_cause_summary(arr_rows)
        out_stats = metrics.taxi_stats(out_hist)
        in_stats = metrics.taxi_stats(in_hist)
        has_data = not dep_rows.empty or not arr_rows.empty or bool(out_hist) or bool(in_hist)
        min_sample = self._settings.s3_min_departures
        if bts_id is not None:
            if dep.months_covered == 0:
                notes.append(
                    f"No On-Time departures are reported for {period}; reporting carriers may not "
                    "serve the airport."
                )
            elif dep.months_covered < len(ALL_MONTHS):
                notes.append(
                    f"On-Time departures are reported for {dep.months_covered} of 12 months of {period}."
                )
            if 0 < out_stats.sample < min_sample:
                notes.append(
                    f"Taxi-out sample of {out_stats.sample:,} departures is below {min_sample:,}; "
                    "interpret taxi-out times with care."
                )
            if 0 < in_stats.sample < min_sample:
                notes.append(
                    f"Taxi-in sample of {in_stats.sample:,} arrivals is below {min_sample:,}; "
                    "interpret taxi-in times with care."
                )
            if delays.attributed_minutes == 0:
                notes.append(
                    "No attributed delay minutes are reported, so delay-cause shares are not available."
                )
        entry = ComparisonAirport(
            airport=_label(airport),
            days_covered=dep.days_covered,
            departures_scheduled=dep.scheduled,
            departures_performed=dep.performed,
            departures_performed_per_day=dep.performed_per_day,
            departure_delay_rate=dep.delay_rate,
            departure_delay_base=dep.delay_base,
            cancellation_rate=dep.cancellation_rate,
            median_taxi_out=out_stats.median,
            taxi_out_p10=out_stats.p10,
            taxi_out_excess=out_stats.excess,
            taxi_out_sample=out_stats.sample or None,
            median_taxi_in=in_stats.median,
            taxi_in_sample=in_stats.sample or None,
            attributed_delay_minutes=delays.attributed_minutes,
            delay_causes=delays.causes,
            constraints=self._data.reference.constraints_for(airport.airport_id),
            notes=notes,
        )
        return entry, has_data

    # -- long-haul share (spec section 4.5) -----------------------------------------------------

    def long_haul_share(
        self,
        airport_id: str,
        *,
        year: int | None = None,
        threshold_miles: float | None = None,
        data_version_id: str | None = None,
    ) -> LongHaulResult:
        """Share of T-100 departures performed (all service classes) above a distance threshold."""
        airport = self._resolve_airports([airport_id], minimum=1, maximum=1)[0]
        _validate_year(year)
        default_threshold = self._settings.long_haul_threshold_miles
        threshold = default_threshold if threshold_miles is None else _validate_threshold(threshold_miles)
        bts_id = self._require_bts_id(airport, "T-100")
        repo, analysis_year, limitations = self._open(data_version_id, _LONG_HAUL_NEEDS, year)
        period = calendar_label(analysis_year)
        breakdown = metrics.long_haul_breakdown(
            repo.t100_segments([bts_id], analysis_year, ALL_MONTHS, None), threshold
        )
        if breakdown.total_departures <= 0:
            raise NoDataError(
                "airport_not_covered",
                f"No T-100 departures are reported from {airport.code} in {period}.",
                {
                    "airport_id": airport.airport_id,
                    "year": analysis_year,
                    "data_version_id": repo.version_id,
                },
            )
        nautical = metrics.statute_to_nautical_miles(threshold)
        definition = (
            f"Long-haul means a segment longer than {threshold:,g} statute miles (about "
            f"{nautical:,.0f} nautical miles)"
        )
        if threshold == default_threshold:
            limitations.append(
                f"{definition}; this threshold is a system choice, not a universal definition, so "
                "see the distance-group distribution."
            )
        else:
            limitations.append(
                f"{definition}, as requested instead of the default {default_threshold:,g} statute "
                "miles; any threshold is a choice, not a universal definition, so see the "
                "distance-group distribution."
            )
        if breakdown.excluded_departures > 0 and breakdown.excluded_share is not None:
            limitations.append(
                f"{breakdown.excluded_departures:,.0f} departures "
                f"({breakdown.excluded_share:.2f}% of all departures performed) have a missing or "
                "zero distance, for example flights that return to the origin airport, and are "
                "excluded from the share."
            )
        if breakdown.long_haul_share is None:
            limitations.append(
                "No departure has a valid distance, so the long-haul share cannot be computed."
            )
        limitations.append(LONG_HAUL_SERVICE_CLASS_LIMITATION)
        parameters: dict[str, ParamValue] = {
            "airport_id": airport.airport_id,
            "airport_code": airport.code,
            "year": analysis_year,
            "threshold_miles": threshold,
            "service_classes": "all",
        }
        return LongHaulResult(
            title=f"Long-haul share of departures: {airport.code} ({period})",
            completeness="partial" if breakdown.long_haul_share is None else "complete",
            units={
                "threshold_miles": "statute miles",
                "threshold_nautical_miles": "nautical miles",
                "departures": "departures performed",
                "long_haul_share": "percent of departures with a valid distance",
                "excluded_share": "percent of all departures performed",
                "by_aircraft_config.share": "percent of the configuration's departures with a valid distance",
                "distance_groups.share": "percent of departures with a valid distance",
                "distance_miles": "statute miles",
            },
            parameters=parameters,
            defaults_applied=[
                name
                for name, value in (("year", year), ("threshold_miles", threshold_miles))
                if value is None
            ],
            periods=[_calendar_period(analysis_year, "bts_t100_segment", "analysis year")],
            reference_version=self._data.reference.reference_version,
            data_version_id=repo.version_id,
            sources=[repo.source_ref("bts_t100_segment", period)],
            limitations=_unique(limitations),
            data=LongHaulData(
                airport=_label(airport),
                year=analysis_year,
                period_label=period,
                threshold_miles=threshold,
                threshold_nautical_miles=nautical,
                total_departures=breakdown.total_departures,
                valid_departures=breakdown.valid_departures,
                long_haul_departures=breakdown.long_haul_departures,
                long_haul_share=breakdown.long_haul_share,
                excluded_departures=breakdown.excluded_departures,
                excluded_share=breakdown.excluded_share,
                by_aircraft_config=breakdown.by_aircraft_config,
                distance_groups=breakdown.distance_groups,
                top_long_haul_destinations=breakdown.top_destinations,
            ),
        )

    # -- unmet demand signs (spec section 4.5) --------------------------------------------------

    def assess_unmet_demand(
        self,
        airport_id: str,
        *,
        year: int | None = None,
        data_version_id: str | None = None,
    ) -> UnmetDemandResult:
        """Classify signs of constrained or weak demand; never a count of missing flights."""
        airport = self._resolve_airports([airport_id], minimum=1, maximum=1)[0]
        _validate_year(year)
        bts_id = self._require_bts_id(airport, "BTS")
        repo, analysis_year, limitations = self._open(data_version_id, _UNMET_NEEDS, year)
        previous_year = analysis_year - 1
        period, previous_period = calendar_label(analysis_year), calendar_label(previous_year)
        classes = [SCHEDULED_PASSENGER_CLASS]
        current = metrics.load_factor(repo.t100_segments([bts_id], analysis_year, ALL_MONTHS, classes))
        prior = metrics.load_factor(repo.t100_segments([bts_id], previous_year, ALL_MONTHS, classes))
        histogram = metrics.histograms_by_airport(
            repo.ontime_taxi_histogram([bts_id], analysis_year, ALL_MONTHS, "taxi_out")
        ).get(bts_id, {})
        s2 = scoring.evaluate_s2(current, self._load_factor_reference(repo, analysis_year), period=period)
        s3 = scoring.evaluate_s3(
            metrics.taxi_stats(histogram),
            self._taxi_out_reference(repo, analysis_year),
            period=period,
            min_departures=self._settings.s3_min_departures,
        )
        passenger_growth = metrics.growth_percent(current.passengers, prior.passengers)
        seat_growth = metrics.growth_percent(current.seats, prior.seats)
        growth_status: SignalStatus = (
            "unknown"
            if passenger_growth is None or seat_growth is None
            else "pass"
            if passenger_growth > seat_growth
            else "fail"
        )
        constraints = self._data.reference.constraints_for(airport.airport_id)
        evidence = self._data.reference.evidence_for(airport.airport_id)
        outcome = scoring.classify_unmet_demand(s2.status, growth_status, s3.status, bool(constraints))
        monthly = metrics.monthly_delay_causes(
            repo.ontime_arrivals([bts_id], analysis_year, ALL_MONTHS), analysis_year
        )
        conditions = _unmet_conditions(
            s2,
            s3,
            constraints,
            passenger_growth,
            seat_growth,
            growth_status,
            period,
            previous_period,
        )

        flag = _load_factor_flag(airport.code, s2, period)
        if flag:
            limitations.append(flag)
        missing_months = [entry.month for entry in monthly if entry.arrivals is None]
        if missing_months:
            limitations.append(
                f"On-Time arrival data is missing for {len(missing_months)} month(s) of {period}, "
                "so those months have no delay-cause shares."
            )
        if outcome.blocking_inputs:
            names = [_UNMET_INPUT_NAMES[name] for name in outcome.blocking_inputs]
            verb = "is" if len(names) == 1 else "are"
            limitations.append(
                f"The classification is undetermined because {join_words(names)} {verb} unknown; "
                "see the conditions."
            )
        limitations += UNMET_LIMITATIONS
        unknown_inputs = "unknown" in (s2.status, s3.status, growth_status)
        parameters: dict[str, ParamValue] = {
            "airport_id": airport.airport_id,
            "airport_code": airport.code,
            "year": analysis_year,
            "comparison_year": previous_year,
            "s3_min_departures": self._settings.s3_min_departures,
            "load_factor_service_class": SCHEDULED_PASSENGER_CLASS,
        }
        return UnmetDemandResult(
            title=f"Unmet demand signs: {airport.code} ({period})",
            completeness="partial" if unknown_inputs or missing_months else "complete",
            units={
                "passengers": "passengers on class F departures",
                "seats": "seats on class F departures",
                "passenger_growth": "percent",
                "seat_growth": "percent",
                "S2": "percent of seats filled",
                "S3": scoring.SIGNAL_UNITS["S3"],
                "monthly_delay_causes.minutes": "attributed delay minutes at arrivals",
                "monthly_delay_causes.share": "percent of attributed delay minutes at arrivals",
            },
            parameters=parameters,
            defaults_applied=["year"] if year is None else [],
            periods=[
                _calendar_period(analysis_year, "bts_t100_segment", "analysis year"),
                _calendar_period(previous_year, "bts_t100_segment", "comparison year"),
                _calendar_period(analysis_year, "bts_ontime", "analysis year"),
            ],
            reference_version=self._data.reference.reference_version,
            data_version_id=repo.version_id,
            sources=_unique_sources(
                [
                    repo.source_ref("bts_t100_segment", f"{previous_period}-{period}"),
                    repo.source_ref("bts_ontime", period),
                    *_reference_sources(constraints, evidence),
                ]
            ),
            limitations=_unique(limitations),
            data=UnmetDemandData(
                airport=_label(airport),
                analysis_year=analysis_year,
                comparison_year=previous_year,
                classification=outcome.classification,
                classification_label=outcome.label,
                rule=outcome.rule,
                conditions=conditions,
                passengers=current.passengers,
                passengers_previous=prior.passengers,
                passenger_growth=passenger_growth,
                seats=current.seats,
                seats_previous=prior.seats,
                seat_growth=seat_growth,
                s2=s2,
                s3=s3,
                constraints=constraints,
                evidence=evidence,
                evidence_status="available" if evidence else "missing",
                monthly_delay_causes=monthly,
            ),
        )

    # -- shared steps ---------------------------------------------------------------------------

    def _resolve_airports(
        self, airport_ids: Sequence[str], *, minimum: int, maximum: int
    ) -> list[AirportRef]:
        if isinstance(airport_ids, str) or not isinstance(airport_ids, Sequence):
            raise AnalysisInputError(
                "invalid_parameter",
                "airport_ids must be a list of airport ids.",
                {"parameter": "airport_ids"},
            )
        distinct = list(dict.fromkeys(airport_ids))
        if len(distinct) < minimum:
            raise AnalysisInputError(
                "too_few_airports",
                f"This analysis needs at least {minimum} distinct airport(s); {len(distinct)} given.",
                {"minimum": minimum, "count": len(distinct)},
            )
        if len(distinct) > maximum:
            raise AnalysisInputError(
                "too_many_airports",
                f"This analysis accepts at most {maximum} distinct airport(s); {len(distinct)} given.",
                {"maximum": maximum, "count": len(distinct)},
            )
        catalog = self._data.reference
        found: list[AirportRef] = []
        unknown: list[str] = []
        for airport_id in distinct:
            airport = catalog.airport(airport_id) if isinstance(airport_id, str) else None
            if airport is None:
                unknown.append(str(airport_id))
            else:
                found.append(airport)
        if unknown:
            raise AnalysisInputError(
                "unknown_airport",
                f"Unknown airport id(s): {', '.join(unknown)}. Resolve airports before analysing them.",
                {"airport_ids": unknown},
            )
        return found

    @staticmethod
    def _require_bts_id(airport: AirportRef, source: str) -> int:
        if airport.bts_airport_id is None:
            raise NoDataError(
                "airport_not_covered",
                f"{airport.code} has no verified BTS airport mapping, so {source} data cannot be joined.",
                {"airport_id": airport.airport_id},
            )
        return airport.bts_airport_id

    def _open(
        self, data_version_id: str | None, needs: _YearNeeds, year: int | None
    ) -> tuple[DataRepository, int, list[str]]:
        """Pin a data version and the analysis year (spec section 8).

        A follow-up keeps its previous data version when that version still exists and covers the
        request; otherwise the active version is used and the switch is disclosed as a limitation.
        """
        reason: str | None = None
        if data_version_id is not None:
            try:
                pinned = self._data.repository(data_version_id)
            except DataUnavailableError:
                reason = f"{data_version_id} is no longer available"
            else:
                planned = _plan_year(pinned.version_id, pinned.coverage(), needs, year)
                if not isinstance(planned, _Gap):
                    return pinned, planned, []
                reason = f"{data_version_id} {planned.reason}"
        try:
            active = self._data.repository(None)
        except DataUnavailableError as exc:
            raise NoDataError(
                "no_data_version",
                "No aviation data version is available yet; a data refresh must complete first.",
            ) from exc
        planned = _plan_year(active.version_id, active.coverage(), needs, year)
        if isinstance(planned, _Gap):
            raise NoDataError("period_unavailable", planned.message, planned.details)
        limitations: list[str] = []
        if reason is not None and active.version_id != data_version_id:
            limitations.append(
                f"Data version changed from {data_version_id} to {active.version_id} because {reason}."
            )
        return active, planned, limitations

    def _taf_reference(self, repo: DataRepository, base_year: int, horizon_year: int) -> NationalReference:
        stored = repo.national_reference("taf_cagr", metrics.taf_period_key(base_year, horizon_year))
        if stored is not None:
            return stored
        return metrics.national_taf_cagr(repo.taf_annual(None), base_year, horizon_year)

    def _load_factor_reference(self, repo: DataRepository, year: int) -> NationalReference:
        stored = repo.national_reference("t100_load_factor", calendar_label(year))
        if stored is not None:
            return stored
        totals = repo.t100_origin_totals(
            year, ALL_MONTHS, [SCHEDULED_PASSENGER_CLASS], metrics.NATIONAL_ORIGIN_COUNTRY
        )
        return metrics.national_load_factor(totals, year)

    def _taxi_out_reference(self, repo: DataRepository, year: int) -> NationalReference:
        min_departures = self._settings.s3_min_departures
        stored = repo.national_reference("ontime_taxi_out_excess_median", calendar_label(year))
        if stored is not None and stored.details.get("min_departures", min_departures) == min_departures:
            return stored
        histograms = repo.ontime_taxi_histogram(None, year, ALL_MONTHS, "taxi_out")
        return metrics.national_taxi_out_excess(histograms, year, min_departures)


def _comparison_gaps(entry: ComparisonAirport) -> bool:
    values = (
        entry.days_covered,
        entry.departures_performed_per_day,
        entry.departure_delay_rate,
        entry.cancellation_rate,
        entry.median_taxi_out,
        entry.taxi_out_excess,
        entry.median_taxi_in,
        entry.attributed_delay_minutes,
    )
    return any(value is None for value in values) or any(cause.share is None for cause in entry.delay_causes)


def _unmet_conditions(
    s2: SignalResult,
    s3: SignalResult,
    constraints: Sequence[ConstraintRecord],
    passenger_growth: float | None,
    seat_growth: float | None,
    growth_status: SignalStatus,
    period: str,
    previous_period: str,
) -> list[ConditionResult]:
    """The three inputs of the unmet-demand rule with the values behind each status."""
    if s2.status == "unknown" or s2.value is None or s2.reference is None:
        s2_detail = s2.note or "Load factor is unknown."
    else:
        relation = "≥" if s2.status == "pass" else "<"
        s2_detail = f"Load factor {s2.value:.1f}% {relation} national {s2.reference:.1f}%."
    if passenger_growth is None or seat_growth is None:
        growth_detail = (
            f"Passenger or seat growth is unknown because class F passengers or seats are missing "
            f"or zero in {previous_period} or {period}."
        )
    else:
        growth_detail = (
            f"Passengers {passenger_growth:+.1f}% vs seats {seat_growth:+.1f}% "
            f"({previous_period} to {period})."
        )
    if s3.status == "unknown" or s3.value is None or s3.reference is None:
        s3_part = f"S3 unknown: {s3.note or 'taxi-out excess is not available.'}"
    else:
        relation = "≥" if s3.status == "pass" else "<"
        s3_part = f"Taxi-out excess {s3.value:.1f} min {relation} national {s3.reference:.1f} min."
    constraint_part = (
        f"Documented constraint outside the terminal: {_constraint_kinds(constraints)}."
        if constraints
        else "No documented constraint outside the terminal is on file."
    )
    either: SignalStatus = (
        "pass" if s3.status == "pass" or constraints else "fail" if s3.status == "fail" else "unknown"
    )
    return [
        ConditionResult(
            condition_id="S2",
            description=f"Seat load factor at or above the national reference in {period} (S2)",
            status=s2.status,
            detail=s2_detail,
        ),
        ConditionResult(
            condition_id="PAX_VS_SEATS",
            description=f"Passenger growth higher than seat growth from {previous_period} to {period}",
            status=growth_status,
            detail=growth_detail,
        ),
        ConditionResult(
            condition_id="S3_OR_CONSTRAINT",
            description=(
                "Taxi-out excess at or above the national median (S3) or a documented constraint "
                "outside the terminal"
            ),
            status=either,
            detail=f"{s3_part} {constraint_part}",
        ),
    ]


# ---------------------------------------------------------------------------------------------
# National references for a data version (used by the refresh job)
# ---------------------------------------------------------------------------------------------


def compute_national_references(
    repo: DataRepository,
    *,
    horizon_years: int = 5,
    min_departures: int = NATIONAL_TAXI_MIN_DEPARTURES,
) -> list[NationalReference]:
    """National benchmarks of one data version, computed from the full source data.

    Returns the TAF CAGR reference for the horizon, the class F load factor of US origins for every
    full T-100 year and the median taxi-out excess for every full On-Time year. Values outside the
    plausibility bounds raise ValueError, so a refresh fails safely and the previous version stays
    active.
    """
    coverage = repo.coverage()
    base_year, last_year = coverage.taf_base_year, coverage.taf_last_year
    if base_year is None or last_year is None:
        raise ValueError("The data version has no TAF edition, so the S1 reference cannot be computed.")
    if not 1 <= horizon_years <= last_year - base_year:
        raise ValueError(f"horizon_years must be between 1 and {last_year - base_year} for this TAF edition.")
    references = [metrics.national_taf_cagr(repo.taf_annual(None), base_year, base_year + horizon_years)]
    for year in coverage.full_years("bts_t100_segment"):
        totals = repo.t100_origin_totals(
            year, ALL_MONTHS, [SCHEDULED_PASSENGER_CLASS], metrics.NATIONAL_ORIGIN_COUNTRY
        )
        references.append(metrics.national_load_factor(totals, year))
    for year in coverage.full_years("bts_ontime"):
        histograms = repo.ontime_taxi_histogram(None, year, ALL_MONTHS, "taxi_out")
        references.append(metrics.national_taxi_out_excess(histograms, year, min_departures))
    for reference in references:
        _check_plausible(reference)
    return references


def _check_plausible(reference: NationalReference) -> None:
    low, high, unit = PLAUSIBLE_RANGES[reference.metric]
    label = f"{reference.metric} {reference.period_key}"
    if reference.value is None:
        raise ValueError(f"National reference {label} could not be computed from the source data.")
    if not low <= reference.value <= high:
        raise ValueError(
            f"National reference {label} = {reference.value:.4f} {unit} is outside the plausible "
            f"range of {low:g} to {high:g}."
        )
    if (
        reference.metric == "ontime_taxi_out_excess_median"
        and (reference.sample_size or 0) < MIN_AIRPORTS_FOR_TAXI_REFERENCE
    ):
        raise ValueError(
            f"National reference {label} is based on {reference.sample_size or 0} airports; at "
            f"least {MIN_AIRPORTS_FOR_TAXI_REFERENCE} are required."
        )
