import type { BTPerformanceCurvePoint, BTPerformanceResponse, KlinePayload } from "../../types";

export type QuantMarketSeries = {
  labels: string[];
  returns: Array<number | null>;
  drawdowns: Array<number | null>;
  validCount: number;
  totalReturnPct: number | null;
  maxDrawdownPct: number | null;
};

export type QuantMarketData = QuantMarketSeries & {
  source: "asset_curve" | "benchmark_curve" | "bars" | "none";
  kline: KlinePayload | null;
  barCount: number | null;
  frequency: string;
  invalidBarCount: number;
};

function finite(value: unknown): number | null {
  const number = typeof value === "number" ? value : typeof value === "string" && value.trim() ? Number(value) : Number.NaN;
  return Number.isFinite(number) ? number : null;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function label(point: Record<string, unknown>, index: number, arenaSafe: boolean): string {
  return arenaSafe ? `#${index + 1}` : String(point.timestamp ?? point.date ?? point.event_time ?? point.time ?? `#${index + 1}`);
}

function newSeries(): QuantMarketSeries {
  return { labels: [], returns: [], drawdowns: [], validCount: 0, totalReturnPct: null, maxDrawdownPct: null };
}

/** One pass over normalized asset levels; missing observations remain gaps. */
function fromCurve(points: BTPerformanceCurvePoint[] | undefined, arenaSafe: boolean): QuantMarketSeries {
  const series = newSeries();
  let peak: number | null = null;
  for (let index = 0; index < (points?.length ?? 0); index += 1) {
    const point = record(points![index]);
    const cumulative = finite(point.cumulative_return);
    const level = finite(point.net_value ?? point.asset_net_value ?? point.benchmark_net_value)
      ?? (cumulative === null ? null : 1 + cumulative);
    series.labels.push(label(point, index, arenaSafe));
    if (level === null || level < 0) {
      series.returns.push(null);
      series.drawdowns.push(null);
      continue;
    }
    // net_value is already normalized by the backend. Do not rebase a
    // privacy-preserving aggregate whose first bucket may be above/below 1.
    const cumulativePct = (level - 1) * 100;
    peak = peak === null ? Math.max(1, level) : Math.max(peak, level);
    const drawdownPct = (level / peak - 1) * 100;
    series.returns.push(cumulativePct);
    series.drawdowns.push(drawdownPct);
    series.validCount += 1;
    series.totalReturnPct = cumulativePct;
    series.maxDrawdownPct = Math.min(series.maxDrawdownPct ?? 0, drawdownPct);
  }
  return series;
}

/** Only real closes generate fallback returns; never use model predictions. */
function fromBars(bars: Array<Record<string, unknown>>, symbol: string) {
  const series = newSeries();
  const kline: KlinePayload = { symbol, dates: [], ohlc: [], volumes: [] };
  let firstClose: number | null = null;
  let peakClose: number | null = null;
  let allVolumes = true;
  let invalidBarCount = 0;
  for (let index = 0; index < bars.length; index += 1) {
    const bar = record(bars[index]);
    const timestamp = String(bar.timestamp ?? bar.date ?? bar.time ?? "");
    const close = finite(bar.close);
    series.labels.push(timestamp || `#${index + 1}`);
    if (close === null || close <= 0) {
      series.returns.push(null);
      series.drawdowns.push(null);
    } else {
      firstClose ??= close;
      peakClose = Math.max(peakClose ?? close, close);
      const cumulativePct = (close / firstClose - 1) * 100;
      const drawdownPct = (close / peakClose - 1) * 100;
      series.returns.push(cumulativePct);
      series.drawdowns.push(drawdownPct);
      series.validCount += 1;
      series.totalReturnPct = cumulativePct;
      series.maxDrawdownPct = Math.min(series.maxDrawdownPct ?? 0, drawdownPct);
    }
    const open = finite(bar.open);
    const high = finite(bar.high);
    const low = finite(bar.low);
    if (!timestamp || open === null || high === null || low === null || close === null
      || Math.min(open, high, low, close) <= 0 || high < Math.max(open, close) || low > Math.min(open, close)) {
      invalidBarCount += 1;
      continue;
    }
    kline.dates.push(timestamp);
    kline.ohlc.push([open, close, low, high]);
    const volume = finite(bar.volume);
    if (volume === null || volume < 0) allVolumes = false;
    else kline.volumes.push(volume);
    if (!kline.symbol && bar.symbol) kline.symbol = String(bar.symbol);
  }
  if (!allVolumes || kline.volumes.length !== kline.dates.length) kline.volumes = [];
  return { series, kline: kline.dates.length ? kline : null, invalidBarCount };
}

/** Linear preparation, including full minute-bar histories. No point lookups. */
export function prepareQuantForecastMarket(data: BTPerformanceResponse, arenaSafe: boolean): QuantMarketData {
  const dataset = record(data.dataset);
  const quality = record(data.data_quality);
  const summary = record(data.summary);
  let source: QuantMarketData["source"] = "asset_curve";
  let series = fromCurve(data.asset_curve, arenaSafe);
  if (!series.validCount) {
    series = fromCurve(data.benchmark_curve, arenaSafe);
    source = "benchmark_curve";
  }
  // Even if a caller accidentally passes unredacted bars into an Arena-safe
  // view, do not inspect them, derive a curve from them, or build a Kline.
  const bars = arenaSafe ? [] : Array.isArray(data.bars) ? data.bars : [];
  const barData = arenaSafe ? null : fromBars(bars, String(dataset.symbol ?? ""));
  if (!series.validCount && barData?.series.validCount) {
    series = barData.series;
    source = "bars";
  }
  if (!series.validCount) source = "none";
  const statedCount = finite(summary.bar_count) ?? finite(quality.bar_count);
  const barCount = statedCount !== null && statedCount >= 0 ? Math.floor(statedCount)
    : bars.length ? bars.length : series.validCount ? series.validCount : null;
  return {
    ...series,
    source,
    kline: barData?.kline ?? null,
    invalidBarCount: barData?.invalidBarCount ?? 0,
    barCount,
    frequency: String(dataset.frequency ?? quality.frequency ?? ""),
  };
}
