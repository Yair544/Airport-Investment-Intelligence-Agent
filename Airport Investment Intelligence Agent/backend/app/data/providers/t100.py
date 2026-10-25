"""BTS T-100 Segment (All Carriers): one national calendar-year CSV per download.

TranStats has no static file for this table. The download goes through its ASP.NET form: GET
the page (the shared httpx client keeps the session cookie), read the hidden state fields and
POST them back with the year, all geographies and months, the zip option and one checkbox per
requested field. Rows are kept as reported; missing values stay NULL.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data.download import (
    DownloadResult,
    SafeHttpClient,
    parse_aspnet_form,
    transtats_download_payload,
)
from app.data.providers import (
    NormalizedTable,
    ParsedFile,
    SourceFormatError,
    open_zip_member,
    require_columns,
)

SOURCE_ID = "bts_t100_segment"
FORM_URL = (
    "https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FMG"
    "&QO_fu146_anzr=Air+Carrier+Statistics+(Form+41+Traffic)-+All+Carriers"
)
CSV_MEMBER = "T_T100_SEGMENT_ALL_CARRIER.csv"
MAX_MEMBER_BYTES = 1024 * 1024 * 1024
FORM_READ_TIMEOUT_SECONDS = 900.0  # the server builds the national annual file on demand

# Source field -> storage column (t100_segments)
COLUMNS: dict[str, str] = {
    "YEAR": "year",
    "MONTH": "month",
    "UNIQUE_CARRIER": "carrier",
    "ORIGIN_AIRPORT_ID": "origin_airport_id",
    "ORIGIN": "origin",
    "ORIGIN_STATE_ABR": "origin_state",
    "ORIGIN_COUNTRY": "origin_country",
    "DEST_AIRPORT_ID": "dest_airport_id",
    "DEST": "dest",
    "DEST_CITY_NAME": "dest_city_name",
    "DEST_STATE_ABR": "dest_state",
    "DEST_COUNTRY": "dest_country",
    "CLASS": "service_class",
    "AIRCRAFT_CONFIG": "aircraft_config",
    "AIRCRAFT_TYPE": "aircraft_type",
    "DATA_SOURCE": "data_source",
    "DEPARTURES_SCHEDULED": "departures_scheduled",
    "DEPARTURES_PERFORMED": "departures_performed",
    "SEATS": "seats",
    "PASSENGERS": "passengers",
    "FREIGHT": "freight",
    "DISTANCE": "distance",
    "DISTANCE_GROUP": "distance_group",
}
FIELDS: list[str] = list(COLUMNS)
TEXT_FIELDS = [
    "UNIQUE_CARRIER",
    "ORIGIN",
    "ORIGIN_STATE_ABR",
    "ORIGIN_COUNTRY",
    "DEST",
    "DEST_CITY_NAME",
    "DEST_STATE_ABR",
    "DEST_COUNTRY",
    "CLASS",
    "DATA_SOURCE",
]
INTEGER_COLUMNS = [
    "origin_airport_id",
    "dest_airport_id",
    "aircraft_config",
    "aircraft_type",
    "distance_group",
]
MEASURE_COLUMNS = [
    "departures_scheduled",
    "departures_performed",
    "seats",
    "passengers",
    "freight",
    "distance",
]
# Characteristics that identify one reported segment record (spec section 3: carriers, service
# classes and aircraft stay distinct).
IDENTIFYING_KEY = [
    "year",
    "month",
    "carrier",
    "origin_airport_id",
    "dest_airport_id",
    "aircraft_type",
    "aircraft_config",
    "service_class",
    "data_source",
]


def download_year(client: SafeHttpClient, year: int, *, dest_dir: Path, tmp_dir: Path) -> DownloadResult:
    """Download the national T-100 Segment file for one calendar year through the TranStats form."""

    def prepare() -> tuple[str, list[tuple[str, str]]]:
        html, page_url = client.get_text_once(FORM_URL)
        form = parse_aspnet_form(html, page_url)
        return form.action_url, transtats_download_payload(form, year=year, fields=FIELDS)

    return client.download(
        FORM_URL,
        dest_dir=dest_dir,
        tmp_dir=tmp_dir,
        stem=str(int(year)),
        method="POST",
        prepare=prepare,
        read_timeout=max(client.timeout_seconds, FORM_READ_TIMEOUT_SECONDS),
    )


def read_segments(path: Path) -> pd.DataFrame:
    """Read the CSV member with text columns kept as text (only empty fields count as missing)."""
    with open_zip_member(path, CSV_MEMBER, MAX_MEMBER_BYTES) as stream:
        try:
            frame = pd.read_csv(
                stream,
                usecols=lambda column: column in COLUMNS,
                dtype={field: "string" for field in TEXT_FIELDS},
                keep_default_na=False,
                na_values=[""],
                encoding="utf-8",
                encoding_errors="replace",
                low_memory=False,
            )
        except (ValueError, pd.errors.ParserError) as exc:
            raise SourceFormatError("The T-100 Segment CSV could not be parsed.") from exc
    require_columns(frame, FIELDS, "The T-100 Segment CSV")
    return frame


def parse_t100_zip(path: Path, year: int) -> ParsedFile:
    raw = read_segments(path)
    frame = raw.rename(columns=COLUMNS)[list(COLUMNS.values())]
    for column in ["year", "month", *INTEGER_COLUMNS, *MEASURE_COLUMNS]:
        converted = pd.to_numeric(frame[column], errors="coerce")
        if (converted.isna() & frame[column].notna()).any():
            raise SourceFormatError(f"The T-100 Segment CSV has non-numeric values in {column}.")
        frame[column] = converted
    for field in TEXT_FIELDS:
        column = COLUMNS[field]
        frame[column] = frame[column].str.strip().replace({"": pd.NA})
    if frame.empty:
        raise SourceFormatError("The T-100 Segment file has no rows.")
    if frame["year"].isna().any() or (frame["year"] != year).any():
        raise SourceFormatError(f"The T-100 Segment file contains rows outside calendar year {year}.")
    if frame["month"].isna().any() or not frame["month"].between(1, 12).all():
        raise SourceFormatError("The T-100 Segment file has invalid months.")
    for column in ["year", "month", *INTEGER_COLUMNS]:
        frame[column] = frame[column].astype("Int64")
    for column in MEASURE_COLUMNS:
        frame[column] = frame[column].astype("float64")

    notes = [f"T-100 Segment CY{year}: {len(frame):,} rows, months {_months_text(frame['month'])}."]
    duplicated = frame.duplicated(IDENTIFYING_KEY, keep=False)
    if duplicated.any():
        exact = frame.duplicated(keep=False)
        notes.append(
            f"{int(duplicated.sum()):,} rows share their identifying characteristics with another row "
            f"({int(exact.sum()):,} of them are identical in every loaded field); all rows are kept as "
            "reported."
        )
    for column in ("origin_airport_id", "dest_airport_id"):
        missing = int(frame[column].isna().sum())
        if missing:
            notes.append(f"{missing:,} rows have no {column}.")
    nonpositive = int((frame["distance"] <= 0).sum())
    if nonpositive:
        notes.append(f"{nonpositive:,} rows report a distance of zero or less (stored as reported).")
    return ParsedFile(tables=[NormalizedTable("t100_segments", frame)], notes=notes)


def _months_text(months: pd.Series) -> str:
    values = sorted(int(month) for month in months.dropna().unique())
    if values and values == list(range(values[0], values[-1] + 1)):
        return f"{values[0]}-{values[-1]}"
    return ",".join(str(month) for month in values)
