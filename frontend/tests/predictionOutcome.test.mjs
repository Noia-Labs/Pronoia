import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import { buildSync } from "esbuild";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";

const require = createRequire(import.meta.url);
const result = buildSync({
  entryPoints: [new URL("../src/components/BacktestDetailWidgets.tsx", import.meta.url).pathname],
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
  packages: "external",
  jsx: "automatic",
});
const compiled = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(require, compiled, compiled.exports);
const { predictionOutcome, MetricsGrid, CatalogRow } = compiled.exports;

const makePrediction = (overrides = {}) => ({
  id: "p1", run_id: "r1", event_id: "e1", pred_direction: "up", abstain: false,
  confidence: 0.75, oracle_label_t3: "up", is_correct_t3: true, ...overrides,
});
const insufficient = makePrediction({
  pred_direction: "neutral", abstain: true, confidence: 0.5,
  rationale: "缺少公告正文与公布数值",
  strategy_metadata: { prediction_status: "insufficient_data" },
});
const invalid = makePrediction({
  pred_direction: "neutral", abstain: true,
  strategy_metadata: { output_failure_kind: "invalid_json" },
});
const catalog = (predictions) => predictions.map((prediction, index) => ({
  event_id: `e${index}`, status: "done", symbol: "000300.SH", prediction,
}));
const render = (Component, props) => renderToStaticMarkup(React.createElement(Component, props));

test("insufficient input is separate from neutral, wrong, and technical failure", () => {
  assert.equal(predictionOutcome(insufficient), "insufficient_data");
  assert.equal(predictionOutcome({ ...insufficient, strategy_metadata: null, prediction_status: "insufficient_data" }), "insufficient_data");
  assert.equal(predictionOutcome(invalid), "invalid_output");
  assert.equal(predictionOutcome(makePrediction({ pred_direction: "neutral", is_correct_t3: false })), "valid_neutral");
  assert.equal(predictionOutcome(makePrediction({ abstain: true })), "voluntary_abstain");
  assert.equal(predictionOutcome(makePrediction({ pred_direction: "down", is_correct_t3: false })), "incorrect");
});

test("catalog presents insufficient data without fake confidence or neutral classification", () => {
  const html = render(CatalogRow, { item: catalog([insufficient])[0], rowIdx: 1, isOpen: false, onToggle() {} });
  assert.match(html, /数据不足/);
  assert.match(html, /缺少公告正文与公布数值/);
  assert.match(html, /不计准确率/);
  assert.doesNotMatch(html, /50%|有效中性|三分类|主动弃权/);
});

test("binary metrics separate insufficiency and invalid output and exclude both from accuracy", () => {
  const html = render(MetricsGrid, {
    m: { run_id: "r1", n_total: 2, metrics: {}, direction_mode: "binary" },
    catalogItems: catalog([makePrediction(), makePrediction({ pred_direction: "down", is_correct_t3: false }), insufficient, invalid]),
  });
  assert.match(html, /预测方向准确率/);
  assert.match(html, /50\.0%/);
  assert.match(html, /k=1 \/ n=2/);
  assert.match(html, /数据不足 1 条 \/ 已返回 4 条/);
  assert.match(html, /无效输出 1 条/);
  assert.match(html, /有效 2 · 数据不足 1 · 输出无效 1/);
  assert.doesNotMatch(html, /三分类准确率|中性占比/);
});

test("missing actual labels do not reduce valid output coverage or inflate direction accuracy", () => {
  const html = render(MetricsGrid, {
    m: { run_id: "r1", n_total: 0, metrics: {} }, directionMode: "binary",
    catalogItems: catalog([makePrediction({ oracle_label_t3: null, is_correct_t3: null }), insufficient]),
  });
  assert.match(html, /有效 1 · 数据不足 1 · 输出无效 0 · 主动弃权 0 · 实际标签缺失 1/);
  assert.match(html, /k=0 \/ n=0/);
  assert.match(html, /k=1 \/ n=2/);
});

test("old runs retain their original three-class presentation", () => {
  const html = render(MetricsGrid, {
    m: { run_id: "old", n_total: 1, metrics: {} },
    catalogItems: catalog([makePrediction({ pred_direction: "neutral", oracle_label_t3: "neutral" })]),
  });
  assert.match(html, /三分类准确率/);
  assert.match(html, /有效输出中的中性占比/);
  assert.match(html, /100\.0%/);
});

test("server summary uses all outputs when the event catalog is hidden or filtered", () => {
  const html = render(MetricsGrid, {
    m: { run_id: "r1", n_total: 1, n_outputs: 4, metrics: {}, direction_mode: "binary",
      insufficient_data_count: 2, invalid_output_count: 1, voluntary_abstain_count: 0 },
  });
  assert.match(html, /数据不足 2 条 \/ 已返回 4 条/);
  assert.match(html, /有效 1 · 数据不足 2 · 输出无效 1/);
  assert.match(html, /25\.0%/);
});
