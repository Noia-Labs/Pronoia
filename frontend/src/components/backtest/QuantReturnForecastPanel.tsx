import { useState } from "react";

export interface QuantReturnForecastSummary {
  status?: string;
  n?: number;
  evaluated_count?: number;
  n_forecasts?: number;
  n_predictions?: number;
  eligible_count?: number;
  pending_count?: number;
  coverage?: number | null;
  evaluation_coverage?: number | null;
  eligible_evaluation_coverage?: number | null;
  mae_pct?: number | null;
  rmse_pct?: number | null;
  median_abs_error_pct?: number | null;
  p90_abs_error_pct?: number | null;
  bias_pct?: number | null;
  error_std_pct?: number | null;
  mean_expected_return_pct?: number | null;
  mean_actual_return_pct?: number | null;
  direction_accuracy?: number | null;
  direction_evaluated_count?: number;
  direction_coverage?: number | null;
  up_call_hit_rate?: number | null;
  up_call_count?: number;
  down_call_hit_rate?: number | null;
  down_call_count?: number;
  pearson_ic?: number | null;
  spearman_rank_ic?: number | null;
  r_squared?: number | null;
  zero_baseline_rmse_pct?: number | null;
  skill_score_vs_zero?: number | null;
  horizon_bars?: number | null;
  by_horizon?: QuantReturnForecastSummary[];
}

export interface QuantReturnForecastRow {
  timestamp: string;
  horizon_bars: number;
  target_timestamp?: string | null;
  expected_return_pct: number;
  actual_return_pct?: number | null;
  error_pct?: number | null;
  status: "evaluated" | "pending";
}

type UnknownRecord = Record<string, unknown>;

const record = (value: unknown): UnknownRecord => value && typeof value === "object" && !Array.isArray(value) ? value as UnknownRecord : {};
const finite = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;
const number = (value: number | null | undefined, signed = false) => value == null || !Number.isFinite(value)
  ? "—" : `${signed && value > 0 ? "+" : ""}${value.toFixed(3)}`;
const ratio = (value: number | null | undefined, signed = false) => value == null || !Number.isFinite(value)
  ? "—" : `${signed && value > 0 ? "+" : ""}${value.toFixed(3)}`;
const percent = (value: number | null | undefined, signed = false) => value == null || !Number.isFinite(value)
  ? "—" : `${signed && value > 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;
const returnPercent = (value: number | null, signed = true) => value == null
  ? "—" : `${signed && value > 0 ? "+" : ""}${(value * 100).toFixed(2)}%`;

function MetricGroup({ title, note, items }: {
  title: string;
  note: string;
  items: Array<[string, string, string]>;
}) {
  return <div className="border-t border-edge first:border-t-0">
    <div className="flex flex-wrap items-baseline justify-between gap-2 px-5 pb-2 pt-4">
      <h4 className="text-xs font-semibold text-ink">{title}</h4>
      <p className="text-[10px] text-mute">{note}</p>
    </div>
    <div className="grid grid-cols-2 gap-px bg-edge sm:grid-cols-3 xl:grid-cols-6">
      {items.map(([label, value, itemNote]) => <div key={label} className="min-w-0 bg-card px-4 py-4">
        <div className="truncate text-[11px] text-mute" title={label}>{label}</div>
        <div className="mt-2 truncate font-mono text-xl tabular-nums text-ink" title={value}>{value}</div>
        <div className="mt-1 min-h-[20px] text-[10px] leading-relaxed text-faint">{itemNote}</div>
      </div>)}
    </div>
  </div>;
}

export default function QuantReturnForecastPanel({
  summary,
  forecasts = [],
  predictionOnly = false,
  financialAnalysis,
  performanceSummary,
}: {
  summary?: QuantReturnForecastSummary | null;
  forecasts?: QuantReturnForecastRow[];
  predictionOnly?: boolean;
  financialAnalysis?: UnknownRecord | null;
  performanceSummary?: UnknownRecord | null;
}) {
  const [page, setPage] = useState(0);
  const [horizon, setHorizon] = useState<number | null>(null);
  const availableHorizons = summary?.by_horizon ?? [];
  const selectedSummary = availableHorizons.find((item) => item.horizon_bars === horizon) ?? summary;
  const rows = horizon == null ? forecasts : forecasts.filter((row) => row.horizon_bars === horizon);
  const pages = Math.max(1, Math.ceil(rows.length / 20));
  const currentPage = Math.min(page, pages - 1);
  const visible = rows.slice(currentPage * 20, currentPage * 20 + 20);
  const provided = (summary?.n_forecasts ?? forecasts.length) > 0;
  const evaluated = selectedSummary?.evaluated_count ?? selectedSummary?.n ?? 0;
  const fullForecastCount = selectedSummary?.n_forecasts ?? rows.length;
  const lowSample = evaluated > 0 && evaluated < 5;

  const analysis = record(financialAnalysis);
  const returns = record(analysis.returns);
  const risk = record(analysis.risk);
  const trading = record(analysis.trading);
  const costs = record(analysis.costs);
  const fallback = record(performanceSummary);
  const financial = (source: UnknownRecord, key: string, fallbackKey = key) => finite(source[key]) ?? finite(fallback[fallbackKey]);
  const hasStrategyFinance = !predictionOnly && analysis.status !== "not_applicable" && [
    financial(returns, "total_return"), financial(risk, "max_drawdown"), financial(risk, "sharpe_ratio"),
  ].some((value) => value != null);

  return <section className="overflow-hidden rounded-2xl border border-edge bg-card">
    <div className="border-b border-edge px-5 py-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h3 className="font-semibold text-ink">标的收益率预测与策略比较</h3><p className="mt-1 text-xs leading-relaxed text-mute">预测质量与交易表现分开计算；所有汇总指标均基于后端完整已到期样本，不受页面明细抽样影响。</p></div>
        {availableHorizons.length > 1 && <select aria-label="收益预测周期" className="rounded-lg border border-edge bg-card px-2 py-1 text-sm" value={horizon ?? "all"}
          onChange={(event) => { setHorizon(event.target.value === "all" ? null : Number(event.target.value)); setPage(0); }}>
          <option value="all">全部周期（仅概览）</option>
          {availableHorizons.map((item) => <option key={item.horizon_bars} value={item.horizon_bars ?? ""}>T+{item.horizon_bars} 根 K 线</option>)}
        </select>}
      </div>
      <p className="mt-2 text-[10px] leading-relaxed text-faint">预测误差 = 预测 − 实际，单位为百分点；IC、Rank IC、R² 和技能分数只用于相同数据、周期与口径的横向比较。</p>
    </div>

    {!provided ? <div className="border-b border-edge px-5 py-6 text-sm text-mute">本次策略没有显式输出未来收益率数值，因此不能计算预测误差、IC 或方向命中率；仓位和评分不会被冒充为收益率预测。</div> : <>
      <MetricGroup title="误差与基线" note={lowSample ? "样本少于 5 个，仅供参考，不进入公开排名" : "百分点；除 Bias 外均为越小越好"} items={[
        ["MAE · 平均绝对误差", number(selectedSummary?.mae_pct), "整体误差尺度"],
        ["RMSE · 均方根误差", number(selectedSummary?.rmse_pct), "更惩罚大误差"],
        ["MedAE · 中位绝对误差", number(selectedSummary?.median_abs_error_pct), "对极端值更稳健"],
        ["P90 绝对误差", number(selectedSummary?.p90_abs_error_pct), "90% 样本不超过此值"],
        ["Bias · 平均偏差", number(selectedSummary?.bias_pct, true), "正值表示整体高估"],
        ["误差波动", number(selectedSummary?.error_std_pct), "误差的总体标准差"],
      ]} />
      <MetricGroup title="方向、相关性与解释力" note="方向指标排除零预测和零实际收益；相关系数无方差时不计算" items={[
        ["方向命中率", percent(selectedSummary?.direction_accuracy), `${selectedSummary?.direction_evaluated_count ?? 0} 个非零方向样本`],
        ["看涨判断命中率", percent(selectedSummary?.up_call_hit_rate), `${selectedSummary?.up_call_count ?? 0} 个看涨判断`],
        ["看跌判断命中率", percent(selectedSummary?.down_call_hit_rate), `${selectedSummary?.down_call_count ?? 0} 个看跌判断`],
        ["Pearson IC", ratio(selectedSummary?.pearson_ic, true), "预测值与实际值线性相关"],
        ["Rank IC", ratio(selectedSummary?.spearman_rank_ic, true), "预测排序与实际排序相关"],
        ["R² · 样本外解释度", ratio(selectedSummary?.r_squared, true), "可为负；越高越好"],
      ]} />
      <MetricGroup title="覆盖、均值与技能" note="技能分数以“始终预测 0%”为基线；正值更好，负值更差" items={[
        ["零基准技能", percent(selectedSummary?.skill_score_vs_zero, true), `基线 RMSE ${number(selectedSummary?.zero_baseline_rmse_pct)}`],
        ["平均预测收益", `${number(selectedSummary?.mean_expected_return_pct, true)}%`, "预测分布中心"],
        ["平均实际收益", `${number(selectedSummary?.mean_actual_return_pct, true)}%`, "到期样本真实均值"],
        ["预测覆盖率", percent(selectedSummary?.coverage), `${selectedSummary?.n_forecasts ?? 0} / ${selectedSummary?.n_predictions ?? 0} 个时点`],
        ["可评估覆盖率", percent(selectedSummary?.eligible_evaluation_coverage ?? selectedSummary?.evaluation_coverage), `${evaluated} / ${selectedSummary?.eligible_count ?? selectedSummary?.n_predictions ?? 0} 个可到期时点`],
        ["待到期预测", String(selectedSummary?.pending_count ?? 0), `当前已评估 ${evaluated} 个`],
      ]} />
    </>}

    {predictionOnly ? <div className="border-t border-edge bg-paper/50 px-5 py-4 text-xs leading-relaxed text-mute">本次为纯预测评估，没有组合仓位、模拟成交或策略净值，因此不展示虚构的收益率、Sharpe 或回撤。需要经济表现对比时，可在模型对比中使用同一行情与统一成交规则回放。</div> : hasStrategyFinance ? <MetricGroup title="同一 Run 的策略金融表现" note="来自真实 K 线组合回测；与上方预测准确率是两套独立口径" items={[
      ["累计收益", returnPercent(financial(returns, "total_return")), "扣除已配置交易成本"],
      ["年化收益", returnPercent(financial(returns, "annualized_return")), "按数据频率年化"],
      ["超额收益", returnPercent(financial(returns, "excess_total_return")), "相对标的买入持有"],
      ["最大回撤", returnPercent(financial(risk, "max_drawdown") == null ? null : -Math.abs(financial(risk, "max_drawdown")!), false), "历史峰值至谷值"],
      ["年化波动率", returnPercent(financial(risk, "annualized_volatility"), false), "净收益波动"],
      ["Sharpe", ratio(financial(risk, "sharpe_ratio"), true), "无风险利率按 0"],
      ["Sortino", ratio(financial(risk, "sortino_ratio"), true), "只惩罚下行波动"],
      ["Calmar", ratio(financial(risk, "calmar_ratio"), true), "年化收益 / 最大回撤"],
      ["活跃周期胜率", percent(financial(trading, "active_bar_win_rate", "win_rate")), "正净收益活跃 K 线占比"],
      ["Profit Factor", ratio(financial(trading, "active_bar_profit_factor", "profit_factor")), "盈利总额 / 亏损总额"],
      ["累计换手", ratio(financial(trading, "total_turnover")), "目标权重绝对变化之和"],
      ["成本拖累", percent(financial(costs, "cost_drag_ratio_to_initial_capital", "cost_drag_ratio"), false), "总成本 / 初始资金"],
    ]} /> : null}

    {provided && <>
      <div className="overflow-x-auto border-t border-edge">
        <table className="w-full whitespace-nowrap text-left text-xs">
          <thead className="bg-paper text-mute"><tr>{["预测时点", "周期", "到期时点", "预测收益率", "实际收益率", "绝对误差", "方向"].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr></thead>
          <tbody>{visible.map((row) => {
            const expected = finite(row.expected_return_pct);
            const actual = finite(row.actual_return_pct);
            const direction = actual == null ? "待评估" : expected === 0 || actual === 0 ? "中性/零收益" : expected != null && expected * actual > 0 ? "命中" : "未命中";
            return <tr key={`${row.timestamp}:${row.horizon_bars}`} className="border-t border-edge/60 text-ink">
              <td className="px-4 py-3">{row.timestamp}</td><td className="px-4 py-3">T+{row.horizon_bars} 根</td><td className="px-4 py-3 text-mute">{row.target_timestamp ?? "尚无到期行情"}</td>
              <td className="px-4 py-3 font-mono">{number(row.expected_return_pct, true)}%</td><td className="px-4 py-3 font-mono">{row.actual_return_pct == null ? "待评估" : `${number(row.actual_return_pct, true)}%`}</td><td className="px-4 py-3 font-mono">{row.error_pct == null ? "—" : `${number(Math.abs(row.error_pct))} pp`}</td><td className="px-4 py-3 text-mute">{direction}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      <div className="flex items-center justify-between border-t border-edge px-5 py-3 text-xs text-mute">
        <span>页面明细 {rows.length} 条{fullForecastCount > rows.length ? `（后端全量 ${fullForecastCount} 条）` : ""} · 第 {currentPage + 1} / {pages} 页</span><div className="flex gap-3">
          <button type="button" className="disabled:opacity-40" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>上一页</button>
          <button type="button" className="disabled:opacity-40" disabled={currentPage + 1 >= pages} onClick={() => setPage(currentPage + 1)}>下一页</button>
        </div>
      </div>
    </>}
  </section>;
}
