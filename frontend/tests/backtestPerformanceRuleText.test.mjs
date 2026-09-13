import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import { buildSync } from "esbuild";

const require = createRequire(import.meta.url);
const result = buildSync({
  entryPoints: [new URL("../src/components/BacktestPerformanceDashboard.tsx", import.meta.url).pathname],
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
  packages: "external",
  jsx: "automatic",
});
const compiled = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(require, compiled, compiled.exports);
const { compactRuleCondition } = compiled.exports;

test("quant rule summary preserves an explicit zero threshold", () => {
  assert.match(compactRuleCondition({
    field: "return",
    operator: "crosses_above",
    lookback: 1,
    threshold: 0,
    threshold_unit: "percent",
    consecutive_count: 3,
  }), /阈值 0\.00%/);
});
