"""Agent loop with a scripted model: tool validation, limits, fallback and response assembly."""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

import pytest

from app.agent.client import ModelError, ModelTurn, ToolCall
from app.agent.service import MODEL_FALLBACK_LIMITATION, AgentError, AgentService, TurnInput
from app.agent.tools import (
    TOOL_DEFINITIONS,
    CompareAirportsArgs,
    LongHaulArgs,
    RankAirportsArgs,
    ToolExecutor,
    compact_result,
)
from app.analysis.models import (
    AirportLabel,
    AirportRef,
    LongHaulData,
    LongHaulResult,
    NationalReference,
    RankingData,
    RankingResult,
    RankingRow,
    ResolutionOutcome,
    ResolvedAirport,
    ResolvedRegion,
    SignalResult,
    SourceRef,
)
from app.analysis.service import NoDataError
from app.config import Settings
from app.data.service import DataUnavailableError

ANC = AirportRef(
    airport_id="apt_anc",
    faa_locid="ANC",
    iata_code="ANC",
    bts_airport_id=10299,
    name="Ted Stevens Anchorage International",
    city="Anchorage",
    state="AK",
)


class FakeReference:
    reference_version = "ref-test"

    def airport(self, airport_id: str) -> AirportRef | None:
        return ANC if airport_id == "apt_anc" else None

    def airports(self) -> list[AirportRef]:
        return [ANC]

    def resolve(self, query: str, context_airport_ids: Sequence[str] = ()) -> ResolutionOutcome:
        if query.lower() in {"anchorage", "anc"}:
            airport = ResolvedAirport(
                airport_id="apt_anc",
                code="ANC",
                faa_locid="ANC",
                name=ANC.name,
                city="Anchorage",
                state="AK",
                matched_on="city",
            )
            return ResolutionOutcome(query=query, status="matched", kind="airport", airports=[airport])
        return ResolutionOutcome(query=query, status="not_found", message="No match")

    def region(self, region_id: str) -> ResolvedRegion | None:
        if region_id == "state_ak":
            return ResolvedRegion(region_id="state_ak", name="Alaska", states=["AK"])
        return None

    def airports_in_states(self, states: Sequence[str]) -> list[AirportRef]:
        return [ANC] if "AK" in states else []

    def constraints_for(self, airport_id: str) -> list[Any]:
        return []

    def evidence_for(self, airport_id: str) -> list[Any]:
        return []


def long_haul_result(threshold: float = 3452.0, version: str = "v1") -> LongHaulResult:
    return LongHaulResult(
        title="Long-haul share of departures from ANC",
        data=LongHaulData(
            airport=AirportLabel(
                airport_id="apt_anc", code="ANC", name=ANC.name, city="Anchorage", state="AK"
            ),
            year=2025,
            period_label="CY2025",
            threshold_miles=threshold,
            threshold_nautical_miles=threshold / 1.150779448,
            total_departures=87988,
            valid_departures=87746,
            long_haul_departures=15089,
            long_haul_share=17.196,
            excluded_departures=242,
            excluded_share=0.275,
            by_aircraft_config=[],
            distance_groups=[],
        ),
        data_version_id=version,
        sources=[
            SourceRef(source_id="bts_t100_segment", name="BTS T-100 Segment (All Carriers)", url="https://x")
        ],
        limitations=["The threshold is a system choice."],
    )


class FakeAnalysis:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.no_data = False

    def long_haul_share(self, airport_id: str, **kwargs: Any) -> LongHaulResult:
        self.calls.append({"airport_id": airport_id, **kwargs})
        if self.no_data:
            raise NoDataError(
                "period_unavailable", "CY2019 is not covered.", {"available_years": [2024, 2025]}
            )
        return long_haul_result(
            kwargs.get("threshold_miles") or 3452.0, version=kwargs.get("data_version_id") or "v1"
        )

    def rank_airports(self, airport_ids: Sequence[str], **kwargs: Any) -> Any:
        self.calls.append({"airport_ids": list(airport_ids), **kwargs})
        raise NoDataError("period_unavailable", "Not needed for this test.", {})


class NoData:
    active: str | None = None

    def repository(self, version_id: str | None = None) -> Any:
        raise DataUnavailableError("no data")

    def active_version_id(self) -> str | None:
        return self.active


class ScriptedModel:
    def __init__(self, turns: list[ModelTurn | Exception]) -> None:
        self.turns = turns
        self.inputs: list[list[dict[str, Any]]] = []

    def respond(
        self,
        *,
        instructions: str,
        input_items: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        deadline: float,
    ) -> ModelTurn:
        self.inputs.append(list(input_items))
        if not self.turns:
            return ModelTurn(
                text="", tool_calls=[ToolCall("c-x", "resolve_airports", '{"queries": ["anc"]}')]
            )
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return turn


def call(name: str, arguments: dict[str, Any], call_id: str = "c1") -> ModelTurn:
    return ModelTurn(text="", tool_calls=[ToolCall(call_id, name, json.dumps(arguments))])


def final(text: str) -> ModelTurn:
    return ModelTurn(text=text, tool_calls=[])


def make_agent(
    model: ScriptedModel, analysis: FakeAnalysis, settings: Settings, data: NoData | None = None
) -> AgentService:
    executor = ToolExecutor(analysis, FakeReference(), 24_000)  # type: ignore[arg-type]
    return AgentService(model, executor, data or NoData(), settings)  # type: ignore[arg-type]


def turn(message: str = "What share of ANC departures are long haul?", **overrides: Any) -> TurnInput:
    values: dict[str, Any] = {
        "owner_id": "u1",
        "conversation_id": "c1",
        "request_id": "r1",
        "attempt_id": "a1",
        "message": message,
        "history": [],
        "analysis_context": None,
        "context_data_version_id": None,
        "deadline": time.monotonic() + 60,
    }
    values.update(overrides)
    return TurnInput(**values)


def test_tool_definitions_match_argument_models() -> None:
    by_name = {tool["name"]: tool for tool in TOOL_DEFINITIONS}
    assert set(by_name) == {
        "resolve_airports",
        "rank_airports",
        "compare_airports",
        "calculate_long_haul_share",
        "assess_unmet_demand",
    }
    for name, model in (
        ("rank_airports", RankAirportsArgs),
        ("compare_airports", CompareAirportsArgs),
        ("calculate_long_haul_share", LongHaulArgs),
    ):
        parameters = by_name[name]["parameters"]
        assert set(parameters["properties"]) == set(model.model_fields) == set(parameters["required"])
        assert parameters["additionalProperties"] is False and by_name[name]["strict"] is True


def test_results_sources_and_limitations_come_from_tools(settings: Settings) -> None:
    analysis = FakeAnalysis()
    model = ScriptedModel(
        [
            call("resolve_airports", {"queries": ["Anchorage"]}),
            call(
                "calculate_long_haul_share",
                {"airport_id": "apt_anc", "year": None, "threshold_miles": None},
                "c2",
            ),
            final("About 17.2% of ANC departures were long haul in CY2025."),
        ]
    )
    output = make_agent(model, analysis, settings).run_turn(turn())
    assert output.answer.startswith("About 17.2%")
    assert [r.analysis_type for r in output.results] == ["long_haul_share"]
    assert output.sources[0].source_id == "bts_t100_segment"
    assert output.limitations == ["The threshold is a system choice."]
    assert output.model_calls == 3 and output.tool_calls == 2 and not output.used_fallback
    assert output.context is not None and output.context["airports"][0]["airport_id"] == "apt_anc"
    assert output.data_version_id == "v1"
    # The tool output for each call id was sent back to the model.
    last_input = model.inputs[-1]
    outputs = [item for item in last_input if item.get("type") == "function_call_output"]
    assert [item["call_id"] for item in outputs] == ["c1", "c2"]


def test_invalid_arguments_and_unknown_tools_are_reported_to_the_model(settings: Settings) -> None:
    analysis = FakeAnalysis()
    model = ScriptedModel(
        [
            call("calculate_long_haul_share", {"airport_id": "apt_anc", "year": None, "threshold_miles": -5}),
            call("delete_everything", {}),
            call(
                "calculate_long_haul_share", {"airport_id": "apt_xyz", "year": None, "threshold_miles": None}
            ),
            call(
                "calculate_long_haul_share", {"airport_id": "apt_anc", "year": None, "threshold_miles": 2500}
            ),
            final("Done."),
        ]
    )
    output = make_agent(model, analysis, settings).run_turn(turn())
    statuses = [
        json.loads(item["output"])["status"]
        for item in model.inputs[-1]
        if item.get("type") == "function_call_output"
    ]
    assert statuses == ["invalid", "invalid", "invalid", "ok"]
    assert len(analysis.calls) == 1 and analysis.calls[0]["threshold_miles"] == 2500
    assert len(output.results) == 1


def test_no_data_becomes_limitation_not_failure(settings: Settings) -> None:
    analysis = FakeAnalysis()
    analysis.no_data = True
    model = ScriptedModel(
        [
            call(
                "calculate_long_haul_share", {"airport_id": "apt_anc", "year": 2019, "threshold_miles": None}
            ),
            final("CY2019 is not available."),
        ]
    )
    output = make_agent(model, analysis, settings).run_turn(turn())
    assert output.results == [] and output.context is None
    assert output.limitations == ["CY2019 is not covered."]


def test_model_failure_after_results_uses_deterministic_fallback(settings: Settings) -> None:
    model = ScriptedModel(
        [
            call(
                "calculate_long_haul_share", {"airport_id": "apt_anc", "year": None, "threshold_miles": None}
            ),
            ModelError("unavailable", "down"),
        ]
    )
    output = make_agent(model, FakeAnalysis(), settings).run_turn(turn())
    assert output.used_fallback and "17.2%" in output.answer
    assert MODEL_FALLBACK_LIMITATION in output.limitations
    assert len(output.results) == 1


@pytest.mark.parametrize(
    ("kind", "status"),
    [
        ("not_configured", 503),
        ("unavailable", 503),
        ("quota_exhausted", 503),
        ("incomplete", 503),
        ("timeout", 504),
    ],
)
def test_model_failure_without_results_raises(settings: Settings, kind: str, status: int) -> None:
    model = ScriptedModel([ModelError(kind, "x")])  # type: ignore[arg-type]
    with pytest.raises(AgentError) as error:
        make_agent(model, FakeAnalysis(), settings).run_turn(turn())
    assert error.value.status == status


def test_round_and_tool_limits(settings: Settings) -> None:
    settings.agent_max_model_rounds = 3
    model = ScriptedModel(
        [call("calculate_long_haul_share", {"airport_id": "apt_anc", "year": None, "threshold_miles": None})]
    )
    output = make_agent(model, FakeAnalysis(), settings).run_turn(turn())
    assert output.model_calls == 3
    assert "The analysis stopped at the step limit for one request." in output.limitations
    assert output.used_fallback  # a validated result exists, so it is summarized

    settings.agent_max_tool_calls = 1
    many = ModelTurn(
        text="",
        tool_calls=[
            ToolCall("a", "resolve_airports", '{"queries": ["anc"]}'),
            ToolCall("b", "resolve_airports", '{"queries": ["anc"]}'),
        ],
    )
    output = make_agent(ScriptedModel([many, final("ok")]), FakeAnalysis(), settings).run_turn(turn())
    assert output.tool_calls == 1
    assert "The analysis stopped at the tool-call limit for one request." in output.limitations


def test_pinned_data_version_and_history_reach_the_tools(settings: Settings) -> None:
    analysis = FakeAnalysis()
    model = ScriptedModel(
        [
            call(
                "calculate_long_haul_share", {"airport_id": "apt_anc", "year": None, "threshold_miles": 2500}
            ),
            final("ok"),
        ]
    )
    history = [{"role": "user", "content": "long haul ANC?"}, {"role": "assistant", "content": "17.2%"}]
    data = NoData()
    data.active = "v-new"
    output = make_agent(model, analysis, settings, data).run_turn(
        turn(
            "And with 2,500 miles?",
            history=history,
            analysis_context={"tool": "calculate_long_haul_share", "airports": []},
            context_data_version_id="v-old",
        )
    )
    assert analysis.calls[0]["data_version_id"] == "v-old"
    assert any("reuses data version v-old" in item for item in output.limitations)
    assert model.inputs[0][:2] == history
    assert model.inputs[0][2] == {"role": "user", "content": "And with 2,500 miles?"}


def test_new_kind_of_question_uses_the_active_version(settings: Settings) -> None:
    analysis = FakeAnalysis()
    model = ScriptedModel(
        [
            call(
                "calculate_long_haul_share", {"airport_id": "apt_anc", "year": None, "threshold_miles": None}
            ),
            final("ok"),
        ]
    )
    output = make_agent(model, analysis, settings).run_turn(
        turn(
            analysis_context={"tool": "compare_airports", "airports": []},
            context_data_version_id="v-old",
        )
    )
    assert analysis.calls[0]["data_version_id"] is None
    assert not any("reuses data version" in item for item in output.limitations)


def test_region_rankings_are_expanded_server_side(settings: Settings) -> None:
    analysis = FakeAnalysis()
    model = ScriptedModel(
        [
            call(
                "rank_airports",
                {"region_id": "state_ak", "airport_ids": None, "year": None, "horizon_years": None},
            ),
            call(
                "rank_airports",
                {"region_id": "nowhere", "airport_ids": None, "year": None, "horizon_years": None},
                "c2",
            ),
            call(
                "rank_airports",
                {"region_id": "state_ak", "airport_ids": ["apt_anc"], "year": None, "horizon_years": None},
                "c3",
            ),
            final("ok"),
        ]
    )
    make_agent(model, analysis, settings).run_turn(turn("Rank Alaska"))
    assert analysis.calls == [
        {
            "airport_ids": ["apt_anc"],
            "region_label": "Alaska",
            "year": None,
            "horizon_years": None,
            "data_version_id": None,
        }
    ]
    statuses = [
        json.loads(item["output"])["status"]
        for item in model.inputs[-1]
        if item.get("type") == "function_call_output"
    ]
    assert statuses == ["no_data", "invalid", "invalid"]


def ranking_result(rows: int) -> RankingResult:
    signal = SignalResult(
        signal="S1", name="Demand growth", value=2.5, reference=1.76, status="pass", unit="%/yr"
    )
    airport_rows = [
        RankingRow(
            rank=index + 1,
            airport=AirportLabel(
                airport_id=f"apt_x{index}",
                code=f"X{index:02d}",
                name=f"Airport {index}",
                city="City",
                state="TX",
            ),
            group="watch",
            group_label="Watch",
            rule_id="R3",
            rule="R3: at least one pressure signal passes. " * 3,
            base_year_enplanements=1_000_000,
            horizon_enplanements=1_100_000,
            delta_enplanements=100_000 - index,
            cagr_5=1.9,
            base_year_commercial_operations=50_000,
            horizon_commercial_operations=52_000,
            delta_commercial_operations=2_000,
            signals=[
                signal,
                signal.model_copy(update={"signal": "S2"}),
                signal.model_copy(update={"signal": "S3"}),
            ],
            evidence_status="missing",
            notes=["A note that explains a detail of this airport's classification."],
        )
        for index in range(rows)
    ]
    reference = NationalReference(metric="taf_cagr", period_key="FY2024-FY2029", value=1.76)
    return RankingResult(
        title="Expansion candidate ranking: South",
        data=RankingData(
            region_label="South",
            analysis_year=2025,
            taf_edition="TAF 2025",
            base_year=2024,
            horizon_year=2029,
            national_references={"S1": reference},
            rows=airport_rows,
        ),
        limitations=[f"Limitation number {i} with some explanatory text." for i in range(9)],
    )


@pytest.mark.parametrize("max_chars", [24_000, 3_000])
def test_large_rankings_fit_the_output_budget(max_chars: int) -> None:
    result = ranking_result(200)
    view = compact_result(result, max_chars)
    assert len(json.dumps(view, ensure_ascii=False)) <= max_chars
    assert view["rows"][0]["code"] == "X00" and view["group_counts"] == {"Watch": 200}
    assert view.get("truncated") or len(view["rows"]) < 200
