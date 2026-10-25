"""Generate `app/data/reference/airports.csv` from ingested TAF, T-100 and On-Time data.

Usage (from the backend/ directory):
    python scripts/build_airport_reference.py [--version-id ID] [--min-enplanements 2500]

Rules (product spec section 3: a verified mapping, never similarity):
- An airport is included when its total enplanements in the TAF base year reach the threshold.
- Its BTS identifiers are `verified` when exactly one BTS airport id reports the FAA location
  identifier as its code in the same state as TAF.
- Every other included airport is listed in `airport_overrides.csv` for manual review. A reviewed
  row that carries `iata_code` and `bts_airport_id` becomes a `manual_override` (its note records
  the evidence); rows without ids stay `faa_only` and carry no BTS identifiers.

Data comes from the active data version, the version given with --version-id, or (when no version
is active yet) the latest ingested file of each source and period.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import get_settings  # noqa: E402
from app.data.resolution import AIRPORT_COLUMNS, REFERENCE_DIR, FileReferenceCatalog  # noqa: E402
from app.storage import Storage  # noqa: E402

OVERRIDE_COLUMNS = ("faa_locid", "iata_code", "bts_airport_id", "note")
HUB_SIZES = {3: "large", 2: "medium", 1: "small", 0: "nonhub"}
# Words kept upper case, and spellings TAF abbreviates or flattens.
ACRONYMS = {
    "AFB",
    "ARB",
    "ANGB",
    "AAF",
    "NAS",
    "NAF",
    "JRB",
    "LRRS",
    "RAF",
    "US",
    "USA",
    "II",
    "III",
    "IV",
    "JFK",
    "LBJ",
}
WORD_FIXES = {
    "Tf": "T.F.",
    "Laguardia": "LaGuardia",
    "Dekalb": "DeKalb",
    "Ntl": "National",
    "Intcntl": "Intercontinental",
    "Trml": "Terminal",
    "Exec": "Executive",
}
_SEPARATORS = re.compile(r"([\s/-]+)")
_MC_SPACED = re.compile(r"(?<![A-Za-z])Mc ([A-Za-z])")  # "MC ALLEN" -> "McAllen"
_MC_JOINED = re.compile(r"(?<![A-Za-z])Mc([a-z])")  # "MCGRATH" -> "McGrath"


@dataclass(frozen=True)
class BtsAirport:
    airport_id: int
    code: str
    state: str | None
    country: str | None
    city_name: str | None
    departures: float


def smart_title(text: str | None) -> str:
    """Readable title case for TAF text: all-caps words are title-cased, known acronyms kept."""
    if not text:
        return ""
    clean = " ".join(text.split())
    all_caps = not any(ch.islower() for ch in clean)
    parts = _SEPARATORS.split(clean)
    result = []
    for part in parts:
        if not part or _SEPARATORS.fullmatch(part):
            result.append(part)
            continue
        word = part
        if all_caps:
            if word.upper() in ACRONYMS:
                word = word.upper()
            elif "'" in word:
                head, _, tail = word.partition("'")
                # O'HARE -> O'Hare, MARTHA'S -> Martha's
                word = head.capitalize() + "'" + (tail.capitalize() if len(head) == 1 else tail.lower())
            else:
                word = word.capitalize()
        result.append(WORD_FIXES.get(word, word))
    titled = _MC_SPACED.sub(lambda m: "Mc" + m.group(1).upper(), "".join(result))
    return _MC_JOINED.sub(lambda m: "Mc" + m.group(1).upper(), titled)


def _read_overrides(path: Path) -> dict[str, dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    overrides: dict[str, dict[str, str]] = {}
    for row in rows:
        locid = (row.get("faa_locid") or "").strip().upper()
        if locid:
            overrides[locid] = {column: (row.get(column) or "").strip() for column in OVERRIDE_COLUMNS}
            overrides[locid]["faa_locid"] = locid
    return overrides


def _select_files(storage: Storage, version_id: str | None) -> dict[str, list[dict[str, Any]]]:
    target = version_id or storage.get_active_data_version_id()
    if target is not None:
        if storage.get_data_version(target) is None:
            raise SystemExit(f"Data version {target} does not exist.")
        records = [file.as_dict() for file in storage.data_version_files(target)]
        print(f"Using data version {target}.")
    else:
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for record in storage.list_data_files(status="ready"):
            key = (record["source_id"], record["period_key"])
            if key not in latest or record["retrieved_at"] > latest[key]["retrieved_at"]:
                latest[key] = record
        records = list(latest.values())
        print("No active data version; using the latest ingested file of each source and period.")
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_source[record["source_id"]].append(record)
    for source in ("faa_taf", "bts_t100_segment"):
        if not by_source.get(source):
            raise SystemExit(f"No ingested {source} file was found. Run the data refresh first.")
    return by_source


def _bts_airports(storage: Storage, t100_file_ids: list[str]) -> list[BtsAirport]:
    merged: dict[tuple[int, str, str | None, str | None], BtsAirport] = {}
    for row in storage.bts_airport_codes(t100_file_ids):
        key = (int(row["airport_id"]), str(row["code"] or ""), row["state"], row["country"])
        previous = merged.get(key)
        merged[key] = BtsAirport(
            airport_id=key[0],
            code=key[1],
            state=row["state"],
            country=row["country"],
            city_name=row["city_name"] or (previous.city_name if previous else None),
            departures=float(row["departures"] or 0) + (previous.departures if previous else 0.0),
        )
    return list(merged.values())


def build(args: argparse.Namespace) -> int:
    settings = get_settings()
    storage = Storage(settings.database_path)
    storage.initialize()
    storage.ensure_aviation_schema()
    files = _select_files(storage, args.version_id)

    taf_file = max(files["faa_taf"], key=lambda record: record["period_key"])
    fiscal = storage.taf_fiscal_years(taf_file["file_id"])
    base_year = max(year for year, forecast, _ in fiscal if not forecast)
    totals = {
        row["faa_locid"]: row["total_enplanements"]
        for row in storage.query_taf_annual([taf_file["file_id"]], None)
        if row["fiscal_year"] == base_year
    }
    taf_airports = {row["faa_locid"]: row for row in storage.query_taf_airports([taf_file["file_id"]])}
    included = sorted(
        locid
        for locid, total in totals.items()
        if total is not None and total >= args.min_enplanements and locid in taf_airports
    )

    t100_ids = [record["file_id"] for record in files["bts_t100_segment"]]
    otp_ids = [record["file_id"] for record in files.get("bts_ontime", [])]
    periods = ", ".join(sorted(f"CY{record['period_key']}" for record in files["bts_t100_segment"]))
    bts = _bts_airports(storage, t100_ids)
    by_code: dict[str, list[BtsAirport]] = defaultdict(list)
    by_id: dict[int, list[BtsAirport]] = defaultdict(list)
    for airport in bts:
        by_code[airport.code].append(airport)
        by_id[airport.airport_id].append(airport)
    otp_codes: dict[int, set[str]] = defaultdict(set)
    for row in storage.ontime_airport_codes(otp_ids):
        otp_codes[int(row["airport_id"])].add(str(row["code"]))

    overrides_path = args.reference_dir / "airport_overrides.csv"
    overrides = _read_overrides(overrides_path)
    output: list[dict[str, Any]] = []
    review: dict[str, dict[str, str]] = {}
    counts: dict[str, int] = defaultdict(int)
    for locid in included:
        taf_row = taf_airports[locid]
        state = (taf_row["state"] or "").strip()
        record: dict[str, Any] = {
            "airport_id": f"apt_{locid.lower()}",
            "faa_locid": locid,
            "iata_code": "",
            "bts_airport_id": "",
            "name": smart_title(taf_row["name"]),
            "city": smart_title(taf_row["city"]),
            "state": state,
            "hub_size": HUB_SIZES.get(taf_row["hub_size"], ""),
            "mapping_status": "faa_only",
        }
        override = overrides.get(locid)
        candidates = by_code.get(locid, [])
        same_state = sorted({a.airport_id for a in candidates if a.state == state})
        if override and override["bts_airport_id"]:
            bts_id = int(override["bts_airport_id"])
            if bts_id not in by_id:
                raise SystemExit(
                    f"Override for {locid} names BTS airport {bts_id}, which is not in the data."
                )
            record.update(
                iata_code=override["iata_code"], bts_airport_id=bts_id, mapping_status="manual_override"
            )
            review[locid] = override
        elif len(same_state) == 1:
            record.update(iata_code=locid, bts_airport_id=same_state[0], mapping_status="verified")
            otp = otp_codes.get(same_state[0])
            if otp and otp != {locid}:
                print(
                    f"Note: On-Time reports BTS airport {same_state[0]} as {sorted(otp)}, T-100 as {locid}."
                )
        else:
            note = (
                override["note"]
                if override and override["note"]
                else _review_note(locid, state, taf_row["city"], candidates, bts, periods)
            )
            review[locid] = {"faa_locid": locid, "iata_code": "", "bts_airport_id": "", "note": note}
        counts[record["mapping_status"]] += 1
        output.append(record)

    for locid, override in overrides.items():  # keep manual decisions for airports outside the list
        if override["bts_airport_id"] and locid not in review:
            review[locid] = override

    if args.dry_run:
        print("Dry run: files not written.")
    else:
        _write_csv(args.reference_dir / "airports.csv", AIRPORT_COLUMNS, output)
        _write_csv(overrides_path, OVERRIDE_COLUMNS, [review[locid] for locid in sorted(review)])
        FileReferenceCatalog(args.reference_dir)  # validates the result together with aliases.csv
    primary = sum(1 for locid in included if (totals[locid] or 0) >= 10_000)
    print(
        f"TAF {taf_file['period_key']} base year FY{base_year}: {len(included)} airports with at least "
        f"{args.min_enplanements:,} enplanements ({primary} with at least 10,000)."
    )
    print("Mapping: " + ", ".join(f"{status} {count}" for status, count in sorted(counts.items())))
    unresolved = [locid for locid in sorted(review) if not review[locid]["bts_airport_id"]]
    print(f"Listed for manual review without BTS ids: {len(unresolved)}")
    return 0


def _review_note(
    locid: str,
    state: str,
    taf_city: str | None,
    candidates: list[BtsAirport],
    bts: list[BtsAirport],
    periods: str,
) -> str:
    if not candidates:
        note = f"No BTS airport reports code {locid} in T-100 {periods}."
    else:
        listed = "; ".join(
            f"{a.airport_id} in {a.state or a.country or '?'}"
            for a in sorted(candidates, key=lambda a: a.airport_id)
        )
        note = f"BTS code {locid} is reported by {listed} (TAF state {state}); review needed."
    city = (taf_city or "").strip().casefold()
    hints = sorted(
        {
            f"{a.airport_id} {a.code} ({a.city_name})"
            for a in bts
            if a.state == state
            and a.code != locid
            and a.city_name
            and a.city_name.split(",")[0].strip().casefold() == city
        }
    )
    if hints:
        note += " Same state and city in BTS (hint only, not a mapping): " + ", ".join(hints) + "."
    return note


def _write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {column: "" if row.get(column) is None else row.get(column) for column in columns}
            )
    tmp.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--version-id", help="Data version to read (default: the active version).")
    parser.add_argument("--min-enplanements", type=float, default=2500, help="TAF base-year threshold.")
    parser.add_argument("--reference-dir", type=Path, default=REFERENCE_DIR, help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help="Report without writing files.")
    return build(parser.parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
