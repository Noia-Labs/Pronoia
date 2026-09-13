export interface QuantReturnForecastParameters {
  method: "historical_mean";
  lookback: number;
  horizon_bars: number;
}

const INPUT = "w-full rounded-xl border border-edgeDark/70 bg-card px-3 py-2.5 text-sm text-ink outline-none focus:border-brand/60 focus:ring-2 focus:ring-brand/10 disabled:opacity-50";

export default function QuantReturnForecastConfig({ value, onChange, disabled = false }: {
  value: QuantReturnForecastParameters;
  onChange: (value: QuantReturnForecastParameters) => void;
  disabled?: boolean;
}) {
  return <div className="space-y-4">
    <div className="rounded-xl border border-brand/20 bg-brand-soft/30 px-4 py-3 text-sm text-ink">
      预测标的从当前收盘到第 N 根 K 线收盘的收益率，例如 +2.5%。实际行情到期后，比较预测与实际的差值。
    </div>
    <div className="grid gap-4 sm:grid-cols-3">
      <label className="space-y-2 text-sm text-ink"><span className="block font-medium">预测方法</span>
        <select className={INPUT} value={value.method} disabled={disabled} onChange={() => onChange({ ...value, method: "historical_mean" })}>
          <option value="historical_mean">历史 N 周期收益均值</option>
        </select>
      </label>
      <label className="space-y-2 text-sm text-ink"><span className="block font-medium">预测周期 N</span>
        <input className={INPUT} type="number" min={1} max={10000} step={1} value={value.horizon_bars} disabled={disabled}
          onChange={(event) => onChange({ ...value, horizon_bars: Number(event.target.value) })} />
        <span className="block text-xs text-mute">单位为 K 线根数；日 K 的 N=3 表示后续 3 根日 K。</span>
      </label>
      <label className="space-y-2 text-sm text-ink"><span className="block font-medium">历史样本数</span>
        <input className={INPUT} type="number" min={1} max={10000} step={1} value={value.lookback} disabled={disabled}
          onChange={(event) => onChange({ ...value, lookback: Number(event.target.value) })} />
        <span className="block text-xs text-mute">取最近这些已实现的 N 周期收益率，计算平均值。</span>
      </label>
    </div>
    <p className="text-xs leading-relaxed text-mute">每个时点只使用截至该时点的历史行情。开始预测前需要至少 {value.lookback + value.horizon_bars} 根 K 线；末尾尚无到期价格的预测保留为待评估。此方法仅输出收益率预测。</p>
  </div>;
}
