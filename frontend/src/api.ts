import type {
  AgentMeta,
  ArenaComputeResult,
  ArenaItem,
  Artifact,
  BTDataset,
  BTDatasetRegisterInput,
  BTDatasetVersionInput,
  BTCostScenarioInput,
  BTEventExperimentInput,
  BTEventExperimentResponse,
  BTDataSourceCapability,
  BTExecutionSpec,
  BTEventCatalogItem,
  BTEventStatus,
  BTMetricDef,
  BTMetricsV2,
  BTPerformanceKline,
  BTPerformanceResponse,
  BTPredictionDetail,
  BTPredictionItem,
  BTRun,
  BTRunner,
  BTManualDatasetResponse,
  BTManualEventInput,
  BTStrategyType,
  BTStrategyCatalogItem,
  BTStrategySpec,
  BTVisibility,
  BTSSEEvent,
  BTEventsCount,
  BTPromptVariant,
  CaseDetail,
  CaseItem,
  Mode,
  ModelLabBatch,
  ModelLabBatchInput,
  ModelLabBacktestRunResults,
  ModelLabEventCapabilities,
  ModelLabManualScoreInput,
  ModelLabProfile,
  ModelLabProfileInput,
  ModelLabQAResult,
  ModelLabQuestionSet,
  ModelLabResults,
  SkillMeta,
  SuggestionItem,
  SSEEvent,
  SimulationJob,
  ProspectiveDetailResponse,
  ProspectiveRun,
} from "./types";

const BASE = "/api";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const url = BASE + path;
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  const ct = res.headers.get("content-type") ?? "";
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      if (ct.includes("application/json")) {
        const body = await res.json();
        if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } else {
        const text = await res.text();
        const snippet = text.slice(0, 160).replace(/\s+/g, " ");
        if (snippet.trim()) detail += ` · body=${snippet}`;
      }
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  // Avoid Response.clone() for successful JSON. Cloning tees and buffers an
  // unread second body, which used to double the memory pressure of very large
  // historical backtest responses and could freeze the browser process.
  if (ct && !ct.includes("json")) {
    let bodySnippet = "";
    try { bodySnippet = (await res.text()).slice(0, 120).replace(/\s+/g, " "); } catch { /* ignore */ }
    const isHTML = ct.includes("text/html") || /<\s*!doctype|<\s*html/i.test(bodySnippet);
    const hint = isHTML
      ? "后端未返回 JSON。请确认后端已启动且 Vite 代理指向正确端口。"
      : `响应 Content-Type=${ct}，非 application/json`;
    throw new Error(`API 响应解析失败：url=${url} status=${res.status} ct=${ct}${bodySnippet ? ` body-head=${JSON.stringify(bodySnippet)}` : ""} ${hint}`);
  }
  try {
    return (await res.json()) as T;
  } catch (e) {
    const errMsg = e instanceof Error ? e.message : String(e);
    const isHTML = ct.includes("text/html") || /<\s*!doctype|<\s*html/i.test(errMsg);
    let hint = "";
    if (isHTML) {
      hint = "后端未返回 JSON。常见原因：①后端服务版本过旧，缺少该路由（重启 uvicorn 重新加载代码）；②Vite 代理未转发到后端导致返回了 SPA index.html（检查 vite.config.ts proxy 与 :8000 后端进程是否存活）；③后端崩溃后 fallback 到错误页。";
    } else if (ct && !ct.includes("json")) {
      hint = `响应 Content-Type=${ct}，非 application/json`;
    }
    const urlCT = `url=${url} status=${res.status} ct=${ct || "(missing)"}`;
    const parsedSnippet = /Unexpected token '([^']+)'/.exec(errMsg)?.[1] ?? "";
    const snipInfo = parsedSnippet ? ` first-token=${JSON.stringify(parsedSnippet.slice(0, 24))}` : "";
    throw new Error(`API 响应解析失败：${errMsg} · ${urlCT}${snipInfo} ${hint}`.trim());
  }
}

export const api = {
  skills: () => req<SkillMeta[]>("/skills"),
  agents: () => req<AgentMeta[]>("/agents"),
  suggestions: () => req<{ items: SuggestionItem[]; source: string; fallback: boolean }>("/suggestions"),
  cases: () => req<CaseItem[]>("/cases"),
  createCase: (title?: string) =>
    req<CaseItem>("/cases", { method: "POST", body: JSON.stringify(title ? { title } : {}) }),
  caseDetail: (id: string) => req<CaseDetail>(`/cases/${id}`),
  deleteCase: (id: string) => req<{ ok: boolean }>(`/cases/${id}`, { method: "DELETE" }),
  pinArtifact: (caseId: string, artifactId: string) =>
    // 后端 toggle_pin 返回完整 artifact（含最新 pinned 字段）
    req<Artifact>(`/cases/${caseId}/artifacts/${artifactId}/pin`, {
      method: "POST",
      body: "{}",
    }),
  genReport: (caseId: string) =>
    req<Artifact>(`/cases/${caseId}/report`, { method: "POST", body: "{}" }),
  startSimulation: (
    caseId: string,
    body: {
      source_graph_artifact_id: string;
      question?: string;
      horizon_days?: number;
      mode?: "quick" | "calibrated";
      max_actors?: number;
    },
  ) => req<SimulationJob>(`/cases/${caseId}/simulations`, {
    method: "POST",
    body: JSON.stringify(body),
  }),
  simulationPreview: (
    caseId: string,
    body: {
      source_graph_artifact_id: string;
      question?: string;
      horizon_days?: number;
      mode?: "quick";
      max_actors?: number;
    },
  ) => req<{
    actor_selection: {
      mode: "auto" | "manual_cap";
      recommended_count: number;
      applied_limit: number;
      configured_count: number;
      rationale: string;
    };
    actors: Array<{ id: string; label: string; kind: string; selection_reason: string }>;
  }>(`/cases/${caseId}/simulations/preview`, {
    method: "POST",
    body: JSON.stringify(body),
  }),
  simulation: (jobId: string) => req<SimulationJob>(`/simulations/${jobId}`),
  simulations: (caseId: string) => req<SimulationJob[]>(`/cases/${caseId}/simulations`),
  cancelSimulation: (jobId: string) => req<SimulationJob>(`/simulations/${jobId}/cancel`, {
    method: "POST",
    body: "{}",
  }),
  resumeSimulation: (jobId: string) => req<SimulationJob>(`/simulations/${jobId}/resume`, {
    method: "POST",
    body: "{}",
  }),
  logicAutoCheck: (item: {
    hypothesis: string;
    category?: string;
    scope?: string;
    horizon?: string;
    check?: string;
    question?: string;
  }) =>
    fetch(`${BASE}/logic/auto_check`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item),
    }).then(async (r) => {
      if (!r.ok) {
        let detail = `auto_check ${r.status}`;
        try {
          const j = await r.json();
          if (j?.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
        } catch { /* ignore */ }
        throw new Error(detail);
      }
      return (await r.json()) as {
        verdict: "verified" | "rejected" | "inconclusive" | "pending_scheduled" | "error";
        status: string;
        reasoning: string;
        evidence: Array<{ skill: string; args?: Record<string, unknown>; ok?: boolean; summary?: string }>;
        data_summary: string;
        next_check_at: string | null;
        ran_at: string;
      };
    }),

  /* ===================================== Backtest (P0) ===================================== */

  btRuns: (limit = 100) =>
    req<{ total: number; items: BTRun[] }>(`/bt/runs?limit=${limit}`),

  btCreateRun: (data: {
    name: string;
    runner: BTRunner | string;
    /** 本次 Run 真正采用的 Oracle 评价窗口。 */
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
  }) =>
    req<BTRun>("/bt/runs", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  /** 一次保存事件预测与可选问答；两项测试独立启动。 */
  btCreateEventExperiment: (data: BTEventExperimentInput) =>
    req<BTEventExperimentResponse>("/bt/event-experiments", { method: "POST", body: JSON.stringify(data) }),

  /** 冻结用户录入的事件事实；可选附带完整分析供 provided_analysis 使用。 */
  btCreateManualDataset: (data: { name: string; events: BTManualEventInput[] }) =>
    req<BTManualDatasetResponse>("/bt/datasets/manual", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  /** 为手工事件数据集拉取真实行情并生成 Oracle；绝不使用模拟价格。 */
  btBuildDatasetOracle: (datasetId: string, requiredHorizon = "t3") => {
    const qs = new URLSearchParams({ required_horizon: requiredHorizon });
    return req<BTDataset>(
      `/bt/datasets/${encodeURIComponent(datasetId)}/oracle?${qs.toString()}`,
      { method: "POST" },
    );
  },

  btGetRun: (runId: string) => req<BTRun>(`/bt/runs/${runId}`),

  btDeleteRun: (runId: string) =>
    req<{ ok: boolean; run_id?: string; message?: string }>(`/bt/runs/${runId}`, { method: "DELETE" }),

  btDeleteHistoryRecords: (data: { run_ids: string[]; batch_ids: string[] }) =>
    req<{
      ok: boolean;
      message?: string;
      requested_count: number;
      deleted_run_ids: string[];
      deleted_batch_ids: string[];
    }>("/bt/history/delete", { method: "POST", body: JSON.stringify(data) }),

  btStartRun: (runId: string) =>
    req<{ ok: boolean; run_id?: string; message?: string }>(`/bt/runs/${runId}/start`, {
      method: "POST",
      body: "{}",
    }),

  btCancelRun: (runId: string) =>
    req<{ ok: boolean; run_id?: string; message?: string }>(`/bt/runs/${runId}/cancel`, {
      method: "POST",
      body: "{}",
    }),

  btPauseRun: (runId: string) =>
    req<{ ok: boolean; run_id?: string; message?: string }>(`/bt/runs/${runId}/pause`, {
      method: "POST",
      body: "{}",
    }),

  btResumeRun: (runId: string) =>
    req<{ ok: boolean; run_id?: string; message?: string }>(`/bt/runs/${runId}/resume`, {
      method: "POST",
      body: "{}",
    }),

  btListEvents: (runId: string, params?: {
    page?: number;
    page_size?: number;
    market?: string;
    event_type_l2?: string;
    only_incorrect?: boolean;
  }) => {
    const p = new URLSearchParams();
    if (params?.page) p.set("page", String(params.page));
    if (params?.page_size) p.set("page_size", String(params.page_size));
    if (params?.market) p.set("market", params.market);
    if (params?.event_type_l2) p.set("event_type_l2", params.event_type_l2);
    if (params?.only_incorrect) p.set("only_incorrect", "1");
    const qs = p.toString();
    return req<{
      total: number;
      page: number;
      page_size: number;
      items: BTPredictionItem[];
      primary_oracle_horizon?: string;
    }>(`/bt/runs/${runId}/events${qs ? "?" + qs : ""}`);
  },

  /** 事件目录（推荐用于 Detail 页事件列表）：从 events JSONL 返回全部 N 条待处理事件 + status + 已完成 prediction。
   *  与 btListEvents 最大区别：pending/processing 的事件一开始就会渲染，不再等 prediction 写入 DB。
   */
  btListEventCatalog: (runId: string, params?: {
    market?: string;
    event_type_l2?: string;
    only_incorrect?: boolean;
    status?: BTEventStatus;
  }) => {
    const p = new URLSearchParams();
    if (params?.market) p.set("market", params.market);
    if (params?.event_type_l2) p.set("event_type_l2", params.event_type_l2);
    if (params?.only_incorrect) p.set("only_incorrect", "1");
    if (params?.status) p.set("status", params.status);
    const qs = p.toString();
    return req<{ total: number; items: BTEventCatalogItem[]; primary_oracle_horizon?: string }>(
      `/bt/runs/${runId}/events-catalog${qs ? "?" + qs : ""}`,
    );
  },

  /** 单条 case 详情（GET /runs/{rid}/events/{eid}）：返回 prediction 行 + trajectory ckpt（如果是 team_full） */
  btGetPredictionDetail: (runId: string, eventId: string) =>
    req<BTPredictionDetail>(`/bt/runs/${runId}/events/${eventId}`),

  /** 单事件行情；完整 OHLC 返回 K 线，只有真实 close 时显式返回 line_only。 */
  btGetEventKline: (runId: string, eventId: string) =>
    req<{ ok: boolean; payload?: BTPerformanceKline; error?: string }>(
      `/bt/runs/${runId}/events/${eventId}/kline`,
    ),

  btGetMetrics: (runId: string) => req<BTMetricsV2>(`/bt/runs/${runId}/metrics`),

  /** 真实投资表现；无可评测行情时后端返回明确空结果/错误，前端绝不生成演示曲线。 */
  btGetPerformance: (runId: string, includeKline = true) =>
    req<BTPerformanceResponse>(
      // Keep the main result fast: preload one representative market replay;
      // additional events remain available lazily from the event table.
      `/bt/runs/${encodeURIComponent(runId)}/performance?include_kline=${includeKline ? "true" : "false"}&kline_limit=1`,
    ),

  /** 基于已完成组合回测创建新的冻结成本情景 Run；原 Run 永不被覆盖。 */
  btCreateCostScenario: (runId: string, data: BTCostScenarioInput) =>
    req<BTRun>(`/bt/runs/${encodeURIComponent(runId)}/cost-scenario`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  /** 列出已注册的指标元信息（display_name / tier / higher_is_better），用于前端动态渲染。 */
  btMetricDefs: () => req<Record<string, BTMetricDef>>("/bt/metrics/defs"),

  /** 列出 bt_datasets 中已注册的数据集（Data list 下拉用），附带 market/type/symbol 分布 & labels_path。 */
  btListDatasets: () => req<BTDataset[]>("/bt/datasets"),

  /** 行情/事件数据基准能力目录；旧后端缺少路由时调用方应回落到本地静态能力说明。 */
  btDataSourceCapabilities: () =>
    req<{ items: BTDataSourceCapability[] }>("/bt/data-sources/capabilities"),

  /** 后端公开的策略适配器目录。 */
  btStrategies: () =>
    req<{ items: BTStrategyCatalogItem[] }>("/bt/strategies"),

  /** 注册数据基准元数据。无本地 path 时允许以 pending 状态保存。 */
  btRegisterDataset: (data: BTDatasetRegisterInput) =>
    req<BTDataset>("/bt/datasets/register", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  btDatasetVersions: (datasetId: string) =>
    req<{ dataset_id: string; total: number; items: BTDataset[] }>(
      `/bt/datasets/${encodeURIComponent(datasetId)}/versions`,
    ),

  /** 校验并冻结一个版本；后端只接受路径/connector 引用，不接收浏览器伪造行情。 */
  btCreateDatasetVersion: (datasetId: string, data: BTDatasetVersionInput) =>
    req<BTDataset>(`/bt/datasets/${encodeURIComponent(datasetId)}/versions`, {
      method: "POST",
      body: JSON.stringify(data),
    }),

  /** 下载服务端审计 CSV。Arena-safe 时后端仍是最终访问控制边界。 */
  btDownloadTradesCsv: async (runId: string): Promise<Blob> => {
    const res = await fetch(`${BASE}/bt/runs/${encodeURIComponent(runId)}/trades.csv`, {
      headers: { Accept: "text/csv" },
    });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const body = await res.json();
        if (body?.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch { /* ignore */ }
      throw new Error(detail);
    }
    return res.blob();
  },

  /** 列出 prompt 变体：baseline/team_prompt/team_full，附带完整 prompt_text */
  btPromptVariants: (runner = "team_prompt") =>
    req<BTPromptVariant[]>(`/bt/prompt-variants?runner=${encodeURIComponent(runner)}`),

  /** 校验 events 文件并返回事件数 */
  btEventsCount: (path: string) =>
    req<BTEventsCount>(`/bt/events-count?path=${encodeURIComponent(path)}`),

  /** 校验 labels 文件并返回数量；若传 events_path 则额外对比 event_id 覆盖率 */
  btLabelsCount: (path: string, eventsPath?: string) => {
    const qs = new URLSearchParams({ path });
    if (eventsPath?.trim()) qs.set("events_path", eventsPath.trim());
    return req<BTEventsCount>(`/bt/labels-count?${qs.toString()}`);
  },

  /* ==================== Prospective evaluation ==================== */
  prospectiveRuns: (limit = 100) =>
    req<{ total: number; items: ProspectiveRun[] }>(`/prospective/runs?limit=${limit}`),
  prospectiveCreateRun: (data: {
    name: string;
    capture_at: string;
    settle_after_days: number;
    source_config?: {
      source: string;
      lookback_trade_days?: number;
      keywords?: string[];
      symbols?: string[];
      candidate_limit?: number;
      selection_count?: number;
      max_items?: number;
      [key: string]: unknown;
    };
    predictor_config?: Record<string, unknown>;
    metric_config?: {
      settlement_mode?: "after_trade_days" | "absolute_trade_date";
      settlement_trade_date?: string;
      epsilon?: number;
      [key: string]: unknown;
    };
  }) => req<ProspectiveRun>("/prospective/runs", { method: "POST", body: JSON.stringify(data) }),
  prospectiveRun: (runId: string) => req<ProspectiveDetailResponse>(`/prospective/runs/${runId}`),
  prospectiveCapture: (runId: string) => req<ProspectiveRun>(`/prospective/runs/${runId}/capture`, { method: "POST", body: "{}" }),
  prospectiveSettle: (runId: string, force = false) => req<ProspectiveRun>(`/prospective/runs/${runId}/settle?force=${force ? "1" : "0"}`, { method: "POST", body: "{}" }),
  prospectiveCancel: (runId: string) => req<ProspectiveRun>(`/prospective/runs/${runId}/cancel`, { method: "POST", body: "{}" }),

  /* ===================================== Pronoia 模型评测 ===================================== */

  modelLabProfiles: () =>
    req<{ items: ModelLabProfile[]; total: number; default_profile_id: string | null }>("/model-lab/profiles"),

  modelLabEventCapabilities: () =>
    req<ModelLabEventCapabilities>("/model-lab/event-capabilities"),

  modelLabBacktestRunResults: (runId: string) =>
    req<ModelLabBacktestRunResults>(`/model-lab/backtest-runs/${encodeURIComponent(runId)}/results`),

  modelLabCreateProfile: (data: ModelLabProfileInput) =>
    req<ModelLabProfile>("/model-lab/profiles", { method: "POST", body: JSON.stringify(data) }),

  modelLabUpdateProfile: (profileId: string, data: Partial<ModelLabProfileInput>) =>
    req<ModelLabProfile>(`/model-lab/profiles/${encodeURIComponent(profileId)}`, { method: "PATCH", body: JSON.stringify(data) }),

  modelLabDeleteProfile: (profileId: string) =>
    req<{ ok: boolean }>(`/model-lab/profiles/${encodeURIComponent(profileId)}`, { method: "DELETE" }),

  modelLabValidateProfile: (profileId: string) =>
    req<{ ok: boolean; profile: ModelLabProfile; message?: string }>(`/model-lab/profiles/${encodeURIComponent(profileId)}/validate`, { method: "POST", body: "{}" }),

  modelLabSetDefaultProfile: (profileId: string) =>
    req<{ ok: boolean; default_profile_id: string; profile: ModelLabProfile }>(`/model-lab/profiles/${encodeURIComponent(profileId)}/set-default`, { method: "POST", body: "{}" }),

  modelLabQuestionSets: () =>
    req<{ items: ModelLabQuestionSet[]; total: number }>("/model-lab/question-sets"),

  modelLabQuestionSet: (questionSetId: string) =>
    req<ModelLabQuestionSet>(`/model-lab/question-sets/${encodeURIComponent(questionSetId)}`),

  modelLabCreateQuestionSet: (data: {
    name: string;
    description?: string | null;
    version: string;
    questions: Array<Record<string, unknown>>;
  }) => req<ModelLabQuestionSet>("/model-lab/question-sets", { method: "POST", body: JSON.stringify(data) }),

  modelLabBatches: (limit = 100) =>
    req<{ items: ModelLabBatch[]; total: number }>(`/model-lab/batches?limit=${limit}`),

  modelLabBatch: (batchId: string) =>
    req<ModelLabBatch>(`/model-lab/batches/${encodeURIComponent(batchId)}`),

  modelLabCreateBatch: (data: ModelLabBatchInput) =>
    req<ModelLabBatch>("/model-lab/batches", { method: "POST", body: JSON.stringify(data) }),

  modelLabStartBatch: (batchId: string) =>
    req<{ ok: boolean; batch: ModelLabBatch }>(`/model-lab/batches/${encodeURIComponent(batchId)}/start`, { method: "POST", body: "{}" }),

  modelLabCancelBatch: (batchId: string) =>
    req<{ ok: boolean; batch: ModelLabBatch }>(`/model-lab/batches/${encodeURIComponent(batchId)}/cancel`, { method: "POST", body: "{}" }),

  modelLabResults: (batchId: string) =>
    req<ModelLabResults>(`/model-lab/batches/${encodeURIComponent(batchId)}/results`),

  modelLabManualScore: (resultId: string, data: ModelLabManualScoreInput) =>
    req<ModelLabQAResult>(`/model-lab/qa-results/${encodeURIComponent(resultId)}/manual-score`, { method: "POST", body: JSON.stringify(data) }),

  /* ===================================== Arena 横向比对 ===================================== */

  /** 列出所有 Arena 比对实验 */
  arenaList: (limit = 100) =>
    req<{ total: number; items: ArenaItem[] }>(`/arena?limit=${limit}`),

  /** 创建一个新 Arena（落库） */
  arenaCreate: (data: {
    name: string;
    run_ids: string[];
    dataset_id?: string;
    description?: string;
    arena_type?: "forecast" | "performance" | "prediction" | "investment" | string;
    /** 只用于探索性对比；正式 Arena 默认严格禁止混合协议。 */
    allow_mixed_protocols?: boolean;
    selected_metric_ids?: string[];
    config?: Record<string, unknown>;
  }) =>
    req<ArenaItem>("/arena", { method: "POST", body: JSON.stringify(data) }),

  /** 获取单个 Arena */
  arenaGet: (arenaId: string) => req<ArenaItem>(`/arena/${arenaId}`),

  /** 删除 Arena */
  arenaDelete: (arenaId: string) =>
    req<{ ok: boolean; arena_id: string }>(`/arena/${arenaId}`, { method: "DELETE" }),

  /** 即时计算比对（不落库，适合临时对比几个 run） */
  arenaComputeInline: (data: {
    run_ids: string[];
    selected_metric_ids?: string[];
    allow_mixed_protocols?: boolean;
  }) =>
    req<ArenaComputeResult>("/arena/compute", { method: "POST", body: JSON.stringify(data) }),

  /** 对已落库的 arena_id 触发计算并保存结果。返回更新后的 ArenaItem */
  arenaComputeAndSave: (arenaId: string, data?: { run_ids?: string[]; selected_metric_ids?: string[] }) =>
    req<ArenaItem>(`/arena/${arenaId}/compute`, {
      method: "POST",
      body: data ? JSON.stringify(data) : "{}",
    }),

  /**
   * 对已落库的 arena 触发计算并直接返回 ComputeResult（便于详情页「计算完成后立刻展示结果」）。
   * 等价于先 arenaComputeAndSave 再取返回的 arena.result；如果 arena.result 缺失，兜底调用一次 arenaGet。
   */
  arenaCompute: async (arenaId: string): Promise<ArenaComputeResult> => {
    const arena = await api.arenaComputeAndSave(arenaId);
    if (arena.result) return arena.result;
    const fresh = await api.arenaGet(arenaId);
    if (!fresh.result) {
      throw new Error("Arena 计算完成，但后端未返回 result 字段。");
    }
    return fresh.result;
  },
};

/* ==================== 回测 SSE 客户端（/api/bt/stream/{run_id}） ==================== */

export interface BTStreamHandlers {
  onEvent: (ev: BTSSEEvent) => void;
  signal?: AbortSignal;
}

/**
 * 订阅回测 SSE 进度流：GET /api/bt/stream/{run_id}
 * 按 `\n\n` 分帧、逐行 `data: {json}` 解析，心跳帧透传给 onEvent。
 */
export async function streamBacktest(
  runId: string,
  { onEvent, signal }: BTStreamHandlers,
): Promise<void> {
  if (signal?.aborted) throw new StreamAbortedError();

  const res = await fetch(`${BASE}/bt/stream/${runId}`, {
    headers: { Accept: "text/event-stream" },
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch { /* ignore */ }
    throw new Error(detail);
  }
  if (!res.body) throw new Error("响应无流式内容");

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let aborted = false;

  if (signal) {
    if (signal.aborted) {
      reader.cancel().catch(() => undefined);
      throw new StreamAbortedError();
    }
    signal.addEventListener("abort", () => {
      aborted = true;
      reader.cancel().catch(() => undefined);
    }, { once: true });
  }

  const handleFrame = (frame: string) => {
    for (const line of frame.split("\n")) {
      const trimmed = line.trimEnd();
      if (!trimmed.startsWith("data:")) continue;
      const data = trimmed.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      try {
        onEvent(JSON.parse(data) as BTSSEEvent);
      } catch {
        // 忽略坏帧
      }
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (aborted) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        handleFrame(frame);
        if (aborted) break;
      }
      if (aborted) break;
    }
  } catch (e) {
    if (aborted || (e instanceof DOMException && e.name === "AbortError")) {
      throw new StreamAbortedError();
    }
    throw e;
  }
  if (aborted) throw new StreamAbortedError();
  if (buf.trim()) handleFrame(buf);
}

/** fetch 主动中止的标记（区别于真实网络错误），供 store 静默处理 */
export class StreamAbortedError extends Error {
  constructor() { super("stream-aborted"); this.name = "StreamAbortedError"; }
}

export interface StreamHandlers {
  onEvent: (ev: SSEEvent) => void;
  signal?: AbortSignal;
}

/**
 * SSE 客户端（design.md §7 / §10）：
 * fetch POST /api/chat + ReadableStream，按 `\n\n` 分帧、逐行 `data: {json}` 解析；
 * AbortSignal → 立即 reader.cancel()，不向 fetch 传 signal，
 * 避免浏览器把 in-flight 请求标记为 ERR_ABORTED 写到 Console。
 */
export async function streamChat(
  body: {
    case_id: string;
    message: string;
    mode: Mode;
    agent?: string | null;
    team_members?: string[] | null;
  },
  { onEvent, signal }: StreamHandlers,
): Promise<void> {
  // 进入前先看 signal；已 abort 则不发请求
  if (signal?.aborted) throw new StreamAbortedError();
  const res = await fetch(`${BASE}/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (j?.detail) detail = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
    } catch {
      /* ignore */
    }
    throw new Error(detail);
  }
  if (!res.body) throw new Error("响应无流式内容");

  const reader = res.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buf = "";
  let aborted = false;

  // 监听外部 abort：立刻关闭 reader，fetch 自然结束（不会触发 ERR_ABORTED）
  if (signal) {
    if (signal.aborted) {
      reader.cancel().catch(() => undefined);
      throw new StreamAbortedError();
    }
    signal.addEventListener("abort", () => {
      aborted = true;
      reader.cancel().catch(() => undefined);
    }, { once: true });
  }

  const handleFrame = (frame: string) => {
    for (const line of frame.split("\n")) {
      const trimmed = line.trimEnd();
      if (!trimmed.startsWith("data:")) continue;
      const data = trimmed.slice(5).trim();
      if (!data || data === "[DONE]") continue;
      try {
        onEvent(JSON.parse(data) as SSEEvent);
      } catch {
        // 忽略坏帧，不中断流
      }
    }
  };

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (aborted) break;
      buf += decoder.decode(value, { stream: true });
      let idx: number;
      while ((idx = buf.indexOf("\n\n")) >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        handleFrame(frame);
        if (aborted) break;
      }
      if (aborted) break;
    }
  } catch (e) {
    if (aborted || (e instanceof DOMException && e.name === "AbortError")) {
      throw new StreamAbortedError();
    }
    throw e;
  }
  if (aborted) throw new StreamAbortedError();
  // 流末尾可能残留未以 \n\n 结尾的一帧
  if (buf.trim()) handleFrame(buf);
}
