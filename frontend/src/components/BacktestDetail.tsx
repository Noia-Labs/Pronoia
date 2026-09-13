import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowLeft,
  CandlestickChart,
  Clock3,
  Database,
  FileDown,
  Filter,
  Fingerprint,
  Layers3,
  ListChecks,
  Loader2,
  LockKeyhole,
  MessageSquareText,
  Pause,
  Play,
  RefreshCw,
  Square,
  TableProperties,
  XCircle,
} from "lucide-react";
import { api, streamBacktest, StreamAbortedError } from "../api";
import { useStore } from "../store";
import { cls } from "../utils";
import type {
  BTRun,
  BTMetricsV2,
  BTPredictionItem,
  BTPredictionDetail,
  BTPerformanceResponse,
  BTStatus,
  BTSSEEvent,
  BTEventCatalogItem,
  BTEventStatus,
  BTRunActivity,
} from "../types";
import { CaseDetailPanel, type DetailTab } from "./BacktestDetailPanels";
import { FilterSelect, StatusBadge } from "./BacktestDetailShared";
import { CatalogRow, MetricsGrid, predictionOutcome, ProgressCard, SSEEventRow } from "./BacktestDetailWidgets";
import QuantReturnForecastPanel from "./backtest/QuantReturnForecastPanel";
import EventReturnForecastPanel from "./backtest/EventReturnForecastPanel";
import QuantFinancialOverview from "./backtest/QuantFinancialOverview";
import BacktestPerformanceDashboard from "./BacktestPerformanceDashboard";
import BacktestQuestionResults, { BacktestQuestionSummary, questionTaskStatus, useBacktestQuestionResults } from "./backtest/BacktestQuestionResults";
import { LabStatus } from "../pages/model-lab/ModelLabResultsPanel";

type ResultTab = "financial_overview" | "return_forecast" | "performance" | "event_audit" | "portfolio_audit" | "qa";

const TEAM_FULL_STAGES = [
  ["planning", "规划"],
  ["expert_research", "专家研究"],
  ["synthesis", "综合"],
  ["review", "复核"],
  ["hypothesis_extraction", "假设提取"],
] as const;

function formatElapsedSeconds(raw: number): string {
  const seconds = Math.max(0, Math.floor(raw));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours > 0) return `${hours}小时 ${minutes}分 ${rest}秒`;
  if (minutes > 0) return `${minutes}分 ${rest}秒`;
  return `${rest}秒`;
}

function activityElapsed(
  item: BTRunActivity["active_events"][number],
  nowMs: number,
): number {
  if (item.started_at) {
    const start = Date.parse(item.started_at);
    if (Number.isFinite(start)) return Math.max(0, (nowMs - start) / 1000);
  }
  return Number(item.elapsed_seconds ?? 0);
}

function TeamFullActivityCard({
  activity,
  startedAt,
  nowMs,
}: {
  activity: BTRunActivity | null;
  startedAt?: string | null;
  nowMs: number;
}) {
  const active = activity?.active_events ?? [];
  const lead = active[0];
  const runStartedMs = startedAt ? Date.parse(startedAt) : Number.NaN;
  const runElapsed = Number.isFinite(runStartedMs) ? Math.max(0, (nowMs - runStartedMs) / 1000) : 0;
  const currentIndex = Math.max(0, Number(lead?.stage_index ?? 0));

  return (
    <div className="border-b border-edge bg-brand-soft/20 px-4 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="text-[11.5px] font-semibold text-ink">多专家单事件阶段</div>
          <div className="mt-0.5 text-[9.5px] text-mute">阶段变化只表示模型仍在工作，不会计入“已完成事件”。</div>
        </div>
        <span className="inline-flex items-center gap-1.5 rounded-full border border-brand/15 bg-card px-2.5 py-1 font-mono text-[10px] text-brand">
          <Clock3 size={11} />
          已运行 {formatElapsedSeconds(runElapsed)}
        </span>
      </div>
      <div className="mt-3 grid grid-cols-5 overflow-hidden rounded-lg border border-edge bg-card">
        {TEAM_FULL_STAGES.map(([key, label], index) => {
          const stageNumber = index + 1;
          const activeStage = lead?.stage === key;
          const passed = currentIndex > stageNumber;
          return (
            <div
              key={key}
              className={cls(
                "border-r border-edge px-1.5 py-2 text-center text-[9px] last:border-r-0",
                activeStage ? "bg-brand text-white" : passed ? "bg-jade-soft/60 text-jade" : "text-faint",
              )}
            >
              <span className="font-mono">{String(stageNumber).padStart(2, "0")}</span>
              <span className="ml-1 hidden sm:inline">{label}</span>
            </div>
          );
        })}
      </div>
      <div className="mt-2 space-y-1.5">
        {active.length === 0 ? (
          <div className="rounded-lg border border-dashed border-edge bg-card/70 px-3 py-2 text-[10.5px] text-mute">
            正在领取首个事件或等待下一次阶段心跳…
          </div>
        ) : active.map((item, index) => {
          const identity = item.event_id
            ? [item.market, item.symbol, `ev=${item.event_id.slice(0, 8)}`].filter(Boolean).join(" · ")
            : `并发任务 ${index + 1}`;
          return (
            <div key={item.event_id ?? `slot-${index}`} className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-lg border border-edge bg-card px-3 py-2 text-[10.5px]">
              <span className="font-medium text-ink">{item.stage_label || "处理中"}</span>
              <span className="font-mono text-faint">{identity}</span>
              <span className="min-w-0 flex-1 truncate text-mute">{item.detail || "阶段执行中"}</span>
              <span className="shrink-0 font-mono text-brand">{formatElapsedSeconds(activityElapsed(item, nowMs))}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

type RunFailureHelp = {
  title: string;
  summary: string;
  action: string;
};

function describeRunFailure(rawError: string): RunFailureHelp {
  const normalized = rawError.toLowerCase();
  const hasStatus = (status: number) => new RegExp(`(?:^|\\D)${status}(?:\\D|$)`).test(normalized);

  if (
    /prompt must contain[\s\S]*\bjson\b/.test(normalized) &&
    /(response[_\s-]*format|json[_\s-]*object)/.test(normalized)
  ) {
    return {
      title: "本地模型的 JSON 输出约束未匹配",
      summary: "模型接口要求提示词明确声明 JSON；本次运行使用了旧版请求格式，因此在处理事件前被拒绝。",
      action: "当前版本已兼容这类接口。请重启 Pronoia 服务，然后点击“重新运行”；原事件集和实验配置可以继续使用。",
    };
  }

  if (
    /(response[_\s-]*format|json[_\s-]*object)/.test(normalized) &&
    /(not supported|unsupported|does not support|unknown|unrecognized|not allowed|不支持)/.test(normalized)
  ) {
    return {
      title: "当前模型不支持结构化输出参数",
      summary: "所选模型或本地兼容接口不接受 response_format=json_object，模型尚未开始分析事件。",
      action: "请切换到支持 JSON 结构化输出的模型，或在模型服务中开启兼容模式，再重新运行。",
    };
  }

  if (
    hasStatus(401) ||
    /(unauthorized|authentication[_\s-]*(?:failed|error)|invalid[_\s-]*(?:api[_\s-]*)?key|incorrect[_\s-]*(?:api[_\s-]*)?key)/.test(normalized)
  ) {
    return {
      title: "模型 API 身份验证失败",
      summary: "模型服务拒绝了当前凭据，事件数据尚未进入模型推理。",
      action: "请在模型列表中打开“修改配置”，填写与 API 地址匹配的 API Key，然后发起新测试。使用平台默认模型时，请检查平台统一基模连接。",
    };
  }

  if (
    hasStatus(403) ||
    /(forbidden|permission denied|access denied|insufficient permissions?)/.test(normalized)
  ) {
    return {
      title: "模型访问权限不足",
      summary: "模型服务已收到请求，但当前凭据无权访问该模型或接口。",
      action: "请确认 API Key 的模型权限、服务端访问控制和模型名称，修正后重新运行。",
    };
  }

  if (
    /(model[_\s-]*not[_\s-]*found|model[\s\S]{0,80}(?:not found|does not exist)|unknown[_\s-]*model|invalid[_\s-]*model|模型不存在|未找到模型)/.test(normalized)
  ) {
    return {
      title: "没有找到配置的模型",
      summary: "模型服务可以访问，但保存的模型 ID 与服务端提供的模型不一致。",
      action: "请在模型列表的“修改配置”中填写服务端提供的准确模型 ID（包括大小写和版本后缀），保存后发起新测试。",
    };
  }

  if (
    /(connection refused|connecterror|connection error|failed to connect|all connection attempts failed|connection reset|network is unreachable|name or service not known|nodename nor servname|timed?\s*out|timeout)/.test(normalized)
  ) {
    return {
      title: "无法连接模型服务",
      summary: "Pronoia 未能连接到配置的模型 API，本次事件分析尚未开始或已因超时中断。",
      action: "请确认本地模型服务正在运行，并检查 API 基础地址、端口及防火墙；浏览器与 Pronoia 不在同一台电脑时不要使用错误的 localhost 地址。",
    };
  }

  if (hasStatus(429) || /(rate limit|too many requests|quota exceeded)/.test(normalized)) {
    return {
      title: "模型服务请求过于频繁",
      summary: "模型服务触发了并发、速率或额度限制，部分事件可能尚未处理。",
      action: "请稍后重试，或降低回测并发数；若持续出现，请检查服务额度和限流配置。",
    };
  }

  return {
    title: "回测运行失败",
    summary: "运行过程中出现了未能自动归类的错误，已停止继续处理事件。",
    action: "请展开技术详情核对原因；修正模型、数据或执行配置后重新运行。",
  };
}

/* ===================================== 主组件 ===================================== */

function runIsArenaSafe(value: BTRun | null): boolean {
  if (!value) return false;
  const config = (value.config ?? {}) as Record<string, unknown>;
  const visibility = String(value.visibility ?? config.visibility ?? "private");
  return visibility === "arena_safe" || visibility === "arena-safe";
}

export function runStrategyFamily(value: BTRun | null): "event" | "quant" | "api" {
  if (!value) return "event";
  const config = (value.config ?? {}) as Record<string, unknown>;
  const spec = (value.strategy_spec ?? config.strategy_spec ?? {}) as Record<string, unknown>;
  const raw = String(spec.type ?? value.strategy_type ?? config.strategy_type ?? "").toLowerCase();
  // A third-party event service shares the HTTP runner with quant APIs. Its
  // explicit event contract determines the result tabs and independent QA.
  if (raw === "event") return "event";
  if (raw === "quant") return "quant";
  if (["api", "external_http", "signal_import", "signal"].includes(raw) || value.runner === "external_http") return "api";
  return "event";
}

function ResultNavigation({
  strategyType,
  active,
  onChange,
  eventNote,
  tradeCount,
  hasQuestionTest,
  predictionOnly,
  hasForecast,
  arenaSafe,
}: {
  strategyType: string;
  active: ResultTab;
  onChange: (tab: ResultTab) => void;
  eventNote?: string | null;
  tradeCount?: number | null;
  hasQuestionTest?: boolean;
  predictionOnly?: boolean;
  hasForecast?: boolean;
  arenaSafe?: boolean;
}) {
  const quantTrading = strategyType !== "event" && !predictionOnly;
  const tabs: Array<{ id: ResultTab; label: string; note: string; icon: React.ReactNode }> = quantTrading
    ? [
      { id: "financial_overview", label: "金融概览", note: arenaSafe ? "聚合收益 · 风险 · 基准 · 成本" : "收益 · 风险 · 基准 · 敞口 · 成本", icon: <TableProperties size={14} /> },
      ...(hasForecast ? [{ id: "return_forecast" as ResultTab, label: "收益率预测", note: "误差 · 方向 · IC · 经济表现", icon: <TableProperties size={14} /> }] : []),
      { id: "performance", label: "图表与行情", note: arenaSafe ? "聚合净值 · 回撤" : tradeCount != null ? `净值 · 回撤 · K 线 · ${Math.round(Number(tradeCount))} 次权重变更` : "净值 · 回撤 · 真实行情", icon: <CandlestickChart size={14} /> },
      ...(!arenaSafe ? [{ id: "portfolio_audit" as ResultTab, label: "规则与交易", note: "信号、仓位与成交流水", icon: <ListChecks size={14} /> }] : []),
    ]
    : strategyType !== "event"
    ? [
      { id: "return_forecast", label: "收益率预测", note: "误差 · 方向 · IC · 技能", icon: <TableProperties size={14} /> },
      { id: "performance", label: "标的行情", note: "标的收益 · 标的回撤 · 真实 K 线", icon: <CandlestickChart size={14} /> },
    ]
    : [
      { id: "return_forecast", label: "收益率预测", note: "误差 · 方向 · IC · 技能", icon: <TableProperties size={14} /> },
      { id: "performance", label: "回测收益与 K 线", note: tradeCount != null ? `净值 · 回撤 · K 线 · ${Math.round(Number(tradeCount))} 笔交易` : "净值 · 回撤 · 真实行情", icon: <CandlestickChart size={14} /> },
      { id: "event_audit", label: "事件判断审计", note: eventNote || "正确率与逐事件", icon: <ListChecks size={14} /> },
    ];
  if (hasQuestionTest) tabs.push({ id: "qa", label: "问答测试", note: "完整回答 · 八维评分 · 逐题对照", icon: <MessageSquareText size={14} /> });
  return (
    <nav className="flex flex-wrap items-center gap-1 rounded-xl border border-edge bg-card p-1.5 shadow-card" aria-label="回测结果视图">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          onClick={() => onChange(tab.id)}
          className={cls(
            "flex min-w-[180px] flex-1 items-center gap-2.5 rounded-lg px-3.5 py-2.5 text-left transition",
            active === tab.id ? "bg-ink text-card shadow-card" : "text-mute hover:bg-edge/35 hover:text-ink",
          )}
        >
          <span className={cls("grid h-7 w-7 shrink-0 place-items-center rounded-lg", active === tab.id ? "bg-card/10" : "bg-edge/55")}>{tab.icon}</span>
          <span>
            <span className="block text-[11.5px] font-semibold">{tab.label}</span>
            <span className={cls("mt-0.5 block text-[9px]", active === tab.id ? "text-card/55" : "text-faint")}>{tab.note}</span>
          </span>
        </button>
      ))}
    </nav>
  );
}

function dateOnly(value: unknown): string | null {
  const text = String(value ?? "").trim();
  const separated = /^(\d{4})[-/](\d{1,2})[-/](\d{1,2})/.exec(text);
  if (separated) return `${separated[1]}-${separated[2].padStart(2, "0")}-${separated[3].padStart(2, "0")}`;
  const compact = /^(\d{4})(\d{2})(\d{2})/.exec(text);
  return compact ? `${compact[1]}-${compact[2]}-${compact[3]}` : null;
}

function eventInExecutionWindow(item: BTEventCatalogItem, start: unknown, end: unknown): boolean {
  const startDate = dateOnly(start);
  const endDate = dateOnly(end);
  if (!startDate && !endDate) return true;
  const eventDate = dateOnly(item.available_time ?? item.event_time);
  if (!eventDate) return false;
  if (startDate && eventDate < startDate) return false;
  if (endDate && eventDate > endDate) return false;
  return true;
}

function scalar(value: unknown): string {
  if (value == null || value === "") return "—";
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: 4 }) : "—";
  return String(value);
}

function auditNumber(value: unknown): number | null {
  const numeric = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(numeric) ? numeric : null;
}

function performanceSamplingSeries(performance: BTPerformanceResponse | null, key: string): Record<string, unknown> {
  const sampling = performance?.response_sampling;
  const series = sampling && typeof sampling === "object" && sampling.series && typeof sampling.series === "object"
    ? sampling.series
    : {};
  const value = series[key];
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function auditCount(value: unknown): string {
  const numeric = auditNumber(value);
  return numeric == null ? "—" : Math.max(0, Math.round(numeric)).toLocaleString("zh-CN");
}

function auditPct(value: unknown): string {
  const numeric = auditNumber(value);
  return numeric == null ? "—" : `${(numeric * 100).toFixed(2)}%`;
}

function auditCurrency(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value !== "string") continue;
    const code = value.trim().toUpperCase();
    if (!/^[A-Z]{3}$/.test(code) || code === "UNSPECIFIED") continue;
    try {
      new Intl.NumberFormat("zh-CN", { style: "currency", currency: code }).format(0);
      return code;
    } catch {
      // Keep looking: a protocol fallback may contain a valid currency code.
    }
  }
  return null;
}

function auditMoney(value: unknown, currency: string | null = null): string {
  const numeric = auditNumber(value);
  if (numeric == null) return "—";
  if (!currency) return numeric.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  try {
    return new Intl.NumberFormat("zh-CN", { style: "currency", currency, maximumFractionDigits: 2 }).format(numeric);
  } catch {
    return `${numeric.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${currency}`;
  }
}

function auditTime(value: unknown): string {
  if (value == null || value === "") return "—";
  return String(value).slice(0, 19).replace("T", " ");
}

function saveAuditBlob(blob: Blob, filename: string) {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = href;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 0);
}

function ruleConditionText(raw: unknown): string {
  if (!raw || typeof raw !== "object") return "条件格式不可识别";
  const condition = raw as Record<string, unknown>;
  const fields: Record<string, string> = {
    price: "价格",
    moving_average: "均线关系",
    breakout: "区间突破",
    return: "区间收益率",
    volume: "相对成交量",
    volume_ratio: "成交量 / 均量",
    volatility: "波动率",
  };
  const operators: Record<string, string> = {
    above: "高于",
    below: "低于",
    crosses_above: "向上穿越",
    crosses_below: "向下穿越",
  };
  const isPercent = condition.threshold_unit === "percent";
  const unit = isPercent ? "%" : condition.threshold_unit === "multiple" ? " 倍" : "";
  const threshold = isPercent
    ? scalar(condition.threshold_display ?? (typeof condition.threshold === "number" ? condition.threshold * 100 : condition.threshold))
    : scalar(condition.threshold);
  const count = Number(condition.consecutive_count ?? 1);
  const confirmation = count > 1 ? ` · 连续 ${count} 次确认` : "";
  return `${fields[String(condition.field)] ?? scalar(condition.field)} · ${operators[String(condition.operator)] ?? scalar(condition.operator)} · 回看 ${scalar(condition.lookback)} bar · 阈值 ${threshold}${unit}${confirmation}`;
}

function AuditPager({ summary, page, pageCount, onChange }: { summary: string; page: number; pageCount: number; onChange: (page: number) => void }) {
  return (
    <div className="flex shrink-0 items-center gap-1.5">
      <span className="font-mono text-[9.5px] text-faint">{summary}</span>
      {pageCount > 1 && (
        <>
          <button type="button" aria-label="上一页" disabled={page <= 0} onClick={() => onChange(Math.max(0, page - 1))} className="rounded border border-edge bg-paper px-2 py-1 text-[9px] text-mute disabled:opacity-35">上一页</button>
          <span className="font-mono text-[9px] text-faint">{page + 1}/{pageCount}</span>
          <button type="button" aria-label="下一页" disabled={page >= pageCount - 1} onClick={() => onChange(Math.min(pageCount - 1, page + 1))} className="rounded border border-edge bg-paper px-2 py-1 text-[9px] text-mute disabled:opacity-35">下一页</button>
        </>
      )}
    </div>
  );
}

function PortfolioAudit({ run, performance, arenaSafe }: { run: BTRun; performance: BTPerformanceResponse | null; arenaSafe: boolean }) {
  const config = (run.config ?? {}) as Record<string, unknown>;
  const runSpec = (run.strategy_spec ?? config.strategy_spec ?? {}) as Record<string, unknown>;
  const responseSpec = performance?.strategy && typeof performance.strategy === "object" ? performance.strategy : {};
  const spec = { ...runSpec, ...responseSpec };
  const parameters = spec.parameters && typeof spec.parameters === "object" ? spec.parameters as Record<string, unknown> : {};
  const entry = parameters.entry && typeof parameters.entry === "object" ? parameters.entry as Record<string, unknown> : {};
  const exit = parameters.exit && typeof parameters.exit === "object" ? parameters.exit as Record<string, unknown> : {};
  const entryConditions = Array.isArray(entry.conditions) ? entry.conditions : [];
  const exitConditions = Array.isArray(exit.conditions) ? exit.conditions : [];
  const positions = performance?.positions ?? performance?.holdings ?? [];
  const trades = performance?.trades ?? [];
  const signals = performance?.signals ?? [];
  const positionSampling = performanceSamplingSeries(performance, "positions");
  const signalSampling = performanceSamplingSeries(performance, "signals");
  const tradeSampling = performanceSamplingSeries(performance, "trades");
  const positionTotal = auditNumber(positionSampling.total_count) ?? positions.length;
  const signalTotal = auditNumber(signalSampling.total_count) ?? signals.length;
  const tradeTotal = auditNumber(tradeSampling.total_count) ?? auditNumber(performance?.summary?.trade_count) ?? trades.length;
  const auditResponseSampled = positionSampling.sampled === true || signalSampling.sampled === true || tradeSampling.sampled === true;
  const unavailable = !performance || performance.status === "unavailable";
  const dataset = performance?.dataset && typeof performance.dataset === "object" ? performance.dataset : {};
  const datasetSymbol = String(dataset.symbol ?? "");
  const performanceRecord = (performance ?? {}) as unknown as Record<string, unknown>;
  const analysisRecord = performance?.financial_analysis && typeof performance.financial_analysis === "object"
    ? performance.financial_analysis as Record<string, unknown>
    : {};
  const analysisUnits = analysisRecord.units && typeof analysisRecord.units === "object"
    ? analysisRecord.units as Record<string, unknown>
    : {};
  const responseUnits = performanceRecord.units && typeof performanceRecord.units === "object"
    ? performanceRecord.units as Record<string, unknown>
    : {};
  const responseProtocol = performance?.effective_protocol && typeof performance.effective_protocol === "object"
    ? performance.effective_protocol as Record<string, unknown>
    : {};
  const responseApplied = responseProtocol.applied && typeof responseProtocol.applied === "object"
    ? responseProtocol.applied as Record<string, unknown>
    : {};
  const runExecution = run.execution_spec && typeof run.execution_spec === "object"
    ? run.execution_spec as Record<string, unknown>
    : {};
  const runApplied = runExecution.applied && typeof runExecution.applied === "object"
    ? runExecution.applied as Record<string, unknown>
    : runExecution;
  const currency = auditCurrency(
    analysisUnits.money,
    responseApplied.currency,
    runApplied.currency,
    responseUnits.money,
    performance?.currency,
  );
  const [positionPage, setPositionPage] = useState(0);
  const [tradePage, setTradePage] = useState(0);
  const auditPageSize = 100;
  // Keep the audit tab responsive without discarding the server's representative
  // sample: mount one page at a time and retain access to every returned row.
  const orderedPositions = useMemo(() => [...positions].reverse(), [positions]);
  const positionPageCount = Math.max(1, Math.ceil(orderedPositions.length / auditPageSize));
  const tradePageCount = Math.max(1, Math.ceil(trades.length / auditPageSize));
  const safePositionPage = Math.min(positionPage, positionPageCount - 1);
  const safeTradePage = Math.min(tradePage, tradePageCount - 1);
  const positionRows = orderedPositions.slice(safePositionPage * auditPageSize, (safePositionPage + 1) * auditPageSize);
  const tradeRows = trades.slice(safeTradePage * auditPageSize, (safeTradePage + 1) * auditPageSize);
  const positionRangeStart = positionRows.length ? safePositionPage * auditPageSize + 1 : 0;
  const positionRangeEnd = safePositionPage * auditPageSize + positionRows.length;
  const tradeRangeStart = tradeRows.length ? safeTradePage * auditPageSize + 1 : 0;
  const tradeRangeEnd = safeTradePage * auditPageSize + tradeRows.length;
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const publicParameters = Object.entries(parameters).filter(([, value]) => value == null || ["string", "number", "boolean"].includes(typeof value));

  const downloadTrades = async () => {
    if (exporting || !trades.length) return;
    setExporting(true);
    setExportError(null);
    try {
      const blob = await api.btDownloadTradesCsv(run.id);
      const safeName = (run.name || run.id).replace(/[^\p{L}\p{N}._-]+/gu, "-").slice(0, 80);
      saveAuditBlob(blob, `${safeName}-trades.csv`);
    } catch (error) {
      setExportError(error instanceof Error ? error.message : String(error));
    } finally {
      setExporting(false);
    }
  };

  if (arenaSafe) {
    return (
      <section className="rounded-card border border-jade/20 bg-jade-soft/30 px-5 py-5 shadow-card">
        <div className="flex items-start gap-3"><LockKeyhole size={16} className="mt-0.5 shrink-0 text-jade" /><div><h3 className="font-serif text-[14px] font-semibold text-ink">策略隐私保护已启用</h3><p className="mt-1 text-[11px] leading-relaxed text-mute">规则、信号、逐笔成交和持仓已隐藏；Arena-safe 仅返回聚合投资表现。</p></div></div>
      </section>
    );
  }

  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-card border border-edge bg-card px-4 py-3 shadow-card">
        <div>
          <div className="text-[9px] font-semibold uppercase tracking-[0.16em] text-faint">Portfolio audit export</div>
          <h3 className="mt-0.5 font-serif text-[14px] font-semibold text-ink">规则、持仓与成交流水</h3>
          <p className="mt-1 text-[9.5px] text-mute">CSV 导出的是服务端保存的完整成交审计字段，不是当前表格的截断视图。{auditResponseSampled ? " 页面表格使用冻结模拟记录的有界抽样以避免大回测卡顿。" : ""}</p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2">
          {auditNumber(performance?.summary?.total_cost) != null && (
            <span className="rounded-lg border border-edge bg-paper px-2.5 py-1.5 text-[9.5px] text-mute">累计成本 <strong className="ml-1 font-mono font-medium text-ink">{auditMoney(performance?.summary?.total_cost, currency)}</strong></span>
          )}
          <button type="button" onClick={() => void downloadTrades()} disabled={exporting || !trades.length} className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-3.5 py-2 text-[10.5px] font-semibold text-card transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40">
            {exporting ? <Loader2 size={12} className="animate-spin" /> : <FileDown size={12} />} 导出全部交易 CSV
          </button>
        </div>
        {exportError && <p className="w-full text-[9.5px] text-rise">CSV 导出失败：{exportError}</p>}
      </div>

      <div className="grid gap-4 xl:grid-cols-[minmax(0,.8fr)_minmax(0,1.2fr)]">
        <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
          <div className="border-b border-edge px-4 py-3"><div className="text-[9px] font-semibold uppercase tracking-[0.16em] text-faint">Strategy contract</div><h3 className="mt-0.5 font-serif text-[14px] font-semibold text-ink">规则说明</h3></div>
          <div className="space-y-3 p-4">
            <div className="grid grid-cols-2 gap-2">
              <DataCell label="策略名称" value={scalar(spec.name ?? run.name)} />
              <DataCell label="策略类型" value={scalar(spec.kind ?? spec.type ?? run.strategy_type)} />
              <DataCell label="适配器" value={scalar(spec.adapter ?? run.runner)} />
              <DataCell label="引擎" value={scalar(run.engine_mode ?? performance?.engine_mode)} />
            </div>
            {String(spec.type ?? run.strategy_type) === "api" ? (
              <div className="rounded-lg border border-violet/15 bg-violet-soft/35 px-3 py-2.5 text-[10px] leading-relaxed text-mute">外部 API 逻辑未存入平台；仅保留 connector/endpoint 契约，认证值仍由服务端环境变量提供。</div>
            ) : entryConditions.length || exitConditions.length ? (
              <div className="space-y-3">
                <RuleGroup label="开仓 · AND" conditions={entryConditions} />
                <RuleGroup label="平仓 · AND" conditions={exitConditions} />
                <p className="text-[9.5px] leading-relaxed text-faint">每根 bar 收盘判定，下一根 bar 开盘成交；展示的是本 Run 冻结配置，不会进入 Arena-safe 输出。</p>
              </div>
            ) : (
              <div className="rounded-lg border border-edge bg-paper px-3 py-3 text-[10px] leading-relaxed text-mute">
                <div className="font-medium text-ink">{scalar(spec.rules_summary ?? spec.name ?? "后端未返回可展示的规则摘要")}</div>
                {publicParameters.length > 0 && <div className="mt-2 flex flex-wrap gap-1.5">{publicParameters.map(([key, value]) => <span key={key} className="rounded bg-card px-2 py-1 font-mono text-[9px] text-mute">{key}={scalar(value)}</span>)}</div>}
                {!spec.rules_summary && !spec.name && <p className="mt-1">不会从成交记录反推或猜测私有策略。</p>}
              </div>
            )}
          </div>
        </div>

        <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
          <div className="flex flex-wrap items-center justify-between gap-2 border-b border-edge px-4 py-3"><div><div className="text-[9px] font-semibold uppercase tracking-[0.16em] text-faint">Position ledger</div><h3 className="mt-0.5 font-serif text-[14px] font-semibold text-ink">仓位与账户净值</h3><p className="mt-1 text-[9px] text-mute">目标权重模型不计算证券份额；这里展示由冻结模拟净值曲线保留的权重、账户权益与净值。</p></div><AuditPager summary={`表格 ${auditCount(positionRangeStart)}–${auditCount(positionRangeEnd)} / 展示 ${auditCount(positions.length)}${positionSampling.sampled === true ? ` · 全量 ${auditCount(positionTotal)}` : ""}`} page={safePositionPage} pageCount={positionPageCount} onChange={setPositionPage} /></div>
          {unavailable ? (
            <AuditEmpty text="组合结果 unavailable，暂无模拟持仓；不会由信号推算仓位。" />
          ) : positions.length ? (
            <div className="max-h-[360px] overflow-auto"><table className="w-full min-w-[660px] text-[10.5px]"><thead className="sticky top-0 bg-[#F8F6F1] text-left text-faint"><tr><th className="px-3 py-2 font-medium">时间</th><th className="px-3 py-2 font-medium">基准标的</th><th className="px-3 py-2 text-right font-medium">目标权重</th><th className="px-3 py-2 text-right font-medium">账户权益</th><th className="px-3 py-2 text-right font-medium">策略净值</th></tr></thead><tbody>{positionRows.map((position, index) => <tr key={`${(position.symbol ?? datasetSymbol) || "position"}-${position.timestamp ?? position.date ?? index}`} className="border-t border-edge/70"><td className="whitespace-nowrap px-3 py-2 font-mono text-mute">{auditTime(position.timestamp ?? position.date)}</td><td className="px-3 py-2 font-mono text-ink">{(position.symbol ?? datasetSymbol) || "单资产组合"}</td><td className="px-3 py-2 text-right font-mono">{auditPct(position.target_weight ?? position.weight)}</td><td className="px-3 py-2 text-right font-mono">{auditMoney(position.equity, currency)}</td><td className="px-3 py-2 text-right font-mono">{auditNumber(position.net_value) == null ? "—" : auditNumber(position.net_value)?.toFixed(4)}</td></tr>)}</tbody></table></div>
          ) : (
            <AuditEmpty text="performance 未返回 positions / holdings。这里不会把交易方向当作持仓快照。" />
          )}
        </div>
      </div>

      <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-edge px-4 py-3"><div><div className="text-[9px] font-semibold uppercase tracking-[0.16em] text-faint">Execution audit</div><h3 className="mt-0.5 font-serif text-[14px] font-semibold text-ink">信号与调仓成交</h3><p className="mt-1 text-[9px] text-mute">一行代表一次目标权重调整，并非配对平仓交易；后端未返回单笔已实现 PnL 时不推算收益。</p></div><AuditPager summary={`${auditCount(signals.length)}${signalSampling.sampled === true ? `/${auditCount(signalTotal)}` : ""} signals · ${auditCount(trades.length)}${tradeSampling.sampled === true ? `/${auditCount(tradeTotal)}` : ""} trades · 表格 ${auditCount(tradeRangeStart)}–${auditCount(tradeRangeEnd)}`} page={safeTradePage} pageCount={tradePageCount} onChange={setTradePage} /></div>
        {trades.length ? (
          <div className="max-h-[420px] overflow-auto"><table className="w-full min-w-[1380px] text-[10.5px]"><thead className="sticky top-0 bg-[#F8F6F1] text-left text-faint"><tr><th className="px-3 py-2 font-medium">信号时间</th><th className="px-3 py-2 font-medium">成交时间</th><th className="px-3 py-2 font-medium">标的</th><th className="px-3 py-2 font-medium">方向</th><th className="px-3 py-2 text-right font-medium">仓位变更</th><th className="px-3 py-2 text-right font-medium">市场价</th><th className="px-3 py-2 text-right font-medium">有效成交价</th><th className="px-3 py-2 text-right font-medium">名义金额</th><th className="px-3 py-2 text-right font-medium">佣金</th><th className="px-3 py-2 text-right font-medium">滑点</th><th className="px-3 py-2 text-right font-medium">卖出印花税</th><th className="px-3 py-2 text-right font-medium">其他费</th><th className="px-3 py-2 text-right font-medium">总成本</th></tr></thead><tbody>{tradeRows.map((trade, index) => <tr key={trade.id ?? `${(trade.symbol ?? datasetSymbol) || "trade"}-${index}`} className="border-t border-edge/70"><td className="whitespace-nowrap px-3 py-2 font-mono text-mute">{auditTime(trade.signal_timestamp ?? trade.signal_time)}</td><td className="whitespace-nowrap px-3 py-2 font-mono text-mute">{auditTime(trade.execution_timestamp ?? trade.entry_time ?? trade.timestamp ?? trade.date)}</td><td className="px-3 py-2 font-mono text-ink">{(trade.symbol ?? datasetSymbol) || "单资产组合"}</td><td className="px-3 py-2 font-medium text-ink">{/sell|short|卖|空/i.test(String(trade.side ?? trade.direction ?? "")) ? "卖出" : "买入"}</td><td className="whitespace-nowrap px-3 py-2 text-right font-mono">{auditPct(trade.from_weight ?? trade.position_before)} → {auditPct(trade.to_weight ?? trade.position_after)}</td><td className="px-3 py-2 text-right font-mono">{scalar(trade.market_price ?? trade.entry_price)}</td><td className="px-3 py-2 text-right font-mono">{scalar(trade.effective_price ?? trade.entry_price)}</td><td className="px-3 py-2 text-right font-mono">{auditMoney(trade.notional, currency)}</td><td className="px-3 py-2 text-right font-mono" title={trade.minimum_commission_applied ? "本笔已触发最低佣金" : "按成交额与 bp 计提"}>{auditMoney(trade.commission, currency)}{trade.minimum_commission_applied ? "*" : ""}</td><td className="px-3 py-2 text-right font-mono">{auditMoney(trade.slippage, currency)}</td><td className="px-3 py-2 text-right font-mono">{auditMoney(trade.stamp_duty, currency)}</td><td className="px-3 py-2 text-right font-mono">{auditMoney(trade.other_cost, currency)}</td><td className="px-3 py-2 text-right font-mono font-semibold text-ink">{auditMoney(trade.total_cost ?? trade.cost, currency)}</td></tr>)}</tbody></table></div>
        ) : (
          <AuditEmpty text={unavailable ? "组合回测不可用，未产生模拟成交。" : "本次结果没有成交记录；不会生成占位交易。"} />
        )}
      </div>
    </section>
  );
}

function DataCell({ label, value }: { label: string; value: string }) {
  return <div className="rounded-lg border border-edge bg-paper px-2.5 py-2"><div className="text-[8.5px] uppercase tracking-wider text-faint">{label}</div><div className="mt-1 truncate font-mono text-[9.5px] font-medium text-ink" title={value}>{value}</div></div>;
}

function RuleGroup({ label, conditions }: { label: string; conditions: unknown[] }) {
  return <div><div className="mb-1.5 text-[9.5px] font-semibold text-ink">{label}</div><div className="space-y-1.5">{conditions.length ? conditions.map((condition, index) => <div key={index} className="rounded-md bg-paper px-2.5 py-2 font-mono text-[9px] text-mute">{index + 1}. {ruleConditionText(condition)}</div>) : <div className="rounded-md border border-dashed border-edge px-2.5 py-2 text-[9px] text-faint">未定义</div>}</div></div>;
}

function AuditEmpty({ text }: { text: string }) {
  return <div className="grid min-h-[180px] place-items-center px-6 text-center text-[10.5px] leading-relaxed text-mute">{text}</div>;
}

export default function BacktestDetail() {
  const runId = useStore((s) => s.currentBTRunId);
  const backFromBTDetail = useStore((s) => s.backFromBTDetail);

  const [run, setRun] = useState<BTRun | null>(null);
  const [metrics, setMetrics] = useState<BTMetricsV2 | null>(null);
  const [performance, setPerformance] = useState<BTPerformanceResponse | null>(null);
  const [performanceLoading, setPerformanceLoading] = useState(false);
  const [performanceError, setPerformanceError] = useState<string | null>(null);
  const [resultTab, setResultTab] = useState<ResultTab>("financial_overview");
  const [loading, setLoading] = useState(true);
  const [runLoadError, setRunLoadError] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [acting, setActing] = useState(false);

  // 保存数据集完整目录；渲染时再按本 Run 的执行窗口/已产生预测收窄，避免把未执行样本当成结果。
  const [catalog, setCatalog] = useState<{ total: number; items: BTEventCatalogItem[] }>({ total: 0, items: [] });
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [catalogLoadedOnce, setCatalogLoadedOnce] = useState(false);
  const [fOnlyIncorrect, setFOnlyIncorrect] = useState(false);
  const [fMarket, setFMarket] = useState<string>("");
  const [fEventType, setFEventType] = useState<string>("");
  const [fStatus, setFStatus] = useState<BTEventStatus | "">(
    "",
  );

  // SSE 最近事件（时间线）
  const [sseEvents, setSseEvents] = useState<BTSSEEvent[]>([]);
  const sseAbortRef = useRef<AbortController | null>(null);
  const activeRunIdRef = useRef(runId);
  const runRequestRef = useRef(0);
  const metricsRequestRef = useRef(0);
  const performanceRequestRef = useRef(0);
  const catalogRequestRef = useRef(0);
  // Update during render, before effects clean up, so a late response from the
  // previous route cannot land in the new Run even for a single frame.
  activeRunIdRef.current = runId;
  const [sseConnected, setSseConnected] = useState(false);
  const [teamFullActivity, setTeamFullActivity] = useState<BTRunActivity | null>(null);
  const [activityClock, setActivityClock] = useState(() => Date.now());

  // 单 case 展开详情（点击表格行切换；detail 网络请求在展开后懒加载）
  const [expandedEventId, setExpandedEventId] = useState<string | null>(null);
  const [predDetail, setPredDetail] = useState<BTPredictionDetail | null>(null);
  const [predDetailLoading, setPredDetailLoading] = useState(false);
  const [predDetailTab, setPredDetailTab] = useState<DetailTab>("log");
  const currentStrategyFamily = runStrategyFamily(run);
  const questionResults = useBacktestQuestionResults(runId, Boolean(run && run.id === runId && currentStrategyFamily === "event"));

  // expandedEventId 变化 → 懒加载详情；切页/取消展开时清零
  useEffect(() => {
    if (!runId || !expandedEventId) {
      setPredDetail(null);
      setPredDetailLoading(false);
      return;
    }
    const matched = catalog.items.find((x) => x.event_id === expandedEventId);
    // 先把列表里已有的 prediction 填进去（若 pending/processing 则没有 prediction，也要构造一个 detail 的 prediction 壳，保证 UI 不空）
    const skeletonEventMeta: Record<string, unknown> = {};
    if (matched) {
      for (const k of ["title", "event_time", "source_url", "symbol", "market", "event_type_l2", "event_text"]) {
        const v = (matched as unknown as Record<string, unknown>)[k];
        if (v != null && v !== "") skeletonEventMeta[k] = v;
      }
    }
    if (matched) {
      if (matched.prediction) {
        setPredDetail({ prediction: matched.prediction, trajectory: null, event_meta: Object.keys(skeletonEventMeta).length ? skeletonEventMeta : undefined });
      } else {
        setPredDetail({
          prediction: {
            id: "",
            run_id: runId,
            event_id: matched.event_id,
            symbol: matched.symbol ?? null,
            market: matched.market ?? null,
            event_type_l2: matched.event_type_l2 ?? null,
            pred_direction: "",
            abstain: false,
            created_at: new Date().toISOString(),
          },
          trajectory: null,
          event_meta: Object.keys(skeletonEventMeta).length ? skeletonEventMeta : undefined,
        });
      }
    }
    let cancelled = false;
    // 只有 status=done 的 prediction 才发网络请求拉 trajectory；pending/processing 直接显示 loading 文案
    if (matched?.status !== "done" || !matched.prediction) {
      setPredDetailLoading(false);
      return;
    }
    setPredDetailLoading(true);
    void (async () => {
      try {
        const d = await api.btGetPredictionDetail(runId, matched?.event_id ?? "");
        if (cancelled) return;
        setPredDetail(d);
      } catch (e) {
        if (cancelled) return;
        console.debug("[bt pred detail] load fail", e instanceof Error ? e.message : String(e));
      } finally {
        if (!cancelled) setPredDetailLoading(false);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [expandedEventId, runId, catalog.items]);

  // -------------------- 初始加载 --------------------
  useEffect(() => {
    if (!runId) return;
    const requestedRunId = runId;
    setRun(null);
    setMetrics(null);
    setPerformance(null);
    setPerformanceLoading(false);
    setPerformanceError(null);
    setCatalog({ total: 0, items: [] });
    setCatalogLoading(false);
    setCatalogError(null);
    setCatalogLoadedOnce(false);
    setExpandedEventId(null);
    setPredDetail(null);
    setRunLoadError(null);
    setActionErr(null);
    setActing(false);
    setSseEvents([]);
    setSseConnected(false);
    setTeamFullActivity(null);
    setResultTab("financial_overview");
    setLoading(true);
    Promise.allSettled([loadRun(), loadMetrics()])
      .finally(() => {
        if (activeRunIdRef.current === requestedRunId) setLoading(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  useEffect(() => {
    if (run?.runner !== "team_full" || run.status !== "running") return;
    setActivityClock(Date.now());
    const interval = window.setInterval(() => setActivityClock(Date.now()), 1000);
    return () => window.clearInterval(interval);
  }, [run?.runner, run?.status]);

  // -------------------- SSE 订阅 --------------------
  useEffect(() => {
    if (!runId || !run || run.id !== runId) return;
    // 一个 runId 只维护一条 SSE 连接（进入详情页且 run 加载完成后开启）。
    // 终态（done/failed/cancelled）不订阅；pending/running/paused 均保持连接，
    // 以便实时收到 prediction / run_status_changed / run_done 等事件。
    // 依赖用「run 是否已加载」(!run) 而非 run.status，避免 status 每次变化都重开连接。
    const terminal = ["done", "failed", "cancelled"].includes(run.status);
    if (terminal) return;

    const ctl = new AbortController();
    let cancelled = false;
    let streamStarted = false;
    let streamDone = false;

    // 用 50ms 延迟规避 React 18 StrictMode 双 mount 首帧立刻 abort 的问题。
    const id = window.setTimeout(() => {
      if (cancelled || ctl.signal.aborted) return;
      streamStarted = true;
      sseAbortRef.current = ctl;
      setSseConnected(true);

      const onEvent = (ev: BTSSEEvent) => {
        if (cancelled || activeRunIdRef.current !== runId) return;
        if (ev.activity !== undefined) setTeamFullActivity(ev.activity ?? null);
        if (ev.type === "heartbeat") return;
        // 收集事件（最新在前，保留最近 50 条）
        setSseEvents((xs) => [ev, ...xs].slice(0, 50));

        if (ev.type === "hello") {
          if (typeof ev.done_events === "number") {
            setRun((r) => r ? { ...r, done_events: ev.done_events!, status: (ev.status as BTStatus) ?? r.status } : r);
          }
        } else if (ev.type === "stage_progress") {
          // Stage updates are advisory only.  In particular, do not change
          // done_events until a real prediction event arrives.
        } else if (ev.type === "progress") {
          if (typeof ev.done_count === "number") {
            setRun((r) => r ? { ...r, done_events: ev.done_count! } : r);
          }
        } else if (ev.type === "prediction") {
          setRun((r) => r ? { ...r, done_events: (r.done_events ?? 0) + 1 } : r);
          // 局部 merge：catalog 里 event_id 匹配 → 直接 status=done + prediction（减少轮询压力 + UI 立即刷新）
          if (ev.event_id && ev.prediction) {
            setCatalog((c) => ({
              ...c,
              items: c.items.map((it) =>
                it.event_id === ev.event_id
                  ? {
                      ...it,
                      status: "done",
                      prediction: ev.prediction as BTPredictionItem,
                    }
                  : it,
              ),
            }));
          }
          // 间隔性同步 run status；catalog 靠 2s 轮询兜底 + 上面 prediction merge 实时
          void loadRun(true);
        } else if (ev.type === "metrics_snapshot") {
          setRun((r) => r ? {
            ...r,
            done_events: ev.done_count ?? r.done_events,
            acc_t3_strict: ev.acc_t3_strict ?? r.acc_t3_strict,
            acc_t3_strict_lo: ev.acc_t3_strict_lo ?? r.acc_t3_strict_lo,
            acc_t3_non_neutral: ev.acc_t3_non_neutral ?? r.acc_t3_non_neutral,
          } : r);
          // 顺便刷新完整 metrics（偶尔）
          if ((ev.done_count ?? 0) % 10 === 0) void loadMetrics(true);
        } else if (ev.type === "run_started") {
          setRun((r) => r ? { ...r, status: "running", started_at: new Date().toISOString() } : r);
        } else if (ev.type === "run_status_changed") {
          // pause/resume 由后端事件驱动状态同步，不再依赖乐观更新
          if (ev.to) setRun((r) => r ? { ...r, status: ev.to as BTStatus } : r);
        } else if (ev.type === "run_done" || ev.type === "run_failed" || ev.type === "run_cancelled") {
          const st: BTStatus = ev.type === "run_done" ? "done" : ev.type === "run_failed" ? "failed" : "cancelled";
          setRun((r) => r ? { ...r, status: st, finished_at: new Date().toISOString(),
            error_msg: ev.error ?? ev.message ?? r.error_msg } : r);
          setTeamFullActivity(null);
          // 结束后拉一次完整数据
          const finalLoads: Promise<unknown>[] = [loadRun(true), loadMetrics(true), loadPerformance(true)];
          if (currentStrategyFamily === "event") finalLoads.push(loadCatalog(true));
          void Promise.allSettled(finalLoads);
        }
      };

      streamBacktest(runId, { onEvent, signal: ctl.signal })
        .catch((e) => {
          if (streamDone) return;
          if (e instanceof StreamAbortedError) return;
          const isAbort = (e instanceof DOMException && e.name === "AbortError") ||
            (typeof (e as Error)?.message === "string" &&
              /abort|cancelled|user aborted/i.test((e as Error).message));
          if (isAbort) return;
          console.debug("[bt sse] disconnected", e instanceof Error ? e.message : String(e));
        })
        .finally(() => {
          streamDone = true;
          if (streamStarted && activeRunIdRef.current === runId) setSseConnected(false);
        });
    }, 50);

    return () => {
      cancelled = true;
      window.clearTimeout(id);
      ctl.abort();
      sseAbortRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, !!run, currentStrategyFamily]);

  // -------------------- 事件目录加载与串行轮询 --------------------
  useEffect(() => {
    if (!runId || !run || run.id !== runId) return;
    if (currentStrategyFamily !== "event" || runIsArenaSafe(run)) {
      setCatalog({ total: 0, items: [] });
      setCatalogLoading(false);
      return;
    }
    const active = run.status === "pending" || run.status === "running";
    let cancelled = false;
    let timer: number | null = null;
    const refresh = async (silent: boolean) => {
      await loadCatalog(silent);
      // Schedule only after the prior request settles. Slow catalog responses
      // therefore cannot continuously invalidate one another.
      if (!cancelled && active) {
        timer = window.setTimeout(() => { void refresh(true); }, 2000);
      }
    };
    void refresh(false);
    return () => {
      cancelled = true;
      if (timer != null) window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, run?.id, run?.status, run?.visibility, currentStrategyFamily, fOnlyIncorrect, fMarket, fEventType, fStatus]);

  // -------------------- 加载函数 --------------------
  async function loadRun(silent = false) {
    if (!runId) return;
    const requestedRunId = runId;
    const request = ++runRequestRef.current;
    const isCurrent = () => activeRunIdRef.current === requestedRunId && runRequestRef.current === request;
    if (!silent) setLoading(true);
    try {
      const r = await api.btGetRun(requestedRunId);
      if (!isCurrent()) return;
      setRunLoadError(null);
      setRun(r);
      if (!silent) {
        const family = runStrategyFamily(r);
        const returnForecastOnly = r.strategy_spec?.kind === "return_forecast";
        setResultTab(family !== "event" && !returnForecastOnly ? "financial_overview" : "return_forecast");
      }
      setTeamFullActivity(r.activity ?? null);
    } catch (error) {
      if (!isCurrent()) return;
      const message = error instanceof Error ? error.message : String(error);
      if (!silent) {
        setRun(null);
        setRunLoadError(message);
      }
      console.debug("[bt run] load fail", message);
    } finally {
      if (!silent && isCurrent()) setLoading(false);
    }
  }

  async function loadMetrics(_silent = false) {
    if (!runId) return;
    const requestedRunId = runId;
    const request = ++metricsRequestRef.current;
    const isCurrent = () => activeRunIdRef.current === requestedRunId && metricsRequestRef.current === request;
    try {
      const m = await api.btGetMetrics(requestedRunId);
      if (!isCurrent()) return;
      setMetrics(m);
    } catch {
      /* 没 labels 时 compute_metrics 可能失败 → 置空 */
      if (isCurrent()) setMetrics(null);
    }
  }

  async function loadPerformance(silent = false, includeKline?: boolean) {
    if (!runId || (run && run.id !== runId)) return;
    const requestedRunId = runId;
    const request = ++performanceRequestRef.current;
    const isCurrent = () => activeRunIdRef.current === requestedRunId && performanceRequestRef.current === request;
    const requestedRun = run?.id === requestedRunId ? run : null;
    const shouldIncludeKline = includeKline ?? (requestedRun ? !runIsArenaSafe(requestedRun) : false);
    if (!silent) setPerformanceLoading(true);
    setPerformanceError(null);
    try {
      const result = await api.btGetPerformance(requestedRunId, shouldIncludeKline);
      if (!isCurrent()) return;
      setPerformance(result);
    } catch (e) {
      if (!isCurrent()) return;
      setPerformance(null);
      setPerformanceError(e instanceof Error ? e.message : String(e));
    } finally {
      if (isCurrent()) setPerformanceLoading(false);
    }
  }

  async function loadCatalog(silent = false) {
    if (!runId) return;
    const requestedRunId = runId;
    const requestedRun = run?.id === requestedRunId ? run : null;
    if (!requestedRun || runStrategyFamily(requestedRun) !== "event" || runIsArenaSafe(requestedRun)) {
      if (requestedRun && runIsArenaSafe(requestedRun) && activeRunIdRef.current === requestedRunId) {
        setCatalog({ total: 0, items: [] });
      }
      return;
    }
    const request = ++catalogRequestRef.current;
    const isCurrent = () => activeRunIdRef.current === requestedRunId && catalogRequestRef.current === request;
    const filters = {
      market: fMarket || undefined,
      event_type_l2: fEventType || undefined,
      only_incorrect: fOnlyIncorrect,
      status: fStatus || undefined,
    };
    if (!silent) setCatalogLoading(true);
    setCatalogError(null);
    try {
      const r = await api.btListEventCatalog(requestedRunId, filters);
      if (!isCurrent()) return;
      setCatalog({ total: r.total, items: r.items });
      setCatalogLoadedOnce(true);
    } catch (e) {
      if (!isCurrent()) return;
      const msg = e instanceof Error ? e.message : String(e);
      setCatalogError(msg);
      console.debug("[bt catalog] load fail", msg);
    } finally {
      if (isCurrent()) setCatalogLoading(false);
    }
  }

  // Run 先加载后再决定是否请求事件 K 线，避免 Arena-safe 首帧暴露事件级行情。
  useEffect(() => {
    if (!runId || !run || run.id !== runId) return;
    void loadPerformance(false, !runIsArenaSafe(run));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, run?.id]);

  // -------------------- 操作 --------------------
  async function doStart() {
    if (!runId) return;
    const requestedRunId = runId;
    const isCurrent = () => activeRunIdRef.current === requestedRunId;
    setActing(true);
    setActionErr(null);
    try {
      setRun((r) => r?.id === requestedRunId ? { ...r, status: "running" } : r);
      const r = await api.btStartRun(requestedRunId);
      if (!isCurrent()) return;
      if (!r.ok) throw new Error(r.message || "start failed");
    } catch (e) {
      if (!isCurrent()) return;
      setActionErr(`启动失败：${e instanceof Error ? e.message : String(e)}`);
      void loadRun(true);
    } finally {
      if (isCurrent()) setActing(false);
    }
  }

  async function doCancel() {
    if (!runId) return;
    const requestedRunId = runId;
    const isCurrent = () => activeRunIdRef.current === requestedRunId;
    setActing(true);
    setActionErr(null);
    try {
      const r = await api.btCancelRun(requestedRunId);
      if (!isCurrent()) return;
      if (!r.ok) throw new Error(r.message || "cancel failed");
      setRun((r) => r?.id === requestedRunId ? { ...r, status: "cancelled" } : r);
      // 后端 orchestrator 收到 cancel 信号后，会在同一条 SSE 上推送 run_cancelled 并自然关闭流；
      // 客户端主动 abort 只会产生 DevTools ERR_ABORTED 噪音。
    } catch (e) {
      if (!isCurrent()) return;
      setActionErr(`取消失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      if (isCurrent()) setActing(false);
    }
  }

  async function doPause() {
    if (!runId) return;
    const requestedRunId = runId;
    const isCurrent = () => activeRunIdRef.current === requestedRunId;
    setActing(true);
    setActionErr(null);
    try {
      setRun((r) => r?.id === requestedRunId ? { ...r, status: "paused" } : r);
      const r = await api.btPauseRun(requestedRunId);
      if (!isCurrent()) return;
      if (!r.ok) throw new Error(r.message || "pause failed");
    } catch (e) {
      if (!isCurrent()) return;
      setActionErr(`暂停失败：${e instanceof Error ? e.message : String(e)}`);
      void loadRun(true);
    } finally {
      if (isCurrent()) setActing(false);
    }
  }

  async function doResume() {
    if (!runId) return;
    const requestedRunId = runId;
    const isCurrent = () => activeRunIdRef.current === requestedRunId;
    setActing(true);
    setActionErr(null);
    try {
      setRun((r) => r?.id === requestedRunId ? { ...r, status: "running" } : r);
      const r = await api.btResumeRun(requestedRunId);
      if (!isCurrent()) return;
      if (!r.ok) throw new Error(r.message || "resume failed");
    } catch (e) {
      if (!isCurrent()) return;
      setActionErr(`继续失败：${e instanceof Error ? e.message : String(e)}`);
      void loadRun(true);
    } finally {
      if (isCurrent()) setActing(false);
    }
  }

  const progress = useMemo(() => {
    if (!run) return 0;
    if (!run.total_events) return 0;
    return Math.min(100, Math.round((run.done_events / run.total_events) * 100));
  }, [run]);

  const runConfig = (run?.config ?? {}) as Record<string, unknown>;
  const strategyType = currentStrategyFamily;
  const visibility = String(run?.visibility ?? runConfig.visibility ?? "private");
  const protocolHash = String(run?.protocol_hash ?? runConfig.protocol_hash ?? "");
  const evaluationHorizon = String(
    run?.evaluation_horizon ??
    runConfig.evaluation_horizon ??
    ((runConfig.protocol as Record<string, unknown> | undefined)?.evaluation_horizon) ??
    "t3",
  );
  const evaluationHorizonLabel = /^t\d+$/i.test(evaluationHorizon)
    ? `T+${evaluationHorizon.slice(1)}`
    : evaluationHorizon.toUpperCase();
  const executionContract = (
    run?.execution_spec ??
    (runConfig.execution_spec && typeof runConfig.execution_spec === "object" ? runConfig.execution_spec : null)
  ) as Record<string, unknown> | null;
  const executionSpec = (
    executionContract?.applied && typeof executionContract.applied === "object"
      ? executionContract.applied
      : executionContract
  ) as Record<string, unknown> | null;
  const datasetSnapshot = (
    runConfig.dataset_snapshot && typeof runConfig.dataset_snapshot === "object"
      ? runConfig.dataset_snapshot
      : {}
  ) as Record<string, unknown>;
  const appliedFrequency = String(
    datasetSnapshot.frequency ??
    (performance?.dataset && typeof performance.dataset === "object" ? performance.dataset.frequency : null) ??
    "频率未声明",
  );
  const predictionOnly = Boolean(performance?.prediction_only || run?.strategy_spec?.kind === "return_forecast");
  const hasQuantForecast = Boolean(
    predictionOnly ||
    (performance?.return_forecast?.n_forecasts ?? 0) > 0 ||
    (Array.isArray(performance?.return_forecasts) && performance.return_forecasts.length > 0),
  );
  // A legacy Run may reveal prediction_only only after performance arrives.
  // Keep the selected tab valid without disturbing any valid user selection on refresh.
  useEffect(() => {
    if (!run || run.id !== runId) return;
    const family = runStrategyFamily(run);
    setResultTab((current) => {
      if (family === "event") {
        return current === "financial_overview" || current === "portfolio_audit" ? "return_forecast" : current;
      }
      if (predictionOnly) {
        return current === "financial_overview" || current === "portfolio_audit" || current === "event_audit" || current === "qa"
          ? "return_forecast"
          : current;
      }
      return (current === "return_forecast" && !hasQuantForecast) || current === "event_audit" || current === "qa" ? "financial_overview" : current;
    });
  }, [runId, run?.id, predictionOnly, hasQuantForecast]);
  const forecastBars = run?.strategy_spec?.parameters?.horizon_bars ?? performance?.return_forecast?.horizon_bars ?? "N";
  const strategyLabel = predictionOnly ? "量化收益预测" : strategyType === "quant"
    ? "量化策略"
    : strategyType === "api"
    ? "外部决策流"
    : "事件模型";
  const isArenaSafe = runIsArenaSafe(run);
  const visibilityLabel = isArenaSafe
    ? "Arena-safe"
    : visibility === "team"
    ? "团队可见"
    : "仅自己可见";
  const failureHelp = run?.error_msg ? describeRunFailure(run.error_msg) : null;
  const semanticQuality = datasetSnapshot.semantic_quality ?? runConfig.semantic_quality;
  const semanticRecord = semanticQuality && typeof semanticQuality === "object"
    ? semanticQuality as Record<string, unknown>
    : {};
  const semanticText = typeof semanticQuality === "string"
    ? semanticQuality
    : String(semanticRecord.status ?? semanticRecord.level ?? "");
  const syntheticDemo = /demo|synthetic|mechanism/i.test(semanticText)
    || semanticRecord.formal_evaluation_eligible === false
    || ["cn_etf_10", "us_etf_10"].includes(String(run?.dataset_id ?? ""));
  const catalogInvalidCount = catalog.items.filter((item) => predictionOutcome(item.prediction) === "invalid_output").length;
  const catalogAbstainCount = catalog.items.filter((item) => predictionOutcome(item.prediction) === "voluntary_abstain").length;
  const runInvalidCount = Number(run?.invalid_output_count ?? catalogInvalidCount ?? 0);
  const runVoluntaryAbstainCount = Number(run?.voluntary_abstain_count ?? catalogAbstainCount ?? 0);
  const runInsufficientDataCount = Number(run?.insufficient_data_count ?? catalog.items.filter((item) => predictionOutcome(item.prediction) === "insufficient_data").length);
  const runWarningCount = Math.max(
    Number(run?.warning_count ?? 0),
    runInvalidCount + runVoluntaryAbstainCount + runInsufficientDataCount,
  );
  const doneWithWarnings = run?.status === "done" && (
    /warning/i.test(String(run.completion_quality ?? "")) ||
    runWarningCount > 0
  );
  const performanceProtocol = performance?.effective_protocol && typeof performance.effective_protocol === "object"
    ? performance.effective_protocol as Record<string, unknown>
    : {};
  const performanceApplied = performanceProtocol.applied && typeof performanceProtocol.applied === "object"
    ? performanceProtocol.applied as Record<string, unknown>
    : {};
  const performanceEventWindow = performanceApplied.event_window && typeof performanceApplied.event_window === "object"
    ? performanceApplied.event_window as Record<string, unknown>
    : {};
  const datasetCoverage = datasetSnapshot.coverage && typeof datasetSnapshot.coverage === "object"
    ? datasetSnapshot.coverage as Record<string, unknown>
    : {};
  const auditWindowStart = performanceEventWindow.start_date ?? executionSpec?.start_date;
  const auditWindowEnd = performanceEventWindow.end_date ?? executionSpec?.end_date;
  const runIsTerminal = ["done", "failed", "cancelled"].includes(String(run?.status ?? ""));
  const auditCatalogItems = catalog.items.filter((item) => (
    runIsTerminal
      ? Boolean(item.prediction)
      : eventInExecutionWindow(item, auditWindowStart, auditWindowEnd)
  ));
  const executionEventTotal = Math.max(0, Math.round(auditNumber(run?.total_events) ?? auditCatalogItems.length));
  const executedEventCount = Math.max(
    auditCatalogItems.filter((item) => Boolean(item.prediction)).length,
    Math.max(0, Math.round(auditNumber(run?.done_events) ?? 0)),
  );
  const catalogFilterActive = Boolean(fOnlyIncorrect || fMarket || fEventType || fStatus);
  const datasetEventTotal = Math.max(
    executionEventTotal,
    Math.round(
      auditNumber(performanceEventWindow.before_count)
      ?? auditNumber(datasetCoverage.row_count)
      ?? (!catalogFilterActive ? auditNumber(catalog.total) : null)
      ?? executionEventTotal,
    ),
  );
  const eventAuditNote = datasetEventTotal > executionEventTotal
    ? (runIsTerminal && executedEventCount === executionEventTotal
      ? `${executedEventCount} 已执行 / ${datasetEventTotal} 数据集`
      : `${executedEventCount}/${executionEventTotal} 已执行 · ${datasetEventTotal} 数据集`)
    : `${executedEventCount}/${executionEventTotal} 已执行`;
  const eventCatalogSummary = catalogFilterActive
    ? `筛选后 ${auditCatalogItems.length} 条 · 本 Run ${executedEventCount}/${executionEventTotal} 已执行`
    : runIsTerminal
    ? `本 Run ${auditCatalogItems.length} 条结果${datasetEventTotal > executionEventTotal ? ` · 数据集共 ${datasetEventTotal} 条` : ""}`
    : `本 Run 已完成 ${executedEventCount}/${executionEventTotal}${datasetEventTotal > executionEventTotal ? ` · 数据集共 ${datasetEventTotal} 条` : ""}`;

  // -------------------- 渲染 --------------------
  if (!runId) {
    return (
      <div className="flex flex-1 items-center justify-center text-mute text-[13px]">
        <button onClick={backFromBTDetail} className="text-brand hover:underline">← 返回回测列表</button>
        <span className="mx-2">·</span>
        未指定回测 run_id
      </div>
    );
  }

  if (!run || run.id !== runId) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-mute text-[13px]">
        <div className="flex items-center">{loading && <Loader2 size={16} className="mr-2 animate-spin" />}{loading ? "加载回测详情..." : "未能加载该回测运行。"}</div>
        {!loading && runLoadError && <p className="max-w-xl text-[10.5px] text-rise">{runLoadError}</p>}
        {!loading && <div className="flex gap-2"><button type="button" onClick={() => void loadRun()} className="rounded-lg border border-edge bg-card px-3 py-1.5 text-[10.5px] text-ink">重试</button><button type="button" onClick={backFromBTDetail} className="rounded-lg px-3 py-1.5 text-[10.5px] text-brand">返回列表</button></div>}
      </div>
    );
  }

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden">
      {/* ===================================== Header ===================================== */}
      <header className="flex shrink-0 items-center justify-between border-b border-edge bg-paper px-6 py-3.5">
        <div className="flex min-w-0 items-center gap-3">
          <button
            onClick={backFromBTDetail}
            className="shrink-0 rounded-lg p-1.5 text-mute transition hover:bg-edge/60 hover:text-ink"
            title="返回回测列表"
          >
            <ArrowLeft size={16} />
          </button>
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="truncate font-serif text-[17px] font-semibold text-ink">{run?.name ?? "加载中..."}</h2>
              {run && <StatusBadge status={run.status} prefix={currentStrategyFamily === "event" ? "预测" : ""} />}
              {currentStrategyFamily === "event" && questionResults.value?.enabled && questionResults.value.results && <button type="button" onClick={() => setResultTab("qa")} title="查看独立问答任务的回答与评分" className="shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-violet/40"><LabStatus status={questionTaskStatus(questionResults.value.results)} prefix="问答" /></button>}
              {doneWithWarnings && (
                <span className="inline-flex items-center gap-1 rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-[10.5px] font-medium text-amber-700" title={`无效输出 ${runInvalidCount} · 数据不足 ${runInsufficientDataCount} · 主动弃权 ${runVoluntaryAbstainCount}`}>
                  <AlertTriangle size={11} /> 完成但有警告{runWarningCount ? ` · ${runWarningCount}` : ""}
                </span>
              )}
              {run && (
                <span className="rounded-full border border-violet/20 bg-violet-soft/50 px-2 py-0.5 text-[10px] font-medium text-violet">
                  {strategyLabel}
                </span>
              )}
              {sseConnected && (
                <span className="inline-flex items-center gap-1 rounded-full bg-jade-soft/70 px-2 py-0.5 text-[10px] text-jade">
                  <span className="h-1.5 w-1.5 animate-blink rounded-full bg-jade"></span>
                  SSE 实时
                </span>
              )}
            </div>
            {run && (
              <div className="mt-0.5 font-mono text-[10.5px] text-faint">
                id {run.id.slice(0, 14)}… · runner={run.runner} · concurrency={run.concurrency}
                {run.prompt_variant ? ` · prompt=${run.prompt_variant}` : ""}
                {run.model_version ? ` · model=${run.model_version}` : ""}
              </div>
            )}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button
            onClick={() => {
              const refreshes: Promise<unknown>[] = [loadRun(true), loadMetrics(true), loadPerformance()];
              if (currentStrategyFamily === "event") refreshes.push(loadCatalog(true));
              questionResults.refresh();
              void Promise.allSettled(refreshes);
            }}
            className="flex items-center gap-1 rounded-lg border border-edge bg-card px-3 py-1.5 text-[12px] text-mute transition hover:bg-edge/40 hover:text-ink"
          >
            <RefreshCw size={12.5} /> 刷新
          </button>
          {run?.status === "pending" && (
            <button
              onClick={() => void doStart()}
              disabled={acting}
              className="flex items-center gap-1.5 rounded-lg bg-jade px-3.5 py-1.5 text-[12.5px] font-medium text-card shadow-card transition hover:bg-jade-hover hover:shadow-pop disabled:opacity-50"
            >
              {acting ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
              启动回测
            </button>
          )}
          {run?.status === "running" && (
            <>
              <button
                onClick={() => void doPause()}
                disabled={acting}
                className="flex items-center gap-1.5 rounded-lg bg-amber px-3.5 py-1.5 text-[12.5px] font-medium text-card shadow-card transition hover:opacity-90 hover:shadow-pop disabled:opacity-50"
              >
                {acting ? <Loader2 size={13} className="animate-spin" /> : <Pause size={13} />}
                暂停
              </button>
              <button
                onClick={() => void doCancel()}
                disabled={acting}
                className="flex items-center gap-1.5 rounded-lg bg-rise px-3.5 py-1.5 text-[12.5px] font-medium text-card shadow-card transition hover:opacity-90 hover:shadow-pop disabled:opacity-50"
              >
                {acting ? <Loader2 size={13} className="animate-spin" /> : <Square size={13} />}
                取消
              </button>
            </>
          )}
          {run?.status === "paused" && (
            <>
              <button
                onClick={() => void doResume()}
                disabled={acting}
                className="flex items-center gap-1.5 rounded-lg bg-jade px-3.5 py-1.5 text-[12.5px] font-medium text-card shadow-card transition hover:bg-jade-hover hover:shadow-pop disabled:opacity-50"
              >
                {acting ? <Loader2 size={13} className="animate-spin" /> : <Play size={13} />}
                继续
              </button>
              <button
                onClick={() => void doCancel()}
                disabled={acting}
                className="flex items-center gap-1.5 rounded-lg bg-rise px-3.5 py-1.5 text-[12.5px] font-medium text-card shadow-card transition hover:opacity-90 hover:shadow-pop disabled:opacity-50"
              >
                {acting ? <Loader2 size={13} className="animate-spin" /> : <Square size={13} />}
                取消
              </button>
            </>
          )}
        </div>
      </header>

      {actionErr && (
        <div className="mx-6 mt-3 rounded-lg border border-rise/30 bg-rise/5 px-3 py-2 text-[12px] text-rise">
          ⚠️ {actionErr}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-4 space-y-5">
        {/* ===================================== 冻结实验口径 ===================================== */}
        {run && (
          <section className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-edge bg-[#F6F4EF] px-4 py-3">
              <div>
                <div className="text-[10px] font-semibold uppercase tracking-[0.16em] text-faint">Frozen experiment</div>
                <h3 className="mt-0.5 font-serif text-[14px] font-semibold text-ink">本次运行的模型与评测口径</h3>
              </div>
              <span className="rounded-full border border-jade/25 bg-jade-soft/50 px-2.5 py-1 font-mono text-[10px] text-jade">
                {protocolHash ? `protocol ${protocolHash.slice(0, 12)}` : "协议指纹将在运行前生成"}
              </span>
            </div>
            <div className="grid grid-cols-1 divide-y divide-edge sm:grid-cols-2 sm:divide-x sm:divide-y-0 xl:grid-cols-4">
              <div className="flex items-start gap-3 px-4 py-3.5">
                <Layers3 size={15} className="mt-0.5 shrink-0 text-violet" />
                <div className="min-w-0"><div className="text-[10px] uppercase tracking-wider text-faint">策略与引擎</div><div className="mt-1 text-[12.5px] font-medium text-ink">{strategyLabel}</div><div className="mt-0.5 truncate font-mono text-[10px] text-mute">{run.engine_mode ?? (strategyType === "event" ? "event_proxy" : "portfolio")} · {run.runner}</div></div>
              </div>
              <div className="flex items-start gap-3 px-4 py-3.5">
                <Database size={15} className="mt-0.5 shrink-0 text-brand" />
                <div className="min-w-0"><div className="text-[10px] uppercase tracking-wider text-faint">数据快照</div><div className="mt-1 truncate text-[12.5px] font-medium text-ink">{run.dataset_name ?? run.dataset_id ?? "自定义数据集"}</div><div className="mt-0.5 truncate font-mono text-[10px] text-mute">{run.dataset_version ? `version ${run.dataset_version}` : `${run.total_events} 个评测样本`}</div></div>
              </div>
              <div className="flex items-start gap-3 px-4 py-3.5">
                <Fingerprint size={15} className="mt-0.5 shrink-0 text-jade" />
                <div className="min-w-0"><div className="text-[10px] uppercase tracking-wider text-faint">执行口径</div><div className="mt-1 text-[12.5px] font-medium text-ink">{predictionOnly ? `${appliedFrequency} · 未来 ${forecastBars} 根 K 线` : strategyType === "event" ? evaluationHorizonLabel : `${appliedFrequency} · next-open`}</div><div className="mt-0.5 text-[10px] text-mute">{predictionOnly ? "当前收盘至目标收盘 · 仅评估预测误差" : `摩擦 ${Number(executionSpec?.fee_bps ?? executionSpec?.commission_bps ?? 0) + Number(executionSpec?.slippage_bps ?? 0)} bps · ${run.result_nature ?? "结果性质待返回"}`}</div></div>
              </div>
              <div className="flex items-start gap-3 px-4 py-3.5">
                <LockKeyhole size={15} className="mt-0.5 shrink-0 text-amber" />
                <div className="min-w-0"><div className="text-[10px] uppercase tracking-wider text-faint">可见范围</div><div className="mt-1 text-[12.5px] font-medium text-ink">{visibilityLabel}</div><div className="mt-0.5 text-[10px] text-mute">敏感策略细节不会进入共享 Arena</div></div>
              </div>
            </div>
          </section>
        )}

        {strategyType === "event" && syntheticDemo && (
          <section role="note" className="rounded-card border border-amber-300 bg-amber-50 px-4 py-3.5 shadow-card">
            <div className="flex items-start gap-2.5">
              <AlertTriangle size={15} className="mt-0.5 shrink-0 text-amber-700" />
              <div>
                <h3 className="text-[12.5px] font-semibold text-amber-900">合成机制演示数据集</h3>
                <p className="mt-1 text-[11px] leading-relaxed text-amber-800">
                  该事件集用于验证创建、模型调用、Oracle 对齐和图表流程；事件事实可能为占位文本，不能用于评价正式准确率，也不应进入正式 Arena 排名。
                </p>
              </div>
            </div>
          </section>
        )}

        <ProgressCard run={run} progress={progress} unitLabel={strategyType === "event" ? "事件" : "执行单元"} />

        {strategyType === "event" && <BacktestQuestionSummary state={questionResults} onOpen={() => setResultTab("qa")} />}

        <ResultNavigation
          strategyType={strategyType}
          predictionOnly={predictionOnly}
          hasForecast={strategyType !== "event" && hasQuantForecast}
          active={resultTab}
          onChange={setResultTab}
          eventNote={eventAuditNote}
          tradeCount={isArenaSafe ? null : performance?.summary?.n_trades ?? performance?.summary?.trade_count ?? performance?.trades?.length}
          hasQuestionTest={strategyType === "event" && Boolean(questionResults.value?.enabled)}
          arenaSafe={isArenaSafe}
        />

        {resultTab === "financial_overview" && strategyType !== "event" && !predictionOnly && (
          <QuantFinancialOverview
            run={run}
            data={performance}
            loading={performanceLoading}
            error={performanceError}
            arenaSafe={isArenaSafe}
            onOpenCharts={() => setResultTab("performance")}
            onOpenAudit={() => setResultTab("portfolio_audit")}
          />
        )}

        {resultTab === "qa" && strategyType === "event" && <BacktestQuestionResults runId={runId} state={questionResults} />}

        {resultTab === "return_forecast" && (
          performanceLoading ? <p className="px-5 py-6 text-sm text-mute">正在读取收益率预测…</p> : performanceError ? <p role="alert" className="px-5 py-6 text-sm text-rise">{performanceError}</p> : strategyType === "event"
            ? <EventReturnForecastPanel metric={metrics?.metrics?.return_forecast ?? run.metrics?.return_forecast} summary={performance?.return_forecast} rows={performance?.event_return_forecasts} horizon={evaluationHorizon} arenaSafe={isArenaSafe} />
            : <QuantReturnForecastPanel summary={performance?.return_forecast} forecasts={performance?.return_forecasts} predictionOnly={performance?.prediction_only} financialAnalysis={performance?.financial_analysis} performanceSummary={performance?.summary} />
        )}

        {resultTab === "performance" && (
          <BacktestPerformanceDashboard
            runId={runId}
            run={run}
            strategyType={strategyType}
            data={performance}
            loading={performanceLoading}
            error={performanceError}
            arenaSafe={isArenaSafe}
          />
        )}

        {resultTab === "event_audit" && strategyType === "event" && (
          <section className="space-y-4">
            {metrics ? (
              <MetricsGrid
                m={metrics}
                horizon={evaluationHorizon}
                catalogItems={!catalogFilterActive ? auditCatalogItems : []}
                runInvalidOutputCount={run.invalid_output_count}
                runInsufficientDataCount={run.insufficient_data_count}
                directionMode={String(runConfig.direction_mode ?? metrics.direction_mode ?? "")}
              />
            ) : (
              <div className="rounded-card border border-edge bg-card px-5 py-4 text-[12px] text-mute shadow-card">
                暂无事件判断正确率。需要已冻结 Oracle labels 与完成的事件决策；页面不会用交易胜率代替方向准确率。
              </div>
            )}
          </section>
        )}

        {resultTab === "portfolio_audit" && strategyType !== "event" && (
          <PortfolioAudit run={run} performance={performance} arenaSafe={isArenaSafe} />
        )}

        {/* ===================================== 实时流 / 事件流 时间线 ===================================== */}
        {run?.status === "running" && resultTab !== "qa" && (
          <section className="rounded-card border border-edge bg-card shadow-card">
            {run.runner === "team_full" && (
              <TeamFullActivityCard
                activity={teamFullActivity ?? run.activity ?? null}
                startedAt={run.started_at}
                nowMs={activityClock}
              />
            )}
            <div className="flex items-center justify-between border-b border-edge px-4 py-2.5">
              <h3 className="flex items-center gap-1.5 text-[13px] font-semibold text-ink">
                <Loader2 size={13} className="animate-spin text-brand" />
                实时事件流
              </h3>
              <span className="font-mono text-[11px] text-faint">{sseEvents.length} 条事件</span>
            </div>
            <div className="max-h-[220px] overflow-y-auto px-4 py-2">
              {sseEvents.length === 0 ? (
                <div className="py-4 text-center text-[12px] text-faint">等待事件到达...</div>
              ) : (
                <ul className="space-y-1.5">
                  {sseEvents.map((ev, i) => (
                    <SSEEventRow key={i} ev={ev} />
                  ))}
                </ul>
              )}
            </div>
          </section>
        )}

        {/* 事件模型专属：仅展示本 Run 执行范围/实际预测，并支持逐事件懒加载详情。 */}
        {resultTab === "event_audit" && strategyType === "event" && (isArenaSafe ? (
          <section className="rounded-card border border-jade/20 bg-jade-soft/30 px-5 py-5 shadow-card">
            <div className="flex items-start gap-3">
              <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl border border-jade/20 bg-card text-jade">
                <LockKeyhole size={16} />
              </span>
              <div>
                <h3 className="font-serif text-[14px] font-semibold text-ink">Arena 安全摘要已启用</h3>
                <p className="mt-1 max-w-3xl text-[11.5px] leading-relaxed text-mute">
                  此 Run 只展示聚合指标与评测协议。事件级标的、方向、置信度、理由、Prompt 和 Agent 轨迹已由服务端隐藏，不会通过详情或 Arena 对决泄露。
                </p>
              </div>
            </div>
          </section>
        ) : (
        <section className="rounded-card border border-edge bg-card shadow-card">
          {/* 表格头 + 筛选 */}
          <div className="flex flex-wrap items-center justify-between gap-3 border-b border-edge px-4 py-3">
            <h3 className="flex items-center gap-1.5 text-[13px] font-semibold text-ink">
              <Filter size={13} className="text-mute" />
              {strategyType === "event" ? "事件目录" : "决策与评测样本"}
              <span className="font-mono text-[11px] font-normal text-faint">
                {eventCatalogSummary}
              </span>
            </h3>
            <div className="flex flex-wrap items-center gap-2">
              <label className="inline-flex items-center gap-1 text-[11.5px] text-mute">
                <input
                  type="checkbox"
                  checked={fOnlyIncorrect}
                  onChange={(e) => setFOnlyIncorrect(e.target.checked)}
                  className="h-3.5 w-3.5 accent-brand"
                />
                仅显示方向错误
              </label>
              <FilterSelect
                placeholder="状态"
                value={fStatus}
                options={[
                  { v: "pending", l: "待执行" },
                  { v: "processing", l: "执行中" },
                  { v: "done", l: "已完成" },
                ]}
                onChange={(v) => setFStatus(v as BTEventStatus | "")}
              />
              <FilterSelect
                placeholder="市场"
                value={fMarket}
                options={["CN", "US", "HK", "FUTURES", "CRYPTO", "FX", "MACRO"]}
                onChange={setFMarket}
              />
              <FilterSelect
                placeholder="事件类型"
                value={fEventType}
                options={["财报", "并购", "股东增减持", "宏观", "评级调整", "行业政策"]}
                onChange={setFEventType}
              />
            </div>
          </div>

          {/* 表格 */}
          <div className="overflow-x-auto">
            <table className="w-full text-[12.5px]">
              <thead>
                <tr className="border-b border-edge bg-edge/20 text-left text-[11px] uppercase tracking-wider text-faint">
                  <th className="w-[38px] px-2 py-2 font-medium"></th>
                  <th className="px-2 py-2 font-medium">#</th>
                  <th className="px-4 py-2 font-medium">状态</th>
                  <th className="px-4 py-2 font-medium">标的</th>
                  <th className="px-4 py-2 font-medium">市场</th>
                  <th className="px-4 py-2 font-medium">类型</th>
                  <th className="px-4 py-2 font-medium">预测方向</th>
                  <th className="px-4 py-2 font-medium">置信度</th>
                  <th className="px-4 py-2 font-medium">Oracle {evaluationHorizonLabel} 标签</th>
                  <th className="px-4 py-2 font-medium">Oracle CAR {evaluationHorizonLabel}</th>
                  <th className="px-4 py-2 font-medium">{evaluationHorizonLabel} 对照结果</th>
                  <th className="px-4 py-2 font-medium">完成时间</th>
                </tr>
              </thead>
              <tbody>
                {catalogLoading && auditCatalogItems.length === 0 ? (
                  <tr><td colSpan={12} className="px-4 py-10 text-center text-mute">
                    <Loader2 size={16} className="mx-auto mb-2 animate-spin text-brand" />
                    加载事件目录...
                  </td></tr>
                ) : catalogError ? (
                  <tr><td colSpan={12} className="px-4 py-10 text-center">
                    <div className="inline-flex flex-col items-center gap-2 text-[12px] text-rose">
                      <XCircle size={16} />
                      <div>事件目录加载失败：<span className="font-mono">{catalogError}</span></div>
                      <button
                        onClick={() => void loadCatalog()}
                        className="mt-1 rounded-md border border-edge bg-card px-3 py-1 text-[11px] text-mute transition hover:bg-edge/40 hover:text-ink"
                      >
                        重新加载
                      </button>
                    </div>
                  </td></tr>
                ) : auditCatalogItems.length === 0 ? (
                  <tr><td colSpan={12} className="px-4 py-10 text-center text-mute text-[12px]">
                    {!catalogLoadedOnce ? "事件目录加载中…" :
                      (catalogFilterActive
                        ? "暂无匹配的已执行结果。尝试调整筛选条件。"
                        : runIsTerminal
                        ? "本 Run 未生成可审计的事件判断结果；数据集中的未执行事件不会作为回测结果展示。"
                        : "本 Run 的执行范围内暂无事件。")}
                  </td></tr>
                ) : (
                  auditCatalogItems.flatMap((it, idx) => {
                    const rowNum = idx + 1;
                    const isOpen = expandedEventId === it.event_id;
                    return [
                      <CatalogRow
                        key={"row-" + it.event_id}
                        item={it}
                        rowIdx={rowNum}
                        isOpen={isOpen}
                        onToggle={() => setExpandedEventId((cur) => (cur === it.event_id ? null : it.event_id))}
                      />,
                      isOpen ? (
                        <tr key={"exp-" + it.event_id}>
                          <td colSpan={12} className="border-b border-edge/60 bg-edge/10">
                            <CaseDetailPanel
                              loading={predDetailLoading}
                              detail={predDetail}
                              activeTab={predDetailTab}
                              onTabChange={setPredDetailTab}
                              itemStatus={it.status}
                              catalogItem={it}
                              runId={runId}
                              allowSensitive={!isArenaSafe}
                            />
                          </td>
                        </tr>
                      ) : null,
                    ];
                  })
                )}
              </tbody>
            </table>
          </div>
        </section>
        ))}

        {run?.error_msg && run.status === "failed" && failureHelp && (
          <section role="alert" className="rounded-card border border-rise/30 bg-rise/5 p-4 text-[12px] shadow-card">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div className="flex min-w-0 items-start gap-3">
                <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-rise/10 text-rise">
                  <XCircle size={16} />
                </span>
                <div className="min-w-0">
                  <h3 className="text-[13px] font-semibold text-rise">{failureHelp.title}</h3>
                  <p className="mt-1 leading-relaxed text-ink">{failureHelp.summary}</p>
                  <p className="mt-2 leading-relaxed text-mute">
                    <span className="font-semibold text-ink">建议：</span>{failureHelp.action}
                  </p>
                </div>
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <button
                  type="button"
                  onClick={() => void doStart()}
                  disabled={acting}
                  className="inline-flex items-center gap-1.5 rounded-lg border border-rise/25 bg-card px-3 py-1.5 text-[11.5px] font-semibold text-rise transition hover:border-rise/40 hover:bg-rise/5 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {acting ? <Loader2 size={12.5} className="animate-spin" /> : <RefreshCw size={12.5} />}
                  重新运行
                </button>
                <span className="text-[9.5px] text-faint">从已完成进度继续</span>
              </div>
            </div>
            <details className="mt-3 border-t border-rise/15 pt-3">
              <summary className="w-fit cursor-pointer select-none text-[10.5px] font-medium text-mute transition hover:text-ink">
                查看原始技术详情
              </summary>
              <pre className="mt-2 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-edge bg-card px-3 py-2.5 font-mono text-[10.5px] leading-relaxed text-mute">
                {run.error_msg}
              </pre>
            </details>
          </section>
        )}

        {/* 底部留白 */}
        <div className="h-6" />
      </div>
    </div>
  );
}
