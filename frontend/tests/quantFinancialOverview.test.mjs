import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import { buildSync } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const require = createRequire(import.meta.url);
const result = buildSync({
  entryPoints: [new URL("../src/components/backtest/QuantFinancialOverview.tsx", import.meta.url).pathname],
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
  packages: "external",
  jsx: "automatic",
});
const compiled = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(require, compiled, compiled.exports);
const QuantFinancialOverview = compiled.exports.default;

const run = {
  id: "quant-1",
  dataset_version: "dsv-test",
  result_nature: "simulated_from_real_bars",
  execution_spec: {
    requested: { start_date: "2025-12-01", end_date: "2026-01-31" },
    applied: { benchmark: "dataset_asset_buy_hold" },
  },
};

const data = {
  status: "available",
  data_frozen: true,
  result_nature: "simulated_from_real_bars",
  dataset_version: "dsv-test",
  summary: {},
  equity_curve: [],
  drawdown_curve: [],
  positions: [{ timestamp: "2026-01-02T09:31:00", equity: 910000, weight: 0.5 }],
  dataset: {
    symbol: "000300.SH",
    market: "CN",
    frequency: "1m",
    source_type: "local_file",
    metadata: {
      adjustment: "none",
      requested_start: "2025-12-01",
      requested_end: "2026-01-31",
      available_start: "2026-01-02T01:30:00Z",
      available_end: "2026-01-02T07:00:00Z",
      effective_start: "2026-01-02T01:30:00Z",
      effective_end: "2026-01-02T07:00:00Z",
      window_clipped: true,
    },
  },
  effective_protocol: { applied: { benchmark: "dataset_asset_buy_hold", currency: null } },
  financial_analysis: {
    schema_version: "v1",
    metrics_basis: "full_frozen_result",
    units: { money: "unspecified" },
    period: {
      start_at: "2026-01-02T01:30:00Z",
      end_at: "2026-01-02T07:00:00Z",
      frequency: "1m",
      bar_count: 240,
      return_observation_count: 239,
      annualization_periods: 60480,
      estimated_years: 239 / 60480,
    },
    returns: {
      initial_capital: 1000000,
      final_equity: 910000,
      total_return: -0.09,
      annualized_return: -0.5,
      benchmark_total_return: 0.02,
      excess_total_return: -0.11,
    },
    risk: {
      max_drawdown: 0.12,
      annualized_volatility: 0.2,
      sharpe_ratio: -1.2,
      sortino_ratio: -1.4,
      historical_var_95_one_bar: 0.01,
      historical_cvar_95_one_bar: 0.015,
      peak_timestamp: "SECRET_PEAK_TIMESTAMP",
      trough_timestamp: "SECRET_TROUGH_TIMESTAMP",
    },
    benchmark: { correlation: 0.7, beta: 0.8, aligned_return_count: 239 },
    trading: { position_change_count: 12, active_bar_count: 100, active_bar_win_rate: 0.51, active_bar_profit_factor: 1.1 },
    exposure: { time_in_market_ratio: 0.6, current_exposure: 0.5 },
    costs: { total_cost: 1000, cost_drag_ratio_to_initial_capital: 0.001 },
    methodology: {
      var_basis: "historical one-bar return distribution at 95% confidence; loss is reported positive",
      win_rate_basis: "positive net-return share of active bars; not completed-trade win rate",
    },
  },
};

const render = (arenaSafe, runValue = run, dataValue = data) => renderToStaticMarkup(React.createElement(QuantFinancialOverview, {
  run: runValue,
  data: dataValue,
  loading: false,
  error: null,
  arenaSafe,
  onOpenCharts() {},
  onOpenAudit() {},
}));

test("private quant overview renders auditable finance detail without guessing CNY", () => {
  const html = render(false);
  assert.match(html, /量化金融概览/);
  assert.match(html, /历史 VaR 95%/);
  assert.match(html, /活跃 bar 盈亏比/);
  assert.match(html, /不是外部 ETF 或含分红总回报指数/);
  assert.match(html, /仓位变更次数/);
  assert.match(html, /不是已完成交易胜率/);
  assert.match(html, /请求执行区间/);
  assert.match(html, /实际执行交集/);
  assert.match(html, /请求日期部分超出行情覆盖/);
  assert.match(html, /2025-12-01 → 2026-01-31/);
  assert.match(html, /2026-01-02 09:30 Asia\/Shanghai/);
  assert.match(html, /2026-01-02 15:00 Asia\/Shanghai/);
  assert.match(html, /资金单位未声明/);
  assert.doesNotMatch(html, /¥|￥/);
});

test("Arena-safe overview never mounts strategy timing or exposure detail", () => {
  const html = render(true);
  assert.match(html, /Arena-safe 只展示可比较的聚合收益、风险、基准与成本/);
  assert.match(html, /请求执行区间/);
  assert.match(html, /实际执行交集/);
  assert.match(html, /Arena-safe 已隐藏/);
  assert.doesNotMatch(html, /仓位变更次数|组合敞口与期末状态|SECRET_PEAK_TIMESTAMP|SECRET_TROUGH_TIMESTAMP|2026-01-02 09:30/);
});

test("legacy quant overview reads requested bounds from config without inventing full coverage", () => {
  const legacyRun = {
    ...run,
    execution_spec: null,
    config: {
      execution_spec: {
        requested: { start_date: "2024-06-01", end_date: "2024-06-30" },
        applied: { benchmark: "dataset_asset_buy_hold" },
      },
    },
  };
  const legacyData = {
    ...data,
    dataset: { ...data.dataset, metadata: { adjustment: "none" } },
  };
  const html = render(false, legacyRun, legacyData);
  assert.match(html, /2024-06-01 → 2024-06-30/);
  assert.match(html, /完整数据覆盖/);
  assert.match(html, /未返回 → 未返回/);
  assert.doesNotMatch(html, /请求日期部分超出行情覆盖/);
});
