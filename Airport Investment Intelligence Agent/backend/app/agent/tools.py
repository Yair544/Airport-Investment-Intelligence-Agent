"""The five tools the model may call: schemas, argument validation and dispatch.

Only names registered here can run. Arguments are validated with Pydantic and airport ids are
checked against the reference catalog before any analysis function is called.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from app.analysis.models import (
    AnalysisResult,
    ComparisonResult,
    LongHaulResult,
    RankingResult,
    ResolutionOutcome,
    UnmetDemandResult,
)
from app.analysis.service import AnalysisInputError, AnalysisService, NoDataError
from app.data.service import DataUnavailableError, ReferenceCatalog

logger = logging.getLogger("app.agent.tools")

AirportId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
Year = Annotated[int, Field(ge=1990, le=2100)]


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResolveAirportsArgs(_Args):
    queries: list[Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]] = (
        Field(min_length=1, max_length=10)
    )


class RankAirportsArgs(_Args):
    region_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)] | None
    airport_ids: Annotated[list[AirportId], Field(min_length=1, max_length=60)] | None
    year: Year | None
    horizon_years: Annotated[int, Field(ge=1, le=20)] | None

    @model_validator(mode="after")
    def _one_selection(self) -> RankAirportsArgs:
        if (self.region_id is None) == (self.airport_ids is None):
            raise ValueError("provide exactly one of region_id or airport_ids")
        return self


class CompareAirportsArgs(_Args):
    airport_ids: list[AirportId] = Field(min_length=2, max_length=6)
    year: Year | None


class LongHaulArgs(_Args):
    airport_id: AirportId
    year: Year | None
    threshold_miles: Annotated[float, Field(gt=0, le=12_000)] | None


class UnmetDemandArgs(_Args):
    airport_id: AirportId
    year: Year | None


def _function(name: str, description: str, properties: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "name": name,
        "description": description,
        "strict": True,
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        },
    }


_YEAR = {
    "type": ["integer", "null"],
    "description": "Calendar year of historical data; null = latest full year available.",
}

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    _function(
        "resolve_airports",
        "Map airport codes, names, aliases (English or Hebrew), cities, US states or regions "
        "(e.g. 'New England') to canonical airport ids. Each query returns matched, ambiguous "
        "(with candidates to ask the user about) or not_found.",
        {
            "queries": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Each airport, city, state or region mention as written (1-10).",
            }
        },
    ),
    _function(
        "rank_airports",
        "Deterministic terminal-expansion screening for the airports of a region or state, or for a "
        "hand-picked set: forecast passenger increase (TAF), signals S1-S3 against national "
        "references, documented constraints, group and deciding rule, in final display order. "
        "Give exactly one of region_id or airport_ids.",
        {
            "region_id": {
                "type": ["string", "null"],
                "description": "Region or state id from resolve_airports (e.g. 'new_england', "
                "'state_ca'); the server ranks every airport in it. Use this for regions and states.",
            },
            "airport_ids": {
                "type": ["array", "null"],
                "items": {"type": "string"},
                "description": "Explicit airport ids for a hand-picked set (up to 60); "
                "null when region_id is given.",
            },
            "year": _YEAR,
            "horizon_years": {
                "type": ["integer", "null"],
                "description": "Forecast horizon in years after the TAF base year; null = 5.",
            },
        },
    ),
    _function(
        "compare_airports",
        "Side-by-side On-Time congestion metrics for 2-6 airports in one calendar year "
        "(volume, delay and cancellation rates, taxi times, delay causes). No combined score.",
        {
            "airport_ids": {"type": "array", "items": {"type": "string"}},
            "year": _YEAR,
        },
    ),
    _function(
        "calculate_long_haul_share",
        "Share of departures performed from one airport above a distance threshold in statute "
        "miles, split by aircraft configuration, with the distance-group distribution.",
        {
            "airport_id": {"type": "string"},
            "year": _YEAR,
            "threshold_miles": {
                "type": ["number", "null"],
                "description": "Statute miles; null = 3,452 (about 3,000 nautical miles).",
            },
        },
    ),
    _function(
        "assess_unmet_demand",
        "Deterministic signs of unmet demand at one airport (constrained, weak or undetermined) "
        "with supporting metrics, monthly delay causes and documented constraints. "
        "It never quantifies missing flights.",
        {"airport_id": {"type": "string"}, "year": _YEAR},
    ),
]

ToolStatus = Literal["ok", "no_data", "invalid", "error"]


@dataclass(frozen=True)
class _CallContext:
    data_version_id: str | None  # data version pinned by the previous analysis, if any
    context_airport_ids: list[str]  # airports already in the conversation, to settle ambiguity


@dataclass
class ToolOutcome:
    tool: str
    status: ToolStatus
    payload: dict[str, Any]  # compact JSON returned to the model
    arguments: dict[str, Any] | None = None
    result: AnalysisResult | None = None
    resolutions: list[ResolutionOutcome] = field(default_factory=list)
    limitation: str | None = None


class ToolExecutor:
    """Validates and dispatches tool calls. Holds no per-request state."""

    def __init__(
        self, analysis: AnalysisService, reference: ReferenceCatalog, max_output_chars: int = 24_000
    ) -> None:
        self._analysis = analysis
        self._reference = reference
        self._max_output_chars = max_output_chars
        self._handlers: dict[str, tuple[type[_Args], Callable[[Any, _CallContext], ToolOutcome]]] = {
            "resolve_airports": (ResolveAirportsArgs, self._resolve),
            "rank_airports": (RankAirportsArgs, self._rank),
            "compare_airports": (CompareAirportsArgs, self._compare),
            "calculate_long_haul_share": (LongHaulArgs, self._long_haul),
            "assess_unmet_demand": (UnmetDemandArgs, self._unmet),
        }

    @property
    def tool_names(self) -> list[str]:
        return list(self._handlers)

    def execute(
        self,
        name: str,
        raw_arguments: str,
        *,
        context_airport_ids: list[str] | None = None,
        data_version_id: str | None = None,
    ) -> ToolOutcome:
        handler = self._handlers.get(name)
        if handler is None:
            return _invalid(
                name, "unknown_tool", f"Unknown tool '{name}'. Use one of: {', '.join(self._handlers)}."
            )
        model, run = handler
        try:
            arguments = model.model_validate(json.loads(raw_arguments or "{}"))
        except json.JSONDecodeError:
            return _invalid(name, "invalid_arguments", "Arguments must be a JSON object.")
        except ValidationError as exc:
            problems = "; ".join(
                f"{'.'.join(str(p) for p in error['loc'])}: {error['msg']}" for error in exc.errors()[:5]
            )
            return _invalid(name, "invalid_arguments", f"Invalid arguments: {problems}")
        unknown = [
            airport_id
            for airport_id in _airport_ids(arguments)
            if self._reference.airport(airport_id) is None
        ]
        if unknown:
            return _invalid(
                name,
                "unknown_airport",
                f"Unknown airport ids: {', '.join(unknown)}. Call resolve_airports to get valid ids.",
            )
        try:
            outcome = run(arguments, _CallContext(data_version_id, context_airport_ids or []))
        except AnalysisInputError as exc:
            outcome = _invalid(name, exc.code, str(exc), getattr(exc, "details", None))
        except NoDataError as exc:
            outcome = ToolOutcome(
                tool=name,
                status="no_data",
                payload={
                    "status": "no_data",
                    "error": _error(exc.code, str(exc), getattr(exc, "details", None)),
                },
                limitation=str(exc),
            )
        except DataUnavailableError as exc:
            outcome = ToolOutcome(
                tool=name,
                status="no_data",
                payload={"status": "no_data", "error": _error("no_data_version", str(exc), None)},
                limitation="No aviation data version is currently available.",
            )
        except Exception:
            logger.exception("tool_failed", extra={"tool": name})
            outcome = ToolOutcome(
                tool=name,
                status="error",
                payload={
                    "status": "error",
                    "error": _error("internal_error", "The analysis failed unexpectedly.", None),
                },
            )
        outcome.arguments = arguments.model_dump(mode="json")
        return outcome

    # -- handlers -----------------------------------------------------------------------------

    def _resolve(self, args: ResolveAirportsArgs, call: _CallContext) -> ToolOutcome:
        outcomes = [self._reference.resolve(query, call.context_airport_ids) for query in args.queries]
        payload = {"status": "ok", "results": [_resolution_view(outcome) for outcome in outcomes]}
        return ToolOutcome(tool="resolve_airports", status="ok", payload=payload, resolutions=outcomes)

    def _rank(self, args: RankAirportsArgs, call: _CallContext) -> ToolOutcome:
        # Regions are expanded here from the reference catalog, so the ranked set and its label
        # always match: the model cannot drop airports from a region or mislabel a selection.
        if args.region_id is not None:
            region = self._reference.region(args.region_id)
            if region is None:
                return _invalid(
                    "rank_airports",
                    "unknown_region",
                    f"Unknown region_id '{args.region_id}'. Call resolve_airports to get a region_id.",
                )
            airport_ids = [
                airport.airport_id for airport in self._reference.airports_in_states(region.states)
            ]
            region_label: str | None = region.name
        else:
            airport_ids = _unique(args.airport_ids or [])
            region_label = None
        result = self._analysis.rank_airports(
            airport_ids,
            region_label=region_label,
            year=args.year,
            horizon_years=args.horizon_years,
            data_version_id=call.data_version_id,
        )
        return self._ok("rank_airports", result)

    def _compare(self, args: CompareAirportsArgs, call: _CallContext) -> ToolOutcome:
        result = self._analysis.compare_airports(
            _unique(args.airport_ids), year=args.year, data_version_id=call.data_version_id
        )
        return self._ok("compare_airports", result)

    def _long_haul(self, args: LongHaulArgs, call: _CallContext) -> ToolOutcome:
        result = self._analysis.long_haul_share(
            args.airport_id,
            year=args.year,
            threshold_miles=args.threshold_miles,
            data_version_id=call.data_version_id,
        )
        return self._ok("calculate_long_haul_share", result)

    def _unmet(self, args: UnmetDemandArgs, call: _CallContext) -> ToolOutcome:
        result = self._analysis.assess_unmet_demand(
            args.airport_id, year=args.year, data_version_id=call.data_version_id
        )
        return self._ok("assess_unmet_demand", result)

    def _ok(self, tool: str, result: AnalysisResult) -> ToolOutcome:
        payload = {"status": "ok", "result": compact_result(result, self._max_output_chars)}
        return ToolOutcome(tool=tool, status="ok", payload=payload, result=result)


# -- helpers ----------------------------------------------------------------------------------


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _airport_ids(arguments: _Args) -> list[str]:
    if isinstance(arguments, RankAirportsArgs):
        return list(arguments.airport_ids or [])
    if isinstance(arguments, CompareAirportsArgs):
        return list(arguments.airport_ids)
    if isinstance(arguments, LongHaulArgs | UnmetDemandArgs):
        return [arguments.airport_id]
    return []


def _error(code: str, message: str, details: dict[str, Any] | None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if details:
        error["details"] = details
    return error


def _invalid(tool: str, code: str, message: str, details: dict[str, Any] | None = None) -> ToolOutcome:
    return ToolOutcome(
        tool=tool, status="invalid", payload={"status": "invalid", "error": _error(code, message, details)}
    )


def _resolution_view(outcome: ResolutionOutcome) -> dict[str, Any]:
    def airport(item: Any) -> dict[str, Any]:
        return {
            "airport_id": item.airport_id,
            "code": item.code,
            "name": item.name,
            "city": item.city,
            "state": item.state,
            "matched_on": item.matched_on,
        }

    view: dict[str, Any] = {"query": outcome.query, "status": outcome.status, "kind": outcome.kind}
    if outcome.region is not None:
        # Areas can hold hundreds of airports: send the id and a sample, not the full list.
        view["region"] = outcome.region.model_dump()
        view["airport_count"] = len(outcome.airports)
        view["sample_codes"] = [item.code for item in outcome.airports[:12]]
        view["hint"] = "Pass region_id to rank_airports to screen every airport in this area."
    elif outcome.airports:
        view["airports"] = [airport(item) for item in outcome.airports]
    if outcome.candidates:
        view["candidates"] = [airport(item) for item in outcome.candidates]
    if outcome.message:
        view["message"] = outcome.message
    return view


def _rounded(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, dict):
        return {key: _rounded(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_rounded(item) for item in value]
    return value


def _signals(signals: list[Any]) -> dict[str, Any]:
    return {
        signal.signal: {
            "status": signal.status,
            "value": signal.value,
            "national_reference": signal.reference,
            "unit": signal.unit,
            **({"note": signal.note} if signal.note else {}),
        }
        for signal in signals
    }


def _size(view: dict[str, Any]) -> int:
    return len(json.dumps(view, ensure_ascii=False))


def compact_result(result: AnalysisResult, max_chars: int = 24_000) -> dict[str, Any]:
    """Model-facing view of a result, bounded by `max_chars`.

    Optional detail is dropped first; large rankings keep full detail for the top rows and list
    the remaining rows briefly, shrinking the detailed part until the view fits.
    """
    compact: dict[str, Any] = {}
    for detail_rows in (25, 12, 6, 3):
        compact, optional_keys = _view(result, detail_rows)
        for key in optional_keys:
            if _size(compact) <= max_chars:
                break
            compact.pop(key, None)
            compact["truncated"] = True
        if _size(compact) <= max_chars or not isinstance(result, RankingResult):
            break
    if _size(compact) > max_chars:
        compact["limitations"] = compact.get("limitations", [])[:3]
        compact.pop("other_rows", None)
        compact["truncated"] = True
    return compact


def _view(result: AnalysisResult, detail_rows: int) -> tuple[dict[str, Any], list[str]]:
    view: dict[str, Any] = {
        "analysis_type": result.analysis_type,
        "title": result.title,
        "completeness": result.completeness,
        "periods": [
            f"{period.label} ({period.nature}{', ' + period.role if period.role else ''})"
            for period in result.periods
        ],
        "parameters": result.parameters,
        "defaults_applied": result.defaults_applied,
        "data_version_id": result.data_version_id,
        "limitations": result.limitations,
    }
    optional_keys: list[str] = []
    if isinstance(result, RankingResult):
        data = result.data
        view["region"] = data.region_label
        view["analysis_year"] = data.analysis_year
        view["taf"] = {
            "edition": data.taf_edition,
            "base_fy": data.base_year,
            "horizon_fy": data.horizon_year,
        }
        view["national_references"] = {
            key: {"value": ref.value, "period": ref.period_key}
            for key, ref in data.national_references.items()
        }
        view["rows"] = [
            {
                "rank": row.rank,
                "code": row.airport.code,
                "name": row.airport.name,
                "state": row.airport.state,
                "group": row.group_label,
                "rule": row.rule,
                "delta_enplanements": row.delta_enplanements,
                "enplanements_base_fy": row.base_year_enplanements,
                "enplanements_horizon_fy": row.horizon_enplanements,
                "cagr_pct_per_year": row.cagr_5,
                "delta_commercial_operations": row.delta_commercial_operations,
                "signals": _signals(row.signals),
                "constraints": [f"{c.constraint_type}: {c.description}" for c in row.constraints],
                "further_review": row.further_review,
                "notes": row.notes,
            }
            for row in data.rows[:detail_rows]
        ]
        if len(data.rows) > detail_rows:
            view["other_rows"] = [
                f"{row.rank}. {row.airport.code} - {row.group_label}" for row in data.rows[detail_rows:]
            ]
        counts: dict[str, int] = {}
        for row in data.rows:
            counts[row.group_label] = counts.get(row.group_label, 0) + 1
        view["group_counts"] = counts
        view["excluded"] = [{"code": item.airport.code, "reason": item.reason} for item in data.excluded]
        optional_keys = ["excluded"]
    elif isinstance(result, ComparisonResult):
        view["year"] = result.data.year
        view["airports"] = [
            {
                "code": item.airport.code,
                "name": item.airport.name,
                "departures_performed_per_day": item.departures_performed_per_day,
                "departure_delay_rate_pct": item.departure_delay_rate,
                "cancellation_rate_pct": item.cancellation_rate,
                "median_taxi_out_min": item.median_taxi_out,
                "taxi_out_p10_min": item.taxi_out_p10,
                "taxi_out_excess_min": item.taxi_out_excess,
                "taxi_out_sample": item.taxi_out_sample,
                "median_taxi_in_min": item.median_taxi_in,
                "delay_cause_share_pct": {cause.cause: cause.share for cause in item.delay_causes},
                "constraints": [f"{c.constraint_type}: {c.description}" for c in item.constraints],
                "notes": item.notes,
            }
            for item in result.data.airports
        ]
    elif isinstance(result, LongHaulResult):
        data_lh = result.data
        view.update(
            {
                "airport": f"{data_lh.airport.code} ({data_lh.airport.name})",
                "year": data_lh.year,
                "threshold_miles": data_lh.threshold_miles,
                "threshold_nautical_miles": data_lh.threshold_nautical_miles,
                "long_haul_share_pct": data_lh.long_haul_share,
                "long_haul_departures": data_lh.long_haul_departures,
                "valid_departures": data_lh.valid_departures,
                "excluded_departures": data_lh.excluded_departures,
                "excluded_share_pct": data_lh.excluded_share,
                "by_aircraft_config": [
                    {
                        "config": c.label,
                        "share_pct": c.share,
                        "long_haul": c.long_haul_departures,
                        "valid": c.valid_departures,
                    }
                    for c in data_lh.by_aircraft_config
                ],
                "distance_groups": [
                    {"group": g.label, "share_pct": g.share} for g in data_lh.distance_groups
                ],
                "top_long_haul_destinations": [
                    {
                        "dest": d.dest_code,
                        "name": d.dest_name,
                        "miles": d.distance_miles,
                        "departures": d.departures,
                    }
                    for d in data_lh.top_long_haul_destinations
                ],
            }
        )
        optional_keys = ["distance_groups", "top_long_haul_destinations"]
    elif isinstance(result, UnmetDemandResult):
        data_ud = result.data
        view.update(
            {
                "airport": f"{data_ud.airport.code} ({data_ud.airport.name})",
                "analysis_year": data_ud.analysis_year,
                "comparison_year": data_ud.comparison_year,
                "classification": data_ud.classification_label,
                "rule": data_ud.rule,
                "conditions": [
                    {
                        "id": c.condition_id,
                        "status": c.status,
                        "description": c.description,
                        "detail": c.detail,
                    }
                    for c in data_ud.conditions
                ],
                "passenger_growth_pct": data_ud.passenger_growth,
                "seat_growth_pct": data_ud.seat_growth,
                "s2": _signals([data_ud.s2])["S2"],
                "s3": _signals([data_ud.s3])["S3"],
                "constraints": [f"{c.constraint_type}: {c.description}" for c in data_ud.constraints],
                "evidence": [f"{e.topic}/{e.scenario}: {e.statement}" for e in data_ud.evidence],
                "monthly_delay_cause_share_pct": [
                    {"month": f"{m.year}-{m.month:02d}", **{c.cause: c.share for c in m.causes}}
                    for m in data_ud.monthly_delay_causes
                ],
            }
        )
        optional_keys = ["evidence", "monthly_delay_cause_share_pct"]
    return _rounded(view), optional_keys
