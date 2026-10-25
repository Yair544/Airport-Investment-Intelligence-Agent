"""Deterministic metric calculations for the analysis layer (spec section 4).

Every function here is pure: it takes normalized DataFrames (columns documented on
`app.data.service.DataRepository`) or plain numbers and never touches the data layer, a database
or a language model. Calculation rules from spec section 4.6:

- A missing input stays missing. A metric whose inputs are absent, or whose denominator is zero,
  is None, and only the metric that depends on that input is affected.
- Ratios are computed from sums, never as a mean of percentages.
- Percentages are percent values (84.2 means 84.2%), distances are statute miles and durations
  are minutes.
"""

from __future__ import annotations

import math
import statistics
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

import pandas as pd

from app.analysis.models import (
    ALL_MONTHS,
    DELAY_CAUSES,
    AircraftConfigShare,
    DelayCause,
    DelayCauseShare,
    DestinationRow,
    DistanceGroupRow,
    MonthlyDelayCauses,
    NationalReference,
    calendar_label,
    fiscal_label,
)

SCHEDULED_PASSENGER_CLASS = "F"  # T-100 service class "Scheduled Passenger/Cargo Service"
NATIONAL_ORIGIN_COUNTRY = "US"  # national load factor: segments departing US airports only
STATUTE_MILES_PER_NAUTICAL_MILE = 1.150779448
DISTANCE_GROUP_MILES = 500  # BTS DISTANCE_GROUP: 500-mile intervals, group 1 is under 500 miles
UNKNOWN_AIRCRAFT_CONFIG = 0  # used as the key when AIRCRAFT_CONFIG is missing
AIRCRAFT_CONFIG_LABELS: dict[int, str] = {
    1: "Passenger",
    2: "Freight",
    3: "Passenger/freight combination",
    4: "Seaplane",
}
OTHER_AIRCRAFT_CONFIG_LABEL = "Other/unknown"
DELAY_COLUMNS: dict[DelayCause, str] = {cause: f"delay_{cause}" for cause in DELAY_CAUSES}

Histogram = dict[int, int]  # whole minutes -> number of flights


# ---------------------------------------------------------------------------------------------
# Scalars and sums
# ---------------------------------------------------------------------------------------------


def to_float(value: Any) -> float | None:
    """A pandas or Python scalar as float, keeping missing values (None, NaN, NA) as None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) else number


def as_int(value: float | None) -> int | None:
    """Round a count stored as float to an int, keeping None."""
    return None if value is None else round(value)


def column_sum(frame: pd.DataFrame, column: str) -> float | None:
    """Sum of the reported values in a column; None when the column has no values at all."""
    if frame.empty:
        return None
    return to_float(pd.to_numeric(frame[column], errors="coerce").sum(min_count=1))


def _numeric(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").astype(float)


def _total(series: pd.Series) -> float:
    """Sum of departures in a selection; an empty selection is a real zero, not a missing value."""
    return float(series.sum())


def percent(numerator: float | None, denominator: float | None) -> float | None:
    """100 x numerator / denominator, or None when an input is missing or the denominator is 0."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return 100.0 * numerator / denominator


def growth_percent(current: float | None, previous: float | None) -> float | None:
    """Period-over-period growth 100 x (current / previous - 1); None without a usable base."""
    if current is None or previous is None or previous == 0:
        return None
    return 100.0 * (current - previous) / previous


def cagr_percent(base: float | None, horizon: float | None, years: int) -> float | None:
    """Compound annual growth rate in percent: ((horizon / base) ** (1 / years) - 1) x 100.

    None when either value is missing, the base is not positive or the horizon is negative.
    """
    if base is None or horizon is None or base <= 0 or horizon < 0 or years <= 0:
        return None
    return 100.0 * ((horizon / base) ** (1.0 / years) - 1.0)


def median(values: Iterable[float]) -> float | None:
    """Ordinary median (the mean of the middle pair for an even count); None for no values."""
    present = [value for value in values if not math.isnan(value)]
    return float(statistics.median(present)) if present else None


def statute_to_nautical_miles(miles: float) -> float:
    return miles / STATUTE_MILES_PER_NAUTICAL_MILE


def rows_where(frame: pd.DataFrame, column: str, value: object) -> pd.DataFrame:
    """Rows whose `column` equals `value`; an empty frame (even without columns) stays empty."""
    if frame.empty:
        return frame
    return frame[frame[column] == value]


# ---------------------------------------------------------------------------------------------
# TAF growth (spec sections 4.2 and 4.3, S1)
# ---------------------------------------------------------------------------------------------


def taf_period_key(base_year: int, horizon_year: int) -> str:
    """Period key of the national TAF growth reference, e.g. "FY2024-FY2029"."""
    return f"{fiscal_label(base_year)}-{fiscal_label(horizon_year)}"


@dataclass(frozen=True)
class TafGrowth:
    """Forecast growth of one airport between the TAF base year b and the horizon year b + h."""

    base_year: int
    horizon_year: int
    base_enplanements: int | None
    horizon_enplanements: int | None
    delta_enplanements: int | None  # ΔE = E(b+h) - E(b), the primary ranking metric
    cagr: float | None  # percent per year over h years
    base_operations: int | None
    horizon_operations: int | None
    delta_operations: int | None  # commercial operations, shown separately from ΔE


def taf_growth(rows: pd.DataFrame, base_year: int, horizon_year: int) -> TafGrowth:
    """ΔE, CAGR and the change in commercial operations from one airport's TAF rows."""

    def year_total(column: str, year: int) -> int | None:
        if rows.empty:
            return None
        return as_int(column_sum(rows[rows["fiscal_year"] == year], column))

    e_base = year_total("total_enplanements", base_year)
    e_horizon = year_total("total_enplanements", horizon_year)
    ops_base = year_total("commercial_operations", base_year)
    ops_horizon = year_total("commercial_operations", horizon_year)
    return TafGrowth(
        base_year=base_year,
        horizon_year=horizon_year,
        base_enplanements=e_base,
        horizon_enplanements=e_horizon,
        delta_enplanements=(e_horizon - e_base if e_base is not None and e_horizon is not None else None),
        cagr=cagr_percent(e_base, e_horizon, horizon_year - base_year),
        base_operations=ops_base,
        horizon_operations=ops_horizon,
        delta_operations=(
            ops_horizon - ops_base if ops_base is not None and ops_horizon is not None else None
        ),
    )


def _enplanements_by_airport(taf: pd.DataFrame, year: int) -> pd.Series:
    rows = taf[taf["fiscal_year"] == year]
    values = pd.to_numeric(rows["total_enplanements"], errors="coerce")
    present = values.notna()
    return values[present].groupby(rows.loc[present, "faa_locid"]).sum()


def national_taf_cagr(taf: pd.DataFrame, base_year: int, horizon_year: int) -> NationalReference:
    """National S1 reference: CAGR of total enplanements summed over every TAF airport in the
    edition that reports both the base year and the horizon year."""
    base = _enplanements_by_airport(taf, base_year) if not taf.empty else pd.Series(dtype=float)
    horizon = _enplanements_by_airport(taf, horizon_year) if not taf.empty else pd.Series(dtype=float)
    common = base.index.intersection(horizon.index)
    base_sum = float(base[common].sum())
    horizon_sum = float(horizon[common].sum())
    airports = len(common)
    return NationalReference(
        metric="taf_cagr",
        period_key=taf_period_key(base_year, horizon_year),
        value=cagr_percent(base_sum, horizon_sum, horizon_year - base_year) if airports else None,
        sample_size=airports,
        details={
            "base_year": base_year,
            "horizon_year": horizon_year,
            "airports": airports,
            "base_enplanements": base_sum,
            "horizon_enplanements": horizon_sum,
        },
    )


# ---------------------------------------------------------------------------------------------
# Seat load factor (S2) and passenger/seat growth
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadFactor:
    """Seat load factor 100 x ΣPASSENGERS / ΣSEATS with the sums behind it."""

    value: float | None  # percent
    passengers: float | None
    seats: float | None
    departures: float | None
    rows: int


def load_factor(segments: pd.DataFrame) -> LoadFactor:
    """Load factor of segment rows (already filtered to one origin, service class and period)."""
    passengers = column_sum(segments, "passengers")
    seats = column_sum(segments, "seats")
    return LoadFactor(
        value=percent(passengers, seats),
        passengers=passengers,
        seats=seats,
        departures=column_sum(segments, "departures_performed"),
        rows=len(segments),
    )


def national_load_factor(origin_totals: pd.DataFrame, year: int) -> NationalReference:
    """National S2 reference from per-origin T-100 sums (class F, US origins, all 12 months)."""
    totals = load_factor(origin_totals)
    airports = 0 if origin_totals.empty else int((_numeric(origin_totals["seats"]) > 0).sum())
    return NationalReference(
        metric="t100_load_factor",
        period_key=calendar_label(year),
        value=totals.value,
        sample_size=airports,
        details={
            "passengers": totals.passengers,
            "seats": totals.seats,
            "origin_airports": airports,
            "service_class": SCHEDULED_PASSENGER_CLASS,
            "origin_country": NATIONAL_ORIGIN_COUNTRY,
        },
    )


# ---------------------------------------------------------------------------------------------
# Taxi times from whole-minute histograms (S3 and the congestion comparison)
# ---------------------------------------------------------------------------------------------


def histograms_by_airport(rows: pd.DataFrame) -> dict[int, Histogram]:
    """Merge histogram rows (airport_id, minutes, flights) into one histogram per airport,
    summing the flights of each minute across months."""
    if rows.empty:
        return {}
    frame = pd.DataFrame(
        {
            "airport_id": pd.to_numeric(rows["airport_id"], errors="coerce"),
            "minutes": pd.to_numeric(rows["minutes"], errors="coerce"),
            "flights": pd.to_numeric(rows["flights"], errors="coerce"),
        }
    ).dropna()
    frame = frame[frame["flights"] > 0]
    grouped = frame.groupby(["airport_id", "minutes"], as_index=False)["flights"].sum()
    result: dict[int, Histogram] = {}
    for airport_id, minute, flights in grouped.itertuples(index=False, name=None):
        result.setdefault(int(airport_id), {})[int(minute)] = round(flights)
    return result


def merge_histograms(histograms: Iterable[Mapping[int, int]]) -> Histogram:
    merged: Histogram = {}
    for histogram in histograms:
        for minute, flights in histogram.items():
            merged[minute] = merged.get(minute, 0) + flights
    return merged


def grouped_quantile(histogram: Mapping[int, int], q: float) -> float | None:
    """Quantile of whole-minute data computed as a grouped-data percentile.

    BTS reports taxi times in whole minutes, so a reported value v stands for the interval
    [v - 0.5, v + 0.5). With n flights and target rank r = q x n, the minute bins are walked in
    ascending order; at the first bin v whose cumulative count c + f reaches r (c flights before
    the bin, f flights in it) the result is the linear interpolation (v - 0.5) + (r - c) / f.
    This avoids the ties that a nearest-rank percentile produces on integer minutes. The target
    rank uses exact rational arithmetic so bin boundaries are not affected by rounding.
    Returns None for an empty histogram.
    """
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be between 0 and 1")
    bins = sorted((int(minute), int(flights)) for minute, flights in histogram.items() if flights > 0)
    n = sum(flights for _, flights in bins)
    if n == 0:
        return None
    target = Fraction(q).limit_denominator(1_000_000) * n
    before = 0
    for minute, flights in bins:
        if before + flights >= target:
            return float(Fraction(2 * minute - 1, 2) + (target - before) / flights)
        before += flights
    raise AssertionError("unreachable: the last bin always reaches the target rank")


@dataclass(frozen=True)
class TaxiStats:
    """Median, 10th percentile and taxi-out excess (median - P10) of one histogram.

    P10 approximates the airport's unimpeded taxi time (spec section 4.3), so the excess reflects
    congestion rather than airport size or the gate-to-runway distance.
    """

    sample: int  # flights in the histogram
    median: float | None
    p10: float | None
    excess: float | None


def taxi_stats(histogram: Mapping[int, int]) -> TaxiStats:
    sample = sum(flights for flights in histogram.values() if flights > 0)
    p50 = grouped_quantile(histogram, 0.5)
    p10 = grouped_quantile(histogram, 0.1)
    excess = p50 - p10 if p50 is not None and p10 is not None else None
    return TaxiStats(sample=sample, median=p50, p10=p10, excess=excess)


def national_taxi_out_excess(histograms: pd.DataFrame, year: int, min_departures: int) -> NationalReference:
    """National S3 reference: the ordinary median of the taxi-out excess over all airports with
    at least `min_departures` departures in the taxi-out histogram for the year."""
    excesses: list[float] = []
    departures = 0
    for _, histogram in sorted(histograms_by_airport(histograms).items()):
        stats = taxi_stats(histogram)
        if stats.sample >= min_departures and stats.excess is not None:
            excesses.append(stats.excess)
            departures += stats.sample
    return NationalReference(
        metric="ontime_taxi_out_excess_median",
        period_key=calendar_label(year),
        value=median(excesses),
        sample_size=len(excesses),
        details={"min_departures": min_departures, "airports": len(excesses), "departures": departures},
    )


# ---------------------------------------------------------------------------------------------
# Long-haul share (spec section 4.5)
# ---------------------------------------------------------------------------------------------


def aircraft_config_label(config: int) -> str:
    return AIRCRAFT_CONFIG_LABELS.get(config, OTHER_AIRCRAFT_CONFIG_LABEL)


def distance_group_label(group: int) -> str:
    """Label of a BTS 500-mile distance group: 1 -> "< 500 mi", 8 -> "3,500–3,999 mi"."""
    if group <= 1:
        return f"< {DISTANCE_GROUP_MILES:,} mi"
    low = DISTANCE_GROUP_MILES * (group - 1)
    high = DISTANCE_GROUP_MILES * group - 1
    return f"{low:,}–{high:,} mi"


@dataclass(frozen=True)
class LongHaulBreakdown:
    total_departures: float
    valid_departures: float  # departures with a distance that is present and greater than zero
    long_haul_departures: float  # valid departures with distance strictly above the threshold
    long_haul_share: float | None  # percent of valid departures
    excluded_departures: float  # departures with a missing or non-positive distance
    excluded_share: float | None  # percent of all departures performed
    by_aircraft_config: list[AircraftConfigShare]
    distance_groups: list[DistanceGroupRow]
    top_destinations: list[DestinationRow]


def long_haul_breakdown(
    segments: pd.DataFrame, threshold_miles: float, *, top_destinations: int = 10
) -> LongHaulBreakdown:
    """Share of departures performed whose segment distance exceeds the threshold.

    share = 100 x ΣDEPARTURES_PERFORMED(distance > X) / ΣDEPARTURES_PERFORMED(valid distance),
    where a valid distance is present and greater than zero; a distance exactly equal to X is not
    long-haul. The aircraft-configuration entries and distance groups reconcile to the totals.
    """
    if segments.empty:
        return LongHaulBreakdown(0.0, 0.0, 0.0, None, 0.0, None, [], [], [])
    segments = segments.reset_index(drop=True)
    departures = _numeric(segments["departures_performed"])
    distance = _numeric(segments["distance"])
    valid = distance.notna() & (distance > 0)
    long_haul = valid & (distance > threshold_miles)
    performed = departures > 0

    total = _total(departures)
    valid_total = _total(departures[valid])
    long_total = _total(departures[long_haul])
    excluded = _total(departures[~valid])

    config = _numeric(segments["aircraft_config"]).fillna(UNKNOWN_AIRCRAFT_CONFIG).astype(int)
    by_config: list[AircraftConfigShare] = []
    for code in sorted(set(config[performed].tolist())):
        in_config = config == code
        config_valid = _total(departures[valid & in_config])
        config_long = _total(departures[long_haul & in_config])
        by_config.append(
            AircraftConfigShare(
                aircraft_config=code,
                label=aircraft_config_label(code),
                long_haul_departures=config_long,
                valid_departures=config_valid,
                share=percent(config_long, config_valid),
            )
        )

    derived_group = distance // DISTANCE_GROUP_MILES + 1
    group = _numeric(segments["distance_group"]).fillna(derived_group)
    groups: list[DistanceGroupRow] = []
    for number in sorted(set(group[valid & performed].astype(int).tolist())):
        group_departures = _total(departures[valid & (group == number)])
        groups.append(
            DistanceGroupRow(
                distance_group=number,
                label=distance_group_label(number),
                departures=group_departures,
                share=percent(group_departures, valid_total),
            )
        )

    return LongHaulBreakdown(
        total_departures=total,
        valid_departures=valid_total,
        long_haul_departures=long_total,
        long_haul_share=percent(long_total, valid_total),
        excluded_departures=excluded,
        excluded_share=percent(excluded, total),
        by_aircraft_config=by_config,
        distance_groups=groups,
        top_destinations=_top_destinations(segments[long_haul & performed], departures, top_destinations),
    )


def _top_destinations(rows: pd.DataFrame, departures: pd.Series, limit: int) -> list[DestinationRow]:
    """Destinations of long-haul rows ranked by departures (ties by destination code)."""
    if rows.empty:
        return []
    frame = pd.DataFrame(
        {
            "dest": rows["dest"],
            "dest_city_name": rows["dest_city_name"],
            "distance": _numeric(rows["distance"]),
            "departures": departures[rows.index],
        }
    )
    summary = (
        frame.groupby("dest", as_index=False)
        .agg(departures=("departures", "sum"), distance=("distance", "max"))
        .sort_values(["departures", "dest"], ascending=[False, True])
        .head(limit)
    )
    named = frame.dropna(subset=["dest_city_name"]).sort_values(["dest", "dest_city_name"])
    names = dict(named.drop_duplicates("dest")[["dest", "dest_city_name"]].itertuples(index=False, name=None))
    return [
        DestinationRow(
            dest_code=str(dest),
            dest_name=str(names[dest]) if dest in names else None,
            distance_miles=float(miles),
            departures=float(count),
        )
        for dest, count, miles in summary[["dest", "departures", "distance"]].itertuples(
            index=False, name=None
        )
    ]


# ---------------------------------------------------------------------------------------------
# On-Time departures and delay causes (spec section 4.5)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class DepartureSummary:
    months_covered: int
    days_covered: int | None  # Σ days_in_month over the months with rows
    scheduled: int | None
    cancelled: int | None
    performed: int | None  # scheduled - cancelled; diverted flights did depart
    performed_per_day: float | None
    delay_base: int | None  # non-cancelled departures with a valid DepDel15
    delayed: int | None  # departures with DepDel15 = 1
    delay_rate: float | None  # percent of delay_base
    cancellation_rate: float | None  # percent of scheduled departures


def departure_summary(rows: pd.DataFrame) -> DepartureSummary:
    """Departure volume, delay and cancellation rates from monthly departure aggregates."""
    if rows.empty:
        return DepartureSummary(0, None, None, None, None, None, None, None, None, None)
    scheduled = as_int(column_sum(rows, "scheduled"))
    cancelled = as_int(column_sum(rows, "cancelled"))
    performed = scheduled - cancelled if scheduled is not None and cancelled is not None else None
    days_by_month = (
        pd.to_numeric(rows["days_in_month"], errors="coerce").groupby([rows["year"], rows["month"]]).max()
    )
    days = as_int(to_float(days_by_month.sum(min_count=1)))
    delay_base = as_int(column_sum(rows, "del15_valid"))
    delayed = as_int(column_sum(rows, "del15_count"))
    return DepartureSummary(
        months_covered=len(days_by_month),
        days_covered=days,
        scheduled=scheduled,
        cancelled=cancelled,
        performed=performed,
        performed_per_day=performed / days if performed is not None and days else None,
        delay_base=delay_base,
        delayed=delayed,
        delay_rate=percent(delayed, delay_base),
        cancellation_rate=percent(cancelled, scheduled),
    )


@dataclass(frozen=True)
class DelayCauseSummary:
    arrivals: int | None
    attributed_minutes: float | None  # sum of the five cause minutes
    causes: list[DelayCauseShare]


def delay_cause_summary(arrivals: pd.DataFrame) -> DelayCauseSummary:
    """Each cause's share of the attributed delay minutes at arrivals (None when the sum is 0)."""
    minutes = {cause: column_sum(arrivals, column) for cause, column in DELAY_COLUMNS.items()}
    known = [value for value in minutes.values() if value is not None]
    total = math.fsum(known) if len(known) == len(minutes) else None
    return DelayCauseSummary(
        arrivals=as_int(column_sum(arrivals, "arrivals")),
        attributed_minutes=total,
        causes=[
            DelayCauseShare(cause=cause, minutes=value, share=percent(value, total))
            for cause, value in minutes.items()
        ],
    )


def monthly_delay_causes(
    arrivals: pd.DataFrame, year: int, months: Iterable[int] = ALL_MONTHS
) -> list[MonthlyDelayCauses]:
    """Delay-cause shares for each month of the year; months without rows stay None."""
    result: list[MonthlyDelayCauses] = []
    for month in months:
        rows = (
            arrivals
            if arrivals.empty
            else arrivals[(arrivals["year"] == year) & (arrivals["month"] == month)]
        )
        summary = delay_cause_summary(rows)
        result.append(
            MonthlyDelayCauses(
                year=year,
                month=month,
                arrivals=summary.arrivals,
                attributed_delay_minutes=summary.attributed_minutes,
                causes=summary.causes,
            )
        )
    return result
