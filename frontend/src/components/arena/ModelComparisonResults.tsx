import { useEffect, useMemo, useRef, useState } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { AlertTriangle, BarChart3, CandlestickChart, CheckCircle2, ChevronDown, Info } from "lucide-react";
import type { KlinePayload } from "../../types";
import KlineChart, { type KlineTradeMarker } from "../KlineChart";
import { cls } from "../../utils";
import { comparisonKindLabel, type ComparisonCurvePoint, type ComparisonModel, type EventComparisonDetail, type ModelComparisonResult } from "./arenaComparison";
import { ArenaFieldArt } from "./ArenaMotif";

echarts.use([LineChart, GridComponent, LegendComponent, TooltipComponent, DataZoomComponent, CanvasRenderer]);

const COLORS = ["#B45309", "#0F766E", "#62758A", "#A65E53", "#7B7188", "#7A845A", "#B18C55", "#466366"];
const NO_MARKERS: KlineTradeMarker[] = [];
const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const pct = (value: number | null | undefined) => finite(value) ? `${value > 0 ? "+" : ""}${(value * 100).toFixed(2)}%` : "暂无数据";
const ratioPct = (value: number | null | undefined) => finite(value) ? `${(value * 100).toFixed(2)}%` : "暂无数据";
const numeric = (value: number | null | undefined, digits = 2) => finite(value) ? value.toLocaleString("zh-CN", { maximumFractionDigits: digits }) : "暂无数据";
const elapsed = (value: number | null | undefined) => !finite(value) ? "暂无数据" : value >= 60 ? `${Math.floor(value / 60)} 分 ${(value % 60).toFixed(1)} 秒` : `${value.toFixed(1)} 秒`;
const hasSignals = (model: ComparisonModel) => model.coverage?.signal_count > 0;
const frequency = (value: string) => ({ "1m": "1 分钟", "5m": "5 分钟", "15m": "15 分钟", "30m": "30 分钟", "60m": "60 分钟", "1h": "1 小时", "1d": "日线", "1w": "周线" } as Record<string, string>)[value] ?? value;
const stamp = (value: string | undefined | null) => value ? value.replace("T", " ").slice(0, 19) : "暂无数据";

type PlotSeries = { name: string; id: string; color: string; points: ComparisonCurvePoint[]; benchmark?: boolean };

function ComparisonChart({ series, drawdown, redacted }: { series: PlotSeries[]; drawdown: boolean; redacted: boolean }) {
  const container = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!container.current || !series.length) return;
    let dated = !redacted;
    const prepared = series.map((item) => {
      let peak = 1;
      return {
        ...item,
        points: item.points.map((point, index) => {
          const time = point.timestamp ? Date.parse(point.timestamp) : Number.NaN;
          if (!Number.isFinite(time)) dated = false;
          const level = finite(point.net_value) && point.net_value >= 0 ? point.net_value : null;
          if (level !== null) peak = Math.max(peak, level);
          const dd = finite(point.drawdown) ? -Math.abs(point.drawdown) : level === null ? null : level / peak - 1;
          const value = drawdown ? dd === null ? null : dd * 100 : level === null ? null : (level - 1) * 100;
          return { index, time, value };
        }),
      };
    });
    const chart = echarts.init(container.current);
    chart.setOption({
      animation: false,
      textStyle: { fontFamily: "inherit" },
      legend: { type: "scroll", top: 8, left: 16, right: 16, textStyle: { fontSize: 11, color: "#6B6862" }, icon: "roundRect", itemWidth: 13, itemHeight: 4 },
      tooltip: { trigger: "axis", renderMode: "richText", textStyle: { fontSize: 11 }, valueFormatter: (raw: unknown) => { const value = Array.isArray(raw) ? raw[1] : raw; return finite(value) ? `${value > 0 ? "+" : ""}${value.toFixed(3)}%` : "无观测" } },
      grid: { left: 65, right: 22, top: 56, bottom: 75 },
      xAxis: {
        type: dated ? "time" : "value", minInterval: dated ? undefined : 1,
        axisLine: { lineStyle: { color: "#E8E5E0" } }, axisTick: { show: false },
        axisLabel: { hideOverlap: true, color: "#6B6862", fontSize: 10 }, splitLine: { show: false },
      },
      yAxis: { type: "value", ...(drawdown ? { max: 0 } : {}), axisLabel: { color: "#6B6862", fontSize: 10, formatter: (value: number) => `${Number(value.toFixed(2))}%` }, splitLine: { lineStyle: { color: "#F0EDE8" } } },
      dataZoom: [
        { type: "inside", start: 0, end: 100 },
        { type: "slider", start: 0, end: 100, bottom: 9, height: 19, borderColor: "#E8E5E0", fillerColor: "rgba(180,83,9,.08)", textStyle: { fontSize: 9 } },
      ],
      series: prepared.map((item) => ({
        id: item.id, name: item.name, type: "line", sampling: "lttb", showSymbol: false, connectNulls: false,
        data: item.points.map((point) => [dated ? point.time : point.index, point.value]),
        lineStyle: { width: item.benchmark ? 1.5 : 2, color: item.color, type: item.benchmark ? "dashed" : "solid" },
        itemStyle: { color: item.color },
      })),
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(container.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [series, drawdown, redacted]);
  if (!series.length) return <p className="grid min-h-64 place-items-center px-5 text-center text-[12px] text-mute">没有可展示的有效曲线。</p>;
  return <div ref={container} className="w-full" style={{ height: 365 }} role="img" aria-label={drawdown ? "各模型按统一规则模拟的回撤对比" : "各模型按统一规则模拟的累计收益对比"} />;
}

function marketKline(result: ModelComparisonResult): KlinePayload | null {
  if (result.privacy?.market_redacted || result.privacy?.timestamps_redacted) return null;
  const payload: KlinePayload = { symbol: result.market.symbol, dates: [], ohlc: [], volumes: [] };
  let completeVolumes = true;
  for (const bar of result.bars ?? []) {
    if (!bar.timestamp || ![bar.open, bar.high, bar.low, bar.close].every((value) => finite(value) && value > 0)
      || bar.high < Math.max(bar.open, bar.close) || bar.low > Math.min(bar.open, bar.close)) continue;
    payload.dates.push(bar.timestamp);
    payload.ohlc.push([bar.open, bar.close, bar.low, bar.high]);
    if (finite(bar.volume) && bar.volume >= 0) payload.volumes.push(bar.volume);
    else completeVolumes = false;
  }
  if (!completeVolumes) payload.volumes = [];
  return payload.dates.length ? payload : null;
}

function PredictionQuality({ models }: { models: ComparisonModel[] }) {
  if (!models.some((model) => model.prediction_quality)) return null;
  return <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card">
    <div className="border-b border-edge px-4 py-3.5"><h3 className="text-[13px] font-semibold text-ink">预测准确性 · 各模型的有效样本</h3><p className="mt-1 text-[12px] leading-relaxed text-mute">方向准确率衡量判断正确的比例，MAE 与 RMSE 衡量收益率数值偏差。各模型的可评估样本可能不同；方向口径与样本数一并展示，不按准确率混合排名。</p></div>
    <div className="overflow-x-auto"><table className="w-full min-w-[1080px] text-left text-[12px]"><thead className="bg-paper text-mute"><tr>{["模型", "方向准确率", "正确 / 方向样本", "准确率区间（95%）", "MAE", "RMSE", "平均偏差", "数值样本", "待兑现"].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr></thead><tbody className="divide-y divide-edge">{models.map((model) => {
      const quality = model.prediction_quality;
      const interval = quality?.accuracy_ci95;
      const intervalValid = interval && interval.length === 2 && interval.every(finite);
      const directionEvaluated = finite(quality?.n_direction) && quality!.n_direction > 0;
      const numericEvaluated = finite(quality?.n_numeric) && quality!.n_numeric > 0;
      return <tr key={model.run_id}><td className="max-w-[230px] px-4 py-3.5"><p className="break-words font-semibold text-ink">{model.name}</p><p className="mt-1 text-[11px] text-mute">{comparisonKindLabel(model.model_kind)}</p></td><td className="px-4 py-3.5"><p className="font-mono">{directionEvaluated ? ratioPct(quality?.directional_accuracy) : "暂无数据"}</p><p className="mt-1 max-w-[160px] text-[11px] leading-relaxed text-mute">{quality?.direction_label || (quality?.direction_basis === "market_excess" ? "事件相对基准方向" : quality?.direction_basis === "asset_return" ? "标的涨跌" : "方向口径未提供")}</p></td><td className="px-4 py-3.5 font-mono">{quality && finite(quality.n_direction) ? `${numeric(quality.correct_direction, 0)} / ${numeric(quality.n_direction, 0)}` : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{directionEvaluated && intervalValid ? `${ratioPct(interval[0])}–${ratioPct(interval[1])}` : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{numericEvaluated ? numeric(quality?.mae_pct, 3) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{numericEvaluated ? numeric(quality?.rmse_pct, 3) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{numericEvaluated ? numeric(quality?.bias_pct, 3) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{numeric(quality?.n_numeric, 0)}</td><td className="px-4 py-3.5 font-mono">{numeric(quality?.pending_count, 0)}</td></tr>;
    })}</tbody></table></div>
    <p className="border-t border-edge px-4 py-3 text-[11px] leading-relaxed text-mute">MAE、RMSE、平均偏差的单位为百分点；平均偏差为预测减实际。准确率区间越宽，当前样本的不确定性越大；单个事件不能代表长期能力。尚未到期或缺少真值的预测不计为错误。</p>
  </section>;
}

function FinancialMetrics({ models }: { models: ComparisonModel[] }) {
  const singleEvent = models.some((model) => model.metrics.annualization_status === "single_event");
  return <details className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card"><summary className="cursor-pointer px-4 py-3.5 text-[13px] font-semibold text-ink">更多金融指标 · 年化、风险与交易效率</summary><p className="border-t border-edge px-4 py-3 text-[12px] leading-relaxed text-mute">年化收益与波动衡量收益水平和波动，夏普与卡玛衡量风险调整后的表现。样本不足时显示“暂无数据”；短区间的年化指标应结合实际收益、回撤和样本长度阅读。{singleEvent && <span className="mt-1.5 block font-medium text-ink">本次是单事件测试，只展示实际累计收益与回撤，不外推年化收益、年化波动、夏普或卡玛。</span>}</p><div className="overflow-x-auto"><table className="w-full min-w-[1000px] text-left text-[12px]"><thead className="bg-paper text-mute"><tr>{["模型", "年化收益", "年化波动", "夏普比率", "卡玛比率", "交易胜率", "累计换手", "交易次数", "模拟成本"].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr></thead><tbody className="divide-y divide-edge">{models.map((model) => <tr key={model.run_id}><td className="max-w-[230px] break-words px-4 py-3.5 font-semibold text-ink">{model.name}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? pct(model.metrics.annualized_return) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? ratioPct(model.metrics.annualized_volatility) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? numeric(model.metrics.sharpe_ratio, 3) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? numeric(model.metrics.calmar_ratio, 3) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? ratioPct(model.metrics.win_rate) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? ratioPct(model.metrics.total_turnover) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? numeric(model.metrics.trade_count, 0) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? numeric(model.metrics.total_cost) : "暂无数据"}</td></tr>)}</tbody></table></div></details>;
}

function ExecutionMetrics({ models }: { models: ComparisonModel[] }) {
  if (!models.some((model) => model.execution)) return null;
  const hasTokens = models.some((model) => finite(model.execution?.total_tokens));
  const hasFailures = models.some((model) => finite(model.execution?.failed_items));
  return <details className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card"><summary className="cursor-pointer px-4 py-3.5 text-[13px] font-semibold text-ink">运行效率</summary><p className="border-t border-edge px-4 py-3 text-[12px] text-mute">展示本次实际记录的处理时间和完成量，耗时包含模型调用与相关处理。</p><div className="overflow-x-auto"><table className="min-w-[520px] w-full text-left text-[12px]"><thead className="bg-paper text-mute"><tr>{["模型", "耗时", "完成 / 总预测", ...(hasFailures ? ["失败预测"] : []), ...(hasTokens ? ["实际 tokens"] : [])].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr></thead><tbody className="divide-y divide-edge">{models.map((model) => <tr key={model.run_id}><td className="max-w-[260px] break-words px-4 py-3.5 font-semibold text-ink">{model.name}</td><td className="px-4 py-3.5 font-mono">{elapsed(model.execution?.elapsed_seconds)}</td><td className="px-4 py-3.5 font-mono">{finite(model.execution?.items_done) && finite(model.execution?.items_total) ? `${numeric(model.execution!.items_done, 0)} / ${numeric(model.execution!.items_total, 0)}` : "暂无数据"}</td>{hasFailures && <td className="px-4 py-3.5 font-mono">{numeric(model.execution?.failed_items, 0)}</td>}{hasTokens && <td className="px-4 py-3.5 font-mono">{numeric(model.execution?.total_tokens, 0)}</td>}</tr>)}</tbody></table></div></details>;
}

const directionLabel = (value: string | null | undefined) => ({ up: "看涨", down: "看跌", neutral: "中性" } as Record<string, string>)[value ?? ""] ?? "缺失";
const directionTone = (value: string | null | undefined) => value === "up" ? "border-rise/20 bg-rise/5 text-rise" : value === "down" ? "border-fall/20 bg-fall/5 text-fall" : value === "neutral" ? "border-edge bg-paper text-mute" : "border-amber-200 bg-amber-50 text-amber-800";
const returnTone = (value: number | null | undefined) => !finite(value) ? "text-faint" : value > 0 ? "text-fall" : value < 0 ? "text-rise" : "text-mute";
const returnDirection = (value: number | null | undefined) => !finite(value) ? null : value > 0 ? "up" : value < 0 ? "down" : "neutral";

function EventMetric({ label, value, tone = "text-ink" }: { label: string; value: string; tone?: string }) {
  return <div className="min-w-0 rounded-lg border border-edge bg-paper/60 px-3 py-2.5">
    <p className="text-[9.5px] text-faint">{label}</p>
    <p className={cls("mt-1 truncate font-mono text-[14px] font-semibold", tone)}>{value}</p>
  </div>;
}

function EventRecord({ detail, index }: { detail: EventComparisonDetail; index: number }) {
  const prediction = detail.prediction ?? detail.direction;
  const actual = finite(detail.asset_return) ? detail.asset_return : detail.actual_return;
  const actualDirection = returnDirection(actual) ?? detail.actual_direction;
  const directional = detail.net_directional_return ?? detail.directional_return;
  return <details className="group rounded-xl border border-edge bg-card open:border-brand/25 open:shadow-card">
    <summary className="cursor-pointer list-none px-3.5 py-3 [&::-webkit-details-marker]:hidden">
      <div className="grid items-center gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(220px,1.8fr)_minmax(90px,.65fr)_minmax(120px,.8fr)_minmax(130px,.9fr)_minmax(130px,.9fr)_auto]">
        <div className="min-w-0">
          <p className="truncate text-[12px] font-semibold text-ink" title={detail.title || detail.event_id}>{detail.title || `事件 ${index + 1}`}</p>
          <p className="mt-1 truncate font-mono text-[9.5px] text-faint">{stamp(detail.time ?? detail.timestamp)} · {detail.symbol || "—"}</p>
        </div>
        <div><p className="mb-1 text-[9px] text-faint">方向标签</p><span className={cls("inline-flex rounded border px-1.5 py-0.5 text-[10px] font-medium", directionTone(detail.label))}>{directionLabel(detail.label)}</span></div>
        <div><p className="mb-1 text-[9px] text-faint">模型预测</p><p className="text-[11px] font-medium text-ink">{directionLabel(prediction)}{finite(detail.confidence) ? <span className="ml-1 font-mono text-[9.5px] text-mute">{ratioPct(detail.confidence)}</span> : null}</p></div>
        <div><p className="mb-1 text-[9px] text-faint">实际走势 / 标的收益</p><p className={cls("font-mono text-[11px] font-medium", returnTone(actual))}>{directionLabel(actualDirection)} · {pct(actual)}</p></div>
        <div><p className="mb-1 text-[9px] text-faint">方向净收益</p><p className={cls("font-mono text-[12px] font-semibold", returnTone(directional))}>{pct(directional)}</p></div>
        <span className="inline-flex items-center justify-end gap-1 text-[10px] text-brand">下钻<ChevronDown size={13} className="transition group-open:rotate-180" /></span>
      </div>
    </summary>
    <div className="border-t border-edge bg-paper/50 px-3.5 py-3">
      <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        <EventMetric label="标的实际收益" value={pct(detail.asset_return)} tone={returnTone(detail.asset_return)} />
        <EventMetric label="Oracle / 基准调整收益" value={pct(detail.actual_return)} tone={returnTone(detail.actual_return)} />
        <EventMetric label="方向毛收益" value={pct(detail.directional_return)} tone={returnTone(detail.directional_return)} />
        <EventMetric label="本事件盈亏" value={finite(detail.pnl) ? numeric(detail.pnl) : "暂无数据"} tone={returnTone(detail.pnl)} />
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1 text-[10px] text-mute">
        <span>事件 ID：<span className="font-mono text-ink">{detail.event_id}</span></span>
        {detail.metrics?.horizon && <span>窗口：<span className="font-mono text-ink">{detail.metrics.horizon.toUpperCase()}</span></span>}
        <span>标签判断：<span className={detail.metrics?.is_correct === true ? "text-jade" : detail.metrics?.is_correct === false ? "text-rise" : "text-faint"}>{detail.metrics?.is_correct === true ? "正确" : detail.metrics?.is_correct === false ? "错误" : "无法评估"}</span></span>
        <span>方向交易：<span className={detail.metrics?.is_win === true ? "text-jade" : detail.metrics?.is_win === false ? "text-rise" : "text-faint"}>{detail.metrics?.active_trade ? detail.metrics.is_win ? "盈利" : "亏损或持平" : "空仓 / 缺失"}</span></span>
        {detail.return_basis && <span>收益口径：<span className="font-mono text-ink">{detail.return_basis}</span></span>}
      </div>
    </div>
  </details>;
}

function EventComparisonResults({ result, preview }: { result: ModelComparisonResult; preview: boolean }) {
  const [selectedModelId, setSelectedModelId] = useState(result.models[0]?.run_id ?? "");
  useEffect(() => {
    if (!result.models.some((model) => model.run_id === selectedModelId)) setSelectedModelId(result.models[0]?.run_id ?? "");
  }, [result.models, selectedModelId]);
  const ordered = useMemo(() => [...result.models].sort((a, b) => {
    const av = finite(a.metrics.total_return) ? a.metrics.total_return : -Infinity;
    const bv = finite(b.metrics.total_return) ? b.metrics.total_return : -Infinity;
    return bv - av;
  }), [result.models]);
  const selected = result.models.find((model) => model.run_id === selectedModelId) ?? result.models[0];
  const details = selected?.event_details ?? selected?.records ?? [];
  const series = useMemo<PlotSeries[]>(() => result.models.flatMap((model, index) => (
    model.curve?.some((point) => finite(point.net_value))
      ? [{ id: model.run_id, name: model.name, color: COLORS[index % COLORS.length], points: model.curve }]
      : []
  )), [result.models]);
  const warnings = useMemo(() => result.models.flatMap((model) => (model.warnings ?? []).map((message) => ({ model: model.name, message }))), [result.models]);
  const eventCount = result.events?.length ?? Math.max(0, ...result.models.map((model) => model.metrics.event_count ?? 0));
  const isSet = result.subject.kind === "event_set" || eventCount > 1;
  const dated = !result.privacy?.timestamps_redacted;

  return <div className="min-w-0 space-y-5" aria-label="Event Arena 模型对比结果">
    <section className="relative overflow-hidden rounded-2xl border border-edgeDark/70 bg-[linear-gradient(110deg,#ffffff,#fbf8f2)] px-4 py-5 sm:px-6">
      <ArenaFieldArt className="absolute -right-3 -top-16 hidden h-[240px] w-[400px] text-brand/10 lg:block" />
      <div className="relative flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="mb-2 text-[9px] tracking-[.23em] text-violet">EVENT ARENA · THE COMMON GROUND</p>
          <p className="text-[11px] font-semibold text-jade">{isSet ? `同一事件集 · ${eventCount} 个事件` : "同一单事件"} · {preview ? "比较预览" : "已保存比较"}</p>
          <h2 className="mt-2 break-words font-serif text-[22px] font-semibold text-ink">{result.subject.title}</h2>
          <p className="mt-2 text-[11px] text-mute">{result.market.symbol} · {result.market.market} · {frequency(result.market.frequency)} · Oracle {String(result.rules.evaluation_horizon ?? `T+${result.rules.holding_bars ?? "—"}`).toUpperCase()}</p>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-violet/15 bg-violet-soft/70 px-3 py-1.5 text-[10px] text-violet"><CheckCircle2 size={13} />{result.models.length} 个事件模型 · 独立排名</span>
      </div>
      <p className="relative mt-3 text-[12px] leading-relaxed text-mute">{dated ? `${stamp(result.market.start_at)} → ${stamp(result.market.end_at)}` : "时间按当前可见性隐藏"}。标签作为方向真值；看涨跟随实际收益，看跌取实际收益的相反数，中性为空仓。所有模型共用冻结事件、标签、Oracle、预测窗口和成本。</p>
      <div className="relative mt-4 grid gap-2 rounded-xl border border-edge bg-card/75 p-3 text-[10.5px] sm:grid-cols-3">
        <p><span className="text-faint">实际收益口径：</span><span className="text-ink">Oracle CAR / 冻结行情</span></p>
        <p><span className="text-faint">方向规则：</span><span className="text-ink">看涨 +R · 看跌 −R · 中性 0</span></p>
        <p><span className="text-faint">交易成本：</span><span className="text-ink">统一往返成本 {numeric(result.rules.round_trip_cost_bps as number | null | undefined)} bps</span></p>
      </div>
    </section>

    <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card">
      <div className="border-b border-edge px-4 py-3.5"><h3 className="flex items-center gap-2 text-[13px] font-semibold text-ink"><BarChart3 size={15} />事件集模型成绩</h3><p className="mt-1 text-[12px] leading-relaxed text-mute">准确率按冻结标签计算；胜率只统计有方向且已兑现的交易，平均与累计收益则保留中性空仓的零收益。全部收益均按统一方向规则和成本计算。</p></div>
      <div className="overflow-x-auto"><table className="w-full min-w-[920px] text-left text-[12px]"><thead className="bg-paper text-mute"><tr>{["模型", "准确率", "胜率", "平均方向收益", "累计方向收益", "最大回撤", "预测覆盖"].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr></thead><tbody className="divide-y divide-edge">{ordered.map((model) => <tr key={model.run_id} className={cls(selected?.run_id === model.run_id && "bg-brand-soft/15")}><td className="max-w-[250px] px-4 py-3.5"><button type="button" onClick={() => setSelectedModelId(model.run_id)} className="break-words text-left font-semibold text-ink hover:text-brand">{model.name}</button><p className="mt-1 text-[10px] text-mute">{comparisonKindLabel(model.model_kind)}</p></td><td className="px-4 py-3.5 font-mono">{ratioPct(model.metrics.accuracy ?? model.prediction_quality?.directional_accuracy)}</td><td className="px-4 py-3.5 font-mono">{ratioPct(model.metrics.win_rate)}</td><td className={cls("px-4 py-3.5 font-mono", returnTone(model.metrics.average_return))}>{pct(model.metrics.average_return)}</td><td className={cls("px-4 py-3.5 font-mono font-semibold", returnTone(model.metrics.total_return))}>{pct(model.metrics.total_return)}</td><td className="px-4 py-3.5 font-mono">{finite(model.metrics.max_drawdown) ? pct(-Math.abs(model.metrics.max_drawdown)) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{numeric(model.metrics.prediction_count, 0)} / {numeric(model.metrics.event_count, 0)}</td></tr>)}</tbody></table></div>
    </section>

    <div className={cls("grid gap-3", result.models.length > 2 ? "sm:grid-cols-2 xl:grid-cols-4" : "sm:grid-cols-2")}>{result.models.map((model, index) => <button key={model.run_id} type="button" aria-pressed={selected?.run_id === model.run_id} onClick={() => setSelectedModelId(model.run_id)} className={cls("relative min-w-0 overflow-hidden rounded-xl border bg-card p-4 text-left transition", selected?.run_id === model.run_id ? "border-brand/45 shadow-card" : "border-edgeDark/70 hover:border-brand/25")}><span className="absolute inset-x-0 top-0 h-0.5" style={{ backgroundColor: COLORS[index % COLORS.length] }} /><p className="truncate font-serif text-[15px] font-semibold text-ink">{model.name}</p><div className="mt-3 grid grid-cols-2 gap-2"><EventMetric label="准确率" value={ratioPct(model.metrics.accuracy ?? model.prediction_quality?.directional_accuracy)} /><EventMetric label="胜率" value={ratioPct(model.metrics.win_rate)} /><EventMetric label="平均收益" value={pct(model.metrics.average_return)} tone={returnTone(model.metrics.average_return)} /><EventMetric label="累计收益" value={pct(model.metrics.total_return)} tone={returnTone(model.metrics.total_return)} /></div></button>)}</div>

    {selected && <section className="min-w-0 rounded-xl border border-edge bg-paper/50">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-edge px-4 py-3.5"><div><h3 className="text-[13px] font-semibold text-ink">逐事件结果 · {selected.name}</h3><p className="mt-1 text-[12px] leading-relaxed text-mute">每行同时展示标签、模型方向、实际走势、方向收益和盈亏；展开即可下钻单事件口径。</p></div><span className="rounded-full border border-edge bg-card px-2.5 py-1 text-[10px] text-mute">{details.length} 条</span></div>
      <div className="space-y-2 p-3">{details.map((detail, index) => <EventRecord key={`${detail.event_id}-${index}`} detail={detail} index={index} />)}</div>
      {!details.length && <p className="px-5 py-10 text-center text-[12px] text-mute">该模型没有可展示的逐事件记录；缺失预测不会补成中性或零收益。</p>}
    </section>}

    {series.length > 0 && <>
      <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card"><div className="border-b border-edge px-4 py-3.5"><h3 className="text-[13px] font-semibold text-ink">事件序列累计方向收益</h3><p className="mt-1 text-[12px] text-mute">按事件信息可得时间排序，以相同名义资金逐事件复利；可点击图例切换模型。</p></div><ComparisonChart series={series} drawdown={false} redacted={!dated} /></section>
      <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card"><div className="border-b border-edge px-4 py-3.5"><h3 className="text-[13px] font-semibold text-ink">事件序列最大回撤</h3><p className="mt-1 text-[12px] text-mute">基于同一事件排序与方向收益口径计算。</p></div><ComparisonChart series={series} drawdown redacted={!dated} /></section>
    </>}

    <ExecutionMetrics models={result.models} />
    {warnings.length > 0 && <details className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-[12px] text-amber-800"><summary className="cursor-pointer font-semibold"><AlertTriangle size={13} className="mr-1.5 inline" />{warnings.length} 项事件覆盖或数据说明</summary><ul className="mt-3 list-inside list-disc space-y-2 leading-relaxed">{warnings.map((warning, index) => <li key={index} className="break-words"><span className="font-semibold">{warning.model}：</span>{warning.message}</li>)}</ul></details>}
    {(result.notes ?? []).length > 0 && <div className="flex items-start gap-2 rounded-xl border border-edge bg-paper px-4 py-3 text-[12px] leading-relaxed text-mute"><Info size={14} className="mt-0.5 shrink-0" /><div className="min-w-0 space-y-1.5">{result.notes.map((note, index) => <p key={index} className="break-words">{note}</p>)}</div></div>}
  </div>;
}

function QuantComparisonResults({ result, preview = false }: { result: ModelComparisonResult; preview?: boolean }) {
  const [view, setView] = useState<"returns" | "forecast">("returns");
  const ordered = useMemo(() => [...result.models].sort((a, b) => {
    const av = hasSignals(a) && finite(a.metrics.total_return) ? a.metrics.total_return : -Infinity;
    const bv = hasSignals(b) && finite(b.metrics.total_return) ? b.metrics.total_return : -Infinity;
    return bv - av;
  }), [result]);
  const series = useMemo<PlotSeries[]>(() => [
    ...result.models.flatMap((model, index) => hasSignals(model) && model.curve?.some((point) => finite(point.net_value)) ? [{ id: model.run_id, name: model.name, color: COLORS[index % COLORS.length], points: model.curve }] : []),
    ...(result.benchmark_curve?.some((point) => finite(point.net_value)) ? [{ id: "asset-benchmark", name: "标的买入持有", color: "#6B6862", points: result.benchmark_curve, benchmark: true }] : []),
  ], [result]);
  const kline = useMemo(() => marketKline(result), [result]);
  const warnings = useMemo(() => result.models.flatMap((model) => (model.warnings ?? []).map((message) => ({ model: model.name, message }))), [result]);
  const dated = !result.privacy?.timestamps_redacted;
  const holding = result.rules.holding_bars;

  return <div className="min-w-0 space-y-5" aria-label="同一对象的模型对比结果">
    <section className="relative overflow-hidden rounded-2xl border border-edgeDark/70 bg-[linear-gradient(110deg,#ffffff,#fbf8f2)] px-4 py-5 sm:px-6">
      <ArenaFieldArt className="absolute -right-3 -top-16 hidden h-[240px] w-[400px] text-brand/10 lg:block" />
      <div className="relative flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><p className="mb-2 text-[9px] tracking-[.23em] text-brand">THE COMMON GROUND</p><p className="text-[11px] font-semibold text-jade">{result.subject.kind === "event" ? "同一事件" : "同一标的"} · {preview ? "比较预览" : "已保存比较"}</p><h2 className="mt-2 break-words font-serif text-[22px] font-semibold text-ink">{result.subject.title}</h2><p className="mt-2 text-[11px] text-mute">{result.market.symbol} · {result.market.market} · {frequency(result.market.frequency)} · {numeric(result.market.bar_count, 0)} 条共同行情</p></div><span className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-jade/15 bg-card/80 px-3 py-1.5 text-[10px] text-jade"><CheckCircle2 size={13} />{result.models.length} 个模型 · 统一规则模拟</span></div>
      <p className="mt-3 text-[12px] leading-relaxed text-mute">{dated ? `${stamp(result.market.start_at)} → ${stamp(result.market.end_at)}` : "时间按当前可见性隐藏"}。各模型的预测统一转换为做多或空仓信号，使用同一份行情和成交规则重新模拟。</p>
      {(result.market.source_label || result.market.snapshot_hash) && !result.privacy?.market_redacted && <div className="mt-2 space-y-1 text-[12px] leading-relaxed text-mute">{result.market.source_label && <p className="break-words">行情来源：{result.market.source_label}</p>}{result.market.snapshot_hash && <details><summary className="cursor-pointer">行情快照 · 用于复核所用数据</summary><p className="mt-1 break-all font-mono text-[11px]">{result.market.snapshot_hash}</p></details>}</div>}
      <details className="relative mt-4 border-t border-edgeDark/65 pt-3 text-[12px] text-mute"><summary className="cursor-pointer font-medium text-ink">查看统一模拟规则与预测信号处理</summary><div className="mt-2 space-y-1.5 leading-relaxed"><p>初始资金 {numeric(result.rules.initial_capital)}；持有周期 {numeric(holding, 0)} 根 K 线；每次买入或卖出分别计手续费 {numeric(result.rules.fee_bps)} bps、滑点 {numeric(result.rules.slippage_bps)} bps。1 bps = 0.01%。</p><p>优先使用收益率数值：正值做多，零或负值空仓；只提供方向时，看涨做多，看跌或中性空仓。收盘后形成的信号在下一根 K 线开盘执行。</p><p>新预测会更新方向并重新计算持有期；缺少新预测时保持原信号至到期，再转为空仓。期末按最后收盘价计净值，不补造强制卖出。</p><p>收益与回撤来自本次统一模拟；标的买入持有曲线按真实行情计算。预测误差和信号覆盖率单独列出。</p></div></details>
    </section>

    <div className="flex flex-wrap gap-1 border-b border-edgeDark/70" role="tablist" aria-label="模型比较内容"><button type="button" role="tab" aria-selected={view === "returns"} onClick={() => setView("returns")} className={cls("relative -mb-px flex items-center gap-2.5 rounded-t-lg border-b-2 px-4 py-3.5 text-[13px] font-semibold transition", view === "returns" ? "border-brand bg-brand-soft/30 text-brand" : "border-transparent text-mute hover:text-ink")}><span aria-hidden="true" className="font-serif text-[15px] font-normal opacity-60">I</span>收益与行情</button><button type="button" role="tab" aria-selected={view === "forecast"} onClick={() => setView("forecast")} className={cls("relative -mb-px flex items-center gap-2.5 rounded-t-lg border-b-2 px-4 py-3.5 text-[13px] font-semibold transition", view === "forecast" ? "border-jade bg-jade-soft/40 text-jade" : "border-transparent text-mute hover:text-ink")}><span aria-hidden="true" className="font-serif text-[15px] font-normal opacity-60">II</span>预测准确性与覆盖</button></div>

    {view === "returns" ? <>
      <div className={cls("grid gap-3", result.models.length > 2 ? "sm:grid-cols-2 xl:grid-cols-4" : "sm:grid-cols-2")}>{result.models.map((model, index) => <article key={model.run_id} className="relative min-w-0 overflow-hidden rounded-xl border border-edgeDark/70 bg-card p-4"><div className="absolute inset-x-0 top-0 h-0.5" style={{ backgroundColor: COLORS[index % COLORS.length] }} /><div className="flex items-center justify-between gap-2"><span className="font-mono text-[9px] tracking-[.16em]" style={{ color: COLORS[index % COLORS.length] }}>MODEL / {String(index + 1).padStart(2, "0")}</span><span className="text-[10px] text-faint">{comparisonKindLabel(model.model_kind)}</span></div><h3 className="mt-3 break-words font-serif text-[16px] font-semibold text-ink">{model.name}</h3><div className="mt-4 flex flex-wrap items-end justify-between gap-3"><div><p className="mb-1 text-[10px] text-mute">模拟累计收益</p><p className={cls("font-mono tracking-tight", hasSignals(model) ? "text-[24px] text-ink" : "text-[15px] text-faint")}>{hasSignals(model) ? pct(model.metrics.total_return) : "暂无数据"}</p></div><div className="text-right"><p className="mb-1 text-[10px] text-mute">最大回撤</p><p className="font-mono text-[12px] text-mute">{hasSignals(model) && finite(model.metrics.max_drawdown) ? pct(-Math.abs(model.metrics.max_drawdown)) : "暂无数据"}</p></div></div></article>)}</div>
      <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card">
        <div className="border-b border-edge px-4 py-3.5"><h3 className="flex items-center gap-2 text-[13px] font-semibold text-ink"><BarChart3 size={15} />统一规则下的模型表现</h3><p className="mt-1 text-[12px] leading-relaxed text-mute">按本次模拟累计收益排序。各模型的信号覆盖区间可能不同，此排序不代表同样本预测能力排名；请结合覆盖率与预测误差判断。无有效信号的模型不计成绩。</p></div>
        <div className="overflow-x-auto"><table className="min-w-[850px] w-full text-left text-[12px]"><thead className="bg-paper text-mute"><tr>{["模型", "累计收益", "相对买入持有", "最大回撤", "交易次数", "模拟成本", "信号覆盖率"].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr></thead><tbody className="divide-y divide-edge">{ordered.map((model) => <tr key={model.run_id}><td className="max-w-[260px] px-4 py-3.5"><p className="break-words font-semibold text-ink">{model.name}</p><p className="mt-1 text-[11px] text-mute">{comparisonKindLabel(model.model_kind)}{!hasSignals(model) && " · 无有效信号"}</p></td><td className="px-4 py-3.5 font-mono text-ink">{hasSignals(model) ? pct(model.metrics.total_return) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono text-ink">{hasSignals(model) && finite(model.metrics.excess_total_return) ? `${(model.metrics.excess_total_return * 100).toFixed(2)} 个百分点` : "暂无数据"}</td><td className="px-4 py-3.5 font-mono text-ink">{hasSignals(model) && finite(model.metrics.max_drawdown) ? pct(-Math.abs(model.metrics.max_drawdown)) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono text-ink">{hasSignals(model) ? numeric(model.metrics.trade_count, 0) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono text-ink">{hasSignals(model) ? numeric(model.metrics.total_cost) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono text-ink">{ratioPct(model.coverage.ratio)}</td></tr>)}</tbody></table></div>
      </section>
      <FinancialMetrics models={ordered} />
      <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card"><div className="border-b border-edge px-4 py-3.5"><h3 className="text-[13px] font-semibold text-ink">累计收益对比</h3><p className="mt-1 text-[12px] text-mute">可点击图例切换模型，拖动滑块查看区间。{dated ? "横轴为真实行情时间。" : "横轴为可共享观测序号。"}</p></div><ComparisonChart series={series} drawdown={false} redacted={!dated} /></section>
      <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card"><div className="border-b border-edge px-4 py-3.5"><h3 className="text-[13px] font-semibold text-ink">回撤对比</h3><p className="mt-1 text-[12px] text-mute">相对此前最高净值的跌幅。</p></div><ComparisonChart series={series} drawdown redacted={!dated} /></section>
      <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card"><div className="border-b border-edge px-4 py-3.5"><h3 className="flex items-center gap-2 text-[13px] font-semibold text-ink"><CandlestickChart size={15} />共同行情 K 线 · {result.market.symbol}</h3><p className="mt-1 text-[12px] text-mute">所有模型使用下方同一份真实 OHLC 行情进行模拟。</p></div>{kline ? <KlineChart payload={kline} height={440} tradeMarkers={NO_MARKERS} /> : <p className="px-5 py-10 text-center text-[12px] text-mute">{result.privacy?.market_redacted || !dated ? "当前可见性不展示行情明细。" : "未提供完整的真实 OHLC，无法绘制 K 线。"}</p>}</section>
    </> : <><PredictionQuality models={result.models} /><section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card">
      <div className="border-b border-edge px-4 py-3.5"><h3 className="text-[13px] font-semibold text-ink">共同样本的预测质量与信号覆盖</h3><p className="mt-1 text-[12px] leading-relaxed text-mute">所有数值指标仅比较各模型共同拥有、期限相同且已到期的预测。误差以百分点计；IC、Rank IC、R² 与技能分数越高越好，方向准确率使用共同的标的涨跌样本。</p></div>
      <div className="overflow-x-auto"><table className="min-w-[1500px] w-full text-left text-[12px]"><thead className="bg-paper text-mute"><tr>{["模型", "MAE / RMSE", "MedAE / P90", "Bias", "IC / Rank IC", "R²", "零基准技能", "共同 / 自身数值样本", "方向准确率", "共同 / 自身方向样本", "有效信号", "行情覆盖"].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr></thead><tbody className="divide-y divide-edge">{result.models.map((model) => <tr key={model.run_id}><td className="max-w-[260px] px-4 py-3.5"><p className="break-words font-semibold text-ink">{model.name}</p><p className="mt-1 text-[11px] text-mute">{comparisonKindLabel(model.model_kind)}</p></td><td className="px-4 py-3.5 font-mono">{model.forecast.n > 0 ? `${numeric(model.forecast.mae_pct, 3)} / ${numeric(model.forecast.rmse_pct, 3)}` : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{model.forecast.n > 0 ? `${numeric(model.forecast.median_abs_error_pct, 3)} / ${numeric(model.forecast.p90_abs_error_pct, 3)}` : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{numeric(model.forecast.bias_pct, 3)}</td><td className="px-4 py-3.5 font-mono">{`${numeric(model.forecast.pearson_ic, 3)} / ${numeric(model.forecast.spearman_rank_ic, 3)}`}</td><td className="px-4 py-3.5 font-mono">{numeric(model.forecast.r_squared, 3)}</td><td className="px-4 py-3.5 font-mono">{ratioPct(model.forecast.skill_score_vs_zero)}</td><td className="px-4 py-3.5 font-mono">{numeric(model.forecast.matched_samples ?? model.forecast.n, 0)} / {numeric(model.forecast.own_n, 0)}</td><td className="px-4 py-3.5 font-mono">{hasSignals(model) ? ratioPct(model.forecast.directional_accuracy) : "暂无数据"}</td><td className="px-4 py-3.5 font-mono">{numeric(model.forecast.directional_n, 0)} / {numeric(model.forecast.directional_own_n, 0)}</td><td className="px-4 py-3.5 font-mono">{numeric(model.coverage.signal_count, 0)}</td><td className="px-4 py-3.5"><p className="font-mono">{ratioPct(model.coverage.ratio)}</p><p className="mt-1 text-[11px] text-mute">{numeric(model.coverage.covered_bars, 0)} / {numeric(model.coverage.total_bars, 0)} 根</p></td></tr>)}</tbody></table></div>
    </section><FinancialMetrics models={ordered} /></>}

    <ExecutionMetrics models={result.models} />
    {warnings.length > 0 && <details className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-[12px] text-amber-800"><summary className="cursor-pointer font-semibold"><AlertTriangle size={13} className="mr-1.5 inline" />{warnings.length} 项覆盖或数据说明</summary><ul className="mt-3 list-inside list-disc space-y-2 leading-relaxed">{warnings.map((warning, index) => <li key={index} className="break-words"><span className="font-semibold">{warning.model}：</span>{warning.message}</li>)}</ul></details>}
    {(result.notes ?? []).length > 0 && <div className="flex items-start gap-2 rounded-xl border border-edge bg-paper px-4 py-3 text-[12px] leading-relaxed text-mute"><Info size={14} className="mt-0.5 shrink-0" /><div className="min-w-0 space-y-1.5">{result.notes.map((note, index) => <p key={index} className="break-words">{note}</p>)}</div></div>}
  </div>;
}

export default function ModelComparisonResults({ result, preview = false }: { result: ModelComparisonResult; preview?: boolean }) {
  if (result.track === "event" || result.schema_version === "arena-event-comparison-v1") {
    return <EventComparisonResults result={result} preview={preview} />;
  }
  return <QuantComparisonResults result={result} preview={preview} />;
}
