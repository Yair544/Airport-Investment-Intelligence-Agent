"""BTS T-100 Segment summary by origin airport and month (public SODA JSON API, no key).

This is an all-class rollup of the same T-100 Segment data. It is used only to
(a) find the latest calendar year whose 12 months are published,
(b) fingerprint a T-100 year cheaply (sha256 of that year's monthly totals per origin), so an
    unchanged year is not downloaded again, and
(c) cross-check each ingested T-100 year (sums per origin and month must match).
Its numbers are never added to metrics, so the same activity is never counted twice.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.data.download import DownloadError, SafeHttpClient, safe_child
from app.data.providers import NormalizedTable, ParsedFile, SourceFormatError

SOURCE_ID = "bts_t100_summary"
API_URL = "https://data.bts.gov/resource/r495-tyji.json"
PAGE_SIZE = 5000
MAX_PAGES = 40
FIELDS = "origin_airport_id,origin_airport_code,reporting_month,total_departures,total_passengers,total_seats"
COLUMNS = ["airport_id", "code", "year", "month", "departures", "passengers", "seats"]


@dataclass(frozen=True)
class SummaryYear:
    year: int
    frame: pd.DataFrame  # COLUMNS
    content: bytes  # canonical JSON stored as the source file
    sha256: str

    @property
    def months(self) -> list[int]:
        return sorted(int(month) for month in self.frame["month"].unique())


def _month(value: Any) -> tuple[int, int]:
    text = str(value)
    try:
        return int(text[0:4]), int(text[5:7])
    except (TypeError, ValueError) as exc:
        raise SourceFormatError(
            f"Unexpected reporting_month value {text[:20]!r} in the T-100 summary."
        ) from exc


def latest_month(client: SafeHttpClient) -> tuple[int, int]:
    """Latest (year, month) published in the summary API."""
    rows = client.get_json(API_URL, params={"$select": "max(reporting_month) AS latest"})
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict) or not rows[0].get("latest"):
        raise SourceFormatError("The T-100 summary API did not return its latest month.")
    return _month(rows[0]["latest"])


def latest_complete_year(client: SafeHttpClient) -> int:
    year, month = latest_month(client)
    return year if month == 12 else year - 1


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise SourceFormatError("The T-100 summary API returned a non-numeric total.") from exc


def normalize_rows(rows: list[dict[str, Any]], year: int) -> pd.DataFrame:
    records = []
    for row in rows:
        if not isinstance(row, dict):
            raise SourceFormatError("The T-100 summary API returned an unexpected record.")
        row_year, month = _month(row.get("reporting_month"))
        if row_year != year or not 1 <= month <= 12:
            raise SourceFormatError(f"The T-100 summary API returned a record outside {year}.")
        try:
            airport_id = int(str(row["origin_airport_id"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise SourceFormatError("A T-100 summary record has no valid origin airport id.") from exc
        records.append(
            (
                airport_id,
                str(row.get("origin_airport_code") or "").strip() or None,
                year,
                month,
                _number(row.get("total_departures")),
                _number(row.get("total_passengers")),
                _number(row.get("total_seats")),
            )
        )
    frame = pd.DataFrame.from_records(records, columns=COLUMNS)
    if frame.duplicated(["airport_id", "month"]).any():
        raise SourceFormatError("The T-100 summary API returned more than one record per airport and month.")
    return frame.sort_values(["airport_id", "month"], ignore_index=True)


def canonical_content(frame: pd.DataFrame, year: int) -> bytes:
    """Stable serialization of one year's totals; its sha256 is the T-100 change fingerprint."""
    columns = ["airport_id", "code", "month", "departures", "passengers", "seats"]
    rows = [
        [int(airport_id), code, int(month), _plain(departures), _plain(passengers), _plain(seats)]
        for airport_id, code, month, departures, passengers, seats in frame[columns].itertuples(
            index=False, name=None
        )
    ]
    payload = {
        "source": API_URL,
        "year": year,
        "columns": ["airport_id", "code", "month", "departures", "passengers", "seats"],
        "rows": rows,
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _plain(value: Any) -> float | int | None:
    if value is None or pd.isna(value):
        return None
    number = float(value)
    return int(number) if number.is_integer() else number


def fetch_year(client: SafeHttpClient, year: int) -> SummaryYear:
    rows: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        batch = client.get_json(
            API_URL,
            params={
                "$select": FIELDS,
                "$where": (
                    f"reporting_month between '{year:04d}-01-01T00:00:00' and '{year:04d}-12-31T23:59:59'"
                ),
                "$order": "origin_airport_id,reporting_month",
                "$limit": str(PAGE_SIZE),
                "$offset": str(page * PAGE_SIZE),
            },
        )
        if not isinstance(batch, list):
            raise SourceFormatError("The T-100 summary API returned an unexpected response.")
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
    else:
        raise DownloadError("The T-100 summary API returned more pages than expected.")
    frame = normalize_rows(rows, year)
    content = canonical_content(frame, year)
    return SummaryYear(year=year, frame=frame, content=content, sha256=hashlib.sha256(content).hexdigest())


def write_year_file(summary: SummaryYear, dest_dir: Path, tmp_dir: Path) -> Path:
    """Store the canonical content as `{year}-{sha12}.json` (atomic replace)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    name = f"{summary.year}-{summary.sha256[:12]}"
    final = safe_child(dest_dir, name).with_suffix(".json")
    tmp = tmp_dir / f"{name}.{os.getpid()}.json.part"
    tmp.write_bytes(summary.content)
    os.replace(tmp, final)
    return final


def parse_summary_file(path: Path, year: int) -> ParsedFile:
    try:
        payload = json.loads(path.read_bytes())
        columns = payload["columns"]
        rows = payload["rows"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SourceFormatError("The stored T-100 summary file is not readable.") from exc
    if payload.get("year") != year or columns != [
        "airport_id",
        "code",
        "month",
        "departures",
        "passengers",
        "seats",
    ]:
        raise SourceFormatError("The stored T-100 summary file does not match the requested year.")
    frame = pd.DataFrame.from_records(rows, columns=columns)
    frame.insert(2, "year", year)
    for column in ("departures", "passengers", "seats"):
        frame[column] = frame[column].astype("float64")
    months = sorted(int(month) for month in frame["month"].unique())
    notes = [
        f"T-100 summary CY{year}: {len(frame):,} origin-month totals, months {months[0]}-{months[-1]}."
        if months
        else f"T-100 summary CY{year}: no rows."
    ]
    return ParsedFile(tables=[NormalizedTable("t100_summary_month", frame[COLUMNS])], notes=notes)
