import type { ArenaItem } from "../../types";

export interface ComparisonMarketSource {
  id: string;
  label: string;
  frequency: string;
  start_at?: string | null;
  end_at?: string | null;
}

export interface ComparisonSubject {
  key: string;
  kind: "event" | "event_set" | "asset";
  title: string;
  symbol: string;
  market: string;
  event_time?: string | null;
  run_ids: string[];
  sources: ComparisonMarketSource[];
}

export interface ComparisonCandidate {
  id: string;
  name: string;
  model_kind: string;
  model_name?: string | null;
  eligible: boolean;
  reason?: string | null;
  subject_keys: string[];
}

export interface ComparisonCatalog {
  subjects: ComparisonSubject[];
  runs: ComparisonCandidate[];
}

export interface ComparisonRequest {
  subject_key: string;
  run_ids: string[];
  market_source_id: string;
  start_date?: string;
  end_date?: string;
  holding_bars: number;
  fee_bps: number;
  slippage_bps: number;
  initial_capital: number;
  position_rule: "long_flat";
}

export interface ComparisonCurvePoint {
  timestamp?: string | null;
  event_id?: string | null;
  net_value: number | null;
  drawdown?: number | null;
}

export interface EventComparisonDetail {
  schema_version?: "arena-result-record-v1";
  event_id: string;
  title?: string | null;
  time?: string | null;
  timestamp?: string | null;
  occurred_at?: string | null;
  market?: string | null;
  symbol?: string | null;
  label?: "up" | "down" | "neutral" | null;
  direction?: "up" | "down" | "neutral" | null;
  prediction?: "up" | "down" | "neutral" | null;
  confidence?: number | null;
  actual_direction?: "up" | "down" | "neutral" | null;
  actual_return?: number | null;
  asset_return?: number | null;
  benchmark_return?: number | null;
  directional_return?: number | null;
  net_directional_return?: number | null;
  pnl?: number | null;
  return_unit?: string | null;
  return_basis?: string | null;
  metrics?: {
    is_correct?: boolean | null;
    is_win?: boolean | null;
    active_trade?: boolean | null;
    horizon?: string | null;
  };
  drilldown?: { event_id?: string | null; target_id?: string | null };
}

export interface ComparisonModel {
  run_id: string;
  name: string;
  model_kind: string;
  metrics: {
    total_return: number | null;
    max_drawdown: number | null;
    excess_total_return: number | null;
    trade_count: number | null;
    total_cost: number | null;
    annualized_return?: number | null;
    sharpe_ratio?: number | null;
    win_rate?: number | null;
    calmar_ratio?: number | null;
    annualized_volatility?: number | null;
    total_turnover?: number | null;
    annualization_status?: string;
    accuracy?: number | null;
    directional_accuracy?: number | null;
    average_return?: number | null;
    average_directional_return?: number | null;
    cumulative_return?: number | null;
    event_count?: number | null;
    prediction_count?: number | null;
    active_trade_count?: number | null;
    neutral_count?: number | null;
    missing_return_count?: number | null;
    return_basis?: string | null;
    return_unit?: string | null;
  };
  curve: ComparisonCurvePoint[];
  forecast: {
    mae_pct?: number | null;
    rmse_pct?: number | null;
    median_abs_error_pct?: number | null;
    p90_abs_error_pct?: number | null;
    bias_pct?: number | null;
    error_std_pct?: number | null;
    pearson_ic?: number | null;
    spearman_rank_ic?: number | null;
    r_squared?: number | null;
    skill_score_vs_zero?: number | null;
    zero_baseline_rmse_pct?: number | null;
    n: number;
    matched_samples?: number;
    own_n?: number;
    directional_accuracy: number | null;
    directional_n?: number;
    directional_own_n?: number;
    numeric_direction_accuracy?: number | null;
    numeric_direction_count?: number;
    horizon_bars?: number;
  };
  coverage: { signal_count: number; covered_bars: number; total_bars: number; ratio: number | null };
  prediction_quality?: {
    n_direction: number;
    correct_direction: number;
    directional_accuracy: number | null;
    accuracy_ci95: [number, number] | null;
    n_numeric: number;
    mae_pct: number | null;
    rmse_pct: number | null;
    bias_pct: number | null;
    pending_count: number;
    neutral_threshold_pct?: number;
    direction_basis?: string;
    direction_label?: string;
  };
  execution?: { elapsed_seconds?: number | null; items_done?: number | null; items_total?: number | null; failed_items?: number | null; total_tokens?: number | null };
  records?: EventComparisonDetail[];
  event_details?: EventComparisonDetail[];
  formal_metrics?: Record<string, unknown>;
  warnings: string[];
}

export interface ModelComparisonResult {
  schema_version: "arena-model-comparison-v1" | "arena-event-comparison-v1";
  record_schema_version?: "arena-result-record-v1";
  track?: "quant" | "event";
  subject: Pick<ComparisonSubject, "key" | "kind" | "title" | "symbol" | "market"> & { event_time?: string | null };
  market: { symbol: string; market: string; frequency: string; start_at: string; end_at: string; bar_count: number; source_label?: string | null; snapshot_hash?: string | null };
  rules: Partial<Omit<ComparisonRequest, "subject_key" | "run_ids" | "market_source_id">> & Record<string, unknown>;
  models: ComparisonModel[];
  benchmark_curve: ComparisonCurvePoint[];
  bars: Array<{ timestamp: string; open: number; high: number; low: number; close: number; volume?: number | null }>;
  events?: Array<{ event_id: string; title?: string | null; time?: string | null; market?: string | null; symbol?: string | null }>;
  notes: string[];
  privacy?: { market_redacted?: boolean; timestamps_redacted?: boolean };
}

export interface ComparisonPreview { preview_id: string; result: ModelComparisonResult }

export function isModelComparisonResult(value: unknown): value is ModelComparisonResult {
  return Boolean(value && typeof value === "object"
    && ["arena-model-comparison-v1", "arena-event-comparison-v1"].includes(String((value as Record<string, unknown>).schema_version))
    && Array.isArray((value as Record<string, unknown>).models));
}

export function comparisonKindLabel(kind: string): string {
  return ({ event: "事件模型", quant: "量化模型", pronoia: "Pronoia", raw: "独立大模型", raw_model: "独立大模型", external_model: "独立大模型", external_http: "第三方模型", external_service: "第三方模型", provided_analysis: "导入预测", imported_predictions: "导入预测" } as Record<string, string>)[kind] ?? (kind || "模型");
}

function errorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(errorMessage).join("；");
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    return [record.message, record.msg, record.reason, record.hint].filter((part): part is string => typeof part === "string" && Boolean(part)).join("；") || JSON.stringify(detail);
  }
  return "请求未能完成，请重试。";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/arena/model-comparison${path}`, init);
  const contentType = response.headers.get("content-type") ?? "";
  if (path === "/catalog" && (response.status === 404 || contentType.includes("text/html"))) {
    throw new Error("当前页面与 Pronoia 服务版本不同步，模型对比接口尚未启用。请重启本地 Pronoia 服务后重试。");
  }
  if (!contentType.includes("json")) {
    throw new Error("模型对比服务暂未就绪，请刷新后重试。");
  }
  const body = await response.json();
  if (!response.ok) throw new Error(errorMessage(body?.detail ?? body));
  return body as T;
}

export const arenaComparisonApi = {
  catalog: (signal?: AbortSignal) => request<ComparisonCatalog>("/catalog", { signal }),
  preview: (payload: ComparisonRequest, signal?: AbortSignal) => request<ComparisonPreview>("/preview", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload), signal,
  }),
  save: (name: string, previewId: string) => request<ArenaItem>("/save", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name, preview_id: previewId }),
  }),
};
