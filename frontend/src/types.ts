// 与后端 schemas 对齐的类型定义（docs/20260729_design.md §7/§8/§9）

export type Mode = "auto" | "agent" | "team";
export type RightTab = "artifacts" | "skills" | "team" | "logic";
export type ArtifactKind = "kline" | "line" | "table" | "evidence" | "report" | "graph" | "simulation";

export interface SimulationJob {
  id: string;
  case_id: string;
  graph_artifact_id: string;
  gateway_job_id: string;
  status: "queued" | "running" | "cancelling" | "completed" | "partial" | "failed" | "cancelled";
  stage: string;
  progress: number;
  error?: string | null;
  artifact_id?: string | null;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
}

/** 左栏研究案例（POST /api/cases 返回时不带 message_count） */
export interface CaseItem {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  message_count?: number;
}

/** 产出物（artifact），payload 结构见 design.md §9 */
export interface Artifact {
  id: string;
  case_id: string;
  message_id?: string | null;
  kind: ArtifactKind;
  title: string;
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  payload: any;
  pinned?: number;
  created_at: string;
}

/** 技能元信息 GET /api/skills */
export interface SkillMeta {
  name: string;
  description: string;
  parameters?: Record<string, unknown>;
  category?: "atomic" | "skill";
  internal?: boolean;
  composes?: string[];
}

/** Agent 花名册 GET /api/agents */
export interface AgentMeta {
  id: string;
  name: string;
  avatar_color: string;
  description: string;
  persona?: string;
  skills: string[];
}

export interface SuggestionItem {
  text: string;
  mode: Mode;
  icon_hint: "newspaper" | "sparkles" | "trending" | "landmark" | "candlestick" | "users";
  desc: string;
  query?: string;
  agent?: string;
}

/** team 模式 Planner 拆解的子任务 */
export interface PlanItem {
  agent: string;
  agent_name?: string;
  task?: string;
  question?: string;
}

/** 待验证推演（研究逻辑库条目，design.md §6.4）。
 *  status 闭环：
 *    pending（待验证）→ 等待用户/系统验证
 *    pending_scheduled（窗口未到）→ 自动验证后判定：horizon 还没到，记 next_check_at
 *    verified（已证实）→ 深度验证或人工标记
 *    rejected（已证伪）→ 深度验证或人工标记
 *    inconclusive（暂无法验证）→ 数据不足，下次再试
 *    dismissed（已忽略）→ 用户主动忽略 */
export type LogicStatus =
  | "pending"
  | "pending_scheduled"
  | "verified"
  | "rejected"
  | "inconclusive"
  | "dismissed";

/** 单次深度验证产出（写入 check_history） */
export interface LogicCheckEntry {
  at: string;
  verdict: LogicStatus | "error";
  reasoning: string;
  data_summary?: string;
  next_check_at?: string | null;
  evidence?: Array<{
    skill: string;
    args?: Record<string, unknown>;
    ok?: boolean;
    summary?: string;
  }>;
  /** 触发方式：auto（后端自动验证）/ manual（用户手动标记） */
  source: "auto" | "manual";
}

export interface LogicItem {
  id: string;
  case_id?: string | null;
  /** 当时所在 assistant 消息的 id，用于跳转/回溯 */
  message_id?: string | null;
  /** 用户原问题（用于再次验证时预填） */
  question?: string;
  hypothesis: string;
  category: string;
  probability: string;
  scope: string;
  horizon: string;
  check: string;
  status: LogicStatus;
  created_at: string;
  verified_at?: string | null;
  /** 验证后的简短备注（用户填） */
  verification_note?: string;
  /** 下次自动验证时间（pending_scheduled 必填） */
  next_check_at?: string | null;
  /** 上次自动验证时间 */
  last_check_at?: string | null;
  /** 所有验证记录（最新在前） */
  check_history?: LogicCheckEntry[];
}

/** assistant 消息由按时间序排列的 parts 组成（design.md §10 消息渲染） */
export type Part =
  | { type: "thinking"; agent?: string; text: string }
  | {
      type: "tool_call";
      id: string;
      agent?: string;
      skill: string;
      args?: Record<string, unknown>;
      status: "running" | "done" | "error";
      preview?: string;
      artifactId?: string;
    }
  | { type: "artifact"; agent?: string; artifactId: string; kind: ArtifactKind; title: string }
  | { type: "text"; agent?: string; text: string }
  | {
      type: "agent_step";
      phase: string;
      agent?: string;
      note?: string;
      plan?: PlanItem[];
      verdict?: string;
      simulationJobId?: string;
    }
  | { type: "logic_items"; items: LogicItem[] };

export interface Message {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  agent?: string | null;
  mode?: Mode;
  parts?: Part[];
  created_at?: string;
  /** 流式期间为 true，用于渲染打字光标/停止逻辑 */
  pending?: boolean;
  error?: boolean;
  /** 具体错误信息，用于失败卡片展示 */
  errorMessage?: string;
}

/** SSE 事件（design.md §7） */
export interface SSEEvent {
  type:
    | "meta"
    | "thinking"
    | "token"
    | "tool_call"
    | "tool_result"
    | "artifact"
    | "agent_step"
    | "case_title"
    | "logic_items"
    | "done"
    | "error";
  case_id?: string;
  mode?: Mode;
  agent?: string;
  /** mode="team" 时透传的白名单（与请求体一致） */
  team_members?: string[] | null;
  delta?: string;
  id?: string;
  skill?: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  preview?: string;
  artifact_id?: string | null;
  artifact?: Artifact;
  phase?: string;
  note?: string;
  plan?: PlanItem[];
  verdict?: string;
  title?: string;
  message_id?: string;
  message?: string;
  simulation_job_id?: string;
  /** logic_items 事件携带的待验证推演条目 */
  items?: LogicItem[];
}

/**
 * GET /api/cases/{id} 响应中的历史消息。
 * tool_trace 后端可能已解析成数组，也可能是 JSON 字符串。
 */
export interface HistoryMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  agent?: string | null;
  tool_trace?: string | Record<string, unknown>[] | null;
  created_at?: string;
}

export interface CaseDetail {
  case: CaseItem;
  messages: HistoryMessage[];
  artifacts: Artifact[];
}

export type ProspectiveRunStatus = "draft" | "scheduled" | "capturing" | "waiting" | "settling" | "completed" | "partial" | "failed" | "cancelled" | string;

export interface ProspectiveRun {
  id: string;
  name: string;
  status: ProspectiveRunStatus;
  capture_at: string;
  settle_after_days: number;
  settle_after_seconds?: number;
  settle_at?: string | null;
  target_settle_at?: string | null;
  source_config?: Record<string, unknown>;
  predictor_config?: Record<string, unknown>;
  metric_config?: Record<string, unknown>;
  total_items?: number;
  frozen_items?: number;
  settled_items?: number;
  correct_items?: number;
  candidate_items?: number;
  selected_items?: number;
  error_message?: string | null;
  evidence_cutoff_at?: string | null;
  evidence_start_date?: string | null;
  evidence_end_date?: string | null;
  evidence_policy?: string | null;
  actual_capture_at?: string | null;
  result_available_at?: string | null;
  next_check_at?: string | null;
  settlement_summary?: {
    total: number; settled: number; waiting: number; failed: number;
    accuracy?: number | null; coverage?: number | null; result_available_at?: string | null;
    next_check_at?: string | null; message?: string;
  };
  created_at: string;
  updated_at: string;
}

export interface ProspectiveCandidate {
  id: string;
  run_id?: string;
  event_id?: string;
  symbol?: string;
  market?: string;
  company_name?: string;
  announcement_title?: string;
  title?: string;
  announcement_date?: string;
  event_time?: string;
  source_url?: string;
  event_type?: string;
  event_type_l2?: string;
  latest_event_at?: string;
  event_count?: number;
  selected?: boolean | number;
  selection_rank?: number | null;
  selection_score?: number | null;
  selection_reason?: string | null;
  selector_model_version?: string | null;
  selector_prompt_version?: string | null;
  status?: string;
  evidence?: Record<string, unknown> | Array<Record<string, unknown>>;
}

export interface ProspectiveSettlement {
  id?: string;
  settlement_mode?: string;
  status?: string;
  actual_label?: string | null;
  actual_value?: number | null;
  reasoning?: string | null;
  result_source?: Record<string, unknown> | string | null;
  settled_at?: string | null;
  created_at?: string | null;
  [key: string]: unknown;
}

export interface ProspectiveItemDetail {
  item: {
    id: string;
    candidate_id?: string | null;
    event_id: string;
    assertion_text?: string;
    market?: string;
    symbol?: string;
    event_type_l2?: string;
    captured_at?: string;
    settle_at?: string;
    status: string;
    actual_label?: string | null;
    actual_value?: number | null;
    is_correct?: number | null;
    error_message?: string | null;
  };
  prediction?: {
    id: string;
    pred_direction: string;
    confidence?: number | null;
    rationale?: string | null;
    model_version?: string | null;
    prompt_version?: string | null;
    as_of_at: string;
    evidence_hash?: string | null;
    raw_prediction?: Record<string, unknown>;
  } | null;
  evidence: Array<{
    id: string;
    source_kind?: string;
    source_url?: string;
    title?: string;
    content?: string;
    published_at?: string;
    retrieved_at?: string;
    content_hash?: string;
  }>;
  settlements: ProspectiveSettlement[];
  trace?: Array<{
    id: string;
    sequence_no: number;
    stage: string;
    stage_title: string;
    as_of_at: string;
    input_snapshot?: Record<string, unknown>;
    output_snapshot?: Record<string, unknown>;
    evidence_refs?: string[];
    model_version?: string | null;
    prompt_version?: string | null;
    trace_version?: string;
    complete?: boolean;
  }>;
}

export interface ProspectiveDetailResponse {
  run: ProspectiveRun;
  items: ProspectiveItemDetail[];
  candidates?: ProspectiveCandidate[];
  metrics: Record<string, unknown>;
  snapshots: Array<Record<string, unknown>>;
  settlement_summary?: ProspectiveRun["settlement_summary"];
}

/* ------- artifact payloads（design.md §9） ------- */

export interface KlinePayload {
  symbol?: string;
  dates: string[];
  ohlc: [number, number, number, number][]; // [open, close, low, high]
  volumes: number[];
  event_date?: string;
}

export interface LinePayload {
  title?: string;
  x: string[];
  series: { name: string; data: (number | null)[] }[];
  yname?: string;
}

export interface TablePayload {
  columns: string[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  rows: any[][];
  note?: string;
}

export interface EvidenceItem {
  title: string;
  date?: string;
  source: string;
  url?: string;
  snippet: string;
}

export interface EvidencePayload {
  items: EvidenceItem[];
}

export interface ReportPayload {
  markdown: string;
}

/* ===================================== Backtest (P0) ===================================== */

export type BTRunner = "baseline" | "team_prompt" | "team_full" | "raw_model" | "provided_analysis";
export type BTStatus = "pending" | "running" | "paused" | "done" | "failed" | "cancelled";
export type BTDirection = "up" | "down" | "neutral";
export type BTStrategyType = "event" | "quant" | "signal_import";
export type BTVisibility = "private" | "team" | "arena_safe";
export type BTTeamFullStage =
  | "planning"
  | "expert_research"
  | "synthesis"
  | "review"
  | "hypothesis_extraction"
  | string;

export interface BTActiveEventProgress {
  event_id?: string;
  symbol?: string;
  market?: string;
  title?: string;
  slot?: number;
  stage: BTTeamFullStage;
  stage_label?: string;
  stage_index?: number;
  stage_total?: number;
  detail?: string;
  agent?: string | null;
  started_at?: string;
  updated_at?: string;
  elapsed_seconds?: number;
  privacy?: "arena_safe" | string;
}

export interface BTRunActivity {
  runner: "team_full" | string;
  active_count: number;
  active_events: BTActiveEventProgress[];
  updated_at?: string;
  stage_total?: number;
  note?: string;
  privacy?: "arena_safe" | string;
}

/** 冻结到每次 Run 的成交与评价口径；这些字段共同决定 Run 是否能在 Arena 中严格比较。 */
export interface BTExecutionSpec {
  frequency?: string;
  benchmark?: string;
  price_field?: "open" | "close" | "vwap" | string;
  execution_delay?: string;
  fee_bps?: number;
  commission_bps?: number;
  slippage_bps?: number;
  stamp_duty_bps?: number;
  other_cost_bps?: number;
  minimum_commission?: number;
  initial_capital?: number;
  max_abs_weight?: number;
  allow_short?: boolean;
  leverage?: number;
  holding_horizon?: string;
  timezone?: string;
  adjustment?: string;
  futures_roll_rule?: string;
  [key: string]: unknown;
}

/** 从已完成组合回测派生的新成本情景；服务端会创建新 Run，不修改原结果。 */
export interface BTCostScenarioInput {
  name?: string;
  commission_bps: number;
  slippage_bps: number;
  stamp_duty_bps: number;
  other_cost_bps: number;
  minimum_commission: number;
  auto_start: true;
}

export interface BTCreateRunInput {
  name: string;
  runner: BTRunner | string;
  horizon?: string;
  strategy_type?: BTStrategyType | string;
  protocol_hash?: string;
  visibility?: BTVisibility | string;
  execution_spec?: BTExecutionSpec;
  dataset_id?: string;
  dataset_version?: string;
  strategy_spec?: BTStrategySpec;
  events_path?: string;
  labels_path?: string;
  prompt_variant?: string;
  model_version?: string;
  concurrency?: number;
  config?: Record<string, unknown>;
}

export type BTEventQAInput = ModelLabBatchInput["qa"] & {
  candidate_profile_id?: string | null;
};

export interface BTEventExperimentInput {
  prediction: BTCreateRunInput;
  prediction_profile_id?: string | null;
  qa?: BTEventQAInput;
  auto_start: boolean;
}

export interface BTEventExperimentResponse {
  run: BTRun;
  qa_batch_id: string | null;
  started: boolean;
  start_errors?: { prediction?: string; qa?: string };
}

export interface ModelLabEventCapabilities {
  platform_default: { available: boolean; name: string; model_id: string | null };
  allow_private_model_endpoints?: boolean;
}

export interface ModelLabBacktestRunResults {
  enabled: boolean;
  batch_id?: string | null;
  results: ModelLabResults | null;
}

export interface BTArenaEligibility {
  has_labels_snapshot?: boolean;
  has_event_snapshot?: boolean;
  has_result_artifact?: boolean;
  prediction_only?: boolean;
  has_numeric_forecast?: boolean;
  evaluated_forecast_count?: number | null;
  performance_sample_sufficient?: boolean;
  forecast_sample_sufficient?: boolean;
  small_sample_redacted?: boolean;
  point_in_time_enforced?: boolean;
  external_portfolio?: boolean;
  eligible?: boolean;
  formal_eligible?: boolean;
  event_content_verified?: boolean;
  model_lab_demo?: boolean;
  [key: string]: unknown;
}

/**
 * Strategy-free comparison facts exposed for Arena candidate selection. Exact
 * source windows and private strategy/model fields are deliberately absent.
 */
export interface BTArenaComparison extends Record<string, unknown> {
  version?: string;
  dataset?: {
    dataset_version?: string | null;
    snapshot_hash?: string | null;
    labels_sha256?: string | null;
    [key: string]: unknown;
  } | null;
  benchmark?: string | null;
  execution_timing?: Record<string, unknown> | null;
  execution_constraints?: Record<string, unknown> | null;
  costs?: Record<string, unknown> | null;
  evaluation?: Record<string, unknown> | null;
  engine_mode?: "event_proxy" | "portfolio" | string | null;
  result_nature?: "proxy" | "simulated_from_real_bars" | "unavailable" | string | null;
  comparison_protocol_hash?: string | null;
}

export interface BTRunConfig extends Record<string, unknown> {
  arena_eligibility?: BTArenaEligibility | null;
  arena_comparison?: BTArenaComparison | null;
}

export interface BTRun {
  id: string;
  name: string;
  status: BTStatus;
  runner: BTRunner | string;
  /** 后端冻结并用于指标、曲线和 Arena 的实际评价窗口。 */
  evaluation_horizon?: string | null;
  /** 统一实验类型：事件模型、量化策略或外部预计算信号。 */
  strategy_type?: BTStrategyType | string | null;
  /** Run 不可变输入的完整性指纹；不能直接作为不同策略的 Arena 分组键。 */
  protocol_hash?: string | null;
  /** 策略无关的 Arena 公平比较指纹。 */
  comparison_protocol_hash?: string | null;
  /** 服务端规范化后的可比条件，不含策略规则或模型机密。 */
  comparison_signature?: Record<string, unknown> | null;
  /** 策略细节的展示范围。 */
  visibility?: BTVisibility | string | null;
  oracle_status?: "available" | "unavailable" | "building" | "failed" | string | null;
  /** 本次运行冻结的成交、成本与基准口径。 */
  execution_spec?: BTExecutionSpec | null;
  prompt_variant?: string | null;
  model_version?: string | null;
  events_path: string;
  labels_path?: string | null;
  out_path: string;
  ckpt_dir?: string | null;
  concurrency: number;
  total_events: number;
  done_events: number;
  /** 事件模型终态质量。done_with_warnings 表示流程完成，但存在无效模型输出或主动弃权。 */
  completion_quality?: "valid" | "completed_with_warnings" | "pending" | "unavailable" | "not_applicable" | string | null;
  warning_count?: number | null;
  invalid_output_count?: number | null;
  voluntary_abstain_count?: number | null;
  insufficient_data_count?: number | null;
  /** 关联数据集（bt_datasets.id）：创建 Run 时可选指定，Arena 聚合时用来分组 */
  dataset_id?: string | null;
  /** 冻结的行情数据版本；正式比较必须与 comparison_protocol_hash 一起匹配。 */
  dataset_version?: string | null;
  /** 数据集名字：冗余字段，方便列表页直接显示 */
  dataset_name?: string | null;
  /** 统一策略契约。旧 Run 可能仅在 config.strategy_type 中保存类型。 */
  strategy_spec?: BTStrategySpec | null;
  /** event_proxy 为事件收益代理；portfolio 为逐 bar 撮合组合回测。 */
  engine_mode?: "event_proxy" | "portfolio" | string | null;
  /** 显式区分真实 bar 仿真、代理结果和不可用，前端不得自行推断。 */
  result_nature?: "proxy" | "simulated_from_real_bars" | "unavailable" | string | null;
  result_path?: string | null;
  acc_t3_strict?: number | null;
  acc_t3_strict_lo?: number | null;
  acc_t3_non_neutral?: number | null;
  /** v2 可插拔指标完整字典（metrics_json 解析后产物） */
  metrics?: Record<string, BTMetricItem> | null;
  config?: BTRunConfig | null;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error_msg?: string | null;
  /** team_full 单事件内的实时阶段；仅供观测，不代表完成数。 */
  activity?: BTRunActivity | null;
}

export interface BTPredictionItem {
  id: string;
  run_id: string;
  event_id: string;
  symbol?: string | null;
  market?: string | null;
  event_type_l2?: string | null;
  pred_direction: BTDirection | string;
  expected_return_pct?: number | null;
  confidence?: number | null;
  abstain: boolean;
  prediction_status?: "valid" | "insufficient_data" | string | null;
  /** 模型输出校验信息；旧记录可能没有，前端需兼容。 */
  strategy_metadata?: {
    prediction_status?: "valid" | "insufficient_data" | string | null;
    direction_mode?: "binary" | "three_class" | string | null;
    output_failure_kind?: string | null;
    failure_kind?: string | null;
    output_validation_errors?: string[] | string | null;
    trajectory_available?: boolean | null;
    trajectory_file_exists?: boolean | null;
    [key: string]: unknown;
  } | null;
  output_failure_kind?: string | null;
  failure_kind?: string | null;
  output_validation_errors?: string[] | string | null;
  trajectory_available?: boolean | null;
  trajectory_file_exists?: boolean | null;
  rationale?: string | null;
  oracle_label_t3?: BTDirection | string | null;
  oracle_car_t3?: number | null;
  is_correct_t3?: boolean | null;
  /** 实际冻结的 Oracle 主窗口；legacy `*_t3` 字段承载的是该窗口的值。 */
  oracle_horizon?: string | null;
  trajectory_ckpt?: string | null;
  horizon?: string | null;
  tokens_in?: number | null;
  tokens_out?: number | null;
  step_ms?: number | null;
  cost_usd?: number | null;
  created_at: string;
}

export type BTEventStatus = "pending" | "processing" | "done";

/** 事件目录：直接从 events JSONL 取完整事件清单 + 执行状态 + 已完成 prediction。
 *  Detail 页面一开始就可以渲染全部 N 条事件（pending/processing/done），
 *  而不是等 prediction 写入 DB 后才渲染。
 */
export interface BTEventCatalogItem {
  event_id: string;
  symbol?: string | null;
  market?: string | null;
  event_type_l2?: string | null;
  title?: string | null;
  /** 事件客观发生时间；与信息可得时间分开，用于防止未来函数。 */
  occurred_at?: string | null;
  /** 策略在历史时点真正能看到该信息的时间。 */
  available_time?: string | null;
  event_time?: string | null;
  source_url?: string | null;
  event_text?: string | null;
  status: BTEventStatus;
  prediction?: BTPredictionItem | null;
  oracle_horizon?: string | null;
}

/** bt_datasets 行：已注册数据集，用于创建回测时的 Data list 下拉 */
export interface BTDataset {
  id: string;
  name: string;
  path: string;
  labels_path?: string | null;
  total_events: number;
  by_market: Record<string, number>;
  by_type: Record<string, number>;
  by_symbol: Record<string, number>;
  date_range?: { min?: string; max?: string } | null;
  created_at?: string | null;
  oracle_status?: "available" | "unavailable" | string;
  /** 是否满足 provided_analysis 的最小字段契约。 */
  decision_ready?: boolean;
  input_contract?: "events_only" | "event_plus_provided_analysis" | string;
  /** 统一数据目录字段；legacy 数据集可能没有这些字段。 */
  dataset_kind?: "event" | "market" | string;
  dataset_version?: string | null;
  version?: string | null;
  snapshot_hash?: string | null;
  snapshot?: string | null;
  status?: "pending" | "available" | "unavailable" | "invalid" | string;
  source?: BTDataSourceRef | null;
  markets?: string[];
  /** Frozen symbol universe; older rows may expose this only under coverage. */
  symbols?: string[] | null;
  asset_type?: string | null;
  frequency?: string | null;
  adjustment?: string | null;
  calendar?: string | null;
  schema_mapping?: Record<string, unknown> | null;
  capabilities?: BTMarketDataCapabilities | null;
  coverage?: BTMarketDataCoverage | null;
  quality_status?: "pending" | "passed" | "partial" | "failed" | "unverified" | string;
  quality_report?: Record<string, unknown> | string | null;
  /** 语义质量不是文件/OHLC 校验；synthetic_demo 仅可验证机制，不应用于正式准确率。 */
  semantic_quality?: string | Record<string, unknown> | null;
}

export interface BTDataSourceRef {
  /** `file` is retained only for reading legacy records; new local registrations use `local_file`. */
  type?: "builtin" | "local_file" | "file" | "api" | "database" | "managed" | string;
  provider?: string | null;
  ref?: string | null;
  metadata?: Record<string, unknown> | null;
  /** 仅保存环境变量名，不保存密钥值。 */
  secret_env_ref?: string | null;
  [key: string]: unknown;
}

export interface BTMarketDataCapabilities {
  daily?: boolean;
  minute?: boolean;
  ohlc?: boolean;
  volume?: boolean;
  point_in_time?: boolean;
  supported_frequencies?: string[];
  [key: string]: unknown;
}

export interface BTMarketDataCoverage {
  start_at?: string | null;
  end_at?: string | null;
  symbols?: number | string[] | null;
  row_count?: number | null;
  [key: string]: unknown;
}

export interface BTDataSourceCapability {
  id?: string;
  provider?: string;
  label?: string;
  status?: "available" | "pending" | "unavailable" | "invalid" | string;
  markets?: string[];
  asset_types?: string[];
  frequencies?: string[];
  capabilities?: BTMarketDataCapabilities;
  message?: string | null;
  [key: string]: unknown;
}

export interface BTStrategySpec {
  type: "event" | "quant" | "api" | "signal_import" | string;
  adapter?: string | null;
  kind?: string | null;
  runner?: string | null;
  model_id?: string | null;
  version?: string | null;
  input_contract?: string | null;
  rules_summary?: string | null;
  api_ref?: string | null;
  endpoint?: string | null;
  output_contract?: string | null;
  headers?: Record<string, string> | null;
  parameters?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface BTStrategyCatalogItem {
  id: string;
  name?: string;
  type?: "event" | "quant" | "api" | string;
  status?: "available" | "pending" | "unavailable" | string;
  description?: string | null;
  input_contract?: string | null;
  supported_frequencies?: string[];
  [key: string]: unknown;
}

export interface BTDatasetRegisterInput {
  id?: string;
  name: string;
  dataset_kind: "market" | "event" | string;
  path?: string | null;
  source: BTDataSourceRef;
  markets: string[];
  asset_type?: string | null;
  frequency?: string | null;
  adjustment?: string | null;
  calendar?: string | null;
  schema_mapping?: Record<string, unknown> | null;
  symbols?: string[];
  capabilities?: BTMarketDataCapabilities | null;
  coverage?: BTMarketDataCoverage | null;
  status?: string | null;
  description?: string | null;
}

export interface BTDatasetVersionInput {
  source_ref?: string | null;
  path?: string | null;
  schema_mapping?: Record<string, unknown> | null;
  metadata?: Record<string, unknown> | null;
  source?: BTDataSourceRef | null;
  coverage?: BTMarketDataCoverage | null;
  capabilities?: BTMarketDataCapabilities | null;
  quality_status?: string | null;
}

/* ===================================== Pronoia 模型评测 ===================================== */

export interface ModelLabProfile {
  id: string;
  name: string;
  provider: string;
  base_url: string;
  model_id: string;
  /** 创建时传入；公开响应会省略环境变量名与密钥值。 */
  secret_env_ref?: string;
  secret_configured: boolean;
  max_output_tokens: number;
  timeout_seconds: number;
  thinking_mode: "auto" | "disabled" | "enabled";
  input_price_per_million: number;
  output_price_per_million: number;
  currency: string;
  is_active: boolean;
  last_validated_at?: string | null;
  last_validation_status?: "ok" | "failed" | string | null;
  last_validation_message?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ModelLabProfileInput {
  name: string;
  provider: string;
  base_url: string;
  model_id: string;
  /** 仅用于提交到服务端保存；公开响应不包含 API Key。与 secret_env_ref 二选一。 */
  api_key?: string;
  secret_env_ref?: string;
  max_output_tokens: number;
  timeout_seconds: number;
  thinking_mode: "auto" | "disabled" | "enabled";
  input_price_per_million: number;
  output_price_per_million: number;
  currency: string;
  is_active: boolean;
}

export interface ModelLabQuestion {
  id: string;
  question_set_id: string;
  code: string;
  experiment?: string | null;
  category?: string | null;
  role?: string | null;
  method?: string | null;
  scenario?: string | null;
  prompt: string;
  skills?: string[];
  deliverable?: string | null;
  gold_standard?: string | null;
  metrics?: string | null;
  risk?: string | null;
  priority?: string | null;
  position?: number;
}

export interface ModelLabQuestionSet {
  id: string;
  name: string;
  description?: string | null;
  version: string;
  is_builtin: boolean;
  question_count: number;
  questions?: ModelLabQuestion[];
  created_at?: string;
  updated_at?: string;
}

export interface ModelLabBatchInput {
  name: string;
  profile_ids: string[];
  auto_start: boolean;
  dry_run: boolean;
  prediction: {
    enabled: boolean;
    dataset_id?: string | null;
    dataset_version?: string | null;
    runner: "team_prompt" | "team_full";
    horizon: "t1" | "t3" | "t5" | "t7" | "t15" | "t30" | "t60";
    concurrency: number;
    prompt_variant: string;
  };
  qa: {
    enabled: boolean;
    question_set_id?: string | null;
    question_ids: string[];
    repeats: number;
    variants: Array<"pronoia" | "raw">;
    scoring_mode: "auto" | "manual" | "mixed";
    judge_profile_id?: string | null;
    concurrency: number;
  };
}

export interface ModelLabTask {
  id: string;
  batch_id: string;
  kind: "prediction" | "qa" | string;
  profile_id: string;
  status: BTStatus | "partial" | string;
  total_items: number;
  done_items: number;
  progress: number;
  config?: Record<string, unknown>;
  result_summary?: Record<string, unknown> | null;
  backtest_run_id?: string | null;
  arena_eligible?: boolean;
  demo?: boolean;
  error_msg?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ModelLabBatch {
  id: string;
  name: string;
  status: BTStatus | "partial" | string;
  completion_quality?: string | null;
  warning_count?: number | null;
  profile_ids: string[];
  dataset_id?: string | null;
  dataset_version?: string | null;
  question_set_id?: string | null;
  prediction?: ModelLabBatchInput["prediction"];
  qa?: ModelLabBatchInput["qa"];
  scoring?: Record<string, unknown>;
  task_counts?: Record<string, number>;
  tasks?: ModelLabTask[];
  demo?: boolean;
  error_msg?: string | null;
  created_at?: string;
  updated_at?: string;
  started_at?: string | null;
  finished_at?: string | null;
}

export interface ModelLabScore {
  fact?: number;
  evidence?: number;
  method?: number;
  reasoning?: number;
  risk?: number;
  usability?: number;
  reproducibility?: number;
  user_value?: number;
  total?: number;
  major_error?: boolean;
  rationale?: string;
  reviewer?: string | null;
  source?: "auto" | "manual" | string;
  [key: string]: unknown;
}

export interface ModelLabQAResult {
  id: string;
  batch_id: string;
  task_id: string;
  profile_id: string;
  /** Frozen model identity from the task snapshot; use the live profile directory only for legacy rows. */
  profile_name?: string | null;
  model_id?: string | null;
  provider?: string | null;
  judge_profile_id?: string | null;
  judge_profile_name?: string | null;
  judge_model_id?: string | null;
  judge_provider?: string | null;
  question_id: string;
  /** Frozen question used for this answer, not the potentially edited library. */
  question?: ModelLabQuestion | null;
  variant: "pronoia" | "raw" | string;
  repeat_no: number;
  status: string;
  answer?: string | null;
  latency_ms?: number | null;
  cost?: number | null;
  usage?: Record<string, unknown> | null;
  auto_score?: ModelLabScore | null;
  manual_score?: ModelLabScore | null;
  final_score?: ModelLabScore | null;
  error_msg?: string | null;
  created_at?: string;
  updated_at?: string;
}

export interface ModelLabQAComparison {
  profile_id: string;
  profile_name?: string | null;
  model_id?: string | null;
  provider?: string | null;
  judge_profile_id?: string | null;
  judge_profile_name?: string | null;
  judge_model_id?: string | null;
  judge_provider?: string | null;
  variant: "pronoia" | "raw" | string;
  answer_count: number;
  scored_count: number;
  failed_count?: number;
  awaiting_manual_count?: number;
  average_total?: number | null;
  dimension_averages?: Record<string, number | null>;
  major_error_rate?: number | null;
  average_latency_ms?: number | null;
  total_cost?: number | null;
}

export interface ModelLabPredictionRun {
  task_id: string;
  profile_id: string;
  profile_name?: string | null;
  model_id?: string | null;
  provider?: string | null;
  status: string;
  task_status?: string;
  direction_mode?: "binary" | "ternary" | string | null;
  epsilon?: number | null;
  completion_quality?: string | null;
  warning_count?: number | null;
  valid_output_count?: number | null;
  insufficient_data_count?: number | null;
  invalid_output_count?: number | null;
  voluntary_abstain_count?: number | null;
  neutral_count?: number | null;
  n_outputs?: number | null;
  metrics_error?: string | null;
  prediction_issues?: Array<{ event_id?: string | null; symbol?: string | null; prediction_status: string; reason: string }>;
  issues_total?: number | null;
  issues_truncated?: boolean;
  dataset_semantic_quality?: string | null;
  dataset_demo?: boolean;
  backtest_run_id?: string | null;
  bt_run_id?: string | null;
  arena_eligible?: boolean;
  demo?: boolean;
  metrics?: Record<string, unknown> | null;
  protocol_hash?: string | null;
}

export interface ModelLabResultsSummary {
  demo?: boolean;
  formal_results?: boolean;
  prediction_runs?: ModelLabPredictionRun[];
  qa_comparison?: ModelLabQAComparison[];
  score_dimensions?: Array<{ key: string; label: string; max: number }>;
  [key: string]: unknown;
}

export interface ModelLabResults {
  batch: ModelLabBatch;
  tasks: ModelLabTask[];
  qa_results: ModelLabQAResult[];
  summary: ModelLabResultsSummary;
}

export interface ModelLabManualScoreInput {
  fact: number;
  evidence: number;
  method: number;
  reasoning: number;
  risk: number;
  usability: number;
  reproducibility: number;
  user_value: number;
  major_error: boolean;
  rationale: string;
  reviewer?: string | null;
}

/** 用户直接录入的事件事实；也可附带已完成的分析结果。 */
export interface BTManualEventInput {
  expected_return_pct?: number;
  event_id?: string;
  market: string;
  symbol: string;
  event_time: string;
  available_time?: string;
  event_type_l2: string;
  title: string;
  event_text?: string;
  source_url?: string;
  benchmark?: string;
  sector_etf?: string;
  analysis_direction?: BTDirection | string;
  confidence?: number;
  rationale?: string;
  horizon?: string;
  /** 截至 available_time 已公开的结构化事件数值，全部可选。 */
  event_facts?: {
    actual_value?: number;
    expected_value?: number;
    previous_value?: number;
    value_unit?: string;
  };
  /** 仅允许使用事件信息可得时点之前已收盘的数据；收益字段单位为百分点。 */
  pre_event_features?: {
    asset_return_5d_pct?: number;
    asset_return_20d_pct?: number;
    benchmark_return_5d_pct?: number;
    benchmark_return_20d_pct?: number;
    excess_return_5d_pct?: number;
    excess_return_20d_pct?: number;
    as_of_note?: "prior_close_only" | string;
  };
}

export interface BTManualDatasetResponse {
  id: string;
  name: string;
  path: string;
  total_events: number;
  labels_path?: string | null;
  oracle_status?: "available" | "unavailable" | string;
  decision_ready?: boolean;
  input_contract?: string;
  dataset_version?: string | null;
  version?: string | null;
  snapshot_hash?: string | null;
}

/** 单条 case 的完整详情：预测记录 + trajectory（team_full 才有）+ 事件元信息 */
export interface BTPredictionDetail {
  prediction: BTPredictionItem;
  trajectory: BTTrajectoryCkpt | null;
  /** 事件说明：后端从 ckpt.event_meta + as_of_packet 提取；含 event_text/title/source_url 等 */
  event_meta?: Record<string, unknown> | null;
}

/** trajectory ckpt JSON：team_full runner 每个 event 会写一个完整记录 */
export interface BTTrajectoryCkpt {
  generated_at?: string;
  wall_seconds?: number;
  event_id?: string;
  event_meta?: Record<string, unknown>;
  run_id?: string;
  model_version?: string;
  system_prompt_variant?: string;
  llm_trajectory_stats?: {
    n_sse_events?: number;
    n_sse_events_stored?: number;
    n_tokens_total?: number;
    n_tool_calls?: number;
    n_hypotheses?: number;
    n_final_chars?: number;
    agents_seen?: string[] | number;
  } | null;
  as_of_packet?: string;
  question_to_team?: string;
  structured_extract?: {
    direction?: string;
    confidence?: number;
    rationale?: string;
    conf_gate_applied?: boolean;
  } | null;
  team_final_state?: {
    content_full?: string;
    tool_trace?: Array<Record<string, unknown>>;
    hypotheses?: Array<Record<string, unknown>>;
  } | null;
  trajectory_sse_events?: Array<{
    type?: string;
    phase?: string;
    note?: string;
    [k: string]: unknown;
  }>;
  [k: string]: unknown;
}

export interface BTPredAccStat {
  acc: number;
  k: number;
  n: number;
  wilson_lo_95?: number | null;
  wilson_hi_95?: number | null;
}

/** v2: 可插拔单指标结果（来自 metrics_registry.MetricResult） */
export interface BTMetricItem {
  value: number | string | Record<string, unknown> | null;
  display_name: string;
  description: string;
  tier: "core" | "extended" | "custom";
  higher_is_better: boolean;
  breakdown?: Record<string, unknown>;
  meta?: Record<string, unknown>;
}

/** v2: 指标元信息（列表 GET /api/bt/metrics/defs 返回） */
export interface BTMetricDef {
  display_name: string;
  description: string;
  tier: "core" | "extended" | "custom";
  higher_is_better: boolean;
}

/** v2: GET /api/bt/runs/{rid}/metrics 返回完整结构 */
export interface BTMetricsV2 {
  run_id: string;
  direction_mode?: "binary" | "three_class" | string | null;
  primary_oracle_horizon: string;
  epsilon: number;
  n_total: number;
  /** 所有已返回预测，包含数据不足和技术无效；不同于可评分交集 n_total。 */
  n_outputs?: number;
  total?: number;
  neutral_count?: number;
  neutral_ratio?: number;
  abstain_count?: number;
  valid_output_count?: number;
  invalid_output_count?: number;
  voluntary_abstain_count?: number;
  insufficient_data_count?: number;
  missing_oracle_count?: number;
  metrics: Record<string, BTMetricItem>;
  // 向后兼容字段（保留老代码不崩）
  acc_t3_strict?: BTPredAccStat;
  acc_t3_non_neutral?: BTPredAccStat;
}

export interface BTMetrics {
  total: number;
  abstain_count: number;
  neutral_count: number;
  neutral_ratio?: number;
  acc_t3_strict: BTPredAccStat;
  acc_t3_non_neutral: BTPredAccStat;
  direction_recall?: Record<string, BTPredAccStat>;
  by_event_type?: Record<string, BTPredAccStat>;
  by_market?: Record<string, BTPredAccStat>;
}

/** GET /api/bt/runs/{run_id}/performance: realized, auditable investment results. */
export interface BTPerformanceSummary {
  total_return?: number | null;
  gross_return?: number | null;
  gross_total_return?: number | null;
  annualized_return?: number | null;
  benchmark_total_return?: number | null;
  benchmark_return?: number | null;
  asset_total_return?: number | null;
  asset_return?: number | null;
  excess_total_return?: number | null;
  excess_return?: number | null;
  max_drawdown?: number | null;
  annualized_volatility?: number | null;
  sharpe_ratio?: number | null;
  sharpe_proxy?: number | null;
  calmar_ratio?: number | null;
  win_rate?: number | null;
  win_rate_basis?: "active_period" | "closed_trade" | "event_window" | string | null;
  profit_factor?: number | null;
  trade_count?: number | null;
  n_trades?: number | null;
  evaluated_events?: number | null;
  n_events_in_protocol?: number | null;
  n_missing_oracle?: number | null;
  total_turnover?: number | null;
  total_cost?: number | null;
  total_commission?: number | null;
  total_slippage?: number | null;
  total_stamp_duty?: number | null;
  total_other_cost?: number | null;
  total_cost_bps?: number | null;
  total_cost_rate_sum?: number | null;
  total_cost_amount_proxy?: number | null;
  fee_bps?: number | null;
  slippage_bps?: number | null;
  round_trip_cost_bps?: number | null;
  initial_capital?: number | null;
  initial_value?: number | null;
  final_value?: number | null;
  final_equity?: number | null;
  [key: string]: unknown;
}

export interface BTPerformanceCurvePoint {
  index?: number;
  timestamp?: string;
  date?: string;
  event_time?: string;
  value?: number | null;
  net_value?: number | null;
  equity?: number | null;
  cumulative_return?: number | null;
  return?: number | null;
  period_return?: number | null;
  drawdown?: number | null;
  close?: number | null;
  [key: string]: unknown;
}

export interface BTPerformanceTrade {
  index?: number;
  id?: string;
  event_id?: string;
  symbol?: string;
  market?: string;
  timestamp?: string;
  date?: string;
  event_time?: string;
  entry_time?: string;
  exit_time?: string;
  direction?: string;
  side?: string;
  return?: number | null;
  realized_return?: number | null;
  net_return?: number | null;
  net_proxy_return?: number | null;
  gross_proxy_return?: number | null;
  oracle_car?: number | null;
  asset_return?: number | null;
  benchmark_return?: number | null;
  pnl?: number | null;
  cost?: number | null;
  entry_price?: number | null;
  exit_price?: number | null;
  quantity?: number | null;
  notional?: number | null;
  position_before?: number | null;
  position_after?: number | null;
  holding_period?: number | string | null;
  signal_time?: string | null;
  signal_timestamp?: string | null;
  execution_timestamp?: string | null;
  from_weight?: number | null;
  to_weight?: number | null;
  turnover?: number | null;
  market_price?: number | null;
  effective_price?: number | null;
  commission?: number | null;
  slippage?: number | null;
  stamp_duty?: number | null;
  other_cost?: number | null;
  total_cost?: number | null;
  occurred_at?: string | null;
  available_time?: string | null;
  confidence?: number | null;
  oracle_label?: string | null;
  correct?: boolean | null;
  [key: string]: unknown;
}

export interface BTPerformancePosition {
  timestamp?: string;
  date?: string;
  symbol?: string;
  market?: string;
  quantity?: number | null;
  weight?: number | null;
  target_weight?: number | null;
  equity?: number | null;
  net_value?: number | null;
  market_value?: number | null;
  avg_cost?: number | null;
  unrealized_pnl?: number | null;
  side?: string | null;
  [key: string]: unknown;
}

export interface BTPerformanceEventMarker {
  event_id?: string;
  symbol?: string;
  timestamp?: string;
  date?: string;
  direction?: string;
  return?: number | null;
  net_proxy_return?: number | null;
  [key: string]: unknown;
}

export interface BTPerformanceKline extends Partial<KlinePayload> {
  dates?: string[];
  /** close-only provider output; the dashboard renders it as a line, never fake OHLC. */
  closes?: number[];
  close?: number[];
  line_only?: boolean;
  series_type?: "ohlc" | "line_only" | string;
  ok?: boolean;
  payload?: BTPerformanceKline;
  error?: string | null;
  [key: string]: unknown;
}

export interface BTPerformanceSamplingSeries {
  total_count?: number;
  returned_count?: number;
  omitted_count?: number;
  limit?: number;
  sampled?: boolean;
  [key: string]: unknown;
}

/**
 * The performance endpoint may bound large arrays for display. Metrics and the
 * frozen result remain full-resolution; only the browser payload is sampled.
 */
export interface BTPerformanceResponseSampling {
  scope?: "display_only" | string;
  source_result_preserved?: boolean;
  metrics_basis?: "full_frozen_result" | string;
  method?: string;
  applied?: boolean;
  series?: Record<string, BTPerformanceSamplingSeries>;
  [key: string]: unknown;
}

/**
 * Stable, compact financial facts for portfolio/quant runs.  Newer servers
 * populate these groups directly; the UI keeps summary/dataset fallbacks for
 * frozen results produced before this contract existed.
 */
export interface BTFinancialAnalysis {
  schema_version?: string | null;
  metrics_basis?: string | null;
  units?: Record<string, unknown> | null;
  period?: Record<string, unknown> | null;
  returns?: Record<string, unknown> | null;
  risk?: Record<string, unknown> | null;
  benchmark?: Record<string, unknown> | null;
  trading?: Record<string, unknown> | null;
  exposure?: Record<string, unknown> | null;
  costs?: Record<string, unknown> | null;
  methodology?: Record<string, unknown> | null;
  [key: string]: unknown;
}

export interface BTPerformanceResponse {
  event_return_forecasts?: import("./components/backtest/EventReturnForecastPanel").EventReturnForecastRow[];
  prediction_only?: boolean;
  return_forecast?: import("./components/backtest/QuantReturnForecastPanel").QuantReturnForecastSummary | null;
  return_forecasts?: import("./components/backtest/QuantReturnForecastPanel").QuantReturnForecastRow[];
  run_id?: string;
  mode?: "event_proxy" | string;
  status?: "available" | "partial" | "unavailable" | string;
  primary_horizon?: string;
  currency?: string | null;
  initial_value?: number | null;
  as_of?: string | null;
  data_frozen?: boolean;
  summary: BTPerformanceSummary;
  equity_curve: BTPerformanceCurvePoint[];
  drawdown_curve: BTPerformanceCurvePoint[];
  asset_curve?: BTPerformanceCurvePoint[];
  benchmark_curve?: BTPerformanceCurvePoint[];
  trades?: BTPerformanceTrade[];
  event_markers?: BTPerformanceEventMarker[];
  kline_by_event?: Record<string, BTPerformanceKline>;
  kline_refs?: Array<Record<string, unknown>>;
  proxy_disclaimer?: string | null;
  effective_protocol?: Record<string, unknown> | null;
  strategy_type?: "event" | "quant" | "api" | "signal_import" | string;
  engine_mode?: "event_proxy" | "portfolio" | string;
  result_nature?: "proxy" | "simulated_from_real_bars" | "unavailable" | string;
  dataset_version?: string | null;
  dataset?: Record<string, unknown> | null;
  data_basis?: Record<string, unknown> | null;
  data_provenance?: Record<string, unknown> | null;
  rule_summary?: string | Record<string, unknown> | null;
  strategy?: Record<string, unknown> | null;
  rules?: string | string[] | Record<string, unknown> | null;
  positions?: BTPerformancePosition[];
  holdings?: BTPerformancePosition[];
  orders?: Array<Record<string, unknown>>;
  signals?: Array<Record<string, unknown>>;
  bars?: Array<Record<string, unknown>>;
  event_decisions?: Array<Record<string, unknown>>;
  correctness?: Record<string, unknown> | null;
  trade_csv_available?: boolean;
  response_sampling?: BTPerformanceResponseSampling | null;
  /** Compact financial overview; does not duplicate curve/bar arrays. */
  financial_analysis?: BTFinancialAnalysis | null;
  data_quality?: {
    labels_file_present?: boolean;
    valid_oracle_event_ids?: number;
    missing_oracle_event_ids?: string[];
    asset_curve_is_event_compounded?: boolean;
    benchmark_curve_is_event_compounded?: boolean;
    kline_is_fetched_on_request_not_frozen?: boolean;
    [key: string]: unknown;
  } | null;
  [key: string]: unknown;
}

export interface BTMetricsSnapshot {
  id: number;
  run_id: string;
  done_count: number;
  acc_t3_strict?: number | null;
  acc_t3_strict_lo?: number | null;
  acc_t3_non_neutral?: number | null;
  neutral_ratio?: number | null;
  created_at: string;
}

export interface BTSSEEvent {
  type:
    | "hello"
    | "heartbeat"
    | "run_started"
    | "run_info"
    | "stage_progress"
    | "progress"
    | "prediction"
    | "metrics_snapshot"
    | "run_status_changed"
    | "run_done"
    | "run_failed"
    | "run_cancelled";
  run_id?: string;
  status?: BTStatus | string;
  done_events?: number;
  total_events?: number;
  from_catchup?: boolean;
  message?: string;
  // prediction 事件字段
  prediction?: BTPredictionItem;
  event_id?: string;
  symbol?: string;
  // metrics_snapshot 事件字段
  done_count?: number;
  acc_t3_strict?: number | null;
  acc_t3_strict_lo?: number | null;
  acc_t3_non_neutral?: number | null;
  neutral_ratio?: number | null;
  // run_status_changed 事件字段（pause/resume）
  from?: BTStatus | string;
  to?: BTStatus | string;
  reason?: string;
  error?: string;
  activity?: BTRunActivity | null;
  stage?: BTTeamFullStage;
  stage_label?: string;
  stage_index?: number;
  stage_total?: number;
  detail?: string;
  agent?: string | null;
  event_elapsed_seconds?: number;
  privacy?: "arena_safe" | string;
}

/** prompt_variant 下拉选项（含具体 system prompt 全文） */
export interface BTPromptVariant {
  id: string;
  label: string;
  description: string;
  market_hint: string;
  prompt_text: string;
}

/** events.jsonl 计数 + 校验结果 */
export interface BTEventsCount {
  path: string;
  valid: boolean;
  count: number;
  message?: string | null;
}

/* ===================================== Arena 横向比对 ===================================== */

export type ArenaStatus = "ready" | "computing" | "done" | "failed";

/** bt_arenas 行 */
export interface ArenaItem {
  id: string;
  name: string;
  arena_type?: "forecast" | "performance" | string | null;
  protocol_hash?: string | null;
  dataset_id?: string | null;
  dataset_name?: string | null;
  run_ids: string[];
  description?: string | null;
  config?: Record<string, unknown> | null;
  /** 创建时可选指定的指标列表（保存到 config.selected_metric_ids 后反序列化到此） */
  selected_metric_ids?: string[] | null;
  status: ArenaStatus | string;
  result?: ArenaComputeResult | null;
  created_at: string;
  updated_at: string;
  finished_at?: string | null;
}

/** Arena 计算结果（POST /api/arena/compute & arena.result 字段） */
export interface ArenaComputeResult {
  arena_type?: "forecast" | "performance" | "prediction" | "investment" | string | null;
  run_count: number;
  selected_metric_ids: string[];
  metric_defs: Record<string, BTMetricDef>;
  comparison_protocol?: {
    strict_comparable: boolean;
    protocol_hash?: string | null;
    comparison_protocol_hash?: string | null;
    comparison_mode?: "formal" | "exploration" | string;
    mismatched_fields?: string[];
    dataset_id?: string | null;
    reason?: string | null;
    warnings?: string[];
  } | null;
  per_run: Record<
    string,
    {
      run_id: string;
      display_name: string;
      runner: string;
      prompt_variant: string;
      model_version: string;
      status: string;
      done_events: number;
      total_events: number;
      metrics: Record<string, BTMetricItem>;
      /* 成本 / 耗时（来自 bt_predictions tokens 字段聚合 + 估算） */
      tokens_in?: number | null;
      tokens_out?: number | null;
      tokens_total?: number | null;
      step_ms_total?: number | null;
      cost_usd_estimate?: number | null;
    }
  >;
  ranking: Record<
    string,
    Array<{ run_id: string; value: number | null; rank: number | null; display_name: string }>
  >;
  radar_chart: {
    axes: Array<{ metric_id: string; display_name: string }>;
    series: Array<{ run_id: string; label: string; values: (number | null)[] }>;
  };
  /**
   * Pairwise 显著性检验（后端按 metric 跑 McNemar / 置换检验）。
   * 嵌套结构：pairwise_tests[runA_id][runB_id].per_metric[metric_id] = {p_value, delta, winner, test_meta?}
   * 与 run-level 概览（如共同事件数）共存。前端可以直接矩阵式渲染。
   */
  pairwise_tests:
    | Record<
        string,
        Record<
          string,
          {
            shared_events?: number;
            per_metric: Record<
              string,
              {
                display_name?: string;
                p_value?: number | null;
                delta?: number | null;
                winner?: string | null; // runA_id / runB_id / "tie"
                test_name?: string;
                test_meta?: Record<string, unknown>;
              }
            >;
          }
        >
      >
    | null;
  /**
   * Head-to-Head 事件级对决：
   * - summary 每个 (runA, runB) 对的胜负平计数
   * - event_level 每一条共同事件、每个 Run 的预测、是否正确、该事件最佳 Run 列表
   */
  head_to_head:
    | {
        summary?: Record<
          string,
          { a_wins: number; b_wins: number; ties: number; shared: number }
        >;
        privacy?: {
          event_level_redacted?: boolean;
          redacted_run_ids?: string[];
        };
        event_level: Array<{
          event_id: string;
          oracle?: string | null;
          per_run: Record<
            string,
            {
              prediction?: string | null;
              confidence?: number | null;
              correct?: boolean | null;
              abstain?: boolean | null;
            }
          >;
          best_runs?: string[];
        }>;
      }
    | null;
  /**
   * 综合得分：按 method 聚合多指标排名。每个 Run 的得分 + 全局排名。
   */
  composite_score: {
    method: "rank_average" | "z_score" | string;
    description: string;
    per_run_score: Record<string, { score: number; rank: number }>;
  } | null;
  /**
   * Pareto 成本/效果 二维散点图数据：
   *  - points：每个 run 一个散点（effect=效果越大越好，cost_tokens=总成本越小越好，on_pareto_frontier 标记是否在非支配边界上）
   *  - frontier_line：按 cost_tokens 排序的非支配点序列（用于连出"帕累托前沿"折线）
   *  - defaults：默认坐标映射（前端允许用户切换坐标轴）
   */
  pareto_chart:
    | {
        points: Array<{
          run_id: string;
          label: string;
          runner: string;
          prompt_variant: string;
          model_version: string;
          effect: number;
          effect_metric_id: string;
          cost_tokens: number;
          cost_usd: number;
          step_ms: number;
          composite_score?: number | null;
          on_pareto_frontier?: boolean | null;
        }>;
        frontier_run_ids: string[];
        frontier_line: Array<{ run_id: string | null; cost_tokens: number; effect: number }>;
        defaults: {
          y_axis: string;
          x_axis: string;
        };
      }
    | null;
  /**
   * 表现赛道的多 Run 累计净值叠加。私有 Run 可带真实时间戳；Arena-safe
   * 仅下发隐私聚合观察序号与累计净值，不包含事件 ID、逐笔收益或交易明细。
   */
  performance_curves?:
    | {
        status: "available" | "partial" | "unavailable";
        reason?: string | null;
        strict_comparable: boolean;
        axis: {
          type: "event_observation_index" | string;
          label: string;
        };
        series: Array<{
          run_id: string;
          name: string;
          display_name?: string;
          status: "available" | "partial" | "unavailable";
          reason?: string | null;
          aggregate_only: boolean;
          privacy_aggregated?: boolean;
          points: Array<{ index: number; net_value: number; timestamp?: string; segment?: number }>;
        }>;
        alignment?: Record<string, unknown>;
        coverage?: Record<string, unknown>;
        native_metrics?: Record<string, Record<string, unknown>>;
        aligned_metrics?: Record<string, Record<string, unknown>>;
        aligned_ranking?: Record<
          string,
          Array<{ run_id: string; display_name?: string; value: number | null; rank?: number | null }>
        >;
        privacy?: {
          aggregation_level?: string;
          arena_safe_run_ids?: string[];
          downsampled_run_ids?: string[];
          min_bucket_size?: number;
          excluded_fields?: string[];
          arena_safe_excluded_fields?: string[];
          timestamp_policy?: string;
        };
        disclaimer?: string;
      }
    | null;
}

/* ===================================== Live Log (debug) ===================================== */

/** 后端实时日志条目（来自 EventSource /api/admin/live-log）。
 *  ts 为 ISO 字符串，msg 为单行日志原文（含关键字标签如 TIMING/LLM/SKILL/LLM_JSON）。 */
export interface LogEntry {
  ts: string;
  msg: string;
}
