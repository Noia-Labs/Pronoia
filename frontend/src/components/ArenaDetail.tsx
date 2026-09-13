import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  CalendarRange,
  ChevronRight,
  CircleAlert,
  Clock3,
  Database,
  Download,
  Gauge,
  LineChart,
  Loader2,
  LockKeyhole,
  Medal,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { api } from "../api";
import { useStore } from "../store";
import type { ArenaComputeResult, ArenaItem, BTMetricItem, BTPerformanceCurvePoint, BTPerformanceResponse } from "../types";
import { cls } from "../utils";
import ModelComparisonResults from "./arena/ModelComparisonResults";
import { isModelComparisonResult } from "./arena/arenaComparison";
import ArenaWorkspaceDetail from "./arena/ArenaWorkspaceDetail";
import { isArenaWorkspace } from "./arena/arenaWorkspace";

const COLORS = ["#B45309", "#0F766E", "#7C3AED", "#2563EB", "#D14343", "#0891B2", "#BE185D", "#4D7C0F"];
const asRecord = (v: unknown): Record<string, unknown> => v && typeof v === "object" && !Array.isArray(v) ? v as Record<string, unknown> : {};
const text = (v: unknown) => typeof v === "string" ? v : v === null || v === undefined ? "" : String(v);
const short = (v: string) => v.length > 20 ? `${v.slice(0, 11)}…${v.slice(-6)}` : v || "—";
const color = (i: number) => COLORS[i % COLORS.length];
const n = (v: unknown): number | null => v === null || v === undefined || v === "" || typeof v === "boolean" ? null : Number.isFinite(Number(v)) ? Number(v) : null;
const pct = (v: number | null | undefined, signed = false) => v === null || v === undefined ? "—" : `${signed && v > 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
const number = (v: number | null | undefined, digits = 2) => v === null || v === undefined || !Number.isFinite(v) ? "—" : v.toFixed(digits);
/** 后端可能把回撤传成负收益率或正幅度；界面统一展示为正的损失幅度。 */
const drawdownMagnitude = (v: number | null | undefined) => v === null || v === undefined || !Number.isFinite(v) ? null : Math.abs(v);

type CurvePoint = { x: number; y: number; index: number; timestamp: string | undefined; segment: number };
type Curve = { runId: string; name: string; points: CurvePoint[] };
type ArenaRow = {
  id: string;
  name: string;
  ret: number | null;
  dd: number | null;
  sharpe: number | null;
  annual: number | null;
  calmar: number | null;
  volatility: number | null;
  win: number | null;
  tradeCount: number | null;
  turnover: number | null;
  totalCost: number | null;
  totalCostRate: number | null;
  currency: string;
  info: ArenaComputeResult["per_run"][string];
};
type MeasurementScope = {
  start: string;
  end: string;
  frequency: string;
  observations: string;
  dateAxis: boolean;
  privacyRedacted: boolean;
  alignmentMode: string;
};

function metricValue(item: BTMetricItem | undefined): number | null { return item && typeof item.value === "number" && Number.isFinite(item.value) ? item.value : null; }
function getPerfMetric(perf: BTPerformanceResponse | undefined, names: string[]): number | null {
  const s = perf?.summary as Record<string, unknown> | undefined;
  if (!s) return null;
  for (const name of names) { const value = n(s[name]); if (value !== null) return value; }
  return null;
}
function runMetric(result: ArenaComputeResult, runId: string, perf: BTPerformanceResponse | undefined, aliases: string[]): number | null {
  const metrics = result.per_run[runId]?.metrics ?? {};
  for (const id of aliases) { const value = metricValue(metrics[id]); if (value !== null) return value; }
  const normalized = aliases.map((s) => s.toLowerCase());
  for (const [id, item] of Object.entries(metrics)) if (normalized.some((a) => id.toLowerCase().includes(a))) { const value = metricValue(item); if (value !== null) return value; }
  return getPerfMetric(perf, aliases);
}
function eventAccuracy(result: ArenaComputeResult, runId: string, perf: BTPerformanceResponse | undefined): number | null {
  const fromArena = runMetric(result, runId, undefined, [
    "acc_primary_non_neutral", "acc_t3_strict", "acc_avg_all_strict",
    "directional_accuracy", "accuracy", "hit_rate",
  ]);
  if (fromArena !== null) return fromArena;
  const correctness = asRecord(perf?.correctness);
  const nonNeutral = asRecord(correctness.oracle_non_neutral);
  return n(nonNeutral.accuracy) ?? n(correctness.accuracy);
}
function recordMetric(record: Record<string, unknown>, aliases: string[]): number | null {
  for (const name of aliases) { const value = n(record[name]); if (value !== null) return value; }
  return null;
}
function curveTimestamp(point: Record<string, unknown>): string {
  return text(point.timestamp) || text(point.date) || text(point.event_time);
}
function timestampValue(value: string): number | null {
  if (!value) return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}
function curvePoints(points: BTPerformanceCurvePoint[] | undefined, useDate = false): CurvePoint[] {
  if (!Array.isArray(points)) return [];
  return points.map((p, index) => {
    const raw = p as unknown as Record<string, unknown>;
    const value = n(p.net_value) ?? n(p.value) ?? n(p.equity);
    const observationIndex = n(p.index) ?? index;
    const timestamp = curveTimestamp(p);
    const time = timestampValue(timestamp);
    return value === null ? null : {
      x: useDate && time !== null ? time : observationIndex,
      y: value,
      index: observationIndex,
      timestamp: timestamp || undefined,
      segment: n(raw.segment) ?? n(raw.segment_id) ?? 0,
    };
  }).filter((p): p is CurvePoint => p !== null);
}
function maxDrawdown(points: CurvePoint[]) {
  if (!points.length) return null; let peak = -Infinity; let worst = 0;
  for (const p of points) { peak = Math.max(peak, p.y); if (peak > 0) worst = Math.min(worst, p.y / peak - 1); }
  return worst;
}
function drawdownPoints(points: CurvePoint[]): CurvePoint[] {
  let peak = -Infinity;
  return points.map((point) => {
    peak = Math.max(peak, point.y);
    return { ...point, y: peak > 0 ? point.y / peak - 1 : 0 };
  });
}
const CHART = { left: 52, right: 18, top: 14, bottom: 42 };
function path(points: CurvePoint[], width: number, height: number, minX: number, maxX: number, minY: number, maxY: number) {
  if (!points.length) return ""; const sx = (x: number) => CHART.left + ((x - minX) / Math.max(maxX - minX, 1)) * (width - CHART.left - CHART.right); const sy = (y: number) => CHART.top + (1 - (y - minY) / Math.max(maxY - minY, 1e-9)) * (height - CHART.top - CHART.bottom);
  return points.map((p, i) => `${i && p.segment === points[i - 1].segment ? "L" : "M"}${sx(p.x).toFixed(1)},${sy(p.y).toFixed(1)}`).join(" ");
}

function displayTime(value: string, intraday = false): string {
  if (!value) return "—";
  const normalized = value.replace("T", " ");
  return intraday && normalized.length >= 16 ? normalized.slice(0, 16) : normalized.slice(0, 10);
}
function frequencyLabel(value: string): string {
  const normalized = value.trim().toLowerCase();
  if (normalized === "1d" || normalized === "d" || normalized === "day") return "日 K · 1D";
  const minute = normalized.match(/^(\d+)m(?:in)?$/);
  if (minute) return `${minute[1]} 分钟 K`;
  return value || "—";
}
function chartDateLabel(value: number, intraday: boolean): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  const parts = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    ...(intraday ? { hour: "2-digit", minute: "2-digit", hour12: false } : {}),
  }).formatToParts(date).reduce<Record<string, string>>((acc, item) => { acc[item.type] = item.value; return acc; }, {});
  return intraday ? `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}` : `${parts.year}-${parts.month}-${parts.day}`;
}

function MultiCurve({ curves, title, drawdown = false, dateAxis = false, frequency = "" }: { curves: Curve[]; title: string; drawdown?: boolean; dateAxis?: boolean; frequency?: string }) {
  const prepared = curves.map((c) => drawdown ? { ...c, points: drawdownPoints(c.points) } : c).filter((c) => c.points.length > 1);
  if (!prepared.length) return <NoData text={drawdown ? "没有可用净值序列，无法推导回撤曲线。" : "没有可用收益曲线。请完成使用同一数据协议的回测后重新计算。"} />;
  const all = prepared.flatMap((c) => c.points); const minX = Math.min(...all.map((p) => p.x)); const maxX = Math.max(...all.map((p) => p.x)); const minY = Math.min(...all.map((p) => p.y)); const maxY = Math.max(...all.map((p) => p.y)); const W = 960, H = 320; const plotHeight = H - CHART.top - CHART.bottom; const intraday = !["1d", "d", "day"].includes(frequency.toLowerCase()); const tickFractions = [0, .25, .5, .75, 1]; const axisLabel = (value: number) => dateAxis ? chartDateLabel(value, intraday) : String(Math.round(value));
  const sx = (x: number) => CHART.left + ((x - minX) / Math.max(maxX - minX, 1)) * (W - CHART.left - CHART.right);
  const sy = (y: number) => CHART.top + (1 - (y - minY) / Math.max(maxY - minY, 1e-9)) * (H - CHART.top - CHART.bottom);
  const hasGaps = prepared.some((curve) => curve.points.some((point, index) => index > 0 && point.segment !== curve.points[index - 1].segment));
  return <section className="rounded-xl border border-edgeDark/70 bg-card p-4 shadow-card">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <div><h2 className="text-[13px] font-semibold text-ink">{title}</h2><p className="mt-0.5 text-[10px] text-mute">{dateAxis ? "横轴为冻结行情的真实交易日期。" : "横轴为隐私安全的统一观察序号。"} 仅展示真实回测净值；{hasGaps ? "缺失区间已断线。" : "没有制造插值点。"}</p></div>
      <div className="flex flex-wrap gap-x-3 gap-y-1">{prepared.map((c, i) => <span key={c.runId} className="inline-flex max-w-[170px] items-center gap-1.5 truncate text-[9.5px] text-mute"><i className="h-2 w-2 shrink-0 rounded-full" style={{ background: color(i) }} />{c.name}</span>)}</div>
    </div>
    <svg viewBox={`0 0 ${W} ${H}`} className="h-[300px] w-full overflow-visible">
      <line x1={CHART.left} x2={W - CHART.right} y1={CHART.top} y2={CHART.top} stroke="#E8E5E0" /><line x1={CHART.left} x2={W - CHART.right} y1={H - CHART.bottom} y2={H - CHART.bottom} stroke="#E8E5E0" />
      {[.25,.5,.75].map((f) => <line key={f} x1={CHART.left} x2={W - CHART.right} y1={CHART.top + plotHeight * f} y2={CHART.top + plotHeight * f} stroke="#E8E5E0" strokeDasharray="3 4" />)}
      <text x="2" y="20" fill="#96918A" fontSize="10">{drawdown ? pct(maxY) : number(maxY, 3)}</text><text x="2" y={H - CHART.bottom + 3} fill="#96918A" fontSize="10">{drawdown ? pct(minY) : number(minY, 3)}</text>
      {tickFractions.map((fraction, index) => { const x = CHART.left + fraction * (W - CHART.left - CHART.right); const value = minX + fraction * (maxX - minX); return <g key={fraction}><line x1={x} x2={x} y1={H - CHART.bottom} y2={H - CHART.bottom + 4} stroke="#B8B3AB" /><text x={x} y={H - 15} textAnchor={index === 0 ? "start" : index === tickFractions.length - 1 ? "end" : "middle"} fill="#96918A" fontSize={dateAxis && intraday ? "8.5" : "9.5"}>{axisLabel(value)}</text></g>; })}
      {prepared.map((c, i) => <g key={c.runId}><path d={path(c.points, W, H, minX, maxX, minY, maxY)} fill="none" stroke={color(i)} strokeWidth="2.2" strokeLinecap="round" />{c.points.map((point, pointIndex) => pointIndex > 0 && point.segment !== c.points[pointIndex - 1].segment ? <circle key={`${c.runId}-${pointIndex}`} cx={sx(point.x)} cy={sy(point.y)} r="3" fill={color(i)} stroke="#fff" strokeWidth="1.5" /> : null)}</g>)}
    </svg>
  </section>;
}

function NoData({ text: message }: { text: string }) { return <div className="rounded-xl border border-dashed border-edgeDark bg-card px-6 py-12 text-center"><CircleAlert className="mx-auto text-faint" size={19} /><p className="mx-auto mt-3 max-w-lg text-[10.5px] leading-relaxed text-mute">{message}</p></div>; }

function Scatter({ rows }: { rows: Array<{ runId: string; name: string; returnValue: number | null; drawdown: number | null }> }) {
  const points = rows.filter((r) => r.returnValue !== null && r.drawdown !== null) as Array<{ runId: string; name: string; returnValue: number; drawdown: number }>;
  if (!points.length) return <NoData text="缺少同一批策略的累计收益与最大回撤，暂不能绘制风险收益分布。" />;
  const W = 760, H = 300, xs = points.map((p) => Math.abs(p.drawdown)), ys = points.map((p) => p.returnValue), maxX = Math.max(...xs, .01), minY = Math.min(...ys, 0), maxY = Math.max(...ys, .01); const px = (x: number) => 54 + (x / maxX) * (W - 76); const py = (y: number) => 20 + (1 - (y - minY) / Math.max(maxY - minY, .01)) * (H - 50);
  return <section className="rounded-xl border border-edgeDark/70 bg-card p-4 shadow-card"><div className="mb-2"><h2 className="text-[13px] font-semibold text-ink">收益 / 回撤分布</h2><p className="mt-0.5 text-[10px] text-mute">越靠左上通常代表更高收益与更低回撤；不构成投资建议。</p></div><svg viewBox={`0 0 ${W} ${H}`} className="h-[275px] w-full"><line x1="54" x2="54" y1="20" y2={H - 30} stroke="#E8E5E0" /><line x1="54" x2={W - 20} y1={H - 30} y2={H - 30} stroke="#E8E5E0" /><text x="2" y="25" fontSize="10" fill="#96918A">收益 {pct(maxY)}</text><text x="2" y={H - 30} fontSize="10" fill="#96918A">{pct(minY)}</text><text x={W - 112} y={H - 8} fontSize="10" fill="#96918A">回撤 {pct(maxX)}</text>{points.map((p, i) => <g key={p.runId}><circle cx={px(Math.abs(p.drawdown))} cy={py(p.returnValue)} r="6" fill={color(i)} /><text x={px(Math.abs(p.drawdown)) + 9} y={py(p.returnValue) + 4} fontSize="10" fill="#383632">{p.name.slice(0, 14)}</text></g>)}</svg></section>;
}

function downloadCsv(blob: Blob, name: string) { const url = URL.createObjectURL(blob); const a = document.createElement("a"); a.href = url; a.download = name; a.click(); URL.revokeObjectURL(url); }

function formatCost(row: ArenaRow): string {
  if (row.totalCost !== null) {
    try {
      return new Intl.NumberFormat("zh-CN", { style: "currency", currency: row.currency || "CNY", maximumFractionDigits: 2 }).format(row.totalCost);
    } catch {
      return `${number(row.totalCost)} ${row.currency}`.trim();
    }
  }
  return row.totalCostRate !== null ? pct(row.totalCostRate) : "—";
}

function ScopeStrip({ scope }: { scope: MeasurementScope }) {
  const intraday = !["1d", "d", "day"].includes(scope.frequency.toLowerCase());
  const cells = [
    { label: scope.alignmentMode === "union" || scope.alignmentMode === "native" ? "展示时间范围" : "对齐测算区间", value: scope.privacyRedacted ? "日期按 Arena-safe 协议隐藏" : `${displayTime(scope.start, intraday)} → ${displayTime(scope.end, intraday)}`, icon: <CalendarRange size={14} /> },
    { label: "对齐方式", value: alignmentModeLabel(scope.alignmentMode), icon: <Clock3 size={14} /> },
    { label: "净值观察点", value: scope.observations, icon: <LineChart size={14} /> },
    { label: "频率 / 横轴", value: `${frequencyLabel(scope.frequency)} · ${scope.dateAxis ? "真实日期" : "观察序号"}`, icon: scope.dateAxis ? <CalendarRange size={14} /> : <LockKeyhole size={14} /> },
  ];
  return <section className="rounded-xl border border-edgeDark/70 bg-card shadow-card"><div className="grid divide-y divide-edge md:grid-cols-4 md:divide-x md:divide-y-0">{cells.map((cell) => <div key={cell.label} className="min-w-0 px-4 py-3"><div className="flex items-center gap-1.5 text-[9.5px] text-faint"><span className="text-brand">{cell.icon}</span>{cell.label}</div><div className="mt-1.5 truncate text-[11px] font-semibold tabular-nums text-ink" title={cell.value}>{cell.value}</div></div>)}</div></section>;
}

const alignmentModeLabel = (mode: string) => ({
  intersection: "共同区间",
  union: "完整并集",
  manual: "手动区间",
  native: "各自原始区间",
}[mode] || "历史兼容口径");
const alignmentFrequencyLabel = (frequency: string) => ({
  native: "原始频率",
  day: "日",
  week: "周",
  month: "月",
}[frequency] || frequency || "原始频率");
const coverageStatusLabel = (status: string) => ({
  complete: "完整",
  partial: "部分覆盖",
  insufficient: "样本偏短",
  unavailable: "不可用",
}[status] || status || "未知");
function warningLabel(code: string): string {
  if (code === "aligned_sample_short") return "对齐后样本偏短，年化与风险指标仅供参考";
  if (code === "history_starts_after_selected_window") return "起始时间晚于选定区间（指数/ETF 的实际存续期可能较短）";
  if (code === "history_ends_before_selected_window") return "历史数据在选定区间结束前已终止";
  if (code === "internal_missing_periods") return "历史中存在较长缺失区间，曲线已断开";
  if (code.includes("without_upsampling")) return "请求频率高于源数据，已保留原始频率且未升采样";
  return code;
}

function AlignmentSummary({ contract, rows }: { contract: Record<string, unknown>; rows: ArenaRow[] }) {
  const alignment = asRecord(contract.alignment);
  const requested = asRecord(alignment.requested);
  const coverage = asRecord(contract.coverage);
  const byRun = asRecord(coverage.by_run);
  const explicit = Object.keys(alignment).length > 0;
  if (!explicit) return <section className="rounded-xl border border-edgeDark/70 bg-card px-4 py-3 text-[10px] leading-relaxed text-mute"><b className="text-ink">历史 Arena · 原始时间口径</b><span className="ml-2">该 Arena 创建于时间对齐功能之前，保持原始结果不被重新解释。若需共同区间、并集或手动区间，请新建 Arena。</span></section>;
  const mode = text(alignment.mode) || text(requested.mode);
  const frequency = text(alignment.frequency) || text(requested.frequency);
  const rankingAllowed = alignment.ranking_allowed === true;
  const warnings = Array.isArray(alignment.warnings) ? alignment.warnings.map(text).filter(Boolean) : [];
  return <section className="overflow-hidden rounded-xl border border-edgeDark/70 bg-card shadow-card">
    <div className="flex flex-wrap items-start justify-between gap-3 border-b border-edgeDark/70 px-4 py-3">
      <div><div className="flex items-center gap-2"><h2 className="text-[13px] font-semibold text-ink">时间覆盖与可信度</h2><span className="rounded-full bg-brand/10 px-2 py-0.5 text-[8.5px] font-semibold text-brand">{alignmentModeLabel(mode)} · {alignmentFrequencyLabel(frequency)}</span></div><p className="mt-1 text-[10px] leading-relaxed text-mute">仅裁剪真实观测或向低频取真实收盘点；不插值、不前向填充、不拉伸曲线。</p></div>
      <span className={cls("rounded-full px-2.5 py-1 text-[9px] font-semibold", rankingAllowed ? "bg-jade/10 text-jade" : "bg-amber-500/10 text-amber-700")}>{rankingAllowed ? "对齐指标可用于正式排名" : "对齐指标仅作观察"}</span>
    </div>
    <div className="divide-y divide-edge">
      {rows.map((row) => {
        const item = asRecord(byRun[row.id]);
        const ratio = n(item.window_coverage_ratio);
        const status = text(item.status);
        const itemWarnings = Array.isArray(item.warnings) ? item.warnings.map(text).filter(Boolean) : [];
        const allWarnings = [...new Set([...itemWarnings, ...warnings.filter((warning) => warning.startsWith(`${row.id}:`)).map((warning) => warning.slice(row.id.length + 1))])];
        const observations = n(item.aligned_observations);
        const nativeObservations = n(item.native_observations);
        return <div key={row.id} className="grid gap-3 px-4 py-3 lg:grid-cols-[minmax(170px,.9fr)_minmax(180px,1.1fr)_minmax(280px,1.7fr)] lg:items-center">
          <div className="min-w-0"><div className="truncate text-[10.5px] font-semibold text-ink">{row.name}</div><div className="mt-0.5 text-[9px] text-faint">{text(item.source_frequency) || "未知源频率"} → {alignmentFrequencyLabel(text(item.effective_frequency))}</div></div>
          <div><div className="mb-1 flex items-center justify-between text-[9px] text-mute"><span>{coverageStatusLabel(status)}</span><span className="tabular-nums">{ratio === null ? "—" : `${(ratio * 100).toFixed(1)}%`}</span></div><div className="h-1.5 overflow-hidden rounded-full bg-edge"><div className={cls("h-full rounded-full", status === "complete" ? "bg-jade" : status === "unavailable" ? "bg-rise" : "bg-amber-500")} style={{ width: `${Math.max(0, Math.min(100, (ratio ?? 0) * 100))}%` }} /></div><div className="mt-1 text-[8.5px] tabular-nums text-faint">观察点 {observations ?? "—"} / 原始 {nativeObservations ?? "—"}</div></div>
          <div className="text-[9.5px] leading-relaxed text-mute">{item.dates_redacted === true ? "日期已按 Arena-safe 协议隐藏" : `${displayTime(text(item.aligned_start))} → ${displayTime(text(item.aligned_end))}`}{allWarnings.length > 0 && <ul className="mt-1 list-disc pl-4 text-amber-700">{allWarnings.map((warning) => <li key={warning}>{warningLabel(warning)}</li>)}</ul>}</div>
        </div>;
      })}
    </div>
  </section>;
}

function OriginalAlignedTable({ contract, rows }: { contract: Record<string, unknown>; rows: ArenaRow[] }) {
  const native = asRecord(contract.native_metrics);
  const aligned = asRecord(contract.aligned_metrics);
  const alignment = asRecord(contract.alignment);
  if (!Object.keys(aligned).length) return null;
  const cells = rows.map((row) => {
    const nativeItem = asRecord(native[row.id]);
    const alignedItem = asRecord(aligned[row.id]);
    return {
      row,
      nativeReturn: recordMetric(nativeItem, ["total_return"]) ?? row.ret,
      alignedReturn: recordMetric(alignedItem, ["total_return"]),
      nativeAnnual: recordMetric(nativeItem, ["annualized_return"]) ?? row.annual,
      alignedAnnual: recordMetric(alignedItem, ["annualized_return"]),
      nativeDrawdown: recordMetric(nativeItem, ["max_drawdown"]) ?? row.dd,
      alignedDrawdown: recordMetric(alignedItem, ["max_drawdown"]),
      nativeSharpe: recordMetric(nativeItem, ["sharpe_ratio", "sharpe_proxy"]) ?? row.sharpe,
      alignedSharpe: recordMetric(alignedItem, ["sharpe_ratio"]),
      alignedCount: n(alignedItem.n_observations),
      sufficient: alignedItem.sample_sufficient === true,
    };
  });
  const rankingAllowed = alignment.ranking_allowed === true;
  return <section className="overflow-hidden rounded-xl border border-edgeDark/70 bg-card shadow-card">
    <div className="flex items-start justify-between gap-3 border-b border-edgeDark/70 px-4 py-3"><div><h2 className="text-[13px] font-semibold text-ink">原始周期 vs 对齐周期</h2><p className="mt-0.5 text-[10px] text-mute">原始列保留各 Run 全历史指标；对齐列仅使用当前所选真实时间区间。两者不混排。</p></div><span className={cls("shrink-0 rounded-full px-2 py-1 text-[8.5px] font-semibold", rankingAllowed ? "bg-jade/10 text-jade" : "bg-amber-500/10 text-amber-700")}>{rankingAllowed ? "共同口径" : "不可作公平名次"}</span></div>
    <div className="overflow-x-auto"><table className="min-w-[1080px] w-full text-left text-[10px]"><thead className="bg-edge/40 text-[8.5px] uppercase tracking-wide text-faint"><tr><th rowSpan={2} className="px-4 py-3">策略 / 模型</th><th colSpan={2} className="border-l border-edge px-3 py-2 text-center">累计收益</th><th colSpan={2} className="border-l border-edge px-3 py-2 text-center">年化收益</th><th colSpan={2} className="border-l border-edge px-3 py-2 text-center">最大回撤</th><th colSpan={2} className="border-l border-edge px-3 py-2 text-center">Sharpe</th><th rowSpan={2} className="border-l border-edge px-3 py-3">对齐样本</th></tr><tr><th className="border-l border-edge px-3 py-2">原始</th><th className="px-3 py-2 text-brand">对齐</th><th className="border-l border-edge px-3 py-2">原始</th><th className="px-3 py-2 text-brand">对齐</th><th className="border-l border-edge px-3 py-2">原始</th><th className="px-3 py-2 text-brand">对齐</th><th className="border-l border-edge px-3 py-2">原始</th><th className="px-3 py-2 text-brand">对齐</th></tr></thead><tbody>{cells.map((item) => <tr key={item.row.id} className="border-t border-edge/70"><td className="px-4 py-3"><div className="font-semibold text-ink">{item.row.name}</div><div className="mt-0.5 text-[8.5px] text-faint">{item.row.info.model_version || item.row.info.runner || "策略"}</div></td><td className="border-l border-edge/70 px-3 py-3 tabular-nums">{pct(item.nativeReturn, true)}</td><td className="bg-brand/[.025] px-3 py-3 font-semibold tabular-nums text-brand">{pct(item.alignedReturn, true)}</td><td className="border-l border-edge/70 px-3 py-3 tabular-nums">{pct(item.nativeAnnual, true)}</td><td className="bg-brand/[.025] px-3 py-3 tabular-nums text-brand">{pct(item.alignedAnnual, true)}</td><td className="border-l border-edge/70 px-3 py-3 tabular-nums">{pct(drawdownMagnitude(item.nativeDrawdown))}</td><td className="bg-brand/[.025] px-3 py-3 tabular-nums text-brand">{pct(drawdownMagnitude(item.alignedDrawdown))}</td><td className="border-l border-edge/70 px-3 py-3 tabular-nums">{number(item.nativeSharpe)}</td><td className="bg-brand/[.025] px-3 py-3 tabular-nums text-brand">{number(item.alignedSharpe)}</td><td className={cls("border-l border-edge/70 px-3 py-3 tabular-nums", item.sufficient ? "text-jade" : "text-amber-700")}>{item.alignedCount ?? "—"}{!item.sufficient && " · 偏短"}</td></tr>)}</tbody></table></div>
  </section>;
}

function lineageLabel(info: ArenaRow["info"]): string {
  const lineage = asRecord((info as unknown as Record<string, unknown>).lineage);
  const profile = text(lineage.profile_name) || text(lineage.profile_id);
  const experiment = text(lineage.experiment_id) || text(lineage.batch_id);
  return [profile && `模型 ${profile}`, experiment && `实验 ${short(experiment)}`].filter(Boolean).join(" · ");
}

function MetricMatrix({ rows, title = "统一口径收益与风险", onExport, exploratory = false }: { rows: ArenaRow[]; title?: string; onExport?: (id: string, label: string) => void; exploratory?: boolean }) {
  const ranking = [...rows].sort((a, b) => (b.ret ?? -Infinity) - (a.ret ?? -Infinity));
  const hasTurnover = rows.some((row) => row.turnover !== null);
  const hasCost = rows.some((row) => row.totalCost !== null || row.totalCostRate !== null);
  return <section className="rounded-xl border border-edgeDark/70 bg-card shadow-card"><div className="flex items-center justify-between border-b border-edgeDark/70 px-4 py-3"><div><h2 className="text-[13px] font-semibold text-ink">{title}</h2><p className="mt-0.5 text-[10px] text-mute">{exploratory ? "仅按总收益排列便于阅读；协议不同，不代表公平名次。" : "指标直接读取各 Run 冻结结果；缺失项保留为空，不做推算或填补。"}</p></div><LineChart size={16} className="text-brand" /></div><div className="overflow-x-auto"><table className="min-w-[1120px] w-full text-left text-[10.5px]"><thead className="bg-edge/40 text-[8.5px] uppercase tracking-wide text-faint"><tr><th className="sticky left-0 z-10 min-w-[235px] bg-[#F6F4F1] px-4 py-3">{exploratory ? "探索排序 / 策略" : "排名 / 策略"}</th><th className="px-3 py-3">总收益</th><th className="px-3 py-3">年化收益</th><th className="px-3 py-3">最大回撤</th><th className="px-3 py-3" title="年化风险调整收益">Sharpe</th><th className="px-3 py-3" title="年化收益 / 最大回撤">Calmar</th><th className="px-3 py-3">年化波动</th><th className="px-3 py-3" title="口径以各 Run 的 win_rate_basis 为准">胜率</th><th className="px-3 py-3">交易数</th>{hasTurnover && <th className="px-3 py-3" title="累计绝对目标权重变化">累计换手</th>}{hasCost && <th className="px-3 py-3">总成本</th>}{onExport && <th className="px-3 py-3">明细</th>}</tr></thead><tbody>{ranking.map((row, index) => { const lineage = lineageLabel(row.info); return <tr key={row.id} className="border-t border-edge/70"><td className="sticky left-0 z-[5] bg-card px-4 py-3"><div className="flex items-center gap-2"><span className={cls("grid h-6 w-6 shrink-0 place-items-center rounded-full text-[9px] font-bold", !exploratory && index === 0 ? "bg-brand text-white" : "bg-edge text-mute")}>{exploratory ? index + 1 : `#${index + 1}`}</span><div className="min-w-0"><div className="max-w-[195px] truncate font-semibold text-ink" title={row.name}>{row.name}</div><div className="mt-0.5 text-[9px] text-faint">{lineage || `${row.info.runner} · ${row.info.model_version || "策略"}`}</div></div></div></td><td className={cls("px-3 py-3 font-semibold tabular-nums", row.ret === null ? "text-faint" : row.ret >= 0 ? "text-jade" : "text-rise")}>{pct(row.ret, true)}</td><td className="px-3 py-3 tabular-nums text-ink">{pct(row.annual, true)}</td><td className="px-3 py-3 tabular-nums text-rise">{pct(drawdownMagnitude(row.dd))}</td><td className="px-3 py-3 tabular-nums text-ink">{number(row.sharpe)}</td><td className="px-3 py-3 tabular-nums text-ink">{number(row.calmar)}</td><td className="px-3 py-3 tabular-nums text-ink">{pct(row.volatility)}</td><td className="px-3 py-3 tabular-nums text-ink">{pct(row.win)}</td><td className="px-3 py-3 tabular-nums text-ink">{row.tradeCount === null ? "—" : Math.round(row.tradeCount).toLocaleString("zh-CN")}</td>{hasTurnover && <td className="px-3 py-3 tabular-nums text-ink">{row.turnover === null ? "—" : `${number(row.turnover)}×`}</td>}{hasCost && <td className="px-3 py-3 whitespace-nowrap tabular-nums text-ink">{formatCost(row)}</td>}{onExport && <td className="px-3 py-3"><button onClick={() => void onExport(row.id, row.name)} className="inline-flex items-center gap-1 whitespace-nowrap rounded-md border border-edgeDark px-2 py-1 text-[9px] text-mute hover:text-ink"><Download size={10} />交易 CSV</button></td>}</tr>; })}</tbody></table></div></section>;
}

export default function ArenaDetail() {
  const id = useStore((state) => state.currentArenaId);
  const back = useStore((state) => state.backFromArenaDetail);
  const [arena, setArena] = useState<ArenaItem | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    if (!id) return;
    let active = true;
    setLoading(true); setError(null);
    void api.arenaGet(id).then((item) => { if (active) setArena(item); })
      .catch((reason) => { if (active) setError(reason instanceof Error ? reason.message : "无法读取 Arena，请重试。"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [id, revision]);
  const current = arena?.id === id ? arena : null;
  if (current && isArenaWorkspace(current)) return <ArenaWorkspaceDetail key={current.id} initialArena={current} />;
  if (current && !isModelComparisonResult(current.result)) return <LegacyArenaDetail key={current.id} initialArena={current} />;
  return <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper">
    <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-edge bg-card px-5 py-4 sm:px-7"><div className="min-w-0"><button type="button" onClick={back} className="mb-2 inline-flex items-center gap-1.5 text-[12px] text-mute hover:text-ink"><ArrowLeft size={14} />Arena</button><h1 className="break-words font-serif text-[21px] font-semibold text-ink">{current?.name || "模型对比"}</h1></div><button type="button" onClick={() => setRevision((value) => value + 1)} disabled={loading} className="inline-flex items-center gap-2 rounded-lg border border-edge px-3 py-2 text-[12px] text-mute disabled:opacity-50"><RefreshCw size={14} className={cls(loading && "animate-spin")} />刷新结果</button></header>
    <main className="min-h-0 min-w-0 flex-1 overflow-y-auto"><div className="mx-auto w-full max-w-[1400px] px-4 py-5 sm:px-7">{error && <div role="alert" className="mb-4 rounded-xl border border-rise/20 bg-rise/5 px-4 py-3 text-[12px] text-rise">{error}</div>}{current && isModelComparisonResult(current.result) ? <ModelComparisonResults key={current.id} result={current.result} /> : <div className="flex min-h-64 items-center justify-center gap-2 text-[12px] text-mute">{loading && <Loader2 size={17} className="animate-spin" />}{loading ? "正在读取已保存的比较结果…" : "暂无可读取的模型对比结果。"}</div>}</div></main>
  </div>;
}

function LegacyArenaDetail({ initialArena }: { initialArena: ArenaItem }) {
  const currentArenaId = useStore((s) => s.currentArenaId); const back = useStore((s) => s.backFromArenaDetail); const goBacktest = useStore((s) => () => s.setView("backtest-list")); const patchArena = useStore((s) => s.patchArena);
  const [arena, setArena] = useState<ArenaItem | null>(null); const [result, setResult] = useState<ArenaComputeResult | null>(null); const [performance, setPerformance] = useState<Record<string, BTPerformanceResponse>>({}); const [loading, setLoading] = useState(false); const [computing, setComputing] = useState(false); const [error, setError] = useState<string | null>(null); const [tab, setTab] = useState<"overview" | "curves" | "events">("overview");
  const load = async (id: string, preloaded?: ArenaItem) => { setLoading(true); setError(null); try { const item = preloaded ?? await api.arenaGet(id); setArena(item); setResult(item.result ?? null); if (item.result) { const loaded = await Promise.allSettled(item.run_ids.map((runId) => api.btGetPerformance(runId, false))); const next: Record<string, BTPerformanceResponse> = {}; loaded.forEach((x, i) => { if (x.status === "fulfilled" && x.value.status !== "unavailable") next[item.run_ids[i]] = x.value; }); setPerformance(next); } } catch (e) { setError(e instanceof Error ? e.message : "加载失败"); } finally { setLoading(false); } };
  useEffect(() => { if (currentArenaId) { setTab("overview"); void load(currentArenaId, initialArena.id === currentArenaId ? initialArena : undefined); } }, [currentArenaId]);
  const compute = async () => { if (!currentArenaId) return; setComputing(true); setError(null); patchArena(currentArenaId, { status: "computing" }); try { const r = await api.arenaCompute(currentArenaId); setResult(r); setArena((a) => a ? { ...a, result: r, status: "done" } : a); patchArena(currentArenaId, { status: "done", result: r }); const loaded = await Promise.allSettled((arena?.run_ids ?? []).map((id) => api.btGetPerformance(id, false))); const next: Record<string, BTPerformanceResponse> = {}; loaded.forEach((x, i) => { const id = arena?.run_ids[i]; if (id && x.status === "fulfilled" && x.value.status !== "unavailable") next[id] = x.value; }); setPerformance(next); } catch (e) { setError(e instanceof Error ? e.message : "计算失败"); patchArena(currentArenaId, { status: "failed" }); } finally { setComputing(false); } };
  const protocol = asRecord(result?.comparison_protocol); const config = asRecord(arena?.config); const engine = text(config.engine_mode) || text(protocol.engine_mode) || text((result as unknown as Record<string, unknown> | null)?.engine_mode) || "—"; const nature = text(config.result_nature) || text(protocol.result_nature) || text((result as unknown as Record<string, unknown> | null)?.result_nature) || ""; const arenaType = text(result?.arena_type || arena?.arena_type || config.arena_type).toLowerCase(); const forecastArena = ["forecast", "prediction"].includes(arenaType); const unverified = engine === "portfolio" && nature === "unverified"; const exploratory = unverified || Boolean(config.allow_mixed_protocols) || text(config.comparison_mode) === "exploration" || text(protocol.comparison_mode) === "exploration"; const strict = !exploratory && protocol.strict_comparable !== false && Boolean(text(protocol.comparison_protocol_hash) || text(protocol.protocol_hash) || text(arena?.protocol_hash) || text(config.comparison_protocol_hash)); const formal = strict && engine === "portfolio" && (!nature || nature === "simulated_from_real_bars");
  const runIds = result ? Object.keys(result.per_run) : arena?.run_ids ?? [];
  const curveContract = asRecord((result as unknown as Record<string, unknown> | null)?.performance_curves); const curvePrivacy = asRecord(curveContract.privacy); const protectedRunIds = new Set(Array.isArray(curvePrivacy.arena_safe_run_ids) ? curvePrivacy.arena_safe_run_ids.map(text) : []);
  const rows: ArenaRow[] = result ? runIds.map((id) => { const fullPerf = performance[id]; const perf = protectedRunIds.has(id) ? undefined : fullPerf; const info = result.per_run[id]; const ret = runMetric(result, id, perf, ["total_return", "net_total_return", "cumulative_return", "return"]); const dd = runMetric(result, id, perf, ["max_drawdown", "drawdown"]); const sharpe = runMetric(result, id, perf, ["sharpe_ratio", "sharpe"]); const annual = runMetric(result, id, perf, ["annualized_return", "annual_return"]); const win = runMetric(result, id, perf, ["win_rate"]); const absoluteCost = getPerfMetric(perf, ["total_cost", "total_cost_amount_proxy"]); return { id, name: info?.display_name || id, ret, dd, sharpe, annual, calmar: getPerfMetric(perf, ["calmar_ratio"]), volatility: getPerfMetric(perf, ["annualized_volatility"]), win, tradeCount: getPerfMetric(perf, ["trade_count", "n_trades"]), turnover: getPerfMetric(perf, ["total_turnover"]), totalCost: absoluteCost, totalCostRate: absoluteCost === null ? getPerfMetric(perf, ["total_cost_rate_sum"]) : null, currency: text(perf?.currency) || "CNY", info: info ?? { run_id: id, display_name: id, runner: "—", prompt_variant: "", model_version: "", status: "unknown", done_events: 0, total_events: 0, metrics: {} } }; }) : [];
  const alignment = asRecord(curveContract.alignment);
  const alignedMetrics = asRecord(curveContract.aligned_metrics);
  const alignedRanking = asRecord(curveContract.aligned_ranking);
  const alignedRankingAllowed = !forecastArena && alignment.ranking_allowed === true;
  const totalReturnRanking = Array.isArray(alignedRanking.total_return) ? alignedRanking.total_return as Array<Record<string, unknown>> : [];
  const alignedOrder = new Map(totalReturnRanking.map((item, index) => [text(item.run_id), n(item.rank) ?? index + 1]));
  const comparisonRows: ArenaRow[] = alignedRankingAllowed
    ? rows.map((row) => {
        const metrics = asRecord(alignedMetrics[row.id]);
        return {
          ...row,
          ret: recordMetric(metrics, ["total_return"]),
          annual: recordMetric(metrics, ["annualized_return"]),
          dd: recordMetric(metrics, ["max_drawdown"]),
          sharpe: recordMetric(metrics, ["sharpe_ratio"]),
          calmar: recordMetric(metrics, ["calmar_ratio"]),
          volatility: recordMetric(metrics, ["annualized_volatility"]),
        };
      }).sort((a, b) => (alignedOrder.get(a.id) ?? Number.MAX_SAFE_INTEGER) - (alignedOrder.get(b.id) ?? Number.MAX_SAFE_INTEGER))
    : rows;
  const curveModel = useMemo(() => {
    const raw = (result as unknown as Record<string, unknown> | null)?.performance_curves; const series = Array.isArray(asRecord(raw).series) ? asRecord(raw).series as Array<Record<string, unknown>> : [];
    const privacy = asRecord(asRecord(raw).privacy); const safeIds = new Set(Array.isArray(privacy.arena_safe_run_ids) ? privacy.arena_safe_run_ids.map(text) : []);
    const candidates: Curve[] = series.length ? series.map((s) => {
      const runId = text(s.run_id); const perfPoints = performance[runId]?.equity_curve ?? []; const timeByIndex = new Map<number, string>(); perfPoints.forEach((point, index) => { const timestamp = curveTimestamp(point); if (timestamp) timeByIndex.set(n(point.index) ?? index, timestamp); }); const protectedCurve = Boolean(s.privacy_aggregated) || safeIds.has(runId);
      const points = (Array.isArray(s.points) ? s.points : []).map((rawPoint, fallbackIndex) => { const point = asRecord(rawPoint); const index = n(point.index) ?? fallbackIndex; const value = n(point.net_value); const timestamp = protectedCurve ? "" : curveTimestamp(point) || timeByIndex.get(index) || ""; const segment = n(point.segment) ?? n(point.segment_id) ?? 0; return value === null ? null : { x: index, y: value, index, timestamp: timestamp || undefined, segment }; }).filter((point): point is CurvePoint => point !== null);
      return { runId, name: text(s.display_name) || text(s.name) || runId, points };
    }) : runIds.map((id) => ({ runId: id, name: result?.per_run[id]?.display_name || id, points: curvePoints(performance[id]?.equity_curve) }));
    const privacyRedacted = safeIds.size > 0 || series.some((item) => Boolean(item.privacy_aggregated));
    // 事件净值的 index=0 是首个事件发生前的基准点，后端不会为它补造时间；
    // 仅把它锚定到首个真实事件时间，以便日期轴从真实评测起点开始。
    const timestampCompleted = candidates.map((curve) => { const firstRealTimestamp = curve.points.find((point) => timestampValue(point.timestamp || "") !== null)?.timestamp; return { ...curve, points: curve.points.map((point) => ({ ...point, timestamp: !point.timestamp && point.index === 0 ? firstRealTimestamp : point.timestamp })) }; });
    const dated = !privacyRedacted && timestampCompleted.length > 0 && timestampCompleted.every((curve) => curve.points.length > 1 && curve.points.every((point) => timestampValue(point.timestamp || "") !== null));
    return { privacyRedacted, dateAxis: dated, curves: timestampCompleted.map((curve) => ({ ...curve, points: curve.points.map((point) => ({ ...point, x: dated ? timestampValue(point.timestamp || "") ?? point.index : point.index })) })) };
  }, [result, performance, runIds.join("|")]);
  const curves = curveModel.curves;
  const scope: MeasurementScope = useMemo(() => {
    const alignment = asRecord(curveContract.alignment); const requested = asRecord(alignment.requested); const frequencies = runIds.map((id) => text(asRecord(performance[id]?.dataset).frequency)).filter(Boolean); const uniqueFrequency = [...new Set(frequencies)]; const configuredFrequency = text(alignment.frequency) || text(requested.frequency); const frequency = configuredFrequency || (uniqueFrequency.length === 1 ? uniqueFrequency[0] : uniqueFrequency.length ? "混合频率" : ""); const counts = curves.filter((curve) => curve.points.length).map((curve) => curve.points.length); const uniqueCounts = [...new Set(counts)]; const observations = uniqueCounts.length === 1 ? uniqueCounts[0].toLocaleString("zh-CN") : counts.length ? `${Math.min(...counts).toLocaleString("zh-CN")}–${Math.max(...counts).toLocaleString("zh-CN")}` : "—"; const timestamps = curveModel.dateAxis ? curves.flatMap((curve) => curve.points.map((point) => point.timestamp || "").filter(Boolean)) : []; const sorted = timestamps.sort((a, b) => (timestampValue(a) ?? 0) - (timestampValue(b) ?? 0)); return { start: text(alignment.window_start) || sorted[0] || "", end: text(alignment.window_end) || sorted[sorted.length - 1] || "", frequency, observations, dateAxis: curveModel.dateAxis, privacyRedacted: curveModel.privacyRedacted, alignmentMode: text(alignment.mode) || "legacy" };
  }, [curveContract, curveModel, curves, performance, runIds.join("|")]);
  const eventRelevant = rows.some((r) => { const type = text((arena?.config as Record<string, unknown> | null)?.strategy_type) || text(performance[r.id]?.strategy_type); return type === "event" || (result ? eventAccuracy(result, r.id, performance[r.id]) !== null : false); });
  const exportTrades = async (id: string, label: string) => { try { downloadCsv(await api.btDownloadTradesCsv(id), `${label.replace(/[^\w\-\u4e00-\u9fa5]+/g, "_")}-trades.csv`); } catch (e) { setError(e instanceof Error ? e.message : "导出失败"); } };
  return <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper"><header className="border-b border-edgeDark/70 bg-card/85 px-6 py-3.5"><div className="flex items-center justify-between gap-3"><div className="min-w-0"><div className="mb-1 flex items-center gap-1 text-[10px] text-mute"><button onClick={back} className="inline-flex items-center gap-1 hover:text-ink"><ArrowLeft size={12} />Arena</button><ChevronRight size={11} /><button onClick={goBacktest} className="hover:text-ink">回测中心</button></div><div className="flex min-w-0 items-center gap-2"><h1 className="truncate font-serif text-[17px] font-semibold text-ink">{arena?.name || "Arena"}</h1><span className={cls("shrink-0 rounded-full px-2 py-0.5 text-[9px] font-semibold", exploratory ? "bg-amber-500/10 text-amber-700" : formal ? "bg-jade/10 text-jade" : "bg-brand/10 text-brand")}>{exploratory ? "探索对照 · 非公平排名" : forecastArena ? "预测能力" : formal ? "正式组合回测" : "事件收益代理"}</span></div></div><div className="flex shrink-0 gap-2"><button onClick={() => currentArenaId && void load(currentArenaId)} className="rounded-lg border border-edgeDark p-2 text-mute"><RefreshCw size={13} className={cls(loading && "animate-spin")} /></button><button onClick={() => void compute()} disabled={computing || !currentArenaId} className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-3.5 py-2 text-[10.5px] font-semibold text-white disabled:opacity-50">{computing && <Loader2 size={12} className="animate-spin" />}{result ? "重新计算" : "计算 Arena"}</button></div></div></header>
    <div className="flex items-center justify-between border-b border-edgeDark/70 bg-edge/20 px-6 py-2"><div className="flex gap-1"><button onClick={() => setTab("overview")} className={cls("rounded-md px-3 py-1.5 text-[10.5px] font-medium", tab === "overview" ? "bg-card text-ink shadow-card" : "text-mute")}>{exploratory ? "探索概览" : forecastArena ? "预测排名" : "排名概览"}</button>{!forecastArena && <button onClick={() => setTab("curves")} className={cls("rounded-md px-3 py-1.5 text-[10.5px] font-medium", tab === "curves" ? "bg-card text-ink shadow-card" : "text-mute")}>曲线与风险</button>}{eventRelevant && <button onClick={() => setTab("events")} className={cls("rounded-md px-3 py-1.5 text-[10.5px] font-medium", tab === "events" ? "bg-card text-ink shadow-card" : "text-mute")}>事件方向准确率</button>}</div><div className="hidden items-center gap-2 text-[9.5px] text-mute md:flex">{strict ? <ShieldCheck size={12} className="text-jade" /> : <ShieldAlert size={12} className="text-amber-700" />}{strict ? "公平口径一致" : exploratory ? "探索模式 · 口径可能不同" : "协议未完整签名"}<span>·</span><span>版本 {short(text(config.dataset_version) || text(protocol.dataset_version))}</span></div></div>
    <main className="min-h-0 flex-1 overflow-y-auto"><div className="mx-auto max-w-[1400px] px-6 py-5">{loading && !arena ? <div className="grid h-64 place-items-center text-[11px] text-mute"><Loader2 size={16} className="animate-spin" /></div> : <>{error && <div className="mb-4 flex items-center justify-between rounded-lg border border-rise/25 bg-rise/5 px-3 py-2 text-[10.5px] text-rise"><span>{error}</span><button onClick={() => setError(null)}>×</button></div>}{!result ? <div className="mx-auto mt-14 max-w-xl rounded-2xl border border-dashed border-edgeDark bg-card px-8 py-14 text-center"><Gauge className="mx-auto text-mute" size={24} /><h2 className="mt-4 font-serif text-[17px] font-semibold text-ink">尚未生成比较结果</h2><p className="mt-2 text-[11px] leading-relaxed text-mute">系统会先核验数据与执行协议，再计算当前赛道的真实指标；没有有效结果时不会生成示意数据。</p><button onClick={() => void compute()} className="mt-5 rounded-lg bg-ink px-4 py-2 text-[11px] font-semibold text-white">计算 Arena</button></div> : <><section className={cls("mb-5 flex items-start gap-3 rounded-xl border p-3.5", strict ? "border-jade/20 bg-jade/5" : exploratory ? "border-amber-500/25 bg-amber-50" : "border-rise/25 bg-rise/5")}><span className={cls("grid h-8 w-8 place-items-center rounded-lg", strict ? "bg-jade/10 text-jade" : exploratory ? "bg-amber-500/10 text-amber-700" : "bg-rise/10 text-rise")}>{strict ? <ShieldCheck size={16} /> : <ShieldAlert size={15} />}</span><div><div className={cls("text-[11.5px] font-semibold", strict ? "text-jade" : exploratory ? "text-amber-800" : "text-rise")}>{strict ? "公平对比协议已锁定" : exploratory ? "探索对照 · 不构成公平排名" : "比较结果缺少完整协议签名"}</div><p className="mt-1 text-[10px] leading-relaxed text-mute">{strict ? `数据版本 ${short(text(config.dataset_version) || text(protocol.dataset_version))} · 协议 ${short(text(arena?.protocol_hash) || text(protocol.comparison_protocol_hash) || text(protocol.protocol_hash) || text(config.comparison_protocol_hash))} · 引擎 ${engine}` : exploratory ? `这些 Run 可以来自不同历史长度或协议；原始指标保留，表现曲线只裁剪或降采样真实观测。差异：${Array.isArray(protocol.mismatched_fields) ? protocol.mismatched_fields.map(text).slice(0, 5).join("、") : "见各 Run 协议"}` : text(protocol.reason) || "这是历史兼容结果，仅作探索性参考；不可与正式结果混排。"}</p></div></section>{tab === "overview" && (forecastArena ? <ForecastOverview result={result} performance={performance} /> : <Overview rows={rows} comparisonRows={comparisonRows} alignedRankingUsed={alignedRankingAllowed} result={result} contract={curveContract} onExport={exportTrades} />)}{tab === "curves" && !forecastArena && <div className="space-y-4"><ScopeStrip scope={scope} /><AlignmentSummary contract={curveContract} rows={comparisonRows} /><OriginalAlignedTable contract={curveContract} rows={comparisonRows} /><MultiCurve curves={curves} title="累计净值" dateAxis={scope.dateAxis} frequency={scope.frequency} /><MultiCurve curves={curves} title="回撤曲线" drawdown dateAxis={scope.dateAxis} frequency={scope.frequency} /><Scatter rows={comparisonRows.map((r) => ({ runId: r.id, name: r.name, returnValue: r.ret, drawdown: r.dd ?? maxDrawdown(curves.find((c) => c.runId === r.id)?.points ?? []) }))} /></div>}{tab === "events" && eventRelevant && <EventPanel rows={rows} result={result} performance={performance} />}</>}</>}</div></main></div>;
}

function Overview({ rows, comparisonRows, alignedRankingUsed, result, contract, onExport }: { rows: ArenaRow[]; comparisonRows: ArenaRow[]; alignedRankingUsed: boolean; result: ArenaComputeResult; contract: Record<string, unknown>; onExport: (id: string, label: string) => void }) {
  const comparison = asRecord(result.comparison_protocol);
  const exploratory = comparison.strict_comparable === false || text(comparison.comparison_mode) === "exploration";
  // 收益严格按降序排名；回撤统一转为正幅度后取最小值。
  const ranking = [...comparisonRows].sort((a,b) => (b.ret ?? -Infinity) - (a.ret ?? -Infinity)); const leader = ranking[0];
  const drawdowns = comparisonRows.map((r) => drawdownMagnitude(r.dd)).filter((v): v is number => v !== null); const bestDrawdown = drawdowns.length ? Math.min(...drawdowns) : null;
  const basis = alignedRankingUsed ? "对齐" : exploratory ? "探索" : "原始";
  return <div className="space-y-5"><div className="grid gap-3 md:grid-cols-4"><Metric label="策略数量" value={String(rows.length)} icon={<Database size={15} />} /><Metric label={exploratory ? "当前排序首位" : `${basis}口径领先策略`} value={leader?.name || "—"} icon={<Medal size={15} />} /><Metric label={exploratory ? "当前最高收益（仅排序）" : `${basis}口径最高累计收益`} value={pct(leader?.ret, true)} tone="jade" icon={<TrendingUp size={15} />} /><Metric label={exploratory ? "当前最小回撤（仅排序）" : `${basis}口径最小最大回撤`} value={pct(bestDrawdown)} tone="brand" icon={<TrendingDown size={15} />} /></div><AlignmentSummary contract={contract} rows={comparisonRows} /><OriginalAlignedTable contract={contract} rows={comparisonRows} /><MetricMatrix rows={rows} title={exploratory ? "原始周期指标（仅供探索排序）" : alignedRankingUsed ? "原始周期指标（对齐排名以其上方对齐列为准）" : "原始周期策略排名与专业指标"} onExport={onExport} exploratory={exploratory || alignedRankingUsed} /></div>;
}
function Metric({ label, value, icon, tone = "ink" }: { label: string; value: string; icon: React.ReactNode; tone?: "ink" | "jade" | "brand" }) { return <div className="rounded-xl border border-edgeDark/70 bg-card p-3.5 shadow-card"><div className="flex items-center justify-between text-[9.5px] text-faint"><span>{label}</span><span className={tone === "jade" ? "text-jade" : tone === "brand" ? "text-brand" : "text-mute"}>{icon}</span></div><div className="mt-2 truncate text-[16px] font-semibold tabular-nums text-ink" title={value}>{value}</div></div>; }
function ForecastOverview({ result, performance }: { result: ArenaComputeResult; performance: Record<string, BTPerformanceResponse> }) {
  const runIds = Object.keys(result.per_run);
  const requested = result.selected_metric_ids?.length ? result.selected_metric_ids : Object.keys(result.metric_defs || {});
  const available = requested.filter((id) => runIds.some((runId) => metricValue(result.per_run[runId]?.metrics?.[id]) !== null));
  // Keep the numeric forecast score visible even when many event metrics exist.
  const metricIds = [...available.filter((id) => id === "return_forecast"), ...available.filter((id) => id !== "return_forecast")].slice(0, 8);
  const primaryMetric = metricIds.find((id) => id.includes("acc")) || metricIds[0];
  const metricLabel = (id: string) => id === "return_forecast" ? "收益率预测 MAE" : result.metric_defs?.[id]?.display_name || id;
  const formatMetric = (id: string, value: number | null) => {
    if (value === null) return "—";
    if (id === "return_forecast") return `${number(value, 3)} 个百分点`;
    const percentLike = id.includes("acc") || id.includes("rate") || id.includes("ratio") || id.includes("coverage");
    return percentLike ? pct(value) : number(value);
  };
  const ranked = primaryMetric && Array.isArray(result.ranking?.[primaryMetric]) ? result.ranking[primaryMetric] : [];
  const lowerIsBetter = primaryMetric === "return_forecast" || result.metric_defs?.[primaryMetric]?.higher_is_better === false;
  const scored = primaryMetric ? runIds.flatMap((id) => {
    const value = metricValue(result.per_run[id]?.metrics?.[primaryMetric]);
    return value === null ? [] : [{ id, value }];
  }).sort((a, b) => lowerIsBetter ? a.value - b.value : b.value - a.value) : [];
  const leaderId = ranked.find((item) => item.rank === 1)?.run_id || scored[0]?.id;
  const leader = leaderId ? result.per_run[leaderId] : undefined;
  const eventRows = runIds.filter((id) => eventAccuracy(result, id, performance[id]) !== null);
  return <div className="space-y-5">
    <div className="grid gap-3 md:grid-cols-3">
      <Metric label="参评模型 / 策略" value={String(runIds.length)} icon={<Database size={15} />} />
      <Metric label="预测领先模型 / 策略" value={leader?.display_name || "—"} icon={<Medal size={15} />} />
      <Metric label={primaryMetric ? `${metricLabel(primaryMetric)}（${lowerIsBetter ? "越低越好" : "越高越好"}）` : "预测成绩"} value={leader && primaryMetric ? formatMetric(primaryMetric, metricValue(leader.metrics?.[primaryMetric])) : "—"} tone="jade" icon={<TrendingUp size={15} />} />
    </div>
    <section className="overflow-hidden rounded-xl border border-edgeDark/70 bg-card shadow-card">
      <div className="border-b border-edgeDark/70 px-4 py-3"><h2 className="text-[13px] font-semibold text-ink">预测指标对比</h2><p className="mt-0.5 text-[10px] text-mute">读取冻结结果中适用的预测指标。收益率误差以百分点计，越低越好；公平比较要求数据版本、预测周期与评价口径一致。</p></div>
      <div className="overflow-x-auto"><table className="min-w-[780px] w-full text-left text-[10px]">
        <thead className="bg-edge/40 text-[8.5px] uppercase tracking-wide text-faint"><tr><th className="px-4 py-3">模型 / 策略</th>{metricIds.map((id) => <th key={id} className="px-3 py-3">{metricLabel(id)}</th>)}</tr></thead>
        <tbody>{runIds.map((runId) => {
          const info = result.per_run[runId];
          return <tr key={runId} className="border-t border-edge/70"><td className="px-4 py-3"><div className="font-semibold text-ink">{info.display_name}</div><div className="mt-0.5 text-[8.5px] text-faint">{info.model_version || info.runner}</div></td>{metricIds.map((id) => {
            const item = info.metrics?.[id];
            const meta = asRecord(item?.meta);
            return <td key={id} className="px-3 py-3 tabular-nums text-ink"><div>{formatMetric(id, metricValue(item))}</div>{id === "return_forecast" && <div className="mt-1 space-y-0.5 whitespace-nowrap text-[8.5px] text-faint"><div>RMSE {formatMetric(id, n(meta.rmse_pct))} · MedAE {formatMetric(id, n(meta.median_abs_error_pct))} · P90 {formatMetric(id, n(meta.p90_abs_error_pct))}</div><div>方向 {pct(n(meta.direction_accuracy))} · IC {number(n(meta.pearson_ic), 3)} · Rank IC {number(n(meta.spearman_rank_ic), 3)}</div><div>R² {number(n(meta.r_squared), 3)} · 零基准技能 {pct(n(meta.skill_score_vs_zero))} · Bias {formatMetric(id, n(meta.bias_pct))}</div><div>评估样本 {number(n(meta.evaluated_count), 0)} · 覆盖率 {pct(n(meta.coverage))}</div></div>}</td>;
          })}</tr>;
        })}</tbody>
      </table></div>
      {metricIds.length === 0 && <p className="px-4 py-6 text-[10px] text-mute">暂无可评分的预测指标。</p>}
    </section>
    {eventRows.length > 0 && <EventPanel rows={eventRows.map((id) => ({ id, name: result.per_run[id]?.display_name || id }))} result={result} performance={performance} />}
  </div>;
}
function EventPanel({ rows, result, performance }: { rows: Array<{ id: string; name: string }>; result: ArenaComputeResult; performance: Record<string, BTPerformanceResponse> }) { const eventRows = rows.filter((r) => text(performance[r.id]?.strategy_type) === "event" || eventAccuracy(result, r.id, performance[r.id]) !== null).map((r) => { const correctness = asRecord(performance[r.id]?.correctness); return { ...r, accuracy: eventAccuracy(result, r.id, performance[r.id]), evaluated: n(correctness.scored) ?? getPerfMetric(performance[r.id], ["evaluated_events", "n_events_in_protocol"]) }; }); if (!eventRows.some((r) => r.accuracy !== null)) return <NoData text="当前结果没有可用的事件方向评分；数值收益率预测请查看预测指标中的误差。" />; return <section className="rounded-xl border border-edgeDark/70 bg-card shadow-card"><div className="border-b border-edgeDark/70 px-4 py-3"><h2 className="text-[13px] font-semibold text-ink">事件方向准确率</h2><p className="mt-0.5 text-[10px] text-mute">仅展示事件方向评分；数值收益率预测独立按误差评测。</p></div><div className="divide-y divide-edge">{eventRows.map((r) => <div key={r.id} className="flex items-center justify-between gap-4 px-4 py-3"><div><div className="text-[11px] font-semibold text-ink">{r.name}</div><div className="mt-0.5 text-[9px] text-mute">评测样本 {r.evaluated ?? "—"}</div></div><div className="text-[16px] font-semibold tabular-nums text-ink">{pct(r.accuracy)}</div></div>)}</div></section>; }
