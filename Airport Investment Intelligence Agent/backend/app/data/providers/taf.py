"""FAA Terminal Area Forecast (TAF): annual edition ZIP with Excel workbooks.

Totals follow the FAA TAF glossary: total enplanements = air carrier + air taxi + commuter +
international US flag + international foreign flag; commercial operations = itinerant air
carrier + itinerant air taxi/commuter operations. Years are FAA fiscal years (October 1 to
September 30); scenario 0 rows are historical and scenario 1 rows are forecasts.
"""

from __future__ import annotations

import io
import re
import warnings
import zipfile
from datetime import date
from pathlib import Path

import pandas as pd

from app.data.download import RemoteMeta, SafeHttpClient
from app.data.providers import (
    NormalizedTable,
    ParsedFile,
    SourceFormatError,
    read_zip_member,
    require_columns,
)

SOURCE_ID = "faa_taf"
DOWNLOAD_URL = "https://taf.faa.gov/Downloads/APO100_TAF_Final_{year}.zip"
AIRPORTS_MEMBER = "Airports.xlsx"
ENPLANEMENTS_MEMBER = "Enplanements.xlsx"
OPERATIONS_MEMBER = "AirportsOperations.xlsx"
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_WORKBOOK_XML_BYTES = 512 * 1024 * 1024
HUB_SIZES = {3: "large", 2: "medium", 1: "small", 0: "nonhub"}
ENPLANEMENT_COLUMNS = ["aac", "aat", "commuter", "us_flag", "frgn_flag"]
_WHITESPACE = re.compile(r"\s+")


def edition_url(year: int) -> str:
    return DOWNLOAD_URL.format(year=int(year))


def edition_label(year: int) -> str:
    return f"TAF {year}"


def dataset_version(year: int) -> str:
    return f"TAF {year} edition"


def discover_latest_edition(client: SafeHttpClient, today: date) -> tuple[int, RemoteMeta] | None:
    """Newest published edition among next year, this year and last year (FAA naming)."""
    for year in (today.year + 1, today.year, today.year - 1):
        meta = client.head(edition_url(year))
        if meta is not None:
            return year, meta
    return None


def clean_text(value: object) -> str | None:
    """Strip the fixed-width padding of TAF text fields and collapse inner whitespace."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    text = _WHITESPACE.sub(" ", str(value)).strip()
    return text or None


def _read_workbook(path: Path, member: str, columns: list[str], what: str) -> pd.DataFrame:
    data = read_zip_member(path, member, MAX_MEMBER_BYTES)
    try:  # an .xlsx is itself a ZIP: bound its expanded size before openpyxl parses it
        with zipfile.ZipFile(io.BytesIO(data)) as workbook:
            if sum(info.file_size for info in workbook.infolist()) > MAX_WORKBOOK_XML_BYTES:
                raise SourceFormatError(f"{what} expands beyond the size limit.")
    except zipfile.BadZipFile as exc:
        raise SourceFormatError(f"{what} is not a valid Excel workbook.") from exc
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # openpyxl warns about the workbook's missing styles
        try:
            frame = pd.read_excel(io.BytesIO(data), engine="openpyxl", dtype=object)
        except Exception as exc:  # openpyxl raises a variety of parsing errors
            raise SourceFormatError(f"{what} could not be read as an Excel workbook.") from exc
    require_columns(frame, columns, what)
    return frame[columns].copy()


def _numeric(frame: pd.DataFrame, columns: list[str], what: str) -> None:
    for column in columns:
        converted = pd.to_numeric(frame[column], errors="coerce")
        bad = converted.isna() & frame[column].notna()
        if bad.any():
            raise SourceFormatError(f"{what} has non-numeric values in column {column}.")
        frame[column] = converted.astype("float64")


def _year_scenario(frame: pd.DataFrame, what: str) -> None:
    frame["locid"] = frame["locid"].map(clean_text)
    if frame["locid"].isna().any():
        raise SourceFormatError(f"{what} has rows without a location identifier.")
    for column in ("scenario", "ayear"):
        converted = pd.to_numeric(frame[column], errors="coerce")
        if converted.isna().any():
            raise SourceFormatError(f"{what} has rows without a valid {column}.")
        frame[column] = converted.astype("int64")
    if not frame["scenario"].isin([0, 1]).all():
        raise SourceFormatError(f"{what} has scenario values other than 0 (historical) and 1 (forecast).")
    if frame.duplicated(["locid", "ayear"]).any():
        raise SourceFormatError(f"{what} has more than one row for the same airport and fiscal year.")


def parse_taf_zip(path: Path, edition_year: int) -> ParsedFile:
    """Normalize one TAF edition into `taf_airports` and `taf_annual` rows."""
    airports = _read_workbook(
        path,
        AIRPORTS_MEMBER,
        ["LOCID", "APORT_NAME", "CITY", "STATE", "REGION", "HUB_SIZE"],
        "TAF Airports.xlsx",
    )
    enplanements = _read_workbook(
        path,
        ENPLANEMENTS_MEMBER,
        ["locid", "scenario", "ayear", *ENPLANEMENT_COLUMNS],
        "TAF Enplanements.xlsx",
    )
    operations = _read_workbook(
        path,
        OPERATIONS_MEMBER,
        ["locid", "scenario", "ayear", "itn_Ac", "itn_at"],
        "TAF AirportsOperations.xlsx",
    )

    airport_rows = pd.DataFrame(
        {
            "faa_locid": airports["LOCID"].map(clean_text),
            "name": airports["APORT_NAME"].map(clean_text),
            "city": airports["CITY"].map(clean_text),
            "state": airports["STATE"].map(clean_text),
            "faa_region": airports["REGION"].map(clean_text),
            "hub_size": pd.to_numeric(airports["HUB_SIZE"], errors="coerce").astype("Int64"),
        }
    )
    if airport_rows["faa_locid"].isna().any() or airport_rows["faa_locid"].duplicated().any():
        raise SourceFormatError("TAF Airports.xlsx has missing or repeated location identifiers.")

    _year_scenario(enplanements, "TAF Enplanements.xlsx")
    _year_scenario(operations, "TAF AirportsOperations.xlsx")
    _numeric(enplanements, ENPLANEMENT_COLUMNS, "TAF Enplanements.xlsx")
    _numeric(operations, ["itn_Ac", "itn_at"], "TAF AirportsOperations.xlsx")
    # A missing component keeps the total missing (skipna=False) instead of under-counting it.
    enplanements["total_enplanements"] = enplanements[ENPLANEMENT_COLUMNS].sum(axis=1, skipna=False)
    operations["commercial_operations"] = operations[["itn_Ac", "itn_at"]].sum(axis=1, skipna=False)

    annual = enplanements.merge(
        operations, on=["locid", "ayear"], how="outer", suffixes=("", "_ops"), validate="one_to_one"
    )
    scenario = annual["scenario"].fillna(annual["scenario_ops"])
    conflicts = (
        annual["scenario"].notna()
        & annual["scenario_ops"].notna()
        & (annual["scenario"] != annual["scenario_ops"])
    )
    if conflicts.any():
        raise SourceFormatError("TAF enplanement and operations workbooks disagree on forecast years.")
    annual_rows = pd.DataFrame(
        {
            "faa_locid": annual["locid"],
            "fiscal_year": annual["ayear"].astype("int64"),
            "is_forecast": scenario.astype("int64"),
            "enpl_air_carrier": annual["aac"],
            "enpl_air_taxi": annual["aat"],
            "enpl_commuter": annual["commuter"],
            "enpl_intl_us_flag": annual["us_flag"],
            "enpl_intl_foreign_flag": annual["frgn_flag"],
            "total_enplanements": annual["total_enplanements"],
            "ops_itinerant_air_carrier": annual["itn_Ac"],
            "ops_itinerant_air_taxi": annual["itn_at"],
            "commercial_operations": annual["commercial_operations"],
        }
    ).sort_values(["faa_locid", "fiscal_year"], ignore_index=True)

    historical = annual_rows.loc[annual_rows["is_forecast"] == 0, "fiscal_year"]
    if historical.empty or (annual_rows["is_forecast"] == 1).sum() == 0:
        raise SourceFormatError("The TAF edition must contain both historical and forecast years.")
    base_year = int(historical.max())
    notes = [
        f"{edition_label(edition_year)}: {len(airport_rows)} airports, fiscal years "
        f"{int(annual_rows['fiscal_year'].min())}-{int(annual_rows['fiscal_year'].max())}, "
        f"base (last historical) year FY{base_year}."
    ]
    unknown = sorted(set(annual_rows["faa_locid"]) - set(airport_rows["faa_locid"]))
    if unknown:
        notes.append(f"{len(unknown)} location identifiers have annual rows but no airport record.")
    return ParsedFile(
        tables=[NormalizedTable("taf_airports", airport_rows), NormalizedTable("taf_annual", annual_rows)],
        dataset_version=dataset_version(edition_year),
        notes=notes,
    )
