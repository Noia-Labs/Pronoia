"""Pydantic schemas: REST request/response + SSE event shapes (design.md §7/§8)."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ChatRequest(BaseModel):
    case_id: Optional[str] = None
    message: str = Field(..., min_length=1)
    # auto  → 单个 router Agent（主理人 + 工具循环）
    # agent → 直接调单个指定 Agent（agent 字段必填；如缺省则降级为 router）
    # team  → Planner 拆解 + 多专家串行 + 复核 + 提炼
    mode: Literal["auto", "agent", "team"] = "auto"
    # mode="agent" 时指定具体 agent_id（predictor / market_analyst / event_scout 等）
    agent: Optional[str] = None
    # mode="team" 时限制可调度的专家 Agent id 列表（前端可让用户去选）。
    # 缺省=全部；deep_researcher 是硬规则，不会被过滤掉。
    team_members: Optional[list[str]] = None


class CreateCaseRequest(BaseModel):
    title: Optional[str] = None


class SkillInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class AgentInfo(BaseModel):
    id: str
    name: str
    avatar_color: str
    description: str
    persona: str
    skills: list[str]


# ---------------------------------------------------------------- SSE ------
# Every SSE frame is `data: {json}\n\n`. Event types (design.md §7):
#   meta / thinking / token / tool_call / tool_result / artifact /
#   agent_step / case_title / done / error

def sse(event: dict[str, Any]) -> str:
    """Serialize one SSE frame."""
    import json

    return f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"


# ======================================================== Backtest (P0) ======

EvaluationHorizonValue = Literal["t1", "t3", "t5", "t7", "t15", "t30", "t60"]


class _StrategySpecBase(BaseModel):
    """Frozen, public description of a strategy adapter.

    Unknown adapter-specific parameters are retained so new plug-ins can be
    registered without a database migration.  Secrets are never accepted as
    literal header values; the API layer validates ``env:VARIABLE`` references.
    """

    model_config = ConfigDict(extra="allow")

    name: Optional[str] = Field(None, max_length=120)
    version: Optional[str] = Field("1", min_length=1, max_length=80)
    parameters: dict[str, Any] = Field(default_factory=dict)


class EventStrategySpec(_StrategySpecBase):
    type: Literal["event"] = "event"
    adapter: Literal["existing_platform", "raw_model", "external_http", "imported_decisions"] = "existing_platform"
    runner: Optional[str] = Field(None, max_length=80)
    endpoint: Optional[str] = Field(None, max_length=4_000)
    headers: dict[str, str] = Field(default_factory=dict)
    batch: bool = False


class QuantStrategySpec(_StrategySpecBase):
    type: Literal["quant"] = "quant"
    adapter: Literal["builtin", "signal_file"] = "builtin"
    kind: Optional[str] = Field(None, min_length=1, max_length=80)
    model_id: Optional[str] = Field(None, min_length=1, max_length=80)
    input_contract: str = Field("market_bars", max_length=80)
    path: Optional[str] = Field(None, max_length=4_000)


class APIStrategySpec(_StrategySpecBase):
    type: Literal["api"] = "api"
    adapter: Literal["external_http"] = "external_http"
    endpoint: Optional[str] = Field(None, min_length=1, max_length=4_000)
    api_ref: Optional[str] = Field(None, min_length=1, max_length=4_000)
    output_contract: Literal["target_weight", "event_decision"] = "target_weight"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(30.0, gt=0.0, le=300.0)
    max_bars: int = Field(20_000, ge=2, le=1_000_000)


StrategySpec = Annotated[
    Union[EventStrategySpec, QuantStrategySpec, APIStrategySpec],
    Field(discriminator="type"),
]


class CreateBacktestRunRequest(BaseModel):
    """POST /api/bt/runs — 创建新回测 run 请求。"""
    name: str = Field(..., min_length=1, max_length=120, description="显示名")
    runner: Optional[str] = Field(None, min_length=1, max_length=80, description="旧版 runner 字段；新调用优先使用 strategy_spec")
    dataset_id: Optional[str] = Field(None, description="bt_datasets.id；若传则 events/labels 从 dataset 取")
    dataset_version: Optional[str] = Field(None, max_length=128, description="要冻结的不可变数据版本；省略时冻结数据集当前版本")
    events_path: Optional[str] = Field(None, description="数据集 JSONL 路径，相对路径以 Pronoia 项目根目录为基准，或填绝对路径；与 dataset_id 二选一")
    labels_path: Optional[str] = Field(None, description="labels JSONL 路径（可选，不传则不做 oracle 对照）")
    prompt_variant: str = Field("v0", description="system prompt / agent 编排 变体标识（用于 A/B 对照，例：v0 / optimized-team / ab-1）")
    model_version: Optional[str] = Field(None, description="指定 LLM 模型版本")
    horizon: Optional[EvaluationHorizonValue] = Field(
        None,
        description="本次 Run 冻结并用于指标、收益曲线与 Arena 的 Oracle 评价窗口",
    )
    concurrency: int = Field(4, ge=1, le=10, description="并发数，范围 1~10，默认 4；越大越快但 LLM / Rate Limit 压力越高")
    strategy_type: Optional[str] = Field(None, min_length=1, max_length=60, description="旧版策略类别；新调用由 strategy_spec.type 推导")
    strategy_spec: Optional[StrategySpec] = Field(None, description="事件 / 量化 / 外部 API 三类可插拔策略契约")
    protocol_hash: Optional[str] = Field(None, max_length=128, description="外部冻结的评测协议哈希；留空由服务端生成")
    visibility: Literal["private", "team", "arena_safe", "arena-safe"] = Field("private", description="策略可见性")
    execution_spec: dict[str, Any] = Field(default_factory=dict, description="手续费、滑点、延迟、基准、换月等统一成交假设")
    config: dict[str, Any] = Field(default_factory=dict, description="任意额外参数 JSON")


class CreateCostScenarioRequest(BaseModel):
    """Clone a completed portfolio Run with a new, immutable cost contract."""

    name: Optional[str] = Field(None, min_length=1, max_length=120)
    commission_bps: Optional[float] = Field(None, ge=0.0, le=10_000.0)
    slippage_bps: Optional[float] = Field(None, ge=0.0, le=10_000.0)
    stamp_duty_bps: Optional[float] = Field(None, ge=0.0, le=10_000.0)
    other_cost_bps: Optional[float] = Field(None, ge=0.0, le=10_000.0)
    minimum_commission: Optional[float] = Field(None, ge=0.0, le=1_000_000_000.0)
    auto_start: bool = True


class BacktestRunResponse(BaseModel):
    """回测 Run 详情响应。"""
    id: str
    name: str
    status: str  # pending/running/done/failed
    runner: str
    evaluation_horizon: str = "t3"
    strategy_type: str = "event"
    strategy_spec: Optional[dict[str, Any]] = None
    engine_mode: str = "event_proxy"
    result_nature: str = "proxy"
    dataset_id: Optional[str] = None
    dataset_name: Optional[str] = None
    dataset_version: Optional[str] = None
    protocol_hash: Optional[str] = None
    comparison_protocol_hash: Optional[str] = None
    comparison_signature: Optional[dict[str, Any]] = None
    visibility: str = "private"
    oracle_status: str = "unavailable"
    execution_spec: Optional[dict[str, Any]] = None
    prompt_variant: Optional[str]
    model_version: Optional[str]
    events_path: str
    labels_path: Optional[str]
    out_path: str
    result_path: Optional[str] = None
    ckpt_dir: Optional[str]
    concurrency: int
    total_events: int
    done_events: int
    completion_quality: str = "pending"
    warning_count: int = 0
    invalid_output_count: int = 0
    voluntary_abstain_count: int = 0
    insufficient_data_count: int = 0
    acc_t3_strict: Optional[float]
    acc_t3_strict_lo: Optional[float]
    acc_t3_non_neutral: Optional[float]
    config: Optional[dict[str, Any]]
    metrics: Optional[dict[str, Any]] = None
    created_at: str
    updated_at: str
    started_at: Optional[str]
    finished_at: Optional[str]
    error_msg: Optional[str]
    # Process-local, advisory progress for a currently executing team_full
    # event.  Old/terminal Runs legitimately return null.  It never contributes
    # to done_events.
    activity: Optional[dict[str, Any]] = None


class ListBacktestRunsResponse(BaseModel):
    total: int
    items: list[BacktestRunResponse]


class BacktestPredictionItem(BaseModel):
    id: str
    run_id: str
    event_id: str
    symbol: Optional[str]
    market: Optional[str]
    event_type_l2: Optional[str]
    pred_direction: str
    confidence: Optional[float]
    abstain: bool
    rationale: Optional[str]
    oracle_label_t3: Optional[str]
    oracle_car_t3: Optional[float]
    is_correct_t3: Optional[bool]
    oracle_horizon: Optional[str] = Field(
        None,
        description="上述兼容字段实际对应的 Run 主 Oracle 窗口；列名 *_t3 仅为旧库兼容",
    )
    trajectory_ckpt: Optional[str]
    trajectory_available: bool = False
    horizon: Optional[str] = None
    tokens_in: int = 0
    tokens_out: int = 0
    step_ms: int = 0
    cost_usd: float = 0.0
    strategy_metadata: Optional[dict[str, Any]] = None
    expected_return_pct: Optional[float] = Field(None, description="未来冻结 T+N 标的收益率预测；2 表示 +2%，缺失不从方向推算")
    created_at: str


class ListPredictionsResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[BacktestPredictionItem]
    primary_oracle_horizon: Optional[str] = None


class EventCatalogItem(BaseModel):
    """事件目录项：events JSONL 全量条目 + 执行状态 + 已完成的 prediction。

    用于 Detail 页面一开始就展示所有待处理事件（pending/processing/done），
    而不是等每条 prediction 写入 DB 后才显示。
    """
    event_id: str
    symbol: Optional[str] = None
    market: Optional[str] = None
    event_type_l2: Optional[str] = None
    title: Optional[str] = None
    event_time: Optional[str] = None
    occurred_at: Optional[str] = None
    available_time: Optional[str] = None
    source_url: Optional[str] = None
    event_text: Optional[str] = Field(None, description="事件正文摘要（catalog 列表截断约 300 字，完整正文走 detail 接口）")
    # pending: 未开始；processing: 执行中（ckpt 已出现但 DB prediction 未写）；done: 已完成
    status: Literal["pending", "processing", "done"] = "pending"
    prediction: Optional[BacktestPredictionItem] = None


class ListEventCatalogResponse(BaseModel):
    total: int
    items: list[EventCatalogItem]
    primary_oracle_horizon: Optional[str] = None


class BacktestMetricsSnapshot(BaseModel):
    id: int
    run_id: str
    done_count: int
    acc_t3_strict: Optional[float]
    acc_t3_strict_lo: Optional[float]
    acc_t3_non_neutral: Optional[float]
    neutral_ratio: Optional[float]
    created_at: str


class BacktestMetricsSnapshotList(BaseModel):
    items: list[BacktestMetricsSnapshot]


class ActionResponse(BaseModel):
    """简单操作返回。"""
    ok: bool
    message: Optional[str] = None
    run_id: Optional[str] = None


class DeleteBacktestHistoryRequest(BaseModel):
    """原子删除运行历史中的 Run 与 Model Lab 批次。"""

    run_ids: list[str] = Field(default_factory=list, max_length=500)
    batch_ids: list[str] = Field(default_factory=list, max_length=500)

    @model_validator(mode="after")
    def validate_records(self) -> "DeleteBacktestHistoryRequest":
        self.run_ids = list(dict.fromkeys(str(item).strip() for item in self.run_ids))
        self.batch_ids = list(dict.fromkeys(str(item).strip() for item in self.batch_ids))
        identifiers = [*self.run_ids, *self.batch_ids]
        if not identifiers:
            raise ValueError("请至少选择一条运行记录")
        if len(identifiers) > 1000:
            raise ValueError("每次最多删除 1000 条运行记录")
        if any(not item or len(item) > 200 for item in identifiers):
            raise ValueError("运行记录标识无效，请刷新列表后重试")
        return self


# ---------------------------------------------------------------------------
# UI 辅助接口：prompt 变体 & 事件计数
# ---------------------------------------------------------------------------


class PromptVariantItem(BaseModel):
    """前端 prompt_variant 下拉选项 + 预览。"""
    id: str = Field(..., description="后端 prompt_variant 实际取值（提交 runs 时用这个）")
    label: str = Field(..., description="前端显示名")
    description: str = Field(..., description="一句话描述该变体的差异点")
    market_hint: str = Field(..., description="适用市场说明")
    prompt_text: str = Field(..., description="具体的 system prompt 或 team_full 任务指令全文")


class EventsCountResponse(BaseModel):
    """events.jsonl 事件计数 + 校验结果。"""
    path: str = Field(..., description="解析后的绝对路径")
    valid: bool = Field(..., description="文件存在且通过 load_events 校验")
    count: int = Field(..., description="事件数量；无效时为 0")
    message: Optional[str] = Field(None, description="校验失败时的错误信息")


class BTDatasetResponse(BaseModel):
    """bt_datasets 表的可选项：用于前端 Data list 下拉框。"""
    id: str = Field(..., description="dataset_id，创建 run 时传给 dataset_id")
    name: str = Field(..., description="显示名，例：CN A股半年报 10例")
    path: str = Field(..., description="events JSONL 绝对路径")
    labels_path: Optional[str] = Field(None, description="labels JSONL 绝对路径（可能为空）")
    total_events: int = Field(0, description="事件总数")
    by_market: dict[str, int] = Field(default_factory=dict, description="按 market 统计：{CN:10}")
    by_type: dict[str, int] = Field(default_factory=dict, description="按 event_type_l2 统计")
    by_symbol: dict[str, int] = Field(default_factory=dict, description="按 symbol 统计")
    date_range: Optional[dict] = Field(None, description="事件时间范围：{min, max}")
    created_at: Optional[str] = Field(None, description="首次入库时间")
    oracle_status: str = Field("unavailable", description="available / unavailable")
    oracle_horizon: Optional[str] = Field(None, description="最近一次已校验的 Oracle 必需窗口")
    oracle_coverage: Optional[dict[str, Any]] = Field(None, description="最近一次 Oracle 生成响应中的覆盖统计")
    decision_ready: bool = Field(False, description="全部记录是否满足 provided_analysis 的最小决策字段契约")
    input_contract: str = Field("events_only", description="events_only / event_plus_provided_analysis")
    dataset_kind: str = Field("event", description="event / market")
    dataset_version: Optional[str] = Field(None, description="当前不可变内容版本")
    snapshot_hash: Optional[str] = Field(None, description="当前数据快照 SHA-256")
    status: str = Field("available", description="pending / available / unavailable / invalid")
    source: dict[str, Any] = Field(default_factory=dict)
    markets: list[str] = Field(default_factory=list)
    asset_type: Optional[str] = None
    frequency: Optional[str] = None
    adjustment: Optional[str] = None
    calendar: Optional[str] = None
    symbols: list[str] = Field(default_factory=list)
    schema_mapping: dict[str, str] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    quality_status: str = Field("unverified", description="pending / passed / partial / failed / unverified")
    quality_report: dict[str, Any] = Field(default_factory=dict)
    semantic_quality: dict[str, Any] = Field(
        default_factory=dict,
        description="事件数据语义质量；独立于仅表示结构校验结果的 quality_status",
    )


class DatasetSourceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["local_file", "api", "database", "managed"] = "local_file"
    provider: Optional[str] = Field(None, max_length=120)
    ref: Optional[str] = Field(None, max_length=4_000)
    secret_env_ref: Optional[str] = Field(None, max_length=256)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("secret_env_ref")
    @classmethod
    def validate_data_secret_env_ref(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        from .model_endpoint_security import validate_secret_env_name

        return validate_secret_env_name(
            value,
            namespace="data",
            label="数据源 secret_env_ref",
        )

    @field_validator("metadata")
    @classmethod
    def validate_non_sensitive_metadata(cls, value: dict[str, Any]) -> dict[str, Any]:
        from .model_endpoint_security import is_credential_field_name

        def inspect(item: Any, location: str = "metadata") -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if is_credential_field_name(key):
                        raise ValueError(f"{location}.{key} 不能保存密钥；请使用 secret_env_ref")
                    inspect(child, f"{location}.{key}")
            elif isinstance(item, (list, tuple)):
                for index, child in enumerate(item):
                    inspect(child, f"{location}[{index}]")

        inspect(value)
        return value


class RegisterBTDatasetRequest(BaseModel):
    """Register a data benchmark; registration never downloads market data."""

    id: Optional[str] = Field(None, min_length=1, max_length=120)
    name: str = Field(..., min_length=1, max_length=120)
    dataset_kind: Literal["event", "market"] = "market"
    path: Optional[str] = Field(None, max_length=4_000)
    labels_path: Optional[str] = Field(None, max_length=4_000)
    source: DatasetSourceSpec = Field(default_factory=DatasetSourceSpec)
    markets: list[str] = Field(default_factory=list, max_length=50)
    asset_type: Optional[str] = Field(None, max_length=80)
    frequency: Optional[str] = Field(None, max_length=40)
    adjustment: Optional[str] = Field(None, max_length=80)
    calendar: Optional[str] = Field(None, max_length=80)
    symbols: list[str] = Field(default_factory=list, max_length=20_000)
    schema_mapping: dict[str, str] = Field(default_factory=dict)
    capabilities: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    status: Optional[Literal["pending", "available", "unavailable", "invalid"]] = None
    quality_status: Optional[Literal["pending", "passed", "partial", "failed", "unverified"]] = None
    quality_report: dict[str, Any] = Field(default_factory=dict)


class CreateBTDatasetVersionRequest(BaseModel):
    path: Optional[str] = Field(None, max_length=4_000)
    source_ref: Optional[str] = Field(None, max_length=4_000)
    source: Optional[DatasetSourceSpec] = None
    schema_mapping: Optional[dict[str, str]] = None
    coverage: Optional[dict[str, Any]] = None
    capabilities: Optional[dict[str, Any]] = None
    adjustment: Optional[str] = Field(None, max_length=80)
    calendar: Optional[str] = Field(None, max_length=80)
    quality_status: Optional[Literal["pending", "passed", "partial", "failed", "unverified"]] = None


class ManualEventFacts(BaseModel):
    """Small allowlist of facts observable at the event timestamp."""

    model_config = ConfigDict(extra="forbid")

    actual_value: Optional[float] = None
    expected_value: Optional[float] = None
    previous_value: Optional[float] = None
    value_unit: Optional[str] = Field(None, max_length=80)


class ManualPreEventFeatures(BaseModel):
    """Returns known at the prior close; post-event/Oracle keys are forbidden."""

    model_config = ConfigDict(extra="forbid")

    asset_return_5d_pct: Optional[float] = None
    asset_return_20d_pct: Optional[float] = None
    benchmark_return_5d_pct: Optional[float] = None
    benchmark_return_20d_pct: Optional[float] = None
    excess_return_5d_pct: Optional[float] = None
    excess_return_20d_pct: Optional[float] = None
    as_of_note: Optional[Literal["prior_close_only"]] = None


class ManualBacktestEvent(BaseModel):
    """A user-supplied event fact, optionally with an imported decision."""
    event_id: Optional[str] = Field(None, max_length=120)
    market: str = Field(..., min_length=1, max_length=20)
    symbol: str = Field(..., min_length=1, max_length=80)
    event_time: str = Field(..., min_length=1, max_length=80)
    available_time: Optional[str] = Field(None, max_length=80, description="策略当时真正可获得该信息的时间")
    event_type_l2: str = Field("用户事件", min_length=1, max_length=120)
    title: str = Field(..., min_length=1, max_length=500)
    event_text: str = Field("", max_length=100_000)
    source_url: Optional[str] = Field(None, max_length=4_000)
    benchmark: Optional[str] = Field(None, max_length=80)
    event_facts: ManualEventFacts = Field(
        default_factory=ManualEventFacts,
        description="事件时点已知的结构化事实，例如实际值、预期值、前值与单位",
    )
    pre_event_features: ManualPreEventFeatures = Field(
        default_factory=ManualPreEventFeatures,
        description="事件前已知的六项百分比行情特征；不得包含事件后收益",
    )
    analysis_direction: Optional[Literal["up", "down", "neutral"]] = None
    confidence: Optional[float] = Field(None, ge=0.0, le=1.0)
    rationale: Optional[str] = Field(None, max_length=100_000)
    horizon: EvaluationHorizonValue = Field("t3", description="该分析声明的持有/评价窗口")
    expected_return_pct: Optional[float] = Field(None, strict=True, allow_inf_nan=False, description="该窗口标的收益率预测，2 表示 +2%；没有预测时留空")

    @field_validator("event_time", "available_time")
    @classmethod
    def validate_offset_aware_timestamp(cls, value: Optional[str]) -> Optional[str]:
        """Manual UI/API timestamps must identify one absolute instant.

        Registered legacy datasets may still contain date-only values and are
        normalized later using their market clock. New manual events have an
        explicit input timezone in the UI, so accepting a naive value here
        would discard that guarantee and reintroduce cross-market ambiguity.
        """
        if value is None:
            return None
        raw = value.strip()
        if not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})",
            raw,
        ):
            raise ValueError("必须是带 UTC offset 的 ISO-8601 日期时间（Z 或 ±HH:MM）")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("必须是带 UTC offset 的 ISO-8601 日期时间") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("必须包含 UTC offset（例如 +08:00 或 Z）")
        return raw

    @model_validator(mode="after")
    def validate_optional_analysis_bundle(self) -> "ManualBacktestEvent":
        if self.available_time:
            occurred_at = datetime.fromisoformat(self.event_time.replace("Z", "+00:00"))
            available_at = datetime.fromisoformat(self.available_time.replace("Z", "+00:00"))
            if available_at < occurred_at:
                raise ValueError("available_time 不能早于 event_time")
        supplied = (
            self.analysis_direction is not None,
            self.confidence is not None,
            bool((self.rationale or "").strip()),
        )
        if any(supplied) and not all(supplied):
            raise ValueError(
                "导入已分析决策时 analysis_direction、confidence、rationale 必须同时完整；"
                "仅录入事件事实时请全部省略"
            )
        return self


class CreateManualDatasetRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    events: list[ManualBacktestEvent] = Field(..., min_length=1, max_length=5_000)
