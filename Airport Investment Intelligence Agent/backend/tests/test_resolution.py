"""Airport reference catalog: loading rules and deterministic resolution."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from app.data.resolution import (
    REFERENCE_DIR,
    FileReferenceCatalog,
    ReferenceDataError,
    load_reference_catalog,
    normalize,
)

AIRPORTS = """airport_id,faa_locid,iata_code,bts_airport_id,name,city,state,hub_size,mapping_status
apt_lax,LAX,LAX,12892,Los Angeles International,Los Angeles,CA,large,verified
apt_sna,SNA,SNA,14908,John Wayne/Orange County,Santa Ana,CA,medium,verified
apt_pdx,PDX,PDX,14057,Portland International,Portland,OR,medium,verified
apt_pwm,PWM,PWM,14321,Portland International Jetport,Portland,ME,small,verified
apt_bgr,BGR,BGR,10581,Bangor International,Bangor,ME,nonhub,verified
apt_bos,BOS,BOS,10721,General Edward Lawrence Logan International,Boston,MA,large,verified
apt_nyl,NYL,YUM,16133,Yuma MCAS/Yuma International,Yuma,AZ,nonhub,manual_override
apt_xyz,XYZ,,,Somewhere Municipal,Smalltown,LA,nonhub,faa_only
"""
ALIASES = """airport_id,alias,language
apt_lax,Los Angeles,en
apt_lax,LA,en
apt_lax,לוס אנג'לס,he
apt_sna,John Wayne,en
apt_sna,סנטה אנה,he
apt_bos,Logan,en
"""
CONSTRAINT_HEADER = "airport_id,constraint_type,description,source_title,source_url,checked_on\n"


def make_reference(directory: Path, **overrides: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    files = {"airports.csv": AIRPORTS, "aliases.csv": ALIASES, **overrides}
    for name, text in files.items():
        (directory / name).write_text(text, encoding="utf-8")
    shutil.copy(REFERENCE_DIR / "regions.json", directory / "regions.json")
    return directory


@pytest.fixture
def catalog(tmp_path: Path) -> FileReferenceCatalog:
    return FileReferenceCatalog(make_reference(tmp_path / "reference"))


def codes(items: list) -> list[str]:  # type: ignore[type-arg]
    return [item.code for item in items]


def test_normalize_handles_case_punctuation_quotes_and_generic_words() -> None:
    assert normalize("  Los   Angeles International AIRPORT ") == "los angeles"
    assert normalize("L.A.") == normalize("LA") == "la"
    assert normalize("O'Hare") == normalize("O’Hare") == "ohare"
    assert normalize("לוס אנג׳לס") == normalize("לוס אנג'לס") == normalize('לוס אנג"לס')
    assert normalize("נמל התעופה של בוסטון") == normalize("שדה התעופה הבינלאומי בוסטון") == "בוסטון"
    assert normalize("Dallas/Fort Worth") == "dallas fort worth"


def test_code_alias_and_name_matches(catalog: FileReferenceCatalog) -> None:
    lax = catalog.resolve("lax")
    assert lax.status == "matched" and lax.kind == "airport"
    assert [(a.airport_id, a.matched_on) for a in lax.airports] == [("apt_lax", "code")]
    assert codes(catalog.resolve("YUM").airports) == ["YUM"]  # IATA code
    assert codes(catalog.resolve("NYL").airports) == ["YUM"]  # FAA identifier of the same airport
    sna = catalog.resolve("סנטה אנה")
    assert [(a.airport_id, a.matched_on) for a in sna.airports] == [("apt_sna", "alias")]
    assert codes(catalog.resolve("John Wayne Airport").airports) == ["SNA"]
    assert codes(catalog.resolve("לוס אנג׳לס").airports) == ["LAX"]  # geresh variant
    bangor = catalog.resolve("Bangor International Airport")
    assert [(a.code, a.matched_on) for a in bangor.airports] == [("BGR", "name")]
    assert codes(catalog.resolve("Boston Logan").airports) == []  # no fuzzy or partial matching


def test_la_is_los_angeles_and_two_letter_state_codes_are_not_aliases(catalog: FileReferenceCatalog) -> None:
    assert [(a.code, a.matched_on) for a in catalog.resolve("LA").airports] == [("LAX", "alias")]
    for query in ("CA", "MA", "ME"):
        assert catalog.resolve(query).status == "not_found"
    louisiana = catalog.resolve("Louisiana")
    assert louisiana.kind == "state" and louisiana.region is not None and louisiana.region.states == ["LA"]


def test_city_matches_are_ambiguous_unless_context_decides(catalog: FileReferenceCatalog) -> None:
    santa_ana = catalog.resolve("Santa Ana")
    assert [(a.code, a.matched_on) for a in santa_ana.airports] == [("SNA", "city")]

    portland = catalog.resolve("Portland")
    assert portland.status == "ambiguous" and portland.airports == []
    assert codes(portland.candidates) == ["PDX", "PWM"]  # larger hub first
    assert portland.message and "2 airports" in portland.message
    # The official name "Portland International" reduces to the shared city name: still ambiguous.
    assert catalog.resolve("Portland International Airport").status == "ambiguous"

    with_context = catalog.resolve("Portland", context_airport_ids=["apt_pwm", "apt_lax"])
    assert [(a.code, a.matched_on) for a in with_context.airports] == [("PWM", "context")]
    both = catalog.resolve("Portland", context_airport_ids=["apt_pwm", "apt_pdx"])
    assert both.status == "ambiguous"


def test_states_and_regions_expand_to_airports(catalog: FileReferenceCatalog) -> None:
    california = catalog.resolve("California")
    assert california.status == "matched" and california.kind == "state"
    assert codes(california.airports) == ["LAX", "SNA"]
    assert {a.matched_on for a in california.airports} == {"state"}
    assert codes(catalog.resolve("קליפורניה").airports) == ["LAX", "SNA"]

    for query in ("New England", "new england", "ניו אינגלנד"):
        region = catalog.resolve(query)
        assert region.kind == "region" and region.region is not None
        assert region.region.name == "New England"
        assert sorted(region.region.states) == ["CT", "MA", "ME", "NH", "RI", "VT"]
        assert codes(region.airports) == ["BOS", "PWM", "BGR"]

    west = catalog.resolve("West")
    assert west.region is not None and {"CA", "OR", "AZ", "AK"} <= set(west.region.states)


def test_not_found_hebrew_prefix_and_empty_queries(catalog: FileReferenceCatalog) -> None:
    missing = catalog.resolve("Atlantis")
    assert missing.status == "not_found" and missing.message and "Atlantis" in missing.message
    assert codes(catalog.resolve("בסנטה אנה").airports) == ["SNA"]  # "in Santa Ana"
    assert catalog.resolve("   ").status == "not_found"
    assert catalog.resolve("airport").status == "not_found"


def test_loading_validates_rows_and_tolerates_missing_curated_files(tmp_path: Path) -> None:
    catalog = FileReferenceCatalog(make_reference(tmp_path / "a"))
    assert catalog.constraints_for("apt_sna") == [] and catalog.evidence_for("apt_sna") == []
    assert catalog.airport("apt_xyz") is not None and catalog.airport("apt_xyz").mapping_status == "faa_only"  # type: ignore[union-attr]
    assert catalog.airport("apt_lax").aliases == ["Los Angeles", "LA", "לוס אנג'לס"]  # type: ignore[union-attr]

    bad_airport = AIRPORTS.replace("apt_bgr,BGR", "apt_bgx,BGR")
    with pytest.raises(ReferenceDataError, match="apt_<faa_locid>"):
        FileReferenceCatalog(make_reference(tmp_path / "b", **{"airports.csv": bad_airport}))
    with pytest.raises(ReferenceDataError, match="unknown airport id apt_zzz"):
        FileReferenceCatalog(
            make_reference(tmp_path / "c", **{"aliases.csv": ALIASES + "apt_zzz,Nowhere,en\n"})
        )
    unmapped = AIRPORTS.replace("apt_bgr,BGR,BGR,10581", "apt_bgr,BGR,,")
    with pytest.raises(ReferenceDataError, match="needs BTS identifiers"):
        FileReferenceCatalog(make_reference(tmp_path / "d", **{"airports.csv": unmapped}))

    constraints = CONSTRAINT_HEADER + (
        'apt_sna,curfew,"No departures 10 p.m.-7 a.m.",Noise ordinance,https://example.gov/a,2026-10-20\n'
        "apt_dca,slot_control,Slots,Rule,https://example.gov/b,2026-10-20\n"
    )
    loaded = FileReferenceCatalog(make_reference(tmp_path / "e", **{"constraints.csv": constraints}))
    assert [c.constraint_type for c in loaded.constraints_for("apt_sna")] == ["curfew"]
    assert loaded.warnings and "apt_dca" in loaded.warnings[0]  # outside the list: skipped, not fatal

    invalid = CONSTRAINT_HEADER + "apt_sna,noise,Text,Title,https://example.gov/a,yesterday\n"
    with pytest.raises(ReferenceDataError, match=r"constraints\.csv record 2"):
        FileReferenceCatalog(make_reference(tmp_path / "f", **{"constraints.csv": invalid}))


def test_evidence_records_and_reference_version(tmp_path: Path) -> None:
    record = {
        "evidence_id": "sna-cap",
        "airport_id": "apt_sna",
        "topic": "constraint",
        "scenario": "existing",
        "statement": "Passenger cap of 12.2 million annual passengers.",
        "stance_subject": "constraint",
        "stance": "supports",
        "source_title": "Settlement Agreement",
        "source_url": "https://example.gov/sna",
        "source_locator": "Section 2",
        "checked_on": "2026-10-20",
    }
    first = FileReferenceCatalog(make_reference(tmp_path / "a", **{"evidence.json": json.dumps([record])}))
    second = FileReferenceCatalog(
        make_reference(tmp_path / "b", **{"evidence.json": json.dumps({"records": [record]})})
    )
    assert [e.evidence_id for e in first.evidence_for("apt_sna")] == ["sna-cap"]
    assert second.evidence_for("apt_sna") == first.evidence_for("apt_sna")
    assert first.reference_version != second.reference_version  # different file bytes
    assert FileReferenceCatalog(tmp_path / "a").reference_version == first.reference_version
    with pytest.raises(ReferenceDataError, match="duplicate evidence_id"):
        FileReferenceCatalog(
            make_reference(tmp_path / "c", **{"evidence.json": json.dumps([record, record])})
        )


# -- the version-controlled reference files ---------------------------------------------------------


def test_curated_reference_files_resolve_the_exam_airports() -> None:
    catalog = load_reference_catalog()
    assert catalog is load_reference_catalog()  # cached until the files change
    assert [(a.code, a.matched_on) for a in catalog.resolve("LA").airports] == [("LAX", "alias")]
    assert codes(catalog.resolve("סנטה אנה").airports) == ["SNA"]
    assert codes(catalog.resolve("John Wayne").airports) == ["SNA"]
    assert codes(catalog.resolve("Anchorage").airports) == ["ANC"]
    assert codes(catalog.resolve("SFO").airports) == ["SFO"]
    new_england = catalog.resolve("New England")
    assert new_england.kind == "region"
    assert {"BOS", "BDL", "PVD", "PWM", "BTV", "MHT"} <= set(codes(new_england.airports))
    assert {a.state for a in new_england.airports} <= {"CT", "ME", "MA", "NH", "RI", "VT"}
    for city, expected in (
        ("Portland", {"PDX", "PWM"}),
        ("Chicago", {"ORD", "MDW"}),
        ("New York", {"JFK", "LGA"}),
    ):
        outcome = catalog.resolve(city)
        assert outcome.status == "ambiguous" and expected <= set(codes(outcome.candidates))


def test_every_curated_alias_resolves_to_its_airport() -> None:
    catalog = load_reference_catalog()
    for airport in catalog.airports():
        for alias in airport.aliases:
            outcome = catalog.resolve(alias)
            found = {a.airport_id for a in outcome.airports} | {a.airport_id for a in outcome.candidates}
            assert airport.airport_id in found, (alias, outcome.status)
