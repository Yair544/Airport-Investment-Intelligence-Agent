"""Version-controlled airport reference data and deterministic name resolution (spec section 3).

`load_reference_catalog()` reads `reference/airports.csv`, `aliases.csv`, `regions.json`,
`constraints.csv` and `evidence.json` (the last two may be absent or empty), validates every row
with the shared pydantic models and builds exact-match indexes.

Resolution order: exact IATA/FAA code -> exact curated alias (English or Hebrew) -> exact official
name -> exact city -> US state (full name, Hebrew names for common states) -> region (US Census
regions and divisions, e.g. New England). Matching is exact after normalization (case, spacing,
punctuation, quote/geresh variants and generic words such as "airport" or "נמל התעופה"), never by
similarity. Two-letter state codes are not state aliases, so "LA" is the curated alias of LAX.
Several matches are ambiguous unless exactly one candidate is already in the conversation context.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.analysis.models import (
    AirportRef,
    ConstraintRecord,
    EvidenceRecord,
    ResolutionOutcome,
    ResolvedAirport,
    ResolvedRegion,
)

logger = logging.getLogger("app.data.resolution")

REFERENCE_DIR = Path(__file__).resolve().with_name("reference")
AIRPORTS_FILE = "airports.csv"
ALIASES_FILE = "aliases.csv"
REGIONS_FILE = "regions.json"
CONSTRAINTS_FILE = "constraints.csv"
EVIDENCE_FILE = "evidence.json"
REFERENCE_FILES = (AIRPORTS_FILE, ALIASES_FILE, REGIONS_FILE, CONSTRAINTS_FILE, EVIDENCE_FILE)
AIRPORT_COLUMNS = (
    "airport_id",
    "faa_locid",
    "iata_code",
    "bts_airport_id",
    "name",
    "city",
    "state",
    "hub_size",
    "mapping_status",
)
HUB_RANK = {"large": 0, "medium": 1, "small": 2, "nonhub": 3}

# Generic words dropped before matching (both from queries and from indexed names and aliases).
GENERIC_TOKENS = frozenset(
    {
        "airport",
        "airports",
        "international",
        "intl",
        "airfield",
        "נמל",
        "שדה",
        "התעופה",
        "תעופה",
        "הבינלאומי",
        "בינלאומי",
    }
)
_REMOVED_MARKS = ".'’‘`´׳\"״“”"  # removed inside words: "L.A." == "LA", "O'Hare" == "OHare"
_NON_WORD = re.compile(r"[^\w\s]")
_CODE = re.compile(r"[a-z0-9]{3,4}")
_HEBREW_PREFIXES = "בלמהו"


class ReferenceDataError(Exception):
    """A reference file is malformed or inconsistent."""


def normalize(text: str) -> str:
    """Canonical form for exact matching: casefolded words without punctuation or generic words."""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = unicodedata.normalize("NFKC", stripped).casefold()
    for mark in _REMOVED_MARKS:
        folded = folded.replace(mark, "")
    words = _NON_WORD.sub(" ", folded).replace("_", " ").split()
    words = [word for word in words if word not in GENERIC_TOKENS]
    if words and words[0] == "של":  # "נמל התעופה של בוסטון" -> "בוסטון"
        words = words[1:]
    return " ".join(words)


def _strip_hebrew_prefix(key: str) -> str | None:
    """`בבוסטון` -> `בוסטון`: one attached Hebrew preposition/article, tried only after no match."""
    if len(key) > 2 and key[0] in _HEBREW_PREFIXES and 0x0590 <= ord(key[1]) <= 0x05FF:
        return key[1:]
    return None


@dataclass(frozen=True)
class Region:
    region_id: str
    name: str
    states: tuple[str, ...]


def _read_text(path: Path) -> str | None:
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8-sig")


def _csv_rows(text: str | None, required: Sequence[str], file_name: str) -> list[dict[str, str]]:
    if text is None or not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    missing = [column for column in required if column not in (reader.fieldnames or [])]
    if missing:
        raise ReferenceDataError(f"{file_name} is missing columns: {', '.join(missing)}")
    return [
        {key: (value or "").strip() for key, value in row.items() if key is not None}
        for row in reader
        if any((value or "").strip() for value in row.values() if isinstance(value, str))
    ]


def _validation_message(file_name: str, line: int, exc: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()
    )
    return f"{file_name} record {line}: {problems}"


class FileReferenceCatalog:
    """ReferenceCatalog backed by the version-controlled files in `app/data/reference/`."""

    def __init__(self, reference_dir: Path = REFERENCE_DIR) -> None:
        self.reference_dir = reference_dir
        self.warnings: list[str] = []  # skipped rows, e.g. constraints for airports outside the list
        texts = {name: _read_text(reference_dir / name) for name in REFERENCE_FILES}
        self._reference_version = self._signature(texts)
        airports = self._load_airports(texts[AIRPORTS_FILE])
        aliases = self._load_aliases(texts[ALIASES_FILE], airports)
        self._airports = {
            airport.airport_id: airport.model_copy(update={"aliases": aliases.get(airport.airport_id, [])})
            for airport in airports.values()
        }
        self._states, self._state_index, self._regions, self._region_index, city_aliases = self._load_regions(
            texts[REGIONS_FILE]
        )
        self._constraints = self._load_constraints(texts[CONSTRAINTS_FILE])
        self._evidence = self._load_evidence(texts[EVIDENCE_FILE])

        self._codes: dict[str, set[str]] = {}
        self._alias_index: dict[str, set[str]] = {}
        self._names: dict[str, set[str]] = {}
        self._cities: dict[str, set[str]] = {}
        for airport in self._airports.values():
            for code in {airport.faa_locid, airport.iata_code}:
                if code:
                    self._codes.setdefault(code.upper(), set()).add(airport.airport_id)
            for alias in airport.aliases:
                if key := normalize(alias):
                    self._alias_index.setdefault(key, set()).add(airport.airport_id)
            if key := normalize(airport.name):
                self._names.setdefault(key, set()).add(airport.airport_id)
            if key := normalize(airport.city):
                self._cities.setdefault(key, set()).add(airport.airport_id)
        self._city_aliases = {normalize(alias): normalize(city) for alias, city in city_aliases.items()}

    # -- loading -----------------------------------------------------------------------------------

    @staticmethod
    def _signature(texts: dict[str, str | None]) -> str:
        digest = hashlib.sha256()
        for name in REFERENCE_FILES:
            digest.update(name.encode("utf-8") + b"\0")
            text = texts[name]
            digest.update(b"<missing>" if text is None else text.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()[:16]

    @staticmethod
    def _load_airports(text: str | None) -> dict[str, AirportRef]:
        rows = _csv_rows(text, AIRPORT_COLUMNS, AIRPORTS_FILE)
        if not rows:
            raise ReferenceDataError(f"{AIRPORTS_FILE} is missing or empty.")
        airports: dict[str, AirportRef] = {}
        locids: set[str] = set()
        for line, row in enumerate(rows, start=2):
            payload: dict[str, Any] = {column: row.get(column) or None for column in AIRPORT_COLUMNS}
            try:
                airport = AirportRef.model_validate(payload)
            except ValidationError as exc:
                raise ReferenceDataError(_validation_message(AIRPORTS_FILE, line, exc)) from exc
            if airport.airport_id != f"apt_{airport.faa_locid.lower()}":
                raise ReferenceDataError(f"{AIRPORTS_FILE} line {line}: airport_id must be apt_<faa_locid>.")
            if airport.airport_id in airports or airport.faa_locid in locids:
                raise ReferenceDataError(
                    f"{AIRPORTS_FILE} line {line}: duplicate airport {airport.faa_locid}."
                )
            if airport.mapping_status != "faa_only" and (
                airport.iata_code is None or airport.bts_airport_id is None
            ):
                raise ReferenceDataError(
                    f"{AIRPORTS_FILE} line {line}: a mapped airport needs BTS identifiers."
                )
            airports[airport.airport_id] = airport
            locids.add(airport.faa_locid)
        return airports

    @staticmethod
    def _load_aliases(text: str | None, airports: dict[str, AirportRef]) -> dict[str, list[str]]:
        aliases: dict[str, list[str]] = {}
        for line, row in enumerate(
            _csv_rows(text, ("airport_id", "alias", "language"), ALIASES_FILE), start=2
        ):
            airport_id, alias = row["airport_id"], row["alias"]
            if airport_id not in airports:
                raise ReferenceDataError(f"{ALIASES_FILE} line {line}: unknown airport id {airport_id}.")
            if not normalize(alias):
                raise ReferenceDataError(f"{ALIASES_FILE} line {line}: the alias has no matchable words.")
            aliases.setdefault(airport_id, []).append(alias)
        return aliases

    @staticmethod
    def _load_regions(
        text: str | None,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, Region], dict[str, str], dict[str, str]]:
        if text is None:
            raise ReferenceDataError(f"{REGIONS_FILE} is missing.")
        try:
            payload = json.loads(text)
            states: dict[str, str] = {}
            state_index: dict[str, str] = {}
            for item in payload["states"]:
                code = str(item["code"]).upper()
                if not re.fullmatch(r"[A-Z]{2}", code):
                    raise ReferenceDataError(f"{REGIONS_FILE}: invalid state code {code}.")
                states[code] = str(item["name"])
                for name in [item["name"], *item.get("aliases", [])]:
                    key = normalize(str(name))
                    if len(key) <= 2:
                        raise ReferenceDataError(f"{REGIONS_FILE}: state names must not be two-letter codes.")
                    state_index[key] = code
            regions: dict[str, Region] = {}
            region_index: dict[str, str] = {}
            for item in payload["regions"]:
                region = Region(
                    str(item["region_id"]), str(item["name"]), tuple(str(s) for s in item["states"])
                )
                unknown = [state for state in region.states if state not in states]
                if unknown:
                    raise ReferenceDataError(
                        f"{REGIONS_FILE}: region {region.name} has unknown states {unknown}."
                    )
                regions[region.region_id] = region
                for name in [item["name"], *item.get("aliases", [])]:
                    region_index[normalize(str(name))] = region.region_id
            city_aliases = {str(k): str(v) for k, v in payload.get("city_aliases", {}).items()}
        except (KeyError, TypeError, ValueError) as exc:
            raise ReferenceDataError(f"{REGIONS_FILE} is malformed: {exc}") from exc
        return states, state_index, regions, region_index, city_aliases

    def _load_constraints(self, text: str | None) -> dict[str, list[ConstraintRecord]]:
        result: dict[str, list[ConstraintRecord]] = {}
        columns = tuple(ConstraintRecord.model_fields)
        for line, row in enumerate(_csv_rows(text, columns, CONSTRAINTS_FILE), start=2):
            try:
                record = ConstraintRecord.model_validate({column: row.get(column) for column in columns})
            except ValidationError as exc:
                raise ReferenceDataError(_validation_message(CONSTRAINTS_FILE, line, exc)) from exc
            if self._known_airport(record.airport_id, CONSTRAINTS_FILE, line):
                result.setdefault(record.airport_id, []).append(record)
        return result

    def _load_evidence(self, text: str | None) -> dict[str, list[EvidenceRecord]]:
        if text is None or not text.strip():
            return {}
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise ReferenceDataError(f"{EVIDENCE_FILE} is not valid JSON: {exc}") from exc
        items = payload.get("records", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ReferenceDataError(f"{EVIDENCE_FILE} must contain a list of records.")
        result: dict[str, list[EvidenceRecord]] = {}
        seen: set[str] = set()
        for index, item in enumerate(items, start=1):
            try:
                record = EvidenceRecord.model_validate(item)
            except ValidationError as exc:
                raise ReferenceDataError(_validation_message(EVIDENCE_FILE, index, exc)) from exc
            if record.evidence_id in seen:
                raise ReferenceDataError(
                    f"{EVIDENCE_FILE} record {index}: duplicate evidence_id {record.evidence_id}."
                )
            seen.add(record.evidence_id)
            if self._known_airport(record.airport_id, EVIDENCE_FILE, index):
                result.setdefault(record.airport_id, []).append(record)
        return result

    def _known_airport(self, airport_id: str, file_name: str, line: int) -> bool:
        """Rows for airports outside the reference list cannot affect any analysis: skip and warn."""
        if airport_id in self._airports:
            return True
        message = f"{file_name} record {line}: airport {airport_id} is not in {AIRPORTS_FILE}; row skipped."
        self.warnings.append(message)
        logger.warning("reference_row_skipped", extra={"detail": message})
        return False

    # -- ReferenceCatalog ----------------------------------------------------------------------------

    @property
    def reference_version(self) -> str:
        return self._reference_version

    def airport(self, airport_id: str) -> AirportRef | None:
        return self._airports.get(airport_id)

    def airports(self) -> list[AirportRef]:
        return sorted(self._airports.values(), key=lambda airport: airport.airport_id)

    def airports_in_states(self, states: Iterable[str]) -> list[AirportRef]:
        wanted = set(states)
        return self._sorted(a.airport_id for a in self._airports.values() if a.state in wanted)

    def region(self, region_id: str) -> ResolvedRegion | None:
        """A region or state by the id that `resolve` returns (e.g. "new_england", "state_ca")."""
        if region_id.startswith("state_"):
            code = region_id.removeprefix("state_").upper()
            if code in self._states:
                return ResolvedRegion(region_id=region_id, name=self._states[code], states=[code])
            return None
        item = self._regions.get(region_id)
        if item is None:
            return None
        return ResolvedRegion(region_id=item.region_id, name=item.name, states=list(item.states))

    def constraints_for(self, airport_id: str) -> list[ConstraintRecord]:
        return list(self._constraints.get(airport_id, []))

    def evidence_for(self, airport_id: str) -> list[EvidenceRecord]:
        return list(self._evidence.get(airport_id, []))

    def resolve(self, query: str, context_airport_ids: Sequence[str] = ()) -> ResolutionOutcome:
        text = query.strip()
        key = normalize(text)
        if not key:
            return ResolutionOutcome(
                query=query,
                status="not_found",
                message="The request did not name an airport, city, state or region.",
            )
        outcome = self._resolve_key(query, key, context_airport_ids)
        if outcome.status == "not_found" and (stripped := _strip_hebrew_prefix(key)):
            retry = self._resolve_key(query, stripped, context_airport_ids)
            if retry.status != "not_found":
                return retry
        return outcome

    # -- resolution steps ------------------------------------------------------------------------------

    def _resolve_key(self, query: str, key: str, context: Sequence[str]) -> ResolutionOutcome:
        if _CODE.fullmatch(key) and (ids := self._codes.get(key.upper())):
            return self._airport_outcome(query, ids, "code", context)
        if ids := self._alias_index.get(key):
            return self._airport_outcome(query, ids, "alias", context)
        names = self._names.get(key, set())
        # A name that reduces to a city shared by other airports ("Portland International") is
        # resolved by the city rule, so it stays ambiguous instead of silently picking one airport.
        if names and not names < self._cities.get(key, set()):
            return self._airport_outcome(query, names, "name", context)
        city_key = self._city_aliases.get(key, key)
        if ids := self._cities.get(city_key):
            return self._airport_outcome(query, ids, "city", context)
        if state := self._state_index.get(key):
            region = ResolvedRegion(
                region_id=f"state_{state.lower()}", name=self._states[state], states=[state]
            )
            return self._area_outcome(query, "state", region)
        if region_id := self._region_index.get(key):
            item = self._regions[region_id]
            region = ResolvedRegion(region_id=item.region_id, name=item.name, states=list(item.states))
            return self._area_outcome(query, "region", region)
        return ResolutionOutcome(
            query=query,
            status="not_found",
            message=(
                f"No airport, city, US state or region matches '{query.strip()}'. Use an airport code "
                "(for example LAX), an airport or city name, a full state name or a region such as "
                "New England."
            ),
        )

    def _sorted(self, airport_ids: Iterable[str]) -> list[AirportRef]:
        airports = [self._airports[airport_id] for airport_id in set(airport_ids)]
        return sorted(airports, key=lambda a: (HUB_RANK.get(a.hub_size or "", 9), a.code, a.airport_id))

    @staticmethod
    def _resolved(airport: AirportRef, matched_on: str) -> ResolvedAirport:
        return ResolvedAirport(
            airport_id=airport.airport_id,
            code=airport.code,
            faa_locid=airport.faa_locid,
            name=airport.name,
            city=airport.city,
            state=airport.state,
            matched_on=matched_on,
        )

    def _airport_outcome(
        self, query: str, airport_ids: set[str], matched_on: str, context: Sequence[str]
    ) -> ResolutionOutcome:
        candidates = self._sorted(airport_ids)
        if len(candidates) == 1:
            return ResolutionOutcome(
                query=query,
                status="matched",
                kind="airport",
                airports=[self._resolved(candidates[0], matched_on)],
            )
        context_ids = set(context)
        in_context = [airport for airport in candidates if airport.airport_id in context_ids]
        if len(in_context) == 1:
            return ResolutionOutcome(
                query=query,
                status="matched",
                kind="airport",
                airports=[self._resolved(in_context[0], "context")],
            )
        return ResolutionOutcome(
            query=query,
            status="ambiguous",
            kind="airport",
            candidates=[self._resolved(airport, matched_on) for airport in candidates],
            message=f"'{query.strip()}' matches {len(candidates)} airports. Ask the user which one is meant.",
        )

    def _area_outcome(self, query: str, kind: str, region: ResolvedRegion) -> ResolutionOutcome:
        airports = self.airports_in_states(region.states)
        return ResolutionOutcome(
            query=query,
            status="matched",
            kind="state" if kind == "state" else "region",
            airports=[self._resolved(airport, kind) for airport in airports],
            region=region,
            message=None if airports else f"The reference list has no airports in {region.name}.",
        )


@lru_cache(maxsize=4)
def _cached_catalog(
    reference_dir: Path, signature: tuple[tuple[str, float, int], ...]
) -> FileReferenceCatalog:
    return FileReferenceCatalog(reference_dir)


def load_reference_catalog(reference_dir: Path | None = None) -> FileReferenceCatalog:
    """Load (and cache until the files change) the reference catalog."""
    directory = (reference_dir or REFERENCE_DIR).resolve()
    signature = tuple(
        (name, path.stat().st_mtime, path.stat().st_size)
        if (path := directory / name).is_file()
        else (name, 0.0, -1)
        for name in REFERENCE_FILES
    )
    return _cached_catalog(directory, signature)
