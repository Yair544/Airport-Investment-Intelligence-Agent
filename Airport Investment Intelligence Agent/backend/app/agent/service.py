"""One conversation turn: bounded model/tool loop and deterministic response assembly.

The model chooses tools and writes the explanation. Results, sources and limitations in the
response come only from validated tool outputs, never from model text.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.agent.client import ModelClient, ModelError
from app.agent.prompts import build_instructions
from app.agent.tools import TOOL_DEFINITIONS, ToolExecutor, ToolOutcome, compact_result
from app.analysis.models import (
    RULES_VERSION,
    AnalysisResult,
    ComparisonResult,
    LongHaulResult,
    RankingResult,
    SourceRef,
    UnmetDemandResult,
)
from app.config import Settings
from app.data.service import DataService, DataUnavailableError

logger = logging.getLogger("app.agent")

MODEL_FALLBACK_LIMITATION = (
    "The language model explanation was unavailable; the summary was generated from the validated results."
)


class AgentError(Exception):
    """A turn that produced no usable answer. Maps to an HTTP status in the API layer."""

    def __init__(self, code: str, status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.public_message = message


@dataclass(frozen=True)
class TurnInput:
    owner_id: str
    conversation_id: str
    request_id: str
    attempt_id: str
    message: str
    history: list[dict[str, str]]  # oldest first: {"role": "user"|"assistant", "content": ...}
    analysis_context: dict[str, Any] | None
    context_data_version_id: str | None
    deadline: float  # time.monotonic() value


@dataclass
class TurnOutput:
    answer: str
    results: list[AnalysisResult]
    sources: list[SourceRef]
    limitations: list[str]
    context: dict[str, Any] | None  # new analysis context, or None to keep the previous one
    data_version_id: str | None
    rules_version: str
    model_calls: int = 0
    tool_calls: int = 0
    used_fallback: bool = False


@dataclass
class _TurnState:
    outcomes: list[ToolOutcome] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    model_calls: int = 0
    tool_calls: int = 0


class AgentService:
    def __init__(
        self,
        client: ModelClient,
        executor: ToolExecutor,
        data: DataService,
        settings: Settings,
    ) -> None:
        self._client = client
        self._executor = executor
        self._data = data
        self._settings = settings

    def run_turn(self, turn: TurnInput) -> TurnOutput:
        state = _TurnState()
        instructions = build_instructions(
            today=datetime.now(UTC).date(),
            data_facts=self._data_facts(),
            analysis_context=turn.analysis_context,
        )
        input_items: list[dict[str, Any]] = [
            {"role": item["role"], "content": item["content"]} for item in turn.history
        ]
        input_items.append({"role": "user", "content": turn.message})
        context_ids = _context_airport_ids(turn.analysis_context)
        # A follow-up that repeats the previous analysis keeps that analysis's data version so the
        # numbers stay comparable; any other question uses the active version.
        pinned_version = turn.context_data_version_id
        context_tool = (turn.analysis_context or {}).get("tool")
        active_version = self._active_version_id()

        answer: str | None = None
        for _ in range(self._settings.agent_max_model_rounds):
            try:
                model_turn = self._client.respond(
                    instructions=instructions,
                    input_items=input_items,
                    tools=TOOL_DEFINITIONS,
                    deadline=turn.deadline,
                )
            except ModelError as exc:
                logger.warning("model_turn_failed", extra={"kind": exc.kind, "request_id": turn.request_id})
                if self._analysis_results(state):
                    break
                raise _agent_error(exc) from exc
            state.model_calls += 1
            input_items.extend(model_turn.output_items)
            if not model_turn.tool_calls:
                answer = model_turn.text
                break
            for call in model_turn.tool_calls:
                if state.tool_calls >= self._settings.agent_max_tool_calls:
                    payload = {
                        "status": "error",
                        "error": {
                            "code": "tool_call_limit",
                            "message": "Tool call limit reached for this turn.",
                        },
                    }
                    _add_once(
                        state.limitations, "The analysis stopped at the tool-call limit for one request."
                    )
                else:
                    state.tool_calls += 1
                    version = pinned_version if pinned_version and call.name == context_tool else None
                    outcome = self._executor.execute(
                        call.name,
                        call.arguments,
                        context_airport_ids=context_ids,
                        data_version_id=version,
                    )
                    state.outcomes.append(outcome)
                    if (
                        version is not None
                        and active_version is not None
                        and version != active_version
                        and outcome.result is not None
                        and outcome.result.data_version_id == version
                    ):
                        _add_once(
                            state.limitations,
                            f"This follow-up reuses data version {version} from earlier in the "
                            f"conversation so the numbers stay comparable; a newer version "
                            f"({active_version}) is active. Ask it as a new question to use the latest data.",
                        )
                    for resolution in outcome.resolutions:
                        context_ids.extend(
                            a.airport_id for a in resolution.airports if a.airport_id not in context_ids
                        )
                    if outcome.limitation:
                        _add_once(state.limitations, outcome.limitation)
                    payload = outcome.payload
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call.call_id,
                        "output": json.dumps(payload, ensure_ascii=False),
                    }
                )
        else:
            _add_once(state.limitations, "The analysis stopped at the step limit for one request.")

        results = self._analysis_results(state)
        used_fallback = False
        if not answer:
            if results:
                answer = fallback_answer(results)
                used_fallback = True
                _add_once(state.limitations, MODEL_FALLBACK_LIMITATION)
            else:
                answer = (
                    "I could not complete this analysis within the allowed steps, so I have no validated "
                    "result to report. Please rephrase or narrow the question."
                )
        return self._assemble(turn, state, results, answer, used_fallback)

    # -- assembly -------------------------------------------------------------------------------

    def _assemble(
        self,
        turn: TurnInput,
        state: _TurnState,
        results: list[AnalysisResult],
        answer: str,
        used_fallback: bool,
    ) -> TurnOutput:
        sources: list[SourceRef] = []
        seen_sources: set[tuple[Any, ...]] = set()
        limitations: list[str] = []
        for result in results:
            for source in result.sources:
                key = (source.source_id, source.name, source.url, source.period, source.dataset_version)
                if key not in seen_sources:
                    seen_sources.add(key)
                    sources.append(source)
            for limitation in result.limitations:
                _add_once(limitations, limitation)
        for limitation in state.limitations:
            _add_once(limitations, limitation)
        context = build_context(results, state.outcomes) if results else None
        data_version_id = results[-1].data_version_id if results else turn.context_data_version_id
        logger.info(
            "agent_turn_completed",
            extra={
                "request_id": turn.request_id,
                "attempt_id": turn.attempt_id,
                "model_calls": state.model_calls,
                "tool_calls": state.tool_calls,
                "results": len(results),
                "data_version_id": data_version_id,
                "used_fallback": used_fallback,
            },
        )
        return TurnOutput(
            answer=answer,
            results=results,
            sources=sources,
            limitations=limitations,
            context=context,
            data_version_id=data_version_id,
            rules_version=results[-1].rules_version if results else RULES_VERSION,
            model_calls=state.model_calls,
            tool_calls=state.tool_calls,
            used_fallback=used_fallback,
        )

    @staticmethod
    def _analysis_results(state: _TurnState) -> list[AnalysisResult]:
        """Successful analysis results in execution order, without duplicates of the same call."""
        results: list[AnalysisResult] = []
        seen: set[str] = set()
        for outcome in state.outcomes:
            if outcome.status != "ok" or outcome.result is None:
                continue
            key = json.dumps([outcome.tool, outcome.arguments], sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            results.append(outcome.result)
        return results

    def _active_version_id(self) -> str | None:
        try:
            return self._data.active_version_id()
        except Exception:
            logger.warning("active_version_lookup_failed")
            return None

    def _data_facts(self) -> dict[str, Any] | None:
        try:
            repository = self._data.repository()
        except DataUnavailableError:
            return None
        coverage = repository.coverage()
        return {
            "active_data_version": coverage.version_id,
            "latest_full_calendar_year": coverage.latest_full_year(),
            "t100_full_years": coverage.full_years("bts_t100_segment"),
            "ontime_full_years": coverage.full_years("bts_ontime"),
            "taf_edition": coverage.taf_edition,
            "taf_base_fiscal_year": coverage.taf_base_year,
            "taf_last_forecast_fiscal_year": coverage.taf_last_year,
            "defaults": {
                "year": "latest full calendar year",
                "forecast_horizon_years": self._settings.forecast_horizon_years,
                "long_haul_threshold_miles": self._settings.long_haul_threshold_miles,
                "primary_airport_min_enplanements": self._settings.primary_airport_min_enplanements,
            },
        }


# -- module helpers ---------------------------------------------------------------------------


def _add_once(items: list[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _agent_error(exc: ModelError) -> AgentError:
    if exc.kind == "not_configured":
        return AgentError("model_not_configured", 503, "The language model is not configured on the server.")
    if exc.kind == "quota_exhausted":
        return AgentError(
            "model_quota_exhausted",
            503,
            "The language model account has no remaining credits. Ask an administrator to add credits.",
        )
    if exc.kind == "incomplete":
        return AgentError(
            "model_incomplete", 503, "The language model could not finish its answer. Please retry."
        )
    if exc.kind == "timeout":
        return AgentError("model_timeout", 504, "The language model did not answer in time. Please retry.")
    return AgentError(
        "model_unavailable", 503, "The language model is temporarily unavailable. Please retry."
    )


def _context_airport_ids(context: dict[str, Any] | None) -> list[str]:
    if not context:
        return []
    return [airport["airport_id"] for airport in context.get("airports", []) if "airport_id" in airport]


def build_context(results: list[AnalysisResult], outcomes: list[ToolOutcome]) -> dict[str, Any]:
    """Structured context for follow-ups: selected airports, period, parameters and last result."""
    last = results[-1]
    airports: list[dict[str, str]] = []
    if isinstance(last, RankingResult):
        airports = [
            {"airport_id": row.airport.airport_id, "code": row.airport.code, "name": row.airport.name}
            for row in last.data.rows
        ]
    elif isinstance(last, ComparisonResult):
        airports = [
            {"airport_id": item.airport.airport_id, "code": item.airport.code, "name": item.airport.name}
            for item in last.data.airports
        ]
    elif isinstance(last, LongHaulResult | UnmetDemandResult):
        label = last.data.airport
        airports = [{"airport_id": label.airport_id, "code": label.code, "name": label.name}]
    tool_by_type = {
        "ranking": "rank_airports",
        "comparison": "compare_airports",
        "long_haul_share": "calculate_long_haul_share",
        "unmet_demand": "assess_unmet_demand",
    }
    arguments = next(
        (o.arguments for o in reversed(outcomes) if o.result is last and o.arguments is not None), None
    )
    summary = compact_result(last, max_chars=3_000)
    return {
        "analysis_type": last.analysis_type,
        "tool": tool_by_type[last.analysis_type],
        "tool_arguments": arguments,
        "airports": airports,
        "parameters": last.parameters,
        "periods": [period.label for period in last.periods],
        "data_version_id": last.data_version_id,
        "analyses_in_last_turn": [
            {"tool": o.tool, "arguments": o.arguments} for o in outcomes if o.status == "ok" and o.result
        ],
        "result_summary": summary,
    }


def _fmt(value: float | None, suffix: str = "", digits: int = 1) -> str:
    return "not available" if value is None else f"{value:,.{digits}f}{suffix}"


def fallback_answer(results: list[AnalysisResult]) -> str:
    """Deterministic summary used when the model explanation is unavailable."""
    lines = ["The validated results are shown below. Summary:"]
    for result in results:
        if isinstance(result, RankingResult):
            data = result.data
            groups: dict[str, list[str]] = {}
            for row in data.rows:
                groups.setdefault(row.group_label, []).append(row.airport.code)
            grouped = "; ".join(f"{label}: {', '.join(codes)}" for label, codes in groups.items())
            lines.append(
                f"- {result.title}: {len(data.rows)} airports screened for CY{data.analysis_year} "
                f"signals and TAF FY{data.base_year}-FY{data.horizon_year}. {grouped}."
            )
        elif isinstance(result, ComparisonResult):
            parts = [
                f"{item.airport.code}: {_fmt(item.departures_performed_per_day)} departures/day, "
                f"departure delay rate {_fmt(item.departure_delay_rate, '%')}, "
                f"taxi-out excess {_fmt(item.taxi_out_excess, ' min')}"
                for item in result.data.airports
            ]
            lines.append(f"- {result.title} ({result.data.period_label}): " + "; ".join(parts) + ".")
        elif isinstance(result, LongHaulResult):
            lh = result.data
            lines.append(
                f"- {_fmt(lh.long_haul_share, '%')} of departures performed from {lh.airport.code} in "
                f"{lh.period_label} flew more than {lh.threshold_miles:,.0f} statute miles "
                f"(about {lh.threshold_nautical_miles:,.0f} nautical miles)."
            )
        elif isinstance(result, UnmetDemandResult):
            ud = result.data
            lines.append(f"- {ud.airport.code} (CY{ud.analysis_year}): {ud.classification_label}. {ud.rule}")
    return "\n".join(lines)
