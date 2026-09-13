import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import { buildSync } from "esbuild";

const require = createRequire(import.meta.url);
const result = buildSync({
  entryPoints: [new URL("../src/components/ArenaList.tsx", import.meta.url).pathname],
  bundle: true,
  write: false,
  platform: "node",
  format: "cjs",
  packages: "external",
  jsx: "automatic",
});
const compiled = { exports: {} };
new Function("require", "module", "exports", result.outputFiles[0].text)(require, compiled, compiled.exports);
const {
  arenaComparisonForRun,
  groupRuns,
  runEligibility,
} = compiled.exports;

const hash = (letter) => letter.repeat(64);

function safeRun(id, overrides = {}) {
  const engineMode = overrides.engine_mode ?? "portfolio";
  const event = engineMode === "event_proxy";
  return {
    id,
    name: id,
    status: "done",
    runner: "arena_safe",
    strategy_type: event ? "event" : "quant",
    engine_mode: engineMode,
    result_nature: event ? "proxy" : "simulated_from_real_bars",
    visibility: "arena_safe",
    events_path: "[arena-safe-redacted]",
    labels_path: null,
    out_path: "[arena-safe-redacted]",
    result_path: null,
    concurrency: 0,
    total_events: 100,
    done_events: 100,
    created_at: "2026-01-01",
    updated_at: "2026-01-01",
    comparison_signature: {
      version: "PRIVATE-SIGNATURE-MUST-NOT-BE-USED",
      window: { start_date: "SECRET_WINDOW" },
    },
    config: {
      arena_eligibility: {
        eligible: true,
        formal_eligible: true,
        has_event_snapshot: event,
        has_labels_snapshot: event,
        has_result_artifact: !event,
        prediction_only: false,
        has_numeric_forecast: false,
        evaluated_forecast_count: null,
        performance_sample_sufficient: true,
        forecast_sample_sufficient: true,
        point_in_time_enforced: true,
        external_portfolio: false,
        event_content_verified: event,
        model_lab_demo: false,
      },
      arena_comparison: {
        version: event ? "pronoia-event-arena-comparison-v2" : "pronoia-arena-comparison-v2",
        dataset: {
          dataset_version: "dsv-public",
          snapshot_hash: hash("a"),
          ...(event ? { labels_sha256: hash("b") } : {}),
        },
        benchmark: "dataset_asset_buy_hold",
        execution_timing: { execution_delay: "next_open" },
        execution_constraints: event ? null : { max_abs_weight: 1, allow_short: true },
        costs: { commission_bps: 3 },
        evaluation: { evaluator_version: "v1" },
        engine_mode: engineMode,
        result_nature: event ? "proxy" : "simulated_from_real_bars",
        comparison_protocol_hash: hash("c"),
      },
    },
    ...overrides,
  };
}

test("Arena-safe candidates use public eligibility rather than redacted artifact paths", () => {
  assert.equal(runEligibility(safeRun("portfolio"), false, "performance").eligible, true);
  assert.equal(runEligibility(safeRun("event", { engine_mode: "event_proxy" }), false, "performance").eligible, true);

  const missing = safeRun("missing");
  missing.config.arena_eligibility.has_result_artifact = false;
  assert.equal(runEligibility(missing, false, "performance").eligible, false);
});

test("formal eligibility fails closed while exploration honors the auditable flag", () => {
  const run = safeRun("exploration-only");
  run.config.arena_eligibility.formal_eligible = false;
  assert.equal(runEligibility(run, false, "performance").eligible, false);
  assert.equal(runEligibility(run, true, "performance").eligible, true);

  const unspecified = safeRun("unspecified");
  delete unspecified.config.arena_eligibility.eligible;
  assert.equal(runEligibility(unspecified, true, "performance").eligible, false);
});

test("public comparison groups redacted Runs without consulting private signatures", () => {
  const first = safeRun("first");
  const second = safeRun("second");
  second.comparison_signature = {
    version: "DIFFERENT-PRIVATE-SIGNATURE",
    window: { start_date: "ANOTHER_SECRET_WINDOW" },
  };

  const exposed = arenaComparisonForRun(first);
  assert.equal(exposed.window, undefined);
  assert.doesNotMatch(JSON.stringify(exposed), /SECRET_WINDOW|PRIVATE-SIGNATURE/);

  const groups = groupRuns([first, second], false, "performance");
  assert.equal(groups.length, 1);
  assert.equal(groups[0].runs.length, 2);
  assert.equal(groups[0].strict, true);
});

test("formal event candidates require server-verified content hashes", () => {
  const run = safeRun("event", { engine_mode: "event_proxy" });
  run.config.arena_eligibility.event_content_verified = false;
  assert.equal(runEligibility(run, false, "performance").eligible, false);
  assert.equal(runEligibility(run, true, "performance").eligible, true);
});

test("Arena-safe candidates fail closed on effective per-track sample sizes", () => {
  const performance = safeRun("thin-performance");
  performance.config.arena_eligibility.performance_sample_sufficient = false;
  assert.equal(runEligibility(performance, false, "performance").eligible, false);
  assert.match(runEligibility(performance, false, "performance").reason, /不足 5/);

  const forecast = safeRun("thin-forecast", { engine_mode: "event_proxy" });
  forecast.config.arena_eligibility.forecast_sample_sufficient = false;
  assert.equal(runEligibility(forecast, false, "forecast").eligible, false);
  assert.match(runEligibility(forecast, false, "forecast").reason, /不足 5/);
});
