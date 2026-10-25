"""Shared normalized reference records and analysis result models.

These models are the contract between the data layer, the analysis layer, the agent, the HTTP API
and the frontend (`frontend/src/types.ts` mirrors them). Percentages are expressed as percent
values (for example 84.2 means 84.2%), distances in statute miles and durations in minutes.
Missing values stay `None` and are never replaced by zero.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

RULES_VERSION = "2026.10-1"

SignalStatus = Literal["pass", "fail", "unknown"]
SourceId = Literal[
    "faa_taf",
    "bts_t100_segment",
    "bts_ontime",
    "bts_t100_summary",
    "constraints_table",
    "evidence_records",
]
DelayCause = Literal["carrier", "weather", "nas", "security", "late_aircraft"]
DELAY_CAUSES: tuple[DelayCause, ...] = ("carrier", "weather", "nas", "security", "late_aircraft")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Reference records (version-controlled files under app/data/reference/)
# ---------------------------------------------------------------------------


class AirportRef(Model):
    """Canonical airport record with a verified mapping to the identifiers used by each source."""

    airport_id: str  # stable internal id, e.g. "apt_lax"
    faa_locid: str  # FAA location identifier used by TAF
    iata_code: str | None  # code used by BTS T-100 and On-Time (ORIGIN/DEST)
    bts_airport_id: int | None  # BTS numeric AIRPORT_ID, the join key for BTS tables
    name: str
    city: str
    state: str  # two-letter USPS code
    hub_size: str | None = None  # "large" | "medium" | "small" | "nonhub" | None
    mapping_status: Literal["verified", "manual_override", "faa_only"] = "verified"
    aliases: list[str] = Field(default_factory=list)

    @property
    def code(self) -> str:
        return self.iata_code or self.faa_locid


ConstraintType = Literal["runway_airspace", "curfew", "legal_cap", "slot_control", "other"]


class ConstraintRecord(Model):
    """Documented limit outside the terminal that may block added activity (spec section 4.3)."""

    airport_id: str
    constraint_type: ConstraintType
    description: str
    source_title: str
    source_url: str
    checked_on: date


class EvidenceRecord(Model):
    """Official demand/infrastructure evidence for an airport (spec sections 2 and 3)."""

    evidence_id: str
    airport_id: str
    topic: Literal["demand", "capacity", "constraint", "expansion_plan"]
    infrastructure_component: str | None = None  # e.g. "terminal", "gates", "runway"
    scenario: Literal["existing", "future_without_project", "future_with_project", "not_applicable"]
    statement: str
    value: float | None = None
    unit: str | None = None
    activity_direction: str | None = None  # e.g. "enplanements", "total passengers", "departures"
    period: str | None = None  # observation period or forecast horizon, e.g. "2023" or "2040"
    assumptions: str | None = None
    limitations: str | None = None
    project_status: str | None = None
    expected_operation: str | None = None
    stance_subject: Literal["demand", "constraint", "expansion_fit"]
    stance: Literal["supports", "contradicts", "insufficient"]
    source_title: str
    source_url: str
    source_locator: str  # page, section or table
    source_date: str | None = None
    checked_on: date


# ---------------------------------------------------------------------------
# Provenance, periods and national references
# ---------------------------------------------------------------------------


class SourceRef(Model):
    source_id: SourceId
    name: str
    url: str
    period: str | None = None  # e.g. "CY2025", "2025-01 to 2025-06", "FY2024-FY2029"
    retrieved_at: datetime | None = None
    dataset_version: str | None = None  # e.g. "TAF 2025 edition" or a file signature
    data_version_id: str | None = None


ALL_MONTHS: tuple[int, ...] = tuple(range(1, 13))


def calendar_label(year: int) -> str:
    """Historical analysis periods are full calendar years in this version (e.g. "CY2025")."""
    return f"CY{year}"


def fiscal_label(year: int) -> str:
    """TAF periods are FAA fiscal years, October 1 to September 30 (e.g. "FY2029")."""
    return f"FY{year}"


class PeriodInfo(Model):
    """A period used by a result, labelled so historical and forecast values are never mixed up."""

    label: str  # "CY2025", "FY2024", "FY2029"
    kind: Literal["calendar_year", "calendar_months", "fiscal_year"]
    nature: Literal["historical", "forecast"]
    source_id: SourceId
    start: date
    end: date
    role: str | None = None  # e.g. "base year", "forecast horizon", "analysis year"


class NationalReference(Model):
    """A national benchmark computed from the full source data of one data version."""

    metric: Literal["taf_cagr", "t100_load_factor", "ontime_taxi_out_excess_median"]
    period_key: str  # e.g. "FY2024-FY2029", "CY2025"
    value: float | None
    sample_size: int | None = None  # airports or flights behind the value
    details: dict[str, float | int | str | None] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Airport resolution (tool output for the model; not part of `results`)
# ---------------------------------------------------------------------------


class ResolvedAirport(Model):
    airport_id: str
    code: str
    faa_locid: str
    name: str
    city: str
    state: str
    matched_on: str  # e.g. "code", "alias", "name", "city", "region", "state", "context"


class ResolvedRegion(Model):
    region_id: str
    name: str
    states: list[str]


class ResolutionOutcome(Model):
    query: str
    status: Literal["matched", "ambiguous", "not_found"]
    kind: Literal["airport", "region", "state"] | None = None
    airports: list[ResolvedAirport] = Field(default_factory=list)  # matched airport(s)
    candidates: list[ResolvedAirport] = Field(default_factory=list)  # options when ambiguous
    region: ResolvedRegion | None = None
    message: str | None = None


# ---------------------------------------------------------------------------
# Analysis results (one model per analysis type)
# ---------------------------------------------------------------------------


class AirportLabel(Model):
    airport_id: str
    code: str
    name: str
    city: str
    state: str


class SignalResult(Model):
    signal: Literal["S1", "S2", "S3"]
    name: str
    value: float | None
    reference: float | None  # national reference used for the comparison
    status: SignalStatus
    unit: str
    sample_size: int | None = None
    note: str | None = None


RankingGroup = Literal[
    "strong_candidate", "watch", "external_constraint", "information_needed", "low_pressure"
]
RANKING_GROUP_ORDER: tuple[RankingGroup, ...] = (
    "strong_candidate",
    "watch",
    "external_constraint",
    "information_needed",
    "low_pressure",
)
RANKING_GROUP_LABELS: dict[RankingGroup, str] = {
    "strong_candidate": "Strong candidate",
    "watch": "Watch",
    "external_constraint": "Known constraint outside the terminal",
    "information_needed": "Information needed",
    "low_pressure": "Low pressure",
}


class RankingRow(Model):
    rank: int  # position in the displayed order, starting at 1
    airport: AirportLabel
    group: RankingGroup
    group_label: str
    rule_id: Literal["R1", "R2", "R3", "R4", "UNKNOWN"]
    rule: str  # human-readable rule that decided the group
    possible_groups: list[RankingGroup] = Field(default_factory=list)  # when information is needed
    base_year_enplanements: int | None
    horizon_enplanements: int | None
    delta_enplanements: int | None  # ΔE, the primary metric
    cagr_5: float | None  # percent per year
    base_year_commercial_operations: int | None
    horizon_commercial_operations: int | None
    delta_commercial_operations: int | None
    signals: list[SignalResult]  # S1, S2, S3 in this order
    constraints: list[ConstraintRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    evidence_status: Literal["available", "missing"]
    # True for strong/watch airports without terminal evidence on file (spec section 2):
    # shown as "candidate for further review" instead of a proven need.
    further_review: bool = False
    notes: list[str] = Field(default_factory=list)


class ExcludedAirport(Model):
    airport: AirportLabel
    reason: str


class RankingData(Model):
    region_label: str | None
    analysis_year: int  # t, the historical calendar year for S2 and S3
    taf_edition: str
    base_year: int  # b, last historical fiscal year in the TAF edition
    horizon_year: int  # b + horizon
    national_references: dict[Literal["S1", "S2", "S3"], NationalReference]
    rows: list[RankingRow]
    excluded: list[ExcludedAirport] = Field(default_factory=list)


class DelayCauseShare(Model):
    cause: DelayCause
    minutes: float | None
    share: float | None  # percent of attributed delay minutes


class ComparisonAirport(Model):
    airport: AirportLabel
    days_covered: int | None
    departures_scheduled: int | None
    departures_performed: int | None
    departures_performed_per_day: float | None
    departure_delay_rate: float | None  # percent of non-cancelled departures with DepDel15 = 1
    departure_delay_base: int | None  # non-cancelled departures with a valid DepDel15
    cancellation_rate: float | None  # percent of scheduled departures
    median_taxi_out: float | None
    taxi_out_p10: float | None
    taxi_out_excess: float | None
    taxi_out_sample: int | None
    median_taxi_in: float | None
    taxi_in_sample: int | None
    attributed_delay_minutes: float | None
    delay_causes: list[DelayCauseShare]
    constraints: list[ConstraintRecord] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class ComparisonData(Model):
    year: int
    period_label: str
    airports: list[ComparisonAirport]


class AircraftConfigShare(Model):
    aircraft_config: int
    label: str  # "Passenger", "Freight", "Passenger/freight combination", "Seaplane", "Unknown"
    long_haul_departures: float
    valid_departures: float
    share: float | None  # percent


class DistanceGroupRow(Model):
    distance_group: int
    label: str  # e.g. "3,500-3,999 mi"
    departures: float
    share: float | None  # percent of departures with a valid distance


class DestinationRow(Model):
    dest_code: str
    dest_name: str | None = None
    distance_miles: float
    departures: float


class LongHaulData(Model):
    airport: AirportLabel
    year: int
    period_label: str
    threshold_miles: float
    threshold_nautical_miles: float
    total_departures: float
    valid_departures: float
    long_haul_departures: float
    long_haul_share: float | None  # percent
    excluded_departures: float  # departures with a missing or non-positive distance
    excluded_share: float | None  # percent of all performed departures
    by_aircraft_config: list[AircraftConfigShare]
    distance_groups: list[DistanceGroupRow]
    top_long_haul_destinations: list[DestinationRow] = Field(default_factory=list)


class ConditionResult(Model):
    condition_id: str  # e.g. "S2", "PAX_VS_SEATS", "S3_OR_CONSTRAINT"
    description: str
    status: SignalStatus
    detail: str | None = None


class MonthlyDelayCauses(Model):
    year: int
    month: int
    arrivals: int | None
    attributed_delay_minutes: float | None
    causes: list[DelayCauseShare]


class UnmetDemandData(Model):
    airport: AirportLabel
    analysis_year: int
    comparison_year: int
    classification: Literal["constrained_demand_signs", "weak_demand_signs", "undetermined"]
    classification_label: str
    rule: str
    conditions: list[ConditionResult]
    passengers: float | None
    passengers_previous: float | None
    passenger_growth: float | None  # percent
    seats: float | None
    seats_previous: float | None
    seat_growth: float | None  # percent
    s2: SignalResult
    s3: SignalResult
    constraints: list[ConstraintRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)
    evidence_status: Literal["available", "missing"]
    monthly_delay_causes: list[MonthlyDelayCauses]


class ResultBase(Model):
    title: str
    completeness: Literal["complete", "partial"] = "complete"
    units: dict[str, str] = Field(default_factory=dict)
    parameters: dict[str, str | int | float | bool | list[str] | None] = Field(default_factory=dict)
    defaults_applied: list[str] = Field(default_factory=list)  # parameter names set by default
    periods: list[PeriodInfo] = Field(default_factory=list)
    rules_version: str = RULES_VERSION
    reference_version: str | None = None  # signature of the reference files used
    data_version_id: str | None = None
    sources: list[SourceRef] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class RankingResult(ResultBase):
    analysis_type: Literal["ranking"] = "ranking"
    data: RankingData


class ComparisonResult(ResultBase):
    analysis_type: Literal["comparison"] = "comparison"
    data: ComparisonData


class LongHaulResult(ResultBase):
    analysis_type: Literal["long_haul_share"] = "long_haul_share"
    data: LongHaulData


class UnmetDemandResult(ResultBase):
    analysis_type: Literal["unmet_demand"] = "unmet_demand"
    data: UnmetDemandData


AnalysisResult = Annotated[
    RankingResult | ComparisonResult | LongHaulResult | UnmetDemandResult,
    Field(discriminator="analysis_type"),
]
