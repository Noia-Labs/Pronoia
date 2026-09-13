import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { BarChart, LineChart, ScatterChart } from "echarts/charts";
import {
  AxisPointerComponent,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  TooltipComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  CandlestickChart,
  CircleDollarSign,
  Coins,
  Database,
  FlaskConical,
  Info,
  Loader2,
  LockKeyhole,
  ReceiptText,
  ShieldCheck,
  TrendingDown,
} from "lucide-react";
import type {
  BTPerformanceCurvePoint,
  BTPerformanceEventMarker,
  BTPerformanceKline,
  BTPerformanceResponse,
  BTPerformanceTrade,
  BTRun,
  BTCostScenarioInput,
  KlinePayload,
} from "../types";
import { api } from "../api";
import { useStore } from "../store";
import { cls } from "../utils";
import KlineChart, { type KlineTradeMarker } from "./KlineChart";
import QuantForecastMarketPanel from "./backtest/QuantForecastMarketPanel";

echarts.use([
  LineChart,
  BarChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  DataZoomComponent,
  AxisPointerComponent,
  CanvasRenderer,
]);

const COLORS = {
  strategy: "#B45309",
  asset: "#4A5D6B",
  benchmark: "#0F766E",
  drawdown: "#D14343",
  positive: "#D14343",
  negative: "#2E9E5B",
  ink: "#1C1B1A",
  mute: "#6B6862",
  edge: "#E8E5E0",
};

type CurveDatum = { time: string; value: number };

function finite(value: unknown): number | null {
  const n = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : NaN;
  return Number.isFinite(n) ? n : null;
}

function pointTime(point: BTPerformanceCurvePoint, index: number): string {
  return String(point.timestamp ?? point.date ?? point.event_time ?? `#${index + 1}`);
}

function curveRawValue(
  point: BTPerformanceCurvePoint,
  kind: "equity" | "asset" | "benchmark" | "drawdown",
): { value: number | null; isReturn: boolean } {
  if (kind === "drawdown") return { value: finite(point.drawdown ?? point.value), isReturn: false };
  const dedicated = kind === "asset"
    ? finite(point.asset_net_value ?? point.asset_value)
    : kind === "benchmark"
    ? finite(point.benchmark_net_value ?? point.benchmark_value)
    : null;
  if (dedicated != null) return { value: dedicated, isReturn: false };
  const net = finite(point.net_value);
  if (net != null) return { value: net, isReturn: false };
  const equity = finite(point.equity);
  if (equity != null) return { value: equity, isReturn: false };
  const cumulative = finite(point.cumulative_return);
  if (cumulative != null) return { value: cumulative, isReturn: true };
  const generic = finite(point.value);
  if (generic != null) return { value: generic, isReturn: false };
  const close = finite(point.close);
  if (close != null) return { value: close, isReturn: false };
  const ret = finite(point.return);
  return { value: ret, isReturn: ret != null };
}

function normalizeCurve(
  points: BTPerformanceCurvePoint[] | undefined,
  kind: "equity" | "asset" | "benchmark" | "drawdown",
): CurveDatum[] {
  const raw = (points ?? []).map((point, index) => {
    const resolved = curveRawValue(point, kind);
    return {
      time: pointTime(point, index),
      value: resolved.value,
      isReturn: resolved.isReturn,
    };
  }).filter((point): point is { time: string; value: number; isReturn: boolean } => point.value != null);
  if (kind === "drawdown") return raw.map(({ time, value }) => ({ time, value }));
  const firstLevel = raw.find((point) => !point.isReturn && point.value !== 0)?.value ?? null;
  return raw.map((point) => ({
    time: point.time,
    value: point.isReturn ? 1 + point.value : firstLevel ? point.value / firstLevel : point.value,
  }));
}

function tradeTime(trade: BTPerformanceTrade): string {
  return String(trade.exit_time ?? trade.execution_timestamp ?? trade.timestamp ?? trade.date ?? trade.event_time ?? trade.entry_time ?? trade.signal_timestamp ?? "");
}

function tradeReturn(trade: BTPerformanceTrade): number | null {
  return finite(trade.net_proxy_return ?? trade.realized_return ?? trade.net_return ?? trade.return);
}

function markerTime(marker: BTPerformanceEventMarker): string {
  return String(marker.timestamp ?? marker.date ?? "");
}

function summaryNumber(data: BTPerformanceResponse, keys: string[]): number | null {
  for (const key of keys) {
    const value = finite(data.summary?.[key]);
    if (value != null) return value;
  }
  return null;
}

function fmtPct(value: number | null, digits = 2): string {
  if (value == null) return "—";
  return `${value > 0 ? "+" : ""}${(value * 100).toFixed(digits)}%`;
}

function fmtRatio(value: number | null): string {
  return value == null ? "—" : value.toFixed(2);
}

function fmtMoney(value: number | null): string {
  if (value == null) return "—";
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(value);
}

function commonAxisLabels(series: CurveDatum[][]): string[] {
  const labels = new Set<string>();
  for (const points of series) for (const point of points) labels.add(point.time);
  return Array.from(labels).sort((a, b) => a.localeCompare(b));
}

type CurveLookup = { exact: Map<string, number>; firstByDate: Map<string, number>; dateOnly: boolean };

function hasIntradayTime(value: string): boolean {
  return /(?:T|\s)\d{1,2}:\d{2}/.test(value);
}

function buildCurveLookup(points: CurveDatum[]): CurveLookup {
  const exact = new Map<string, number>();
  const firstByDate = new Map<string, number>();
  for (const point of points) {
    exact.set(point.time, point.value);
    const date = point.time.slice(0, 10);
    if (!firstByDate.has(date)) firstByDate.set(date, point.value);
  }
  return { exact, firstByDate, dateOnly: points.every((point) => !hasIntradayTime(point.time)) };
}

function lookupCurveValue(lookup: CurveLookup, time: string): number | null {
  const exact = lookup.exact.get(time);
  if (exact != null) return exact;
  // Same-day fallback is valid when at least one side only identifies a day.
  // Two minute timestamps must never be silently placed on different bars.
  if (lookup.dateOnly || !hasIntradayTime(time)) {
    return lookup.firstByDate.get(time.slice(0, 10)) ?? null;
  }
  return null;
}

function PerformanceCurveChart({
  strategy,
  asset,
  benchmark,
  markers,
  height = 330,
}: {
  strategy: CurveDatum[];
  asset: CurveDatum[];
  benchmark: CurveDatum[];
  markers: BTPerformanceEventMarker[];
  height?: number;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    const labels = commonAxisLabels([strategy, asset, benchmark]);
    const strategyLookup = buildCurveLookup(strategy);
    const assetLookup = buildCurveLookup(asset);
    const benchmarkLookup = buildCurveLookup(benchmark);
    const labelByDate = new Map<string, string>();
    for (const label of labels) {
      const date = label.slice(0, 10);
      if (!labelByDate.has(date)) labelByDate.set(date, label);
    }
    const toValues = (lookup: CurveLookup) => labels.map((time) => lookupCurveValue(lookup, time));
    const markerData = markers.flatMap((marker) => {
      const time = markerTime(marker);
      const value = lookupCurveValue(strategyLookup, time);
      if (!time || value == null) return [];
      return [{
        name: marker.symbol ?? marker.event_id ?? "事件",
        value: [strategyLookup.exact.has(time) ? time : labelByDate.get(time.slice(0, 10)) ?? time, value],
        eventId: marker.event_id,
        direction: marker.direction,
        eventReturn: marker.net_proxy_return ?? marker.return,
      }];
    });
    const lineSeries = [
      { name: "策略净值", data: toValues(strategyLookup), color: COLORS.strategy, width: 2.5 },
      { name: "资产持有", data: toValues(assetLookup), color: COLORS.asset, width: 1.5 },
      { name: "评测基准", data: toValues(benchmarkLookup), color: COLORS.benchmark, width: 1.5 },
    ].filter((item) => item.data.some((value) => value != null));

    chart.setOption({
      animation: false,
      color: lineSeries.map((item) => item.color),
      textStyle: { fontFamily: "inherit" },
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "cross", lineStyle: { color: "#B9B4AA" } },
        backgroundColor: "#FFFFFF",
        borderColor: COLORS.edge,
        textStyle: { color: COLORS.ink, fontSize: 11 },
        valueFormatter: (value: unknown) => {
          const number = finite(value);
          return number == null ? "—" : `${number.toFixed(4)} (${((number - 1) * 100).toFixed(2)}%)`;
        },
      },
      legend: { top: 0, left: 8, itemWidth: 18, itemHeight: 3, textStyle: { color: COLORS.mute, fontSize: 11 } },
      grid: { left: 58, right: 22, top: 42, bottom: 48 },
      xAxis: {
        type: "category",
        data: labels,
        boundaryGap: false,
        axisLine: { lineStyle: { color: COLORS.edge } },
        axisTick: { show: false },
        axisLabel: { color: COLORS.mute, fontSize: 10, interval: Math.max(0, Math.floor(labels.length / 7)) },
      },
      yAxis: {
        type: "value",
        scale: true,
        axisLabel: { color: COLORS.mute, fontSize: 10, formatter: (value: number) => value.toFixed(2) },
        splitLine: { lineStyle: { color: "#F0EDE8" } },
      },
      dataZoom: [
        { type: "inside", start: 0, end: 100 },
        {
          type: "slider", bottom: 5, height: 16, borderColor: COLORS.edge,
          backgroundColor: "#FAF9F7", fillerColor: "rgba(180,83,9,.08)",
          handleStyle: { color: COLORS.strategy }, textStyle: { color: COLORS.mute, fontSize: 9 },
        },
      ],
      series: [
        ...lineSeries.map((item) => ({
          name: item.name,
          type: "line" as const,
          data: item.data,
          showSymbol: false,
          connectNulls: true,
          lineStyle: { color: item.color, width: item.width },
          itemStyle: { color: item.color },
          emphasis: { focus: "series" as const },
        })),
        ...(markerData.length ? [{
          name: "事件/交易",
          type: "scatter" as const,
          data: markerData,
          symbol: "diamond",
          symbolSize: 8,
          itemStyle: { color: COLORS.strategy, borderColor: "#FFFFFF", borderWidth: 1.5 },
          tooltip: { valueFormatter: (value: unknown) => String(value ?? "") },
          z: 8,
        }] : []),
      ],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [strategy, asset, benchmark, markers]);

  return <div ref={ref} className="w-full" style={{ height }} />;
}

function DrawdownChart({ points, height = 230 }: { points: CurveDatum[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      animation: false,
      textStyle: { fontFamily: "inherit" },
      tooltip: {
        trigger: "axis", backgroundColor: "#FFFFFF", borderColor: COLORS.edge,
        textStyle: { color: COLORS.ink, fontSize: 11 },
        valueFormatter: (value: unknown) => fmtPct(finite(value)),
      },
      grid: { left: 54, right: 18, top: 18, bottom: 34 },
      xAxis: {
        type: "category", data: points.map((point) => point.time), boundaryGap: false,
        axisLine: { lineStyle: { color: COLORS.edge } }, axisTick: { show: false },
        axisLabel: { color: COLORS.mute, fontSize: 9, interval: Math.max(0, Math.floor(points.length / 5)) },
      },
      yAxis: {
        type: "value", max: 0,
        axisLabel: { color: COLORS.mute, fontSize: 10, formatter: (value: number) => `${(value * 100).toFixed(0)}%` },
        splitLine: { lineStyle: { color: "#F0EDE8" } },
      },
      series: [{
        name: "回撤", type: "line", data: points.map((point) => Math.min(0, point.value)),
        showSymbol: false, lineStyle: { color: COLORS.drawdown, width: 1.5 },
        areaStyle: { color: "rgba(209,67,67,.13)" }, itemStyle: { color: COLORS.drawdown },
      }],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [points]);
  return <div ref={ref} className="w-full" style={{ height }} />;
}

function TradeReturnChart({ trades, height = 230 }: { trades: BTPerformanceTrade[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const usable = useMemo(() => trades.map((trade, index) => ({
    trade,
    index,
    value: tradeReturn(trade),
  })).filter((item): item is { trade: BTPerformanceTrade; index: number; value: number } => item.value != null), [trades]);

  useEffect(() => {
    if (!ref.current || !usable.length) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      animation: false,
      textStyle: { fontFamily: "inherit" },
      tooltip: {
        trigger: "axis", backgroundColor: "#FFFFFF", borderColor: COLORS.edge,
        textStyle: { color: COLORS.ink, fontSize: 11 },
        valueFormatter: (value: unknown) => fmtPct(finite(value)),
      },
      grid: { left: 52, right: 16, top: 18, bottom: 34 },
      xAxis: {
        type: "category", data: usable.map((item) => item.trade.symbol ?? `#${item.index + 1}`),
        axisLine: { lineStyle: { color: COLORS.edge } }, axisTick: { show: false },
        axisLabel: { color: COLORS.mute, fontSize: 9, interval: Math.max(0, Math.floor(usable.length / 7)) },
      },
      yAxis: {
        type: "value", axisLabel: { color: COLORS.mute, fontSize: 10, formatter: (value: number) => `${(value * 100).toFixed(0)}%` },
        splitLine: { lineStyle: { color: "#F0EDE8" } },
      },
      series: [{
        type: "bar",
        data: usable.map((item) => ({
          value: item.value,
          itemStyle: { color: item.value >= 0 ? COLORS.positive : COLORS.negative, borderRadius: item.value >= 0 ? [3, 3, 0, 0] : [0, 0, 3, 3] },
        })),
        barMaxWidth: 18,
      }],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [usable]);
  return <div ref={ref} className="w-full" style={{ height }} />;
}

export function CloseOnlyChart({ dates, closes, height = 300 }: { dates: string[]; closes: number[]; height?: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const chart = echarts.init(ref.current);
    chart.setOption({
      animation: false,
      textStyle: { fontFamily: "inherit" },
      tooltip: {
        trigger: "axis", backgroundColor: "#FFFFFF", borderColor: COLORS.edge,
        textStyle: { color: COLORS.ink, fontSize: 11 },
        valueFormatter: (value: unknown) => finite(value)?.toFixed(4) ?? "—",
      },
      grid: { left: 58, right: 20, top: 24, bottom: 42 },
      xAxis: {
        type: "category", data: dates, boundaryGap: false,
        axisLine: { lineStyle: { color: COLORS.edge } }, axisTick: { show: false },
        axisLabel: { color: COLORS.mute, fontSize: 10, interval: Math.max(0, Math.floor(dates.length / 7)) },
      },
      yAxis: {
        type: "value", scale: true, axisLabel: { color: COLORS.mute, fontSize: 10 },
        splitLine: { lineStyle: { color: "#F0EDE8" } },
      },
      series: [{
        name: "收盘价", type: "line", data: closes, showSymbol: false,
        lineStyle: { color: COLORS.asset, width: 1.8 }, areaStyle: { color: "rgba(74,93,107,.08)" },
      }],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [dates, closes]);
  return <div ref={ref} className="w-full" style={{ height }} />;
}

function EmptyChart({ text }: { text: string }) {
  return (
    <div className="grid min-h-[220px] place-items-center rounded-lg border border-dashed border-edge bg-edge/10 px-6 text-center text-[11.5px] leading-relaxed text-mute">
      {text}
    </div>
  );
}

function SectionHeading({ icon, eyebrow, title, note }: { icon: React.ReactNode; eyebrow: string; title: string; note?: string }) {
  return (
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-edge px-4 py-3">
      <div className="flex items-start gap-2.5">
        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-brand-soft/70 text-brand">{icon}</span>
        <div>
          <div className="text-[9.5px] font-semibold uppercase tracking-[0.16em] text-faint">{eyebrow}</div>
          <h3 className="mt-0.5 font-serif text-[14px] font-semibold text-ink">{title}</h3>
        </div>
      </div>
      {note && <span className="max-w-xl text-right text-[10.5px] leading-relaxed text-mute">{note}</span>}
    </div>
  );
}

function protocolItems(data: BTPerformanceResponse): Array<[string, string]> {
  const root = data.effective_protocol ?? {};
  const applied = root.applied && typeof root.applied === "object" ? root.applied as Record<string, unknown> : {};
  const requested = root.requested && typeof root.requested === "object" ? root.requested as Record<string, unknown> : {};
  const protocol = { ...requested, ...applied };
  const executionDelay = protocol.execution_delay;
  const executionDelayValue = executionDelay && typeof executionDelay === "object"
    ? (executionDelay as Record<string, unknown>).effective ?? (executionDelay as Record<string, unknown>).requested
    : executionDelay ?? protocol.entry_rule;
  const displayProtocolValue = (value: unknown): string => {
    const raw = String(value ?? "");
    const labels: Record<string, string> = {
      information_close_window: "信息可得收盘窗",
      event_close: "事件收盘窗",
      dataset_default: "数据集基准",
      close: "Close",
    };
    return labels[raw] ?? raw;
  };
  const candidates: Array<[string, string]> = [
    ["持有窗口", displayProtocolValue(protocol.holding_horizon ?? protocol.horizon)],
    ["成交时点", displayProtocolValue(executionDelayValue)],
    ["价格字段", displayProtocolValue(protocol.price_field)],
    ["手续费", protocol.fee_bps != null ? `${protocol.fee_bps} bps` : ""],
    ["滑点", protocol.slippage_bps != null ? `${protocol.slippage_bps} bps` : ""],
    ["基准", displayProtocolValue(protocol.benchmark)],
  ];
  return candidates.filter((item) => item[1]);
}

function klinePayload(raw: BTPerformanceKline | undefined): { full?: KlinePayload; dates: string[]; closes: number[]; lineOnly: boolean; error?: string } {
  if (!raw || typeof raw !== "object") return { dates: [], closes: [], lineOnly: true };
  const nested = raw.payload && typeof raw.payload === "object" ? raw.payload as Partial<BTPerformanceKline> : null;
  const source = nested ? { ...raw, ...nested } : raw;
  const dates = Array.isArray(source.dates) ? source.dates.map(String) : [];
  const closesRaw = Array.isArray(source.closes) ? source.closes : Array.isArray(source.close) ? source.close : [];
  const closes = closesRaw.map(finite).filter((value): value is number => value != null);
  const rawOhlc = Array.isArray(source.ohlc) ? source.ohlc : [];
  const ohlc = rawOhlc.map((row) => Array.isArray(row) ? row.map(finite) : []);
  const rawVolumes = Array.isArray(source.volumes) ? source.volumes : [];
  const volumes = rawVolumes.map(finite);
  const validOhlc = ohlc.length === dates.length && ohlc.every((row) => row.length === 4 && row.every((value) => value != null));
  const validVolumes = volumes.length === dates.length && volumes.every((value) => value != null);
  const full = dates.length > 0 && validOhlc
    ? {
        symbol: source.symbol,
        dates,
        ohlc: ohlc as [number, number, number, number][],
        volumes: validVolumes ? volumes as number[] : [],
        event_date: source.event_date,
      }
    : undefined;
  return {
    full,
    dates,
    closes,
    lineOnly: Boolean(source.line_only) || source.series_type === "line_only" || !full,
    error: typeof source.error === "string" ? source.error : undefined,
  };
}

function barsAsKline(data: BTPerformanceResponse | null): BTPerformanceKline | null {
  if (!Array.isArray(data?.bars) || !data.bars.length) return null;
  const dates: string[] = [];
  const ohlc: [number, number, number, number][] = [];
  const volumes: number[] = [];
  let allVolumes = true;
  const dataset = data.dataset && typeof data.dataset === "object" ? data.dataset : {};
  let symbol = String(dataset.symbol ?? "");
  for (const raw of data.bars) {
    if (!raw || typeof raw !== "object") continue;
    const bar = raw as Record<string, unknown>;
    const date = String(bar.timestamp ?? bar.date ?? bar.time ?? "");
    const open = finite(bar.open);
    const high = finite(bar.high);
    const low = finite(bar.low);
    const close = finite(bar.close);
    if (!date || open == null || high == null || low == null || close == null) continue;
    dates.push(date);
    ohlc.push([open, close, low, high]);
    const volume = finite(bar.volume);
    if (volume == null) allVolumes = false;
    else volumes.push(volume);
    if (!symbol && bar.symbol) symbol = String(bar.symbol);
  }
  if (!dates.length || ohlc.length !== dates.length) return null;
  return {
    symbol: symbol || "portfolio",
    dates,
    ohlc,
    volumes: allVolumes && volumes.length === dates.length ? volumes : [],
    series_type: "ohlc",
    source: "performance.bars",
  };
}

function objectRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function displayCount(value: unknown): string {
  const count = finite(value);
  return count == null ? "—" : Math.max(0, Math.round(count)).toLocaleString("zh-CN");
}

function samplingSeriesRecord(data: BTPerformanceResponse | null, key: string): Record<string, unknown> {
  const sampling = objectRecord(data?.response_sampling);
  return objectRecord(objectRecord(sampling.series)[key]);
}

function samplingLabel(data: BTPerformanceResponse | null, key: string, label: string): string | null {
  const series = samplingSeriesRecord(data, key);
  if (series.sampled !== true) return null;
  return `${label} ${displayCount(series.returned_count)}/${displayCount(series.total_count)}`;
}

export function compactRuleCondition(value: unknown): string {
  const condition = objectRecord(value);
  const fields: Record<string, string> = { price: "价格", moving_average: "均线", breakout: "突破", return: "收益率", volume: "成交量", volume_ratio: "成交量 / 均量", volatility: "波动率" };
  const operators: Record<string, string> = { above: "高于", below: "低于", crosses_above: "向上穿越", crosses_below: "向下穿越" };
  const threshold = finite(condition.threshold);
  const thresholdText = threshold == null ? "" : `，阈值 ${condition.threshold_unit === "percent" ? `${(threshold * 100).toFixed(2)}%` : threshold}`;
  const count = Number(condition.consecutive_count ?? 1);
  return `${fields[String(condition.field)] ?? String(condition.field ?? "条件")}${operators[String(condition.operator)] ?? String(condition.operator ?? "")}${condition.lookback == null ? "" : `（${condition.lookback} bar${thresholdText}）`}${count > 1 ? `，连续 ${count} 次确认` : ""}`;
}

function strategyRuleText(strategy: Record<string, unknown>, arenaSafe: boolean): string {
  if (arenaSafe) return "Arena-safe 模式已隐藏规则和参数，仅保留聚合表现。";
  const kind = String(strategy.kind ?? strategy.model_id ?? "");
  const parameters = objectRecord(strategy.parameters);
  if (kind === "buy_hold") return "首个可成交开盘建立 100% 多头仓位，随后持有至测算结束。";
  if (kind === "ma_cross") {
    return `MA${parameters.short_window ?? "短"} / MA${parameters.long_window ?? "长"} 趋势过滤；多头目标权重 100%，离场目标权重 ${Number(parameters.short_weight ?? 0) * 100}%。`;
  }
  if (kind === "momentum") {
    return `${parameters.lookback ?? "设定"} bar 动量过滤；动量为正时持有多头，否则目标权重 ${Number(parameters.negative_weight ?? 0) * 100}%。`;
  }
  const entry = objectRecord(parameters.entry);
  const exit = objectRecord(parameters.exit);
  const entryConditions = Array.isArray(entry.conditions) ? entry.conditions : [];
  const exitConditions = Array.isArray(exit.conditions) ? exit.conditions : [];
  if (entryConditions.length || exitConditions.length) {
    const entryText = entryConditions.length ? entryConditions.slice(0, 2).map(compactRuleCondition).join(` ${String(entry.combinator ?? "and").toUpperCase()} `) : "未定义";
    const exitText = exitConditions.length ? exitConditions.slice(0, 2).map(compactRuleCondition).join(` ${String(exit.combinator ?? "and").toUpperCase()} `) : "未定义";
    return `开仓：${entryText} · 平仓：${exitText}`;
  }
  return String(strategy.rules_summary ?? strategy.name ?? "后端未返回公开规则摘要；不会根据收益曲线反推策略。 ");
}

function costBps(value: unknown): string {
  const numeric = finite(value);
  return numeric == null ? "未配置" : `${numeric.toFixed(numeric % 1 === 0 ? 0 : 2)} bps`;
}

function timingLabel(value: unknown): string {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const timing = value as Record<string, unknown>;
    for (const key of ["effective", "applied", "requested", "value"]) {
      if (timing[key] != null && timing[key] !== value) return timingLabel(timing[key]);
    }
    return "未声明";
  }
  const labels: Record<string, string> = {
    bar_close: "每根 bar 收盘",
    next_open: "下一根 bar 开盘",
    event_close: "事件收盘",
    information_close_window: "信息可得收盘窗",
    oracle_close_window_proxy: "Oracle 收盘窗口代理",
  };
  return labels[String(value ?? "")] ?? String(value ?? "未声明");
}

function realCurveTime(point: BTPerformanceCurvePoint): string | null {
  const value = point.timestamp ?? point.date ?? point.event_time;
  if (value == null) return null;
  const text = String(value).trim();
  if (!text || /^#\d+$/.test(text)) return null;
  if (!/^\d{4}[-/]\d{1,2}[-/]\d{1,2}(?:$|[T\s])/.test(text) && !/^\d{8}(?:$|[T\s])/.test(text)) return null;
  return text;
}

function displayCurveTime(value: unknown): string {
  if (value == null) return "未返回";
  const text = String(value).trim();
  if (!text || /^#\d+$/.test(text)) return "未返回";
  return text.replace("T", " ").slice(0, 16);
}

function natureMeta(nature: string): { label: string; className: string } {
  if (nature === "simulated_from_real_bars") return { label: "真实 bar 仿真", className: "border-jade/20 bg-jade-soft text-jade" };
  if (nature === "unavailable") return { label: "结果不可用", className: "border-rise/20 bg-rise/5 text-rise" };
  return { label: "代理收益", className: "border-amber-200 bg-amber-50 text-amber-800" };
}

function CostScenarioField({ label, value, unit, step = 0.1, onChange }: { label: string; value: number; unit: string; step?: number; onChange: (value: number) => void }) {
  return (
    <label className="block min-w-0">
      <span className="mb-1 block text-[9px] font-medium text-mute">{label}</span>
      <span className="flex overflow-hidden rounded-lg border border-edge bg-card focus-within:border-brand/40">
        <input type="number" min={0} step={step} value={value} onChange={(event) => onChange(Number(event.target.value))} className="min-w-0 flex-1 bg-transparent px-2.5 py-2 font-mono text-[10.5px] text-ink outline-none" />
        <span className="grid place-items-center border-l border-edge bg-paper px-2 text-[8.5px] text-faint">{unit}</span>
      </span>
    </label>
  );
}

export default function BacktestPerformanceDashboard({
  runId,
  run,
  strategyType,
  data,
  loading,
  error,
  arenaSafe,
}: {
  runId: string;
  run: BTRun;
  strategyType: string;
  data: BTPerformanceResponse | null;
  loading: boolean;
  error: string | null;
  arenaSafe: boolean;
}) {
  const openBTDetail = useStore((state) => state.openBTDetail);
  const [selectedKline, setSelectedKline] = useState("");
  const [scenarioBusy, setScenarioBusy] = useState(false);
  const [scenarioError, setScenarioError] = useState<string | null>(null);
  const [scenarioDraft, setScenarioDraft] = useState({
    name: "",
    commission_bps: 0,
    slippage_bps: 0,
    stamp_duty_bps: 0,
    other_cost_bps: 0,
    minimum_commission: 0,
  });
  const strategyCurve = useMemo(() => normalizeCurve(data?.equity_curve, "equity"), [data]);
  const assetCurve = useMemo(() => normalizeCurve(data?.asset_curve, "asset"), [data]);
  const benchmarkCurve = useMemo(() => normalizeCurve(data?.benchmark_curve, "benchmark"), [data]);
  const drawdownCurve = useMemo(() => normalizeCurve(data?.drawdown_curve, "drawdown"), [data]);
  const trades = useMemo(() => Array.isArray(data?.trades) ? data.trades : [], [data]);
  const responseSampling = objectRecord(data?.response_sampling);
  const samplingApplied = responseSampling.applied === true;
  const samplingSummary = [
    samplingLabel(data, "equity_curve", "净值"),
    samplingLabel(data, "asset_curve", "资产"),
    samplingLabel(data, "benchmark_curve", "基准"),
    samplingLabel(data, "drawdown_curve", "回撤"),
    samplingLabel(data, "bars", "K 线"),
    samplingLabel(data, "trades", "交易"),
    samplingLabel(data, "positions", "持仓"),
    samplingLabel(data, "signals", "信号"),
  ].filter((value): value is string => Boolean(value));
  const comparisonCurvesSampled = ["equity_curve", "asset_curve", "benchmark_curve"]
    .some((key) => samplingSeriesRecord(data, key).sampled === true);
  const markers = useMemo(() => {
    if (Array.isArray(data?.event_markers) && data.event_markers.length) return data.event_markers;
    return trades.map((trade) => ({
      event_id: trade.event_id,
      symbol: trade.symbol,
      timestamp: tradeTime(trade),
      direction: trade.direction ?? trade.side,
      return: tradeReturn(trade),
    }));
  }, [data, trades]);
  const portfolioKline = useMemo(() => barsAsKline(data), [data]);
  const klineEntries = useMemo(() => {
    const entries = Object.entries(data?.kline_by_event ?? {});
    if (!entries.length && portfolioKline) return [["portfolio", portfolioKline] as [string, BTPerformanceKline]];
    return entries;
  }, [data, portfolioKline]);
  const responseProtocol = objectRecord(data?.effective_protocol);
  const performanceApplied = objectRecord(responseProtocol.applied);
  const runExecution = objectRecord(run.execution_spec);
  const runApplied = objectRecord(runExecution.applied);
  const appliedExecution = { ...runExecution, ...runApplied, ...performanceApplied };
  const commissionDefault = finite(appliedExecution.commission_bps ?? appliedExecution.fee_bps) ?? 0;
  const slippageDefault = finite(appliedExecution.slippage_bps) ?? 0;
  const stampDutyDefault = finite(appliedExecution.stamp_duty_bps) ?? 0;
  const otherCostDefault = finite(appliedExecution.other_cost_bps) ?? 0;
  const minimumCommissionDefault = finite(appliedExecution.minimum_commission) ?? 0;

  useEffect(() => {
    if (!klineEntries.length) { setSelectedKline(""); return; }
    if (!klineEntries.some(([eventId]) => eventId === selectedKline)) setSelectedKline(klineEntries[0][0]);
  }, [klineEntries, selectedKline]);

  useEffect(() => {
    setScenarioDraft({
      name: `${run.name} · 成本情景`,
      commission_bps: commissionDefault,
      slippage_bps: slippageDefault,
      stamp_duty_bps: stampDutyDefault,
      other_cost_bps: otherCostDefault,
      minimum_commission: minimumCommissionDefault,
    });
    setScenarioError(null);
  }, [runId, commissionDefault, slippageDefault, stampDutyDefault, otherCostDefault, minimumCommissionDefault, run.name]);

  const createCostScenario = async () => {
    if (scenarioBusy) return;
    const numericValues = [scenarioDraft.commission_bps, scenarioDraft.slippage_bps, scenarioDraft.stamp_duty_bps, scenarioDraft.other_cost_bps, scenarioDraft.minimum_commission];
    if (numericValues.some((value) => !Number.isFinite(value) || value < 0)) {
      setScenarioError("成本参数必须是大于或等于 0 的数字。");
      return;
    }
    setScenarioBusy(true);
    setScenarioError(null);
    try {
      const payload: BTCostScenarioInput = {
        name: scenarioDraft.name.trim() || undefined,
        commission_bps: scenarioDraft.commission_bps,
        slippage_bps: scenarioDraft.slippage_bps,
        stamp_duty_bps: scenarioDraft.stamp_duty_bps,
        other_cost_bps: scenarioDraft.other_cost_bps,
        minimum_commission: scenarioDraft.minimum_commission,
        auto_start: true,
      };
      const rawCreated = await api.btCreateCostScenario(runId, payload);
      const wrapped = rawCreated as BTRun & { run?: BTRun };
      const created = wrapped.run ?? rawCreated;
      if (!created?.id) throw new Error("服务端没有返回新 Run ID");
      openBTDetail(created.id);
    } catch (error) {
      setScenarioError(error instanceof Error ? error.message : String(error));
    } finally {
      setScenarioBusy(false);
    }
  };

  if (loading && !data) {
    return (
      <section className="rounded-card border border-edge bg-card px-5 py-8 shadow-card">
        <div className="flex items-center justify-center gap-2 text-[12px] text-mute">
          <Loader2 size={14} className="animate-spin text-brand" /> 正在读取收益、回撤、真实 K 线与交易序列…
        </div>
      </section>
    );
  }

  if (!data) {
    return (
      <section className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <SectionHeading icon={<CandlestickChart size={15} />} eyebrow="Performance & market replay" title="收益曲线、回撤与真实 K 线" />
        <div className="flex items-start gap-3 px-5 py-5">
          <Info size={16} className="mt-0.5 shrink-0 text-faint" />
          <div>
            <div className="text-[12.5px] font-medium text-ink">完成 Run 后，这里会显示完整图表</div>
            <p className="mt-1 max-w-3xl text-[11.5px] leading-relaxed text-mute">
              {strategyType === "event"
                ? "当前 Run 尚未返回真实收益与行情。请先完成事件分析并取得 Oracle 行情覆盖，再刷新查看收益曲线、回撤曲线和关键事件 K 线。"
                : "当前 Run 尚未返回真实收益与行情。请先选择 available 且包含 OHLC 的冻结数据版本并完成运行，再刷新查看 K 线、买卖点和收益曲线。"}
              页面不会用演示值、随机数或推测价格补齐图表。
            </p>
            {error && <div className="mt-2 font-mono text-[10px] text-faint">{error}</div>}
          </div>
        </div>
      </section>
    );
  }

  if (data.prediction_only || run.strategy_spec?.kind === "return_forecast") {
    return <QuantForecastMarketPanel data={data} arenaSafe={arenaSafe} />;
  }

  if (data.status === "unavailable") {
    return (
      <section className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <SectionHeading icon={<CandlestickChart size={15} />} eyebrow="Performance & market replay" title="收益曲线、回撤与真实 K 线" />
        <div className="flex items-start gap-3 px-5 py-5">
          <Info size={16} className="mt-0.5 shrink-0 text-faint" />
          <div>
            <div className="text-[12.5px] font-medium text-ink">本次运行暂不可计算投资表现</div>
            <p className="mt-1 max-w-3xl text-[11.5px] leading-relaxed text-mute">
              {strategyType === "event"
                ? "本次事件运行尚无 Oracle CAR 或真实行情覆盖。完成运行并补齐对应事件时点的真实行情后，关键事件 K 线才会出现。"
                : "本次运行尚无可执行的冻结 OHLC 行情或有效成交。完成运行后，真实 K 线、买卖点与收益曲线才会出现。"}
              页面不会生成替代曲线。
            </p>
            {data.proxy_disclaimer && <div className="mt-2 text-[10.5px] leading-relaxed text-amber-800">{data.proxy_disclaimer}</div>}
          </div>
        </div>
      </section>
    );
  }

  const totalReturn = summaryNumber(data, ["total_return"]);
  const annualizedReturn = summaryNumber(data, ["annualized_return"]);
  const assetReferenceReturn = summaryNumber(data, ["asset_total_return", "asset_return"]);
  const benchmarkReferenceReturn = summaryNumber(data, ["benchmark_total_return", "benchmark_return"]);
  const excessReturn = summaryNumber(data, ["excess_total_return", "excess_return"]);
  const maxDrawdown = summaryNumber(data, ["max_drawdown"]);
  const sharpe = summaryNumber(data, ["sharpe_ratio", "sharpe_proxy"]);
  const winRate = summaryNumber(data, ["win_rate"]);
  const tradeCount = summaryNumber(data, ["n_trades", "trade_count", "n_events_in_protocol", "evaluated_events"]);
  const totalCost = summaryNumber(data, ["total_cost_amount_proxy", "total_cost", "total_cost_rate_sum"]);
  const protocol = protocolItems(data);
  const selectedRaw = klineEntries.find(([eventId]) => eventId === selectedKline)?.[1];
  const selectedChart = klinePayload(selectedRaw);
  const selectedSymbol = String(selectedRaw?.symbol ?? selectedRaw?.payload?.symbol ?? "");
  const tradeMarkers: KlineTradeMarker[] = trades.flatMap((trade) => {
    if (selectedSymbol && trade.symbol && String(trade.symbol) !== selectedSymbol) return [];
    const markersOut: KlineTradeMarker[] = [];
    const side = String(trade.side ?? trade.direction ?? "");
    const executionTime = trade.entry_time ?? trade.execution_timestamp ?? trade.timestamp ?? trade.date;
    if (executionTime) markersOut.push({
      date: String(executionTime),
      side: /sell|short|down|空|卖/i.test(side) ? "sell" : "buy",
      price: finite(trade.entry_price ?? trade.effective_price ?? trade.market_price),
      label: /sell|short|down|空|卖/i.test(side) ? "卖出/开空" : "买入/开多",
    });
    if (trade.exit_time) markersOut.push({
      date: String(trade.exit_time),
      side: /sell|short|down|空|卖/i.test(side) ? "buy" : "sell",
      price: finite(trade.exit_price),
      label: "平仓",
    });
    return markersOut;
  });
  const runConfig = (run.config ?? {}) as Record<string, unknown>;
  const configuredBasis = runConfig.data_basis && typeof runConfig.data_basis === "object" ? runConfig.data_basis as Record<string, unknown> : {};
  const responseDataset = data.dataset && typeof data.dataset === "object" ? data.dataset : {};
  const responseBasis = data.data_basis && typeof data.data_basis === "object" ? data.data_basis : {};
  const basis = { ...configuredBasis, ...responseDataset, ...responseBasis };
  const provenance = data.data_provenance && typeof data.data_provenance === "object" ? data.data_provenance : {};
  const resultNature = String(data.result_nature ?? run.result_nature ?? (data.mode === "event_proxy" ? "proxy" : "unavailable"));
  const nature = natureMeta(resultNature);
  const frequency = String(basis.frequency ?? run.execution_spec?.frequency ?? "未声明");
  const market = String(basis.market ?? (Array.isArray(basis.markets) ? basis.markets.join("/") : "未声明"));
  const provider = String(provenance.provider ?? provenance.source ?? basis.provider ?? basis.source_type ?? "未声明");
  const datasetVersion = String(data.dataset_version ?? run.dataset_version ?? basis.dataset_version ?? "未返回");
  const winRateBasis = String(data.summary.win_rate_basis ?? (strategyType === "event" ? "event_window" : "active_period"));
  const responseStrategy = objectRecord(data.strategy);
  const configuredStrategy = objectRecord(run.strategy_spec ?? objectRecord(run.config).strategy_spec);
  const strategyContract = { ...configuredStrategy, ...responseStrategy };
  const strategyName = arenaSafe ? "受保护策略" : String(strategyContract.name ?? run.name);
  const strategyKind = arenaSafe ? "private" : String(strategyContract.kind ?? strategyContract.model_id ?? strategyContract.type ?? run.runner);
  const strategyAdapter = arenaSafe ? "已隐藏" : String(configuredStrategy.adapter ?? strategyContract.adapter ?? run.runner);
  const decisionTiming = strategyContract.decision_timing ?? objectRecord(strategyContract.parameters).signal_timing ?? appliedExecution.signal_timing;
  const executionTiming = strategyContract.execution_timing ?? objectRecord(strategyContract.parameters).execution_timing ?? appliedExecution.execution_delay;
  const portfolioMode = String(data.engine_mode ?? run.engine_mode) === "portfolio";
  const appliedEventWindow = objectRecord(performanceApplied.event_window);
  const appliedEventDelay = objectRecord(performanceApplied.execution_delay);
  const realCurveTimes = data.equity_curve.map(realCurveTime).filter((value): value is string => Boolean(value));
  const curveStart = realCurveTimes[0]
    ?? String(appliedEventWindow.start_date ?? appliedExecution.start_date ?? responseDataset.start_at ?? "未返回");
  const curveEnd = realCurveTimes[realCurveTimes.length - 1]
    ?? String(appliedEventWindow.end_date ?? appliedExecution.end_date ?? responseDataset.end_at ?? "未返回");
  const barCount = summaryNumber(data, ["bar_count"])
    ?? (portfolioMode ? strategyCurve.length : summaryNumber(data, ["n_events_in_protocol", "n_predictions"]) ?? Math.max(0, strategyCurve.length - 1));
  const horizonRaw = String(data.primary_horizon ?? appliedExecution.holding_horizon ?? "t3");
  const horizonLabel = /^t\d+$/i.test(horizonRaw) ? `T+${horizonRaw.slice(1)}` : horizonRaw.toUpperCase();
  const eventReturnAnchor = appliedEventDelay.effective ?? executionTiming;
  const eventRequestedDelay = appliedEventDelay.requested ?? appliedExecution.execution_delay;
  const timingTitle = portfolioMode
    ? `${timingLabel(decisionTiming)}形成信号，${timingLabel(executionTiming)}成交`
    : `请求 ${timingLabel(eventRequestedDelay)}；实际以${timingLabel(eventReturnAnchor)}锚定 ${horizonLabel} Oracle CAR`;
  const totalCommission = summaryNumber(data, ["total_commission"]);
  const totalSlippage = summaryNumber(data, ["total_slippage"]);
  const totalStampDuty = summaryNumber(data, ["total_stamp_duty"]);
  const totalOtherCost = summaryNumber(data, ["total_other_cost"]);
  const initialCapital = summaryNumber(data, ["initial_value", "initial_capital"]);
  const totalCostImpact = totalCost != null && initialCapital && initialCapital > 0 ? totalCost / initialCapital : null;
  const tradeSampling = samplingSeriesRecord(data, "trades");
  const barSampling = samplingSeriesRecord(data, "bars");
  const returnedTrades = finite(tradeSampling.returned_count) ?? trades.length;
  const totalTrades = finite(tradeSampling.total_count) ?? tradeCount ?? trades.length;
  const returnedBars = finite(barSampling.returned_count) ?? finite(data.bars?.length);
  const totalBars = finite(barSampling.total_count) ?? barCount;
  const tradeSectionNote = tradeSampling.sampled === true
    ? `展示 ${displayCount(returnedTrades)} / 全量 ${displayCount(totalTrades)} 条；CSV 导出保持全量`
    : `${displayCount(trades.length)} 条真实结果记录`;
  const klineSectionNote = portfolioKline
    ? barSampling.sampled === true
      ? `冻结 performance.bars 抽样展示 ${displayCount(returnedBars)}/${displayCount(totalBars)} · B/S 为模拟成交标记`
      : "来自冻结 performance.bars · B/S 为实际成交标记"
    : "真实行情按需读取；未返回 bar 时不绘图";

  const kpis = [
    { label: "策略总收益", value: fmtPct(totalReturn), note: "净值首尾变动", tone: totalReturn == null ? "text-ink" : totalReturn >= 0 ? "text-rise" : "text-fall" },
    { label: "年化收益", value: fmtPct(annualizedReturn), note: annualizedReturn == null ? "接口未返回" : "按 applied 年化因子", tone: annualizedReturn == null ? "text-ink" : annualizedReturn >= 0 ? "text-rise" : "text-fall" },
    { label: "基准收益", value: fmtPct(benchmarkReferenceReturn), note: benchmarkReferenceReturn == null ? "未返回独立基准" : strategyType === "event" ? "Oracle 同窗基准" : "数据资产买入持有", tone: benchmarkReferenceReturn == null ? "text-ink" : benchmarkReferenceReturn >= 0 ? "text-rise" : "text-fall" },
    { label: "超额收益", value: fmtPct(excessReturn), note: excessReturn == null ? (assetReferenceReturn == null ? "接口未返回" : `资产参照 ${fmtPct(assetReferenceReturn)}`) : "策略减已声明基准", tone: excessReturn == null ? "text-ink" : excessReturn >= 0 ? "text-rise" : "text-fall" },
    { label: "最大回撤", value: fmtPct(maxDrawdown == null ? null : -Math.abs(maxDrawdown)), note: "峰值至谷值", tone: maxDrawdown == null ? "text-ink" : "text-fall" },
    { label: "Sharpe", value: fmtRatio(sharpe), note: data.summary.sharpe_ratio != null ? "年化风险调整" : "代理估计", tone: sharpe == null ? "text-ink" : sharpe >= 1 ? "text-jade" : "text-ink" },
    { label: winRateBasis === "active_period" ? "活跃周期胜率" : "事件窗口胜率", value: winRate == null ? "—" : `${(winRate * 100).toFixed(1)}%`, note: winRateBasis === "active_period" ? "持仓活跃 bar 正收益占比" : "有效事件收益样本", tone: "text-ink" },
    { label: strategyType === "event" ? "事件样本" : "权重变更", value: tradeCount == null ? "—" : String(Math.round(tradeCount)), note: totalCost == null ? "成本未返回" : `累计模拟成本 ${fmtMoney(totalCost)}`, tone: "text-ink" },
  ];

  return (
    <section className="space-y-4">
      <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
        <SectionHeading
          icon={<Activity size={15} />}
          eyebrow="Performance contract"
          title="投资表现与风险"
          note="指标按全量冻结结果计算；长序列按真实点有界展示"
        />
        <div className="border-b border-edge bg-[#F8F6F1] px-4 py-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              <Database size={13} className="text-brand" />
              <span className="text-[10px] font-semibold uppercase tracking-[0.14em] text-faint">统一数据基准</span>
              <span className={cls("rounded-full border px-2 py-1 text-[9.5px] font-medium", nature.className)}>{nature.label}</span>
              <span className="rounded-full border border-edge bg-card px-2 py-1 text-[9.5px] text-mute">市场 <strong className="font-mono font-medium text-ink">{market}</strong></span>
              <span className="rounded-full border border-edge bg-card px-2 py-1 text-[9.5px] text-mute">频率 <strong className="font-mono font-medium text-ink">{frequency}</strong></span>
              <span className="max-w-[240px] truncate rounded-full border border-edge bg-card px-2 py-1 text-[9.5px] text-mute" title={provider}>来源 <strong className="font-mono font-medium text-ink">{provider}</strong></span>
              <span className="max-w-[220px] truncate rounded-full border border-edge bg-card px-2 py-1 text-[9.5px] text-mute" title={datasetVersion}>版本 <strong className="font-mono font-medium text-ink">{datasetVersion}</strong></span>
              <span className={cls("rounded-full border px-2 py-1 text-[9.5px]", data.data_frozen ? "border-jade/20 bg-jade-soft text-jade" : "border-amber-200 bg-amber-50 text-amber-800")}>{data.data_frozen ? "评测数据已冻结" : "冻结状态未确认"}</span>
            </div>
          </div>
          {protocol.length > 0 && (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              <ShieldCheck size={12} className="text-jade" />
              {protocol.map(([label, value]) => <span key={label} className="rounded bg-card px-1.5 py-0.5 text-[9px] text-mute">{label} <strong className="font-mono font-medium text-ink">{value}</strong></span>)}
            </div>
          )}
          {samplingApplied && (
            <p role="note" className="mt-2 flex items-start gap-1.5 rounded-lg border border-brand/15 bg-brand-soft/35 px-2.5 py-2 text-[10px] leading-relaxed text-mute">
              <Info size={11} className="mt-0.5 shrink-0 text-brand" />
              <span>为保持页面流畅，本页只传输真实序列的有界抽样点{samplingSummary.length ? `（${samplingSummary.join(" · ")}）` : ""}；收益、回撤等指标仍按全量冻结结果计算，原始结果文件未被裁剪。</span>
            </p>
          )}
          {(data.proxy_disclaimer || resultNature === "proxy") && (
            <p className="mt-2 flex items-start gap-1.5 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-2 text-[10px] leading-relaxed text-amber-800"><AlertTriangle size={11} className="mt-0.5 shrink-0" />{data.proxy_disclaimer || "这是事件窗口代理收益，不等同于完整资金占用、重叠仓位与逐 bar 撮合结果。"}</p>
          )}
          {data.status === "partial" && <p className="mt-2 text-[9.5px] text-amber-700">部分行情或 Oracle 缺失；缺失观察不会被补值。</p>}
        </div>
        <div className="border-b border-edge px-4 py-3.5">
          <div className="flex items-center gap-2">
            <span className="grid h-7 w-7 place-items-center rounded-lg bg-violet-soft/55 text-violet"><FlaskConical size={13} /></span>
            <div><div className="text-[9px] font-semibold uppercase tracking-[0.15em] text-faint">Applied strategy</div><h4 className="mt-0.5 text-[11.5px] font-semibold text-ink">本次执行策略与成本</h4></div>
          </div>
          <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
            <div className="rounded-lg border border-edge bg-paper px-3 py-2.5"><div className="text-[8.5px] uppercase tracking-wider text-faint">策略 / 适配器</div><div className="mt-1 truncate text-[10.5px] font-semibold text-ink" title={strategyName}>{strategyName}</div><div className="mt-0.5 truncate font-mono text-[9px] text-mute">{strategyAdapter} · {strategyKind}</div></div>
            <div className="rounded-lg border border-edge bg-paper px-3 py-2.5" title={timingTitle}><div className="text-[8.5px] uppercase tracking-wider text-faint">{portfolioMode ? "信号 → 成交" : "事件判断 → 代理计量"}</div><div className="mt-1 text-[10.5px] font-semibold text-ink">{portfolioMode ? `${timingLabel(decisionTiming)}形成信号` : "信息可得时点形成判断"}</div><div className="mt-0.5 text-[9px] text-mute">{portfolioMode ? `${timingLabel(executionTiming)}成交` : `${timingLabel(eventReturnAnchor)}锚定 ${horizonLabel} Oracle CAR（非订单成交）`}</div></div>
            <div className="rounded-lg border border-edge bg-paper px-3 py-2.5"><div className="text-[8.5px] uppercase tracking-wider text-faint">曲线测算日期</div><div className="mt-1 whitespace-nowrap font-mono text-[10.5px] font-semibold text-ink">{displayCurveTime(curveStart)} → {displayCurveTime(curveEnd)}</div><div className="mt-0.5 text-[9px] text-mute">{Math.round(barCount)} {portfolioMode ? "根有效 bar" : "个有效事件观察"}</div></div>
            <div className="rounded-lg border border-edge bg-paper px-3 py-2.5"><div className="text-[8.5px] uppercase tracking-wider text-faint">执行规则摘要</div><div className="mt-1 line-clamp-2 text-[9.5px] leading-relaxed text-ink" title={strategyRuleText(strategyContract, arenaSafe)}>{strategyRuleText(strategyContract, arenaSafe)}</div></div>
          </div>

          {portfolioMode && (
            <div className="mt-3 overflow-hidden rounded-xl border border-edge bg-paper">
              <div className="flex flex-wrap items-center justify-between gap-3 px-3.5 py-3">
                <div className="flex items-start gap-2.5">
                  <span className="grid h-7 w-7 shrink-0 place-items-center rounded-lg bg-brand-soft/60 text-brand"><ReceiptText size={13} /></span>
                  <div>
                    <div className="text-[10.5px] font-semibold text-ink">已应用成本归因</div>
                    <div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[9px] text-mute">
                      <span>手续费 <strong className="font-mono font-medium text-ink">{costBps(appliedExecution.commission_bps ?? appliedExecution.fee_bps)}</strong>{totalCommission != null ? ` · ${fmtMoney(totalCommission)}` : ""}</span>
                      <span>滑点 <strong className="font-mono font-medium text-ink">{costBps(appliedExecution.slippage_bps)}</strong>{totalSlippage != null ? ` · ${fmtMoney(totalSlippage)}` : ""}</span>
                      <span>卖出印花税 <strong className="font-mono font-medium text-ink">{costBps(appliedExecution.stamp_duty_bps)}</strong>{totalStampDuty != null ? ` · ${fmtMoney(totalStampDuty)}` : ""}</span>
                      <span>其他费用 <strong className="font-mono font-medium text-ink">{costBps(appliedExecution.other_cost_bps)}</strong>{totalOtherCost != null ? ` · ${fmtMoney(totalOtherCost)}` : ""}</span>
                      <span>最低单笔手续费 <strong className="font-mono font-medium text-ink">{finite(appliedExecution.minimum_commission) == null ? "未配置" : fmtMoney(finite(appliedExecution.minimum_commission))}</strong></span>
                    </div>
                  </div>
                </div>
                <div className="text-right"><div className="font-mono text-[14px] font-semibold text-ink">{fmtMoney(totalCost)}</div><div className="mt-0.5 text-[8.5px] text-faint">累计成本{totalCostImpact == null ? "" : ` · 期初资金占比 ${(totalCostImpact * 100).toFixed(2)}%`}</div></div>
              </div>

              {!arenaSafe && (
                <details className="group border-t border-edge bg-card">
                  <summary className="flex cursor-pointer list-none items-center justify-between gap-3 px-3.5 py-2.5 text-[10.5px] font-semibold text-ink transition hover:bg-edge/20">
                    <span className="inline-flex items-center gap-1.5"><Coins size={12} className="text-brand" /> 新建成本情景</span>
                    <span className="text-[9px] font-normal text-mute">展开调整费率；会创建新 Run，不改写本次结果</span>
                  </summary>
                  <div className="border-t border-edge px-3.5 py-3">
                    <label className="block"><span className="mb-1 block text-[9px] font-medium text-mute">情景名称</span><input value={scenarioDraft.name} onChange={(event) => setScenarioDraft((current) => ({ ...current, name: event.target.value }))} className="w-full rounded-lg border border-edge bg-paper px-2.5 py-2 text-[10.5px] text-ink outline-none focus:border-brand/40" /></label>
                    <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-5">
                      <CostScenarioField label="手续费" value={scenarioDraft.commission_bps} unit="bps / 成交" onChange={(value) => setScenarioDraft((current) => ({ ...current, commission_bps: value }))} />
                      <CostScenarioField label="滑点" value={scenarioDraft.slippage_bps} unit="bps / 成交" onChange={(value) => setScenarioDraft((current) => ({ ...current, slippage_bps: value }))} />
                      <CostScenarioField label="卖出印花税" value={scenarioDraft.stamp_duty_bps} unit="bps / 卖出" onChange={(value) => setScenarioDraft((current) => ({ ...current, stamp_duty_bps: value }))} />
                      <CostScenarioField label="其他双边费用" value={scenarioDraft.other_cost_bps} unit="bps / 成交" onChange={(value) => setScenarioDraft((current) => ({ ...current, other_cost_bps: value }))} />
                      <CostScenarioField label="最低单笔手续费" value={scenarioDraft.minimum_commission} unit="CNY" step={0.01} onChange={(value) => setScenarioDraft((current) => ({ ...current, minimum_commission: value }))} />
                    </div>
                    <div className="mt-3 flex flex-wrap items-center justify-between gap-3">
                      <p className="max-w-3xl text-[9px] leading-relaxed text-mute">平台将复用本次冻结数据与策略，创建并运行一个新的成本情景。旧 Run 保持不变；成本协议不同的 Run 不能进入严格 Arena 同组。</p>
                      <button type="button" onClick={() => void createCostScenario()} disabled={scenarioBusy} className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-3.5 py-2 text-[10.5px] font-semibold text-card transition hover:opacity-90 disabled:opacity-45">{scenarioBusy ? <Loader2 size={12} className="animate-spin" /> : <FlaskConical size={12} />} 创建并运行成本情景</button>
                    </div>
                    {scenarioError && <p className="mt-2 text-[9.5px] text-rise">创建失败：{scenarioError}</p>}
                  </div>
                </details>
              )}
            </div>
          )}
        </div>
        <div className="grid grid-cols-2 divide-x divide-y divide-edge sm:grid-cols-4 xl:grid-cols-8 xl:divide-y-0">
          {kpis.map((kpi) => (
            <div key={kpi.label} className="min-w-0 px-4 py-3.5">
              <div className="text-[10px] uppercase tracking-[0.08em] text-faint">{kpi.label}</div>
              <div className={cls("mt-1 font-mono text-[21px] font-semibold tabular-nums", kpi.tone)}>{kpi.value}</div>
              <div className="mt-0.5 truncate text-[9.5px] text-mute">{kpi.note}</div>
            </div>
          ))}
        </div>
      </div>

      <nav className="flex flex-wrap items-center gap-2 rounded-card border border-edge bg-card px-3 py-2.5 shadow-card" aria-label="投资图表快捷导航">
        <span className="mr-1 text-[9.5px] font-semibold uppercase tracking-[0.12em] text-faint">图表导航</span>
        <a href="#backtest-equity-chart" className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-paper px-2.5 py-1.5 text-[10.5px] font-medium text-mute transition hover:border-edgeDark hover:text-ink">
          <BarChart3 size={11} /> 收益曲线
        </a>
        <a href="#backtest-drawdown-chart" className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-paper px-2.5 py-1.5 text-[10.5px] font-medium text-mute transition hover:border-edgeDark hover:text-ink">
          <TrendingDown size={11} /> 回撤曲线
        </a>
        {!arenaSafe && (
          <a href="#backtest-kline-chart" className="inline-flex items-center gap-1.5 rounded-lg border border-brand/20 bg-brand-soft/45 px-2.5 py-1.5 text-[10.5px] font-semibold text-brand transition hover:border-brand/35 hover:bg-brand-soft">
            <CandlestickChart size={11} /> {strategyType === "event" ? "关键事件 K 线" : "真实 K 线与买卖点"}
          </a>
        )}
        <span className="text-[9.5px] text-faint">图表只读取本次 Run 的冻结结果</span>
      </nav>

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1.65fr)_minmax(320px,.85fr)]">
        <div id="backtest-equity-chart" className="scroll-mt-4 overflow-hidden rounded-card border border-edge bg-card shadow-card">
          <SectionHeading icon={<BarChart3 size={15} />} eyebrow="Equity curve" title="策略净值 vs 资产 / 基准" note={comparisonCurvesSampled ? "统一归一化为 1.0000 · 图表为真实点抽样" : "统一归一化为 1.0000"} />
          <div className="p-3">
            {strategyCurve.length ? (
              <PerformanceCurveChart strategy={strategyCurve} asset={assetCurve} benchmark={benchmarkCurve} markers={arenaSafe ? [] : markers} />
            ) : (
              <EmptyChart text="performance 已返回，但没有可绘制的 equity_curve；不会推导或补造净值。" />
            )}
          </div>
        </div>
        <div id="backtest-drawdown-chart" className="scroll-mt-4 overflow-hidden rounded-card border border-edge bg-card shadow-card">
          <SectionHeading icon={<TrendingDown size={15} />} eyebrow="Underwater" title="回撤曲线" note={samplingSeriesRecord(data, "drawdown_curve").sampled === true ? "0% 表示历史净值高点 · 保留全局极值" : "0% 表示历史净值高点"} />
          <div className="p-3">
            {drawdownCurve.length ? <DrawdownChart points={drawdownCurve} /> : <EmptyChart text="暂无真实 drawdown_curve。" />}
          </div>
        </div>
      </div>

      {!arenaSafe && (
        <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(420px,1.25fr)]">
          <div className="overflow-hidden rounded-card border border-edge bg-card shadow-card">
            <SectionHeading icon={<CircleDollarSign size={15} />} eyebrow={strategyType === "event" ? "Event attribution" : "Trade attribution"} title={strategyType === "event" ? "逐笔事件收益" : "逐次模拟成交与成本"} note={tradeSectionNote} />
            <div className="p-3">
              {trades.some((trade) => tradeReturn(trade) != null)
                ? <TradeReturnChart trades={trades} />
                : <EmptyChart text={strategyType === "event" ? "暂无逐笔收益字段；不会把预测置信度当作交易收益。" : "暂无已实现交易收益；不会从信号强度推算成交结果。"} />}
            </div>
            {trades.length > 0 && (
              <div className="max-h-[230px] overflow-auto border-t border-edge">
                <table className="w-full text-[10.5px]">
                  <thead className="sticky top-0 bg-[#F8F6F1] text-left text-faint">
                    <tr><th className="px-3 py-2 font-medium">时间</th><th className="px-3 py-2 font-medium">标的</th><th className="px-3 py-2 font-medium">方向</th><th className="px-3 py-2 text-right font-medium">{strategyType === "event" ? "净收益" : "成交成本"}</th></tr>
                  </thead>
                  <tbody>
                    {trades.slice(0, 50).map((trade, index) => {
                      const value = strategyType === "event" ? tradeReturn(trade) : finite(trade.total_cost ?? trade.cost);
                      return (
                        <tr key={trade.id ?? trade.event_id ?? index} className="border-t border-edge/70">
                          <td className="whitespace-nowrap px-3 py-2 font-mono text-mute">{tradeTime(trade).slice(0, 16).replace("T", " ") || "—"}</td>
                          <td className="px-3 py-2 font-mono text-ink">{trade.symbol ?? selectedSymbol ?? "—"}</td>
                          <td className="px-3 py-2 text-mute">{trade.direction ?? trade.side ?? "—"}</td>
                          <td className={cls("px-3 py-2 text-right font-mono tabular-nums", strategyType === "event" ? (value == null ? "text-faint" : value >= 0 ? "text-rise" : "text-fall") : "text-mute")}>{strategyType === "event" ? fmtPct(value) : fmtMoney(value)}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div id="backtest-kline-chart" className="scroll-mt-4 overflow-hidden rounded-card border border-edge bg-card shadow-card">
            <SectionHeading icon={<CandlestickChart size={15} />} eyebrow="Market replay" title={strategyType === "event" ? "关键事件 K 线" : "行情与买卖点"} note={klineSectionNote} />
            {klineEntries.length ? (
              <>
                <div className="flex gap-1 overflow-x-auto border-b border-edge px-3 py-2">
                  {klineEntries.slice(0, 20).map(([eventId, raw]) => (
                    <button
                      key={eventId}
                      type="button"
                      onClick={() => setSelectedKline(eventId)}
                      className={cls(
                        "shrink-0 rounded-md border px-2.5 py-1 text-[10px] transition",
                        selectedKline === eventId ? "border-brand/30 bg-brand-soft text-brand" : "border-edge bg-card text-mute hover:text-ink",
                      )}
                    >
                      {String(raw?.symbol ?? raw?.payload?.symbol ?? eventId).slice(0, 16)}
                    </button>
                  ))}
                </div>
                <div className="p-3">
                  {selectedChart.error ? (
                    <EmptyChart text={`行情不可用：${selectedChart.error}`} />
                  ) : selectedChart.full && !selectedChart.lineOnly ? (
                    <KlineChart payload={selectedChart.full} height={300} tradeMarkers={tradeMarkers} />
                  ) : selectedChart.dates.length && selectedChart.closes.length === selectedChart.dates.length ? (
                    <div>
                      <div className="mb-2 inline-flex rounded bg-edge/50 px-2 py-1 text-[9.5px] text-mute">close-only · 不补造 OHLC</div>
                      <CloseOnlyChart dates={selectedChart.dates} closes={selectedChart.closes} />
                    </div>
                  ) : (
                    <EmptyChart text="该事件未返回可绘制的真实 K 线。请确认 Run 已完成且 Oracle 有真实行情覆盖；也可在“事件判断审计”中展开单事件行情重试。" />
                  )}
                </div>
              </>
            ) : (
              <div className="p-3"><EmptyChart text={strategyType === "event" ? "performance 未附带 K 线。请确认 Run 已完成且 Oracle 有真实行情覆盖；随后可在“事件判断审计”中按事件懒加载行情。" : "performance 未附带真实 OHLC bars。请先用 available 的冻结行情数据完成 Run，再刷新查看 K 线与实际买卖点。"} /></div>
            )}
          </div>
        </div>
      )}

      {arenaSafe && (
        <div className="flex items-start gap-2 rounded-card border border-jade/20 bg-jade-soft/30 px-4 py-3 text-[11px] leading-relaxed text-mute">
          <LockKeyhole size={14} className="mt-0.5 shrink-0 text-jade" />
          Arena-safe 模式仅展示聚合净值与风险统计；逐笔交易、事件标记和事件 K 线已隐藏。
        </div>
      )}
    </section>
  );
}
