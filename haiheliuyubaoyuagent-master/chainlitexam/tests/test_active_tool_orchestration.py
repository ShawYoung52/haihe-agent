from __future__ import annotations

import inspect

import pytest

from chainlitexam.tests.stubs import ensure_stubs

ensure_stubs()

from chainlitexam import message_orchestrator as mo
from tools.active_tool_router import ActiveToolRouter, ToolRouteDecision
from tools.tool_round_evidence import ToolRoundEvidence
from chainlitexam.timing_logger import TimingContext


class _PlannerMessage:
    def __init__(self, calls=None, content=""):
        self.tool_calls = list(calls or [])
        self.content = content


class _Router:
    def __init__(self, decision, selected_chain="filtered", full_chain="full"):
        self.decision = decision
        self.selected_chain = selected_chain
        self.full_chain = full_chain

    def select(self, text, limit=12):
        return self.decision

    def chain_for(self, decision):
        return self.selected_chain if decision.mode == "filtered" else self.full_chain


class _ProcessReasoning:
    _closed = False
    step = type("Step", (), {"id": "reasoning"})()

    async def __aenter__(self): return self
    async def __aexit__(self, *args): return None
    async def stage(self, *args, **kwargs): return None
    async def line(self, *args, **kwargs): return None
    async def append(self, *args, **kwargs): return None
    async def close(self): self._closed = True


def _install_process_context(monkeypatch, outputs: list[str], thread_id: str):
    class _StreamMessage:
        def __init__(self, **kwargs): self.content = ""
        async def send(self): return None
        async def update(self): outputs.append(self.content)
        async def remove(self): return None

    monkeypatch.setattr(mo, "ReasoningStep", lambda name="": _ProcessReasoning())
    monkeypatch.setattr(mo.cl, "Message", _StreamMessage)
    try:
        from chainlit.context import context_var, init_http_context
        context_var.set(init_http_context(thread_id=thread_id))
    except Exception:
        pass
    return _StreamMessage


def _process_callbacks(*, router, planner, answer, outputs):
    async def noop(*args, **kwargs):
        return None

    async def stream_text(text, stream_msg=None, **kwargs):
        outputs.append(text)

    return {
        "active_tool_router": router,
        "astream_planner_think": planner,
        "astream_thinking_to_reasoning": noop,
        "astream_answer_chain_to_message": answer,
        "need_river_plot": lambda text: False,
        "append_followup_if_needed": lambda text, query: text,
        "stream_text_to_message": stream_text,
        "tool_observation_to_text": lambda value: str(value),
        "enrich_with_impact_time_tool": lambda **kwargs: kwargs.get("observation"),
        "should_force_admin_units_reply": lambda text: False,
        "build_admin_units_only_reply": lambda value: value,
        "should_force_partition_table_reply": lambda text: False,
        "build_partition_only_reply": lambda value: value,
        "should_force_structured_impact_reply": lambda text: False,
        "build_structured_impact_reply": lambda value: value,
    }


def test_active_flags_default_true_and_explicit_false_rolls_back(monkeypatch):
    monkeypatch.delenv("ENABLE_ACTIVE_TOOL_FILTER", raising=False)
    monkeypatch.delenv("ENABLE_EVIDENCE_EARLY_FINALIZE", raising=False)
    assert mo._active_tool_filter_enabled() is True
    assert mo._evidence_early_finalize_enabled() is True

    monkeypatch.setenv("ENABLE_ACTIVE_TOOL_FILTER", "false")
    monkeypatch.setenv("ENABLE_EVIDENCE_EARLY_FINALIZE", "false")
    assert mo._active_tool_filter_enabled() is False
    assert mo._evidence_early_finalize_enabled() is False


def test_select_request_planner_chain_uses_filtered_router_and_safe_limit(monkeypatch):
    monkeypatch.delenv("ENABLE_ACTIVE_TOOL_FILTER", raising=False)
    monkeypatch.setenv("ACTIVE_TOOL_LIMIT", "bad")
    decision = ToolRouteDecision("filtered", "water_level", ("query_water_level",), True, "single")
    router = _Router(decision)
    chain, selected = mo._select_request_planner_chain("full-input", {"active_tool_router": router}, "水位")
    assert chain == "filtered"
    assert selected is decision


def test_filtered_missing_or_outside_tool_call_requires_full_fallback():
    decision = ToolRouteDecision("filtered", "water_level", ("query_water_level",), True, "single")
    assert mo._needs_full_planner_fallback(decision, _PlannerMessage()) == "missing_tool_call"
    outside = _PlannerMessage([{"name": "query_rolling_forecast"}])
    assert mo._needs_full_planner_fallback(decision, outside) == "candidate_mismatch"
    valid = _PlannerMessage([{"name": "query_water_level"}])
    assert mo._needs_full_planner_fallback(decision, valid) == ""


def test_full_mode_and_disabled_filter_keep_original_chain(monkeypatch):
    full_decision = ToolRouteDecision("full", "unsafe", (), False, "unsafe")
    router = _Router(full_decision)
    chain, selected = mo._select_request_planner_chain("original", {"active_tool_router": router}, "应急")
    assert chain == "full"
    assert selected is full_decision

    monkeypatch.setenv("ENABLE_ACTIVE_TOOL_FILTER", "false")
    chain, selected = mo._select_request_planner_chain("original", {"active_tool_router": router}, "水位")
    assert chain == "original"
    assert selected is None


def test_early_finalize_is_safe_domain_only():
    safe_decision = ToolRouteDecision(
        "filtered", "water_level", ("query_water_level",), True, "single"
    )
    complete = ToolRoundEvidence()
    complete.record("query_water_level", "ok", {"records": [{"water_level_m": 3.2}]})
    assert mo._should_early_finalize(
        "water_level", complete, emergency=False, request_decision=safe_decision
    ) is True
    assert mo._should_early_finalize(
        "water_level", complete, emergency=True, request_decision=safe_decision
    ) is False
    assert mo._should_early_finalize(
        "unknown", complete, emergency=False, request_decision=safe_decision
    ) is False

    failed = ToolRoundEvidence()
    failed.record("query_water_level", "error", {"error": "timeout"})
    assert mo._should_early_finalize(
        "water_level", failed, emergency=False, request_decision=safe_decision
    ) is False


def test_mixed_or_unsafe_request_and_cross_domain_error_never_early_finalize():
    complete = ToolRoundEvidence()
    complete.record("query_water_level", "ok", {"records": [{"water_level_m": 3.2}]})
    unsafe = ToolRouteDecision("full", "unsafe", (), False, "unsafe_domain")
    mixed = ToolRouteDecision("full", "mixed", (), False, "mixed_or_unknown")
    assert mo._should_early_finalize(
        "water_level", complete, emergency=False, request_decision=unsafe
    ) is False
    assert mo._should_early_finalize(
        "water_level", complete, emergency=False, request_decision=mixed
    ) is False

    cross_domain_failure = ToolRoundEvidence()
    cross_domain_failure.record(
        "query_water_level", "ok", {"records": [{"water_level_m": 3.2}]}
    )
    cross_domain_failure.record("rag_search", "error", {"error": "timeout"})
    assert mo._should_early_finalize(
        "water_level",
        cross_domain_failure,
        emergency=False,
        request_decision=ToolRouteDecision(
            "filtered", "water_level", ("query_water_level",), True, "single"
        ),
    ) is False


def test_run_tool_round_keeps_five_tuple_contract_and_accepts_evidence_sink():
    signature = inspect.signature(mo._run_tool_round)
    assert "evidence_sink" in signature.parameters
    source = inspect.getsource(mo._run_tool_round)
    assert "evidence_sink.record" in source
    assert "return forced_final_text, ree, warning_bundles, rolling_forecast_bundles, tianhe_passthrough_text" in source


@pytest.mark.asyncio
async def test_parallel_tool_failure_is_recorded_as_error_evidence(monkeypatch):
    """防止并行失败文本经过普通分支后被误记为成功证据。"""
    class _Tool:
        name = "query_current_weather_observation"

    async def fail_invoke(*args, **kwargs):
        raise TimeoutError("upstream timeout")

    monkeypatch.setattr(mo, "_invoke_tool_with_tolerance", fail_invoke)
    planner = _PlannerMessage([{
        "id": "current-1",
        "name": "query_current_weather_observation",
        "args": {},
    }])
    evidence = ToolRoundEvidence()
    messages = []
    try:
        from chainlit.context import context_var, init_http_context
        context_var.set(init_http_context(thread_id="test-prefetch-evidence"))
    except Exception:
        pass

    await mo._run_tool_round(
        planner,
        [_Tool()],
        messages,
        "天津现在天气",
        1,
        {"tool_observation_to_text": lambda observation: str(observation)},
        evidence_sink=evidence,
    )

    assert len(evidence.items) == 1
    assert evidence.items[0].status == "error"
    assert "timeout" in str(evidence.items[0].payload)


@pytest.mark.asyncio
async def test_planner_invocation_counter_counts_success_and_failure_attempts():
    timing = TimingContext("planner-count")

    async def success(chain, payload, reasoning):
        return "ok"

    assert await mo._invoke_planner_once(
        {"astream_planner_think": success}, "chain", [], None, timing
    ) == "ok"

    async def failure(chain, payload, reasoning):
        raise TimeoutError("planner timeout")

    with pytest.raises(TimeoutError):
        await mo._invoke_planner_once(
            {"astream_planner_think": failure}, "chain", [], None, timing
        )

    assert timing.planner_rounds == 2


def test_filtered_transition_to_full_planner_records_reason_once():
    timing = TimingContext("planner-fallback")
    filtered = ToolRouteDecision(
        "filtered", "water_level", ("query_water_level",), True, "single"
    )
    mo._mark_full_planner_transition(timing, filtered, "evidence_incomplete")
    assert timing.full_planner_fallback is True
    assert timing.full_planner_fallback_reason == "evidence_incomplete"

    mo._mark_full_planner_transition(timing, filtered, "tool_error")
    assert timing.full_planner_fallback_reason == "evidence_incomplete"


@pytest.mark.parametrize(
    "question",
    [
        "根据当前水位是否启动响应",
        "查询子牙河当前水位并说明它汇入哪条河",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_request_does_not_finalize_after_water_only(monkeypatch, question):
    """完整水位证据不能绕过应急或河网关系业务规则。"""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)
    counters = {"planner": 0, "answer": 0}
    planner_chains = []
    outputs = []

    async def planner(chain, payload, reasoning):
        counters["planner"] += 1
        planner_chains.append(chain)
        if counters["planner"] == 1:
            return _PlannerMessage([{
                "id": "water-1",
                "name": "query_water_level",
                "args": {"river_name": "子牙河"},
            }])
        return _PlannerMessage([], "仍需由完整 Planner 综合专用业务工具。")

    async def answer(*args, **kwargs):
        counters["answer"] += 1
        return "不应使用的水位提前答案"

    class _WaterTool:
        name = "query_water_level"

        async def ainvoke(self, args):
            return {"records": [{"station_name": "子牙河站", "water_level_m": 3.2}]}

    router = ActiveToolRouter(
        tools=[_WaterTool()],
        full_chain="full",
        build_chain=lambda selected: "filtered",
        candidate_index=None,
    )
    assert router.select(question).mode == "full"
    _install_process_context(monkeypatch, outputs, "test-unsafe-water-emergency")
    callbacks = _process_callbacks(
        router=router, planner=planner, answer=answer, outputs=outputs
    )

    await mo.process_message(
        type("Message", (), {"content": question})(),
        "full",
        None,
        None,
        [_WaterTool()],
        [],
        callbacks,
    )

    assert counters == {"planner": 2, "answer": 0}
    assert planner_chains == ["full", "full"]
    assert any("完整 Planner" in output for output in outputs)


@pytest.mark.parametrize(
    "question,tool_name,payload",
    [
        (
            "子牙河现在水位多高",
            "query_water_level",
            {"records": [{"station_name": "子牙河站", "water_level_m": 3.2}]},
        ),
        (
            "当前九分区平均降水量是多少",
            "query_basin_areal_rainfall",
            [{"zone_name": "大清河", "avg_rainfall_mm": 12.5}],
        ),
    ],
)
@pytest.mark.asyncio
async def test_safe_complete_evidence_finalizes_after_one_planner(
    monkeypatch, question, tool_name, payload,
):
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)
    counters = {"planner": 0, "answer": 0}
    planner_chains = []
    outputs = []

    class _Tool:
        name = tool_name

        async def ainvoke(self, args):
            return payload

    async def planner(chain, request, reasoning):
        counters["planner"] += 1
        planner_chains.append(chain)
        return _PlannerMessage([{
            "id": "safe-1", "name": tool_name, "args": {},
        }])

    async def answer(*args, **kwargs):
        counters["answer"] += 1
        return "基于完整业务数据生成的答案"

    router = ActiveToolRouter(
        tools=[_Tool()],
        full_chain="full",
        build_chain=lambda selected: "filtered",
        candidate_index=None,
    )
    _install_process_context(monkeypatch, outputs, f"test-safe-{tool_name}")
    callbacks = _process_callbacks(
        router=router, planner=planner, answer=answer, outputs=outputs
    )

    await mo.process_message(
        type("Message", (), {"content": question})(),
        "full",
        None,
        None,
        [_Tool()],
        [],
        callbacks,
    )

    assert counters == {"planner": 1, "answer": 1}
    assert planner_chains == ["filtered"]
    assert any("完整业务数据" in output for output in outputs)


@pytest.mark.asyncio
async def test_filtered_tool_failure_transitions_to_full_planner(monkeypatch):
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)
    planner_chains = []
    outputs = []

    class _FailingWaterTool:
        name = "query_water_level"

        async def ainvoke(self, args):
            raise TimeoutError("water upstream timeout")

    async def planner(chain, request, reasoning):
        planner_chains.append(chain)
        if len(planner_chains) == 1:
            return _PlannerMessage([{
                "id": "water-fail", "name": "query_water_level", "args": {},
            }])
        return _PlannerMessage(content="完整 Planner 已处理工具失败。")

    async def answer(*args, **kwargs):
        raise AssertionError("失败证据不能进入 Answer 提前收口")

    router = ActiveToolRouter(
        tools=[_FailingWaterTool()],
        full_chain="full",
        build_chain=lambda selected: "filtered",
        candidate_index=None,
    )
    _install_process_context(monkeypatch, outputs, "test-tool-failure-full-fallback")
    callbacks = _process_callbacks(
        router=router, planner=planner, answer=answer, outputs=outputs
    )

    await mo.process_message(
        type("Message", (), {"content": "子牙河现在水位多高"})(),
        "full",
        None,
        None,
        [_FailingWaterTool()],
        [],
        callbacks,
    )

    assert planner_chains == ["filtered", "full"]
    assert any("工具失败" in output for output in outputs)


@pytest.mark.asyncio
async def test_filtered_planner_without_tool_calls_retries_full_chain(monkeypatch):
    """过滤 Planner 空手返回时，真实流程必须切回完整 Planner 一次。"""
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)
    decision = ToolRouteDecision(
        "filtered", "water_level", ("query_water_level",), True, "single"
    )
    router = _Router(decision, selected_chain="filtered", full_chain="full")
    planner_chains = []
    outputs = []

    async def planner(chain, payload, reasoning):
        planner_chains.append(chain)
        if chain == "filtered":
            return _PlannerMessage()
        return _PlannerMessage(content="完整 Planner 已接管并保守回答。")

    async def answer(*args, **kwargs):
        raise AssertionError("完整 Planner 已给出答案，不应调用 Answer")

    _install_process_context(monkeypatch, outputs, "test-filtered-full-fallback")
    callbacks = _process_callbacks(
        router=router, planner=planner, answer=answer, outputs=outputs
    )

    await mo.process_message(
        type("Message", (), {"content": "子牙河现在水位多高"})(),
        "full",
        None,
        None,
        [],
        [],
        callbacks,
    )

    assert planner_chains == ["filtered", "full"]
    assert any("完整 Planner 已接管" in output for output in outputs)


@pytest.mark.asyncio
async def test_mixed_current_future_request_bypasses_simple_route_and_uses_full_planner(
    monkeypatch,
):
    monkeypatch.setattr(mo, "ENABLE_FAST_PATHS", False)
    monkeypatch.setattr(mo, "ENABLE_LLM_THINKING", False)
    outputs = []
    planner_chains = []
    question = "对比天津现在和明天的天气"

    class _Tool:
        def __init__(self, name):
            self.name = name

    async def planner(chain, request, reasoning):
        planner_chains.append(chain)
        return _PlannerMessage(content="完整 Planner 已处理混合时态。")

    async def answer(*args, **kwargs):
        raise AssertionError("Planner 已直接回答，不应调用 Answer")

    tools = [_Tool("query_current_weather_observation"), _Tool("query_rolling_forecast")]
    router = ActiveToolRouter(
        tools=tools,
        full_chain="full",
        build_chain=lambda selected: "filtered",
        candidate_index=None,
    )
    assert router.select(question).mode == "full"
    _install_process_context(monkeypatch, outputs, "test-mixed-time-full")
    callbacks = _process_callbacks(
        router=router, planner=planner, answer=answer, outputs=outputs
    )

    await mo.process_message(
        type("Message", (), {"content": question})(),
        "full",
        None,
        None,
        tools,
        [],
        callbacks,
    )

    assert planner_chains == ["full"]
    assert any("混合时态" in output for output in outputs)


@pytest.mark.asyncio
async def test_complete_evidence_answer_failure_uses_tool_observation(monkeypatch):
    """提前收口的 Answer 失败时，已取得的业务数据不能丢失。"""
    outputs = []
    stream_type = _install_process_context(
        monkeypatch, outputs, "test-evidence-answer-fallback"
    )
    messages = [
        mo.HumanMessage(content="子牙河现在水位多高"),
        mo.ToolMessage(
            content="子牙河站当前水位 3.2 米",
            tool_call_id="water-1",
            role="tool",
        ),
    ]

    async def answer(*args, **kwargs):
        raise TimeoutError("answer timeout")

    text = await mo._finalize_complete_tool_evidence(
        answer_chain=None,
        messages=messages,
        stream_msg=stream_type(),
        reasoning=_ProcessReasoning(),
        callbacks={
            "astream_answer_chain_to_message": answer,
            "append_followup_if_needed": lambda value, query: value,
        },
        user_text="子牙河现在水位多高",
    )

    assert "子牙河站当前水位 3.2 米" in text
    assert isinstance(messages[-1], mo.AIMessage)


@pytest.mark.asyncio
async def test_empty_evidence_fallback_keeps_reasoning_open_for_full_planner(monkeypatch):
    outputs = []
    stream_type = _install_process_context(
        monkeypatch, outputs, "test-empty-evidence-fallback"
    )
    reasoning = _ProcessReasoning()

    async def answer(*args, **kwargs):
        raise TimeoutError("answer timeout")

    text = await mo._finalize_complete_tool_evidence(
        answer_chain=None,
        messages=[
            mo.HumanMessage(content="查询水位"),
            mo.ToolMessage(content="", tool_call_id="water-1", role="tool"),
        ],
        stream_msg=stream_type(),
        reasoning=reasoning,
        callbacks={
            "astream_answer_chain_to_message": answer,
            "append_followup_if_needed": lambda value, query: value,
        },
        user_text="查询水位",
    )

    assert text == ""
    assert reasoning._closed is False
