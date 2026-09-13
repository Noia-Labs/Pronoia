import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";
import ts from "typescript";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const require = createRequire(import.meta.url);
const source = readFileSync(new URL("../src/pages/model-lab/ModelLabResultsPanel.tsx", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText;
const module = { exports: {} };
// Isolate summary calculation and status rendering from browser API/store setup.
const load = (name) => name === "../../utils" ? { cls: (...parts) => parts.filter(Boolean).join(" ") }
  : name.startsWith("../") ? {}
  : require(name);
new Function("require", "module", "exports", compiled)(load, module, module.exports);
const { buildPredictionComparisons, LabStatus } = module.exports;

const fixture = (overrides = {}) => ({
  batch: { id: "batch", name: "基模比较", status: "done", profile_ids: ["profile"], prediction: { runner: "team_full", horizon: "t3" } },
  tasks: [{ id: "task", kind: "prediction", profile_id: "profile", status: "done", config: { runner: "team_full", horizon: "t3" } }],
  qa_results: [],
  summary: { prediction_runs: [{
    task_id: "task", profile_id: "profile", profile_name: "基模 A", model_id: "test-base",
    status: "done", direction_mode: "binary", n_outputs: 10, valid_output_count: 8,
    insufficient_data_count: 1, invalid_output_count: 1, voluntary_abstain_count: 0,
    metrics: {
      primary_oracle_horizon: "t3", acc_primary_directional_trade: { value: 0.75, meta: { n: 8 } },
      acc_t3_strict: { value: 0.6, meta: { n: 10 } },
      return_forecast: { meta: { n_predictions: 10, n_forecasts: 8, evaluated_count: 8, coverage: 0.8, mae_pct: 1, rmse_pct: 2 } },
    },
    prediction_issues: [
      { event_id: "e1", prediction_status: "insufficient_data", reason: "缺少公布值" },
      { event_id: "e2", prediction_status: "invalid_output", reason: "模型 API 超时" },
    ],
    ...overrides,
  }] },
});
const compare = (overrides) => buildPredictionComparisons(fixture(overrides), () => undefined)[0];

test("binary base evaluation excludes insufficient/invalid outputs from accuracy while showing coverage and reasons", () => {
  const result = compare();
  assert.equal(result.directionLabel, "看涨 / 看跌");
  assert.equal(result.directionAccuracy, 0.75);
  assert.equal(result.directionCount, 8);
  assert.equal(result.outputValidity, 0.8);
  assert.equal(result.insufficientCount, 1);
  assert.equal(result.invalidCount, 1);
  assert.equal(result.issues[0].reason, "缺少公布值");
  assert.equal(result.issues[1].reason, "模型 API 超时");
  assert.equal(result.status, "done");
  assert.equal(result.hasWarnings, true);
});

test("binary evaluation never falls back to the legacy strict metric", () => {
  const result = compare({ metrics: { acc_t3_strict: { value: 0.6, meta: { n: 10 } } } });
  assert.equal(result.directionAccuracy, null);
  assert.equal(result.directionCount, null);
});

test("legacy base evaluation preserves its original strict three-class score", () => {
  const result = compare({ direction_mode: undefined });
  assert.equal(result.directionLabel, "历史三分类（严格）");
  assert.equal(result.directionAccuracy, 0.6);
  assert.equal(result.directionCount, 10);
});

test("unscored, empty, and demo outputs do not invent zero accuracy", () => {
  assert.equal(compare({ metrics: { acc_primary_directional_trade: { value: 0, meta: { n: 0 } } } }).directionAccuracy, null);
  const empty = compare({ n_outputs: 0, valid_output_count: 0 });
  assert.equal(empty.outputValidity, null);
  const demo = compare({ demo: true });
  assert.equal(demo.directionAccuracy, null);
  assert.equal(demo.outputValidity, null);
  assert.equal(demo.mae, null);
});

test("metric computation failure remains visible and cannot display stale success scores", () => {
  const result = compare({ metrics_error: "标签文件无法读取" });
  assert.equal(result.metricsError, "标签文件无法读取");
  assert.equal(result.directionAccuracy, null);
  assert.equal(result.mae, null);
  assert.equal(result.coverage, null);
  assert.equal(result.hasWarnings, true);
});

test("synthetic event data hides ability scores but retains real-call output quality and issue reasons", () => {
  const result = compare({ dataset_demo: true, dataset_semantic_quality: "demo_only" });
  assert.equal(result.demo, false);
  assert.equal(result.datasetDemo, true);
  assert.equal(result.directionAccuracy, null);
  assert.equal(result.directionCount, null);
  assert.equal(result.mae, null);
  assert.equal(result.rmse, null);
  assert.equal(result.evaluatedCount, null);
  assert.equal(result.outputValidity, 0.8);
  assert.equal(result.insufficientCount, 1);
  assert.equal(result.invalidCount, 1);
  assert.equal(result.issues[0].reason, "缺少公布值");
});

test("completion warning is a display state and leaves clean or running task labels intact", () => {
  const render = (props) => renderToStaticMarkup(React.createElement(LabStatus, props));
  assert.match(render({ status: "done", hasWarnings: true }), /已完成 · 有问题/);
  assert.doesNotMatch(render({ status: "done" }), /有问题/);
  assert.match(render({ status: "running", hasWarnings: true }), /运行中/);
});
