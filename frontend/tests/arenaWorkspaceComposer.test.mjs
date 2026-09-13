import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import { buildSync } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const require = createRequire(import.meta.url);

function compile(entry) {
  const result = buildSync({
    entryPoints: [new URL(entry, import.meta.url).pathname],
    bundle: true,
    write: false,
    platform: "node",
    format: "cjs",
    packages: "external",
    jsx: "automatic",
  });
  const compiled = { exports: {} };
  new Function("require", "module", "exports", result.outputFiles[0].text)(require, compiled, compiled.exports);
  return compiled.exports;
}

const workspace = compile("../src/components/arena/arenaWorkspace.ts");
const comparison = compile("../src/components/arena/arenaComparison.ts");

test("workspace catalogue helpers keep Arena tracks and target scopes separate", () => {
  assert.equal(workspace.workspaceModelTrack({ kind: "quant" }), "quant");
  assert.equal(workspace.workspaceModelTrack({ kind: "imported_predictions", track: "quant" }), "quant");
  assert.equal(workspace.workspaceModelTrack({ kind: "pronoia", track: "event" }), "event");
  assert.equal(workspace.workspaceTargetScope({ kind: "event" }), "single_event");
  assert.equal(workspace.workspaceTargetScope({ kind: "event_set" }), "event_set");
  assert.equal(workspace.workspaceTargetScope({ kind: "asset" }), "asset");
  assert.equal(workspace.workspaceTargetAvailabilityReason({ available: false, reason: "Oracle 快照已变化" }), "Oracle 快照已变化");
  assert.equal(workspace.workspaceTargetAvailabilityReason({ available: true }), "");
});

test("quant frequency options preserve the frozen dataset metadata", () => {
  const options = workspace.workspaceFrequencyOptions({
    default_frequency: "1d",
    available_frequencies: [
      { frequency: "1m", dataset_id: "minute", snapshot_hash: "a".repeat(64), start_date: "2026-01-01" },
      { frequency: "5m", dataset_id: "five", snapshot_hash: "b".repeat(64) },
      { frequency: "1d", dataset_id: "daily", snapshot_hash: "c".repeat(64) },
    ],
  });
  assert.deepEqual(options.map((item) => item.frequency), ["1m", "5m", "1d"]);
  assert.equal(options[0].dataset_id, "minute");
  assert.equal(options[2].snapshot_hash, "c".repeat(64));
});

test("composer is a four-page wizard with one vertical scroll owner", () => {
  const source = readFileSync(new URL("../src/components/arena/ArenaWorkspaceComposer.tsx", import.meta.url), "utf8");
  assert.equal(source.match(/overflow-y-auto/g)?.length, 1);
  for (const step of [1, 2, 3, 4]) assert.match(source, new RegExp(`activeStep === ${step} && <section`));
  assert.match(source, /Quant Arena/);
  assert.match(source, /Event Arena/);
  assert.match(source, /target_scope: form\.targetScope/);
  assert.match(source, /track: form\.track/);
  assert.match(source, /单事件/);
  assert.match(source, /事件集/);
  assert.match(source, /oracle\?\.available_horizons/);
  assert.doesNotMatch(source, /target_mode|\blane\b/);
});

test("event comparison results use the shared result guard", () => {
  assert.equal(comparison.isModelComparisonResult({
    schema_version: "arena-event-comparison-v1",
    models: [],
  }), true);
  assert.equal(comparison.isModelComparisonResult({
    schema_version: "arena-model-comparison-v1",
    models: [],
  }), true);
});

test("event result view exposes set metrics and per-event drilldown fields", () => {
  const source = readFileSync(new URL("../src/components/arena/ModelComparisonResults.tsx", import.meta.url), "utf8");
  for (const label of ["准确率", "胜率", "平均方向收益", "累计方向收益", "最大回撤", "逐事件结果", "方向标签", "模型预测", "实际走势 / 标的收益", "方向净收益"]) {
    assert.match(source, new RegExp(label));
  }
  assert.match(source, /event_details/);
  assert.match(source, /arena-event-comparison-v1/);
});

test("event result envelope renders a profitable bearish prediction", () => {
  const Results = compile("../src/components/arena/ModelComparisonResults.tsx").default;
  const result = {
    schema_version: "arena-event-comparison-v1",
    track: "event",
    subject: { key: "set", kind: "event_set", title: "测试事件集", symbol: "EVENT_SET", market: "MULTI" },
    market: { symbol: "EVENT_SET", market: "MULTI", frequency: "1d", start_at: "2026-01-01", end_at: "2026-01-02", bar_count: 1 },
    rules: { holding_bars: 3, evaluation_horizon: "t3", round_trip_cost_bps: 10 },
    models: [{
      run_id: "model-1",
      name: "事件模型一",
      model_kind: "pronoia",
      metrics: { accuracy: 1, win_rate: 1, average_return: 0.029, total_return: 0.029, max_drawdown: 0, excess_total_return: null, trade_count: 1, total_cost: 100, event_count: 1, prediction_count: 1 },
      curve: [],
      forecast: { n: 0, directional_accuracy: 1 },
      coverage: { signal_count: 1, covered_bars: 1, total_bars: 1, ratio: 1 },
      warnings: [],
      event_details: [{
        event_id: "event-1",
        title: "测试事件",
        time: "2026-01-01",
        symbol: "TEST",
        label: "down",
        prediction: "down",
        confidence: 0.8,
        actual_direction: "down",
        actual_return: -0.03,
        directional_return: 0.03,
        net_directional_return: 0.029,
        pnl: 2900,
        metrics: { is_correct: true, is_win: true, active_trade: true, horizon: "t3" },
      }],
    }],
    benchmark_curve: [],
    bars: [],
    events: [{ event_id: "event-1" }],
    notes: [],
  };
  const html = renderToStaticMarkup(React.createElement(Results, { result }));
  assert.match(html, /逐事件结果/);
  assert.match(html, /看跌/);
  assert.match(html, /\+2\.90%/);
});
