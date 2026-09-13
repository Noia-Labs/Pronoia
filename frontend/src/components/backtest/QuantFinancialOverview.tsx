import {
  Activity,
  ArrowRight,
  BarChart3,
  CandlestickChart,
  Database,
  Info,
  Layers3,
  Loader2,
  ReceiptText,
  ShieldCheck,
  TrendingDown,
} from "lucide-react";
import type { BTPerformanceResponse, BTRun } from "../../types";
import { cls } from "../../utils";

type UnknownRecord = Record<string, unknown>;

function record(value: unknown): UnknownRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
}

function finite(value: unknown): number | null {
  const number = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(number) ? number : null;
}

function firstNumber(sources: UnknownRecord[], keys: string[]): number | null {
  for (const source of sources) {
    for (const key of keys) {
      const value = finite(source[key]);
      if (value != null) return value;
    }
  }
  return null;
}

function firstText(sources: UnknownRecord[], keys: string[]): string | null {
  for (const source of sources) {
    for (const key of keys) {
      const value = source[key];
      if (value == null || value === "") continue;
      if (["string", "number", "boolean"].includes(typeof value)) return String(value);
    }
  }
  return null;
}

function firstCurrency(sources: UnknownRecord[], keys: string[]): string | null {
  for (const source of sources) {
    for (const key of keys) {
      if (typeof source[key] !== "string") continue;
      const code = String(source[key]).trim().toUpperCase();
      if (!/^[A-Z]{3}$/.test(code) || code === "UNSPECIFIED") continue;
      try {
        new Intl.NumberFormat("zh-CN", { style: "currency", currency: code }).format(0);
        return code;
      } catch {
        // Continue to lower-priority protocol fields.
      }
    }
  }
  return null;
}

function count(value: number | null): string {
  return value == null ? "未计算" : Math.max(0, Math.round(value)).toLocaleString("zh-CN");
}

function pct(value: number | null, signed = true): string {
  if (value == null) return "未计算";
  const prefix = signed && value > 0 ? "+" : "";
  return `${prefix}${(value * 100).toFixed(2)}%`;
}

function ratio(value: number | null): string {
  return value == null ? "未计算" : value.toFixed(2);
}

function money(value: number | null, currency: string | null): string {
  if (value == null) return "未计算";
  if (!currency) return value.toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  try {
    return new Intl.NumberFormat("zh-CN", { style: "currency", currency, maximumFractionDigits: 2 }).format(value);
  } catch {
    return `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })} ${currency}`;
  }
}

function weight(value: number | null): string {
  return value == null ? "未计算" : `${(value * 100).toFixed(2)}%`;
}

function multiple(value: number | null): string {
  return value == null ? "未计算" : `${value.toLocaleString("zh-CN", { maximumFractionDigits: 2 })}×`;
}

const MARKET_TIME_ZONES: Record<string, string> = {
  CN: "Asia/Shanghai",
  HK: "Asia/Hong_Kong",
  FUTURES: "Asia/Shanghai",
  US: "America/New_York",
  CRYPTO: "UTC",
  FX: "UTC",
};

function dateTime(value: unknown, market?: string): string {
  const text = String(value ?? "").trim();
  if (!text) return "未返回";
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
  const hasExplicitZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text);
  const marketZone = MARKET_TIME_ZONES[String(market ?? "").trim().toUpperCase()];
  if (hasExplicitZone && marketZone) {
    const instant = new Date(text);
    if (!Number.isNaN(instant.getTime())) {
      const parts = new Intl.DateTimeFormat("en-CA", {
        timeZone: marketZone,
        year: "numeric", month: "2-digit", day: "2-digit",
        hour: "2-digit", minute: "2-digit", hourCycle: "h23",
      }).formatToParts(instant);
      const part = (type: Intl.DateTimeFormatPartTypes) => parts.find((item) => item.type === type)?.value ?? "";
      return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")} ${marketZone}`;
    }
  }
  const matched = text.match(/^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?$/i);
  if (matched) return `${matched[1]} ${matched[2]}${matched[3] ? ` ${matched[3].toUpperCase() === "Z" ? "UTC" : matched[3]}` : ""}`;
  return text;
}

function samplingTotal(data: BTPerformanceResponse | null, key: string, fallback: number): number {
  const series = record(record(record(data?.response_sampling).series)[key]);
  return firstNumber([series], ["total_count"]) ?? fallback;
}

function lastPosition(data: BTPerformanceResponse | null): UnknownRecord {
  const rows = data?.positions ?? data?.holdings ?? [];
  return record(rows[rows.length - 1]);
}

function curveBoundary(data: BTPerformanceResponse | null, edge: "first" | "last"): string | null {
  const rows = data?.equity_curve ?? [];
  const point = rows[edge === "first" ? 0 : rows.length - 1];
  if (!point) return null;
  const value = point.timestamp ?? point.date ?? point.event_time;
  return value == null ? null : String(value);
}

function MetricTile({
  label,
  value,
  note,
  tone = "text-ink",
}: {
  label: string;
  value: string;
  note: string;
  tone?: string;
}) {
  const unavailable = value === "未计算";
  return (
    <div className="min-w-0 rounded-xl border border-edge bg-paper px-3.5 py-3">
      <div className="text-[9px] font-semibold uppercase tracking-[0.1em] text-faint">{label}</div>
      <div className={cls("mt-1.5 truncate font-mono text-[18px] font-semibold tabular-nums", unavailable ? "text-faint" : tone)} title={value}>{value}</div>
      <div className="mt-1 min-h-[24px] text-[9px] leading-relaxed text-mute">{unavailable ? `未计算 · ${note}` : note}</div>
    </div>
  );
}

function Fact({ label, value, title }: { label: string; value: string; title?: string }) {
  return (
    <div className="min-w-0 rounded-lg border border-edge/80 bg-card px-3 py-2.5">
      <div className="text-[8.5px] uppercase tracking-wider text-faint">{label}</div>
      <div className="mt-1 truncate text-[10.5px] font-medium text-ink" title={title ?? value}>{value}</div>
    </div>
  );
}

function SectionTitle({ icon, eyebrow, title, note }: { icon: React.ReactNode; eyebrow: string; title: string; note: string }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-2 border-b border-edge px-4 py-3">
      <div className="flex items-start gap-2.5">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-brand-soft/55 text-brand">{icon}</span>
        <div><div className="text-[8.5px] font-semibold uppercase tracking-[0.16em] text-faint">{eyebrow}</div><h3 className="mt-0.5 font-serif text-[13px] font-semibold text-ink">{title}</h3></div>
      </div>
      <p className="max-w-xl text-right text-[9px] leading-relaxed text-mute">{note}</p>
    </div>
  );
}

export default function QuantFinancialOverview({
  run,
  data,
  loading,
  error,
  arenaSafe,
  onOpenCharts,
  onOpenAudit,
}: {
  run: BTRun;
  data: BTPerformanceResponse | null;
  loading: boolean;
  error: string | null;
  arenaSafe: boolean;
  onOpenCharts: () => void;
  onOpenAudit: () => void;
}) {
  if (loading && !data) {
    return (
      <section className="rounded-card border border-edge bg-card px-5 py-10 shadow-card">
        <div className="flex items-center justify-center gap-2 text-[12px] text-mute"><Loader2 size={14} className="animate-spin text-brand" /> 正在读取金融概览…</div>
      </section>
    );
  }

  const analysis = record(data?.financial_analysis);
  const response = record(data);
  const returns = record(analysis.returns);
  const risk = record(analysis.risk);
  const benchmark = record(analysis.benchmark);
  const trading = record(analysis.trading);
  const exposure = record(analysis.exposure);
  const costs = record(analysis.costs);
  const period = record(analysis.period);
  const units = record(analysis.units);
  const methodology = record(analysis.methodology);
  const summary = record(data?.summary);
  const dataset = record(data?.dataset);
  const datasetMetadata = record(dataset.metadata);
  const basis = { ...record(data?.data_basis), ...dataset };
  const provenance = record(data?.data_provenance);
  const runConfig = record(run.config);
  const protocol = record(data?.effective_protocol);
  const configExecution = record(runConfig.execution_spec);
  const runExecution = record(run.execution_spec);
  const requested = {
    ...configExecution,
    ...runExecution,
    ...record(configExecution.requested),
    ...record(runExecution.requested),
    ...record(protocol.requested),
  };
  const applied = {
    ...configExecution,
    ...runExecution,
    ...record(configExecution.applied),
    ...record(runExecution.applied),
    ...record(protocol.applied),
  };
  const datasetSnapshot = record(runConfig.dataset_snapshot);
  const snapshotCoverage = record(datasetSnapshot.coverage);
  const position = lastPosition(data);
  const currency = firstCurrency([units, applied, response, analysis], ["money", "currency"]);
  const moneyBasis = currency ?? "资金单位未声明";

  const totalReturn = firstNumber([returns, summary], ["total_return", "net_total_return"]);
  const annualizedReturn = firstNumber([returns, summary], ["annualized_return", "annual_return"]);
  const grossReturn = firstNumber([returns, summary], ["gross_total_return", "gross_return"]);
  const benchmarkReturn = firstNumber([benchmark, returns, summary], ["benchmark_total_return", "benchmark_return"]);
  const assetReturn = firstNumber([benchmark, returns, summary], ["asset_total_return", "asset_return", "buy_hold_return"]);
  const excessReturn = firstNumber([benchmark, returns, summary], ["excess_total_return", "excess_return", "active_return"]);
  const averageBarReturn = firstNumber([returns], ["average_bar_return"]);
  const bestBarReturn = firstNumber([returns], ["best_bar_return"]);
  const worstBarReturn = firstNumber([returns], ["worst_bar_return"]);
  const initialCapital = firstNumber([returns, summary, analysis], ["initial_value", "initial_capital"])
    ?? firstNumber([response, applied], ["initial_value", "initial_capital"]);
  const finalValue = firstNumber([returns, summary], ["final_equity", "final_value"]);

  const maxDrawdown = firstNumber([risk, summary], ["max_drawdown"]);
  const volatility = firstNumber([risk, summary], ["annualized_volatility", "volatility_annualized"]);
  const downsideDeviation = firstNumber([risk], ["annualized_downside_deviation"]);
  const sharpe = firstNumber([risk, summary], ["sharpe_ratio", "sharpe_proxy"]);
  const sortino = firstNumber([risk], ["sortino_ratio"]);
  const calmar = firstNumber([risk, summary], ["calmar_ratio"]);
  const profitFactor = arenaSafe ? null : firstNumber([risk, trading, summary], ["active_bar_profit_factor", "profit_factor"]);
  const valueAtRisk = firstNumber([risk], ["historical_var_95_one_bar", "var_95_per_bar", "var_95", "value_at_risk_95", "historical_var_95"]);
  const conditionalValueAtRisk = firstNumber([risk], ["historical_cvar_95_one_bar", "cvar_95_per_bar", "cvar_95", "expected_shortfall_95", "conditional_value_at_risk_95"]);
  const currentDrawdown = firstNumber([risk], ["current_drawdown"]);
  const peakToTroughBars = firstNumber([risk], ["peak_to_trough_bars"]);
  const recoveryBars = firstNumber([risk], ["recovery_bars"]);
  const longestUnderwaterBars = firstNumber([risk], ["longest_underwater_bars"]);
  const positiveStreak = firstNumber([risk], ["max_consecutive_positive_bars"]);
  const negativeStreak = firstNumber([risk], ["max_consecutive_negative_bars"]);
  const peakTimestamp = firstText([risk], ["peak_timestamp"]);
  const troughTimestamp = firstText([risk], ["trough_timestamp"]);
  const recoveryTimestamp = firstText([risk], ["recovery_timestamp"]);

  const benchmarkCorrelation = firstNumber([benchmark], ["correlation"]);
  const benchmarkBeta = firstNumber([benchmark], ["beta"]);
  const benchmarkAlpha = firstNumber([benchmark], ["annualized_alpha_rf0"]);
  const trackingError = firstNumber([benchmark], ["annualized_tracking_error"]);
  const informationRatio = firstNumber([benchmark], ["information_ratio"]);
  const alignedReturnCount = firstNumber([benchmark], ["aligned_return_count"]);
  const returnObservationCount = firstNumber([period], ["return_observation_count"]);
  const annualizationPeriods = firstNumber([period, methodology, risk, summary, applied], ["annualization_periods", "annualization_factor", "periods_per_year"]);
  const estimatedYears = firstNumber([period], ["estimated_years"]);

  const tradeCount = firstNumber([trading, summary], ["position_change_count", "trade_count", "n_trades"]);
  const buyCount = firstNumber([trading], ["buy_count"]);
  const sellCount = firstNumber([trading], ["sell_count"]);
  const entryCount = firstNumber([trading], ["entry_count"]);
  const exitCount = firstNumber([trading], ["exit_count"]);
  const reversalCount = firstNumber([trading], ["reversal_count"]);
  const activeBarCount = firstNumber([trading], ["active_bar_count"]);
  const winRate = firstNumber([trading, summary], ["active_bar_win_rate", "win_rate"]);
  const turnover = firstNumber([trading, summary], ["total_turnover", "turnover"]);
  const avgTurnover = firstNumber([trading], ["average_turnover_per_change", "average_turnover", "avg_turnover_per_trade"]);
  const tradedNotional = firstNumber([trading], ["total_traded_notional"]);
  const averageTradedNotional = firstNumber([trading], ["average_traded_notional"]);
  const signalTotal = samplingTotal(data, "signals", data?.signals?.length ?? 0);
  const positionTotal = samplingTotal(data, "positions", (data?.positions ?? data?.holdings ?? []).length);
  const barTotal = firstNumber([period, summary, dataset], ["bar_count", "n_bars"])
    ?? samplingTotal(data, "bars", data?.bars?.length ?? run.total_events ?? 0);

  const latestWeight = firstNumber([exposure, position], ["current_exposure", "ending_weight", "latest_weight", "weight"]);
  const targetWeight = firstNumber([exposure, position], ["ending_target_weight", "latest_target_weight", "target_weight"]);
  const averageSignedExposure = firstNumber([exposure], ["average_signed_exposure"]);
  const averageExposure = firstNumber([exposure], ["average_absolute_exposure", "average_abs_weight", "average_exposure", "avg_exposure"]);
  const maxExposure = firstNumber([exposure], ["maximum_absolute_exposure", "max_abs_weight", "maximum_exposure", "max_exposure"]);
  const timeInMarket = firstNumber([exposure], ["time_in_market_ratio"]);
  const longExposureRatio = firstNumber([exposure], ["long_exposure_ratio"]);
  const shortExposureRatio = firstNumber([exposure], ["short_exposure_ratio"]);
  const flatRatio = firstNumber([exposure], ["flat_ratio"]);
  const longestInMarketBars = firstNumber([exposure], ["longest_in_market_bars"]);
  const latestEquity = firstNumber([exposure, position], ["ending_equity", "latest_equity", "equity"]);
  const latestMarketValue = firstNumber([exposure, position], ["ending_market_value", "latest_market_value", "market_value"]);

  const totalCost = firstNumber([costs, summary], ["total_cost", "total_cost_amount_proxy"]);
  const commission = firstNumber([costs, summary], ["total_commission", "commission"]);
  const slippage = firstNumber([costs, summary], ["total_slippage", "slippage"]);
  const stampDuty = firstNumber([costs, summary], ["total_stamp_duty", "stamp_duty"]);
  const otherCost = firstNumber([costs, summary], ["total_other_cost", "other_cost"]);
  const costDrag = firstNumber([costs, summary], ["cost_drag_ratio_to_initial_capital", "cost_drag_ratio", "total_cost_ratio"])
    ?? (totalCost != null && initialCapital != null && initialCapital > 0 ? totalCost / initialCapital : null);
  const costToNotional = firstNumber([costs], ["cost_to_traded_notional_ratio"]);
  const averageCost = firstNumber([costs], ["average_cost_per_change"]);

  const start = arenaSafe ? null : firstText([period], ["start", "start_at", "start_date"])
    ?? firstText([datasetMetadata, applied], ["effective_start", "start_at"])
    ?? curveBoundary(data, "first")
    ?? firstText([dataset], ["start_at", "start_date"]);
  const end = arenaSafe ? null : firstText([period], ["end", "end_at", "end_date"])
    ?? firstText([datasetMetadata, applied], ["effective_end", "end_at"])
    ?? curveBoundary(data, "last")
    ?? firstText([dataset], ["end_at", "end_date"]);
  const requestedStart = arenaSafe ? null : firstText([requested, datasetMetadata], ["start_date", "requested_start", "start_at", "start"]);
  const requestedEnd = arenaSafe ? null : firstText([requested, datasetMetadata], ["end_date", "requested_end", "end_at", "end"]);
  const availableStart = arenaSafe ? null : firstText([datasetMetadata, snapshotCoverage], ["available_start", "start_at", "start"]);
  const availableEnd = arenaSafe ? null : firstText([datasetMetadata, snapshotCoverage], ["available_end", "end_at", "end"]);
  const clippedValue = datasetMetadata.window_clipped ?? applied.window_clipped;
  const rangeWasClipped = clippedValue === true || String(clippedValue ?? "").toLowerCase() === "true";
  const symbol = firstText([period, dataset, basis], ["symbol", "asset", "ticker"]) ?? "未声明";
  const market = firstText([period, dataset, basis], ["market"]) ?? "未声明";
  const frequency = firstText([period, dataset, basis, applied], ["frequency", "bar_frequency"]) ?? "未声明";
  const provider = firstText([provenance, dataset, basis], ["provider", "source", "source_type"]) ?? "未声明";
  const datasetVersion = firstText([period, dataset, analysis, response], ["dataset_version", "version"])
    ?? data?.dataset_version
    ?? run.dataset_version
    ?? "未返回";
  const riskBasis = firstText([risk, methodology, units], ["var_basis", "tail_risk_basis", "risk_return_basis", "risk_horizon", "var_horizon", "period_return"])
    ?? `单根 ${frequency} bar 收益`;
  const riskBasisLabel = /historical one-bar return distribution/i.test(riskBasis)
    ? `单根 ${frequency} bar 的历史收益分布`
    : riskBasis;
  const metricsBasis = firstText([analysis], ["metrics_basis"]) ?? "本次 Run 的完整冻结结果";
  const resultUnavailable = !data || data.status === "unavailable";
  const winBasisRaw = firstText([methodology, trading, summary], ["win_rate_basis"]);
  const winBasis = winBasisRaw && /active[ _-]?(bar|period)|positive net-return share/i.test(winBasisRaw)
    ? "活跃 bar 中净收益大于 0 的占比；不是已完成交易胜率"
    : winBasisRaw ?? "未声明";
  const positionLabel = arenaSafe ? "Arena-safe 已隐藏" : `${symbol} · ${dateTime(position.timestamp ?? position.date ?? end, market)}`;
  const annualizationLabel = annualizationPeriods == null
    ? "后端年化因子未返回"
    : `${Math.round(annualizationPeriods).toLocaleString("zh-CN")} bar/年`;
  const benchmarkBasis = firstText([applied, benchmark], ["benchmark", "benchmark_basis", "basis"]);
  const benchmarkDescription = benchmarkBasis === "dataset_asset_buy_hold"
    ? "本次冻结标的的收盘价首尾买入持有，不计策略交易成本；不是外部 ETF 或含分红总回报指数"
    : benchmarkBasis
    ? `后端声明：${benchmarkBasis}`
    : "后端未声明基准构造方式";
  const adjustment = firstText([datasetMetadata, dataset, basis], ["adjustment", "price_adjustment"]) ?? "未声明";

  const positiveTone = (value: number | null) => value == null ? "text-ink" : value >= 0 ? "text-rise" : "text-fall";
  const returnTiles = [
    { label: "策略总收益", value: pct(totalReturn), note: "期初至期末净值变动", tone: positiveTone(totalReturn) },
    { label: "年化收益", value: pct(annualizedReturn), note: `按后端收益口径 · ${annualizationLabel}`, tone: positiveTone(annualizedReturn) },
    { label: "期末权益", value: money(finalValue ?? (arenaSafe ? null : latestEquity), currency), note: initialCapital == null ? "期初资金未返回" : `期初 ${money(initialCapital, currency)}`, tone: positiveTone(totalReturn) },
    { label: "基准收益", value: pct(benchmarkReturn), note: "冻结标的收盘价买入持有", tone: positiveTone(benchmarkReturn) },
    { label: "超额收益", value: pct(excessReturn), note: "策略收益减声明基准", tone: positiveTone(excessReturn) },
    { label: "平均单 bar 收益", value: pct(averageBarReturn), note: `每根 ${frequency} bar 的算术均值`, tone: positiveTone(averageBarReturn) },
    { label: "最佳单 bar", value: pct(bestBarReturn), note: `全窗口最佳 ${frequency} bar`, tone: positiveTone(bestBarReturn) },
    { label: "最差单 bar", value: pct(worstBarReturn), note: `全窗口最差 ${frequency} bar`, tone: "text-fall" },
    ...(grossReturn != null ? [{ label: "毛收益", value: pct(grossReturn), note: "扣除交易成本前", tone: positiveTone(grossReturn) }] : []),
    ...(assetReturn != null ? [{ label: "标的买入持有", value: pct(assetReturn), note: "同数据窗口资产参照", tone: positiveTone(assetReturn) }] : []),
  ];
  const riskTiles = [
    { label: "最大回撤", value: pct(maxDrawdown == null ? null : -Math.abs(maxDrawdown), false), note: "历史净值峰值至谷值", tone: "text-fall" },
    { label: "当前回撤", value: pct(currentDrawdown == null ? null : -Math.abs(currentDrawdown), false), note: "期末相对最近净值高点", tone: "text-fall" },
    { label: "年化波动率", value: pct(volatility, false), note: "收益波动的年化统计", tone: "text-ink" },
    { label: "年化下行波动", value: pct(downsideDeviation, false), note: "只计下行收益偏差", tone: "text-ink" },
    { label: "Sharpe", value: ratio(sharpe), note: `rf=0；全体 bar；总体标准差；${annualizationLabel}`, tone: sharpe != null && sharpe >= 1 ? "text-jade" : "text-ink" },
    { label: "Sortino", value: ratio(sortino), note: "年化收益 / 下行波动", tone: sortino != null && sortino >= 1 ? "text-jade" : "text-ink" },
    { label: "Calmar", value: ratio(calmar), note: "年化收益 / 最大回撤", tone: calmar != null && calmar >= 1 ? "text-jade" : "text-ink" },
    { label: "活跃 bar 盈亏比", value: ratio(profitFactor), note: "活跃 bar 正收益总和 / 负收益绝对值；非闭合交易", tone: profitFactor != null && profitFactor >= 1 ? "text-jade" : "text-ink" },
    { label: "历史 VaR 95%（单 bar）", value: pct(valueAtRisk, false), note: `左尾第 5 百分位损失 · ${riskBasisLabel}`, tone: "text-fall" },
    { label: "历史 CVaR 95%（单 bar）", value: pct(conditionalValueAtRisk, false), note: `最差 5% bar 的平均损失 · ${riskBasisLabel}`, tone: "text-fall" },
    { label: "最长水下期", value: count(longestUnderwaterBars), note: `${frequency} bars`, tone: "text-ink" },
    { label: "最长连续上涨", value: count(positiveStreak), note: `${frequency} bars`, tone: "text-rise" },
    { label: "最长连续下跌", value: count(negativeStreak), note: `${frequency} bars`, tone: "text-fall" },
  ];
  const benchmarkTiles = [
    { label: "基准收益", value: pct(benchmarkReturn), note: "冻结标的收盘价首尾买入持有", tone: positiveTone(benchmarkReturn) },
    { label: "相关系数", value: ratio(benchmarkCorrelation), note: "策略与基准单 bar 收益相关性", tone: "text-ink" },
    { label: "Beta", value: ratio(benchmarkBeta), note: "对基准收益变动的敏感度", tone: "text-ink" },
    { label: "年化 Alpha (rf=0)", value: pct(benchmarkAlpha), note: "以无风险利率 0 估算", tone: positiveTone(benchmarkAlpha) },
    { label: "年化跟踪误差", value: pct(trackingError, false), note: "策略减基准收益的波动", tone: "text-ink" },
    { label: "信息比率", value: ratio(informationRatio), note: "年化超额收益 / 跟踪误差", tone: informationRatio != null && informationRatio >= 1 ? "text-jade" : "text-ink" },
    { label: "对齐收益样本", value: count(alignedReturnCount), note: `${frequency} bars`, tone: "text-ink" },
  ];
  const diagnostics: string[] = [];
  if (maxDrawdown != null && Math.abs(maxDrawdown) > 0.2) diagnostics.push(`最大回撤 ${pct(-Math.abs(maxDrawdown), false)}，历史峰谷跌幅超过 20%。`);
  if (costDrag != null && costDrag > 0.1) diagnostics.push(`累计成本相当于期初资金的 ${pct(costDrag, false)}，结果对换手和成本口径较敏感。`);
  if (excessReturn != null && excessReturn < 0) diagnostics.push(`本窗口超额收益为 ${pct(excessReturn)}，策略跑输所声明基准。`);
  if (diagnostics.length === 0) diagnostics.push("当前已计算指标未触发高成本（>10% 期初资金）、高回撤（>20%）或负超额收益提示；这只是历史结果描述。 ");

  return (
    <section className="space-y-4" aria-label="量化金融概览">
      <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <div className="flex flex-wrap items-start justify-between gap-3 border-b border-edge bg-[#F6F4EF] px-4 py-3.5">
          <div className="flex items-start gap-3">
            <span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-brand-soft/60 text-brand"><Activity size={16} /></span>
            <div>
              <div className="text-[9px] font-semibold uppercase tracking-[0.18em] text-faint">Quant financial overview</div>
              <h2 className="mt-0.5 font-serif text-[15px] font-semibold text-ink">量化金融概览</h2>
              <p className="mt-1 text-[10px] leading-relaxed text-mute">{arenaSafe ? "收益、风险、基准与成本来自本次聚合 performance 结果；交易时点和敞口已按 Arena-safe 隐藏。" : "收益、风险、基准、模拟交易、敞口与成本均来自本次 performance 结果；缺失项明确标为“未计算”。"}</p>
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <button type="button" onClick={onOpenCharts} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[10.5px] font-semibold text-mute transition hover:border-edgeDark hover:text-ink"><CandlestickChart size={12} /> 图表与行情 <ArrowRight size={11} /></button>
            {!arenaSafe && <button type="button" onClick={onOpenAudit} className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-3 py-2 text-[10.5px] font-semibold text-card transition hover:opacity-90"><Layers3 size={12} /> 规则与交易 <ArrowRight size={11} /></button>}
          </div>
        </div>

        {error && <p role="alert" className="border-b border-rise/20 bg-rise/5 px-4 py-2 text-[10px] text-rise">读取 performance 失败：{error}</p>}
        {resultUnavailable && (
          <div className="flex items-start gap-2 border-b border-amber-200 bg-amber-50 px-4 py-3 text-[10.5px] leading-relaxed text-amber-800"><Info size={13} className="mt-0.5 shrink-0" /><span>本 Run 尚无可计算的投资结果。下方仍展示已冻结的数据与执行口径，不会补造任何金融指标。</span></div>
        )}
        {!arenaSafe && rangeWasClipped && (
          <div className="flex items-start gap-2 border-b border-amber-200 bg-amber-50 px-4 py-3 text-[10.5px] leading-relaxed text-amber-800"><Info size={13} className="mt-0.5 shrink-0" /><span>请求日期部分超出行情覆盖；本次仅执行真实数据交集 {dateTime(start, market)} → {dateTime(end, market)}，未补造区间外 K 线。</span></div>
        )}

        <div className="grid gap-2 p-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-5">
          {returnTiles.map((tile) => <MetricTile key={tile.label} {...tile} />)}
        </div>
      </div>

      <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <SectionTitle icon={<TrendingDown size={14} />} eyebrow="Risk diagnostics" title="风险诊断" note={`指标基于 ${metricsBasis}；VaR / CVaR 使用${riskBasisLabel}，不是持有期或组合终值损失。`} />
        <div className="grid gap-2 p-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7">
          {riskTiles.map((tile) => <MetricTile key={tile.label} {...tile} />)}
        </div>
        {!arenaSafe && <div className="grid gap-2 border-t border-edge bg-paper/40 p-4 sm:grid-cols-2 lg:grid-cols-4">
          <Fact label="最大回撤峰值" value={dateTime(peakTimestamp, market)} />
          <Fact label="最大回撤谷值" value={dateTime(troughTimestamp, market)} />
          <Fact label="净值恢复时点" value={dateTime(recoveryTimestamp, market)} />
          <Fact label="峰→谷 / 恢复耗时" value={`${count(peakToTroughBars)} / ${count(recoveryBars)} ${frequency} bars`} />
        </div>}
      </div>

      <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <SectionTitle icon={<BarChart3 size={14} />} eyebrow="Benchmark diagnostics" title="基准诊断" note={`${benchmarkDescription}。相关性、Beta、Alpha、跟踪误差和信息比率只使用两者同时存在的单 bar 收益。`} />
        <div className="grid gap-2 p-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-7">
          {benchmarkTiles.map((tile) => <MetricTile key={tile.label} {...tile} />)}
        </div>
      </div>

      {!arenaSafe && <div className="grid gap-4 xl:grid-cols-2">
        <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
          <SectionTitle icon={<BarChart3 size={14} />} eyebrow="Trading activity" title="交易活动" note="次数与金额来自 single_asset_next_open 模拟成交汇总，不代表券商实盘成交。" />
          <div className="grid gap-2 p-4 sm:grid-cols-2 lg:grid-cols-3">
            <MetricTile label="仓位变更次数" value={count(tradeCount)} note="目标权重调整成交" />
            <MetricTile label="买入 / 卖出" value={`${count(buyCount)} / ${count(sellCount)}`} note="模拟成交方向计数" />
            <MetricTile label="入场 / 离场" value={`${count(entryCount)} / ${count(exitCount)}`} note="从空仓入场 / 回到空仓" />
            <MetricTile label="反向次数" value={count(reversalCount)} note="多空目标权重直接反转" />
            <MetricTile label="活跃 bars" value={count(activeBarCount)} note={`有敞口的 ${frequency} bars`} />
            <MetricTile label="活跃胜率" value={pct(winRate, false)} note={`统计口径：${winBasis}`} />
            <MetricTile label="累计换手" value={multiple(turnover)} note={avgTurnover == null ? "全窗口绝对权重变动" : `平均每次 ${multiple(avgTurnover)}`} />
            <MetricTile label="累计成交名义额" value={money(tradedNotional, currency)} note={averageTradedNotional == null ? moneyBasis : `平均每次 ${money(averageTradedNotional, currency)} · ${moneyBasis}`} />
            <MetricTile label="信号记录" value={count(signalTotal)} note="全量序列计数" />
          </div>
        </div>

        <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
          <SectionTitle icon={<Layers3 size={14} />} eyebrow="Portfolio exposure" title="组合敞口与期末状态" note="权重来自逐 bar 模拟组合；期末值是净值曲线派生并保留的末端快照。" />
          <div className="grid gap-2 p-4 sm:grid-cols-2 lg:grid-cols-3">
            <MetricTile label="在场时间" value={arenaSafe ? "已隐藏" : pct(timeInMarket, false)} note="非零敞口 bar 占比" />
            <MetricTile label="多头 / 空头" value={arenaSafe ? "已隐藏" : `${pct(longExposureRatio, false)} / ${pct(shortExposureRatio, false)}`} note="多头与空头 bar 占比" />
            <MetricTile label="空仓时间" value={arenaSafe ? "已隐藏" : pct(flatRatio, false)} note="目标权重为零的 bar 占比" />
            <MetricTile label="平均净敞口" value={arenaSafe ? "已隐藏" : weight(averageSignedExposure)} note="带方向的平均目标权重" />
            <MetricTile label="平均 / 最大总敞口" value={arenaSafe ? "已隐藏" : `${weight(averageExposure)} / ${weight(maxExposure)}`} note="绝对目标权重统计" />
            <MetricTile label="最长连续在场" value={arenaSafe ? "已隐藏" : count(longestInMarketBars)} note={`${frequency} bars`} />
            <MetricTile label="模拟期末敞口" value={arenaSafe ? "已隐藏" : weight(latestWeight)} note={positionLabel} />
            <MetricTile label="期末目标权重" value={arenaSafe ? "已隐藏" : weight(targetWeight)} note="冻结策略的目标权重" />
            <MetricTile label="期末持仓市值" value={arenaSafe ? "已隐藏" : money(latestMarketValue, currency)} note={`净值曲线派生末端快照 · ${moneyBasis}`} />
            <MetricTile label="持仓快照" value={count(positionTotal)} note="全量序列计数" />
          </div>
        </div>
      </div>}

      {arenaSafe && (
        <div className="flex items-start gap-2.5 rounded-card border border-jade/20 bg-jade-soft/30 px-4 py-3 text-[10px] leading-relaxed text-mute shadow-card">
          <ShieldCheck size={13} className="mt-0.5 shrink-0 text-jade" />
          <span>Arena-safe 只展示可比较的聚合收益、风险、基准与成本；此概览不展示仓位、交易活动、精确回撤时点或其他可还原策略的信息。</span>
        </div>
      )}

      <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <SectionTitle icon={<ReceiptText size={14} />} eyebrow="Cost attribution" title="交易成本归因" note="金额取模拟成交汇总；“成本 / 期初资金”只是规模比率，不是有成本与无成本收益差。" />
        <div className="grid gap-2 p-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-8">
          <MetricTile label="累计总成本" value={money(totalCost, currency)} note={`佣金、滑点、税费与其他费合计 · ${moneyBasis}`} />
          <MetricTile label="成本 / 期初资金" value={pct(costDrag, false)} note="累计成本除以期初资金" />
          <MetricTile label="成本 / 成交名义额" value={pct(costToNotional, false)} note="累计成本除以累计成交名义额" />
          <MetricTile label="平均每次成本" value={money(averageCost, currency)} note={`每次仓位变更均值 · ${moneyBasis}`} />
          <MetricTile label="手续费" value={money(commission, currency)} note={`应用费率 ${finite(applied.commission_bps ?? applied.fee_bps)?.toFixed(2) ?? "未返回"} bps`} />
          <MetricTile label="滑点" value={money(slippage, currency)} note={`应用费率 ${finite(applied.slippage_bps)?.toFixed(2) ?? "未返回"} bps`} />
          <MetricTile label="卖出印花税" value={money(stampDuty, currency)} note={`应用费率 ${finite(applied.stamp_duty_bps)?.toFixed(2) ?? "未返回"} bps`} />
          <MetricTile label="其他费用" value={money(otherCost, currency)} note={`应用费率 ${finite(applied.other_cost_bps)?.toFixed(2) ?? "未返回"} bps`} />
        </div>
      </div>

      <div className="rounded-card border border-amber-200 bg-amber-50 px-4 py-3 shadow-card">
        <div className="flex items-start gap-2.5">
          <Info size={13} className="mt-0.5 shrink-0 text-amber-700" />
          <div><div className="text-[10.5px] font-semibold text-amber-900">条件诊断</div><ul className="mt-1 space-y-0.5 text-[9.5px] leading-relaxed text-amber-800">{diagnostics.map((item) => <li key={item}>· {item}</li>)}</ul><p className="mt-1 text-[8.5px] text-amber-700">仅描述本次历史回测结果，不构成投资建议。</p></div>
        </div>
      </div>

      <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <SectionTitle icon={<Database size={14} />} eyebrow="Data & methodology" title="数据覆盖与计算口径" note="这里只展示紧凑元数据与计数，不复制或重新请求 K 线、净值、持仓和成交长数组。" />
        <div className="grid gap-2 bg-paper/40 p-4 sm:grid-cols-2 lg:grid-cols-4 xl:grid-cols-6">
          <Fact label="标的 / 市场" value={`${symbol} · ${market}`} />
          <Fact label="请求执行区间" value={arenaSafe ? "Arena-safe 已隐藏" : `${requestedStart ? dateTime(requestedStart, market) : "不限"} → ${requestedEnd ? dateTime(requestedEnd, market) : "不限"}`} />
          <Fact label="完整数据覆盖" value={arenaSafe ? "Arena-safe 已隐藏" : `${dateTime(availableStart, market)} → ${dateTime(availableEnd, market)}`} />
          <Fact label="实际执行交集" value={arenaSafe ? "Arena-safe 已隐藏" : `${dateTime(start, market)} → ${dateTime(end, market)}`} />
          <Fact label="频率 / 有效 K 线" value={`${frequency} · ${count(barTotal)} bars`} />
          <Fact label="收益观测 / 估算年数" value={`${count(returnObservationCount)} / ${estimatedYears == null ? "未计算" : estimatedYears.toFixed(2)}`} />
          <Fact label="年化因子" value={annualizationLabel} />
          <Fact label="数据来源" value={provider} />
          <Fact label="价格复权" value={adjustment} />
          <Fact label="冻结版本" value={datasetVersion} title={datasetVersion} />
          <Fact label="基准构造" value={benchmarkDescription} title={benchmarkDescription} />
          <Fact label="指标基准" value={metricsBasis} />
          <Fact label="资金单位" value={moneyBasis} />
        </div>
        <div className="flex flex-wrap items-center gap-2 border-t border-edge px-4 py-3 text-[9.5px] text-mute">
          <ShieldCheck size={12} className="text-jade" />
          <span>{data?.data_frozen ? "数据已冻结" : "冻结状态未确认"}</span>
          <span>·</span>
          <span>结果性质 {String(data?.result_nature ?? run.result_nature ?? "未返回")}</span>
          {data?.response_sampling?.applied && <><span>·</span><span>页面序列采用冻结结果的有界抽样，以上金融指标仍按全量结果计算</span></>}
          {analysis.schema_version != null && analysis.schema_version !== "" && <><span>·</span><span className="font-mono">financial_analysis {String(analysis.schema_version)}</span></>}
        </div>
      </div>
    </section>
  );
}
