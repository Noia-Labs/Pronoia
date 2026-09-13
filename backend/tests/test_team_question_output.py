"""QA must use the actual team pipeline without an event-only output schema."""
from contextlib import nullcontext
from unittest.mock import AsyncMock

import pytest


@pytest.fixture()
def captured_team_synthesis(monkeypatch):
    from app import llm
    from app.agents import team

    messages = []

    async def expert(agent_id, _task, _question, _store, **_kwargs):
        yield {"type": "agent_findings", "agent": agent_id,
               "findings": "财务数据与来源已整理。", "tool_trace": []}

    async def synthesize(_agent_id, prompt, *, state, **_kwargs):
        messages.append(prompt)
        state["content"] = "完整财务分析：原始数据、公式、来源、事实与判断。"
        yield {"type": "token", "delta": state["content"]}

    monkeypatch.setattr(team, "complete_json", AsyncMock(return_value={
        "tasks": [{"agent": "fundamentals_analyst", "task": "整理五年财务数据"}],
        "verdict": "pass",
    }))
    monkeypatch.setattr(team, "_run_expert_serial", expert)
    monkeypatch.setattr(team, "run_agent", synthesize)
    monkeypatch.setattr(team, "_route_signals", AsyncMock(return_value=None))
    monkeypatch.setattr(llm, "model_profile_context", lambda _profile: nullcontext())
    return messages


@pytest.mark.asyncio
async def test_model_lab_qa_keeps_full_question_without_direction_contract(captured_team_synthesis):
    from app.model_lab.service import _pronoia_answer

    question = "用最近5个完整年度数据判断600519的ROIC、自由现金流质量和估值位置；列原始数、公式、来源，并把事实与判断分栏。"
    result = await _pronoia_answer({}, {"code": "V01", "prompt": question})

    assert len(captured_team_synthesis) == 1
    prompt = captured_team_synthesis[0][-1]["content"]
    assert question in prompt
    assert "逐项完成用户原始问题" in prompt
    assert "篇幅以完整回答问题为准" in prompt
    assert "【最终方向】" not in prompt
    assert "【置信度】" not in prompt
    assert "【neutral 约束" not in prompt
    assert "不超过 300 字" not in prompt
    assert result["answer"] == "完整财务分析：原始数据、公式、来源、事实与判断。"


@pytest.mark.asyncio
async def test_event_team_keeps_parser_contract_even_without_signal_scorecard(captured_team_synthesis):
    from app.agents.team import run_team
    from app.llm import noop_artifact_store

    state = {"content": "", "tool_trace": []}
    async for _event in run_team(
        "预测公告后的方向", [], state, noop_artifact_store,
        event_meta={"symbol": "600519", "market": "CN", "event_type_l2": "earnings"},
        skip_verify=True, skip_hypothesis=True,
    ):
        pass

    assert len(captured_team_synthesis) == 1
    prompt = captured_team_synthesis[0][-1]["content"]
    assert "【最终方向】" in prompt
    assert "【置信度】" in prompt
    assert "【中文理由】" in prompt
    assert "不超过 300 字" in prompt
    assert "【neutral 约束" in prompt
