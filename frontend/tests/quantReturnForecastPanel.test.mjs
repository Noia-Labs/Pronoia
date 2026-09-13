import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import { buildSync } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const require = createRequire(import.meta.url);
const result = buildSync({
  entryPoints: [new URL("../src/components/backtest/QuantReturnForecastPanel.tsx", import.meta.url).pathname],
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
  packages: "external",
  jsx: "automatic",
});
const compiled = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(require, compiled, compiled.exports);
const QuantReturnForecastPanel = compiled.exports.default;

const summary = {
  n: 8,
  evaluated_count: 8,
  n_forecasts: 10,
  n_predictions: 12,
  eligible_count: 11,
  pending_count: 2,
  coverage: 10 / 12,
  eligible_evaluation_coverage: 8 / 11,
  mae_pct: 0.7,
  rmse_pct: 0.9,
  median_abs_error_pct: 0.5,
  p90_abs_error_pct: 1.4,
  bias_pct: -0.1,
  error_std_pct: 0.8,
  mean_expected_return_pct: 0.2,
  mean_actual_return_pct: 0.3,
  direction_accuracy: 0.625,
  direction_evaluated_count: 8,
  up_call_hit_rate: 0.75,
  up_call_count: 4,
  down_call_hit_rate: 0.5,
  down_call_count: 4,
  pearson_ic: 0.42,
  spearman_rank_ic: 0.38,
  r_squared: 0.12,
  zero_baseline_rmse_pct: 1.1,
  skill_score_vs_zero: 0.18,
};

const finance = {
  status: "available",
  returns: { total_return: 0.12, annualized_return: 0.08, excess_total_return: 0.03 },
  risk: { max_drawdown: 0.06, annualized_volatility: 0.15, sharpe_ratio: 0.8, sortino_ratio: 1.1, calmar_ratio: 1.3 },
  trading: { active_bar_win_rate: 0.53, active_bar_profit_factor: 1.2, total_turnover: 4.5 },
  costs: { cost_drag_ratio_to_initial_capital: 0.004 },
};

const render = (props) => renderToStaticMarkup(React.createElement(QuantReturnForecastPanel, props));

test("forecast panel exposes full-sample comparison metrics and strategy finance separately", () => {
  const html = render({ summary, forecasts: [], financialAnalysis: finance, performanceSummary: {}, predictionOnly: false });
  assert.match(html, /标的收益率预测与策略比较/);
  assert.match(html, /MedAE · 中位绝对误差/);
  assert.match(html, /P90 绝对误差/);
  assert.match(html, /方向命中率/);
  assert.match(html, /Pearson IC/);
  assert.match(html, /Rank IC/);
  assert.match(html, /R² · 样本外解释度/);
  assert.match(html, /零基准技能/);
  assert.match(html, /同一 Run 的策略金融表现/);
  assert.match(html, /Profit Factor/);
  assert.match(html, /成本拖累/);
});

test("prediction-only panel never fabricates portfolio return or Sharpe", () => {
  const html = render({ summary, forecasts: [], financialAnalysis: finance, performanceSummary: {}, predictionOnly: true });
  assert.match(html, /没有组合仓位、模拟成交或策略净值/);
  assert.doesNotMatch(html, /同一 Run 的策略金融表现|Profit Factor/);
});

test("strategy finance remains useful when no explicit return forecast exists", () => {
  const html = render({ summary: { n_forecasts: 0 }, forecasts: [], financialAnalysis: finance, performanceSummary: {}, predictionOnly: false });
  assert.match(html, /没有显式输出未来收益率数值/);
  assert.match(html, /同一 Run 的策略金融表现/);
});
