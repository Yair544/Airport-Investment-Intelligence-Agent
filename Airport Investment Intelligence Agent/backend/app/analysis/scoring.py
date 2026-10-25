"""Deterministic signal tests, classification rules and ranking order (spec sections 4.3-4.5).

Signals have three states: pass, fail or unknown. A rule set is applied to every pass/fail
completion of the unknown signals; when all completions agree the outcome is decided, otherwise
the result says which unknown inputs block the decision. Missing data therefore never counts as
evidence for or against an airport.
"""

from __future__ import annotations

import itertools
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from app.analysis.metrics import LoadFactor, TaxiStats, as_int
from app.analysis.models import (
    RANKING_GROUP_LABELS,
    RANKING_GROUP_ORDER,
    ConstraintRecord,
    EvidenceRecord,
    NationalReference,
    RankingGroup,
    RankingRow,
    SignalResult,
    SignalStatus,
)

SIGNAL_NAMES: dict[str, str] = {
    "S1": "Forecast demand growth (TAF CAGR)",
    "S2": "Seat load factor (T-100 class F departures)",
    "S3": "Taxi-out excess (median minus P10)",
}
SIGNAL_UNITS: dict[str, str] = {"S1": "percent per year", "S2": "percent", "S3": "minutes"}
LOAD_FACTOR_REVIEW_NOTE = "Load factor above 100% — flagged for review."
FURTHER_REVIEW_NOTE = "Candidate for further review: no terminal capacity evidence on file"
CONSTRAINT_TYPE_LABELS: dict[str, str] = {
    "runway_airspace": "runway or airspace",
    "curfew": "curfew",
    "legal_cap": "legal or contractual cap",
    "slot_control": "slot control",
    "other": "other constraint",
}


def threshold_status(value: float | None, reference: float | None) -> SignalStatus:
    """pass when value >= reference (compared at full precision), fail when below, unknown when
    either value is missing."""
    if value is None or reference is None:
        return "unknown"
    return "pass" if value >= reference else "fail"


def join_words(words: Sequence[str]) -> str:
    """ "S1", "S1 and S2", "S1, S2 and S3"."""
    if len(words) <= 1:
        return "".join(words)
    return f"{', '.join(words[:-1])} and {words[-1]}"


# ---------------------------------------------------------------------------------------------
# Signal evaluation
# ---------------------------------------------------------------------------------------------


def _signal(
    signal: Literal["S1", "S2", "S3"],
    value: float | None,
    reference: NationalReference,
    status: SignalStatus,
    notes: Sequence[str],
    sample_size: int | None = None,
) -> SignalResult:
    return SignalResult(
        signal=signal,
        name=SIGNAL_NAMES[signal],
        value=value,
        reference=reference.value,
        status=status,
        unit=SIGNAL_UNITS[signal],
        sample_size=sample_size,
        note=" ".join(notes) or None,
    )


def evaluate_s1(cagr: float | None, reference: NationalReference, *, period: str) -> SignalResult:
    """S1 passes when the airport's TAF CAGR is at least the national CAGR of the same edition."""
    notes: list[str] = []
    if cagr is None:
        notes.append(f"TAF enplanements for {period} are missing or zero, so the growth rate is unknown.")
    elif reference.value is None:
        notes.append(f"The national TAF growth reference for {period} is not available.")
    return _signal("S1", cagr, reference, threshold_status(cagr, reference.value), notes)


def evaluate_s2(load_factor: LoadFactor | None, reference: NationalReference, *, period: str) -> SignalResult:
    """S2 passes when the class F load factor is at least the national load factor.

    `load_factor` is None when the airport has no verified BTS mapping. A load factor above
    100% keeps its status and is flagged for review.
    """
    if load_factor is None:
        return _signal(
            "S2",
            None,
            reference,
            "unknown",
            ["No verified BTS airport mapping, so T-100 data cannot be joined."],
        )
    value = load_factor.value
    notes: list[str] = []
    if value is None:
        if load_factor.rows == 0:
            notes.append(f"No T-100 class F departures are reported for {period}.")
        elif load_factor.seats is None:
            notes.append(f"T-100 seat counts are missing for {period}.")
        else:
            notes.append(f"T-100 class F departures report zero seats for {period}.")
    elif reference.value is None:
        notes.append(f"The national load factor reference for {period} is not available.")
    if value is not None and value > 100:
        notes.append(LOAD_FACTOR_REVIEW_NOTE)
    return _signal(
        "S2",
        value,
        reference,
        threshold_status(value, reference.value),
        notes,
        sample_size=as_int(load_factor.departures),
    )


def evaluate_s3(
    stats: TaxiStats | None, reference: NationalReference, *, period: str, min_departures: int
) -> SignalResult:
    """S3 passes when the taxi-out excess is at least the national median excess.

    `stats` is None when the airport has no verified BTS mapping. A sample below
    `min_departures` departures makes the signal unknown; its value is still reported.
    """
    if stats is None:
        return _signal(
            "S3",
            None,
            reference,
            "unknown",
            ["No verified BTS airport mapping, so On-Time data cannot be joined."],
        )
    notes: list[str] = []
    status: SignalStatus = "unknown"
    if stats.sample == 0:
        notes.append(f"No On-Time taxi-out data for {period}; reporting carriers may not serve the airport.")
    elif stats.sample < min_departures:
        notes.append(
            f"Taxi-out sample of {stats.sample:,} departures is below the {min_departures:,} "
            "minimum, so S3 is unknown."
        )
    else:
        status = threshold_status(stats.excess, reference.value)
        if reference.value is None:
            notes.append(f"The national taxi-out excess reference for {period} is not available.")
    return _signal("S3", stats.excess, reference, status, notes, sample_size=stats.sample)


def has_terminal_evidence(evidence: Sequence[EvidenceRecord]) -> bool:
    return any((record.infrastructure_component or "").strip().lower() == "terminal" for record in evidence)


# ---------------------------------------------------------------------------------------------
# Completions of unknown inputs
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Completions[T]:
    """Outcomes of a rule set for every pass/fail completion of the unknown inputs."""

    unknown: tuple[str, ...]  # unknown inputs, in input order
    outcomes: dict[tuple[bool, ...], T]  # completion (one bool per unknown input) -> outcome
    relevant: tuple[str, ...]  # unknown inputs that change the outcome in some completion

    @property
    def distinct(self) -> list[T]:
        seen: list[T] = []
        for outcome in self.outcomes.values():
            if outcome not in seen:
                seen.append(outcome)
        return seen

    def outcome_for(self, assignment: Mapping[str, bool]) -> T:
        """Outcome when the given unknown inputs take these values (others do not matter)."""
        for completion, outcome in self.outcomes.items():
            values = dict(zip(self.unknown, completion, strict=True))
            if all(values[name] == value for name, value in assignment.items()):
                return outcome
        raise KeyError(assignment)


def complete_unknowns[T](
    states: Mapping[str, SignalStatus], decide: Callable[[Mapping[str, bool]], T]
) -> Completions[T]:
    """Apply `decide` to every completion of the unknown inputs (pass is tried before fail)."""
    unknown = tuple(name for name, status in states.items() if status == "unknown")
    known = {name: status == "pass" for name, status in states.items() if status != "unknown"}
    outcomes: dict[tuple[bool, ...], T] = {}
    for completion in itertools.product((True, False), repeat=len(unknown)):
        outcomes[completion] = decide({**known, **dict(zip(unknown, completion, strict=True))})
    relevant = tuple(
        name
        for index, name in enumerate(unknown)
        if any(
            outcomes[completion]
            != outcomes[(*completion[:index], not completion[index], *completion[index + 1 :])]
            for completion in outcomes
        )
    )
    return Completions(unknown=unknown, outcomes=outcomes, relevant=relevant)


@dataclass(frozen=True)
class _Wording:
    label: str
    true_word: str
    false_word: str
    either: str  # completes "This holds whether ..."


def _explain_dependency[T](
    completions: Completions[T], wording: Mapping[str, _Wording], describe: Callable[[T], str]
) -> str:
    """E.g. "S2 unknown: pass → Strong candidate, fail → Watch." for the relevant inputs."""
    relevant = completions.relevant
    labels = [wording[name].label for name in relevant]
    parts: list[str] = []
    for values in itertools.product((True, False), repeat=len(relevant)):
        assignment = dict(zip(relevant, values, strict=True))
        if len(relevant) == 1:
            condition = wording[relevant[0]].true_word if values[0] else wording[relevant[0]].false_word
        else:
            condition = ", ".join(
                f"{wording[name].label} {wording[name].true_word if value else wording[name].false_word}"
                for name, value in assignment.items()
            )
        parts.append(f"{condition} → {describe(completions.outcome_for(assignment))}")
    separator = ", " if len(relevant) == 1 else "; "
    return f"{join_words(labels)} unknown: {separator.join(parts)}."


def _holds_for_all[T](completions: Completions[T], wording: Mapping[str, _Wording]) -> str:
    if not completions.unknown:
        return ""
    if len(completions.unknown) == 1:
        return f" This holds whether {wording[completions.unknown[0]].either}."
    labels = join_words([wording[name].label for name in completions.unknown])
    return f" This holds for every possible value of the unknown inputs ({labels})."


# ---------------------------------------------------------------------------------------------
# Ranking classification (spec section 4.4)
# ---------------------------------------------------------------------------------------------

RuleId = Literal["R1", "R2", "R3", "R4", "UNKNOWN"]
RULE_TEXT: dict[str, str] = {
    "R2": "R2: S1 passes and at least one of S2 and S3 passes",
    "R3": "R3: at least one pressure signal passes, but R2 does not apply",
    "R4": "R4: no pressure signal passes",
}
_GROUP_RULE: dict[RankingGroup, RuleId] = {
    "strong_candidate": "R2",
    "watch": "R3",
    "low_pressure": "R4",
}
_SIGNAL_WORDING = {
    name: _Wording(name, "pass", "fail", f"{name} passes or fails") for name in ("S1", "S2", "S3")
}


@dataclass(frozen=True)
class Classification:
    group: RankingGroup
    rule_id: RuleId
    rule: str
    possible_groups: list[RankingGroup]


def decide_group(signals: Mapping[str, bool]) -> RankingGroup:
    """Rules R2-R4 for fully known signals (R1 is checked before any completion)."""
    if signals["S1"] and (signals["S2"] or signals["S3"]):
        return "strong_candidate"
    if signals["S1"] or signals["S2"] or signals["S3"]:
        return "watch"
    return "low_pressure"


def classify_airport(
    s1: SignalStatus,
    s2: SignalStatus,
    s3: SignalStatus,
    constraints: Sequence[ConstraintRecord] = (),
) -> Classification:
    """First matching rule: R1 constraint on file, R2 strong candidate, R3 watch, R4 low pressure.

    With unknown signals the rules run on every completion; disagreeing completions give
    "information_needed" with the possible groups and the unknown signals that decide between
    them. R1 never depends on unknown signals.
    """
    if constraints:
        kinds = sorted({CONSTRAINT_TYPE_LABELS[record.constraint_type] for record in constraints})
        return Classification(
            group="external_constraint",
            rule_id="R1",
            rule=(
                f"R1: a documented constraint outside the terminal is on file ({', '.join(kinds)}),"
                " so the airport is listed separately regardless of the pressure signals."
            ),
            possible_groups=[],
        )
    states = {"S1": s1, "S2": s2, "S3": s3}
    summary = ", ".join(f"{name} {status}" for name, status in states.items())
    completions = complete_unknowns(states, decide_group)
    distinct = completions.distinct
    if len(distinct) == 1:
        group = distinct[0]
        rule_id = _GROUP_RULE[group]
        return Classification(
            group=group,
            rule_id=rule_id,
            rule=f"{RULE_TEXT[rule_id]} ({summary}).{_holds_for_all(completions, _SIGNAL_WORDING)}",
            possible_groups=[],
        )
    explanation = _explain_dependency(completions, _SIGNAL_WORDING, lambda group: RANKING_GROUP_LABELS[group])
    return Classification(
        group="information_needed",
        rule_id="UNKNOWN",
        rule=f"Information needed: the group depends on unknown signals ({summary}). {explanation}",
        possible_groups=[group for group in RANKING_GROUP_ORDER if group in distinct],
    )


def ranking_sort_key(row: RankingRow) -> tuple[int, bool, int, bool, float, str]:
    """Group display order, then ΔE descending, then load factor descending (None last in
    both), then airport code."""
    load_factor = next((signal.value for signal in row.signals if signal.signal == "S2"), None)
    delta = row.delta_enplanements
    return (
        RANKING_GROUP_ORDER.index(row.group),
        delta is None,
        -(delta or 0),
        load_factor is None,
        -(load_factor or 0.0),
        row.airport.code,
    )


def order_ranking_rows(rows: Sequence[RankingRow]) -> list[RankingRow]:
    """Sort rows for display and number them 1..N across the displayed list."""
    ordered = sorted(rows, key=ranking_sort_key)
    return [row.model_copy(update={"rank": position}) for position, row in enumerate(ordered, 1)]


# ---------------------------------------------------------------------------------------------
# Unmet demand signs (spec section 4.5)
# ---------------------------------------------------------------------------------------------

UnmetClass = Literal["constrained_demand_signs", "weak_demand_signs", "undetermined"]
UNMET_LABELS: dict[UnmetClass, str] = {
    "constrained_demand_signs": "Signs of constrained demand",
    "weak_demand_signs": "Signs of weak demand",
    "undetermined": "Cannot be determined",
}
_Pattern = Literal["constrained", "weak", "neither"]
_PATTERN_WORDS: dict[_Pattern, str] = {
    "constrained": "signs of constrained demand",
    "weak": "signs of weak demand",
    "neither": "neither pattern",
}
CONSTRAINED_RULE = (
    "S2 passes, passenger growth exceeds seat growth, and S3 passes or a documented constraint "
    "outside the terminal exists"
)
WEAK_RULE = "S2 fails and passenger growth does not exceed seat growth"
_UNMET_WORDING = {
    "S2": _Wording("S2", "pass", "fail", "S2 passes or fails"),
    "G": _Wording(
        "passenger vs seat growth",
        "higher",
        "not higher",
        "passenger growth is higher than seat growth or not",
    ),
    "S3": _Wording("S3", "pass", "fail", "S3 passes or fails"),
}


@dataclass(frozen=True)
class UnmetClassification:
    classification: UnmetClass
    label: str
    rule: str
    blocking_inputs: tuple[str, ...]  # "S2", "G" (passenger vs seat growth), "S3"


def decide_unmet(values: Mapping[str, bool], has_constraint: bool) -> _Pattern:
    if values["S2"] and values["G"] and (values["S3"] or has_constraint):
        return "constrained"
    if not values["S2"] and not values["G"]:
        return "weak"
    return "neither"


def classify_unmet_demand(
    s2: SignalStatus, growth_higher: SignalStatus, s3: SignalStatus, has_constraint: bool
) -> UnmetClassification:
    """Constrained-demand signs: S2 pass, passenger growth > seat growth, and S3 pass or a known
    constraint. Weak-demand signs: S2 fail and passenger growth not above seat growth. Anything
    else, or an unknown input that changes the outcome, cannot be determined."""
    completions = complete_unknowns(
        {"S2": s2, "G": growth_higher, "S3": s3},
        lambda values: decide_unmet(values, has_constraint),
    )
    distinct = completions.distinct
    holds = _holds_for_all(completions, _UNMET_WORDING)
    if distinct == ["constrained"]:
        return UnmetClassification(
            "constrained_demand_signs",
            UNMET_LABELS["constrained_demand_signs"],
            f"Signs of constrained demand: {CONSTRAINED_RULE}.{holds}",
            (),
        )
    if distinct == ["weak"]:
        return UnmetClassification(
            "weak_demand_signs",
            UNMET_LABELS["weak_demand_signs"],
            f"Signs of weak demand: {WEAK_RULE}.{holds}",
            (),
        )
    if len(distinct) == 1:
        rule = (
            f"Cannot be determined: neither pattern holds. Constrained demand requires that "
            f"{CONSTRAINED_RULE}; weak demand requires that {WEAK_RULE}.{holds}"
        )
        return UnmetClassification("undetermined", UNMET_LABELS["undetermined"], rule, ())
    explanation = _explain_dependency(completions, _UNMET_WORDING, lambda pattern: _PATTERN_WORDS[pattern])
    return UnmetClassification(
        "undetermined",
        UNMET_LABELS["undetermined"],
        f"Cannot be determined because the result depends on unknown inputs. {explanation}",
        completions.relevant,
    )
