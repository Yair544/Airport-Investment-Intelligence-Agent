"""Source adapters: parsing and normalization from small fixture files, plus archive safety.

The builder functions at the top are reused by the refresh tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from app.data.providers import (
    SourceFormatError,
    ontime,
    open_zip_member,
    read_zip_member,
    t100,
    t100_summary,
    taf,
)

FIXTURES = Path(__file__).parent / "fixtures" / "data"


# -- fixture builders (shared with test_refresh.py) ------------------------------------------------


def write_zip(path: Path, members: dict[str, bytes]) -> Path:
    """Deterministic archive (fixed timestamps), so equal content gives equal bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in members.items():
            archive.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return path


def _xlsx(frame: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    frame.to_excel(buffer, index=False, engine="openpyxl")
    return buffer.getvalue()


def taf_members(
    base_year: int = 2024, last_year: int = 2030, scale: float = 1.0, airports: dict[str, float] | None = None
) -> dict[str, bytes]:
    """TAF-like workbooks with the source's padding: LOCIDs right-padded, names space-aligned."""
    airports = airports or {"LAX": 30_000_000.0, "SNA": 5_000_000.0, "BED": 9_000.0}
    meta = {
        "LAX": ("Los Angeles International", "LOS ANGELES", "CA", "AWP", 3),
        "SNA": ("John Wayne/Orange County", "SANTA ANA", "CA", "AWP", 2),
        "BED": ("Laurence G Hanscom Field", "BEDFORD", "MA", "ANE", 0),
    }
    airport_rows = [
        {
            "LOCID": locid,
            "REGION": meta.get(locid, ("X", "X", "CA", "AWP", 0))[3],
            "APORT_NAME": "  " + meta.get(locid, (locid, "X", "CA", "AWP", 0))[0] + " " * 30,
            "CITY": meta.get(locid, ("X", "CITY", "CA", "AWP", 0))[1] + "   ",
            "STATE": meta.get(locid, ("X", "X", "CA", "AWP", 0))[2],
            "HUB_SIZE": meta.get(locid, ("X", "X", "CA", "AWP", 0))[4],
        }
        for locid in airports
    ]
    enplanements, operations = [], []
    for locid, base in airports.items():
        for year in range(base_year - 2, last_year + 1):
            factor = (1.04 ** (year - base_year)) * scale
            total = base * factor
            enplanements.append(
                {
                    "locid": f"{locid} ",
                    "scenario": 0 if year <= base_year else 1,
                    "ayear": year,
                    "aac": round(total * 0.6),
                    "aat": round(total * 0.05),
                    "commuter": round(total * 0.15),
                    "us_flag": round(total * 0.1),
                    "frgn_flag": round(total * 0.1),
                }
            )
            operations.append(
                {
                    "locid": f"{locid} ",
                    "scenario": 0 if year <= base_year else 1,
                    "ayear": year,
                    "itn_Ac": round(total / 100),
                    "itn_at": round(total / 1000),
                    "itn_ga": 10,
                    "itn_mil": 1,
                    "loc_ga": 0,
                    "loc_mil": 0,
                    "tot_overs": 0,
                }
            )
    return {
        taf.AIRPORTS_MEMBER: _xlsx(pd.DataFrame(airport_rows)),
        taf.ENPLANEMENTS_MEMBER: _xlsx(pd.DataFrame(enplanements)),
        taf.OPERATIONS_MEMBER: _xlsx(pd.DataFrame(operations)),
        "BasedAircraft.xlsx": b"not read",
    }


T100_HEADER = (
    "DEPARTURES_SCHEDULED,DEPARTURES_PERFORMED,SEATS,PASSENGERS,FREIGHT,DISTANCE,UNIQUE_CARRIER,"
    "ORIGIN_AIRPORT_ID,ORIGIN,ORIGIN_STATE_ABR,ORIGIN_COUNTRY,DEST_AIRPORT_ID,DEST,DEST_CITY_NAME,"
    "DEST_STATE_ABR,DEST_COUNTRY,AIRCRAFT_TYPE,AIRCRAFT_CONFIG,YEAR,MONTH,DISTANCE_GROUP,CLASS,DATA_SOURCE,"
)


def t100_year_csv(year: int, departures: int = 30) -> bytes:
    """Twelve months of a few T-100 rows (ANC and SNA departures plus a foreign origin)."""
    lines = [T100_HEADER]
    for month in range(1, 13):
        lines.append(
            f'{departures},{departures},5400,4500,1000,1448,AS,10299,ANC,AK,US,14747,SEA,"Seattle, WA",WA,US,'
            f"614,1,{year},{month},3,F,DU,"
        )
        lines.append(
            f'4,4,0,0,300000,3830,KZ,10299,ANC,AK,US,13930,ORD,"Chicago, IL",IL,US,627,2,'
            f"{year},{month},8,G,IU,"
        )
        lines.append(
            f'5,5,900,800,0,1300,AC,16217,YVR,,CA,10299,ANC,"Anchorage, AK",AK,US,612,1,'
            f"{year},{month},3,F,IF,"
        )
        lines.append(
            f'10,10,1500,1200,0,35,WN,14908,SNA,CA,US,12892,LAX,"Los Angeles, CA",CA,US,612,1,'
            f"{year},{month},1,F,DU,"
        )
    return ("\n".join(lines) + "\n").encode("utf-8")


ONTIME_HEADER = (
    "Year,Month,FlightDate,Reporting_Airline,OriginAirportID,Origin,DestAirportID,Dest,DepDel15,TaxiOut,"
    "TaxiIn,Cancelled,Diverted,CarrierDelay,WeatherDelay,NASDelay,SecurityDelay,LateAircraftDelay,"
)


def ontime_month_csv(year: int, month: int, taxi_out: float = 12.0) -> bytes:
    rows = [
        f"{year},{month},{year}-{month:02d}-01,AA,12892,LAX,14908,SNA,0.00,{taxi_out},5.00,0.00,0.00,,,,,,",
        f"{year},{month},{year}-{month:02d}-01,AA,12892,LAX,14908,SNA,1.00,20.00,6.00,0.00,0.00,10.00,0.00,5.00,0.00,15.00,",
        f"{year},{month},{year}-{month:02d}-02,WN,14908,SNA,12892,LAX,,,,1.00,0.00,,,,,,",
    ]
    return ("\n".join([ONTIME_HEADER, *rows]) + "\n").encode("utf-8")


def summary_rows(year: int, departures: int = 30) -> list[dict[str, Any]]:
    """Summary API records consistent with `t100_year_csv` (sums per origin and month)."""
    records = []
    for month in range(1, 13):
        records.append(
            {
                "origin_airport_id": "10299",
                "origin_airport_code": "ANC",
                "reporting_month": f"{year}-{month:02d}-01T00:00:00.000",
                "total_departures": str(departures + 4),
                "total_passengers": "4500",
                "total_seats": "5400",
            }
        )
        records.append(
            {
                "origin_airport_id": "14908",
                "origin_airport_code": "SNA",
                "reporting_month": f"{year}-{month:02d}-01T00:00:00.000",
                "total_departures": "10",
                "total_passengers": "1200",
                "total_seats": "1500",
            }
        )
    return records


# -- TAF -------------------------------------------------------------------------------------------


def test_taf_parse_cleans_padding_and_computes_glossary_totals(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "taf.zip", taf_members())
    parsed = taf.parse_taf_zip(path, 2025)
    tables = {table.table: table.frame for table in parsed.tables}
    airports, annual = tables["taf_airports"], tables["taf_annual"]
    assert parsed.dataset_version == "TAF 2025 edition"
    assert set(airports["faa_locid"]) == {"LAX", "SNA", "BED"}
    lax = airports.set_index("faa_locid").loc["LAX"]
    assert lax["name"] == "Los Angeles International" and lax["city"] == "LOS ANGELES"
    assert int(lax["hub_size"]) == 3

    row = annual[(annual["faa_locid"] == "LAX") & (annual["fiscal_year"] == 2024)].iloc[0]
    components = [
        "enpl_air_carrier",
        "enpl_air_taxi",
        "enpl_commuter",
        "enpl_intl_us_flag",
        "enpl_intl_foreign_flag",
    ]
    assert row["total_enplanements"] == sum(row[c] for c in components) == 30_000_000
    assert row["commercial_operations"] == row["ops_itinerant_air_carrier"] + row["ops_itinerant_air_taxi"]
    assert row["is_forecast"] == 0
    assert annual.loc[annual["fiscal_year"] == 2025, "is_forecast"].eq(1).all()
    assert "base (last historical) year FY2024" in parsed.notes[0]


def test_taf_missing_component_keeps_total_missing(tmp_path: Path) -> None:
    members = taf_members()
    frame = pd.read_excel(io.BytesIO(members[taf.ENPLANEMENTS_MEMBER]))
    frame["commuter"] = frame["commuter"].astype("float64")
    frame.loc[0, "commuter"] = None
    members[taf.ENPLANEMENTS_MEMBER] = _xlsx(frame)
    parsed = taf.parse_taf_zip(write_zip(tmp_path / "taf.zip", members), 2025)
    annual = next(t.frame for t in parsed.tables if t.table == "taf_annual")
    first = frame.iloc[0]
    row = annual[(annual["faa_locid"] == first["locid"].strip()) & (annual["fiscal_year"] == first["ayear"])]
    assert row["total_enplanements"].isna().all()
    assert annual["total_enplanements"].notna().sum() == len(annual) - 1


def test_taf_rejects_missing_workbook_or_columns(tmp_path: Path) -> None:
    members = taf_members()
    del members[taf.OPERATIONS_MEMBER]
    with pytest.raises(SourceFormatError, match=r"AirportsOperations\.xlsx"):
        taf.parse_taf_zip(write_zip(tmp_path / "a.zip", members), 2025)
    members = taf_members()
    members[taf.ENPLANEMENTS_MEMBER] = _xlsx(pd.DataFrame({"locid": ["LAX"], "ayear": [2024]}))
    with pytest.raises(SourceFormatError, match="missing the expected columns"):
        taf.parse_taf_zip(write_zip(tmp_path / "b.zip", members), 2025)


# -- T-100 Segment ---------------------------------------------------------------------------------


def test_t100_parse_keeps_rows_as_reported(tmp_path: Path) -> None:
    csv_bytes = (FIXTURES / "t100_segment_2025.csv").read_bytes()
    parsed = t100.parse_t100_zip(write_zip(tmp_path / "t.zip", {t100.CSV_MEMBER: csv_bytes}), 2025)
    (table,) = parsed.tables
    frame = table.frame
    assert table.table == "t100_segments"
    assert len(frame) == 8
    assert list(frame.columns) == list(t100.COLUMNS.values())
    seattle = frame[(frame["dest"] == "SEA") & (frame["departures_performed"] == 30)].iloc[0]
    assert pd.isna(seattle["freight"])  # empty field stays missing, never 0
    assert frame.loc[frame["dest"] == "FAI", "distance"].isna().all()
    local = frame[(frame["origin"] == "ANC") & (frame["dest"] == "ANC")].iloc[0]
    assert local["distance"] == 0.0  # non-positive distance stored as reported
    singapore = frame[frame["dest"] == "SIN"].iloc[0]
    assert pd.isna(singapore["dest_state"]) and singapore["dest_country"] == "SG"
    assert int(singapore["origin_airport_id"]) == 10299 and int(singapore["distance_group"]) == 14
    assert any("2 rows share their identifying characteristics" in note for note in parsed.notes)
    assert any("distance of zero or less" in note for note in parsed.notes)


def test_t100_rejects_rows_outside_the_year_and_missing_fields(tmp_path: Path) -> None:
    csv_bytes = (FIXTURES / "t100_segment_2025.csv").read_bytes()
    with pytest.raises(SourceFormatError, match="outside calendar year 2024"):
        t100.parse_t100_zip(write_zip(tmp_path / "t.zip", {t100.CSV_MEMBER: csv_bytes}), 2024)
    trimmed = b"YEAR,MONTH,ORIGIN\n2025,1,ANC\n"
    with pytest.raises(SourceFormatError, match="missing the expected columns"):
        t100.parse_t100_zip(write_zip(tmp_path / "u.zip", {t100.CSV_MEMBER: trimmed}), 2025)


# -- On-Time Performance ---------------------------------------------------------------------------


def test_ontime_aggregates_match_hand_counted_fixture(tmp_path: Path) -> None:
    member = ontime.member_name(2025, 1)
    path = write_zip(
        tmp_path / "otp.zip",
        {member: (FIXTURES / "ontime_2025_01.csv").read_bytes(), "readme.html": b"<html></html>"},
    )
    parsed = ontime.parse_ontime_zip(path, 2025, 1)
    tables = {table.table: table.frame for table in parsed.tables}

    departures = tables["otp_departures_month"].set_index("airport_id")
    assert departures.loc[
        12892, ["scheduled", "cancelled", "diverted", "del15_valid", "del15_count"]
    ].tolist() == [
        4,
        1,
        1,
        3,
        2,
    ]
    assert departures.loc[
        14908, ["scheduled", "cancelled", "diverted", "del15_valid", "del15_count"]
    ].tolist() == [
        3,
        1,
        0,
        1,
        0,
    ]
    assert departures.loc[
        10299, ["scheduled", "cancelled", "diverted", "del15_valid", "del15_count"]
    ].tolist() == [
        2,
        0,
        0,
        2,
        1,
    ]
    assert departures.loc[12892, "code"] == "LAX" and (departures["days_in_month"] == 31).all()

    arrivals = tables["otp_arrivals_month"].set_index("airport_id")
    assert arrivals.loc[14908, "arrivals"] == 3
    assert arrivals.loc[
        14908, ["delay_carrier", "delay_weather", "delay_nas", "delay_security", "delay_late_aircraft"]
    ].tolist() == [10.0, 0.0, 5.0, 0.0, 15.0]
    assert arrivals.loc[12892, "arrivals"] == 3  # the cancelled SNA flight did not arrive
    assert arrivals.loc[12892, "delay_weather"] == 30.0
    assert arrivals.loc[10299, "arrivals"] == 0  # cancelled + diverted
    assert arrivals.loc[10299, ["delay_carrier", "delay_nas"]].isna().all()  # nothing reported stays missing

    taxi = tables["otp_taxi_hist"]

    def histogram(kind: str, airport_id: int) -> dict[int, int]:
        rows = taxi[(taxi["kind"] == kind) & (taxi["airport_id"] == airport_id)]
        return dict(zip(rows["minutes"].tolist(), rows["flights"].tolist(), strict=True))

    assert histogram("taxi_out", 12892) == {12: 1, 15: 1, 21: 1}  # 20.5 rounds half up; diverted departed
    assert histogram("taxi_out", 14908) == {10: 1, 11: 1}  # cancelled flight's TaxiOut ignored
    assert histogram("taxi_out", 10299) == {12: 1, 13: 1}
    assert histogram("taxi_in", 14908) == {4: 1, 5: 1, 6: 1}
    assert histogram("taxi_in", 12892) == {8: 1, 10: 1}  # negative TaxiIn excluded
    assert histogram("taxi_in", 10299) == {}  # diverted flight did not arrive
    assert any("1 negative TaxiIn" in note for note in parsed.notes)
    assert any("1 non-cancelled departures have no DepDel15" in note for note in parsed.notes)


def test_ontime_rejects_wrong_month_and_missing_columns(tmp_path: Path) -> None:
    member = ontime.member_name(2025, 2)
    path = write_zip(tmp_path / "otp.zip", {member: (FIXTURES / "ontime_2025_01.csv").read_bytes()})
    with pytest.raises(SourceFormatError, match="outside 2025-02"):
        ontime.parse_ontime_zip(path, 2025, 2)
    bad = write_zip(tmp_path / "bad.zip", {member: b"Year,Month,Origin\n2025,2,LAX\n"})
    with pytest.raises(SourceFormatError, match="DepDel15"):
        ontime.parse_ontime_zip(bad, 2025, 2)
    assert ontime.month_url(2025, 1).endswith("_2025_1.zip")  # BTS does not zero-pad months


# -- T-100 summary API -----------------------------------------------------------------------------


class FakeJsonClient:
    def __init__(self, pages: list[Any]) -> None:
        self.pages = pages
        self.params: list[dict[str, str]] = []

    def get_json(self, url: str, *, params: dict[str, str] | None = None) -> Any:
        self.params.append(dict(params or {}))
        return self.pages.pop(0)


def test_summary_normalization_fingerprint_and_round_trip(tmp_path: Path) -> None:
    rows = json.loads((FIXTURES / "t100_summary_2025_page.json").read_text(encoding="utf-8"))
    frame = t100_summary.normalize_rows(rows, 2025)
    assert frame["airport_id"].tolist() == [10299, 10299, 12892, 14908]
    assert pd.isna(frame.loc[frame["code"] == "SNA", "seats"]).all()  # missing total stays missing
    content = t100_summary.canonical_content(frame, 2025)
    shuffled = t100_summary.canonical_content(
        frame.sample(frac=1, random_state=1).sort_values(["airport_id", "month"]), 2025
    )
    assert content == shuffled

    client = FakeJsonClient([rows])
    summary = t100_summary.fetch_year(client, 2025)  # type: ignore[arg-type]
    assert summary.sha256 == hashlib.sha256(content).hexdigest()
    assert "reporting_month between '2025-01-01" in client.params[0]["$where"]
    path = t100_summary.write_year_file(summary, tmp_path / "out", tmp_path / "tmp")
    assert path.name == f"2025-{summary.sha256[:12]}.json"
    parsed = t100_summary.parse_summary_file(path, 2025)
    (table,) = parsed.tables
    assert table.table == "t100_summary_month" and len(table.frame) == 4
    assert table.frame["departures"].sum() == 7012 + 6512 + 16012 + 3901


def test_summary_pagination_and_latest_complete_year() -> None:
    page = [
        {
            "origin_airport_id": str(10000 + i),
            "origin_airport_code": "X",
            "reporting_month": "2025-03-01T00:00:00.000",
            "total_departures": "1",
            "total_passengers": "1",
            "total_seats": "1",
        }
        for i in range(t100_summary.PAGE_SIZE)
    ]
    client = FakeJsonClient([page, page[:3]])
    with pytest.raises(SourceFormatError):  # second page repeats ids: duplicates are rejected
        t100_summary.fetch_year(client, 2025)  # type: ignore[arg-type]
    assert [p["$offset"] for p in client.params] == ["0", str(t100_summary.PAGE_SIZE)]

    assert (
        t100_summary.latest_complete_year(
            FakeJsonClient([[{"latest": "2026-04-01T00:00:00.000"}]])  # type: ignore[arg-type]
        )
        == 2025
    )
    assert (
        t100_summary.latest_complete_year(
            FakeJsonClient([[{"latest": "2025-12-01T00:00:00.000"}]])  # type: ignore[arg-type]
        )
        == 2025
    )


# -- archive safety --------------------------------------------------------------------------------


def test_zip_member_must_be_expected_unique_and_within_size(tmp_path: Path) -> None:
    traversal = write_zip(tmp_path / "evil.zip", {"../../evil.csv": b"x", "readme.html": b"y"})
    with pytest.raises(SourceFormatError, match="expected file"):
        read_zip_member(traversal, t100.CSV_MEMBER, 1000)
    assert not (tmp_path.parent / "evil.csv").exists()

    big = write_zip(tmp_path / "big.zip", {t100.CSV_MEMBER: b"0" * 5000})
    with pytest.raises(SourceFormatError, match="limit"):
        read_zip_member(big, t100.CSV_MEMBER, 1000)

    duplicate = tmp_path / "dup.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr(t100.CSV_MEMBER, b"a")
        with pytest.warns(UserWarning):
            archive.writestr(t100.CSV_MEMBER, b"b")
    with pytest.raises(SourceFormatError, match="more than once"):
        read_zip_member(duplicate, t100.CSV_MEMBER, 1000)

    not_zip = tmp_path / "page.zip"
    not_zip.write_bytes(b"<html>error</html>")
    with pytest.raises(SourceFormatError, match="not a valid ZIP"):
        read_zip_member(not_zip, t100.CSV_MEMBER, 1000)


def test_zip_stream_stops_when_actual_size_exceeds_declared_limit(tmp_path: Path) -> None:
    path = write_zip(tmp_path / "ok.zip", {"a.csv": b"0123456789" * 100})
    with open_zip_member(path, "a.csv", 2000) as stream:
        assert len(stream.read()) == 1000
    # Forge a smaller declared size in the local and central headers: the reader must not trust it.
    data = bytearray(path.read_bytes())
    info = zipfile.ZipFile(path).getinfo("a.csv")
    forged = bytearray(data)
    size_bytes = info.file_size.to_bytes(4, "little")
    small = (10).to_bytes(4, "little")
    forged = bytearray(bytes(forged).replace(size_bytes, small))
    bad = tmp_path / "forged.zip"
    bad.write_bytes(bytes(forged))
    with pytest.raises((SourceFormatError, zipfile.BadZipFile)), open_zip_member(bad, "a.csv", 20) as stream:
        stream.read()
