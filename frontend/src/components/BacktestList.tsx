import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  BarChart3,
  CalendarRange,
  CandlestickChart,
  CheckCircle2,
  ChevronDown,
  CircleDashed,
  Database,
  FileInput,
  Filter,
  Gauge,
  LayoutGrid,
  List,
  Loader2,
  LockKeyhole,
  MessageSquare,
  Newspaper,
  Pause,
  Play,
  Plus,
  RadioTower,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Sparkles,
  Square,
  Trash2,
  TrendingUp,
  Workflow,
  X,
  Zap,
} from "lucide-react";
import { api } from "../api";
import { useStore } from "../store";
import type {
  BTDataset,
  BTMetricDef,
  BTRun,
  BTStatus,
  BTRunner,
  BTStrategyCatalogItem,
  BTStrategySpec,
  ModelLabEventCapabilities,
  ModelLabProfile,
} from "../types";
import { cls, relTime } from "../utils";
import BacktestNav from "./backtest/BacktestNav";
import EventQAEditor, { defaultEventQA, type EventQAConfig, type EventQAStatus } from "./backtest/EventQAEditor";
import EventModelConnectionEditor from "./backtest/EventModelConnectionEditor";
import ExternalEventServiceEditor, { defaultExternalService, serviceConfigError, saveServiceCredentials } from "./backtest/ExternalEventServiceEditor";
import PredictionFileImport from "./PredictionFileImport";
import QuantReturnForecastConfig, { type QuantReturnForecastParameters } from "./backtest/QuantReturnForecastConfig";
import type { SavedModelSetup } from "./backtest/modelLibraryApi";
import {
  EVENT_INPUT_TIME_ZONES,
  formatLocalDateTimeInZone,
  resolveZonedLocalDateTime,
  type DateTimeDisambiguation,
  type EventInputTimeZone,
  type ZonedDateTimeResolution,
} from "../timezone";

export type StrategyType = "event" | "quant" | "signal_import";
type Visibility = "private" | "team" | "arena_safe";
type StatusFilter = "all" | BTStatus;
type ViewMode = "table" | "cards";

interface RunExtension {
  strategy_type?: StrategyType | string | null;
  visibility?: Visibility | string | null;
  protocol_hash?: string | null;
  execution_spec?: Record<string, unknown> | null;
}

const STRATEGY_META: Record<
  StrategyType,
  {
    label: string;
    short: string;
    description: string;
    eyebrow: string;
    className: string;
    icon: (size?: number) => ReactNode;
  }
> = {
  event: {
    label: "事件模型",
    short: "事件实验",
    description: "比较 Pronoia 与独立大模型的事件方向、未来收益率预测及问答表现。",
    eyebrow: "EVENT INTELLIGENCE",
    className: "border-brand/25 bg-brand-soft/55 text-brand",
    icon: (size = 16) => <Newspaper size={size} />,
  },
  quant: {
    label: "量化模型",
    short: "量化实验",
    description: "基于冻结行情预测未来 N 周期收益率；也可单独评测交易策略的收益与风险。",
    eyebrow: "SYSTEMATIC RESEARCH",
    className: "border-jade/25 bg-jade-soft/65 text-jade",
    icon: (size = 16) => <BarChart3 size={size} />,
  },
  signal_import: {
    label: "外部量化模型",
    short: "外部量化实验",
    description: "接入返回目标仓位的外部金融模型 API，密钥只使用环境变量引用。",
    eyebrow: "DECISION ADAPTER",
    className: "border-violet/25 bg-violet-soft/65 text-violet",
    icon: (size = 16) => <FileInput size={size} />,
  },
};

const INPUT_CLASS =
  "min-w-0 max-w-full w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[12.5px] text-ink outline-none transition placeholder:text-faint focus:border-brand/60 focus:ring-2 focus:ring-brand/10 disabled:cursor-not-allowed disabled:bg-edge/35 disabled:text-faint";

const BUTTON_GHOST =
  "inline-flex items-center justify-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[12px] font-medium text-mute transition hover:border-edgeDark hover:bg-edge/35 hover:text-ink disabled:cursor-not-allowed disabled:opacity-50";

function normalizeStrategyType(value: unknown): StrategyType {
  if (value === "quant") return "quant";
  if (value === "api" || value === "external_http" || value === "signal" || value === "signal_import") return "signal_import";
  return "event";
}

function getRunStrategyType(run: BTRun): StrategyType {
  const extended = run as BTRun & RunExtension;
  const config = (run.config ?? {}) as Record<string, unknown>;
  return normalizeStrategyType(extended.strategy_type ?? config.strategy_type ?? config.experiment_type);
}

function getRunVisibility(run: BTRun): Visibility {
  const extended = run as BTRun & RunExtension;
  const config = (run.config ?? {}) as Record<string, unknown>;
  const value = extended.visibility ?? config.visibility;
  return value === "team" || value === "arena_safe" ? value : "private";
}

export function getProtocolHash(run: BTRun): string | null {
  const extended = run as BTRun & RunExtension;
  const config = (run.config ?? {}) as Record<string, unknown>;
  const arenaComparison = config.arena_comparison && typeof config.arena_comparison === "object" && !Array.isArray(config.arena_comparison)
    ? config.arena_comparison as Record<string, unknown>
    : {};
  const value = extended.protocol_hash
    ?? config.protocol_hash
    ?? arenaComparison.comparison_protocol_hash
    ?? extended.comparison_protocol_hash;
  return typeof value === "string" && value.trim() ? value : null;
}

function runHasCompletionWarnings(run?: BTRun): boolean {
  if (!run || run.status !== "done") return false;
  return /warning/i.test(String(run.completion_quality ?? ""))
    || Number(run.warning_count ?? 0) > 0
    || Number(run.invalid_output_count ?? 0) > 0
    || Number(run.voluntary_abstain_count ?? 0) > 0
    || Number(run.insufficient_data_count ?? 0) > 0;
}

function runCompletionWarningCount(run?: BTRun): number {
  if (!run) return 0;
  return Math.max(
    Number(run.warning_count ?? 0),
    Number(run.invalid_output_count ?? 0) + Number(run.voluntary_abstain_count ?? 0) + Number(run.insufficient_data_count ?? 0),
  );
}

function StatusBadge({ status, run }: { status: BTStatus | string; run?: BTRun }) {
  const doneWithWarnings = status === "done" && runHasCompletionWarnings(run);
  const cfg: Record<string, { label: string; className: string; icon: ReactNode }> = {
    pending: {
      label: "待启动",
      className: "border-edgeDark/70 bg-edge/45 text-mute",
      icon: <CircleDashed size={11} />,
    },
    running: {
      label: "运行中",
      className: "border-brand/20 bg-brand-soft text-brand",
      icon: <Loader2 size={11} className="animate-spin" />,
    },
    paused: {
      label: "已暂停",
      className: "border-amber-200 bg-amber-50 text-amber-700",
      icon: <Pause size={11} />,
    },
    done: {
      label: "已完成",
      className: "border-jade/20 bg-jade-soft text-jade",
      icon: <CheckCircle2 size={11} />,
    },
    failed: {
      label: "失败",
      className: "border-rise/20 bg-rise/5 text-rise",
      icon: <X size={11} />,
    },
    cancelled: {
      label: "已取消",
      className: "border-violet/15 bg-violet-soft/70 text-violet",
      icon: <Square size={11} />,
    },
  };
  const item = doneWithWarnings
    ? {
        label: `完成但有警告${runCompletionWarningCount(run) > 0 ? ` · ${runCompletionWarningCount(run)}` : ""}`,
        className: "border-amber-200 bg-amber-50 text-amber-700",
        icon: <AlertTriangle size={11} />,
      }
    : cfg[status] ?? cfg.pending;
  return (
    <span
      className={cls(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10.5px] font-medium",
        item.className,
      )}
    >
      {item.icon}
      {item.label}
    </span>
  );
}

function StrategyBadge({ type, compact = false }: { type: StrategyType; compact?: boolean }) {
  const meta = STRATEGY_META[type];
  return (
    <span
      className={cls(
        "inline-flex items-center gap-1 rounded-md border font-medium",
        compact ? "px-1.5 py-0.5 text-[10px]" : "px-2 py-1 text-[11px]",
        meta.className,
      )}
    >
      {meta.icon(compact ? 10 : 12)}
      {meta.label}
    </span>
  );
}

interface ManualAssetDraft {
  id: string;
  market: string;
  symbol: string;
  analysis_direction: "" | "up" | "down" | "neutral";
  confidence: number | null;
  expected_return_pct: string;
  rationale: string;
}

type DataSourceMode = "managed" | "registered" | "file" | "api";

interface QuantConditionDraft {
  id: string;
  phase: "entry" | "exit";
  field:
    | "price"
    | "bar_return"
    | "return"
    | "moving_average"
    | "ma_cross"
    | "breakout"
    | "amplitude"
    | "volume_ratio"
    | "volatility"
    | "rsi"
    | "bollinger_position"
    | "macd";
  operator: "above" | "below" | "crosses_above" | "crosses_below";
  consecutive_count?: number;
  lookback: number;
  fast_period: number;
  slow_period: number;
  signal_period: number;
  threshold: number;
}

export interface CreateDraft {
  data_source_mode: DataSourceMode;
  market_dataset_id: string;
  data_market: string;
  bar_frequency: string;
  data_asset_type: "index" | "equity" | "futures";
  data_adjustment: string;
  data_calendar: string;
  custom_data_name: string;
  custom_file_path: string;
  custom_api_provider: string;
  custom_api_ref: string;
  custom_secret_env_ref: string;
  strategy_type: StrategyType;
  event_source: "manual" | "registered";
  event_analysis_mode: "platform" | "external_api" | "provided";
  external_model_mode: "profile" | "http";
  prediction_profile_id: string;
  name: string;
  runner: BTRunner | string;
  dataset_id: string;
  prompt_variant: string;
  model_version: string;
  concurrency: number;
  visibility: Visibility;
  event_title: string;
  event_text: string;
  event_actual_value: string;
  event_expected_value: string;
  event_previous_value: string;
  event_value_unit: string;
  asset_return_5d_pct: string;
  asset_return_20d_pct: string;
  benchmark_return_5d_pct: string;
  benchmark_return_20d_pct: string;
  excess_return_5d_pct: string;
  excess_return_20d_pct: string;
  source_url: string;
  event_type: string;
  event_timezone: EventInputTimeZone;
  event_time_disambiguation: DateTimeDisambiguation;
  available_time_disambiguation: DateTimeDisambiguation;
  event_time: string;
  available_time: string;
  event_horizon: string;
  manual_assets: ManualAssetDraft[];
  generate_oracle: boolean;
  quant_asset_class: "index" | "equity" | "futures";
  quant_mode: "return_forecast" | "trading_strategy";
  quant_forecast: QuantReturnForecastParameters;
  quant_conditions: QuantConditionDraft[];
  quant_entry_combinator: "and" | "or";
  quant_exit_combinator: "and" | "or";
  strategy_version: string;
  frequency: string;
  signal_schema: string;
  signal_version: string;
  external_api_ref: string;
  external_header_name: string;
  external_secret_env_ref: string;
  benchmark: string;
  date_from: string;
  date_to: string;
  price_rule: string;
  initial_capital: number;
  transaction_cost_bps: number;
  slippage_bps: number;
  stamp_duty_bps: number;
  other_cost_bps: number;
  minimum_commission: number;
}

export interface CreateRequest {
  name: string;
  runner: BTRunner | string;
  horizon?: string;
  dataset_id?: string;
  dataset_version?: string;
  events_path?: string;
  labels_path?: string;
  prompt_variant?: string;
  model_version?: string;
  concurrency?: number;
  strategy_type: StrategyType;
  strategy_spec?: BTStrategySpec;
  visibility: Visibility;
  protocol_hash?: string;
  execution_spec: Record<string, unknown>;
  config: Record<string, unknown>;
  prediction_profile_id?: string | null;
  event_qa?: EventQAConfig;
  auto_start?: boolean;
}

type WizardStep = 1 | 2 | 3;

interface WizardIssue {
  id: string;
  step: WizardStep;
  message: string;
  fieldId?: string;
}

interface DateBasis {
  start: string | null;
  end: string | null;
  label: string;
  complete: boolean;
  validateBounds: boolean;
}

interface DateTimeResolutionState {
  value: ZonedDateTimeResolution | null;
  error: string | null;
}

const EVENT_ORACLE_MARKETS = ["CN", "US", "HK", "FUTURES"] as const;
const DEFAULT_EVENT_TIME_ZONE: EventInputTimeZone = "Asia/Shanghai";
const RUN_NAME_MAX_LENGTH = 120;
const MANUAL_DATASET_SUFFIX = " · 事件输入";
const STRATEGY_SECRET_ENV_PREFIX = "PRONOIA_STRATEGY_SECRET_";
const DATA_SECRET_ENV_PREFIX = "PRONOIA_DATA_SECRET_";
const STRATEGY_SECRET_ENV_PATTERN = /^PRONOIA_STRATEGY_SECRET_[A-Z0-9_]+$/;
const DATA_SECRET_ENV_PATTERN = /^PRONOIA_DATA_SECRET_[A-Z0-9_]+$/;

function validSecretEnvRef(value: string, kind: "strategy" | "data"): boolean {
  if (!value) return true;
  return (kind === "strategy" ? STRATEGY_SECRET_ENV_PATTERN : DATA_SECRET_ENV_PATTERN).test(value);
}

function secretEnvRefError(value: string, kind: "strategy" | "data"): string | undefined {
  if (validSecretEnvRef(value, kind)) return undefined;
  const prefix = kind === "strategy" ? STRATEGY_SECRET_ENV_PREFIX : DATA_SECRET_ENV_PREFIX;
  return `必须以 ${prefix} 开头，后缀只能包含大写字母、数字和下划线`;
}

function dateOnly(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const match = value.trim().match(/^(\d{4}-\d{2}-\d{2})/);
  return match?.[1] ?? null;
}

function datasetDateBasis(dataset: BTDataset | undefined, label: string): DateBasis {
  const start = dateOnly(dataset?.coverage?.start_at) ?? dateOnly(dataset?.date_range?.min);
  const end = dateOnly(dataset?.coverage?.end_at) ?? dateOnly(dataset?.date_range?.max);
  return {
    start,
    end,
    label,
    complete: Boolean(start && end),
    validateBounds: true,
  };
}

function datasetHasFrozenIdentity(dataset: BTDataset | undefined) {
  return {
    version: Boolean(dataset?.dataset_version || dataset?.version),
    snapshot: Boolean(dataset?.snapshot_hash || dataset?.snapshot),
  };
}

function datasetQualityFlag(dataset: BTDataset | undefined, key: string): unknown {
  const capabilityValue = dataset?.capabilities?.[key];
  if (capabilityValue !== undefined) return capabilityValue;
  const report = dataset?.quality_report;
  return report && typeof report === "object" ? report[key] : undefined;
}

function datasetIsSyntheticDemo(dataset?: BTDataset): boolean {
  if (!dataset) return false;
  const semantic = dataset.semantic_quality;
  const record = semantic && typeof semantic === "object" ? semantic as Record<string, unknown> : {};
  const status = typeof semantic === "string" ? semantic : String(record.status ?? record.level ?? "");
  return /demo|synthetic|mechanism/i.test(status)
    || record.formal_evaluation_eligible === false
    || ["cn_etf_10", "us_etf_10"].includes(dataset.id);
}

function optionalDraftNumber(value: string): number | undefined {
  if (!value.trim()) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

function localDateTime(offsetMinutes = 0, timeZone: string = DEFAULT_EVENT_TIME_ZONE) {
  return formatLocalDateTimeInZone(new Date(Date.now() + offsetMinutes * 60_000), timeZone);
}

function resolveDraftDateTime(value: string, timeZone: string, disambiguation: DateTimeDisambiguation): DateTimeResolutionState {
  if (!value) return { value: null, error: null };
  try {
    return { value: resolveZonedLocalDateTime(value, timeZone, disambiguation), error: null };
  } catch (error) {
    return { value: null, error: error instanceof Error ? error.message : String(error) };
  }
}

function manualDatasetName(runName: string): string {
  const available = RUN_NAME_MAX_LENGTH - MANUAL_DATASET_SUFFIX.length;
  return `${runName.trim().slice(0, available)}${MANUAL_DATASET_SUFFIX}`;
}

function newAsset(index = 0): ManualAssetDraft {
  return {
    id: `${Date.now()}-${index}-${Math.random().toString(36).slice(2, 7)}`,
    market: "CN",
    symbol: "",
    analysis_direction: "",
    confidence: null,
    expected_return_pct: "",
    rationale: "",
  };
}

function newQuantCondition(phase: "entry" | "exit", index = 0): QuantConditionDraft {
  return {
    id: `${phase}-${Date.now()}-${index}-${Math.random().toString(36).slice(2, 7)}`,
    phase,
    field: "volume_ratio",
    operator: phase === "entry" ? "crosses_above" : "crosses_below",
    lookback: 20,
    fast_period: 10,
    slow_period: 30,
    signal_period: 9,
    threshold: phase === "entry" ? 1.5 : 0.8,
    consecutive_count: 1,
  };
}

function quantThresholdUnit(field: QuantConditionDraft["field"]): "price" | "percent" | "multiple" | "index" | "sigma" {
  if (field === "volume_ratio") return "multiple";
  if (field === "rsi") return "index";
  if (field === "bollinger_position") return "sigma";
  if (["bar_return", "moving_average", "ma_cross", "breakout", "return", "amplitude", "volatility", "macd"].includes(field)) return "percent";
  return "price";
}

export function serializeQuantCondition(condition: QuantConditionDraft): Record<string, unknown> {
  const unit = quantThresholdUnit(condition.field);
  return {
    field: condition.field,
    operator: condition.operator,
    lookback: condition.lookback,
    threshold: unit === "percent" ? condition.threshold / 100 : condition.threshold,
    threshold_unit: unit,
    ...(unit === "percent" ? { threshold_decimal: condition.threshold / 100, threshold_display: condition.threshold } : {}),
    ...(["ma_cross", "macd"].includes(condition.field)
      ? { fast_period: condition.fast_period, slow_period: condition.slow_period }
      : {}),
    ...(condition.field === "macd" ? { signal_period: condition.signal_period } : {}),
    ...((condition.consecutive_count ?? 1) > 1 ? { consecutive_count: condition.consecutive_count } : {}),
  };
}

export function quantConditionError(condition: QuantConditionDraft): string | null {
  if (!Number.isInteger(condition.lookback) || condition.lookback < 1 || condition.lookback > 10_000) return "回看周期需为 1–10000 的整数";
  if (!Number.isFinite(condition.threshold)) return "阈值需为有限数值";
  const consecutiveCount = condition.consecutive_count ?? 1;
  if (!Number.isInteger(consecutiveCount) || consecutiveCount < 1 || consecutiveCount > 10_000) return "连续变化次数需为 1–10000 的整数";
  if (["ma_cross", "macd"].includes(condition.field)) {
    if (!Number.isInteger(condition.fast_period) || !Number.isInteger(condition.slow_period) || condition.fast_period < 1 || condition.fast_period >= condition.slow_period || condition.slow_period > 10_000) {
      return "均线周期需满足 1 ≤ 快线 < 慢线 ≤ 10000";
    }
  }
  if (condition.field === "macd" && (!Number.isInteger(condition.signal_period) || condition.signal_period < 1 || condition.signal_period > 10_000)) return "信号周期需为 1–10000 的整数";
  if (condition.field === "rsi" && (condition.threshold < 0 || condition.threshold > 100)) return "RSI 阈值需在 0–100";
  if (["volume_ratio", "amplitude", "volatility"].includes(condition.field) && condition.threshold < 0) return "该因子阈值不能为负";
  return null;
}

function endpointValidationError(value: string, allowPrivate = false): string | null {
  const raw = value.trim();
  if (!raw) return allowPrivate ? "请输入预测接口地址。" : "请输入预测服务的完整 HTTPS 地址。";
  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return "Endpoint 格式无效。";
  }
  if (!["http:", "https:"].includes(parsed.protocol)) return "接口地址必须使用 HTTP 或 HTTPS 协议。";
  if (parsed.username || parsed.password) return "Endpoint 不能内嵌用户名、密码或 token。";
  if (parsed.hash) return "接口地址不能包含 # 片段。";
  const hostname = parsed.hostname.replace(/^\[|\]$/g, "").replace(/\.$/, "").toLowerCase();
  if (!hostname) return "Endpoint 缺少主机名。";
  const localName = hostname === "localhost" || hostname === "localhost.localdomain" || hostname.endsWith(".local");
  const v4 = hostname.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/)?.slice(1).map(Number);
  const localV4 = Boolean(v4 && (v4[0] === 0 || v4[0] === 10 || v4[0] === 127 || v4[0] >= 224 || (v4[0] === 100 && v4[1] >= 64 && v4[1] <= 127) || (v4[0] === 169 && v4[1] === 254) || (v4[0] === 172 && v4[1] >= 16 && v4[1] <= 31) || (v4[0] === 192 && (v4[1] === 0 || v4[1] === 168)) || (v4[0] === 198 && (v4[1] === 18 || v4[1] === 19 || (v4[1] === 51 && v4[2] === 100))) || (v4[0] === 203 && v4[1] === 0 && v4[2] === 113)));
  const localV6 = hostname.includes(":") && (hostname === "::" || hostname === "::1" || /^(fc|fd|fe[89ab]|ff|2001:db8|::ffff:)/i.test(hostname));
  if (parsed.protocol === "http:") return allowPrivate && (localName || localV4 || localV6) ? null : "公网接口必须使用 HTTPS；只有服务端已允许的本机或私网接口可以使用 HTTP。";
  if (allowPrivate) return null;
  if (hostname === "localhost" || hostname.endsWith(".localhost") || hostname.endsWith(".local") || hostname.endsWith(".localdomain") || hostname.endsWith(".internal") || hostname.endsWith(".lan") || hostname.endsWith(".home") || hostname.endsWith(".corp")) {
    return "不允许 localhost、.local 或内部域名。";
  }
  if ([".test", ".invalid", ".example", ".onion"].some((suffix) => hostname.endsWith(suffix))) {
    return "该域名后缀不是可由回测服务访问的公网 endpoint。";
  }
  const ipv4 = hostname.match(/^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/);
  if (ipv4) {
    const octets = ipv4.slice(1).map(Number);
    if (octets.some((part) => part < 0 || part > 255)) return "IP 地址格式无效。";
    const [a, b, c] = octets;
    const nonPublic =
      a === 0 || a === 10 || a === 127 || a >= 224 ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && b === 0) ||
      (a === 192 && b === 168) ||
      (a === 192 && b === 88 && c === 99) ||
      (a === 198 && (b === 18 || b === 19)) ||
      (a === 198 && b === 51 && c === 100) ||
      (a === 203 && b === 0 && c === 113);
    if (nonPublic) return "不允许私网、回环、链路本地或保留 IP。";
  } else if (hostname.includes(":")) {
    const compact = hostname.replace(/^0+/, "");
    if (hostname === "::" || hostname === "::1" || compact.startsWith("fc") || compact.startsWith("fd") || /^fe[89ab]/.test(compact) || compact.startsWith("ff") || compact.startsWith("2001:db8") || compact.startsWith("::ffff:")) {
      return "不允许私网、回环或链路本地 IPv6 地址。";
    }
  } else if (!hostname.includes(".")) {
    return "不允许单标签内部主机名；请使用公网域名。";
  }
  return null;
}

export function syncMarketDatasetDraft(
  draft: CreateDraft,
  dataset: BTDataset | undefined,
  datasetId = dataset?.id ?? draft.market_dataset_id,
): CreateDraft {
  if (!dataset) return { ...draft, market_dataset_id: datasetId };
  const datasetFrequency = String(dataset.frequency ?? "").trim();
  const frequency = datasetFrequency || draft.bar_frequency || draft.frequency || "1d";
  const assetType = dataset.asset_type;
  return {
    ...draft,
    market_dataset_id: dataset.id || datasetId,
    data_market: dataset.markets?.find((item) => String(item).trim()) ?? draft.data_market,
    bar_frequency: frequency,
    frequency,
    data_asset_type: assetType === "index" || assetType === "equity" || assetType === "futures"
      ? assetType
      : draft.data_asset_type,
    data_adjustment: String(dataset.adjustment ?? "").trim() || draft.data_adjustment,
    data_calendar: String(dataset.calendar ?? "").trim() || draft.data_calendar,
  };
}

export function createDefaultDraft(type: StrategyType, datasets: BTDataset[]): CreateDraft {
  const now = new Date();
  const stamp = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")} ${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}`;
  const defaultMarketDataset = datasets.find((item) => (
    type === "event" ? marketDatasetHasOhlc(item) : marketDatasetCanExecutePortfolio(item)
  ));
  const draft: CreateDraft = {
    data_source_mode: type === "event" ? "managed" : "registered",
    market_dataset_id: defaultMarketDataset?.id ?? "",
    data_market: "CN",
    bar_frequency: "1d",
    data_asset_type: "index",
    data_adjustment: "qfq",
    data_calendar: "exchange",
    custom_data_name: "",
    custom_file_path: "",
    custom_api_provider: "",
    custom_api_ref: "",
    custom_secret_env_ref: "",
    strategy_type: type,
    event_source: "manual",
    event_analysis_mode: "platform",
    external_model_mode: "profile",
    prediction_profile_id: "",
    name: `${STRATEGY_META[type].short} · ${stamp}`,
    runner: type === "event" ? "team_full" : type === "quant" ? "return_forecast" : "external_http",
    dataset_id: (type === "event" ? datasets.find((item) => item.dataset_kind !== "market") : datasets.find((item) => item.decision_ready))?.id ?? "",
    prompt_variant: "v0",
    model_version: "platform-default",
    concurrency: 4,
    visibility: "private",
    event_title: "",
    event_text: "",
    event_actual_value: "",
    event_expected_value: "",
    event_previous_value: "",
    event_value_unit: "",
    asset_return_5d_pct: "",
    asset_return_20d_pct: "",
    benchmark_return_5d_pct: "",
    benchmark_return_20d_pct: "",
    excess_return_5d_pct: "",
    excess_return_20d_pct: "",
    source_url: "",
    event_type: "news",
    event_timezone: DEFAULT_EVENT_TIME_ZONE,
    event_time_disambiguation: "earlier",
    available_time_disambiguation: "earlier",
    event_time: localDateTime(-5, DEFAULT_EVENT_TIME_ZONE),
    available_time: localDateTime(0, DEFAULT_EVENT_TIME_ZONE),
    event_horizon: "t3",
    manual_assets: [newAsset()],
    generate_oracle: true,
    quant_asset_class: "index",
    quant_mode: "return_forecast",
    quant_forecast: { method: "historical_mean", lookback: 20, horizon_bars: 3 },
    quant_conditions: [newQuantCondition("entry"), newQuantCondition("exit", 1)],
    quant_entry_combinator: "and",
    quant_exit_combinator: "and",
    strategy_version: "",
    frequency: "1d",
    signal_schema: "decision.v1",
    signal_version: "",
    external_api_ref: "",
    external_header_name: "Authorization",
    external_secret_env_ref: "",
    benchmark: type === "event" ? "dataset_default" : "dataset_asset_buy_hold",
    date_from: "",
    date_to: "",
    price_rule: type === "event" ? "event_close" : "next_open",
    initial_capital: 1_000_000,
    transaction_cost_bps: 3,
    slippage_bps: 2,
    stamp_duty_bps: 0,
    other_cost_bps: 0,
    minimum_commission: 0,
  };
  // Event experiments use the managed daily Oracle. Portfolio experiments must
  // inherit every data-basis field from the frozen market version they select.
  return type === "event" ? draft : syncMarketDatasetDraft(draft, defaultMarketDataset);
}

function formatTeamFullEstimate(eventCount: number, requestedConcurrency: number): string {
  if (eventCount <= 0) return "单事件通常约 2–8 分钟";
  // The backend intentionally caps team_full at two concurrent events for
  // stability.  The range is a capacity estimate, not a completion promise.
  const effectiveConcurrency = Math.min(2, Math.max(1, requestedConcurrency || 1));
  const batches = Math.ceil(eventCount / effectiveConcurrency);
  const minMinutes = batches * 2;
  const maxMinutes = batches * 8;
  const display = (minutes: number) => minutes < 60
    ? `${minutes} 分钟`
    : `${(minutes / 60).toFixed(minutes % 60 === 0 ? 0 : 1)} 小时`;
  return `${eventCount} 条约 ${display(minMinutes)}–${display(maxMinutes)}`;
}

export function datasetState(dataset?: BTDataset): "available" | "pending" | "failed" {
  if (!dataset) return "pending";
  const status = String(dataset.status ?? dataset.oracle_status ?? "").toLowerCase();
  const quality = String(dataset.quality_status ?? "").toLowerCase();
  if (["invalid", "unavailable", "failed"].includes(status) || quality === "failed") return "failed";
  if (status === "available" && ["passed", "partial", "unverified", ""].includes(quality)) return "available";
  // Legacy registered datasets predate status/quality fields. A concrete path is usable
  // for event input, but is not silently promoted to an OHLC market benchmark below.
  if (!dataset.status && dataset.path) return "available";
  return "pending";
}

export function marketDatasetHasOhlc(dataset?: BTDataset): boolean {
  if (!dataset || dataset.dataset_kind !== "market" || datasetState(dataset) !== "available") return false;
  return dataset.capabilities?.ohlc === true;
}

export function marketDatasetHasExactlyOneSymbol(dataset?: BTDataset): boolean {
  if (!dataset) return false;
  const declaredCounts: number[] = [];
  const addArrayCount = (value: unknown) => {
    if (!Array.isArray(value)) return;
    const symbols = new Set(value.map((item) => String(item).trim()).filter(Boolean));
    // Empty arrays are legacy/default placeholders, not proof of a zero-symbol
    // snapshot. Another catalogue field may still carry the real declaration.
    if (symbols.size > 0) declaredCounts.push(symbols.size);
  };
  addArrayCount((dataset as BTDataset & { symbols?: unknown }).symbols);
  const coverageSymbols = dataset.coverage?.symbols;
  addArrayCount(coverageSymbols);
  if (typeof coverageSymbols === "number" && Number.isFinite(coverageSymbols) && coverageSymbols >= 0) {
    declaredCounts.push(coverageSymbols);
  }
  // Empty legacy arrays carry no information. Every concrete declaration must
  // agree that the frozen dataset contains exactly one symbol.
  return declaredCounts.length > 0 && declaredCounts.every((value) => value === 1);
}

export function marketDatasetCanExecutePortfolio(dataset?: BTDataset): boolean {
  if (!marketDatasetHasOhlc(dataset)) return false;
  const capabilities = dataset?.capabilities;
  if (capabilities?.portfolio_execution !== true) return false;
  if (capabilities.single_asset_execution_ready === false || capabilities.multi_symbol_data === true) return false;
  return marketDatasetHasExactlyOneSymbol(dataset);
}

export function BacktestCreateModal({
  open,
  presentation = "modal",
  initialType,
  initialEventAnalysisMode,
  initialExternalModelMode,
  initialQuantMode,
  savedModelSetup,
  savedModelName,
  allowedTypes,
  onClose,
  onCreate,
}: {
  open: boolean;
  presentation?: "modal" | "inline";
  initialType: StrategyType;
  initialEventAnalysisMode?: CreateDraft["event_analysis_mode"];
  initialExternalModelMode?: CreateDraft["external_model_mode"];
  initialQuantMode?: CreateDraft["quant_mode"];
  savedModelSetup?: SavedModelSetup;
  savedModelName?: string;
  allowedTypes?: StrategyType[];
  onClose: () => void;
  onCreate: (data: CreateRequest) => Promise<void>;
}) {
  const [datasets, setDatasets] = useState<BTDataset[]>([]);
  const [datasetsLoading, setDatasetsLoading] = useState(false);
  const [datasetsErr, setDatasetsErr] = useState<string | null>(null);
  const [strategyCatalog, setStrategyCatalog] = useState<BTStrategyCatalogItem[]>([]);
  const [form, setForm] = useState<CreateDraft>(() => createDefaultDraft(initialType, []));
  const [externalService, setExternalService] = useState(defaultExternalService);
  const [providedSource, setProvidedSource] = useState<"file" | "dataset">("file");
  const [predictionImport, setPredictionImport] = useState<{ id: string; sourceId: string; name: string; horizon: string } | null>(null);
  const [activeStep, setActiveStep] = useState<1 | 2 | 3>(1);
  const [validationRevealed, setValidationRevealed] = useState(false);
  const [profiles, setProfiles] = useState<ModelLabProfile[]>([]);
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [profilesError, setProfilesError] = useState<string | null>(null);
  const [modelOptionsRevision, setModelOptionsRevision] = useState(0);
  const [eventCapabilities, setEventCapabilities] = useState<ModelLabEventCapabilities | null>(null);
  const [qaConfig, setQaConfig] = useState<EventQAConfig>(defaultEventQA);
  const [qaStatus, setQaStatus] = useState<EventQAStatus>({ issues: [], answerCalls: 0, judgeCalls: 0, totalCalls: 0, highVolume: false });
  const [submitting, setSubmitting] = useState(false);
  const [errorMsg, setErrorMsg] = useState<string | null>(null);
  const [oracleFailure, setOracleFailure] = useState<string | null>(null);
  const [preparedDataset, setPreparedDataset] = useState<{
    signature: string;
    id: string;
    name: string;
    dataset_version?: string | null;
    version?: string | null;
  } | null>(null);
  const [dateEdited, setDateEdited] = useState({ from: false, to: false });
  const [savedPredictionFilePath, setSavedPredictionFilePath] = useState("");
  const validationSummaryRef = useRef<HTMLDivElement | null>(null);
  const submitLock = useRef(false);
  const serviceCredentialCache = useRef<{ signature: string; headers: Record<string, string> } | null>(null);

  useEffect(() => {
    if (!open) return;
    const initialDraft = createDefaultDraft(initialType, datasets);
    if (initialExternalModelMode) initialDraft.external_model_mode = initialExternalModelMode;
    if (initialType === "quant" && initialQuantMode) initialDraft.quant_mode = initialQuantMode;
    if (initialType === "event" && initialEventAnalysisMode) {
      initialDraft.event_analysis_mode = initialEventAnalysisMode;
      initialDraft.runner = initialEventAnalysisMode === "platform"
        ? "team_full"
        : initialEventAnalysisMode === "external_api"
          ? initialDraft.external_model_mode === "http" ? "external_http" : "raw_model"
          : "provided_analysis";
      if (initialEventAnalysisMode === "provided") initialDraft.event_source = "registered";
    }
    if (savedModelSetup) {
      const spec = savedModelSetup.strategy_spec;
      initialDraft.name = `${(savedModelName || "模型测试").slice(0, 80)} · ${new Date().toLocaleString("sv-SE").slice(0, 16)}`;
      initialDraft.runner = savedModelSetup.runner;
      initialDraft.prediction_profile_id = savedModelSetup.profile_id === "__platform_default__" ? "" : savedModelSetup.profile_id || "";
      if (initialType === "event") {
        initialDraft.event_source = "registered";
        initialDraft.event_analysis_mode = savedModelSetup.kind === "pronoia" ? "platform" : savedModelSetup.kind === "imported_predictions" ? "provided" : "external_api";
        initialDraft.external_model_mode = savedModelSetup.kind === "external_service" ? "http" : "profile";
      } else {
        initialDraft.quant_mode = spec?.kind === "return_forecast" ? "return_forecast" : "trading_strategy";
        if (spec?.kind === "return_forecast") initialDraft.quant_forecast = { method: "historical_mean", lookback: Number(spec.parameters?.lookback ?? 20), horizon_bars: Number(spec.parameters?.horizon_bars ?? 3) };
        initialDraft.strategy_version = String(spec?.version || "");
        initialDraft.external_api_ref = String(spec?.endpoint || spec?.api_ref || "");
      }
    }
    setForm(initialDraft);
    setSavedPredictionFilePath(String(savedModelSetup?.strategy_spec?.path || ""));
    setExternalService(savedModelSetup?.strategy_spec?.endpoint ? { ...defaultExternalService(), endpoint: String(savedModelSetup.strategy_spec.endpoint), version: String(savedModelSetup.strategy_spec.version || ""), timeout_seconds: Number(savedModelSetup.strategy_spec.timeout_seconds || 30), auth_mode: "none" } : defaultExternalService());
    serviceCredentialCache.current = null;
    setProvidedSource("file");
    setPredictionImport(null);
    setActiveStep(1);
    setValidationRevealed(false);
    setQaConfig(defaultEventQA());
    setQaStatus({ issues: [], answerCalls: 0, judgeCalls: 0, totalCalls: 0, highVolume: false });
    setErrorMsg(null);
    setOracleFailure(null);
    setPreparedDataset(null);
    setDateEdited({ from: false, to: false });
    setDatasetsLoading(true);
    setDatasetsErr(null);
    let alive = true;
    api
      .btListDatasets()
      .then((items) => {
        if (!alive) return;
        const next = items ?? [];
        setDatasets(next);
        setForm((current) => {
          const marketDatasetReady = current.strategy_type === "event"
            ? marketDatasetHasOhlc
            : marketDatasetCanExecutePortfolio;
          const currentMarketDataset = next.find((item) =>
            item.id === current.market_dataset_id && marketDatasetReady(item),
          );
          const selectedMarketDataset = currentMarketDataset
            ?? next.find(marketDatasetReady);
          const marketDraft = current.strategy_type === "event"
            ? { ...current, market_dataset_id: selectedMarketDataset?.id ?? "" }
            : syncMarketDatasetDraft(current, selectedMarketDataset, selectedMarketDataset?.id ?? "");
          return {
            ...marketDraft,
            dataset_id:
            current.dataset_id && next.some((item) =>
              item.id === current.dataset_id && (current.strategy_type === "event" || item.decision_ready),
            )
              ? current.dataset_id
              : (current.strategy_type === "event" ? next.find((item) => item.dataset_kind !== "market") : next.find((item) => item.decision_ready))?.id ?? "",
          };
        });
      })
      .catch((error) => {
        if (alive) setDatasetsErr(error instanceof Error ? error.message : String(error));
      })
      .finally(() => {
        if (alive) setDatasetsLoading(false);
      });
    void api.btStrategies()
      .then((response) => { if (alive) setStrategyCatalog(response.items ?? []); })
      .catch(() => { if (alive) setStrategyCatalog([]); });
    return () => {
      alive = false;
    };
    // datasets intentionally stays cached between openings.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialType, initialEventAnalysisMode, initialExternalModelMode, initialQuantMode, savedModelSetup, savedModelName]);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setProfilesLoading(true);
    setProfilesError(null);
    setEventCapabilities(null);
    void api.modelLabProfiles().then((response) => {
      if (!alive) return;
      const active = response.items.filter((profile) => profile.is_active);
      setProfiles(active);
      setForm((current) => ({ ...current, prediction_profile_id: savedModelSetup ? current.prediction_profile_id : active.some((profile) => profile.id === current.prediction_profile_id) ? current.prediction_profile_id : active[0]?.id ?? "" }));
    }).catch((reason) => { if (alive) setProfilesError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (alive) setProfilesLoading(false); });
    void api.modelLabEventCapabilities().then((response) => { if (alive) setEventCapabilities(response); })
      .catch(() => { if (alive) setEventCapabilities(null); });
    return () => {
      alive = false;
    };
  }, [open, modelOptionsRevision]);

  useEffect(() => {
    if (!open || presentation === "inline") return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !submitting) onClose();
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [open, onClose, presentation, submitting]);

  const selectedDataset = useMemo(
    () => datasets.find((dataset) => dataset.id === form.dataset_id),
    [datasets, form.dataset_id],
  );
  const selectedMarketDataset = useMemo(
    () => datasets.find((dataset) => dataset.id === form.market_dataset_id),
    [datasets, form.market_dataset_id],
  );

  const usesManualEvent = form.strategy_type === "event" && form.event_source === "manual";
  const quantForecast = form.strategy_type === "quant" && form.quant_mode === "return_forecast";
  const savedQuantFile = savedModelSetup?.category === "quant" && (savedModelSetup.strategy_spec?.kind === "signal_file" || savedModelSetup.strategy_spec?.source === "signal_file" || savedModelSetup.strategy_spec?.adapter === "signal_file");
  const teamFullEventCount = usesManualEvent
    ? Math.max(1, form.manual_assets.length)
    : Number(selectedDataset?.total_events ?? 0);
  const teamFullEstimate = formatTeamFullEstimate(teamFullEventCount, form.concurrency);
  const eventTimeResolution = useMemo(
    () => resolveDraftDateTime(form.event_time, form.event_timezone, form.event_time_disambiguation),
    [form.event_time, form.event_time_disambiguation, form.event_timezone],
  );
  const availableTimeResolution = useMemo(
    () => resolveDraftDateTime(form.available_time, form.event_timezone, form.available_time_disambiguation),
    [form.available_time, form.available_time_disambiguation, form.event_timezone],
  );
  const availabilityValid = !usesManualEvent || !eventTimeResolution.value || !availableTimeResolution.value ||
    availableTimeResolution.value.instantMs >= eventTimeResolution.value.instantMs;
  const managedMarketSupported =
    form.data_source_mode === "managed" &&
    form.bar_frequency === "1d" &&
    (form.strategy_type !== "event" || EVENT_ORACLE_MARKETS.includes(form.data_market as typeof EVENT_ORACLE_MARKETS[number]));
  const registeredMarketReady = form.data_source_mode === "registered" && marketDatasetHasOhlc(selectedMarketDataset);
  const dataBenchmarkReady = form.strategy_type === "event" ? managedMarketSupported : registeredMarketReady;
  const externalHttp = form.event_analysis_mode === "external_api" && form.external_model_mode === "http";
  const savedPredictionModel = form.event_analysis_mode === "external_api" && form.external_model_mode === "profile";
  const predictionProfile = savedPredictionModel ? profiles.find((profile) => profile.id === form.prediction_profile_id) : undefined;
  const variantProfile = savedModelSetup?.kind === "pronoia" && savedModelSetup.profile_id !== "__platform_default__" ? profiles.find((profile) => profile.id === savedModelSetup.profile_id) : undefined;
  const usesVariantProfile = savedModelSetup?.kind === "pronoia" && Boolean(savedModelSetup.profile_id) && savedModelSetup.profile_id !== "__platform_default__";
  const platformModelInfo = usesVariantProfile ? variantProfile ? { available: variantProfile.secret_configured, name: variantProfile.name, model_id: variantProfile.model_id } : null : eventCapabilities?.platform_default ?? null;
  const qaModelsSummary = qaConfig.enabled ? qaConfig.variants.map((variant) => variant === "pronoia"
    ? `Pronoia（${platformModelInfo?.model_id ?? "基模待配置"}）`
    : profiles.find((profile) => profile.id === qaConfig.candidate_profile_id)?.name ?? "外部问答模型待选择").join("\n") || "尚未选择" : undefined;
  const scoringModelName = qaConfig.judge_profile_id === "__platform_default__"
    ? eventCapabilities?.platform_default.model_id ?? "平台基模待配置"
    : profiles.find((profile) => profile.id === qaConfig.judge_profile_id)?.name ?? "尚未选择评分模型";
  const scoringSummary = qaConfig.enabled ? qaConfig.scoring_mode === "manual" ? "人工评分"
    : `${scoringModelName} · ${qaConfig.scoring_mode === "mixed" ? "自动评分后人工复核" : "自动评分"}` : undefined;
  const platformPrediction = form.event_analysis_mode === "platform";
  const chatPrediction = platformPrediction || savedPredictionModel;
  const usesExternalStrategy =
    form.strategy_type === "signal_import" ||
    (form.strategy_type === "event" && externalHttp);
  const eventExternalAvailable = strategyCatalog.some((item) =>
    item.type === "event" && String(item.adapter ?? item.id) === "external_http" && item.status === "available",
  );
  const externalEndpointError = usesExternalStrategy ? endpointValidationError(form.strategy_type === "event" ? externalService.endpoint : form.external_api_ref, eventCapabilities?.allow_private_model_endpoints === true) : null;
  const importedPredictionReady = predictionImport?.id === form.dataset_id && predictionImport?.horizon === form.event_horizon;
  const apiAdapterCatalog = strategyCatalog.find((item) =>
    item.type === "api" && String(item.adapter ?? item.id) === "external_http",
  );

  const dateBasis = useMemo<DateBasis>(() => {
    if (form.strategy_type === "event" && form.event_source === "manual") {
      const availableDate = dateOnly(form.available_time);
      return {
        start: availableDate,
        end: availableDate,
        label: "信息可得日",
        complete: Boolean(availableDate),
        validateBounds: false,
      };
    }
    if (form.strategy_type === "event") return datasetDateBasis(selectedDataset, "评测事件集");
    return datasetDateBasis(selectedMarketDataset, "冻结市场数据版本");
  }, [form.available_time, form.event_source, form.strategy_type, selectedDataset, selectedMarketDataset]);

  useEffect(() => {
    if (!open) return;
    setForm((current) => {
      const dateFrom = dateEdited.from ? current.date_from : dateBasis.start ?? "";
      const dateTo = dateEdited.to ? current.date_to : dateBasis.end ?? "";
      if (dateFrom === current.date_from && dateTo === current.date_to) return current;
      return { ...current, date_from: dateFrom, date_to: dateTo };
    });
  }, [dateBasis.end, dateBasis.start, dateEdited.from, dateEdited.to, open]);

  const dateSelectionOrdered = !form.date_from || !form.date_to || form.date_from <= form.date_to;
  const dateSelectionStartsAfterCoverage = Boolean(
    dateBasis.validateBounds && dateBasis.end && form.date_from && form.date_from > dateBasis.end,
  );
  const dateSelectionEndsBeforeCoverage = Boolean(
    dateBasis.validateBounds && dateBasis.start && form.date_to && form.date_to < dateBasis.start,
  );
  const dateSelectionHasNoOverlap = dateSelectionOrdered && (
    dateSelectionStartsAfterCoverage || dateSelectionEndsBeforeCoverage
  );
  const dateSelectionIsClipped = Boolean(
    dateBasis.validateBounds &&
    dateBasis.complete &&
    dateSelectionOrdered &&
    !dateSelectionHasNoOverlap &&
    (
      (form.date_from && dateBasis.start && form.date_from < dateBasis.start) ||
      (form.date_to && dateBasis.end && form.date_to > dateBasis.end)
    ),
  );
  const appliedDateFrom = dateBasis.start && form.date_from && form.date_from > dateBasis.start
    ? form.date_from
    : dateBasis.start;
  const appliedDateTo = dateBasis.end && form.date_to && form.date_to < dateBasis.end
    ? form.date_to
    : dateBasis.end;
  const dateIntersectionNotice = form.strategy_type !== "event"
    ? dateSelectionHasNoOverlap
      ? `所选区间与${dateBasis.label}完全不重叠，无法开始测试。`
      : dateSelectionIsClipped && appliedDateFrom && appliedDateTo
        ? `所选区间部分超出${dateBasis.label}覆盖范围；实际仅执行有数据覆盖的交集区间：${appliedDateFrom} → ${appliedDateTo}。`
        : null
    : null;

  const validationIssues: WizardIssue[] = [];
  const pushIssue = (issue: WizardIssue) => {
    const eventInputIssue = form.strategy_type === "event" && issue.step === 2 && !/^(external-|event-api-|prediction-profile|prediction-import|qa-|platform-api)/.test(issue.id);
    if (!validationIssues.some((item) => item.id === issue.id)) validationIssues.push(eventInputIssue ? { ...issue, step: 1 } : issue);
  };

  if (form.strategy_type === "event" && form.data_source_mode !== "managed") {
    pushIssue({
      id: "event-registered-oracle-unavailable",
      step: 1,
      message: "事件评估目前仅支持系统获取的日线行情，暂不支持上传的多资产行情",
      fieldId: "create-data-source",
    });
  } else if (form.data_source_mode === "managed") {
    if (form.strategy_type !== "event") {
      pushIssue({ id: "managed-portfolio-unavailable", step: 1, message: "量化回测与外部量化服务需要选择已保存的历史行情版本", fieldId: "create-data-source" });
    } else if (!managedMarketSupported) {
      pushIssue({ id: "managed-market-unavailable", step: 1, message: "当前系统行情不支持所选市场或频率", fieldId: "create-data-source" });
    }
  } else if (form.data_source_mode === "registered") {
    if (!selectedMarketDataset) {
      pushIssue({ id: "market-dataset-missing", step: 1, message: "请选择冻结市场数据版本", fieldId: "create-market-dataset" });
    } else {
      const frozen = datasetHasFrozenIdentity(selectedMarketDataset);
      if (datasetState(selectedMarketDataset) !== "available") {
        pushIssue({ id: "market-dataset-unavailable", step: 1, message: "所选市场数据尚未通过质量校验", fieldId: "create-market-dataset" });
      }
      if (selectedMarketDataset.capabilities?.ohlc !== true) {
        pushIssue({ id: "market-dataset-no-ohlc", step: 1, message: "所选行情尚未通过开盘、最高、最低、收盘价校验", fieldId: "create-market-dataset" });
      }
      if (!frozen.version) {
        pushIssue({ id: "market-dataset-no-version", step: 1, message: "所选市场数据缺少冻结版本号", fieldId: "create-market-dataset" });
      }
      if (!frozen.snapshot) {
        pushIssue({ id: "market-dataset-no-snapshot", step: 1, message: "所选市场数据缺少快照哈希", fieldId: "create-market-dataset" });
      }
      const supportedFrequencies = selectedMarketDataset.capabilities?.supported_frequencies;
      if (
        (selectedMarketDataset.frequency && selectedMarketDataset.frequency !== form.bar_frequency) ||
        (supportedFrequencies?.length && !supportedFrequencies.includes(form.bar_frequency))
      ) {
        pushIssue({ id: "market-frequency-mismatch", step: 1, message: `所选版本不支持 ${form.bar_frequency} 频率`, fieldId: "create-market-dataset" });
      }
      if (form.strategy_type !== "event") {
        if (datasetQualityFlag(selectedMarketDataset, "portfolio_execution") !== true) {
          pushIssue({ id: "market-no-portfolio", step: 1, message: "所选版本未声明可用于组合撮合", fieldId: "create-market-dataset" });
        }
        const invalidSymbolUniverse = datasetQualityFlag(selectedMarketDataset, "multi_symbol_data") === true
          || !marketDatasetHasExactlyOneSymbol(selectedMarketDataset);
        if (datasetQualityFlag(selectedMarketDataset, "single_asset_execution_ready") === false || invalidSymbolUniverse) {
          pushIssue({ id: "market-not-single-asset", step: 1, message: "当前组合引擎仅支持已就绪的单标的数据版本", fieldId: "create-market-dataset" });
        }
      }
    }
  } else {
    pushIssue({ id: "data-source-not-frozen", step: 1, message: "请先将文件或 API 数据冻结为可用版本", fieldId: "create-data-source" });
  }

  if (!form.name.trim()) {
    pushIssue({ id: "name-missing", step: 2, message: "请填写实验名称", fieldId: "create-experiment-name" });
  } else if (form.name.trim().length > RUN_NAME_MAX_LENGTH) {
    pushIssue({ id: "name-too-long", step: 2, message: `实验名称不能超过 ${RUN_NAME_MAX_LENGTH} 个字符`, fieldId: "create-experiment-name" });
  }

  if (usesManualEvent) {
    if (!form.event_title.trim()) pushIssue({ id: "event-title", step: 2, message: "请填写事件标题", fieldId: "create-event-title" });
    if (!form.event_text.trim()) pushIssue({ id: "event-text", step: 2, message: "请填写事件正文或事实摘要", fieldId: "create-event-text" });
    if (!form.event_time) pushIssue({ id: "event-time", step: 2, message: "请选择事件发生时间", fieldId: "create-event-time" });
    if (!form.available_time) pushIssue({ id: "available-time", step: 2, message: "请选择信息可得时间", fieldId: "create-available-time" });
    if (form.event_time && eventTimeResolution.error) {
      pushIssue({ id: "event-time-zone", step: 2, message: `事件发生时间：${eventTimeResolution.error}`, fieldId: "create-event-time" });
    }
    if (form.available_time && availableTimeResolution.error) {
      pushIssue({ id: "available-time-zone", step: 2, message: `信息可得时间：${availableTimeResolution.error}`, fieldId: "create-available-time" });
    }
    if (form.event_time && form.available_time && !availabilityValid) {
      pushIssue({ id: "available-before-event", step: 2, message: "信息可得时间不能早于事件发生时间", fieldId: "create-available-time" });
    }
    if (!form.manual_assets.length) pushIssue({ id: "manual-assets", step: 2, message: "请至少添加一个关联资产", fieldId: "create-manual-assets" });
    form.manual_assets.forEach((asset, index) => {
      if (!asset.market.trim()) pushIssue({ id: `asset-market-${asset.id}`, step: 2, message: `资产 ${index + 1} 缺少市场`, fieldId: `create-asset-${asset.id}-market` });
      if (!asset.symbol.trim()) pushIssue({ id: `asset-symbol-${asset.id}`, step: 2, message: `资产 ${index + 1} 缺少标的代码`, fieldId: `create-asset-${asset.id}-symbol` });
      if (form.generate_oracle && !EVENT_ORACLE_MARKETS.includes(asset.market as typeof EVENT_ORACLE_MARKETS[number])) {
        pushIssue({ id: `asset-oracle-market-${asset.id}`, step: 2, message: `资产 ${index + 1} 所属市场暂不支持自动获取评估行情`, fieldId: `create-asset-${asset.id}-market` });
      }
      if (form.event_analysis_mode === "provided") {
        if (!["up", "down"].includes(asset.analysis_direction)) {
          pushIssue({ id: `asset-direction-${asset.id}`, step: 2, message: `资产 ${index + 1} 请明确选择看涨或看跌`, fieldId: `create-asset-${asset.id}-direction` });
        }
        if (asset.confidence === null || !Number.isFinite(asset.confidence) || asset.confidence < 0 || asset.confidence > 100) {
          pushIssue({ id: `asset-confidence-${asset.id}`, step: 2, message: `资产 ${index + 1} 尚未确认 0–100 的置信度`, fieldId: `create-asset-${asset.id}-confidence` });
        }
        if (!asset.rationale.trim()) {
          pushIssue({ id: `asset-rationale-${asset.id}`, step: 2, message: `资产 ${index + 1} 缺少分析理由`, fieldId: `create-asset-${asset.id}-rationale` });
        }
        if (asset.expected_return_pct.trim() && !Number.isFinite(Number(asset.expected_return_pct))) {
          pushIssue({ id: `asset-return-${asset.id}`, step: 2, message: `资产 ${index + 1} 的预期收益率必须是有效数值`, fieldId: `create-asset-${asset.id}-return` });
        }
      }
    });
  } else if (form.strategy_type === "event") {
    if (!selectedDataset) {
      pushIssue({ id: "event-dataset-missing", step: 2, message: "请选择冻结评测事件集", fieldId: "create-event-dataset" });
    } else {
      const frozen = datasetHasFrozenIdentity(selectedDataset);
      if (datasetState(selectedDataset) !== "available") pushIssue({ id: "event-dataset-unavailable", step: 2, message: "所选事件集尚未就绪", fieldId: "create-event-dataset" });
      if (!frozen.version) pushIssue({ id: "event-dataset-no-version", step: 2, message: "所选事件集缺少冻结版本号", fieldId: "create-event-dataset" });
      if (!frozen.snapshot) pushIssue({ id: "event-dataset-no-snapshot", step: 2, message: "所选事件集缺少快照哈希", fieldId: "create-event-dataset" });
      if (form.event_analysis_mode === "provided" && providedSource === "file" && !importedPredictionReady) {
        pushIssue({ id: "prediction-import-missing", step: 2, message: "请上传本地预测文件，校验通过后确认导入", fieldId: "create-prediction-file" });
      } else if (form.event_analysis_mode === "provided" && !selectedDataset.decision_ready) {
        pushIssue({ id: "event-no-decisions", step: 2, message: "所选事件集尚无完整预测，请上传预测文件或选择其他事件集", fieldId: "create-event-dataset" });
      }
    }
  }
  if (form.strategy_type === "event" && externalHttp && !eventExternalAvailable) {
    pushIssue({ id: "event-api-adapter", step: 2, message: "外部事件 API 执行器当前不可用", fieldId: "create-external-endpoint" });
  }
  if (form.strategy_type === "event" && savedPredictionModel) {
    if (!predictionProfile) pushIssue({ id: "prediction-profile-missing", step: 2, message: "请选择一个已保存的模型连接", fieldId: "create-prediction-profile" });
    else if (!predictionProfile.secret_configured) pushIssue({ id: "prediction-profile-secret", step: 2, message: "所选预测模型尚未配置 API Key", fieldId: "create-prediction-profile" });
  }
  if (form.strategy_type === "event" && platformPrediction && !platformModelInfo?.available) {
    pushIssue({ id: "platform-api-unavailable", step: 2, message: usesVariantProfile ? "此 Pronoia 模型保存的基模连接尚未就绪，请检查对应 API 连接" : eventCapabilities ? "Pronoia 的统一平台基模尚未就绪，请先配置平台默认模型" : "尚未读取到 Pronoia 的统一平台基模，请刷新模型连接", fieldId: "create-platform-model" });
  }
  if (form.strategy_type === "event" && qaConfig.enabled) {
    const qaIssues = [...qaStatus.issues];
    if (!qaConfig.question_set_id || !qaConfig.question_ids.length) qaIssues.push("问答测试至少需要选择一道题");
    [...new Set(qaIssues)].forEach((message, index) => pushIssue({ id: `qa-${index}`, step: 2, message, fieldId: "create-event-qa" }));
  }

  if (form.strategy_type === "quant" && !quantForecast && !savedModelSetup) {
    if (!form.quant_conditions.some((condition) => condition.phase === "entry")) {
      pushIssue({ id: "quant-entry", step: 2, message: "请至少添加一条开仓条件", fieldId: "create-quant-rules" });
    }
    if (!form.quant_conditions.some((condition) => condition.phase === "exit")) {
      pushIssue({ id: "quant-exit", step: 2, message: "请至少添加一条平仓条件", fieldId: "create-quant-rules" });
    }
    form.quant_conditions.forEach((condition, index) => {
      const conditionError = quantConditionError(condition);
      if (conditionError) pushIssue({ id: `quant-${condition.id}`, step: 2, message: `量化条件 ${index + 1}：${conditionError}`, fieldId: `create-quant-${condition.id}` });
    });
  }
  if (savedQuantFile && !savedPredictionFilePath.trim()) {
    pushIssue({ id: "quant-prediction-file", step: 2, message: "请填写本次预测文件在本机服务端的路径", fieldId: "create-quant-prediction-file" });
  }
  if (quantForecast) {
    if (!Number.isInteger(form.quant_forecast.lookback) || form.quant_forecast.lookback < 1 || form.quant_forecast.lookback > 10000) {
      pushIssue({ id: "quant-forecast-lookback", step: 2, message: "收益率预测的历史样本窗口需为 1–10000 的整数", fieldId: "create-quant-forecast" });
    }
    if (!Number.isInteger(form.quant_forecast.horizon_bars) || form.quant_forecast.horizon_bars < 1 || form.quant_forecast.horizon_bars > 10000) {
      pushIssue({ id: "quant-forecast-horizon", step: 2, message: "收益率预测的未来周期 N 需为 1–10000 的整数", fieldId: "create-quant-forecast" });
    }
  }

  if (form.strategy_type === "event" && externalHttp) {
    const serviceError = externalEndpointError || serviceConfigError(externalService);
    if (serviceError) pushIssue({ id: "external-service", step: 2, message: serviceError, fieldId: "create-external-service" });
  } else if (usesExternalStrategy) {
    if (externalEndpointError) pushIssue({ id: "external-endpoint", step: 2, message: externalEndpointError, fieldId: "create-external-endpoint" });
    const externalSecretError = secretEnvRefError(form.external_secret_env_ref, "strategy");
    if (externalSecretError) {
      pushIssue({ id: "external-secret", step: 2, message: externalSecretError, fieldId: "create-external-secret" });
    }
    if (form.external_secret_env_ref && !form.external_header_name.trim()) {
      pushIssue({ id: "external-header", step: 2, message: "使用密钥时必须填写请求头名", fieldId: "create-external-header" });
    }
  }
  if (form.strategy_type === "signal_import" && apiAdapterCatalog && apiAdapterCatalog.status !== "available") {
    pushIssue({ id: "signal-api-adapter", step: 2, message: "外部决策 API 执行器当前不可用", fieldId: "create-external-endpoint" });
  }

  if (!dateSelectionOrdered) {
    pushIssue({ id: "date-order", step: 3, message: "开始日期不能晚于结束日期", fieldId: "create-date-from" });
  }
  if (dateSelectionOrdered && form.strategy_type === "event" && dateBasis.validateBounds) {
    if (form.date_from && dateBasis.start && form.date_from < dateBasis.start) {
      pushIssue({ id: "date-from-before-coverage", step: 3, message: `开始日期早于${dateBasis.label}覆盖起点 ${dateBasis.start}`, fieldId: "create-date-from" });
    }
    if (form.date_from && dateBasis.end && form.date_from > dateBasis.end) {
      pushIssue({ id: "date-from-after-coverage", step: 3, message: `开始日期晚于${dateBasis.label}覆盖终点 ${dateBasis.end}`, fieldId: "create-date-from" });
    }
    if (form.date_to && dateBasis.start && form.date_to < dateBasis.start) {
      pushIssue({ id: "date-to-before-coverage", step: 3, message: `结束日期早于${dateBasis.label}覆盖起点 ${dateBasis.start}`, fieldId: "create-date-to" });
    }
    if (form.date_to && dateBasis.end && form.date_to > dateBasis.end) {
      pushIssue({ id: "date-to-after-coverage", step: 3, message: `结束日期晚于${dateBasis.label}覆盖终点 ${dateBasis.end}`, fieldId: "create-date-to" });
    }
  } else if (dateSelectionOrdered && dateSelectionStartsAfterCoverage) {
    pushIssue({ id: "date-from-after-coverage", step: 3, message: `开始日期晚于${dateBasis.label}覆盖终点 ${dateBasis.end}，所选区间完全无数据`, fieldId: "create-date-from" });
  } else if (dateSelectionOrdered && dateSelectionEndsBeforeCoverage) {
    pushIssue({ id: "date-to-before-coverage", step: 3, message: `结束日期早于${dateBasis.label}覆盖起点 ${dateBasis.start}，所选区间完全无数据`, fieldId: "create-date-to" });
  }

  if (form.strategy_type === "event" && form.event_source === "registered" && form.data_source_mode === "registered") {
    const eventRange = datasetDateBasis(selectedDataset, "评测事件集");
    const marketRange = datasetDateBasis(selectedMarketDataset, "冻结市场数据版本");
    if (eventRange.complete && marketRange.complete && eventRange.start! > marketRange.end!) {
      pushIssue({ id: "event-market-no-overlap-after", step: 3, message: "事件集与市场数据日期范围完全不重叠", fieldId: "create-date-from" });
    } else if (eventRange.complete && marketRange.complete && eventRange.end! < marketRange.start!) {
      pushIssue({ id: "event-market-no-overlap-before", step: 3, message: "事件集与市场数据日期范围完全不重叠", fieldId: "create-date-from" });
    }
  }

  const issuesByStep = validationIssues.reduce<Record<WizardStep, WizardIssue[]>>(
    (acc, issue) => {
      acc[issue.step].push(issue);
      return acc;
    },
    { 1: [], 2: [], 3: [] },
  );
  const stepErrorCounts: Record<WizardStep, number> = {
    1: issuesByStep[1].length,
    2: issuesByStep[2].length,
    3: issuesByStep[3].length,
  };
  const canSubmit = validationIssues.length === 0 && !submitting;
  const dateCoverageWarning = dateBasis.validateBounds && !dateBasis.complete
    ? `${dateBasis.label}未返回完整覆盖边界，当前只能校验已知日期；创建后仍由服务端按冻结快照复核。`
    : null;

  const focusTarget = (step: WizardStep, fieldId?: string) => {
    setActiveStep(step);
    window.setTimeout(() => {
      const target = fieldId ? document.getElementById(fieldId) : validationSummaryRef.current;
      target?.scrollIntoView({ behavior: "smooth", block: "center" });
      if (target instanceof HTMLElement) target.focus({ preventScroll: true });
    }, 40);
  };

  const focusIssue = (issue: WizardIssue) => focusTarget(issue.step, issue.fieldId ?? "create-step-error-summary");

  const selectType = (type: StrategyType) => {
    const next = createDefaultDraft(type, datasets);
    setForm((current) => {
      const preservedMarketDataset = datasets.find((item) =>
        item.dataset_kind === "market" && item.id === current.market_dataset_id,
      );
      const selectedMarketDataset = preservedMarketDataset
        ?? datasets.find((item) => item.dataset_kind === "market" && item.id === next.market_dataset_id);
      const sharedDraft: CreateDraft = {
        ...next,
        data_source_mode: type === "event" ? "managed" : "registered",
        market_dataset_id: selectedMarketDataset?.id ?? "",
        custom_data_name: current.custom_data_name,
        custom_file_path: current.custom_file_path,
        custom_api_provider: current.custom_api_provider,
        custom_api_ref: current.custom_api_ref,
        custom_secret_env_ref: current.custom_secret_env_ref,
      };
      if (type !== "event") return syncMarketDatasetDraft(sharedDraft, selectedMarketDataset);
      return {
        ...sharedDraft,
        data_market: EVENT_ORACLE_MARKETS.includes(current.data_market as typeof EVENT_ORACLE_MARKETS[number])
          ? current.data_market
          : "CN",
        bar_frequency: "1d",
        frequency: "1d",
        data_asset_type: current.data_asset_type,
        data_adjustment: current.data_adjustment,
        data_calendar: current.data_calendar,
      };
    });
    setErrorMsg(null);
    setOracleFailure(null);
    setPreparedDataset(null);
    setDateEdited({ from: false, to: false });
  };

  const updateAsset = (id: string, patch: Partial<ManualAssetDraft>) => {
    setForm((current) => ({
      ...current,
      manual_assets: current.manual_assets.map((asset) =>
        asset.id === id ? { ...asset, ...patch } : asset,
      ),
    }));
  };

  const submit = async (autoStart = false) => {
    if (submitLock.current || submitting) return;
    setValidationRevealed(true);
    if (!canSubmit) {
      const firstIssue = validationIssues[0];
      if (firstIssue) focusIssue(firstIssue);
      return;
    }
    if (form.strategy_type === "event" && autoStart && qaConfig.enabled && qaStatus.highVolume && !window.confirm(`本次问答选择 ${qaConfig.question_ids.length} 道题，预计 ${qaStatus.answerCalls} 个回答任务和 ${qaStatus.judgeCalls} 个自动评分任务。Pronoia 每个回答任务内部会多次调用模型。\n\n确认开始预测与问答测试吗？`)) return;
    submitLock.current = true;
    setSubmitting(true);
    setErrorMsg(null);
    try {
      let datasetId = form.strategy_type === "event" ? form.dataset_id : form.market_dataset_id;
      let datasetName = form.strategy_type === "event" ? selectedDataset?.name ?? "" : selectedMarketDataset?.name ?? "";
      let datasetVersion = form.strategy_type === "event"
        ? (selectedDataset?.dataset_version ?? selectedDataset?.version ?? undefined)
        : (selectedMarketDataset?.dataset_version ?? selectedMarketDataset?.version ?? undefined);
      if (usesManualEvent) {
        const resolvedEventTime = resolveZonedLocalDateTime(form.event_time, form.event_timezone, form.event_time_disambiguation);
        const resolvedAvailableTime = resolveZonedLocalDateTime(form.available_time, form.event_timezone, form.available_time_disambiguation);
        if (resolvedAvailableTime.instantMs < resolvedEventTime.instantMs) {
          throw new Error("信息可得时间不能早于事件发生时间");
        }
        const eventFacts = {
          actual_value: optionalDraftNumber(form.event_actual_value),
          expected_value: optionalDraftNumber(form.event_expected_value),
          previous_value: optionalDraftNumber(form.event_previous_value),
          value_unit: form.event_value_unit.trim() || undefined,
        };
        const preEventFeatures = {
          asset_return_5d_pct: optionalDraftNumber(form.asset_return_5d_pct),
          asset_return_20d_pct: optionalDraftNumber(form.asset_return_20d_pct),
          benchmark_return_5d_pct: optionalDraftNumber(form.benchmark_return_5d_pct),
          benchmark_return_20d_pct: optionalDraftNumber(form.benchmark_return_20d_pct),
          excess_return_5d_pct: optionalDraftNumber(form.excess_return_5d_pct),
          excess_return_20d_pct: optionalDraftNumber(form.excess_return_20d_pct),
          as_of_note: "prior_close_only" as const,
        };
        const hasEventFacts = Object.values(eventFacts).some((value) => value !== undefined);
        const hasPreEventFeatures = Object.entries(preEventFeatures).some(([key, value]) => key !== "as_of_note" && value !== undefined);
        const manualPayload = {
          name: manualDatasetName(form.name),
          events: form.manual_assets.map((asset) => ({
            title: form.event_title.trim(),
            event_text: form.event_text.trim(),
            source_url: form.source_url.trim() || undefined,
            market: asset.market.trim().toUpperCase(),
            symbol: asset.symbol.trim().toUpperCase(),
            // Every asset mapping for this event reuses these two resolved
            // instants; market-specific Oracle routing must not reinterpret
            // the browser's timezone independently for each row.
            event_time: resolvedEventTime.iso,
            available_time: resolvedAvailableTime.iso,
            event_type_l2: form.event_type.trim() || "news",
            benchmark: form.benchmark === "dataset_default" ? undefined : form.benchmark,
            event_facts: hasEventFacts ? eventFacts : undefined,
            pre_event_features: hasPreEventFeatures ? preEventFeatures : undefined,
            ...(form.event_analysis_mode === "provided" ? {
              analysis_direction: asset.analysis_direction,
              confidence: (asset.confidence as number) / 100,
              expected_return_pct: optionalDraftNumber(asset.expected_return_pct),
              horizon: form.event_horizon,
              rationale: asset.rationale.trim(),
            } : {}),
          })),
        };
        // Reuse the immutable dataset if Oracle generation failed on the previous attempt.
        // The service derives a stable event_id from event facts, so the submitted payload is deterministic.
        const payloadSignature = JSON.stringify(manualPayload);
        const manualDataset =
          preparedDataset?.signature === payloadSignature
            ? preparedDataset
            : await api.btCreateManualDataset(manualPayload);
        setPreparedDataset({
          signature: payloadSignature,
          id: manualDataset.id,
          name: manualDataset.name,
          dataset_version: manualDataset.dataset_version,
          version: manualDataset.version,
        });
        datasetId = manualDataset.id;
        datasetName = manualDataset.name;
        datasetVersion = manualDataset.dataset_version ?? manualDataset.version ?? undefined;
        if (form.generate_oracle) {
          try {
            const oracleDataset = await api.btBuildDatasetOracle(manualDataset.id, form.event_horizon);
            datasetName = oracleDataset.name || datasetName;
            datasetVersion = oracleDataset.dataset_version ?? oracleDataset.version ?? datasetVersion;
            setOracleFailure(null);
          } catch (error) {
            setOracleFailure(
              `实际行情获取失败：${error instanceof Error ? error.message : String(error)}。实验尚未创建。请检查资产代码与时间后重试；若只需保存预测，可关闭“获取实际行情用于评估预测”后再次提交。`,
            );
            focusTarget(1, "create-oracle-error");
            return;
          }
        }
      }

      const executionSpec = {
        frequency: form.strategy_type === "event" ? "event" : form.bar_frequency,
        benchmark: form.benchmark,
        price_field: form.strategy_type === "event" ? "close" : "next_open",
        execution_delay: form.strategy_type === "event" ? "event_close" : "next_open",
        initial_capital: form.initial_capital,
        fee_bps: quantForecast ? 0 : form.transaction_cost_bps,
        commission_bps: quantForecast ? 0 : form.transaction_cost_bps,
        slippage_bps: quantForecast ? 0 : form.slippage_bps,
        stamp_duty_bps: quantForecast ? 0 : form.stamp_duty_bps,
        other_cost_bps: quantForecast ? 0 : form.other_cost_bps,
        minimum_commission: quantForecast ? 0 : form.minimum_commission,
        holding_horizon: form.strategy_type === "event" ? form.event_horizon : undefined,
        start_date: form.date_from || null,
        end_date: form.date_to || null,
        ...(form.strategy_type === "event" ? { timezone: form.event_timezone } : {}),
      };
      const eventAdapter = form.event_analysis_mode === "provided"
        ? "imported_decisions"
        : externalHttp ? "external_http" : savedPredictionModel ? "raw_model" : "existing_platform";
      const eventRunner = form.event_analysis_mode === "provided"
        ? "provided_analysis"
        : externalHttp ? "external_http" : savedPredictionModel ? "raw_model" : "team_full";
      const eventModelId = form.event_analysis_mode === "provided"
        ? "provided_analysis"
        : externalHttp ? form.signal_schema : savedPredictionModel ? predictionProfile?.model_id ?? "" : platformModelInfo?.model_id ?? "platform-default";
      const eventVersion = form.event_analysis_mode === "provided"
        ? "provided-analysis-v1"
        : externalHttp
          ? externalService.version.trim() || "external-http-v1"
          : savedPredictionModel ? "raw-event-v1" : platformModelInfo?.model_id ?? "platform-default";
      const eventInputContract = form.event_analysis_mode === "provided"
        ? "event_plus_provided_analysis"
        : "events_only";
      const sourceConfig =
        form.strategy_type === "event"
          ? {
              event_source: form.event_source,
              input_contract: eventInputContract,
              analysis_adapter: eventAdapter,
              analysis_horizon: form.event_horizon,
              signal_schema: externalHttp ? form.signal_schema : undefined,
              signal_version: externalHttp ? eventVersion : undefined,
              asset_mapping_count: usesManualEvent ? form.manual_assets.length : undefined,
              input_timezone: usesManualEvent ? form.event_timezone : undefined,
            }
          : form.strategy_type === "quant"
            ? {
                input_contract: "market_bars",
                asset_class: form.quant_asset_class,
                strategy_version_ref: form.strategy_version.trim() || null,
                frequency: form.bar_frequency,
                definition_mode: quantForecast ? "return_forecast" : "declarative_rules",
              }
            : {
                input_contract: "precomputed_decisions",
                signal_schema: form.signal_schema,
                signal_version_ref: form.signal_version.trim() || null,
              };
      const dataBasis = form.strategy_type === "event"
        ? {
            source_mode: "managed",
            provider_contract: "managed_daily_oracle",
            dataset_id: null,
            dataset_version: null,
            markets: [...EVENT_ORACLE_MARKETS],
            market: "per_event_asset",
            asset_type: "per_event_asset",
            frequency: "1d",
            adjustment: "provider_default",
            calendar: "per_market_exchange",
            status: dataBenchmarkReady ? "available" : "unavailable",
            no_implicit_fallback: true,
            registered_market_snapshot_consumed: false,
          }
        : {
            source_mode: "registered",
            dataset_id: form.market_dataset_id,
            dataset_version: selectedMarketDataset?.dataset_version ?? selectedMarketDataset?.version ?? null,
            market: form.data_market,
            asset_type: form.data_asset_type,
            frequency: form.bar_frequency,
            adjustment: form.data_adjustment,
            calendar: form.data_calendar,
            status: dataBenchmarkReady ? "available" : "pending",
            no_implicit_fallback: true,
          };
      let serviceHeaders: Record<string, string> = {};
      if (form.strategy_type === "event" && externalHttp && !savedModelSetup) {
        const signature = JSON.stringify([externalService.endpoint, externalService.auth_mode, externalService.api_key, externalService.header_name, externalService.secret_env_ref]);
        if (serviceCredentialCache.current?.signature === signature) serviceHeaders = serviceCredentialCache.current.headers;
        else {
          serviceHeaders = await saveServiceCredentials(externalService);
          serviceCredentialCache.current = { signature, headers: serviceHeaders };
        }
      }
      const strategySpec: BTStrategySpec = form.strategy_type === "event"
        ? {
            type: "event",
            adapter: eventAdapter,
            runner: eventRunner,
            model_id: eventModelId,
            version: eventVersion,
            input_contract: eventInputContract,
            endpoint: externalHttp ? externalService.endpoint.trim() : null,
            headers: serviceHeaders,
            ...(externalHttp ? { timeout_seconds: externalService.timeout_seconds } : {}),
            parameters: { evaluation_horizon: form.event_horizon },
          }
        : quantForecast
          ? {
              type: "quant",
              adapter: "builtin",
              kind: "return_forecast",
              source: "builtin",
              model_id: "historical_mean",
              name: "历史 N 周期收益均值预测",
              version: "1",
              input_contract: "market_bars",
              parameters: { ...form.quant_forecast },
            }
        : form.strategy_type === "quant"
          ? {
              type: "quant",
              adapter: "builtin",
              kind: "declarative_rules",
              model_id: "declarative_rules",
              version: form.strategy_version.trim() || null,
              input_contract: "market_bars",
              rules_summary: `${form.quant_conditions.filter((item) => item.phase === "entry").length} 条开仓条件 · ${form.quant_conditions.filter((item) => item.phase === "exit").length} 条平仓条件`,
              parameters: {
                signal_timing: "bar_close",
                execution_timing: "next_open",
                entry: {
                  combinator: form.quant_entry_combinator,
                  conditions: form.quant_conditions.filter((item) => item.phase === "entry").map(serializeQuantCondition),
                },
                exit: {
                  combinator: form.quant_exit_combinator,
                  conditions: form.quant_conditions.filter((item) => item.phase === "exit").map(serializeQuantCondition),
                },
              },
            }
          : {
              type: "api",
              adapter: "external_http",
              model_id: form.signal_schema,
              version: form.signal_version.trim() || null,
              input_contract: "external_decision_api",
              api_ref: form.external_api_ref.trim() || null,
              endpoint: form.external_api_ref.trim() || null,
              output_contract: "target_weight",
              headers: form.external_secret_env_ref.trim()
                ? { [form.external_header_name.trim() || "Authorization"]: `env:${form.external_secret_env_ref.trim()}` }
                : {},
              parameters: {
                secret_env_ref: form.external_secret_env_ref.trim() || null,
                stores_secret_value: false,
              },
            };
      const selectedStrategySpec: BTStrategySpec = savedModelSetup?.strategy_spec ? { ...savedModelSetup.strategy_spec,
        ...(savedQuantFile ? { path: savedPredictionFilePath.trim() } : {}),
        ...(quantForecast ? { parameters: { ...savedModelSetup.strategy_spec.parameters, horizon_bars: form.quant_forecast.horizon_bars } } : {}),
      } : strategySpec;
      const protocol = {
        dataset_id: datasetId,
        dataset_name: datasetName,
        dataset_version: datasetVersion ?? null,
        data_basis: dataBasis,
        benchmark: form.benchmark,
        date_from: form.date_from || null,
        date_to: form.date_to || null,
        evaluation_horizon: form.strategy_type === "event" ? form.event_horizon : undefined,
        execution_spec: executionSpec,
      };

      await onCreate({
        ...(form.strategy_type === "event" ? {
          prediction_profile_id: savedPredictionModel ? form.prediction_profile_id : null,
          event_qa: qaConfig.enabled ? { ...qaConfig, candidate_profile_id: qaConfig.variants.includes("raw") ? qaConfig.candidate_profile_id : null } : undefined,
          auto_start: autoStart,
        } : {}),
        auto_start: autoStart,
        name: form.name.trim(),
        horizon: form.strategy_type === "event" ? form.event_horizon : undefined,
        runner:
          form.strategy_type === "event"
            ? eventRunner
            : form.strategy_type === "signal_import" ? "external_http" : quantForecast ? "return_forecast" : "declarative_rules",
        dataset_id: datasetId,
        dataset_version: datasetVersion,
        events_path: "",
        labels_path: "",
        prompt_variant:
          form.strategy_type === "event" && platformPrediction
            ? form.prompt_variant.trim() || "v0"
            : form.strategy_type === "event" && savedPredictionModel
              ? "raw-event-v1"
            : form.strategy_type === "event" && form.event_analysis_mode === "provided"
              ? "provided-analysis"
            : form.strategy_type === "quant"
              ? quantForecast ? "historical-mean-v1" : form.strategy_version.trim() || "precomputed-quant"
              : form.signal_version.trim() || "decision.v1",
        model_version:
          form.strategy_type === "event"
            ? eventVersion
            : form.strategy_type === "quant"
              ? quantForecast ? "historical-mean-v1" : form.strategy_version.trim() || "declarative-rules-v1"
              : form.strategy_type === "signal_import"
                ? form.signal_version.trim() || "external-http-v1"
                : "provided-analysis-v1",
        concurrency: form.strategy_type === "event" && externalHttp ? 1 : form.concurrency,
        strategy_type: form.strategy_type,
        strategy_spec: selectedStrategySpec,
        visibility: form.visibility,
        execution_spec: executionSpec,
        config: {
          schema_version: "experiment.v1",
          ...(savedModelSetup ? { saved_model_id: savedModelSetup.saved_model_id } : {}),
          strategy_type: form.strategy_type,
          visibility: form.visibility,
          source: sourceConfig,
          data_basis: dataBasis,
          strategy_spec: selectedStrategySpec,
          protocol,
          execution_spec: executionSpec,
          confidentiality: {
            stores_strategy_logic: form.strategy_type === "quant",
            arena_exposes_strategy_logic: false,
            expose_event_level_decisions: form.visibility !== "arena_safe",
          },
          oracle_requested: usesManualEvent ? form.generate_oracle : undefined,
        },
      });
      onClose();
    } catch (error) {
      setErrorMsg(error instanceof Error ? error.message : String(error));
      focusTarget(3, "create-submit-error");
    } finally {
      submitLock.current = false;
      setSubmitting(false);
    }
  };

  if (!open) return null;

  return (
    <div className={presentation === "inline" ? "w-full min-w-0" : "fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-3 backdrop-blur-[2px] sm:p-6"}>
      <div
        role={presentation === "modal" ? "dialog" : "region"}
        aria-modal={presentation === "modal" ? true : undefined}
        aria-labelledby="create-backtest-title"
        className={cls("flex w-full min-w-0 flex-col overflow-hidden rounded-[16px] border border-edgeDark/80 bg-paper", presentation === "inline" ? "shadow-card" : "max-h-[94vh] max-w-[1280px] shadow-[0_24px_80px_rgba(28,27,26,0.22)] animate-fadeUp")}
      >
        <div className="flex shrink-0 items-center justify-between border-b border-edge bg-card px-5 py-4 sm:px-6">
          <div className="flex min-w-0 items-center gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-ink text-card">
              <FlaskMark />
            </span>
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <h2 id="create-backtest-title" className="font-serif text-[18px] font-semibold tracking-wide text-ink">{savedModelSetup ? `发起测试 · ${savedModelName || "已保存模型"}` : presentation === "inline" ? "实验设置" : form.strategy_type === "event" ? "创建事件实验" : "创建回测实验"}</h2>
                <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[9.5px] font-semibold uppercase tracking-[0.12em] text-amber-700">
                  未创建
                </span>
              </div>
              <p className="mt-0.5 text-[10px] leading-relaxed text-mute">{savedModelSetup ? "选择数据并确认本次测试设置。" : form.strategy_type === "event" ? "先设置事件预测；需要比较回答质量时，再开启问答测试。" : "选择历史行情，设置预测模型或交易策略，再确认评测方式。"}</p>
            </div>
          </div>
          {presentation === "modal" && <button
            onClick={onClose}
            disabled={submitting}
            className="rounded-lg p-2 text-mute transition hover:bg-edge/60 hover:text-ink disabled:opacity-40"
            aria-label="关闭"
          >
            <X size={17} />
          </button>}
        </div>

        <fieldset disabled={submitting} className="relative z-10 m-0 min-w-0 shrink-0 border-0 bg-paper px-5 pt-4 sm:px-6">
          <WizardSteps savedModel={Boolean(savedModelSetup)} eventMode={form.strategy_type === "event"} active={activeStep} errorCounts={stepErrorCounts} onSelect={(step) => setActiveStep(step)} />
        </fieldset>
        <div className={cls("relative min-h-0 min-w-0 flex-1", presentation === "modal" && "overflow-y-auto overscroll-contain")}>
        <fieldset disabled={submitting} className="m-0 min-w-0 border-0 px-5 pb-5 sm:px-6">

          {activeStep === 2 && !savedModelSetup && (allowedTypes?.length ?? 3) > 1 && <section className="mt-5">
            <div className="mb-2.5 flex items-end justify-between">
              <div>
                <p className="text-[9.5px] font-semibold uppercase tracking-[0.18em] text-faint">02 · Strategy contract</p>
                <h3 className="mt-1 font-serif text-[14px] font-semibold text-ink">选择模型类型</h3>
              </div>
              <span className="hidden text-[10.5px] text-faint sm:block">事件预测对照实际日线行情；交易策略使用已保存的历史行情</span>
            </div>
            <div className="grid gap-2.5 md:grid-cols-3">
              {(allowedTypes ?? (Object.keys(STRATEGY_META) as StrategyType[])).map((type) => {
                const meta = STRATEGY_META[type];
                const active = form.strategy_type === type;
                const catalogType = type === "signal_import" ? "api" : type;
                const adapter = strategyCatalog.find((item) => item.type === catalogType || item.id === catalogType);
                return (
                  <button
                    key={type}
                    type="button"
                    onClick={() => selectType(type)}
                    className={cls(
                      "group rounded-xl border p-3.5 text-left transition",
                      active
                        ? "border-ink/25 bg-card shadow-[0_5px_18px_rgba(28,27,26,0.07)]"
                        : "border-edge bg-card/45 hover:border-edgeDark hover:bg-card",
                    )}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <span className={cls("grid h-8 w-8 place-items-center rounded-lg border", meta.className)}>
                        {meta.icon(15)}
                      </span>
                      <span
                        className={cls(
                          "mt-1 h-3.5 w-3.5 rounded-full border-[3px]",
                          active ? "border-ink bg-card" : "border-edgeDark bg-paper",
                        )}
                      />
                    </div>
                    <div className="mt-3 flex items-center justify-between gap-2">
                      <p className="text-[9px] font-semibold tracking-[0.14em] text-faint">{meta.eyebrow}</p>
                      {adapter && (
                        <DataStateBadge state={adapter.status === "available" ? "available" : "pending"} compact />
                      )}
                    </div>
                    <p className="mt-0.5 text-[13px] font-semibold text-ink">{meta.label}</p>
                    <p className="mt-1 text-[10.5px] leading-[1.55] text-mute">{meta.description}</p>
                  </button>
                );
              })}
            </div>
            <div className="mt-2.5 flex flex-wrap items-center gap-x-4 gap-y-1.5 rounded-xl border border-edge bg-card/65 px-3.5 py-2.5 text-[10px] text-mute">
              <span className="inline-flex items-center gap-1.5 font-semibold text-ink">
                <CandlestickChart size={13} className="text-brand" />
                运行完成后查看评测结果
              </span>
              <span>预测任务展示预测值与误差；交易策略展示净值、回撤和模拟交易记录。</span>
            </div>
          </section>}

          {validationRevealed && issuesByStep[activeStep].length > 0 && (
            <div ref={validationSummaryRef}>
              <StepValidationSummary
                issues={issuesByStep[activeStep]}
                onSelect={focusIssue}
              />
            </div>
          )}

          <div className="mt-5 grid min-w-0 items-start gap-5 lg:grid-cols-[minmax(0,1fr)_280px] xl:grid-cols-[minmax(0,1fr)_300px]">
            <div className="min-w-0 space-y-4">
              {activeStep === 1 && form.strategy_type !== "event" && (
                <DataBenchmarkEditor
                  form={form}
                  setForm={setForm}
                  datasets={datasets}
                  loading={datasetsLoading}
                  error={datasetsErr}
                  selected={selectedMarketDataset}
                  validationError={issuesByStep[1].find((issue) => issue.fieldId === "create-market-dataset")?.message}
                  onManagerOpen={() => { window.open("/backtest/data?tab=market", "_blank", "noopener,noreferrer"); }}
                />
              )}

              {(activeStep === 2 || (form.strategy_type === "event" && activeStep === 1)) && <FormSection eyebrow={activeStep === 1 ? "01 · Event input" : "02 · Model"} title={form.strategy_type === "event" ? activeStep === 1 ? "事件输入" : "事件预测" : "模型与决策输入"} icon={<Sparkles size={14} />}>
                {(form.strategy_type !== "event" || activeStep === 1) && <Field
                  label="实验名称"
                  required
                  hint={`用于运行记录和 Arena 识别 · 最多 ${RUN_NAME_MAX_LENGTH} 字符`}
                  error={validationIssues.find((issue) => issue.fieldId === "create-experiment-name")?.message}
                >
                  <input
                    id="create-experiment-name"
                    value={form.name}
                    onChange={(event) => setForm({ ...form, name: event.target.value })}
                    maxLength={RUN_NAME_MAX_LENGTH}
                    aria-invalid={!form.name.trim() || form.name.trim().length > RUN_NAME_MAX_LENGTH}
                    className={cls(INPUT_CLASS, (!form.name.trim() || form.name.trim().length > RUN_NAME_MAX_LENGTH) && "border-rise/60")}
                    placeholder="例如：政策新闻 · 银行板块 · T+3"
                  />
                </Field>}

                {form.strategy_type === "event" && activeStep === 1 && (
                  <div>
                    <div className="mb-1.5 text-[11px] font-medium text-ink">事件输入方式</div>
                    <div className="grid grid-cols-2 gap-2 rounded-xl border border-edge bg-paper p-1.5">
                      <button
                        type="button"
                        onClick={() => setForm({ ...form, event_source: "manual" })}
                        className={cls(
                          "rounded-lg px-3 py-2 text-left transition",
                          form.event_source === "manual" ? "bg-card text-ink shadow-card" : "text-mute hover:text-ink",
                        )}
                      >
                        <span className="flex items-center gap-1.5 text-[11.5px] font-semibold">
                          <FileInput size={12} /> 手工填写事件
                        </span>
                        <span className="mt-0.5 block text-[9.5px] text-faint">填写事件内容、发布时间和关联资产</span>
                      </button>
                      <button
                        type="button"
                        onClick={() => setForm({ ...form, event_source: "registered" })}
                        className={cls(
                          "rounded-lg px-3 py-2 text-left transition",
                          form.event_source === "registered" ? "bg-card text-ink shadow-card" : "text-mute hover:text-ink",
                        )}
                      >
                        <span className="flex items-center gap-1.5 text-[11.5px] font-semibold">
                          <Database size={12} /> 使用已有事件集
                        </span>
                        <span className="mt-0.5 block text-[9.5px] text-faint">选择已保存的事件，用于预测或导入结果</span>
                      </button>
                    </div>
                  </div>
                )}

                {(form.strategy_type !== "event" || activeStep === 1) && (usesManualEvent ? (
                  <ManualEventEditor
                    form={form}
                    setForm={setForm}
                    updateAsset={updateAsset}
                    eventTimeResolution={eventTimeResolution}
                    availableTimeResolution={availableTimeResolution}
                    availabilityValid={availabilityValid}
                  />
                ) : form.strategy_type === "event" ? (
                  <DatasetSelector
                    datasets={datasets.filter((dataset) => dataset.dataset_kind !== "market")}
                    loading={datasetsLoading}
                    error={datasetsErr}
                    value={form.dataset_id}
                    selected={selectedDataset}
                    onChange={(datasetId) => setForm({ ...form, dataset_id: datasetId })}
                    label="评测事件集"
                    decisionContract={form.event_analysis_mode === "provided" && providedSource === "dataset"}
                    fieldId="create-event-dataset"
                    validationError={validationIssues.find((issue) => issue.fieldId === "create-event-dataset")?.message}
                  />
                ) : (
                  <div className="flex items-start gap-2.5 rounded-xl border border-jade/20 bg-jade-soft/35 px-3.5 py-3 text-[10.5px] leading-relaxed text-mute">
                    <Database size={13} className="mt-0.5 shrink-0 text-jade" />
                    <span>
                      本模型将在第 1 步选择的 <strong className="font-medium text-ink">{selectedMarketDataset?.name ?? "市场数据基准"}</strong> 上执行。
                      {quantForecast ? "收益率预测与后续实际收益使用同一版本的收盘价、周期和复权方式。" : "信号与成交均按该版本的时间戳、交易日历和复权方式对齐。"}
                    </span>
                  </div>
                ))}

                {form.strategy_type === "event" && activeStep === 2 && (
                  <div className="space-y-3">
                    {savedModelSetup ? <div className="rounded-xl border border-jade/20 bg-jade-soft/25 px-4 py-3"><p className="text-[12px] font-semibold text-ink">本次测试模型：{savedModelName}</p><p className="mt-1 text-[11px] text-mute">模型配置已保存，本次只设置数据与评测方式。测试结果将归入此模型历史。</p></div> : <div>
                      <div className="mb-1.5 flex items-center justify-between gap-2"><span className="text-[11px] font-medium text-ink">预测来源（本次选择一项）</span><button type="button" onClick={() => setModelOptionsRevision((value) => value + 1)} disabled={profilesLoading} className="inline-flex items-center gap-1 text-[10px] text-mute hover:text-ink disabled:opacity-40"><RefreshCw size={10} className={cls(profilesLoading && "animate-spin")} />刷新模型连接</button></div>
                      <div className="grid gap-2 rounded-xl border border-edge bg-paper p-1 sm:grid-cols-2">
                        {([
                          ["platform", "platform", "profile", "team_full", "Pronoia（多 Agent）", "使用平台基模，通过多 Agent 流程生成预测"],
                          ["raw_model", "external_api", "profile", "raw_model", "外部大模型（直接预测）", "直接调用你选择的 Qwen、DeepSeek 等模型"],
                          ["external_http", "external_api", "http", "external_http", "第三方预测服务", "连接外部金融模型，接收其返回的预测结果"],
                          ["provided", "provided", "profile", "provided_analysis", "使用已有预测", "上传预测文件、读取事件集预测或手工填写"],
                        ] as const).map(([key, value, externalMode, runner, label, state]) => (
                          <button
                            key={key}
                            type="button"
                            onClick={() => setForm({
                              ...form,
                              event_analysis_mode: value,
                              external_model_mode: value === "external_api" ? externalMode : form.external_model_mode,
                              runner,
                            })}
                            className={cls("rounded-lg border px-3 py-3 text-left transition", form.event_analysis_mode === value && (value !== "external_api" || form.external_model_mode === externalMode) ? "border-violet/30 bg-card text-ink shadow-card" : "border-transparent text-mute hover:text-ink")}
                          >
                            <span className="block text-[10.5px] font-semibold">{label}</span>
                            <span className="mt-0.5 block text-[9px] text-mute">{state}</span>
                          </button>
                        ))}
                      </div>
                    </div>}

                    <div className="rounded-lg border border-brand/15 bg-brand-soft/20 px-3 py-2.5 text-[10px] leading-relaxed text-mute">
                      <p className="font-medium text-ink">预测什么，如何评估</p>
                      <p className="mt-1">方向预测：评估扣除市场基准影响后的超额表现。收益率预测：比较资产自身的预测收益率与实际涨跌幅，例如 2 表示 +2%。</p>
                      <p className="mt-1">每次实验评估一个预测来源。比较多个模型时，请使用同一事件集和评测设置分别运行。</p>
                      <label className="mt-3 flex flex-wrap items-center gap-3 text-[11px] font-medium text-ink">预测窗口<select aria-label="模型预测窗口" className={cls(INPUT_CLASS, "!w-28")} value={form.event_horizon} onChange={(event) => setForm({ ...form, event_horizon: event.target.value })}>{[1, 3, 5, 7, 15, 30, 60].map((days) => <option key={days} value={`t${days}`}>T+{days}</option>)}</select><span className="text-[10px] font-normal text-mute">N 为交易日数；导入预测的窗口需与此一致，第 3 步同步更新。</span></label>
                    </div>

                    {savedPredictionModel && !savedModelSetup && <div className="space-y-3">
                        <Field label="事件预测模型连接" required error={validationIssues.find((issue) => issue.fieldId === "create-prediction-profile")?.message}><select id="create-prediction-profile" value={form.prediction_profile_id} disabled={profilesLoading} onChange={(event) => setForm({ ...form, prediction_profile_id: event.target.value })} className={INPUT_CLASS}><option value="">{profilesLoading ? "正在读取模型连接…" : profiles.length === 0 ? "尚无已启用的模型连接" : "请选择模型连接"}</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model_id}{!profile.secret_configured ? " · 密钥未配置" : ""}</option>)}</select></Field>
                        {profilesError && <p className="text-[10px] text-rise">模型连接读取失败：{profilesError}</p>}
                        {!profilesLoading && profiles.length === 0 && <p className="text-[10px] leading-relaxed text-mute">下拉框显示已保存的模型连接。首次使用请在下方添加服务地址、模型 ID 和 API Key。</p>}
                        <EventModelConnectionEditor disabled={submitting} onCreated={(profile) => { setProfiles((current) => [profile, ...current.filter((item) => item.id !== profile.id)]); setForm((current) => ({ ...current, prediction_profile_id: profile.id })); setProfilesError(null); }} />
                        <p className="text-[10px] leading-relaxed text-mute">所选连接用于本次事件预测。Pronoia 的平台基模、下方问答模型和评分模型分别配置。</p>
                    </div>}

                    {chatPrediction && (
                      <div className="grid gap-3 sm:grid-cols-2">
                        <Field label={platformPrediction ? "Pronoia 当前基模" : "本次预测模型"} hint="随本次实验保存"><div id="create-platform-model" tabIndex={-1} className="rounded-lg border border-edge bg-paper px-3 py-2 text-[11px] text-ink">{savedPredictionModel ? predictionProfile ? `${predictionProfile.name} · ${predictionProfile.model_id}` : "请先添加或选择模型连接" : platformModelInfo?.model_id ? `Pronoia（${platformModelInfo.model_id}）` : "尚未读取到统一平台基模"}</div></Field>
                        <Field label="预测并发数" hint="可填 1–10；当前运行器实际最多同时预测 2 条事件"><input type="number" min={1} max={10} value={form.concurrency} onChange={(event) => setForm({ ...form, concurrency: Math.min(10, Math.max(1, Number(event.target.value) || 1)) })} className={INPUT_CLASS} /></Field>
                        {platformPrediction && (
                          <div className="sm:col-span-2 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-[9.5px] leading-relaxed text-amber-800">
                            <ShieldCheck size={11} className="mt-0.5 shrink-0" />
                            <span>
                              Pronoia 使用{savedModelSetup ? "当前保存的基模配置" : "统一平台基模"}和固定多 Agent 流程，依次完成规划、专家研究、综合、复核和假设提取。第一条预测完成前可能显示 0/N，详情页会持续显示当前阶段与用时。
                              当前粗略耗时：<strong className="font-semibold">{teamFullEstimate}</strong>（实际并发最多 2，受模型和工具响应影响）。
                              {teamFullEventCount > 10
                                ? " 当前样本较大，强烈建议先建立 5–10 条小样本事件集确认模型与接口稳定，再运行全量。"
                                : " 建议先用当前小样本确认模型与接口稳定。"}
                              该流程会使用研究工具；历史事件评测需核查检索信息的时间，避免使用事件之后的信息。
                            </span>
                          </div>
                        )}
                      </div>
                    )}
                    {externalHttp && !savedModelSetup && (
                      <ExternalEventServiceEditor value={externalService} onChange={setExternalService} datasetId={usesManualEvent ? undefined : form.dataset_id} horizon={form.event_horizon} disabled={submitting} allowPrivateEndpoints={eventCapabilities?.allow_private_model_endpoints === true} />
                    )}
                    {form.event_analysis_mode === "provided" && <div id="create-prediction-file" tabIndex={-1} className="min-w-0 space-y-3 outline-none">
                      {usesManualEvent ? <div className="rounded-xl border border-brand/20 bg-brand-soft/20 p-4 text-[11px] leading-6 text-mute"><p>当前输入是手工事件。可返回第 1 步填写预测，或选择事件集后上传本地预测文件。</p><div className="mt-3 flex flex-wrap gap-3"><button type="button" className={BUTTON_GHOST} onClick={() => setActiveStep(1)}>返回填写手工预测</button><button type="button" className={BUTTON_GHOST} onClick={() => { setForm({ ...form, event_source: "registered" }); setProvidedSource("file"); setActiveStep(1); }}>选择事件集并上传文件</button></div></div> : <>
                        <div className="flex flex-wrap gap-2">{([["file", "上传本地预测文件"], ["dataset", "使用事件集内的预测"]] as const).map(([source, label]) => <button type="button" key={source} onClick={() => setProvidedSource(source)} className={cls(BUTTON_GHOST, providedSource === source && "border-brand/40 bg-brand-soft/30 text-brand")}>{label}</button>)}</div>
                        {providedSource === "file" ? importedPredictionReady ? <div className="rounded-xl border border-jade/25 bg-jade-soft/25 p-4 text-[11px] leading-6"><p className="flex items-center gap-2 font-medium text-jade"><CheckCircle2 size={15} />预测文件已导入，可以继续评测</p><p className="mt-1 break-words text-mute">{predictionImport?.name} · {form.event_horizon.toUpperCase()} · 已生成独立数据集</p><button type="button" className={cls(BUTTON_GHOST, "mt-3")} onClick={() => { setForm({ ...form, dataset_id: predictionImport?.sourceId ?? form.dataset_id }); setPredictionImport(null); }}>更换预测文件</button></div> : <PredictionFileImport datasetId={form.dataset_id} horizon={form.event_horizon} disabled={submitting} onImported={(dataset) => { setPredictionImport({ id: dataset.id, name: dataset.name, sourceId: form.dataset_id, horizon: form.event_horizon }); setDatasets((current) => [dataset, ...current.filter((item) => item.id !== dataset.id)]); setForm((current) => ({ ...current, dataset_id: dataset.id })); }} /> : <div className="rounded-xl border border-brand/20 bg-brand-soft/20 p-4 text-[11px] leading-6 text-mute"><p>{selectedDataset?.decision_ready ? `“${selectedDataset.name}”已包含预测，将直接读取方向、置信度、理由和可选收益率。` : "当前事件集没有完整预测。请切换到“上传本地预测文件”，或选择已有预测的事件集。"}</p><button type="button" onClick={() => setActiveStep(1)} className={cls(BUTTON_GHOST, "mt-3")}>选择事件集</button></div>}
                      </>}
                    </div>}
                  </div>
                )}

                {form.strategy_type === "quant" && !savedModelSetup && (
                  <div className="space-y-3">
                    <div className="grid gap-2 sm:grid-cols-2">
                      {([
                        ["return_forecast", "收益率预测", "预测未来 N 个行情周期的收益率，评估数值误差"],
                        ["trading_strategy", "交易策略回测", "根据开平仓规则模拟交易，计算净值与风险"],
                      ] as const).map(([mode, label, note]) => <button key={mode} type="button" onClick={() => setForm({ ...form, quant_mode: mode })} className={cls("rounded-lg border px-3 py-3 text-left transition", form.quant_mode === mode ? "border-jade/30 bg-jade-soft/30" : "border-edge bg-paper")}><span className="block text-[11px] font-semibold text-ink">{label}</span><span className="mt-1 block text-[9px] leading-relaxed text-mute">{note}</span></button>)}
                    </div>
                    {quantForecast ? <div id="create-quant-forecast" tabIndex={-1} className="outline-none"><QuantReturnForecastConfig value={form.quant_forecast} onChange={(quant_forecast) => setForm({ ...form, quant_forecast })} disabled={submitting} /></div> : <QuantEditor form={form} setForm={setForm} />}
                  </div>
                )}

                {savedModelSetup && form.strategy_type !== "event" && <div className="rounded-xl border border-jade/20 bg-jade-soft/25 p-4"><p className="text-[12px] font-semibold text-ink">本次测试模型：{savedModelName}</p><p className="mt-1 text-[11px] text-mute">使用已保存的完整模型配置，只调整测试数据、时间与评测窗口。</p>{savedQuantFile && <Field label="本次预测文件路径" hint="填写本机服务端可读取的 CSV / JSON 文件路径；可按本次标的换文件，模型配置保持不变"><input id="create-quant-prediction-file" value={savedPredictionFilePath} onChange={(event) => setSavedPredictionFilePath(event.target.value)} placeholder="例如：/path/to/predictions.csv" className={INPUT_CLASS} /></Field>}{quantForecast && <Field label="本次预测周期 N" hint="单位为 K 线根数，算法与历史样本窗口保持原配置"><input type="number" min={1} max={10000} step={1} value={form.quant_forecast.horizon_bars} onChange={(event) => setForm({ ...form, quant_forecast: { ...form.quant_forecast, horizon_bars: Number(event.target.value) } })} className={INPUT_CLASS} /></Field>}<details className="mt-3 text-[11px] text-mute"><summary className="cursor-pointer">查看模型配置</summary><pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-all text-[10px] leading-relaxed">{JSON.stringify(savedModelSetup.strategy_spec, null, 2)}</pre></details></div>}
                {form.strategy_type === "signal_import" && !savedModelSetup && (
                  <SignalEditor
                    form={form}
                    setForm={setForm}
                    allowPrivateEndpoints={eventCapabilities?.allow_private_model_endpoints === true}
                    adapterError={apiAdapterCatalog && apiAdapterCatalog.status !== "available" ? "外部量化服务当前不可用，暂时无法开始测试。" : undefined}
                  />
                )}
              </FormSection>}

              {form.strategy_type === "event" && <div className={activeStep === 2 ? "" : "hidden"}><EventQAEditor value={qaConfig} onChange={setQaConfig} profiles={profiles} platformDefault={eventCapabilities?.platform_default ?? null} pronoiaModelDefault={savedModelSetup?.kind === "pronoia" ? platformModelInfo : undefined} customPrediction={externalHttp || form.event_analysis_mode === "provided"} disabled={submitting} onStatusChange={setQaStatus} onProfileCreated={(profile) => setProfiles((current) => [profile, ...current.filter((item) => item.id !== profile.id)])} /></div>}

              {activeStep === 3 && <FormSection eyebrow="03 · Evaluation" title="确认评测设置" icon={<SlidersHorizontal size={14} />}>
                {form.strategy_type === "event" && <div className="rounded-lg border border-brand/15 bg-brand-soft/25 px-3.5 py-3 text-[11px] leading-relaxed text-mute"><p>事件方向预测 / 收益率预测：{usesManualEvent ? `手工事件 · ${form.manual_assets.length} 个资产` : selectedDataset?.name ?? "尚未选择事件集"}。对照真实行情评估方向表现和未来 T+N 预期收益率的数值误差。</p><p className="mt-1">{qaConfig.enabled ? `问答测试：已选 ${qaConfig.question_ids.length} 题，预计 ${qaStatus.totalCalls} 个回答与评分任务。预测和问答独立运行、分别展示结果。` : "问答测试：未启用。"}</p></div>}
                {quantForecast && <div className="rounded-lg border border-jade/20 bg-jade-soft/25 px-3.5 py-3 text-[11px] leading-relaxed text-mute">预测未来 {form.quant_forecast.horizon_bars} 个 {form.bar_frequency} 行情周期的收益率数值。结果展示预测值、实际值与误差；本模式不生成交易或策略净值。</div>}
                <div className="grid gap-3 sm:grid-cols-2">
                  {form.strategy_type === "event" ? (
                    <Field label="预测窗口" hint="未来 N 个交易日；与第 2 步同步">
                      <select
                        value={form.event_horizon}
                        onChange={(event) => setForm({ ...form, event_horizon: event.target.value })}
                        className={INPUT_CLASS}
                      >
                        <option value="t1">T+1</option>
                        <option value="t3">T+3</option>
                        <option value="t5">T+5</option>
                        <option value="t7">T+7</option>
                        <option value="t15">T+15</option>
                        <option value="t30">T+30</option>
                        <option value="t60">T+60</option>
                      </select>
                    </Field>
                  ) : quantForecast ? (
                    <Field label="收益率预测窗口" hint="N 为行情周期数">
                      <div className="rounded-lg border border-edgeDark/80 bg-paper px-3 py-2 text-[12.5px] text-ink">未来 {form.quant_forecast.horizon_bars} 个 {form.bar_frequency} 周期</div>
                    </Field>
                  ) : (
                    <Field label="收益统计周期" hint="按每个行情周期的模拟持仓计算收益">
                      <div className="rounded-lg border border-edgeDark/80 bg-paper px-3 py-2 text-[12.5px] text-ink">
                        每根 {form.bar_frequency} 行情 · 自动按频率年化
                      </div>
                    </Field>
                  )}
                  {quantForecast ? <Field label="对照实际值" hint="与预测使用同一未来窗口"><div className="rounded-lg border border-edgeDark/80 bg-paper px-3 py-2 text-[12.5px] text-ink">同一资产从当前收盘到未来第 N 个周期收盘的实际收益率</div></Field> : <Field label="市场基准" hint={form.strategy_type === "event" ? "用于评估同期的超额表现" : "与买入并持有所选资产的表现比较"}>
                    <select
                      value={form.benchmark}
                      onChange={(event) => setForm({ ...form, benchmark: event.target.value })}
                      className={INPUT_CLASS}
                    >
                      {form.strategy_type === "event" ? (
                        <>
                          <option value="dataset_default">跟随数据集默认基准</option>
                          <option value="000300.SH">沪深 300 · 000300.SH</option>
                          <option value="000905.SH">中证 500 · 000905.SH</option>
                          <option value="cash">现金基准 · 0%</option>
                        </>
                      ) : (
                        <option value="dataset_asset_buy_hold">所选数据资产买入持有（当前）</option>
                      )}
                    </select>
                  </Field>}
                  {form.strategy_type === "event" && (
                    <Field label="事件时间" hint={usesManualEvent ? "使用第 1 步设置的时区；交易日按各资产所属市场确定" : "按每条事件已保存的时间计算，不使用浏览器时区"}>
                      <div className="rounded-lg border border-edgeDark/80 bg-paper px-3 py-2 font-mono text-[11.5px] text-ink">
                        {usesManualEvent ? form.event_timezone : "沿用事件集中的时间信息"}
                      </div>
                    </Field>
                  )}
                  <Field
                    label="开始日期"
                    hint={dateBasis.start ? `默认 ${dateBasis.start}` : "留空使用数据起始日期"}
                    error={issuesByStep[3].find((issue) => issue.fieldId === "create-date-from")?.message}
                  >
                    <input
                      id="create-date-from"
                      type="date"
                      value={form.date_from}
                      min={form.strategy_type === "event" && dateBasis.validateBounds ? dateBasis.start ?? undefined : undefined}
                      max={form.strategy_type === "event" && dateBasis.validateBounds ? dateBasis.end ?? undefined : undefined}
                      aria-invalid={issuesByStep[3].some((issue) => issue.fieldId === "create-date-from")}
                      onChange={(event) => {
                        setDateEdited((current) => ({ ...current, from: true }));
                        setForm({ ...form, date_from: event.target.value });
                      }}
                      className={cls(INPUT_CLASS, issuesByStep[3].some((issue) => issue.fieldId === "create-date-from") && "border-rise/60")}
                    />
                  </Field>
                  <Field
                    label="结束日期"
                    hint={dateBasis.end ? `默认 ${dateBasis.end}` : "留空使用数据结束日期"}
                    error={issuesByStep[3].find((issue) => issue.fieldId === "create-date-to")?.message}
                  >
                    <input
                      id="create-date-to"
                      type="date"
                      value={form.date_to}
                      min={form.strategy_type === "event" && dateBasis.validateBounds ? dateBasis.start ?? undefined : undefined}
                      max={form.strategy_type === "event" && dateBasis.validateBounds ? dateBasis.end ?? undefined : undefined}
                      aria-invalid={issuesByStep[3].some((issue) => issue.fieldId === "create-date-to")}
                      onChange={(event) => {
                        setDateEdited((current) => ({ ...current, to: true }));
                        setForm({ ...form, date_to: event.target.value });
                      }}
                      className={cls(INPUT_CLASS, issuesByStep[3].some((issue) => issue.fieldId === "create-date-to") && "border-rise/60")}
                    />
                  </Field>
                  {dateIntersectionNotice && (
                    <div
                      role="status"
                      aria-live="polite"
                      className={cls(
                        "flex items-start gap-1.5 rounded-lg border px-3 py-2 text-[9.5px] leading-relaxed sm:col-span-2",
                        dateSelectionHasNoOverlap
                          ? "border-rise/25 bg-rise/5 text-rise"
                          : "border-amber-200 bg-amber-50 text-amber-800",
                      )}
                    >
                      <AlertTriangle size={11} className="mt-0.5 shrink-0" />
                      <span>{dateIntersectionNotice}</span>
                    </div>
                  )}
                  {quantForecast ? <Field label="收益率口径" hint="百分数；2 表示 +2%"><div className="rounded-lg border border-edgeDark/80 bg-paper px-3 py-2 text-[12.5px] text-ink">（未来第 N 周期收盘价 ÷ 当前收盘价 − 1）× 100%</div></Field> : <Field label={form.strategy_type === "event" ? "收益计价口径" : "信号与成交时点"} hint="以下为本次实际采用的收益计算方式">
                    <select
                      value={form.price_rule}
                      onChange={(event) => setForm({ ...form, price_rule: event.target.value })}
                      className={INPUT_CLASS}
                    >
                      {form.strategy_type === "event" ? (
                        <option value="event_close">信息可得日收盘 → 目标日收盘（盘后事件顺延）</option>
                      ) : (
                        <option value="next_open">本周期收盘生成信号 → 下一周期开盘模拟成交</option>
                      )}
                    </select>
                  </Field>}
                  {!quantForecast && <><Field label="初始资金" hint={form.strategy_type === "event" ? "用于参考金额换算，不影响预测准确率" : "模拟账户的起始金额；最低佣金可能影响收益率"}>
                    <input
                      type="number"
                      min={1}
                      value={form.initial_capital}
                      onChange={(event) => setForm({ ...form, initial_capital: Number(event.target.value) || 1 })}
                      className={INPUT_CLASS}
                    />
                  </Field>
                  <Field
                    label={form.strategy_type === "event" ? "参考交易成本 (bps)" : "佣金 (bps)"}
                    hint={form.strategy_type === "event" ? "用于参考收益；每个有效方向预测扣除一次" : "买入与卖出分别按成交金额计收"}
                  >
                    <input
                      type="number"
                      min={0}
                      step={0.5}
                      value={form.transaction_cost_bps}
                      onChange={(event) => setForm({ ...form, transaction_cost_bps: Math.max(0, Number(event.target.value) || 0) })}
                      className={INPUT_CLASS}
                    />
                  </Field>
                  <Field label="滑点 (bps)" hint={form.strategy_type === "event" ? "与交易成本一起扣减参考收益" : "模拟成交时按买卖方向调整成交价格"}>
                    <input
                      type="number"
                      min={0}
                      step={0.5}
                      value={form.slippage_bps}
                      onChange={(event) => setForm({ ...form, slippage_bps: Math.max(0, Number(event.target.value) || 0) })}
                      className={INPUT_CLASS}
                    />
                  </Field>
                  {form.strategy_type !== "event" && (
                    <>
                      <Field label="卖出印花税 (bps)" hint="仅在卖出成交时计收；默认不代填市场规则">
                        <input
                          type="number"
                          min={0}
                          step={0.5}
                          value={form.stamp_duty_bps}
                          onChange={(event) => setForm({ ...form, stamp_duty_bps: Math.max(0, Number(event.target.value) || 0) })}
                          className={INPUT_CLASS}
                        />
                      </Field>
                      <Field label="其他双边费 (bps)" hint="买卖两侧均按成交金额计收">
                        <input
                          type="number"
                          min={0}
                          step={0.1}
                          value={form.other_cost_bps}
                          onChange={(event) => setForm({ ...form, other_cost_bps: Math.max(0, Number(event.target.value) || 0) })}
                          className={INPUT_CLASS}
                        />
                      </Field>
                      <Field label="最低佣金（金额）" hint="每笔佣金下限；0 表示不启用">
                        <input
                          type="number"
                          min={0}
                          step={0.01}
                          value={form.minimum_commission}
                          onChange={(event) => setForm({ ...form, minimum_commission: Math.max(0, Number(event.target.value) || 0) })}
                          className={INPUT_CLASS}
                        />
                      </Field>
                    </>
                  )}
                  </>}
                </div>
                <div className={cls(
                  "mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border px-3 py-2.5 text-[9.5px] leading-relaxed",
                  dateCoverageWarning ? "border-amber-200 bg-amber-50 text-amber-800" : "border-brand/15 bg-brand-soft/25 text-mute",
                )}>
                  <span className="inline-flex min-w-0 items-start gap-1.5">
                    {dateCoverageWarning ? <AlertTriangle size={11} className="mt-0.5 shrink-0" /> : <CalendarRange size={11} className="mt-0.5 shrink-0 text-brand" />}
                    <span>
                      {dateCoverageWarning ?? (
                        dateBasis.start && dateBasis.end
                          ? `${dateBasis.label}：${dateBasis.start} → ${dateBasis.end}。日期默认自动带入，仍可在覆盖范围内调整。`
                          : `${dateBasis.label}尚无可显示的日期范围。`
                      )}
                    </span>
                  </span>
                  {(dateEdited.from || dateEdited.to) && (
                    <button
                      type="button"
                      onClick={() => {
                        setDateEdited({ from: false, to: false });
                        setForm({ ...form, date_from: dateBasis.start ?? "", date_to: dateBasis.end ?? "" });
                      }}
                      className="shrink-0 rounded-md border border-current/20 bg-card/60 px-2 py-1 font-medium hover:bg-card"
                    >
                      恢复默认日期
                    </button>
                  )}
                </div>
                <div className="mt-3 flex items-start gap-2 rounded-lg border border-edge bg-paper px-3 py-2.5 text-[9.5px] leading-relaxed text-mute">
                  <ShieldCheck size={11} className="mt-0.5 shrink-0 text-jade" />
                  {quantForecast
                    ? "预测窗口、历史样本窗口和冻结行情版本随本次评测保存。纯收益率预测不扣交易成本，实际值尚未兑现的预测不计入误差。"
                    : form.strategy_type === "event"
                    ? "交易成本与滑点用于事件策略的参考收益计算，不影响方向命中或收益率预测误差。1 bps = 0.01%。"
                    : "费用设置随实验保存，并在每次模拟成交时计入。1 bps = 0.01%；这里只模拟交易，不会发送真实订单。"}
                </div>
              </FormSection>}
            </div>

            <div className={presentation === "inline" && form.strategy_type === "event" && activeStep !== 3 ? "hidden lg:block" : ""}><DraftSummary
              form={form}
              setForm={setForm}
              selectedDataset={selectedDataset}
              selectedMarketDataset={selectedMarketDataset}
              usesManualEvent={usesManualEvent}
              eventModelLabel={form.event_analysis_mode === "provided" ? "已有预测" : externalHttp ? "第三方预测服务" : savedPredictionModel ? predictionProfile ? `${predictionProfile.name} · 直接调用` : "尚未选择外部大模型" : `Pronoia · 多 Agent · ${platformModelInfo?.model_id ?? "尚未读取基模"}`}
              qaSummary={qaConfig.enabled ? `${qaConfig.question_ids.length} 题 × ${qaConfig.repeats} 次` : "未启用"}
              qaModelsSummary={qaModelsSummary}
              scoringSummary={scoringSummary}
              savedModelName={savedModelSetup ? savedModelName : undefined}
            /></div>
          </div>

          {!availabilityValid && (
            <div className="mt-4 rounded-xl border border-rise/20 bg-rise/5 px-3 py-2.5 text-[11.5px] text-rise">
              可得时间不能早于事件发生时间，否则会引入未来信息。
            </div>
          )}
          {!dataBenchmarkReady && (
            <div className="mt-4 rounded-xl border border-amber-200 bg-amber-50 px-3.5 py-3 text-[11px] leading-relaxed text-amber-800">
              <div className="flex items-start gap-2">
                <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                <span>
                  当前数据基准不可执行。分钟数据、自定义 API 与未通过质量校验的数据集不会被在线日线静默替代；
                  {form.strategy_type === "quant" ? "量化回测需要选择可用且已通过价格校验的历史行情。" : "请返回第 1 步选择可用日线或完成数据校验。"}
                </span>
              </div>
            </div>
          )}
          {oracleFailure && (
            <div id="create-oracle-error" tabIndex={-1} className="mt-4 rounded-xl border border-amber-300 bg-amber-50 px-3.5 py-3 text-[11px] leading-relaxed text-amber-800 outline-none focus:ring-2 focus:ring-amber-300/60">
              <div className="flex items-start gap-2">
                <CircleDashed size={13} className="mt-0.5 shrink-0" />
                <span>{oracleFailure}</span>
              </div>
            </div>
          )}
          {errorMsg && (
            <div id="create-submit-error" tabIndex={-1} className="mt-4 rounded-xl border border-rise/25 bg-rise/5 px-3 py-2.5 text-[11.5px] text-rise outline-none focus:ring-2 focus:ring-rise/25">
              {errorMsg}
            </div>
          )}
        </fieldset>
        </div>

        {validationRevealed && validationIssues.length > 0 && (
          <div
            id="create-validation-summary"
            role="status"
            aria-live="polite"
            className="shrink-0 border-t border-amber-200 bg-amber-50/95 px-5 py-2.5 sm:px-6"
          >
            <div className="flex min-w-0 items-center gap-2.5 overflow-x-auto">
              <button
                type="button"
                onClick={() => focusIssue(validationIssues[0])}
                className="inline-flex shrink-0 items-center gap-1.5 font-semibold text-[10.5px] text-amber-900"
              >
                <AlertTriangle size={12} /> 还需完成 {validationIssues.length} 项
              </button>
              <span className="h-4 w-px shrink-0 bg-amber-200" />
              {validationIssues.slice(0, 4).map((issue) => (
                <button
                  key={issue.id}
                  type="button"
                  onClick={() => focusIssue(issue)}
                  className="max-w-[280px] shrink-0 truncate rounded-md border border-amber-200 bg-card/70 px-2 py-1 text-[9.5px] text-amber-900 transition hover:border-amber-300 hover:bg-card"
                  title={`步骤 ${issue.step} · ${issue.message}`}
                >
                  步骤 {issue.step} · {issue.message}
                </button>
              ))}
              {validationIssues.length > 4 && (
                <button type="button" onClick={() => focusIssue(validationIssues[4])} className="shrink-0 text-[9.5px] font-medium text-amber-800 hover:underline">
                  +{validationIssues.length - 4} 项
                </button>
              )}
            </div>
          </div>
        )}

        <div className="relative z-10 flex shrink-0 items-center justify-between gap-4 border-t border-edge bg-card px-5 py-3.5 sm:px-6">
          <div className="hidden items-center gap-2 text-[10.5px] text-mute sm:flex">
            <ShieldCheck size={13} className="text-jade" />
            {savedModelSetup ? "开始后运行当前模型，结果自动保存。" : form.strategy_type === "event" ? "“保存，暂不开始”只创建待运行实验；“开始测试”会运行预测评估及已启用的问答任务。手工事件保存时可先获取实际行情。" : "保存后可从实验详情开始运行。"}
          </div>
          <div className="ml-auto flex items-center gap-2">
            {presentation === "modal" && <button onClick={onClose} disabled={submitting} className={BUTTON_GHOST}>
              取消
            </button>}
            {activeStep > 1 && (
              <button
                type="button"
                onClick={() => setActiveStep((activeStep - 1) as 1 | 2)}
                disabled={submitting}
                className={BUTTON_GHOST}
              >
                <ArrowLeft size={12} /> 上一步
              </button>
            )}
            {activeStep < 3 ? (
              <button
                type="button"
                onClick={() => { setValidationRevealed(true); setActiveStep((activeStep + 1) as 2 | 3); }}
                disabled={submitting}
                className={cls(
                  "inline-flex items-center gap-2 rounded-lg px-4 py-2 text-[12px] font-semibold transition",
                  !submitting
                    ? "bg-ink text-card shadow-card hover:bg-ink/90 hover:shadow-pop"
                    : "cursor-not-allowed bg-edge text-faint",
                )}
              >
                下一步 <ArrowRight size={12} />
              </button>
            ) : <>
              {form.strategy_type === "event" && <button type="button" onClick={() => void submit(false)} disabled={submitting} className={BUTTON_GHOST}>保存，暂不开始</button>}
              <button
                onClick={() => void submit(Boolean(savedModelSetup) || form.strategy_type === "event")}
                disabled={submitting}
                className={cls(
                  "inline-flex items-center gap-2 rounded-lg px-4 py-2 text-[12px] font-semibold transition",
                  validationIssues.length === 0
                    ? "bg-ink text-card shadow-card hover:bg-ink/90 hover:shadow-pop"
                    : "border border-amber-300 bg-amber-50 text-amber-900 hover:bg-amber-100",
                  submitting && "cursor-not-allowed opacity-60",
                )}
              >
                {submitting ? <Loader2 size={13} className="animate-spin" /> : <Zap size={13} />}
                {submitting
                  ? (usesManualEvent ? "写入事件并创建…" : "正在创建…")
                  : validationIssues.length ? `检查缺项 · ${validationIssues.length}` : savedModelSetup || form.strategy_type === "event" ? "开始测试" : "保存实验"}
              </button>
            </>}
          </div>
        </div>
      </div>
    </div>
  );
}

function WizardSteps({
  active,
  errorCounts,
  onSelect,
  eventMode = false,
  savedModel = false,
}: {
  active: WizardStep;
  errorCounts: Record<WizardStep, number>;
  onSelect: (step: WizardStep) => void;
  eventMode?: boolean;
  savedModel?: boolean;
}) {
  const steps: Array<{ id: WizardStep; label: string; note: string; icon: ReactNode }> = [
    { id: 1, label: eventMode ? "事件输入" : "交易数据", note: eventMode ? "手工填写或选择已有事件集" : "市场、频率与数据版本", icon: <Database size={13} /> },
    { id: 2, label: savedModel ? "模型与测试" : eventMode ? "预测与问答" : "模型 / 策略", note: savedModel ? "已保存配置与预测周期" : eventMode ? "预测来源、问答与评分模型" : "量化规则或外部 API", icon: <Workflow size={13} /> },
    { id: 3, label: "评测设置", note: "预测窗口、日期与收益计算", icon: <SlidersHorizontal size={13} /> },
  ];
  return (
    <nav aria-label="创建实验步骤" className="grid overflow-hidden rounded-xl border border-edge bg-card shadow-card sm:grid-cols-3">
      {steps.map((step, index) => {
        const selected = active === step.id;
        const visited = active > step.id;
        const issueCount = errorCounts[step.id];
        return (
          <button
            key={step.id}
            type="button"
            onClick={() => onSelect(step.id)}
            className={cls(
              "relative flex items-center gap-3 px-4 py-3 text-left transition sm:border-l sm:first:border-l-0",
              selected ? "bg-ink text-card" : "border-edge bg-card text-mute hover:bg-edge/25 hover:text-ink",
            )}
          >
            <span className={cls(
              "grid h-7 w-7 shrink-0 place-items-center rounded-full border font-mono text-[10px]",
              selected
                ? issueCount ? "border-amber-300/60 bg-amber-400/15 text-amber-200" : "border-card/30 bg-card/10"
                : issueCount ? "border-rise/25 bg-rise/5 text-rise" : visited ? "border-jade/25 bg-jade-soft text-jade" : "border-edge bg-paper",
            )}>
              {issueCount ? <AlertTriangle size={13} /> : visited ? <CheckCircle2 size={13} /> : step.icon}
            </span>
            <span className="min-w-0">
              <span className="block text-[11.5px] font-semibold">0{index + 1} · {step.label}</span>
              <span className={cls(
                "mt-0.5 block truncate text-[9px]",
                selected ? issueCount ? "text-amber-200" : "text-card/55" : issueCount ? "text-rise" : "text-faint",
              )}>
                {issueCount ? `${issueCount} 项待完成` : step.note}
              </span>
            </span>
          </button>
        );
      })}
    </nav>
  );
}

function StepValidationSummary({ issues, onSelect }: { issues: WizardIssue[]; onSelect: (issue: WizardIssue) => void }) {
  return (
    <div
      id="create-step-error-summary"
      tabIndex={-1}
      role="alert"
      className="mt-4 rounded-xl border border-rise/20 bg-rise/5 px-3.5 py-3 outline-none focus:ring-2 focus:ring-rise/20"
    >
      <div className="flex items-start gap-2.5">
        <AlertTriangle size={13} className="mt-0.5 shrink-0 text-rise" />
        <div className="min-w-0 flex-1">
          <p className="text-[10.5px] font-semibold text-ink">本步骤还有 {issues.length} 项需要完成</p>
          <div className="mt-1.5 flex flex-wrap gap-1.5">
            {issues.map((issue) => (
              <button
                key={issue.id}
                type="button"
                onClick={() => onSelect(issue)}
                className="rounded-md border border-rise/15 bg-card/80 px-2 py-1 text-left text-[9.5px] text-rise transition hover:border-rise/30 hover:bg-card"
              >
                {issue.message}
              </button>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function DataStateBadge({ state, compact = false }: { state: "available" | "pending" | "failed"; compact?: boolean }) {
  const config = state === "available"
    ? { label: "已就绪", icon: <CheckCircle2 size={compact ? 9 : 11} />, className: "border-jade/20 bg-jade-soft text-jade" }
    : state === "failed"
      ? { label: "校验失败", icon: <AlertTriangle size={compact ? 9 : 11} />, className: "border-rise/20 bg-rise/5 text-rise" }
      : { label: "待接入", icon: <CircleDashed size={compact ? 9 : 11} />, className: "border-amber-200 bg-amber-50 text-amber-700" };
  return (
    <span className={cls(
      "inline-flex shrink-0 items-center gap-1 rounded-full border font-medium",
      compact ? "px-1.5 py-0.5 text-[8.5px]" : "px-2 py-1 text-[9.5px]",
      config.className,
    )}>
      {config.icon}{config.label}
    </span>
  );
}

function DataBenchmarkEditor({
  form, setForm, datasets, loading, error, selected, validationError, onManagerOpen,
}: {
  form: CreateDraft;
  setForm: (next: CreateDraft) => void;
  datasets: BTDataset[];
  loading: boolean;
  error: string | null;
  selected?: BTDataset;
  validationError?: string;
  onManagerOpen: () => void;
}) {
  const marketDatasets = datasets.filter((dataset) => dataset.dataset_kind === "market");
  const datasetReady = form.strategy_type === "event"
    ? marketDatasetHasOhlc
    : marketDatasetCanExecutePortfolio;
  return (
    <FormSection eyebrow="01 · Market data" title="选择交易数据" icon={<Database size={14} />}>
      <p className="text-[11px] leading-relaxed text-mute">使用已校验并冻结的行情版本。市场、频率、复权方式和交易日历随数据版本确定。</p>
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
        <div className="min-w-0 flex-1">
          <Field label="行情数据版本" required error={validationError}>
            <select id="create-market-dataset" value={form.market_dataset_id} disabled={loading || !marketDatasets.length} onChange={(event) => {
              const dataset = marketDatasets.find((item) => item.id === event.target.value);
              setForm(syncMarketDatasetDraft(
                { ...form, data_source_mode: "registered" },
                dataset,
                event.target.value,
              ));
            }} className={INPUT_CLASS}>
              <option value="">{loading ? "正在读取数据…" : "请选择行情版本"}</option>
              {marketDatasets.map((dataset) => <option key={dataset.id} value={dataset.id} disabled={!datasetReady(dataset)}>{dataset.name} · {dataset.frequency ?? "频率未声明"}{!datasetReady(dataset) ? (marketDatasetHasOhlc(dataset) ? " · 当前组合引擎不可执行" : " · 未通过 OHLC 校验") : ""}</option>)}
            </select>
          </Field>
        </div>
        <button type="button" onClick={onManagerOpen} className={BUTTON_GHOST}><Database size={12} />管理交易数据</button>
      </div>
      {selected && <div className="grid gap-2 sm:grid-cols-3">
        <DataFact label="市场" value={(selected.markets ?? []).join(" / ") || "未声明"} />
        <DataFact label="频率" value={selected.frequency ?? "未声明"} />
        <DataFact label="复权方式" value={selected.adjustment ?? "数据源默认"} />
        <DataFact label="版本" value={selected.dataset_version ?? selected.version ?? "未冻结"} mono />
        <DataFact label="覆盖" value={selected.coverage?.start_at && selected.coverage?.end_at ? `${String(selected.coverage.start_at).slice(0, 10)} → ${String(selected.coverage.end_at).slice(0, 10)}` : "未返回"} />
        <DataFact label="状态" value={datasetReady(selected) ? (form.strategy_type === "event" ? "OHLC 校验通过" : "组合执行已就绪") : "尚未就绪"} />
      </div>}
      {!loading && !marketDatasets.length && <p className="rounded-lg border border-edge bg-paper px-3 py-3 text-[11px] text-mute">请先在数据管理中导入交易数据，再返回创建实验。</p>}
      {error && <p role="alert" className="text-[11px] text-rise">数据目录读取失败：{error}</p>}
    </FormSection>
  );
}

function DataFact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="rounded-lg border border-edge bg-card px-2.5 py-2">
      <div className="text-[8.5px] uppercase tracking-wider text-faint">{label}</div>
      <div className={cls("mt-1 truncate text-[9.5px] font-medium text-ink", mono && "font-mono")} title={value}>{value}</div>
    </div>
  );
}


function FlaskMark() {
  return (
    <svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
      <path d="M9 3h6M10 3v5.2l-5 8.4A2.9 2.9 0 0 0 7.5 21h9a2.9 2.9 0 0 0 2.5-4.4l-5-8.4V3" />
      <path d="M7.5 15h9" />
    </svg>
  );
}

function Field({
  label,
  hint,
  required,
  error,
  children,
}: {
  label: string;
  hint?: string;
  required?: boolean;
  error?: string;
  children: ReactNode;
}) {
  return (
    <label className="block min-w-0">
      <div className="mb-1.5 flex flex-wrap items-baseline gap-x-1.5 gap-y-0.5">
        <span className="text-[11px] font-medium text-ink">
          {label}
          {required && <span className="ml-0.5 text-rise">*</span>}
        </span>
        {hint && <span className="text-[9.5px] text-faint">{hint}</span>}
      </div>
      {children}
      {error && (
        <span className="mt-1.5 flex items-start gap-1 text-[9px] leading-relaxed text-rise">
          <AlertTriangle size={9} className="mt-0.5 shrink-0" />
          {error}
        </span>
      )}
    </label>
  );
}

function FormSection({
  eyebrow,
  title,
  icon,
  children,
}: {
  eyebrow: string;
  title: string;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="rounded-xl border border-edge bg-card p-4 shadow-card sm:p-5">
      <div className="mb-4 flex items-center gap-2 border-b border-edge/70 pb-3">
        <span className="grid h-7 w-7 place-items-center rounded-lg bg-edge/55 text-mute">{icon}</span>
        <div>
          <p className="text-[8.5px] font-semibold uppercase tracking-[0.16em] text-faint">{eyebrow}</p>
          <h3 className="mt-0.5 font-serif text-[13px] font-semibold text-ink">{title}</h3>
        </div>
      </div>
      <div className="space-y-3.5">{children}</div>
    </section>
  );
}

function DatasetSelector({
  datasets,
  loading,
  error,
  value,
  selected,
  onChange,
  label,
  decisionContract = false,
  fieldId,
  validationError,
}: {
  datasets: BTDataset[];
  loading: boolean;
  error: string | null;
  value: string;
  selected?: BTDataset;
  onChange: (value: string) => void;
  label: string;
  decisionContract?: boolean;
  fieldId?: string;
  validationError?: string;
}) {
  const options = decisionContract ? datasets.filter((dataset) => dataset.decision_ready) : datasets;
  return (
    <Field label={label} required hint="使用同一事件集，便于比较模型结果" error={validationError}>
      <select
        id={fieldId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={Boolean(validationError)}
        className={cls(INPUT_CLASS, validationError && "border-rise/60")}
        disabled={loading || options.length === 0}
      >
        {loading ? (
          <option value="">正在读取数据目录…</option>
        ) : error ? (
          <option value="">数据目录读取失败</option>
        ) : options.length === 0 ? (
          <option value="">{decisionContract ? "暂无包含完整预测的事件集" : "暂无事件集"}</option>
        ) : (
          options.map((dataset) => (
            <option key={dataset.id} value={dataset.id}>
              {datasetIsSyntheticDemo(dataset) ? "[机制演示] " : ""}{dataset.name} · {dataset.total_events || 0} 条 · {Object.keys(dataset.by_market || {}).join("/") || "未分类"}
            </option>
          ))
        )}
      </select>
      {selected && (
        <div className="mt-2 rounded-lg border border-edge bg-paper/80 p-3">
          <div className="flex flex-wrap items-center gap-2">
            <span className="inline-flex items-center gap-1.5 text-[11px] font-semibold text-ink">
              <Database size={11} className="text-brand" /> {selected.name}
            </span>
            <span className="font-mono text-[9px] text-faint">{selected.id}</span>
          </div>
          <div className="mt-2 flex flex-wrap gap-1.5 text-[9.5px]">
            <span className="rounded bg-edge/60 px-1.5 py-0.5 text-mute">{selected.total_events || 0} 条记录</span>
            {Object.entries(selected.by_market || {}).slice(0, 4).map(([market, count]) => (
              <span key={market} className="rounded bg-brand-soft/70 px-1.5 py-0.5 text-brand">
                {market} · {count}
              </span>
            ))}
            {selected.labels_path && (
              <span className="inline-flex items-center gap-1 rounded bg-jade-soft px-1.5 py-0.5 text-jade">
                <CheckCircle2 size={9} /> 已关联实际行情
              </span>
            )}
          </div>
          {selected.date_range?.min && selected.date_range?.max && (
            <div className="mt-2 flex items-center gap-1.5 font-mono text-[9px] text-faint">
              <CalendarRange size={10} />
              {String(selected.date_range.min).slice(0, 10)} → {String(selected.date_range.max).slice(0, 10)}
            </div>
          )}
          {datasetIsSyntheticDemo(selected) && (
            <div className="mt-2 flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-2.5 py-2 text-[9.5px] leading-relaxed text-amber-800">
              <AlertTriangle size={11} className="mt-0.5 shrink-0" />
              <span><strong>合成机制演示，不用于正式准确率或 Arena。</strong> 仅用于检查模型连接和评测流程；其中可能包含虚构事件或占位文本。</span>
            </div>
          )}
        </div>
      )}
      {!loading && error && <p className="mt-1.5 text-[10px] text-rise">{error}</p>}
      {!loading && !error && options.length === 0 && (
        <p className="mt-1.5 text-[10px] text-rise">{decisionContract ? "暂无包含完整预测的事件集。可在第 2 步切换为上传预测文件，再选择原始事件集；还没有事件集时，请先在数据管理中添加。" : "暂无事件集。请先在数据管理中添加，或切换为手工填写事件。"}</p>
      )}
      {decisionContract && (
        <div className="mt-2 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-2 text-[9.5px] leading-relaxed text-amber-800">
          <ShieldCheck size={11} className="mt-0.5 shrink-0" />
          <span>此方式读取事件集中已保存的方向、置信度、理由和预测窗口。只有事件事实的文件，请在第 2 步选择模型生成预测，或上传对应的预测结果。</span>
        </div>
      )}
    </Field>
  );
}

function ManualEventEditor({
  form,
  setForm,
  updateAsset,
  eventTimeResolution,
  availableTimeResolution,
  availabilityValid,
}: {
  form: CreateDraft;
  setForm: (next: CreateDraft) => void;
  updateAsset: (id: string, patch: Partial<ManualAssetDraft>) => void;
  eventTimeResolution: DateTimeResolutionState;
  availableTimeResolution: DateTimeResolutionState;
  availabilityValid: boolean;
}) {
  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-brand/15 bg-brand-soft/25 p-3.5">
        <div className="mb-3 flex items-center justify-between gap-3">
          <div>
            <p className="text-[10.5px] font-semibold text-ink">事件事实</p>
            <p className="mt-0.5 text-[9.5px] text-mute">填写当时已知的事实。信息可得时间用于确定预测可用信息与收益统计起点。</p>
          </div>
          <span className="rounded-full border border-brand/15 bg-card px-2 py-0.5 text-[9px] font-medium text-brand">EVENT</span>
        </div>
        <div className="space-y-3">
          <Field label="新闻 / 事件标题" required error={!form.event_title.trim() ? "请填写事件标题" : undefined}>
            <input
              id="create-event-title"
              value={form.event_title}
              onChange={(event) => setForm({ ...form, event_title: event.target.value })}
              maxLength={500}
              aria-invalid={!form.event_title.trim()}
              className={cls(INPUT_CLASS, !form.event_title.trim() && "border-rise/60")}
              placeholder="例如：某公司发布季度业绩预告"
            />
          </Field>
          <Field label="事件正文或事实摘要" required hint="建议仅写当时可获得的事实" error={!form.event_text.trim() ? "请填写事件正文或事实摘要" : undefined}>
            <textarea
              id="create-event-text"
              value={form.event_text}
              onChange={(event) => setForm({ ...form, event_text: event.target.value })}
              maxLength={100_000}
              aria-invalid={!form.event_text.trim()}
              className={cls(INPUT_CLASS, "min-h-[86px] resize-y leading-relaxed", !form.event_text.trim() && "border-rise/60")}
              placeholder="记录事件内容、关键数字和上下文，不在此处写投资逻辑…"
            />
          </Field>
          <div className="grid gap-3 sm:grid-cols-2">
            <Field label="来源链接" hint="可选，用于审计">
              <input
                value={form.source_url}
                onChange={(event) => setForm({ ...form, source_url: event.target.value })}
                maxLength={4_000}
                className={INPUT_CLASS}
                placeholder="https://…"
              />
            </Field>
            <Field label="事件类型">
              <select
                value={form.event_type}
                onChange={(event) => setForm({ ...form, event_type: event.target.value })}
                className={INPUT_CLASS}
              >
                <option value="news">公司新闻</option>
                <option value="earnings">财报与业绩</option>
                <option value="policy">政策与监管</option>
                <option value="macro">宏观数据</option>
                <option value="supply_chain">产业与供应链</option>
                <option value="other">其他事件</option>
              </select>
            </Field>
            <div className="sm:col-span-2">
              <Field label="输入时区" required hint="下面的发生时间与信息可得时间均按此时区填写；夏令时自动换算">
                <select
                  id="create-event-timezone"
                  value={form.event_timezone}
                  onChange={(event) => setForm({
                    ...form,
                    event_timezone: event.target.value as EventInputTimeZone,
                    event_time_disambiguation: "earlier",
                    available_time_disambiguation: "earlier",
                  })}
                  className={INPUT_CLASS}
                >
                  {EVENT_INPUT_TIME_ZONES.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
                </select>
              </Field>
            </div>
            <Field
              label="事件发生时间"
              required
              hint={form.event_timezone}
              error={!form.event_time ? "请选择事件发生时间" : eventTimeResolution.error ?? undefined}
            >
              <input
                id="create-event-time"
                type="datetime-local"
                value={form.event_time}
                onChange={(event) => setForm({ ...form, event_time: event.target.value, event_time_disambiguation: "earlier" })}
                aria-invalid={!form.event_time || Boolean(eventTimeResolution.error)}
                className={cls(INPUT_CLASS, (!form.event_time || eventTimeResolution.error) && "border-rise/60")}
              />
              {eventTimeResolution.value?.ambiguous && (
                <select
                  value={form.event_time_disambiguation}
                  onChange={(event) => setForm({ ...form, event_time_disambiguation: event.target.value as DateTimeDisambiguation })}
                  className={cls(INPUT_CLASS, "mt-1.5")}
                  aria-label="事件发生时间的夏令时重叠选择"
                >
                  <option value="earlier">夏令时重叠：选择较早时刻</option>
                  <option value="later">夏令时重叠：选择较晚时刻</option>
                </select>
              )}
            </Field>
            <Field
              label="信息可得时间"
              required
              hint={`${form.event_timezone} · 防止未来函数`}
              error={!form.available_time
                ? "请选择信息可得时间"
                : availableTimeResolution.error ?? (!availabilityValid ? "不能早于事件发生时间" : undefined)}
            >
              <input
                id="create-available-time"
                type="datetime-local"
                value={form.available_time}
                onChange={(event) => setForm({ ...form, available_time: event.target.value, available_time_disambiguation: "earlier" })}
                aria-invalid={!form.available_time || Boolean(availableTimeResolution.error) || !availabilityValid}
                className={cls(INPUT_CLASS, (!form.available_time || availableTimeResolution.error || !availabilityValid) && "border-rise/60")}
              />
              {availableTimeResolution.value?.ambiguous && (
                <select
                  value={form.available_time_disambiguation}
                  onChange={(event) => setForm({ ...form, available_time_disambiguation: event.target.value as DateTimeDisambiguation })}
                  className={cls(INPUT_CLASS, "mt-1.5")}
                  aria-label="信息可得时间的夏令时重叠选择"
                >
                  <option value="earlier">夏令时重叠：选择较早时刻</option>
                  <option value="later">夏令时重叠：选择较晚时刻</option>
                </select>
              )}
            </Field>
          </div>
          {(eventTimeResolution.value || availableTimeResolution.value) && (
            <div className="rounded-lg border border-brand/15 bg-card/75 px-3 py-2.5 text-[9.5px] leading-relaxed text-mute">
              <p className="font-medium text-ink">保存时记录时区与准确时间</p>
              {eventTimeResolution.value && <p className="mt-1 font-mono">发生：{eventTimeResolution.value.iso}</p>}
              {availableTimeResolution.value && <p className="mt-0.5 font-mono">可得：{availableTimeResolution.value.iso}</p>}
              <p className="mt-1 text-faint">同一事件关联多个市场时，发布时间保持一致；收益统计按各市场交易日历对齐。</p>
            </div>
          )}
          <details className="group rounded-xl border border-edge bg-card/70" open>
            <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3.5 py-3">
              <span>
                <span className="block text-[10.5px] font-semibold text-ink">补充事件数据与事前行情（可选）</span>
                <span className="mt-0.5 block text-[9.5px] text-mute">所有字段必须在信息可得时间当时已知；留空不会阻止回测。</span>
              </span>
              <ChevronDown size={13} className="shrink-0 text-faint transition group-open:rotate-180" />
            </summary>
            <div className="space-y-4 border-t border-edge/70 px-3.5 py-3.5">
              <div>
                <div className="mb-2 flex items-center justify-between gap-2">
                  <span className="text-[10px] font-semibold text-ink">事件公布数据</span>
                  <span className="rounded bg-brand-soft/50 px-1.5 py-0.5 text-[8.5px] text-brand">当时已知</span>
                </div>
                <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-4">
                  <Field label="公布值" hint="已公布的事件数据">
                    <input type="number" step="any" value={form.event_actual_value} onChange={(event) => setForm({ ...form, event_actual_value: event.target.value })} className={INPUT_CLASS} placeholder="例如：3.2" />
                  </Field>
                  <Field label="市场预期值" hint="公布前的预期">
                    <input type="number" step="any" value={form.event_expected_value} onChange={(event) => setForm({ ...form, event_expected_value: event.target.value })} className={INPUT_CLASS} placeholder="例如：3.0" />
                  </Field>
                  <Field label="上期公布值" hint="同一指标上一期">
                    <input type="number" step="any" value={form.event_previous_value} onChange={(event) => setForm({ ...form, event_previous_value: event.target.value })} className={INPUT_CLASS} placeholder="例如：2.8" />
                  </Field>
                  <Field label="数值单位" hint="可选">
                    <input value={form.event_value_unit} onChange={(event) => setForm({ ...form, event_value_unit: event.target.value })} maxLength={40} className={INPUT_CLASS} placeholder="%、亿元、万桶/日…" />
                  </Field>
                </div>
              </div>
              <div>
                <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                  <span className="text-[10px] font-semibold text-ink">事件前的行情</span>
                  <span className="rounded bg-jade-soft/60 px-1.5 py-0.5 text-[8.5px] text-jade">仅使用事件前一收盘及更早数据</span>
                </div>
                <div className="grid gap-2.5 sm:grid-cols-2 lg:grid-cols-3">
                  <Field label="资产近 5 期收益" hint="百分点">
                    <input type="number" step="0.01" value={form.asset_return_5d_pct} onChange={(event) => setForm({ ...form, asset_return_5d_pct: event.target.value })} className={INPUT_CLASS} placeholder="例如：1.25" />
                  </Field>
                  <Field label="资产近 20 期收益" hint="百分点">
                    <input type="number" step="0.01" value={form.asset_return_20d_pct} onChange={(event) => setForm({ ...form, asset_return_20d_pct: event.target.value })} className={INPUT_CLASS} placeholder="例如：-2.40" />
                  </Field>
                  <Field label="基准近 5 期收益" hint="百分点">
                    <input type="number" step="0.01" value={form.benchmark_return_5d_pct} onChange={(event) => setForm({ ...form, benchmark_return_5d_pct: event.target.value })} className={INPUT_CLASS} placeholder="例如：0.80" />
                  </Field>
                  <Field label="基准近 20 期收益" hint="百分点">
                    <input type="number" step="0.01" value={form.benchmark_return_20d_pct} onChange={(event) => setForm({ ...form, benchmark_return_20d_pct: event.target.value })} className={INPUT_CLASS} placeholder="例如：1.60" />
                  </Field>
                  <Field label="近 5 期超额收益" hint="资产 − 基准，百分点">
                    <input type="number" step="0.01" value={form.excess_return_5d_pct} onChange={(event) => setForm({ ...form, excess_return_5d_pct: event.target.value })} className={INPUT_CLASS} placeholder="例如：0.45" />
                  </Field>
                  <Field label="近 20 期超额收益" hint="资产 − 基准，百分点">
                    <input type="number" step="0.01" value={form.excess_return_20d_pct} onChange={(event) => setForm({ ...form, excess_return_20d_pct: event.target.value })} className={INPUT_CLASS} placeholder="例如：-4.00" />
                  </Field>
                </div>
                <p className="mt-2 text-[9px] leading-relaxed text-faint">这里的 5/20 期指事件发生前的 5/20 个交易日。填写的信息需在事件公布前已知。</p>
                {form.manual_assets.length > 1 && <p className="mt-1 text-[9px] leading-relaxed text-mute">这组事前行情会用于全部关联资产；各资产数据不同时，请分别创建事件。</p>}
              </div>
            </div>
          </details>
          <button
            type="button"
            role="switch"
            aria-checked={form.generate_oracle}
            onClick={() => setForm({ ...form, generate_oracle: !form.generate_oracle })}
            className={cls(
              "flex w-full items-start justify-between gap-4 rounded-xl border px-3.5 py-3 text-left transition",
              form.generate_oracle
                ? "border-jade/25 bg-jade-soft/60"
                : "border-amber-200 bg-amber-50",
            )}
          >
            <span className="flex min-w-0 items-start gap-2.5">
              <TrendingUp size={14} className={cls("mt-0.5 shrink-0", form.generate_oracle ? "text-jade" : "text-amber-700")} />
              <span>
                <span className="block text-[10.5px] font-semibold text-ink">获取实际行情用于评估预测（推荐）</span>
                <span className="mt-0.5 block text-[9.5px] leading-relaxed text-mute">
                  {form.generate_oracle
                    ? "保存事件后获取对应资产与基准的实际行情，用于评估超额方向、收益率预测误差和参考收益。"
                    : "已关闭：本次只保存事件与预测结果，不计算真实市场表现，也不能进入正式 Arena 排名。"}
                </span>
              </span>
            </span>
            <span className={cls("relative mt-0.5 h-5 w-9 shrink-0 rounded-full transition", form.generate_oracle ? "bg-jade" : "bg-amber-300")}>
              <span className={cls("absolute top-0.5 h-4 w-4 rounded-full bg-card shadow-sm transition-all", form.generate_oracle ? "left-[18px]" : "left-0.5")} />
            </span>
          </button>
        </div>
      </div>

      <div id="create-manual-assets" tabIndex={-1} className="rounded-xl outline-none focus:ring-2 focus:ring-brand/15">
        <div className="mb-2 flex items-center justify-between gap-3">
          <div>
            <p className="text-[10.5px] font-semibold text-ink">
              {form.event_analysis_mode === "provided" ? "关联资产与已有预测" : "关联资产"}
            </p>
            <p className="mt-0.5 text-[9.5px] text-mute">
              {form.event_analysis_mode === "provided"
                ? "同一事件可关联多个资产；请分别填写各资产的预测方向、置信度和理由。"
                : "同一事件可关联多个股票、指数或期货，模型将分别预测各资产的表现。"}
            </p>
          </div>
          <button
            type="button"
            onClick={() => setForm({ ...form, manual_assets: [...form.manual_assets, newAsset(form.manual_assets.length)] })}
            className="inline-flex shrink-0 items-center gap-1 rounded-lg border border-edge bg-card px-2.5 py-1.5 text-[10.5px] font-medium text-mute transition hover:border-brand/30 hover:text-brand"
          >
            <Plus size={11} /> 添加资产
          </button>
        </div>
        <div className="space-y-2.5">
          {form.manual_assets.map((asset, index) => (
            <div key={asset.id} className="rounded-xl border border-edge bg-paper/70 p-3.5">
              <div className="mb-3 flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className="grid h-5 w-5 place-items-center rounded bg-ink font-mono text-[8.5px] text-card">{String(index + 1).padStart(2, "0")}</span>
                  <span className="text-[10.5px] font-semibold text-ink">关联资产</span>
                </div>
                {form.manual_assets.length > 1 && (
                  <button
                    type="button"
                    onClick={() => setForm({ ...form, manual_assets: form.manual_assets.filter((item) => item.id !== asset.id) })}
                    className="rounded p-1 text-faint transition hover:bg-rise/5 hover:text-rise"
                    title="移除此资产"
                  >
                    <Trash2 size={11} />
                  </button>
                )}
              </div>
              <div className={cls(
                "grid gap-2.5 sm:grid-cols-2",
                form.event_analysis_mode === "provided" && "lg:grid-cols-4",
              )}>
                <Field label="市场" required error={!asset.market.trim() ? "请选择市场" : undefined}>
                  <select
                    id={`create-asset-${asset.id}-market`}
                    value={asset.market}
                    onChange={(event) => updateAsset(asset.id, { market: event.target.value })}
                    aria-invalid={!asset.market.trim()}
                    className={cls(INPUT_CLASS, !asset.market.trim() && "border-rise/60")}
                  >
                    <option value="CN">中国 A 股</option>
                    <option value="HK">中国香港</option>
                    <option value="US">美国</option>
                    <option value="FUTURES">期货</option>
                    <option value="CRYPTO">加密资产 · 暂仅记录决策</option>
                    <option value="FX">外汇 · 暂仅记录决策</option>
                  </select>
                </Field>
                <Field label="标的代码" required error={!asset.symbol.trim() ? "请填写标的代码" : undefined}>
                  <input
                    id={`create-asset-${asset.id}-symbol`}
                    value={asset.symbol}
                    onChange={(event) => updateAsset(asset.id, { symbol: event.target.value })}
                    maxLength={80}
                    aria-invalid={!asset.symbol.trim()}
                    className={cls(INPUT_CLASS, !asset.symbol.trim() && "border-rise/60")}
                    placeholder="例如：600000.SH"
                  />
                </Field>
                {form.event_analysis_mode === "provided" && (
                  <>
                    <Field label="超额方向预测" hint="看涨或看跌，均相对市场基准" required error={!["up", "down"].includes(asset.analysis_direction) ? "请明确选择看涨或看跌" : undefined}>
                      <select
                        id={`create-asset-${asset.id}-direction`}
                        value={["up", "down"].includes(asset.analysis_direction) ? asset.analysis_direction : ""}
                        onChange={(event) => updateAsset(asset.id, { analysis_direction: event.target.value as ManualAssetDraft["analysis_direction"] })}
                        aria-invalid={!["up", "down"].includes(asset.analysis_direction)}
                        className={cls(INPUT_CLASS, !["up", "down"].includes(asset.analysis_direction) && "border-rise/60")}
                      >
                        <option value="">请选择，不预设</option>
                        <option value="up">看涨 · 正向超额表现</option>
                        <option value="down">看跌 · 负向超额表现</option>
                      </select>
                    </Field>
                    <Field
                      label={`置信度 · ${asset.confidence === null ? "未确认" : `${asset.confidence}%`}`}
                      required
                      hint="0–100，必须主动填写"
                      error={asset.confidence === null || !Number.isFinite(asset.confidence) || asset.confidence < 0 || asset.confidence > 100 ? "请确认 0–100 的置信度" : undefined}
                    >
                      <input
                        id={`create-asset-${asset.id}-confidence`}
                        type="number"
                        min={0}
                        max={100}
                        step={5}
                        value={asset.confidence ?? ""}
                        onChange={(event) => updateAsset(asset.id, { confidence: event.target.value === "" ? null : Number(event.target.value) })}
                        aria-invalid={asset.confidence === null || !Number.isFinite(asset.confidence) || asset.confidence < 0 || asset.confidence > 100}
                        className={cls(INPUT_CLASS, (asset.confidence === null || !Number.isFinite(asset.confidence) || asset.confidence < 0 || asset.confidence > 100) && "border-rise/60")}
                        placeholder="例如：70"
                      />
                    </Field>
                    <Field label="预期收益率 (%)" hint="可选；2 表示 +2%，留空表示无数值预测" error={asset.expected_return_pct.trim() && !Number.isFinite(Number(asset.expected_return_pct)) ? "请填写有效数值" : undefined}>
                      <input
                        id={`create-asset-${asset.id}-return`}
                        type="number"
                        step="any"
                        value={asset.expected_return_pct}
                        onChange={(event) => updateAsset(asset.id, { expected_return_pct: event.target.value })}
                        aria-invalid={Boolean(asset.expected_return_pct.trim() && !Number.isFinite(Number(asset.expected_return_pct)))}
                        className={INPUT_CLASS}
                        placeholder="例如：2.5 或 -1.2"
                      />
                    </Field>
                  </>
                )}
              </div>
              {form.event_analysis_mode === "provided" && (
                <div className="mt-2.5">
                  <Field label="分析理由" required hint="简要说明支持该预测的事件事实与判断依据" error={!asset.rationale.trim() ? "请填写分析理由" : undefined}>
                    <textarea
                      id={`create-asset-${asset.id}-rationale`}
                      value={asset.rationale}
                      onChange={(event) => updateAsset(asset.id, { rationale: event.target.value })}
                      maxLength={100_000}
                      aria-invalid={!asset.rationale.trim()}
                      className={cls(INPUT_CLASS, "min-h-[62px] resize-y leading-relaxed", !asset.rationale.trim() && "border-rise/60")}
                      placeholder="说明方向、影响期限和主要依据；无需提交保密策略逻辑…"
                    />
                  </Field>
                </div>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export function QuantEditor({ form, setForm, showAssetClass = true }: { form: CreateDraft; setForm: (next: CreateDraft) => void; showAssetClass?: boolean }) {
  const updateCondition = (id: string, patch: Partial<QuantConditionDraft>) => {
    setForm({ ...form, quant_conditions: form.quant_conditions.map((item) => item.id === id ? { ...item, ...patch } : item) });
  };
  const fieldMeta: Record<QuantConditionDraft["field"], {
    label: string;
    help: string;
    defaultLookback: number;
    entryThreshold: number;
    exitThreshold: number;
  }> = {
    price: { label: "收盘价", help: "当前 bar 收盘价，阈值使用行情原始计价单位。", defaultLookback: 1, entryThreshold: 0, exitThreshold: 0 },
    bar_return: { label: "单 bar 涨跌幅", help: "当前收盘价相对上一根 bar 收盘价的收益率。", defaultLookback: 1, entryThreshold: 0, exitThreshold: 0 },
    return: { label: "区间累计收益", help: "当前收盘价相对 N 根 bar 前收盘价的累计收益率。", defaultLookback: 20, entryThreshold: 3, exitThreshold: 0 },
    moving_average: { label: "价格 / 均线偏离", help: "收盘价相对 N 周期简单移动平均线的偏离率。", defaultLookback: 20, entryThreshold: 0, exitThreshold: 0 },
    ma_cross: { label: "快慢均线差", help: "快线相对慢线的百分比差；穿越 0 对应公开的均线交叉。", defaultLookback: 30, entryThreshold: 0, exitThreshold: 0 },
    breakout: { label: "区间突破幅度", help: "收盘价相对前 N 根 bar 最高价（向上）或最低价（向下）的偏离率。", defaultLookback: 20, entryThreshold: 0, exitThreshold: 0 },
    amplitude: { label: "区间振幅", help: "最近 N 根 bar 的（最高价 / 最低价 − 1）。", defaultLookback: 20, entryThreshold: 5, exitThreshold: 10 },
    volume_ratio: { label: "成交量 / 均量", help: "当前成交量除以前 N 根 bar 的平均成交量；1.5 表示 1.5 倍。", defaultLookback: 20, entryThreshold: 1.5, exitThreshold: 0.8 },
    volatility: { label: "收益波动率", help: "最近 N 个单 bar 收益率的总体标准差，不做隐式年化。", defaultLookback: 20, entryThreshold: 2, exitThreshold: 4 },
    rsi: { label: "RSI 强弱指标", help: "N 周期简式 RSI，范围 0–100；常见观察线为 30、50、70。", defaultLookback: 14, entryThreshold: 55, exitThreshold: 45 },
    bollinger_position: { label: "布林位置（Z-score）", help: "收盘价距离 N 周期均线的标准差倍数；+2 / −2 约对应上下轨。", defaultLookback: 20, entryThreshold: 0, exitThreshold: 0 },
    macd: { label: "MACD 柱 / 价格", help: "EMA 快慢线差减信号线，并除以收盘价实现跨资产可比。", defaultLookback: 26, entryThreshold: 0, exitThreshold: 0 },
  };
  const operatorLabels: Record<QuantConditionDraft["operator"], string> = {
    above: "高于阈值",
    below: "低于阈值",
    crosses_above: "向上穿越",
    crosses_below: "向下穿越",
  };
  const unitLabels: Record<ReturnType<typeof quantThresholdUnit>, string> = {
    price: "价格",
    percent: "%",
    multiple: "倍",
    index: "0–100",
    sigma: "σ",
  };
  const renderGroup = (phase: "entry" | "exit") => {
    const rows = form.quant_conditions.filter((item) => item.phase === phase);
    const combinator = phase === "entry" ? form.quant_entry_combinator : form.quant_exit_combinator;
    const setCombinator = (next: "and" | "or") => setForm({
      ...form,
      ...(phase === "entry" ? { quant_entry_combinator: next } : { quant_exit_combinator: next }),
    });
    return (
      <div className="min-w-0 rounded-xl border border-edge bg-card p-3">
        <div className="mb-2.5 flex items-center justify-between gap-3">
          <div>
            <p className="text-[10.5px] font-semibold text-ink">{phase === "entry" ? "开仓条件" : "平仓条件"}</p>
            <p className="mt-0.5 text-[9px] text-faint">{combinator === "and" ? "全部条件同时满足" : "任一条件满足"}</p>
          </div>
          <div className="flex items-center gap-1.5">
            <select
              aria-label={`${phase === "entry" ? "开仓" : "平仓"}条件组合方式`}
              value={combinator}
              onChange={(event) => setCombinator(event.target.value as "and" | "or")}
              className="rounded-md border border-edge bg-card px-2 py-1 text-[9.5px] font-medium text-mute outline-none focus:border-brand/60"
            >
              <option value="and">AND · 全部</option>
              <option value="or">OR · 任一</option>
            </select>
            <button
              type="button"
              onClick={() => setForm({ ...form, quant_conditions: [...form.quant_conditions, newQuantCondition(phase, rows.length)] })}
              className="inline-flex items-center gap-1 rounded-md border border-edge px-2 py-1 text-[9.5px] font-medium text-mute transition hover:text-ink"
            >
              <Plus size={10} /> 添加条件
            </button>
          </div>
        </div>
        <div className="space-y-2">
          {rows.length === 0 ? (
            <div className="rounded-lg border border-dashed border-edge px-3 py-3 text-center text-[9.5px] text-faint">尚未定义{phase === "entry" ? "开仓" : "平仓"}条件</div>
          ) : rows.map((condition, index) => {
            const meta = fieldMeta[condition.field];
            const unit = quantThresholdUnit(condition.field);
            const validationError = quantConditionError(condition);
            const dualPeriod = condition.field === "ma_cross" || condition.field === "macd";
            const showLookback = !["price", "bar_return", "ma_cross", "macd"].includes(condition.field);
            const warmup = condition.field === "macd"
              ? condition.slow_period + condition.signal_period - 2
              : condition.field === "ma_cross"
                ? condition.slow_period - 1
                : condition.field === "price"
                  ? 0
                  : condition.field === "bar_return"
                    ? 1
                    : ["moving_average", "amplitude", "bollinger_position"].includes(condition.field)
                      ? Math.max(0, condition.lookback - 1)
                      : condition.lookback;
            return (
              <div
                id={`create-quant-${condition.id}`}
                key={condition.id}
                tabIndex={-1}
                className={cls(
                  "grid min-w-0 grid-cols-[24px_minmax(0,1fr)_26px] items-start gap-2 rounded-lg bg-paper p-2.5 outline-none focus:ring-2 focus:ring-rise/20",
                  validationError && "border border-rise/20",
                )}
              >
                <span className="mt-[19px] grid h-6 w-6 place-items-center rounded bg-edge/70 font-mono text-[8.5px] text-mute">{index + 1}</span>
                <div className="min-w-0">
                  <div className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2">
                    <label className="block min-w-0">
                      <span className="mb-1 block text-[8.5px] text-faint">技术因子</span>
                      <select
                        value={condition.field}
                        onChange={(event) => {
                          const field = event.target.value as QuantConditionDraft["field"];
                          const nextMeta = fieldMeta[field];
                          updateCondition(condition.id, {
                            field,
                            lookback: nextMeta.defaultLookback,
                            fast_period: field === "macd" ? 12 : 10,
                            slow_period: field === "macd" ? 26 : 30,
                            signal_period: 9,
                            threshold: phase === "entry" ? nextMeta.entryThreshold : nextMeta.exitThreshold,
                          });
                        }}
                        className={INPUT_CLASS}
                      >
                        {(Object.keys(fieldMeta) as QuantConditionDraft["field"][]).map((key) => <option key={key} value={key}>{fieldMeta[key].label}</option>)}
                      </select>
                    </label>
                    <label className="block min-w-0">
                      <span className="mb-1 block text-[8.5px] text-faint">比较关系</span>
                      <select value={condition.operator} onChange={(event) => updateCondition(condition.id, { operator: event.target.value as QuantConditionDraft["operator"] })} className={INPUT_CLASS}>
                        {(Object.keys(operatorLabels) as QuantConditionDraft["operator"][]).map((key) => <option key={key} value={key}>{operatorLabels[key]}</option>)}
                      </select>
                    </label>
                  </div>
                  <div className="mt-2 grid min-w-0 grid-cols-2 gap-2 sm:grid-cols-3">
                    {showLookback && (
                      <label className="block min-w-0">
                        <span className="mb-1 block text-[8.5px] text-faint">回看周期 N</span>
                        <input type="number" min={1} max={10000} step={1} value={condition.lookback} onChange={(event) => updateCondition(condition.id, { lookback: Math.max(1, Math.trunc(Number(event.target.value) || 1)) })} className={INPUT_CLASS} />
                      </label>
                    )}
                    {dualPeriod && (
                      <>
                        <label className="block min-w-0">
                          <span className="mb-1 block text-[8.5px] text-faint">快线周期</span>
                          <input type="number" min={1} max={9999} step={1} value={condition.fast_period} onChange={(event) => updateCondition(condition.id, { fast_period: Math.max(1, Math.trunc(Number(event.target.value) || 1)) })} className={INPUT_CLASS} />
                        </label>
                        <label className="block min-w-0">
                          <span className="mb-1 block text-[8.5px] text-faint">慢线周期</span>
                          <input type="number" min={2} max={10000} step={1} value={condition.slow_period} onChange={(event) => updateCondition(condition.id, { slow_period: Math.max(2, Math.trunc(Number(event.target.value) || 2)) })} className={INPUT_CLASS} />
                        </label>
                      </>
                    )}
                    {condition.field === "macd" && (
                      <label className="block min-w-0">
                        <span className="mb-1 block text-[8.5px] text-faint">信号周期</span>
                        <input type="number" min={1} max={10000} step={1} value={condition.signal_period} onChange={(event) => updateCondition(condition.id, { signal_period: Math.max(1, Math.trunc(Number(event.target.value) || 1)) })} className={INPUT_CLASS} />
                      </label>
                    )}
                    <label className="block min-w-0">
                      <span className="mb-1 block truncate text-[8.5px] text-faint">比较阈值 · {unitLabels[unit]}</span>
                      <input type="number" step="0.01" value={condition.threshold} onChange={(event) => updateCondition(condition.id, { threshold: Number(event.target.value) || 0 })} className={INPUT_CLASS} />
                    </label>
                    <label className="block min-w-0">
                      <span className="mb-1 block text-[8.5px] text-faint">连续变化次数</span>
                      <input type="number" min={1} max={10000} step={1} value={condition.consecutive_count ?? 1} onChange={(event) => updateCondition(condition.id, { consecutive_count: Number(event.target.value) })} className={INPUT_CLASS} />
                      <span className="mt-1 block text-[8.5px] leading-relaxed text-faint">默认 1 次 · 连续满足本条条件的 K 线数</span>
                    </label>
                  </div>
                  <div className="mt-2 flex flex-wrap items-start justify-between gap-x-3 gap-y-1 text-[8.5px] leading-relaxed text-faint">
                    <span className="min-w-0 flex-1">{meta.help}</span>
                    <span className="shrink-0 font-mono">warmup {warmup} bars</span>
                  </div>
                  <p className="mt-1.5 text-[8.5px] leading-relaxed text-mute">
                    {condition.field === "bar_return"
                      ? condition.operator === "crosses_above"
                        ? "比较阈值是单根 K 线收益率的触发边界，不是容错率。0% 是有效阈值：上一根收益率 ≤ 0%、当前 > 0% 时向上穿越；0 不表示未填写。"
                        : condition.operator === "crosses_below"
                          ? "比较阈值是单根 K 线收益率的触发边界，不是容错率。0% 是有效阈值：上一根收益率 ≥ 0%、当前 < 0% 时向下穿越；0 不表示未填写。"
                          : condition.operator === "above"
                            ? "比较阈值是单根 K 线收益率的判断边界，不是容错率。0% 是有效阈值：当前收益率严格大于 0% 时满足；0 不表示未填写。"
                            : "比较阈值是单根 K 线收益率的判断边界，不是容错率。0% 是有效阈值：当前收益率严格小于 0% 时满足；0 不表示未填写。"
                      : `比较阈值是“${meta.label}”执行“${operatorLabels[condition.operator]}”判定时使用的边界，单位为${unitLabels[unit]}。`}
                  </p>
                  {(condition.consecutive_count ?? 1) > 1 && <p className="mt-1.5 text-[8.5px] leading-relaxed text-mute">{condition.operator.startsWith("crosses_")
                    ? `穿越当根计第 1 次，之后持续保持在阈值${condition.operator === "crosses_above" ? "上" : "下"}方，满 ${condition.consecutive_count} 次确认；中断后等待下一次穿越。`
                    : `连续 ${condition.consecutive_count} 根 K 线均${condition.operator === "above" ? "高于" : "低于"}阈值才满足条件；中断后重新计数。`}</p>}
                  {validationError && <p className="mt-1.5 text-[8.5px] text-rise">{validationError}</p>}
                </div>
                <button type="button" onClick={() => setForm({ ...form, quant_conditions: form.quant_conditions.filter((item) => item.id !== condition.id) })} className="mt-[19px] grid h-6 w-6 place-items-center rounded text-faint transition hover:bg-rise/5 hover:text-rise" title="删除条件"><Trash2 size={11} /></button>
              </div>
            );
          })}
        </div>
      </div>
    );
  };

  return (
    <div id="create-quant-rules" tabIndex={-1} className="min-w-0 overflow-hidden rounded-xl border border-jade/20 bg-jade-soft/35 p-3.5 outline-none focus:ring-2 focus:ring-jade/20">
      <div className="mb-3 flex items-start gap-2.5">
        <LockKeyhole size={15} className="mt-0.5 shrink-0 text-jade" />
        <div>
          <p className="text-[10.5px] font-semibold text-ink">通用量化策略</p>
          <p className="mt-0.5 text-[9.5px] leading-relaxed text-mute">使用公开条件组件，或只引用你的私有策略版本。这里不预置、展示或猜测任何保密交易逻辑。</p>
        </div>
      </div>
      <div className="mb-3 flex items-start gap-2 rounded-lg border border-jade/15 bg-card/70 px-3 py-2.5 text-[9.5px] leading-relaxed text-mute">
        <Workflow size={11} className="mt-0.5 shrink-0 text-jade" />
        每个行情周期收盘时判断条件，下一周期开盘模拟成交。开仓和平仓可分别设为满足全部或任一条件；指标只使用当时及以前的数据。外部算法可选择“外部量化服务”提供仓位信号。
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        {showAssetClass && <Field label="资产类别">
          <select
            value={form.quant_asset_class}
            onChange={(event) => setForm({ ...form, quant_asset_class: event.target.value as CreateDraft["quant_asset_class"] })}
            className={INPUT_CLASS}
          >
            <option value="index">指数</option>
            <option value="equity">股票</option>
            <option value="futures">期货</option>
          </select>
        </Field>}
        <Field label="策略版本引用" hint="不填写内部逻辑">
          <input
            value={form.strategy_version}
            onChange={(event) => setForm({ ...form, strategy_version: event.target.value })}
            className={INPUT_CLASS}
            placeholder="例如：index-alpha@1.2"
          />
        </Field>
      </div>
      <div className="mt-3 grid min-w-0 gap-3 2xl:grid-cols-2">{renderGroup("entry")}{renderGroup("exit")}</div>
      <p className="mt-3 rounded-lg border border-edge bg-card/70 px-2.5 py-2 text-[9.5px] leading-relaxed text-mute">
        策略回测需要通过校验的历史行情。所选频率缺少数据时，实验会显示不可用，请先补齐对应行情。
      </p>
    </div>
  );
}

function SignalEditor({
  form,
  setForm,
  eventMode = false,
  adapterError,
  allowPrivateEndpoints = false,
}: {
  form: CreateDraft;
  setForm: (next: CreateDraft) => void;
  eventMode?: boolean;
  adapterError?: string;
  allowPrivateEndpoints?: boolean;
}) {
  const secretRefError = secretEnvRefError(form.external_secret_env_ref, "strategy");
  const secretRefValid = !secretRefError;
  const endpointError = endpointValidationError(form.external_api_ref, allowPrivateEndpoints);
  const headerError = form.external_secret_env_ref && !form.external_header_name.trim() ? "使用密钥时必须填写请求头名" : undefined;
  return (
    <div className="rounded-xl border border-violet/20 bg-violet-soft/35 p-3.5">
      <div className="mb-3 flex items-start gap-2.5">
        <FileInput size={15} className="mt-0.5 shrink-0 text-violet" />
        <div>
          <p className="text-[10.5px] font-semibold text-ink">{eventMode ? "外部事件分析 API" : "外部决策 API 适配器"}</p>
          <p className="mt-0.5 text-[9.5px] leading-relaxed text-mute">{eventMode ? "API 接收历史时点可得事件，返回方向、置信度、理由及预期收益率。Qwen、DeepSeek 的聊天 API 请使用“外部大模型（直接调用）”。" : "API 输出标准 target_weight 信号，平台在同一冻结行情上独立撮合。"} 只保存 endpoint 和密钥环境变量名。</p>
        </div>
      </div>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="接口返回格式" hint="当前支持的固定格式">
          <div className={INPUT_CLASS}>{eventMode ? "事件预测结果 · v1" : "目标仓位结果 · v1"}</div>
          <p className="mt-1.5 text-[9px] leading-relaxed text-mute">{eventMode ? "direction、confidence、rationale 表示方向判断；expected_return_pct 表示预期收益率，例如 2 表示 +2%。只有显式提供数值才评估收益率误差。" : "服务返回 target_weight；平台按冻结行情模拟该目标仓位的交易。"}</p>
        </Field>
        <Field label="信号版本引用" hint="用于审计与复现">
          <input
            value={form.signal_version}
            onChange={(event) => setForm({ ...form, signal_version: event.target.value })}
            maxLength={80}
            className={INPUT_CLASS}
            placeholder="例如：vendor-signal@2026-09"
          />
        </Field>
        <Field label="预测接口地址" required hint={allowPrivateEndpoints ? "本机 / 私网 HTTP 已获服务端允许" : "公网 HTTPS"} error={endpointError ?? undefined}>
          <input
            id="create-external-endpoint"
            value={form.external_api_ref}
            onChange={(event) => setForm({ ...form, external_api_ref: event.target.value })}
            maxLength={4_000}
            aria-invalid={Boolean(endpointError)}
            className={cls(INPUT_CLASS, endpointError && "border-rise/60 focus:border-rise/60")}
            placeholder="https://strategy.example.com/signal"
          />
        </Field>
        <Field label="请求头名" hint="常用 Authorization 或 X-API-Key" error={headerError}>
          <input
            id="create-external-header"
            value={form.external_header_name}
            onChange={(event) => setForm({ ...form, external_header_name: event.target.value })}
            aria-invalid={Boolean(headerError)}
            className={cls(INPUT_CLASS, headerError && "border-rise/60")}
            placeholder="Authorization"
          />
        </Field>
        <Field label="策略密钥环境变量名" hint={`仅允许 ${STRATEGY_SECRET_ENV_PREFIX} 前缀；变量值由服务端或分享版启动器配置`} error={secretRefError}>
          <input
            id="create-external-secret"
            value={form.external_secret_env_ref}
            onChange={(event) => setForm({ ...form, external_secret_env_ref: event.target.value })}
            aria-invalid={!secretRefValid}
            className={cls(INPUT_CLASS, !secretRefValid && "border-rise/60")}
            placeholder="PRONOIA_STRATEGY_SECRET_VENDOR"
            autoCapitalize="characters"
            spellCheck={false}
          />
        </Field>
      </div>
      <div className="mt-3 flex items-start gap-2 rounded-lg border border-violet/15 bg-card/70 px-3 py-2.5 text-[9.5px] leading-relaxed text-mute">
        <ShieldCheck size={11} className="mt-0.5 shrink-0 text-violet" />
        {allowPrivateEndpoints ? "服务端已允许可信本机与私网接口使用 HTTP；公网接口仍需 HTTPS。" : "当前仅允许公网 HTTPS 接口。本机或私网模型需先由服务端开启访问权限。"} 密钥填写 PRONOIA_STRATEGY_SECRET_* 环境变量名；地址访问与接口返回由服务端再次校验。
      </div>
      {adapterError && (
        <div className="mt-2 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-[9.5px] leading-relaxed text-amber-800">
          <AlertTriangle size={11} className="mt-0.5 shrink-0" /> {adapterError}
        </div>
      )}
    </div>
  );
}

function DraftSummary({
  form,
  setForm,
  selectedDataset,
  selectedMarketDataset,
  usesManualEvent,
  eventModelLabel,
  qaSummary,
  qaModelsSummary,
  scoringSummary,
  savedModelName,
}: {
  form: CreateDraft;
  setForm: (next: CreateDraft) => void;
  selectedDataset?: BTDataset;
  selectedMarketDataset?: BTDataset;
  usesManualEvent: boolean;
  eventModelLabel?: string;
  qaSummary?: string;
  qaModelsSummary?: string;
  scoringSummary?: string;
  savedModelName?: string;
}) {
  const quantForecast = form.strategy_type === "quant" && form.quant_mode === "return_forecast";
  const visibilityLabels: Record<Visibility, string> = {
    private: "仅自己可见",
    team: "团队可见",
    arena_safe: "Arena 安全摘要",
  };
  return (
    <aside className="space-y-3 lg:sticky lg:top-0">
      <div className="overflow-hidden rounded-xl border border-edge bg-card shadow-card">
        <div className="border-b border-edge bg-ink px-4 py-3.5 text-card">
          <div className="flex items-center justify-between">
            <p className="text-[10px] font-semibold tracking-wide text-card/55">本次实验</p>
            <span className="rounded-full border border-card/20 bg-card/10 px-2 py-0.5 text-[8.5px] font-semibold tracking-wider">未创建</span>
          </div>
          <p className="mt-2 truncate font-serif text-[14px] font-semibold">{form.name || "未命名实验"}</p>
          <div className="mt-2"><StrategyBadge type={form.strategy_type} compact /></div>
        </div>
        <dl className="divide-y divide-edge/70 px-4 text-[10.5px]">
          <SummaryRow label="实际行情" value={form.strategy_type === "event" ? usesManualEvent ? form.generate_oracle ? "保存事件时获取实际行情" : "未启用行情评估" : selectedDataset?.labels_path ? "使用事件集已关联行情" : "尚未关联实际行情" : selectedMarketDataset?.name || "尚未选择"} />
          {form.strategy_type !== "event" && <SummaryRow label="版本" value={selectedMarketDataset?.dataset_version ?? selectedMarketDataset?.version ?? "未冻结"} mono />}
          {form.strategy_type === "event" && <SummaryRow label="事件时间" value={usesManualEvent ? form.event_timezone : "沿用事件集时间"} mono />}
          <SummaryRow label="输入" value={form.strategy_type === "event" ? (usesManualEvent ? `手工事件 · ${form.manual_assets.length} 个资产` : selectedDataset?.name || "尚未选择") : form.strategy_type === "quant" ? quantForecast ? "冻结历史行情" : "交易条件构建器" : "外部 API 适配器"} />
          <SummaryRow
            label={form.strategy_type === "event" ? "预测来源" : "模型 / 策略"}
            value={
              savedModelName || (form.strategy_type === "quant"
                ? quantForecast ? "历史 N 周期收益均值预测" : "交易策略 · 下一周期开盘成交"
                : form.strategy_type === "signal_import"
                  ? "第三方仓位信号 · 模拟交易"
                  : eventModelLabel ?? "Pronoia · 统一平台基模")
            }
            wrap
            mono
          />
          {form.strategy_type === "event" && <SummaryRow label="问答" value={qaSummary ?? "未启用"} />}
          {form.strategy_type === "event" && qaModelsSummary && <SummaryRow label="问答模型" value={qaModelsSummary} wrap />}
          {form.strategy_type === "event" && scoringSummary && <SummaryRow label="评分模型" value={scoringSummary} wrap />}
          <SummaryRow
            label="评测窗口"
            value={quantForecast
              ? `未来 ${form.quant_forecast.horizon_bars} 个 ${form.bar_frequency} 周期 · 收益率误差`
              : form.strategy_type === "event"
              ? `${displayHorizon(form.event_horizon)} · ${form.benchmark === "dataset_default" ? "数据集基准" : form.benchmark}`
              : `${form.bar_frequency} · 数据资产买入持有`}
          />
          <SummaryRow label="收益计算" value={quantForecast ? "当前收盘 → 未来第 N 周期收盘" : form.strategy_type === "event" ? "事件窗收盘价（盘后顺延）" : "收盘生成信号 · 次期开盘模拟成交"} />
          <SummaryRow
            label="成本"
            value={quantForecast
              ? "纯收益率预测 · 不模拟交易"
              : form.strategy_type === "event"
              ? `参考成本 ${form.transaction_cost_bps} · 滑点 ${form.slippage_bps} bps`
              : `佣金 ${form.transaction_cost_bps} · 滑点 ${form.slippage_bps} · 卖出税 ${form.stamp_duty_bps} · 其他 ${form.other_cost_bps} bps${form.minimum_commission > 0 ? ` · 最低佣金 ${form.minimum_commission}` : ""}`}
          />
        </dl>
        <div className="border-t border-edge bg-paper/70 px-4 py-3">
          <div className="flex items-center gap-2 text-[10px] font-medium text-jade">
            <ShieldCheck size={12} /> 保存本次数据与评测设置
          </div>
          <p className="mt-1 text-[9.5px] leading-relaxed text-faint">{form.strategy_type === "event" ? "事件事实、实际行情和评测设置一致的结果，可在 Arena 中比较。" : "使用相同数据版本和评测设置的结果，可在 Arena 中比较。"}</p>
        </div>
      </div>

      <div className="rounded-xl border border-edge bg-card p-4 shadow-card">
        <div className="mb-2 flex items-center gap-2">
          <LockKeyhole size={13} className="text-jade" />
          <p className="text-[10.5px] font-semibold text-ink">可见性与保密</p>
        </div>
        <div className="space-y-1.5">
          {(["private", "arena_safe"] as Visibility[]).map((visibility) => (
            <label key={visibility} className={cls(
              "flex items-start gap-2 rounded-lg border border-transparent px-2 py-1.5 transition",
              "cursor-pointer hover:border-edge hover:bg-paper",
            )}>
              <input
                type="radio"
                name="visibility-preview"
                checked={form.visibility === visibility}
                onChange={() => setForm({ ...form, visibility })}
                className="mt-0.5 accent-ink"
              />
              <span>
                <span className="block text-[10px] font-medium text-ink">{visibilityLabels[visibility]}</span>
                <span className="mt-0.5 block text-[9px] leading-snug text-faint">
                  {visibility === "private"
                    ? "作为私有实验查看完整结果。"
                    : "分享汇总成绩，隐藏逐条预测与分析过程。"}
                </span>
              </span>
            </label>
          ))}
        </div>
        <p className="mt-2 rounded-lg bg-jade-soft/55 px-2.5 py-2 text-[9px] leading-relaxed text-jade">API Key 不在结果摘要中展示；量化策略的具体规则也不会进入 Arena 分享摘要。</p>
      </div>
    </aside>
  );
}

function SummaryRow({ label, value, mono = false, wrap = false }: { label: string; value: string; mono?: boolean; wrap?: boolean }) {
  return (
    <div className="grid grid-cols-[58px_minmax(0,1fr)] gap-2 py-2.5">
      <dt className="text-faint">{label}</dt>
      <dd className={cls("text-right font-medium text-ink", wrap ? "whitespace-pre-line break-words" : "truncate", mono && "font-mono text-[9.5px]")} title={value}>{value}</dd>
    </div>
  );
}

export type BacktestSection = "event" | "quant" | "runs";

export default function BacktestList({ section = "runs", embedded = false, refreshVersion = 0, modelId, modelRunIds }: {
  section?: BacktestSection;
  embedded?: boolean;
  refreshVersion?: number;
  modelId?: string;
  modelRunIds?: ReadonlySet<string>;
}) {
  const btRuns = useStore((state) => state.btRuns);
  const btRunsLoading = useStore((state) => state.btRunsLoading);
  const loadBTRuns = useStore((state) => state.loadBTRuns);
  const patchBTRun = useStore((state) => state.patchBTRun);
  const openBTDetail = useStore((state) => state.openBTDetail);
  const setView = useStore((state) => state.setView);

  const [modalOpen, setModalOpen] = useState(false);
  const [createType, setCreateType] = useState<StrategyType>(section === "quant" ? "quant" : "event");
  const [createEventMode, setCreateEventMode] = useState<CreateDraft["event_analysis_mode"]>("platform");
  const [createExternalMode, setCreateExternalMode] = useState<CreateDraft["external_model_mode"]>("profile");
  const [createQuantMode, setCreateQuantMode] = useState<CreateDraft["quant_mode"]>("return_forecast");
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [actingId, setActingId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const [typeFilter, setTypeFilter] = useState<"all" | StrategyType>("all");
  const [viewMode, setViewMode] = useState<ViewMode>("table");
  const [metricA, setMetricA] = useState("strategy_total_return");
  const [metricB, setMetricB] = useState("strategy_max_drawdown");
  const [metricDefs, setMetricDefs] = useState<Record<string, BTMetricDef>>({});
  const [metricPickerFor, setMetricPickerFor] = useState<"A" | "B" | null>(null);

  useEffect(() => {
    void loadBTRuns();
    void api
      .btMetricDefs()
      .then((defs) => setMetricDefs(defs))
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => { if (refreshVersion > 0) void loadBTRuns(true); }, [refreshVersion, loadBTRuns]);

  const metricsArr = useMemo(() => Object.entries(metricDefs), [metricDefs]);
  const scopedRuns = useMemo(() => btRuns.filter((run) => {
    if (modelRunIds && !modelRunIds.has(run.id) && run.config?.saved_model_id !== modelId) return false;
    const type = getRunStrategyType(run);
    if (section === "event") return type === "event";
    if (section === "quant") return type === "quant" || type === "signal_import";
    return true;
  }), [btRuns, section, modelId, modelRunIds]);
  const filteredRuns = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return scopedRuns.filter((run) => {
      if (statusFilter !== "all" && run.status !== statusFilter) return false;
      if (typeFilter !== "all" && getRunStrategyType(run) !== typeFilter) return false;
      if (!normalized) return true;
      const haystack = [
        run.name,
        run.id,
        run.runner,
        run.dataset_name,
        run.dataset_id,
        run.model_version,
        run.prompt_variant,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(normalized);
    });
  }, [scopedRuns, query, statusFilter, typeFilter]);

  const overview = useMemo(() => {
    const running = scopedRuns.filter((run) => run.status === "running" || run.status === "paused").length;
    const done = scopedRuns.filter((run) => run.status === "done").length;
    const totalEvents = scopedRuns.reduce((sum, run) => sum + Math.max(run.total_events || 0, run.done_events || 0), 0);
    const doneEvents = scopedRuns.reduce((sum, run) => sum + (run.done_events || 0), 0);
    const protocolCount = new Set(scopedRuns.map(getProtocolHash).filter(Boolean)).size;
    return { running, done, totalEvents, doneEvents, protocolCount };
  }, [scopedRuns]);

  const getMetric = (run: BTRun, id: string): number | null | undefined => {
    const metric = run.metrics?.[id];
    if (metric && typeof metric.value === "number") return metric.value;
    if (id === "acc_t3_strict") return run.acc_t3_strict;
    if (id === "acc_t3_non_neutral" || id === "acc_primary_non_neutral") return run.acc_t3_non_neutral;
    return undefined;
  };

  const getMetricLo = (run: BTRun, id: string): number | null | undefined => {
    const metric = run.metrics?.[id];
    const wilson = metric?.breakdown?.wilson;
    if (wilson && typeof wilson === "object" && "lo_95" in wilson) {
      const value = (wilson as { lo_95?: unknown }).lo_95;
      if (typeof value === "number") return value;
    }
    return id === "acc_t3_strict" ? run.acc_t3_strict_lo : undefined;
  };

  const openCreate = (type: StrategyType, options: {
    eventAnalysisMode?: CreateDraft["event_analysis_mode"];
    externalModelMode?: CreateDraft["external_model_mode"];
    quantMode?: CreateDraft["quant_mode"];
  } = {}) => {
    setCreateType(type);
    setCreateEventMode(options.eventAnalysisMode ?? "platform");
    setCreateExternalMode(options.externalModelMode ?? "profile");
    setCreateQuantMode(options.quantMode ?? "return_forecast");
    setModalOpen(true);
  };

  const onCreate = async (data: CreateRequest) => {
    const { event_qa, prediction_profile_id, auto_start, ...prediction } = data;
    const run = data.strategy_type === "event"
      ? (await api.btCreateEventExperiment({ prediction, prediction_profile_id, qa: event_qa, auto_start: auto_start ?? true })).run
      : await api.btCreateRun(prediction);
    await loadBTRuns(true);
    openBTDetail(run.id);
  };

  const doStart = async (id: string) => {
    setActingId(id);
    setActionErr(null);
    try {
      patchBTRun(id, { status: "running", started_at: new Date().toISOString() });
      const result = await api.btStartRun(id);
      if (!result.ok) throw new Error(result.message || "start failed");
      openBTDetail(id);
    } catch (error) {
      setActionErr(`${id.slice(0, 8)}… 启动失败：${error instanceof Error ? error.message : String(error)}`);
      void loadBTRuns(true);
    } finally {
      setActingId(null);
    }
  };

  const doPause = async (id: string) => {
    setActingId(id);
    setActionErr(null);
    try {
      patchBTRun(id, { status: "paused" });
      const result = await api.btPauseRun(id);
      if (!result.ok) throw new Error(result.message || "pause failed");
    } catch (error) {
      setActionErr(`${id.slice(0, 8)}… 暂停失败：${error instanceof Error ? error.message : String(error)}`);
      void loadBTRuns(true);
    } finally {
      setActingId(null);
    }
  };

  const doResume = async (id: string) => {
    setActingId(id);
    setActionErr(null);
    try {
      patchBTRun(id, { status: "running" });
      const result = await api.btResumeRun(id);
      if (!result.ok) throw new Error(result.message || "resume failed");
    } catch (error) {
      setActionErr(`${id.slice(0, 8)}… 继续失败：${error instanceof Error ? error.message : String(error)}`);
      void loadBTRuns(true);
    } finally {
      setActingId(null);
    }
  };

  const doCancel = async (id: string) => {
    setActingId(id);
    setActionErr(null);
    try {
      const result = await api.btCancelRun(id);
      if (!result.ok) throw new Error(result.message || "cancel failed");
      patchBTRun(id, { status: "cancelled" });
    } catch (error) {
      setActionErr(`${id.slice(0, 8)}… 取消失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setActingId(null);
    }
  };

  const doDelete = async (run: BTRun) => {
    if (!window.confirm(`确认删除回测“${run.name || run.id}”？该操作会移除运行记录。`)) return;
    setActingId(run.id);
    setActionErr(null);
    try {
      const result = await api.btDeleteRun(run.id);
      if (!result.ok) throw new Error(result.message || "delete failed");
      await loadBTRuns(true);
    } catch (error) {
      setActionErr(`${run.id.slice(0, 8)}… 删除失败：${error instanceof Error ? error.message : String(error)}`);
    } finally {
      setActingId(null);
    }
  };

  const actions = (run: BTRun): RunActionProps => ({
    run,
    busy: actingId === run.id,
    onStart: () => void doStart(run.id),
    onPause: () => void doPause(run.id),
    onResume: () => void doResume(run.id),
    onCancel: () => void doCancel(run.id),
    onDelete: () => void doDelete(run),
    onOpen: () => openBTDetail(run.id),
  });

  return (
    <div className={embedded ? "min-w-0" : "flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper"}>
      {!embedded && <BacktestNav
        actions={(
          <div className="flex items-center gap-2">
            <button onClick={() => void loadBTRuns(true)} disabled={btRunsLoading} className={BUTTON_GHOST} title="刷新运行记录">
              <RefreshCw size={12} className={cls(btRunsLoading && "animate-spin")} />
              <span className="hidden sm:inline">刷新</span>
            </button>
            {section !== "runs" && (
              <button
                onClick={() => openCreate(section === "quant" ? "quant" : "event")}
                className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-3.5 py-2 text-[12px] font-semibold text-card shadow-card transition hover:bg-ink/90 hover:shadow-pop"
              >
                <Plus size={13} /> 新建{section === "event" ? "事件" : "量化"}实验
              </button>
            )}
          </div>
        )}
      />}

      <div className={embedded ? "min-w-0" : "min-h-0 flex-1 overflow-y-auto"}>
        <div className={embedded ? "w-full min-w-0" : "mx-auto w-full max-w-[1500px] px-5 py-6 sm:px-7 lg:px-9"}>
          {!embedded && <section className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
            <div>
              <div className="flex items-center gap-2">
                <span className={cls(
                  "text-[9px] font-semibold uppercase tracking-[0.2em]",
                  section === "quant" ? "text-jade" : section === "runs" ? "text-mute" : "text-brand",
                )}>
                  {section === "event" ? "Event intelligence" : section === "quant" ? "Systematic research" : "Experiment registry"}
                </span>
                <span className="h-px w-8 bg-brand/35" />
                <span className="text-[9.5px] text-faint">
                  {section === "event" ? "事件方向、收益率预测与可选问答" : section === "quant" ? "收益率预测与交易策略评测" : "统一查看所有历史实验"}
                </span>
              </div>
              <h1 className="mt-2 font-serif text-[25px] font-semibold tracking-wide text-ink sm:text-[28px]">
                {section === "event" ? "事件模型" : section === "quant" ? "量化模型" : "运行记录"}
              </h1>
              <p className="mt-1.5 max-w-2xl text-[11.5px] leading-relaxed text-mute">
                {section === "event"
                  ? "在同一事件集上比较 Pronoia 与外部独立模型，评测方向预测和未来 T+N 收益率预测的数值误差；问答可作为独立的可选任务。"
                  : section === "quant"
                    ? "用冻结的真实行情预测未来 N 周期收益率，并对照实际收益评估误差。交易策略回测另行配置开平仓规则、撮合时点与成本，分别查看结果。"
                    : "查看每一次实验的状态、数据版本、协议和核心指标。历史结果不会因默认模型或新设置而改变。"}
              </p>
              <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[9.5px]">
                {section === "event" ? (
                  <>
                    <span className="inline-flex items-center gap-1 rounded-full border border-brand/20 bg-brand-soft/45 px-2 py-1 font-semibold text-brand"><Newspaper size={10} /> 方向 + 收益率预测</span>
                    <span className="rounded-full border border-edge bg-card px-2 py-1 text-mute">问答测试 · 可选</span>
                    <span className="rounded-full border border-edge bg-card px-2 py-1 text-mute">两类任务独立运行</span>
                  </>
                ) : section === "quant" ? (
                  <>
                    <span className="inline-flex items-center gap-1 rounded-full border border-jade/20 bg-jade-soft/55 px-2 py-1 font-semibold text-jade"><CandlestickChart size={10} /> 未来 N 周期收益率</span>
                    <span className="rounded-full border border-edge bg-card px-2 py-1 text-mute">预测值与实际值对照</span>
                    <span className="rounded-full border border-edge bg-card px-2 py-1 text-mute">无问答测试</span>
                  </>
                ) : (
                  <>
                    <span className="rounded-full border border-edge bg-card px-2 py-1 text-mute">统一搜索与筛选</span>
                    <span className="rounded-full border border-edge bg-card px-2 py-1 text-mute">动态指标列</span>
                    <span className="rounded-full border border-edge bg-card px-2 py-1 text-mute">历史配置与成绩保留</span>
                  </>
                )}
              </div>
            </div>
            <div className="flex items-center gap-2 rounded-xl border border-edge bg-card px-3 py-2 shadow-card">
              <ShieldCheck size={14} className="text-jade" />
              <div>
                <p className="text-[9px] font-semibold uppercase tracking-wider text-faint">不可变实验</p>
                <p className="text-[10.5px] font-medium text-ink">数据版本与协议随 Run 冻结</p>
              </div>
            </div>
          </section>}

          {section === "event" && (
            <section className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
              {([
                { title: "Pronoia（多 Agent）", note: "统一平台基模与固定多 Agent 流程，作为独立选手参加评测。", mode: "platform", externalMode: "profile", icon: <Sparkles size={17} /> },
                { title: "外部大模型（直接调用）", note: "选择 Qwen、DeepSeek 等连接，使用其自身 API Key 和模型 ID。", mode: "external_api", externalMode: "profile", icon: <MessageSquare size={17} /> },
                { title: "第三方预测服务", note: "接入自建预测服务，返回标准方向、置信度与收益率预测。", mode: "external_api", externalMode: "http", icon: <RadioTower size={17} /> },
                { title: "导入已有预测", note: "对照实际行情评测已有判断；缺失的收益率预测不补造。", mode: "provided", externalMode: "profile", icon: <FileInput size={17} /> },
              ] as const).map((source, index) => (
                <button
                  key={source.title}
                  onClick={() => openCreate("event", { eventAnalysisMode: source.mode, externalModelMode: source.externalMode })}
                  className="group rounded-xl border border-brand/15 bg-card p-4 text-left shadow-card transition hover:-translate-y-0.5 hover:border-brand/35 hover:shadow-pop"
                >
                  <div className="flex items-start justify-between">
                    <span className="grid h-9 w-9 place-items-center rounded-xl border border-brand/20 bg-brand-soft/60 text-brand">{source.icon}</span>
                    <span className="font-mono text-[9px] text-faint">0{index + 1}</span>
                  </div>
                  <h2 className="mt-4 font-serif text-[14px] font-semibold text-ink">{source.title}</h2>
                  <p className="mt-1 text-[10px] leading-[1.55] text-mute">{source.note}</p>
                  <div className="mt-3 flex items-center gap-1 text-[10px] font-medium text-brand">配置事件实验 <ArrowRight size={11} /></div>
                </button>
              ))}
            </section>
          )}

          {section === "quant" && (
          <section className="mt-6 grid gap-3 lg:grid-cols-3">
            {([
              { type: "quant", mode: "return_forecast", label: "收益率预测", description: "输出未来 N 个行情周期的预期收益率数值，评估误差与预测覆盖。" },
              { type: "quant", mode: "trading_strategy", label: "交易策略回测", description: "定义开平仓规则，按冻结行情模拟交易，计算策略净值与风险。" },
              { type: "signal_import", mode: "return_forecast", label: "外部量化服务", description: "接入自有量化模型 API，按其约定的收益预测或仓位输出评测。" },
            ] as const).map((entry, index) => {
              const meta = STRATEGY_META[entry.type];
              return (
                <button
                  key={entry.label}
                  onClick={() => openCreate(entry.type, { quantMode: entry.mode })}
                  className="group relative overflow-hidden rounded-xl border border-edge bg-card p-4 text-left shadow-card transition hover:-translate-y-0.5 hover:border-edgeDark hover:shadow-pop"
                >
                  <div className="flex items-start justify-between">
                    <span className={cls("grid h-9 w-9 place-items-center rounded-xl border", meta.className)}>{meta.icon(17)}</span>
                    <span className="font-mono text-[9px] text-faint">0{index + 1}</span>
                  </div>
                  <div className="mt-4 flex items-end justify-between gap-3">
                    <div>
                      <p className="text-[8.5px] font-semibold tracking-[0.15em] text-faint">{meta.eyebrow}</p>
                      <h2 className="mt-1 font-serif text-[14px] font-semibold text-ink">{entry.label}</h2>
                      <p className="mt-1 max-w-sm text-[10px] leading-[1.5] text-mute">{entry.description}</p>
                    </div>
                  </div>
                  <div className="mt-3 flex items-center gap-1 text-[10px] font-medium text-jade opacity-80 transition group-hover:opacity-100">
                    创建此类实验 <ArrowRight size={11} />
                  </div>
                </button>
              );
            })}
          </section>
          )}

          {section !== "runs" && (
            <section className="mt-5 grid gap-3 lg:grid-cols-[minmax(0,1fr)_320px]">
              <div className="rounded-xl border border-edge bg-card p-5 shadow-card">
                <div className="flex items-center justify-between gap-3">
                  <div>
                    <p className="text-[9px] font-semibold uppercase tracking-[0.18em] text-faint">实验流程</p>
                    <h2 className="mt-1 font-serif text-[15px] font-semibold text-ink">一个版面完成一类实验</h2>
                  </div>
                  <button
                    type="button"
                    onClick={() => setView("backtest-runs")}
                    className="inline-flex items-center gap-1 text-[10.5px] font-semibold text-mute transition hover:text-ink"
                  >
                    前往运行记录 <ArrowRight size={11} />
                  </button>
                </div>
                <div className="mt-4 grid gap-2 sm:grid-cols-5">
                  {(section === "event"
                    ? ["选择事件数据", "选择预测选手", "配置预测窗口", "可选问答测试", "对照实际结果"]
                    : ["选择行情版本", "选择评测类型", "配置预测或策略", "冻结评测设置", "查看对应结果"]
                  ).map((step, index) => (
                    <div key={step} className="relative rounded-lg border border-edge bg-paper px-3 py-3">
                      <span className={cls(
                        "font-mono text-[9px] font-semibold",
                        section === "event" ? "text-brand" : "text-jade",
                      )}>0{index + 1}</span>
                      <p className="mt-1 text-[10.5px] font-medium leading-snug text-ink">{step}</p>
                    </div>
                  ))}
                </div>
              </div>
              <div className={cls(
                "rounded-xl border p-5 shadow-card",
                section === "event" ? "border-brand/20 bg-brand-soft/35" : "border-jade/20 bg-jade-soft/40",
              )}>
                <p className={cls("text-[9px] font-semibold uppercase tracking-[0.18em]", section === "event" ? "text-brand" : "text-jade")}>本页边界</p>
                <h3 className="mt-1.5 font-serif text-[14px] font-semibold text-ink">
                  {section === "event" ? "选手独立，结果分别评测" : "收益率预测与策略回测分开"}
                </h3>
                <p className="mt-2 text-[10px] leading-relaxed text-mute">
                  {section === "event"
                    ? "Pronoia 统一使用平台基模；外部模型使用自己的连接。事件方向、收益率预测与可选问答分别产生指标，并按运行协议比较。"
                    : "收益率预测比较未来数值预测与实际值；交易策略回测评估实际模拟持仓的收益与风险。量化任务不创建问答或事件方向预测。"}
                </p>
              </div>
            </section>
          )}

          {section === "runs" && <>
          {!embedded && <section className="mt-4 grid grid-cols-2 gap-2.5 lg:grid-cols-5">
            <StatCard icon={<LayersIcon />} label="全部实验" value={String(scopedRuns.length)} note="运行记录" />
            <StatCard icon={<Activity size={14} />} label="活跃任务" value={String(overview.running)} note="运行或暂停" accent="brand" />
            <StatCard icon={<CheckCircle2 size={14} />} label="已完成" value={String(overview.done)} note={scopedRuns.length ? `${Math.round((overview.done / scopedRuns.length) * 100)}% 完成率` : "暂无样本"} accent="jade" />
            <StatCard icon={<Gauge size={14} />} label="处理覆盖" value={`${overview.doneEvents}/${overview.totalEvents}`} note="已处理 / 总记录" />
            <StatCard icon={<ShieldCheck size={14} />} label="冻结协议" value={String(overview.protocolCount)} note="唯一 protocol hash" accent="violet" className="col-span-2 lg:col-span-1" />
          </section>}

          {actionErr && (
            <div className="mt-4 flex items-start justify-between gap-3 rounded-xl border border-rise/25 bg-rise/5 px-3.5 py-3 text-[11.5px] text-rise">
              <span>{actionErr}</span>
              <button onClick={() => setActionErr(null)} className="shrink-0 rounded p-0.5 hover:bg-rise/10"><X size={12} /></button>
            </div>
          )}

          <section className={cls("overflow-visible rounded-xl border border-edge bg-card shadow-card", !embedded && "mt-5")}>
            <div className="border-b border-edge px-4 py-3.5 sm:px-5">
              <div className="flex flex-col justify-between gap-3 xl:flex-row xl:items-center">
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="font-serif text-[15px] font-semibold text-ink">{embedded ? "回测指标" : "运行记录"}</h2>
                    <span className="rounded-full bg-edge/55 px-2 py-0.5 font-mono text-[9px] text-mute">{filteredRuns.length} / {scopedRuns.length}</span>
                  </div>
                  <p className="mt-0.5 text-[9.5px] text-faint">每一行对应一次回测；查看结果或管理任务。</p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <div className="relative min-w-[190px] flex-1 sm:flex-none">
                    <Search size={12} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      className="w-full rounded-lg border border-edge bg-paper py-1.5 pl-7 pr-3 text-[10.5px] text-ink outline-none transition placeholder:text-faint focus:border-brand/40"
                      placeholder="搜索名称、数据集或版本…"
                    />
                  </div>
                  <FilterSelect value={statusFilter} onChange={(value) => setStatusFilter(value as StatusFilter)} icon={<Filter size={11} />}>
                    <option value="all">全部状态</option>
                    <option value="pending">待启动</option>
                    <option value="running">运行中</option>
                    <option value="paused">已暂停</option>
                    <option value="done">已完成</option>
                    <option value="failed">失败</option>
                    <option value="cancelled">已取消</option>
                  </FilterSelect>
                  <FilterSelect value={typeFilter} onChange={(value) => setTypeFilter(value as "all" | StrategyType)} icon={<SlidersHorizontal size={11} />}>
                    <option value="all">全部类型</option>
                    <option value="event">事件模型</option>
                    <option value="quant">量化模型</option>
                    <option value="signal_import">外部量化模型</option>
                  </FilterSelect>
                  <div className="flex rounded-lg border border-edge bg-paper p-0.5">
                    <button onClick={() => setViewMode("table")} className={cls("rounded-md p-1.5 transition", viewMode === "table" ? "bg-card text-ink shadow-card" : "text-faint hover:text-ink")} title="表格视图"><List size={12} /></button>
                    <button onClick={() => setViewMode("cards")} className={cls("rounded-md p-1.5 transition", viewMode === "cards" ? "bg-card text-ink shadow-card" : "text-faint hover:text-ink")} title="卡片视图"><LayoutGrid size={12} /></button>
                  </div>
                </div>
              </div>

              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t border-edge/65 pt-3">
                <div className="flex items-center gap-1.5 text-[9.5px] text-faint">
                  <TrendingUp size={11} /> 指标列可独立切换，不改变已冻结结果
                </div>
                <div className="flex flex-wrap items-center gap-1.5">
                  <MetricSelect
                    label="A"
                    id={metricA}
                    definition={metricDefs[metricA]}
                    open={metricPickerFor === "A"}
                    onToggle={() => setMetricPickerFor(metricPickerFor === "A" ? null : "A")}
                  >
                    <MetricPickerPop defs={metricsArr} value={metricA} other={metricB} onPick={(id) => { setMetricA(id); setMetricPickerFor(null); }} onClose={() => setMetricPickerFor(null)} />
                  </MetricSelect>
                  <MetricSelect
                    label="B"
                    id={metricB}
                    definition={metricDefs[metricB]}
                    open={metricPickerFor === "B"}
                    onToggle={() => setMetricPickerFor(metricPickerFor === "B" ? null : "B")}
                  >
                    <MetricPickerPop defs={metricsArr} value={metricB} other={metricA} onPick={(id) => { setMetricB(id); setMetricPickerFor(null); }} onClose={() => setMetricPickerFor(null)} />
                  </MetricSelect>
                </div>
              </div>
            </div>

            {btRunsLoading && scopedRuns.length === 0 ? (
              <EmptyState icon={<Loader2 size={22} className="animate-spin text-brand" />} title="正在载入实验目录" description="读取运行状态、协议与评测结果…" />
            ) : scopedRuns.length === 0 ? (
              <EmptyState
                icon={<FlaskMark />}
                title={modelId ? "这个模型还没有回测记录" : "还没有回测记录"}
                description="从模型列表发起测试后，回测指标会显示在这里。"
                actionLabel="前往事件模型"
                onAction={() => setView("backtest-event")}
              />
            ) : filteredRuns.length === 0 ? (
              <EmptyState
                icon={<Search size={20} />}
                title="没有匹配的运行记录"
                description="调整关键词或筛选条件后再试。"
                actionLabel="清除筛选"
                onAction={() => { setQuery(""); setStatusFilter("all"); setTypeFilter("all"); }}
              />
            ) : viewMode === "table" ? (
              <RunTable
                runs={filteredRuns}
                metricA={{ id: metricA, definition: metricDefs[metricA] }}
                metricB={{ id: metricB, definition: metricDefs[metricB] }}
                getMetric={getMetric}
                getMetricLo={getMetricLo}
                actions={actions}
              />
            ) : (
              <div className="grid gap-3 p-4 md:grid-cols-2 xl:grid-cols-3">
                {filteredRuns.map((run) => (
                  <RunCard
                    key={run.id}
                    run={run}
                    metricA={{ id: metricA, definition: metricDefs[metricA], value: getMetric(run, metricA) }}
                    metricB={{ id: metricB, definition: metricDefs[metricB], value: getMetric(run, metricB) }}
                    actions={actions(run)}
                  />
                ))}
              </div>
            )}
          </section>
          </>}
        </div>
      </div>

      {section !== "runs" && (
        <BacktestCreateModal
          open={modalOpen}
          initialType={createType}
          initialEventAnalysisMode={createEventMode}
          initialExternalModelMode={createExternalMode}
          initialQuantMode={createQuantMode}
          allowedTypes={section === "event" ? ["event"] : ["quant", "signal_import"]}
          onClose={() => setModalOpen(false)}
          onCreate={onCreate}
        />
      )}
    </div>
  );
}

function LayersIcon() {
  return <Database size={14} />;
}

function StatCard({
  icon,
  label,
  value,
  note,
  accent = "ink",
  className,
}: {
  icon: ReactNode;
  label: string;
  value: string;
  note: string;
  accent?: "ink" | "brand" | "jade" | "violet";
  className?: string;
}) {
  const accents = {
    ink: "bg-edge/55 text-mute",
    brand: "bg-brand-soft text-brand",
    jade: "bg-jade-soft text-jade",
    violet: "bg-violet-soft text-violet",
  };
  return (
    <div className={cls("rounded-xl border border-edge bg-card p-3.5 shadow-card", className)}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <p className="text-[9.5px] font-medium text-faint">{label}</p>
          <p className="mt-1 font-mono text-[19px] font-semibold tracking-tight text-ink tabular-nums">{value}</p>
        </div>
        <span className={cls("grid h-7 w-7 place-items-center rounded-lg", accents[accent])}>{icon}</span>
      </div>
      <p className="mt-2 border-t border-edge/65 pt-2 text-[8.5px] text-faint">{note}</p>
    </div>
  );
}

function FilterSelect({
  value,
  onChange,
  icon,
  children,
}: {
  value: string;
  onChange: (value: string) => void;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <label className="relative inline-flex items-center">
      <span className="pointer-events-none absolute left-2.5 text-faint">{icon}</span>
      <select
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="appearance-none rounded-lg border border-edge bg-paper py-1.5 pl-7 pr-7 text-[10.5px] text-mute outline-none transition hover:border-edgeDark focus:border-brand/40"
      >
        {children}
      </select>
      <ChevronDown size={10} className="pointer-events-none absolute right-2 text-faint" />
    </label>
  );
}

function MetricSelect({
  label,
  id,
  definition,
  open,
  onToggle,
  children,
}: {
  label: string;
  id: string;
  definition?: BTMetricDef;
  open: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <div className="relative">
      <button onClick={onToggle} className="inline-flex max-w-[210px] items-center gap-1.5 rounded-lg border border-edge bg-paper px-2.5 py-1.5 text-[10px] text-mute transition hover:border-edgeDark hover:text-ink">
        <span className={cls("rounded px-1 py-px text-[8px] font-bold", label === "A" ? "bg-brand-soft text-brand" : "bg-violet-soft text-violet")}>{label}</span>
        <span className="truncate">{definition?.display_name ?? id}</span>
        <ChevronDown size={10} />
      </button>
      {open && children}
    </div>
  );
}

function MetricPickerPop({
  defs,
  value,
  other,
  onPick,
  onClose,
}: {
  defs: Array<[string, BTMetricDef]>;
  value: string;
  other: string;
  onPick: (id: string) => void;
  onClose: () => void;
}) {
  const [query, setQuery] = useState("");
  useEffect(() => {
    const onDocument = (event: MouseEvent) => {
      const target = event.target as HTMLElement;
      if (!target.closest("[data-metric-picker]")) onClose();
    };
    const timer = window.setTimeout(() => document.addEventListener("mousedown", onDocument), 0);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener("mousedown", onDocument);
    };
  }, [onClose]);

  const filtered = defs.filter(([id, definition]) => {
    const normalized = query.trim().toLowerCase();
    return !normalized || `${id} ${definition.display_name} ${definition.description}`.toLowerCase().includes(normalized);
  });

  return (
    <div data-metric-picker className="absolute right-0 z-30 mt-1.5 w-[320px] overflow-hidden rounded-xl border border-edgeDark bg-card shadow-pop">
      <div className="border-b border-edge p-2.5">
        <div className="relative">
          <Search size={11} className="absolute left-2.5 top-1/2 -translate-y-1/2 text-faint" />
          <input autoFocus value={query} onChange={(event) => setQuery(event.target.value)} className="w-full rounded-lg border border-edge bg-paper py-1.5 pl-7 pr-2 text-[10.5px] outline-none focus:border-brand/40" placeholder="搜索指标…" />
        </div>
      </div>
      <div className="max-h-72 overflow-y-auto p-1.5">
        {filtered.map(([id, definition]) => {
          const selected = id === value;
          const disabled = id === other && !selected;
          return (
            <button
              key={id}
              disabled={disabled}
              onClick={() => onPick(id)}
              className={cls("flex w-full items-start justify-between gap-3 rounded-lg px-2.5 py-2 text-left transition", selected ? "bg-brand-soft/55" : "hover:bg-paper", disabled && "cursor-not-allowed opacity-35")}
            >
              <span className="min-w-0">
                <span className="block truncate text-[10.5px] font-medium text-ink">{definition.display_name || id}</span>
                <span className="mt-0.5 block truncate text-[8.5px] text-faint">{definition.description || id}</span>
              </span>
              <span className="shrink-0 text-[8.5px] text-faint">{disabled ? "另一列" : definition.higher_is_better ? "↑" : "↓"}</span>
            </button>
          );
        })}
        {filtered.length === 0 && <p className="py-6 text-center text-[10px] text-faint">没有匹配指标</p>}
      </div>
    </div>
  );
}

interface MetricColumn {
  id: string;
  definition?: BTMetricDef;
}

interface RunActionProps {
  run: BTRun;
  busy: boolean;
  onStart: () => void;
  onPause: () => void;
  onResume: () => void;
  onCancel: () => void;
  onDelete: () => void;
  onOpen: () => void;
}

function RunTable({
  runs,
  metricA,
  metricB,
  getMetric,
  getMetricLo,
  actions,
}: {
  runs: BTRun[];
  metricA: MetricColumn;
  metricB: MetricColumn;
  getMetric: (run: BTRun, id: string) => number | null | undefined;
  getMetricLo: (run: BTRun, id: string) => number | null | undefined;
  actions: (run: BTRun) => RunActionProps;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1120px] border-collapse text-left">
        <thead>
          <tr className="border-b border-edge bg-paper/80 text-[8.5px] font-semibold uppercase tracking-[0.12em] text-faint">
            <th className="px-5 py-2.5 font-semibold">实验</th>
            <th className="px-3 py-2.5 font-semibold">数据与协议</th>
            <th className="px-3 py-2.5 font-semibold">状态</th>
            <th className="px-3 py-2.5 font-semibold">进度</th>
            <th className="px-3 py-2.5 font-semibold" title={metricA.definition?.description}>{metricA.definition?.display_name ?? metricA.id}</th>
            <th className="px-3 py-2.5 font-semibold" title={metricB.definition?.description}>{metricB.definition?.display_name ?? metricB.id}</th>
            <th className="px-3 py-2.5 font-semibold">更新</th>
            <th className="px-5 py-2.5 text-right font-semibold">操作</th>
          </tr>
        </thead>
        <tbody>
          {runs.map((run) => {
            const total = Math.max(run.total_events || 0, run.done_events || 0);
            const progress = run.status === "done" ? 100 : total > 0 ? Math.min(100, Math.round((run.done_events / total) * 100)) : 0;
            const protocolHash = getProtocolHash(run);
            return (
              <tr key={run.id} className="group border-b border-edge/65 transition last:border-b-0 hover:bg-paper/65">
                <td className="max-w-[310px] px-5 py-3.5">
                  <button onClick={actions(run).onOpen} className="block max-w-full text-left">
                    <div className="flex items-center gap-2">
                      <StrategyBadge type={getRunStrategyType(run)} compact />
                      <span className="truncate text-[12px] font-semibold text-ink transition group-hover:text-brand">{run.name || "未命名实验"}</span>
                    </div>
                    <div className="mt-1.5 flex items-center gap-1.5 font-mono text-[8.5px] text-faint">
                      <span>{run.id.slice(0, 9)}</span><span>·</span><span>{engineLabel(run.runner)}</span>
                      {run.model_version && run.model_version !== "provided-analysis" && <><span>·</span><span className="max-w-[100px] truncate">{run.model_version}</span></>}
                    </div>
                  </button>
                </td>
                <td className="max-w-[190px] px-3 py-3.5">
                  <p className="truncate text-[10.5px] font-medium text-ink">{run.dataset_name || run.dataset_id || "独立数据快照"}</p>
                  <div className="mt-1 flex items-center gap-1.5 text-[8.5px] text-faint">
                    {protocolHash ? <><ShieldCheck size={9} className="text-jade" /><span className="font-mono">{protocolHash.slice(0, 10)}…</span></> : <><CircleDashed size={9} /><span>legacy protocol</span></>}
                  </div>
                </td>
                <td className="px-3 py-3.5"><StatusBadge status={run.status} run={run} /></td>
                <td className="px-3 py-3.5">
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 w-20 overflow-hidden rounded-full bg-edge">
                      <div className={cls("h-full rounded-full transition-all", run.status === "done" ? "bg-jade" : run.status === "failed" ? "bg-rise" : run.status === "running" ? "bg-brand" : "bg-faint/60")} style={{ width: `${progress}%` }} />
                    </div>
                    <span className="font-mono text-[9px] text-mute tabular-nums">{progress}%</span>
                  </div>
                  <p className="mt-1 font-mono text-[8.5px] text-faint">{run.done_events}/{total}</p>
                </td>
                <td className="px-3 py-3.5"><MetricValue id={metricA.id} value={getMetric(run, metricA.id)} lo={getMetricLo(run, metricA.id)} /></td>
                <td className="px-3 py-3.5"><MetricValue id={metricB.id} value={getMetric(run, metricB.id)} lo={getMetricLo(run, metricB.id)} /></td>
                <td className="px-3 py-3.5">
                  <p className="text-[10px] text-mute">{relTime(run.updated_at)}</p>
                  <p className="mt-0.5 font-mono text-[8px] text-faint">{run.updated_at?.slice(5, 16).replace("T", " ")}</p>
                </td>
                <td className="px-5 py-3.5"><RunActions {...actions(run)} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RunCard({
  run,
  metricA,
  metricB,
  actions,
}: {
  run: BTRun;
  metricA: MetricColumn & { value?: number | null };
  metricB: MetricColumn & { value?: number | null };
  actions: RunActionProps;
}) {
  const total = Math.max(run.total_events || 0, run.done_events || 0);
  const progress = run.status === "done" ? 100 : total > 0 ? Math.min(100, Math.round((run.done_events / total) * 100)) : 0;
  const protocolHash = getProtocolHash(run);
  return (
    <article className="group rounded-xl border border-edge bg-card p-4 transition hover:border-edgeDark hover:shadow-pop">
      <div className="flex items-start justify-between gap-3">
        <StrategyBadge type={getRunStrategyType(run)} compact />
        <StatusBadge status={run.status} run={run} />
      </div>
      <button onClick={actions.onOpen} className="mt-3 block w-full text-left">
        <h3 className="truncate font-serif text-[13px] font-semibold text-ink transition group-hover:text-brand">{run.name || "未命名实验"}</h3>
        <p className="mt-1 truncate font-mono text-[8.5px] text-faint">{run.id} · {engineLabel(run.runner)}</p>
      </button>
      <div className="mt-4">
        <div className="mb-1.5 flex items-center justify-between text-[8.5px] text-faint">
          <span>{run.done_events} / {total} 条</span><span className="font-mono">{progress}%</span>
        </div>
        <div className="h-1.5 overflow-hidden rounded-full bg-edge"><div className={cls("h-full rounded-full", run.status === "done" ? "bg-jade" : run.status === "running" ? "bg-brand" : "bg-faint/60")} style={{ width: `${progress}%` }} /></div>
      </div>
      <div className="mt-4 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-edge bg-edge">
        {[metricA, metricB].map((metric) => (
          <div key={metric.id} className="bg-paper px-3 py-2.5">
            <p className="truncate text-[8px] text-faint">{metric.definition?.display_name ?? metric.id}</p>
            <div className="mt-1"><MetricValue id={metric.id} value={metric.value} /></div>
          </div>
        ))}
      </div>
      <div className="mt-3 flex items-center justify-between gap-2 border-t border-edge/70 pt-3">
        <div className="min-w-0">
          <p className="truncate text-[9px] text-mute">{run.dataset_name || run.dataset_id || "独立数据快照"}</p>
          <p className="mt-0.5 flex items-center gap-1 font-mono text-[8px] text-faint">{protocolHash ? <ShieldCheck size={8} className="text-jade" /> : <CircleDashed size={8} />}{protocolHash ? `${protocolHash.slice(0, 9)}…` : getRunVisibility(run)}</p>
        </div>
        <RunActions {...actions} compact />
      </div>
    </article>
  );
}

function engineLabel(runner: string) {
  if (runner === "provided_analysis") return "预计算决策";
  if (runner === "team_full") return "Pronoia · 多 Agent";
  if (runner === "raw_model") return "外部大模型 · 直接调用";
  if (runner === "return_forecast") return "量化收益率预测";
  if (runner === "team_prompt") return "单 Agent · 严格历史";
  if (runner === "baseline") return "基线引擎";
  return runner;
}

function displayHorizon(horizon: string) {
  const match = /^t\+?(\d+)$/i.exec(horizon.trim());
  return match ? `T+${match[1]}` : horizon;
}

function MetricValue({ id, value, lo }: { id: string; value?: number | null; lo?: number | null }) {
  if (value == null || Number.isNaN(value)) return <span className="font-mono text-[10px] text-faint">—</span>;
  const percentLike = /(acc|accuracy|rate|coverage|return|car|drawdown|precision|recall|hit)/i.test(id) && Math.abs(value) <= 2;
  const display = percentLike ? `${(value * 100).toFixed(1)}%` : Math.abs(value) >= 1000 ? value.toLocaleString(undefined, { maximumFractionDigits: 1 }) : value.toFixed(3);
  return (
    <div>
      <p className="font-mono text-[11.5px] font-semibold text-ink tabular-nums">{display}</p>
      {lo != null && percentLike && <p className="mt-0.5 font-mono text-[7.5px] text-faint">95% lo {(lo * 100).toFixed(1)}%</p>}
    </div>
  );
}

function RunActions(props: RunActionProps & { compact?: boolean }) {
  const { run, busy, onStart, onPause, onResume, onCancel, onDelete, onOpen, compact = false } = props;
  const deleteBlocked = ["running", "paused", "interrupted", "starting", "queued", "cancelling"].includes(run.status);
  return (
    <div className="flex items-center justify-end gap-1">
      {(run.status === "pending" || run.status === "failed" || run.status === "cancelled") && (
        <button onClick={onStart} disabled={busy} className="inline-flex items-center gap-1 rounded-md border border-jade/25 bg-jade-soft px-2 py-1 text-[9.5px] font-medium text-jade transition hover:bg-jade hover:text-card disabled:opacity-50" title="启动">
          {busy ? <Loader2 size={10} className="animate-spin" /> : <Play size={10} />}{!compact && "启动"}
        </button>
      )}
      {run.status === "running" && (
        <>
          <button onClick={onPause} disabled={busy} className="rounded-md border border-amber-200 bg-amber-50 p-1.5 text-amber-700 transition hover:bg-amber-100 disabled:opacity-50" title="暂停">{busy ? <Loader2 size={10} className="animate-spin" /> : <Pause size={10} />}</button>
          <button onClick={onCancel} disabled={busy} className="rounded-md border border-rise/15 bg-rise/5 p-1.5 text-rise transition hover:bg-rise/10 disabled:opacity-50" title="取消"><Square size={10} /></button>
        </>
      )}
      {run.status === "paused" && (
        <>
          <button onClick={onResume} disabled={busy} className="rounded-md border border-jade/25 bg-jade-soft p-1.5 text-jade transition hover:bg-jade hover:text-card disabled:opacity-50" title="继续">{busy ? <Loader2 size={10} className="animate-spin" /> : <Play size={10} />}</button>
          <button onClick={onCancel} disabled={busy} className="rounded-md border border-rise/15 bg-rise/5 p-1.5 text-rise transition hover:bg-rise/10 disabled:opacity-50" title="取消"><Square size={10} /></button>
        </>
      )}
      <button
        onClick={onOpen}
        className={cls(
          "inline-flex items-center gap-1 rounded-md border px-2 py-1 text-[9.5px] font-medium transition",
          run.status === "done"
            ? "border-brand/20 bg-brand-soft/45 text-brand hover:border-brand/35 hover:bg-brand-soft"
            : "border-edge bg-card text-mute hover:border-edgeDark hover:text-ink",
        )}
        title={run.status === "done" ? "查看收益曲线、回撤曲线与真实 K 线" : "查看实验详情"}
      >
        {run.status === "done" && <CandlestickChart size={10} />}
        {run.status === "done" ? "收益与 K 线" : "详情"}
      </button>
      <button onClick={onDelete} disabled={busy || deleteBlocked} className="rounded-md p-1.5 text-faint transition hover:bg-rise/5 hover:text-rise disabled:opacity-50" title={deleteBlocked ? "请先取消并等待任务停止" : "删除"}><Trash2 size={10} /></button>
    </div>
  );
}

function EmptyState({
  icon,
  title,
  description,
  actionLabel,
  onAction,
}: {
  icon: ReactNode;
  title: string;
  description: string;
  actionLabel?: string;
  onAction?: () => void;
}) {
  return (
    <div className="flex min-h-[290px] flex-col items-center justify-center px-6 py-12 text-center">
      <span className="grid h-11 w-11 place-items-center rounded-xl border border-edge bg-paper text-mute">{icon}</span>
      <h3 className="mt-4 font-serif text-[15px] font-semibold text-ink">{title}</h3>
      <p className="mt-1.5 max-w-md text-[10.5px] leading-relaxed text-mute">{description}</p>
      {actionLabel && onAction && (
        <button onClick={onAction} className="mt-4 inline-flex items-center gap-1.5 rounded-lg bg-ink px-3.5 py-2 text-[10.5px] font-semibold text-card transition hover:bg-ink/90">
          <Plus size={11} /> {actionLabel}
        </button>
      )}
    </div>
  );
}
