import { useEffect, useMemo, useRef } from "react";
import * as echarts from "echarts/core";
import { LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, TooltipComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { BarChart3, CandlestickChart, Info, LockKeyhole } from "lucide-react";
import type { BTPerformanceResponse } from "../../types";
import KlineChart, { type KlineTradeMarker } from "../KlineChart";
import { prepareQuantForecastMarket, type QuantMarketSeries } from "./quantForecastMarket";

echarts.use([LineChart, DataZoomComponent, GridComponent, TooltipComponent, CanvasRenderer]);

const NO_TRADE_MARKERS: KlineTradeMarker[] = [];
const CARD = "min-w-0 overflow-hidden rounded-xl border border-edge bg-card";

function percentage(value: number | null): string {
  return value === null ? "—" : `${value > 0 ? "+" : ""}${value.toFixed(2)}%`;
}

function frequencyLabel(value: string): string {
  return ({ "1m": "1 分钟", "5m": "5 分钟", "15m": "15 分钟", "30m": "30 分钟", "60m": "60 分钟", "1h": "1 小时", "1d": "日线", "1w": "周线", "1M": "月线" } as Record<string, string>)[value] ?? (value || "未提供");
}

function MarketMetric({ label, value, note }: { label: string; value: string; note: string }) {
  return <div className="min-w-0 rounded-xl border border-edge bg-card px-4 py-3.5"><p className="text-[12px] text-mute">{label}</p><p className="mt-2 break-words font-mono text-[20px] font-semibold text-ink">{value}</p><p className="mt-1 text-[11px] leading-relaxed text-faint">{note}</p></div>;
}

function EmptyMarketChart({ message }: { message: string }) {
  return <div className="flex min-h-[260px] items-center justify-center gap-2 px-6 py-10 text-center text-[12px] leading-relaxed text-mute"><Info size={15} className="shrink-0" />{message}</div>;
}

function MarketLineChart({ series, drawdown, arenaSafe }: { series: QuantMarketSeries; drawdown: boolean; arenaSafe: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current || !series.validCount) return;
    const chart = echarts.init(ref.current);
    const color = drawdown ? "#D14343" : "#0F766E";
    chart.setOption({
      animation: false,
      textStyle: { fontFamily: "inherit" },
      tooltip: {
        trigger: "axis", renderMode: "richText", backgroundColor: "#FFFFFF", borderColor: "#E8E5E0",
        textStyle: { color: "#1C1B1A", fontSize: 11 },
        valueFormatter: (value: unknown) => typeof value === "number" && Number.isFinite(value) ? percentage(value) : "缺少观测",
      },
      grid: { left: 62, right: 22, top: 24, bottom: 70 },
      xAxis: {
        type: "category", data: series.labels, boundaryGap: false,
        axisLine: { lineStyle: { color: "#E8E5E0" } }, axisTick: { show: false },
        axisLabel: { color: "#6B6862", fontSize: 9, hideOverlap: true, formatter: (value: string) => arenaSafe ? value : value.replace("T", " ") },
      },
      yAxis: {
        type: "value", ...(drawdown ? { max: 0 } : {}),
        axisLabel: { color: "#6B6862", fontSize: 10, formatter: (value: number) => `${Number(value.toFixed(2))}%` },
        splitLine: { lineStyle: { color: "#F0EDE8" } },
      },
      dataZoom: [
        { type: "inside", start: 0, end: 100, filterMode: "none" },
        { type: "slider", bottom: 8, height: 18, start: 0, end: 100, filterMode: "none", borderColor: "#E8E5E0", backgroundColor: "#FAF9F7", fillerColor: "rgba(15,118,110,.08)", handleStyle: { color }, textStyle: { fontSize: 9, color: "#6B6862" } },
      ],
      series: [{
        name: drawdown ? "标的回撤" : "标的买入持有累计收益",
        type: "line", data: drawdown ? series.drawdowns : series.returns,
        sampling: "lttb", showSymbol: false, connectNulls: false,
        lineStyle: { color, width: 1.5 }, itemStyle: { color },
        areaStyle: { color: drawdown ? "rgba(209,67,67,.10)" : "rgba(15,118,110,.07)" },
      }],
    });
    const observer = new ResizeObserver(() => chart.resize());
    observer.observe(ref.current);
    return () => { observer.disconnect(); chart.dispose(); };
  }, [series, drawdown, arenaSafe]);
  return <div ref={ref} className="w-full" style={{ height: 320 }} role="img" aria-label={drawdown ? "标的实际回撤曲线" : "标的实际买入持有累计收益曲线"} />;
}

export default function QuantForecastMarketPanel({ data, arenaSafe }: { data: BTPerformanceResponse; arenaSafe: boolean }) {
  const market = useMemo(() => prepareQuantForecastMarket(data, arenaSafe), [data, arenaSafe]);
  return <section className="min-w-0 space-y-4" aria-label="收益预测的标的行情">
    <div className="flex items-start gap-2.5 rounded-xl border border-jade/20 bg-jade-soft/30 px-4 py-3.5">
      <BarChart3 size={17} className="mt-0.5 shrink-0 text-jade" />
      <div><h3 className="text-[13px] font-semibold text-ink">标的行情表现</h3><p className="mt-1 text-[12px] leading-relaxed text-mute">这里展示标的实际行情及买入持有表现，按收盘价计算，未计交易成本。本次模型仅预测收益率，没有交易仓位。</p></div>
    </div>

    <div className="grid grid-cols-2 gap-3 xl:grid-cols-4">
      <MarketMetric label="标的区间收益" value={percentage(market.totalReturnPct)} note={arenaSafe ? "按可共享的累计收益曲线" : "区间末相对区间初的累计涨跌"} />
      <MarketMetric label="标的最大回撤" value={percentage(market.maxDrawdownPct)} note={arenaSafe ? "按可共享的观测点计算" : "从历史峰值到后续低点的最大跌幅"} />
      <MarketMetric label="行情条数" value={market.barCount === null ? "—" : market.barCount.toLocaleString("zh-CN")} note={arenaSafe ? "本次评测的行情规模" : "本次评测使用的行情记录"} />
      <MarketMetric label="行情频率" value={frequencyLabel(market.frequency)} note="每条行情对应的时间间隔" />
    </div>

    <div className="grid min-w-0 gap-4 xl:grid-cols-2">
      <div className={CARD}><div className="border-b border-edge px-4 py-3"><h4 className="text-[12px] font-semibold text-ink">标的买入持有累计收益</h4><p className="mt-1 text-[10px] text-mute">{arenaSafe ? "横轴为共享观测点序号" : "按实际收盘价计算；可拖动下方滑块查看区间"}</p></div>{market.validCount ? <MarketLineChart series={market} drawdown={false} arenaSafe={arenaSafe} /> : <EmptyMarketChart message="尚无可用的标的收益曲线或真实收盘价。" />}</div>
      <div className={CARD}><div className="border-b border-edge px-4 py-3"><h4 className="text-[12px] font-semibold text-ink">标的回撤</h4><p className="mt-1 text-[10px] text-mute">每个观测点相对此前最高标的净值的跌幅</p></div>{market.validCount ? <MarketLineChart series={market} drawdown arenaSafe={arenaSafe} /> : <EmptyMarketChart message="尚无可用于计算标的回撤的行情。" />}</div>
    </div>

    {arenaSafe ? <div className="flex items-start gap-2 rounded-xl border border-edge bg-paper px-4 py-3 text-[12px] leading-relaxed text-mute"><LockKeyhole size={14} className="mt-0.5 shrink-0" />Arena 安全摘要仅展示可共享曲线与汇总，不展示行情时间和 K 线。</div> : <div className={CARD}>
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-edge px-4 py-3"><div><h4 className="flex items-center gap-1.5 text-[12px] font-semibold text-ink"><CandlestickChart size={14} />真实 K 线{market.kline?.symbol && <span className="ml-1 font-mono text-[10px] font-normal text-mute">{market.kline.symbol}</span>}</h4><p className="mt-1 text-[10px] text-mute">使用本次评测保存的开盘、最高、最低与收盘价格。</p></div>{market.kline && <span className="text-[10px] text-faint">{market.kline.dates.length.toLocaleString("zh-CN")} 根 K 线</span>}</div>
      {market.kline ? <KlineChart payload={market.kline} height={440} tradeMarkers={NO_TRADE_MARKERS} /> : <EmptyMarketChart message="尚无完整的真实 OHLC 行情，无法展示 K 线。" />}
      {market.invalidBarCount > 0 && <p className="border-t border-edge px-4 py-2.5 text-[10px] leading-relaxed text-mute">{market.invalidBarCount.toLocaleString("zh-CN")} 条记录缺少有效时间或完整 OHLC，未绘制为 K 线；有效收盘价仍可用于上方收益计算。</p>}
    </div>}
  </section>;
}
