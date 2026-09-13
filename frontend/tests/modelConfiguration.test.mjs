import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = (relative) => readFileSync(new URL(relative, import.meta.url), "utf8");
const compile = (text) => {
  const code = ts.transpileModule(text, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText;
  const exports = {};
  new Function("exports", code)(exports);
  return exports;
};
const { hydrateQuantConfiguration, mergeQuantConfiguration, parseStrategyConfiguration } = compile(source("../src/components/backtest/modelConfiguration.ts"));
const list = ts.createSourceFile("BacktestList.tsx", source("../src/components/BacktestList.tsx"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const serializerFunctions = list.statements.filter((node) => ts.isFunctionDeclaration(node) && ["quantThresholdUnit", "serializeQuantCondition"].includes(node.name?.text)).map((node) => node.getText(list)).join("\n");
const { serializeQuantCondition } = compile(serializerFunctions);

const makeSpec = () => ({
  type: "quant", kind: "declarative_rules", version: "original", custom_metadata: { preserve: true },
  parameters: {
    signal_timing: "bar_close", execution_timing: "next_open", retained_parameter: 7,
    entry: { combinator: "all", group_note: "retained", conditions: [
      { field: "return", operator: "above", threshold: 0.03, lookback: 20, consecutive_count: 3, note: "original", warmup_bars: 99 },
      { field: "volume_ratio", operator: "above", threshold: 1.5, lookback: 20, note: "second" },
    ] },
    exit: { combinator: "any", rules: [{ field: "ma_cross", operator: "crosses_below", threshold: -0.01, lookback: 30, fast_period: 10, slow_period: 30 }] },
  },
});

test("saved rules hydrate into display percentages and preserve consecutive counts", () => {
  const draft = hydrateQuantConfiguration(makeSpec(), {});
  assert.equal(draft.quant_entry_combinator, "and");
  assert.equal(draft.quant_exit_combinator, "or");
  assert.equal(draft.quant_conditions[0].threshold, 3);
  assert.equal(draft.quant_conditions[0].consecutive_count, 3);
  assert.equal(draft.quant_conditions[1].threshold, 1.5);
  assert.equal(draft.quant_conditions[1].consecutive_count, 1);
  assert.equal(draft.quant_conditions[2].threshold, -1);
  assert.equal(draft.quant_conditions[2].fast_period, 10);
});

test("editing a rule preserves unknown fields while replacing derived data", () => {
  const original = makeSpec();
  const originalJson = JSON.stringify(original);
  const draft = hydrateQuantConfiguration(original, {});
  draft.quant_conditions[0].threshold = 4;
  draft.quant_conditions[0].consecutive_count = 1;
  draft.strategy_version = "updated";
  const saved = mergeQuantConfiguration(original, draft, serializeQuantCondition);
  assert.deepEqual(saved.custom_metadata, { preserve: true });
  assert.equal(saved.parameters.retained_parameter, 7);
  assert.equal(saved.parameters.entry.group_note, "retained");
  assert.equal(saved.parameters.entry.conditions[0].note, "original");
  assert.equal(saved.parameters.entry.conditions[0].threshold, 0.04);
  assert.equal(saved.parameters.entry.conditions[0].threshold_display, 4);
  assert.ok(!("warmup_bars" in saved.parameters.entry.conditions[0]));
  assert.ok(!("consecutive_count" in saved.parameters.entry.conditions[0]));
  assert.ok(!("rules" in saved.parameters.exit));
  assert.equal(saved.version, "updated");
  assert.equal(JSON.stringify(original), originalJson);
});

test("deleting the first condition does not transfer its metadata to the next", () => {
  const original = makeSpec();
  const draft = hydrateQuantConfiguration(original, {});
  draft.quant_conditions.splice(0, 1);
  const saved = mergeQuantConfiguration(original, draft, serializeQuantCondition);
  assert.equal(saved.parameters.entry.conditions[0].note, "second");
  assert.equal(saved.parameters.entry.conditions[0].field, "volume_ratio");
});

test("legacy relative-volume thresholds convert to equivalent volume ratios", () => {
  const original = makeSpec();
  original.parameters.entry.conditions = [{ field: "volume", operator: "above", threshold: 0.5, lookback: 20 }];
  const draft = hydrateQuantConfiguration(original, {});
  assert.equal(draft.quant_conditions[0].field, "volume_ratio");
  assert.equal(draft.quant_conditions[0].threshold, 1.5);
  const saved = mergeQuantConfiguration(original, draft, serializeQuantCondition);
  assert.equal(saved.parameters.entry.conditions[0].threshold, 1.5);
});

test("unsupported saved rules fall back to complete JSON instead of defaults", () => {
  const original = makeSpec();
  original.parameters.entry.conditions[0].field = "custom_factor";
  assert.equal(hydrateQuantConfiguration(original, {}), null);
  const empty = makeSpec(); empty.parameters.entry.conditions = [];
  assert.equal(hydrateQuantConfiguration(empty, {}), null);
});

test("complete configuration requires an object declaring its model type", () => {
  for (const value of ["[]", "null", '"text"', '{"parameters":{}}']) assert.throws(() => parseStrategyConfiguration(value));
  assert.deepEqual(parseStrategyConfiguration('{"type":"quant","parameters":{"lookback":25}}'), { type: "quant", parameters: { lookback: 25 } });
});
