"""端到端单测：用 case study 事件跑完整 team_full pipeline。

目标（用户原话："确保功能可用"）：
  - event_meta 从 engine 一路传递到 _route_signals
  - _route_signals 路由到正确的 Tier 1 analyzer
  - analyzer 产出的 scorecard 注入 synthesize prompt
  - LLM 最终回复被 parser 正确解析为 direction/confidence/rationale
  - trajectory ckpt 落盘
  - TeamPrediction 被正确构造

策略：用 mock 替换 LLM 调用（complete_json / run_agent），避免消耗 tokens、避免网络/429。
覆盖 3 个路由分支：CN M&A / CN Earnings / US M&A。
"""
import asyncio
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

# 确保能 import backend.app
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 屏蔽 FEVER_BT_STRICT_ASOF 等环境变量影响
os.environ.setdefault("FEVER_BT_FAST", "1")

# 关键：触发 analyzers.py 模块加载，让 @skill decorator 把 6 个新 skill 注册到 REGISTRY。
# 否则 _route_signals 调用 REGISTRY["announcement_classifier"] 会找不到 handler，信号全空 → 返回 None。
from app.skills.registry import ensure_skills_loaded  # noqa: E402
ensure_skills_loaded()


@pytest.fixture(autouse=True)
def _reset_event_loop_policy():
    """每个 test 结束后 reset event loop policy，避免本模块 async test 污染
    其他 sync test（如 test_backtest_p0::test_04_sse 用 get_event_loop() 旧 API）。"""
    import asyncio as _a
    old_policy = _a.get_event_loop_policy()
    yield
    # 关闭可能残留的 loop，重装原 policy
    _a.set_event_loop_policy(old_policy)
    try:
        loop = old_policy.get_event_loop()
        if not loop.is_closed():
            loop.close()
    except Exception:
        pass


# ====== 工具：构造 mock LLM 返回 ======

class _FakeStream:
    """模拟 openai streaming chunk 序列。"""

    def __init__(self, chunks: list[dict]):
        self._chunks = chunks

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._chunks):
            raise StopAsyncIteration
        c = self._chunks[self._idx]
        self._idx += 1
        return c


class _FakeChoice:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        self.delta = types.SimpleNamespace(
            content=content,
            tool_calls=tool_calls,
            reasoning_content=None,
        )
        self.finish_reason = finish_reason


class _FakeChunk:
    def __init__(self, content=None, tool_calls=None, finish_reason=None):
        self.choices = [_FakeChoice(content, tool_calls, finish_reason)]


class _FakeClient:
    """Fake OpenAI client。chat.completions.create 返回 _FakeStream。"""

    def __init__(self, scripted_chunks_per_call: list[list[_FakeChunk]]):
        self._scripted = list(scripted_chunks_per_call)
        self._call_idx = 0
        self.calls: list[dict] = []

    @property
    def chat(self):
        outer = self

        class _Completions:
            @property
            def completions(self):
                return self

            async def create(self, **kwargs):
                outer.calls.append(kwargs)
                if outer._call_idx >= len(outer._scripted):
                    chunks = [_FakeChunk(content="[mock fallback]", finish_reason="stop")]
                else:
                    chunks = outer._scripted[outer._call_idx]
                outer._call_idx += 1
                return _FakeStream(chunks)

        return _Completions()


def _text_chunks(text: str, *, chunk_size: int = 40) -> list[_FakeChunk]:
    """把长文本切成多个 stream chunk，最后加一个空 chunk + finish_reason=stop。"""
    chunks: list[_FakeChunk] = []
    for i in range(0, len(text), chunk_size):
        chunks.append(_FakeChunk(content=text[i:i + chunk_size]))
    chunks.append(_FakeChunk(content="", finish_reason="stop"))
    return chunks


def _make_final_answer(direction: str, confidence: float, rationale: str) -> str:
    """构造符合 parser 的最终回答文本。"""
    return (
        f"【最终方向】{direction}\n"
        f"【置信度】{confidence:.2f}\n"
        f"【中文理由】{rationale}\n"
        "【依据原文片段】公告内容已纳入分析。"
    )


# ====== 事件 fixtures（基于真实 case study 数据形态）======

def _make_cn_ma_event() -> dict:
    """CN 并购事件：凤形股份。"""
    return {
        "event_id": "seed_cn_002760_event_b61013082c",
        "market": "CN",
        "symbol": "002760",
        "event_time": "2025-12-18",
        "event_type_l2": "并购/分拆/再融资",
        "title": "凤形股份:关于本次交易是否构成重大资产重组、关联交易及重组上市的说明",
        "event_text": "凤形股份:关于本次交易是否构成重大资产重组、关联交易及重组上市的说明 | 002760 凤形股份 · 关联交易",
        "source_url": "https://data.eastmoney.com/notices/detail/002760/AN202512171802495426.html",
        "sector_etf": None,
        "benchmark": "sh000300",
        "direction_prior": None,
        "event_strength": None,
    }


def _make_cn_earnings_event() -> dict:
    """CN 财报事件：中石科技（case study N08）。"""
    return {
        "event_id": "seed_cn_300686_event_earnings_case",
        "market": "CN",
        "symbol": "300686",
        "event_time": "2025-10-15",
        "event_type_l2": "财报超预期/不及预期",
        "title": "2025年前三季度业绩预告",
        "event_text": "预计归母净利2.5亿，同比+90%",
        "source_url": "https://data.eastmoney.com/notices/300686/20251015.html",
        "sector_etf": None,
        "benchmark": "sh000300",
        "direction_prior": None,
        "event_strength": None,
    }


def _make_us_ma_event() -> dict:
    """US 并购事件：Rule 425（case study N01）。"""
    return {
        "event_id": "seed_us_nflx_event_rule425",
        "market": "US",
        "symbol": "NFLX",
        "event_time": "2025-06-12",
        "event_type_l2": "并购/分拆/再融资",
        "title": "Rule 425 Filing",
        "event_text": "Securities Act Rule 425 filing",
        "source_url": "https://www.sec.gov/rule425/nflx/20250612.html",
        "sector_etf": None,
        "benchmark": "QQQ",
        "direction_prior": None,
        "event_strength": None,
    }


# ====== 端到端测试 ======

class TestTeamFullE2E:
    """端到端：engine → run_team → _route_signals → analyzer → synthesize → parser。"""

    async def _run_with_mock_llm(self, event: dict, expected_direction: str) -> dict:
        """通用 e2e runner。返回 trajectory ckpt 内容（用于断言）。"""
        from app.event_backtest import engine as eng_mod
        from app.event_backtest.models import EventRecord

        # 构造 EventRecord（dataclass）
        rec = EventRecord(**{k: v for k, v in event.items() if k in EventRecord.__dataclass_fields__})

        # 构造 mock LLM 客户端：每次 create 调用返回一串 chunks
        # 调用顺序（FAST 模式下）：
        #   1. complete_json(plan) —— 不走 client.chat.completions.create streaming，走 json 模式
        #   2. run_agent(market_analyst) —— streaming，1 个 chunk 序列
        #   3. run_agent(fundamentals_analyst) —— streaming
        #   4. run_agent(deep_researcher) —— streaming
        #   5. run_agent(router synthesize) —— streaming，含【最终方向】等
        # FAST 模式跳过 verify 和 hypothesis
        # complete_json 走非 stream 路径，需要单独 mock 返回 resp

        # 准备每路 streaming 调用的 chunks
        market_chunks = _text_chunks("市场分析：T0 个股跌3.02%，基准涨2.39%。pre5 +24.93%，pre20 +53.49%。")
        fundamentals_chunks = _text_chunks("基本面：连续亏损，2025年归母净利-2929万。")
        researcher_chunks = _text_chunks("证据图已沉淀：evidence 3 条、claim 2 条。")
        synth_chunks = _text_chunks(_make_final_answer(expected_direction, 0.72,
            "T0 主动收益 down + 出尽信号触发 + 报告书先验偏空。"))

        fake_client = _FakeClient([market_chunks, fundamentals_chunks, researcher_chunks, synth_chunks])

        # mock complete_json（plan / verify / hypothesis 阶段）
        # FAST 模式：skip_verify=True, skip_hypothesis=True → 只有 plan 一次 complete_json
        async def _fake_complete_json(system, user, *, max_tokens=3000):
            # plan 阶段返回 3 个任务
            return {"tasks": [
                {"agent": "market_analyst", "task": "分析行情"},
                {"agent": "fundamentals_analyst", "task": "分析基本面"},
                {"agent": "deep_researcher", "task": "沉淀证据图"},
            ]}

        # mock run_agent：绕过 streaming，直接把 mock 内容写入 state 并 emit 几个事件
        async def _fake_run_agent(agent_id, messages, *, agent_def, state, artifact_store,
                                  max_rounds=8, emit_thinking=True):
            # 给每个 agent 注入固定 findings
            findings_map = {
                "market_analyst": "T0 个股跌3.02%，基准涨2.39%。pre5 +24.93%，pre20 +53.49%。",
                "fundamentals_analyst": "连续亏损，2025年归母净利-2929万。",
                "deep_researcher": "证据图已沉淀。",
                "router": _make_final_answer(expected_direction, 0.72,
                    "T0 主动收益 down + 出尽信号触发 + 报告书先验偏空。"),
            }
            content = findings_map.get(agent_id, "[mock]")
            state["content"] += content
            yield {"type": "token", "agent": agent_id, "delta": content}

        # Patch 路径
        # 1) app.llm.get_client → 返回 fake_client（万一有 streaming 调用走真 client）
        # 2) app.llm.complete_json → _fake_complete_json
        # 3) app.agents.team.complete_json + run_agent → fake
        # 4) app.llm.noop_artifact_store → 保留（已存在）
        import app.llm as llm_mod
        import app.agents.team as team_mod

        ckpt_dir = Path(__file__).parent / "_e2e_ckpt"
        ckpt_dir.mkdir(exist_ok=True)

        # 调试钩子：拦截 _route_signals 看实际输入输出
        routing_trace: list[dict] = []
        original_route = team_mod._route_signals

        async def _spy_route(event_meta, findings, tool_trace, fallback_text=""):
            routing_trace.append({"event_meta": event_meta,
                                   "findings_keys": list(findings.keys()),
                                   "n_tool_trace": len(tool_trace)})
            r = await original_route(event_meta, findings, tool_trace, fallback_text=fallback_text)
            routing_trace.append({"result_analyzer": (r or {}).get("analyzer"),
                                   "result_direction": (r or {}).get("direction")})
            return r

        with patch.object(llm_mod, "get_client", return_value=fake_client), \
             patch.object(llm_mod, "complete_json", _fake_complete_json), \
             patch.object(team_mod, "complete_json", _fake_complete_json), \
             patch.object(team_mod, "run_agent", _fake_run_agent), \
             patch.object(team_mod, "_route_signals", _spy_route):
            pred = await eng_mod.run_team_full_one_event(
                rec,
                run_id="test_e2e",
                model_version="team-full-e2e-test",
                trajectory_ckpt_dir=str(ckpt_dir),
            )

        # 读回 trajectory ckpt
        ckpt_file = ckpt_dir / f"{event['event_id']}.json"
        assert ckpt_file.exists(), f"trajectory ckpt 未落盘: {ckpt_file}"
        ckpt_data = json.loads(ckpt_file.read_text(encoding="utf-8"))

        return {"pred": pred, "ckpt": ckpt_data, "routing_trace": routing_trace}

    # ----- 3 个路由分支 -----

    async def test_e2e_cn_ma(self):
        """CN 并购 → cn_ma_analyzer 路由 + down 方向（出尽+报告书先验）。"""
        event = _make_cn_ma_event()
        out = await self._run_with_mock_llm(event, expected_direction="down")
        pred = out["pred"]
        ckpt = out["ckpt"]

        # 1. TeamPrediction 字段
        assert pred.event_id == event["event_id"]
        assert pred.pred_direction in {"down", "neutral"}  # mock 设置 down，confidence=0.72>0.60 不会被 gate
        assert pred.pred_direction == "down"
        assert pred.confidence == pytest.approx(0.72, abs=0.01)
        assert pred.run_id == "test_e2e"

        # 2. trajectory ckpt 关键字段
        assert ckpt["event_id"] == event["event_id"]
        assert ckpt["run_id"] == "test_e2e"
        assert "as_of_packet" in ckpt
        assert "team_final_state" in ckpt
        assert "trajectory_sse_events" in ckpt

        # 3. 信号路由事件被记录（signal_routing phase）
        # 在 team_final_state.tool_trace 里找 signal_routing
        tool_trace = ckpt["team_final_state"].get("tool_trace") or []
        routing_events = [t for t in tool_trace if t.get("type") == "signal_routing"]
        assert len(routing_events) >= 1, "未在 trajectory 中找到 signal_routing 记录"
        routing = routing_events[-1]
        assert routing["analyzer"] == "cn_ma_analyzer"
        assert routing["direction"] in {"down", "neutral"}

    async def test_e2e_cn_earnings(self):
        """CN 财报 → cn_earnings_analyzer 路由。"""
        event = _make_cn_earnings_event()
        out = await self._run_with_mock_llm(event, expected_direction="up")
        pred = out["pred"]
        ckpt = out["ckpt"]

        assert pred.pred_direction == "up"
        assert pred.confidence == pytest.approx(0.72, abs=0.01)

        tool_trace = ckpt["team_final_state"].get("tool_trace") or []
        routing_events = [t for t in tool_trace if t.get("type") == "signal_routing"]
        assert len(routing_events) >= 1, "未在 trajectory 中找到 signal_routing 记录"
        routing = routing_events[-1]
        assert routing["analyzer"] == "cn_earnings_analyzer"

    async def test_e2e_us_ma(self):
        """US 并购 → us_ma_analyzer 路由。"""
        event = _make_us_ma_event()
        out = await self._run_with_mock_llm(event, expected_direction="down")
        pred = out["pred"]
        ckpt = out["ckpt"]

        assert pred.pred_direction == "down"

        tool_trace = ckpt["team_final_state"].get("tool_trace") or []
        routing_events = [t for t in tool_trace if t.get("type") == "signal_routing"]
        assert len(routing_events) >= 1, "未在 trajectory 中找到 signal_routing 记录"
        routing = routing_events[-1]
        assert routing["analyzer"] == "us_ma_analyzer"

    # ----- 边界 case -----

    async def test_e2e_unsupported_market_returns_none_routing(self):
        """不支持的市场（HK）→ 不路由 analyzer，但仍能跑通 pipeline 返回 prediction。"""
        event = _make_cn_ma_event()
        event["market"] = "HK"
        event["event_id"] = "hk_unsupported_e2e"
        out = await self._run_with_mock_llm(event, expected_direction="neutral")
        pred = out["pred"]
        ckpt = out["ckpt"]

        # pipeline 不崩，仍返回 prediction
        assert pred.pred_direction in {"up", "down", "neutral"}
        # 没有 signal_routing 记录（HK 无对应 analyzer）
        tool_trace = ckpt["team_final_state"].get("tool_trace") or []
        routing_events = [t for t in tool_trace if t.get("type") == "signal_routing"]
        assert len(routing_events) == 0

    async def test_e2e_low_confidence_retains_supported_direction(self):
        """低置信度保持已给出的方向，不伪装成中性观点。"""
        event = _make_cn_ma_event()
        event["event_id"] = "low_conf_gate_e2e"
        # 构造 confidence=0.45 的最终回答
        from app.event_backtest import engine as eng_mod
        from app.event_backtest.models import EventRecord

        rec = EventRecord(**{k: v for k, v in event.items() if k in EventRecord.__dataclass_fields__})

        async def _fake_complete_json(system, user, *, max_tokens=3000):
            return {"tasks": [
                {"agent": "market_analyst", "task": "分析行情"},
                {"agent": "fundamentals_analyst", "task": "分析基本面"},
                {"agent": "deep_researcher", "task": "沉淀证据图"},
            ]}

        async def _fake_run_agent(agent_id, messages, *, agent_def, state, artifact_store,
                                  max_rounds=8, emit_thinking=True):
            if agent_id == "router":
                content = _make_final_answer("up", 0.45, "低置信度案例。")
            else:
                content = "[mock findings]"
            state["content"] += content
            yield {"type": "token", "agent": agent_id, "delta": content}

        import app.llm as llm_mod
        import app.agents.team as team_mod
        ckpt_dir = Path(__file__).parent / "_e2e_ckpt"

        with patch.object(llm_mod, "get_client", return_value=_FakeClient([])), \
             patch.object(llm_mod, "complete_json", _fake_complete_json), \
             patch.object(team_mod, "complete_json", _fake_complete_json), \
             patch.object(team_mod, "run_agent", _fake_run_agent):
            pred = await eng_mod.run_team_full_one_event(
                rec,
                run_id="test_gate",
                model_version="team-full-e2e-test",
                trajectory_ckpt_dir=str(ckpt_dir),
            )

        # confidence=0.45 remains a directional prediction.
        assert pred.pred_direction == "up"
        assert pred.abstain is False
        assert pred.confidence == pytest.approx(0.45, abs=0.01)
