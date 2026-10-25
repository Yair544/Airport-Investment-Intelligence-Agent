"""Classification rules, completions of unknown signals, ordering and unmet-demand signs."""

from __future__ import annotations

from typing import Literal, get_args

import pytest

from app.analysis import scoring
from app.analysis.metrics import LoadFactor, TaxiStats
from app.analysis.models import (
    RANKING_GROUP_LABELS,
    RANKING_GROUP_ORDER,
    AirportLabel,
    NationalReference,
    RankingGroup,
    RankingRow,
    SignalResult,
    SignalStatus,
)
from tests.analysis_fakes import constraint, evidence

STATUS: dict[str, SignalStatus] = {"p": "pass", "f": "fail", "u": "unknown"}
STRONG, WATCH, LOW = "strong_candidate", "watch", "low_pressure"

# (S1, S2, S3) -> (group, possible groups when information is needed). All 27 combinations.
CLASSIFICATION_TABLE: dict[str, tuple[str, list[str]]] = {
    "ppp": (STRONG, []),
    "ppf": (STRONG, []),
    "pfp": (STRONG, []),
    "pff": (WATCH, []),
    "fpp": (WATCH, []),
    "fpf": (WATCH, []),
    "ffp": (WATCH, []),
    "fff": (LOW, []),
    "upp": ("information_needed", [STRONG, WATCH]),
    "upf": ("information_needed", [STRONG, WATCH]),
    "ufp": ("information_needed", [STRONG, WATCH]),
    "uff": ("information_needed", [WATCH, LOW]),
    "pup": (STRONG, []),  # S3 passes, so S2 does not matter
    "puf": ("information_needed", [STRONG, WATCH]),
    "fup": (WATCH, []),
    "fuf": ("information_needed", [WATCH, LOW]),
    "ppu": (STRONG, []),
    "pfu": ("information_needed", [STRONG, WATCH]),
    "fpu": (WATCH, []),
    "ffu": ("information_needed", [WATCH, LOW]),
    "uup": ("information_needed", [STRONG, WATCH]),
    "uuf": ("information_needed", [STRONG, WATCH, LOW]),
    "upu": ("information_needed", [STRONG, WATCH]),
    "ufu": ("information_needed", [STRONG, WATCH, LOW]),
    "puu": ("information_needed", [STRONG, WATCH]),
    "fuu": ("information_needed", [WATCH, LOW]),
    "uuu": ("information_needed", [STRONG, WATCH, LOW]),
}
RULE_FOR_GROUP = {STRONG: "R2", WATCH: "R3", LOW: "R4", "information_needed": "UNKNOWN"}


def test_the_table_covers_every_signal_combination() -> None:
    assert len(CLASSIFICATION_TABLE) == 27


@pytest.mark.parametrize("states", sorted(CLASSIFICATION_TABLE))
def test_classification_rules(states: str) -> None:
    result = scoring.classify_airport(*(STATUS[state] for state in states))
    group, possible = CLASSIFICATION_TABLE[states]
    assert result.group == group
    assert result.possible_groups == possible
    assert result.rule_id == RULE_FOR_GROUP[group]


@pytest.mark.parametrize("states", sorted(CLASSIFICATION_TABLE))
def test_documented_constraint_takes_precedence_over_every_signal_state(states: str) -> None:
    result = scoring.classify_airport(*(STATUS[state] for state in states), [constraint("apt_x", "curfew")])
    assert (result.group, result.rule_id, result.possible_groups) == ("external_constraint", "R1", [])
    assert "(curfew)" in result.rule


def test_information_needed_names_only_the_signal_that_decides() -> None:
    one = scoring.classify_airport("unknown", "pass", "fail")
    assert one.rule == (
        "Information needed: the group depends on unknown signals (S1 unknown, S2 pass, S3 fail). "
        "S1 unknown: pass → Strong candidate, fail → Watch."
    )
    # With S3 passing, S2 cannot change the outcome, so only S1 is named.
    irrelevant = scoring.classify_airport("unknown", "unknown", "pass")
    assert irrelevant.rule.endswith("S1 unknown: pass → Strong candidate, fail → Watch.")
    two = scoring.classify_airport("unknown", "unknown", "fail")
    assert two.rule.endswith(
        "S1 and S2 unknown: S1 pass, S2 pass → Strong candidate; S1 pass, S2 fail → Watch; "
        "S1 fail, S2 pass → Watch; S1 fail, S2 fail → Low pressure."
    )


def test_decided_group_with_an_unknown_signal_says_it_holds_for_all_completions() -> None:
    result = scoring.classify_airport("pass", "pass", "unknown")
    assert result.rule == (
        "R2: S1 passes and at least one of S2 and S3 passes (S1 pass, S2 pass, S3 unknown). "
        "This holds whether S3 passes or fails."
    )
    known = scoring.classify_airport("fail", "fail", "fail")
    assert known.rule == "R4: no pressure signal passes (S1 fail, S2 fail, S3 fail)."


def test_threshold_status_compares_at_full_precision() -> None:
    assert scoring.threshold_status(1.7644, 1.7644) == "pass"  # equal passes
    assert scoring.threshold_status(1.76439, 1.7644) == "fail"
    assert scoring.threshold_status(None, 1.0) == "unknown"
    assert scoring.threshold_status(1.0, None) == "unknown"


# -- signal evaluation ---------------------------------------------------------------------------


def _reference(value: float | None) -> NationalReference:
    return NationalReference(metric="t100_load_factor", period_key="CY2025", value=value)


def test_s1_reports_missing_growth_and_missing_reference() -> None:
    s1 = scoring.evaluate_s1(2.5, _reference(2.0), period="FY2024-FY2029")
    assert (s1.status, s1.value, s1.reference, s1.unit, s1.note) == (
        "pass",
        2.5,
        2.0,
        "percent per year",
        None,
    )
    missing = scoring.evaluate_s1(None, _reference(2.0), period="FY2024-FY2029")
    assert missing.status == "unknown"
    assert missing.note is not None and "FY2024-FY2029" in missing.note
    no_reference = scoring.evaluate_s1(2.5, _reference(None), period="FY2024-FY2029")
    assert no_reference.status == "unknown"


def test_s2_above_100_percent_keeps_its_status_and_is_flagged() -> None:
    load_factor = LoadFactor(value=103.0, passengers=1_030, seats=1_000, departures=10, rows=1)
    s2 = scoring.evaluate_s2(load_factor, _reference(80.0), period="CY2025")
    assert (s2.status, s2.note, s2.sample_size) == ("pass", scoring.LOAD_FACTOR_REVIEW_NOTE, 10)


def test_s2_without_mapping_rows_or_seats_is_unknown() -> None:
    unmapped = scoring.evaluate_s2(None, _reference(80.0), period="CY2025")
    assert unmapped.status == "unknown"
    assert unmapped.note == "No verified BTS airport mapping, so T-100 data cannot be joined."
    no_rows = scoring.evaluate_s2(LoadFactor(None, None, None, None, 0), _reference(80.0), period="CY2025")
    assert (no_rows.status, no_rows.note) == (
        "unknown",
        "No T-100 class F departures are reported for CY2025.",
    )
    zero_seats = scoring.evaluate_s2(LoadFactor(None, 0, 0, 3, 1), _reference(80.0), period="CY2025")
    assert zero_seats.status == "unknown"
    assert zero_seats.note == "T-100 class F departures report zero seats for CY2025."


def test_s3_small_sample_is_unknown_but_keeps_its_value() -> None:
    small = scoring.evaluate_s3(
        TaxiStats(sample=999, median=12.0, p10=8.0, excess=4.0),
        _reference(3.0),
        period="CY2025",
        min_departures=1_000,
    )
    assert (small.status, small.value, small.sample_size) == ("unknown", 4.0, 999)
    assert small.note == ("Taxi-out sample of 999 departures is below the 1,000 minimum, so S3 is unknown.")
    enough = scoring.evaluate_s3(
        TaxiStats(sample=1_000, median=12.0, p10=8.0, excess=4.0),
        _reference(3.0),
        period="CY2025",
        min_departures=1_000,
    )
    assert (enough.status, enough.note) == ("pass", None)
    empty = scoring.evaluate_s3(
        TaxiStats(0, None, None, None), _reference(3.0), period="CY2025", min_departures=1_000
    )
    assert empty.status == "unknown"
    unmapped = scoring.evaluate_s3(None, _reference(3.0), period="CY2025", min_departures=1_000)
    assert (unmapped.status, unmapped.sample_size) == ("unknown", None)


def test_terminal_evidence_is_matched_on_the_infrastructure_component() -> None:
    assert scoring.has_terminal_evidence([evidence("apt_x", " Terminal ")])
    assert not scoring.has_terminal_evidence([evidence("apt_x", "runway"), evidence("apt_x", None)])
    assert not scoring.has_terminal_evidence([])


# -- ordering ----------------------------------------------------------------------------------


def _row(code: str, group: RankingGroup, delta: int | None, load_factor: float | None) -> RankingRow:
    def signal(name: Literal["S1", "S2", "S3"], value: float | None) -> SignalResult:
        return SignalResult(
            signal=name,
            name=name,
            value=value,
            reference=None,
            status="unknown",
            unit="",
        )

    return RankingRow(
        rank=0,
        airport=AirportLabel(airport_id=f"apt_{code.lower()}", code=code, name=code, city=code, state="MA"),
        group=group,
        group_label=RANKING_GROUP_LABELS[group],
        rule_id="R2",
        rule="",
        base_year_enplanements=None,
        horizon_enplanements=None,
        delta_enplanements=delta,
        cagr_5=None,
        base_year_commercial_operations=None,
        horizon_commercial_operations=None,
        delta_commercial_operations=None,
        signals=[signal("S1", None), signal("S2", load_factor), signal("S3", None)],
        evidence_status="missing",
    )


def test_rows_are_ordered_by_group_then_delta_then_load_factor_then_code() -> None:
    rows = [
        _row("LOW", "low_pressure", 900_000, 90.0),
        _row("WB", "watch", 100, None),
        _row("SA", "strong_candidate", 50_000, 80.0),
        _row("SB", "strong_candidate", 50_000, 85.0),  # same ΔE as SA, higher load factor
        _row("SC", "strong_candidate", 50_000, None),  # same ΔE, load factor missing
        _row("SD", "strong_candidate", None, 99.0),  # ΔE missing: last in its group
        _row("SE", "strong_candidate", 70_000, 10.0),
        _row("WA", "watch", 100, None),  # full tie with WB: airport code decides
        _row("INFO", "information_needed", 1_000_000, 95.0),
        _row("EXT", "external_constraint", 5, 10.0),
    ]
    ordered = scoring.order_ranking_rows(rows)
    assert [row.airport.code for row in ordered] == [
        "SE",
        "SB",
        "SA",
        "SC",
        "SD",
        "WA",
        "WB",
        "EXT",
        "INFO",
        "LOW",
    ]
    assert [row.rank for row in ordered] == list(range(1, 11))
    assert {row.rank for row in rows} == {0}  # the input rows are not modified


def test_group_display_order_matches_the_spec() -> None:
    assert RANKING_GROUP_ORDER == (
        "strong_candidate",
        "watch",
        "external_constraint",
        "information_needed",
        "low_pressure",
    )
    assert set(RANKING_GROUP_ORDER) == set(get_args(RankingGroup))


# -- unmet demand ------------------------------------------------------------------------------

UNMET_CASES = [
    # (S2, passenger growth > seat growth, S3, constraint on file) -> classification, blocking inputs
    (("pass", "pass", "pass", False), "constrained_demand_signs", ()),
    (("pass", "pass", "fail", True), "constrained_demand_signs", ()),
    (("pass", "pass", "unknown", True), "constrained_demand_signs", ()),
    (("pass", "pass", "unknown", False), "undetermined", ("S3",)),
    (("pass", "pass", "fail", False), "undetermined", ()),
    (("fail", "fail", "pass", False), "weak_demand_signs", ()),
    (("fail", "fail", "unknown", True), "weak_demand_signs", ()),
    (("pass", "fail", "pass", True), "undetermined", ()),
    (("fail", "pass", "pass", True), "undetermined", ()),
    (("unknown", "fail", "fail", False), "undetermined", ("S2",)),
    (("unknown", "pass", "pass", False), "undetermined", ("S2",)),
    (("fail", "unknown", "fail", False), "undetermined", ("G",)),
    (("pass", "unknown", "pass", False), "undetermined", ("G",)),
    (("unknown", "unknown", "unknown", False), "undetermined", ("S2", "G", "S3")),
]


@pytest.mark.parametrize(("inputs", "expected", "blocking"), UNMET_CASES)
def test_unmet_demand_classification(
    inputs: tuple[SignalStatus, SignalStatus, SignalStatus, bool],
    expected: str,
    blocking: tuple[str, ...],
) -> None:
    result = scoring.classify_unmet_demand(*inputs)
    assert result.classification == expected
    assert result.blocking_inputs == blocking
    assert result.label == scoring.UNMET_LABELS[result.classification]


def test_unmet_demand_explanations() -> None:
    blocked = scoring.classify_unmet_demand("pass", "pass", "unknown", False)
    assert blocked.rule == (
        "Cannot be determined because the result depends on unknown inputs. "
        "S3 unknown: pass → signs of constrained demand, fail → neither pattern."
    )
    decided = scoring.classify_unmet_demand("pass", "pass", "unknown", True)
    assert decided.rule.endswith("This holds whether S3 passes or fails.")
    growth = scoring.classify_unmet_demand("fail", "unknown", "fail", False)
    assert growth.rule.endswith(
        "passenger vs seat growth unknown: higher → neither pattern, not higher → signs of weak demand."
    )
    neither = scoring.classify_unmet_demand("pass", "fail", "pass", False)
    assert neither.rule.startswith("Cannot be determined: neither pattern holds.")
