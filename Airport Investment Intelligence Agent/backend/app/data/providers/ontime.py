"""BTS Reporting Carrier On-Time Performance: official monthly PREZIP files, stored as aggregates.

Per-flight rows are not stored (about 7 million flights a year). Each month is reduced to:
- departures per origin airport: scheduled flights (every row), cancelled, diverted,
  `del15_valid` (not cancelled and DepDel15 reported) and `del15_count` (DepDel15 = 1);
- arrivals per destination airport: flights that arrived (not cancelled and not diverted) and
  the summed delay-cause minutes reported for them;
- whole-minute taxi histograms: TaxiOut of non-cancelled departures at the origin and TaxiIn of
  arrivals at the destination (fractional minutes round half up; negative values are invalid).
The histograms keep exact medians and percentiles available without the flight rows.
"""

from __future__ import annotations

import calendar
from pathlib import Path

import numpy as np
import pandas as pd

from app.data.download import RemoteMeta, SafeHttpClient
from app.data.providers import (
    NormalizedTable,
    ParsedFile,
    SourceFormatError,
    open_zip_member,
    require_columns,
)

SOURCE_ID = "bts_ontime"
PREZIP_URL = (
    "https://transtats.bts.gov/PREZIP/"
    "On_Time_Reporting_Carrier_On_Time_Performance_1987_present_{year}_{month}.zip"
)
CSV_MEMBER = "On_Time_Reporting_Carrier_On_Time_Performance_(1987_present)_{year}_{month}.csv"
MAX_MEMBER_BYTES = 2 * 1024 * 1024 * 1024

DELAY_COLUMNS: dict[str, str] = {
    "CarrierDelay": "delay_carrier",
    "WeatherDelay": "delay_weather",
    "NASDelay": "delay_nas",
    "SecurityDelay": "delay_security",
    "LateAircraftDelay": "delay_late_aircraft",
}
REQUIRED_COLUMNS = [
    "Year",
    "Month",
    "OriginAirportID",
    "Origin",
    "DestAirportID",
    "Dest",
    "DepDel15",
    "TaxiOut",
    "TaxiIn",
    "Cancelled",
    "Diverted",
    *DELAY_COLUMNS,
]
NUMERIC_COLUMNS = [column for column in REQUIRED_COLUMNS if column not in ("Origin", "Dest")]


def month_url(year: int, month: int) -> str:
    return PREZIP_URL.format(year=int(year), month=int(month))  # BTS does not zero-pad months


def member_name(year: int, month: int) -> str:
    return CSV_MEMBER.format(year=int(year), month=int(month))


def period_key(year: int, month: int) -> str:
    return f"{int(year):04d}-{int(month):02d}"


def month_meta(client: SafeHttpClient, year: int, month: int) -> RemoteMeta | None:
    """HEAD of a monthly file; None while BTS has not published it."""
    return client.head(month_url(year, month))


def read_flights(path: Path, year: int, month: int) -> pd.DataFrame:
    with open_zip_member(path, member_name(year, month), MAX_MEMBER_BYTES) as stream:
        try:
            frame = pd.read_csv(
                stream,
                usecols=lambda column: column in REQUIRED_COLUMNS,
                dtype={"Origin": "string", "Dest": "string"},
                keep_default_na=False,
                na_values=[""],
                low_memory=False,
            )
        except (ValueError, pd.errors.ParserError) as exc:
            raise SourceFormatError("The On-Time Performance CSV could not be parsed.") from exc
    require_columns(frame, REQUIRED_COLUMNS, "The On-Time Performance CSV")
    for column in NUMERIC_COLUMNS:
        converted = pd.to_numeric(frame[column], errors="coerce")
        if (converted.isna() & frame[column].notna()).any():
            raise SourceFormatError(f"The On-Time Performance CSV has non-numeric values in {column}.")
        frame[column] = converted
    return frame


def _whole_minutes(values: pd.Series) -> pd.Series:
    return np.floor(values + 0.5).astype("int64")  # round half up


def _codes(airport_ids: pd.Series, codes: pd.Series) -> pd.Series:
    """Most frequent code reported for each airport id in the month."""
    counts = pd.DataFrame({"airport_id": airport_ids, "code": codes}).value_counts().reset_index()
    counts = counts.sort_values(["airport_id", "count", "code"], ascending=[True, False, True])
    return counts.drop_duplicates("airport_id").set_index("airport_id")["code"]


def aggregate_flights(flights: pd.DataFrame, year: int, month: int) -> ParsedFile:
    if flights.empty:
        raise SourceFormatError("The On-Time Performance file has no rows.")
    if not ((flights["Year"] == year) & (flights["Month"] == month)).all():
        raise SourceFormatError(
            f"The On-Time Performance file contains flights outside {period_key(year, month)}."
        )
    for column in ("OriginAirportID", "DestAirportID", "Cancelled", "Diverted"):
        if flights[column].isna().any():
            raise SourceFormatError(f"The On-Time Performance file has rows without {column}.")
    for column in ("Cancelled", "Diverted"):
        if not flights[column].isin([0, 1]).all():
            raise SourceFormatError(f"The On-Time Performance file has {column} values other than 0 and 1.")

    origin = flights["OriginAirportID"].astype("int64")
    dest = flights["DestAirportID"].astype("int64")
    cancelled = flights["Cancelled"] == 1
    diverted = flights["Diverted"] == 1
    arrived = ~cancelled & ~diverted
    del15_valid = ~cancelled & flights["DepDel15"].notna()
    days = calendar.monthrange(year, month)[1]

    departures = (
        pd.DataFrame(
            {
                "airport_id": origin,
                "scheduled": 1,
                "cancelled": cancelled.astype("int64"),
                "diverted": diverted.astype("int64"),
                "del15_valid": del15_valid.astype("int64"),
                "del15_count": (del15_valid & (flights["DepDel15"] == 1)).astype("int64"),
            }
        )
        .groupby("airport_id", as_index=False)
        .sum()
    )
    departures.insert(1, "code", departures["airport_id"].map(_codes(origin, flights["Origin"])))
    departures.insert(2, "year", year)
    departures.insert(3, "month", month)
    departures.insert(4, "days_in_month", days)

    arrival_parts: dict[str, pd.Series] = {"airport_id": dest, "arrivals": arrived.astype("int64")}
    for source, column in DELAY_COLUMNS.items():
        arrival_parts[column] = flights[source].where(arrived)
    grouped = pd.DataFrame(arrival_parts).groupby("airport_id")
    arrivals = grouped["arrivals"].sum().to_frame()
    for column in DELAY_COLUMNS.values():
        arrivals[column] = grouped[column].sum(min_count=1)  # no reported value stays missing
    arrivals = arrivals.reset_index()
    arrivals.insert(1, "code", arrivals["airport_id"].map(_codes(dest, flights["Dest"])))
    arrivals.insert(2, "year", year)
    arrivals.insert(3, "month", month)

    taxi_out_ok = ~cancelled & flights["TaxiOut"].notna() & (flights["TaxiOut"] >= 0)
    taxi_in_ok = arrived & flights["TaxiIn"].notna() & (flights["TaxiIn"] >= 0)
    histograms = []
    for kind, mask, airports, values in (
        ("taxi_out", taxi_out_ok, origin, flights["TaxiOut"]),
        ("taxi_in", taxi_in_ok, dest, flights["TaxiIn"]),
    ):
        counts = (
            pd.DataFrame({"airport_id": airports[mask], "minutes": _whole_minutes(values[mask])})
            .value_counts()
            .rename("flights")
            .reset_index()
        )
        counts.insert(1, "year", year)
        counts.insert(2, "month", month)
        counts.insert(3, "kind", kind)
        histograms.append(counts)
    taxi = pd.concat(histograms, ignore_index=True).sort_values(
        ["kind", "airport_id", "minutes"], ignore_index=True
    )

    notes = [
        f"On-Time {period_key(year, month)}: {len(flights):,} flights, {int(cancelled.sum()):,} cancelled, "
        f"{int(diverted.sum()):,} diverted, {departures['airport_id'].nunique()} origin airports."
    ]
    missing_del15 = int((~cancelled & flights["DepDel15"].isna()).sum())
    if missing_del15:
        notes.append(f"{missing_del15:,} non-cancelled departures have no DepDel15 value.")
    for label, column, base in (("TaxiOut", "TaxiOut", ~cancelled), ("TaxiIn", "TaxiIn", arrived)):
        negative = int((base & (flights[column] < 0)).sum())
        if negative:
            notes.append(f"{negative:,} negative {label} values were excluded as invalid.")
    return ParsedFile(
        tables=[
            NormalizedTable("otp_departures_month", departures),
            NormalizedTable("otp_arrivals_month", arrivals),
            NormalizedTable("otp_taxi_hist", taxi),
        ],
        notes=notes,
    )


def parse_ontime_zip(path: Path, year: int, month: int) -> ParsedFile:
    return aggregate_flights(read_flights(path, year, month), year, month)
