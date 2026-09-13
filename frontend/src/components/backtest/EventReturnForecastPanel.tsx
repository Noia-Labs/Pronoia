import { useState } from "react";
import type { BTMetricItem } from "../../types";
import type { QuantReturnForecastSummary } from "./QuantReturnForecastPanel";

export interface EventReturnForecastRow {
  event_id: string;
  symbol?: string;
  event_time?: string;
  horizon?: string;
  expected_return_pct: number;
  actual_return_pct?: number | null;
  error_pct?: number | null;
}
const number = (value: unknown, signed = false) => typeof value !== "number" || !Number.isFinite(value) ? "—" : `${signed && value > 0 ? "+" : ""}${value.toFixed(3)}`;
const percent = (value: unknown, signed = false) => typeof value !== "number" || !Number.isFinite(value) ? "—" : `${signed && value > 0 ? "+" : ""}${(value * 100).toFixed(1)}%`;

export default function EventReturnForecastPanel({ metric, summary, rows = [], horizon, arenaSafe }: { metric?: BTMetricItem; summary?: QuantReturnForecastSummary | null; rows?: EventReturnForecastRow[]; horizon: string; arenaSafe?: boolean }) {
  const meta = { ...(metric?.meta ?? {}), ...(summary ?? {}) };
  const [page, setPage] = useState(0);
  const pages = Math.max(1, Math.ceil(rows.length / 20));
  const current = Math.min(page, pages - 1);
  const provided = Number(meta.n_forecasts ?? rows.length) > 0;
  return <section className="overflow-hidden rounded-2xl border border-edge bg-card">
    <div className="border-b border-edge px-5 py-4"><h3 className="text-sm font-semibold text-ink">事件后的收益率预测 · {horizon.toUpperCase().replace("T", "T+")}</h3><p className="mt-2 text-xs leading-relaxed text-mute">模型预测事件后标的的涨跌幅数值，与同一评价窗口的实际标的收益率比较。盘后事件按下一收盘锚定；误差 = 预测 − 实际，单位为百分点。汇总指标按后端完整已到期样本计算。</p></div>
    {!provided ? <p className="px-5 py-7 text-sm text-mute">尚无收益率数值预测。旧模型只输出方向的结果在此显示为未提供；已完成预测后才计算覆盖率与误差。</p> : <>
      <div className="border-b border-edge px-5 pb-2 pt-4"><h4 className="text-xs font-semibold text-ink">误差、方向与预测技能</h4><p className="mt-1 text-[10px] text-mute">IC、Rank IC、R² 与技能分数仅适合在相同事件集、预测期限和评价口径下比较。</p></div>
      <div className="grid grid-cols-2 gap-px bg-edge sm:grid-cols-3 xl:grid-cols-6">{[
        ["MAE · 平均绝对误差", number(meta.mae_pct), "百分点 · 越小越好"],
        ["RMSE · 均方根误差", number(meta.rmse_pct), "百分点 · 越小越好"],
        ["MedAE · 中位绝对误差", number(meta.median_abs_error_pct), "对极端值更稳健"],
        ["P90 绝对误差", number(meta.p90_abs_error_pct), "90% 样本不超过此值"],
        ["Bias · 平均偏差", number(meta.bias_pct, true), "正值表示整体高估"],
        ["误差波动", number(meta.error_std_pct), "误差总体标准差"],
        ["方向命中率", percent(meta.direction_accuracy), `${meta.direction_evaluated_count ?? 0} 个非零方向样本`],
        ["看涨判断命中率", percent(meta.up_call_hit_rate), `${meta.up_call_count ?? 0} 个看涨判断`],
        ["看跌判断命中率", percent(meta.down_call_hit_rate), `${meta.down_call_count ?? 0} 个看跌判断`],
        ["Pearson IC", number(meta.pearson_ic, true), "线性相关 · 越高越好"],
        ["Rank IC", number(meta.spearman_rank_ic, true), "排序相关 · 越高越好"],
        ["R² · 样本外解释度", number(meta.r_squared, true), "可为负；越高越好"],
        ["零基准技能", percent(meta.skill_score_vs_zero, true), "正值优于始终预测 0%"],
        ["预测覆盖率", percent(meta.coverage), `${meta.n_forecasts ?? 0} / ${meta.n_predictions ?? 0} 条预测`],
        ["评估覆盖率", percent(meta.evaluation_coverage), `${meta.evaluated_count ?? meta.n ?? 0} 个已到期样本`],
        ["待到期预测", String(meta.pending_count ?? 0), "尚未具备评价行情"],
      ].map(([label, value, note]) => <div key={label} className="bg-card px-4 py-4"><p className="text-[11px] text-mute">{label}</p><p className="mt-2 font-mono text-xl text-ink">{value}</p><p className="mt-1 text-[10px] text-faint">{note}</p></div>)}</div>
      {arenaSafe ? <p className="px-5 py-5 text-xs text-mute">Arena 安全摘要展示汇总误差，逐事件预测按当前可见性隐藏。</p> : <><div className="overflow-x-auto"><table className="w-full whitespace-nowrap text-left text-xs"><thead className="bg-paper text-mute"><tr>{["事件 / 标的", "事件时间", "预测收益率", "实际收益率", "误差（百分点）"].map((label) => <th key={label} className="px-4 py-3 font-medium">{label}</th>)}</tr></thead><tbody>{rows.slice(current * 20, current * 20 + 20).map((row) => <tr key={row.event_id} className="border-t border-edge"><td className="px-4 py-3 text-ink">{row.symbol || row.event_id}<span className="ml-2 text-faint">{row.symbol ? row.event_id : ""}</span></td><td className="px-4 py-3 text-mute">{row.event_time || "—"}</td><td className="px-4 py-3 font-mono">{number(row.expected_return_pct, true)}%</td><td className="px-4 py-3 font-mono">{row.actual_return_pct == null ? "待评估" : `${number(row.actual_return_pct, true)}%`}</td><td className="px-4 py-3 font-mono">{number(row.error_pct, true)}</td></tr>)}</tbody></table></div><div className="flex items-center justify-between border-t border-edge px-5 py-3 text-xs text-faint"><span>{rows.length} 条 · 第 {current + 1}/{pages} 页</span><div className="flex gap-3"><button type="button" disabled={current === 0} onClick={() => setPage(current - 1)} className="disabled:opacity-40">上一页</button><button type="button" disabled={current + 1 >= pages} onClick={() => setPage(current + 1)} className="disabled:opacity-40">下一页</button></div></div></>}
    </>}
  </section>;
}
