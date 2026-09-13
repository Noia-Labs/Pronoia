import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("../src/components/BacktestList.tsx", import.meta.url), "utf8");
const list = ts.createSourceFile("BacktestList.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
const helperNames = new Set([
  "syncMarketDatasetDraft",
  "datasetState",
  "marketDatasetHasOhlc",
  "marketDatasetHasExactlyOneSymbol",
  "marketDatasetCanExecutePortfolio",
  "getProtocolHash",
]);
const helpers = list.statements.filter((node) =>
  ts.isFunctionDeclaration(node) && helperNames.has(node.name?.text ?? ""),
);
assert.equal(helpers.length, helperNames.size, "market dataset helpers must remain top-level and testable");
const compiled = ts.transpileModule(helpers.map((node) => node.getText(list)).join("\n"), {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const exports = {};
new Function("exports", compiled)(exports);
const {
  syncMarketDatasetDraft,
  marketDatasetHasExactlyOneSymbol,
  marketDatasetCanExecutePortfolio,
  getProtocolHash,
} = exports;

const draft = () => ({
  market_dataset_id: "old",
  data_market: "CN",
  bar_frequency: "1d",
  frequency: "1d",
  data_asset_type: "equity",
  data_adjustment: "qfq",
  data_calendar: "exchange",
  untouched: "keep",
});

test("a selected 1m index snapshot synchronizes every market data-basis field", () => {
  const result = syncMarketDatasetDraft(draft(), {
    id: "index-1m",
    markets: ["", "HK"],
    frequency: "1m",
    asset_type: "index",
    adjustment: "none",
    calendar: "XHKG",
  });

  assert.deepEqual({
    market_dataset_id: result.market_dataset_id,
    data_market: result.data_market,
    bar_frequency: result.bar_frequency,
    frequency: result.frequency,
    data_asset_type: result.data_asset_type,
    data_adjustment: result.data_adjustment,
    data_calendar: result.data_calendar,
  }, {
    market_dataset_id: "index-1m",
    data_market: "HK",
    bar_frequency: "1m",
    frequency: "1m",
    data_asset_type: "index",
    data_adjustment: "none",
    data_calendar: "XHKG",
  });
  assert.equal(result.untouched, "keep");
});

test("clearing a selection only clears its id and keeps the last explicit basis", () => {
  const result = syncMarketDatasetDraft(draft(), undefined, "");
  assert.equal(result.market_dataset_id, "");
  assert.equal(result.bar_frequency, "1d");
  assert.equal(result.frequency, "1d");
  assert.equal(result.data_asset_type, "equity");
});

test("all four selection paths use the same market dataset synchronizer", () => {
  const references = source.match(/syncMarketDatasetDraft\s*\(/g) ?? [];
  // Declaration plus createDefaultDraft, async catalogue hydration, selectType,
  // and the explicit dataset dropdown.
  assert.equal(references.length, 5);
});

test("quant defaults reject unavailable, non-OHLC, multi-symbol, and non-executable datasets", () => {
  const ready = {
    id: "index-1m",
    dataset_kind: "market",
    status: "available",
    quality_status: "passed",
    frequency: "1m",
    asset_type: "index",
    coverage: { symbols: ["000852.SH"] },
    capabilities: {
      ohlc: true,
      portfolio_execution: true,
      single_asset_execution_ready: true,
      multi_symbol_data: false,
    },
  };
  assert.equal(marketDatasetCanExecutePortfolio(ready), true);
  assert.equal(marketDatasetCanExecutePortfolio({ ...ready, status: "pending" }), false);
  assert.equal(marketDatasetCanExecutePortfolio({ ...ready, capabilities: { ...ready.capabilities, ohlc: false } }), false);
  assert.equal(marketDatasetCanExecutePortfolio({ ...ready, capabilities: { ...ready.capabilities, portfolio_execution: false } }), false);
  assert.equal(marketDatasetCanExecutePortfolio({ ...ready, capabilities: { ...ready.capabilities, multi_symbol_data: true } }), false);
  assert.equal(marketDatasetCanExecutePortfolio({ ...ready, coverage: { symbols: ["A", "B"] } }), false);
  assert.equal(marketDatasetCanExecutePortfolio({ ...ready, coverage: { symbols: [] } }), false);
  assert.equal(marketDatasetCanExecutePortfolio({ ...ready, coverage: { symbols: 0 } }), false);
  assert.equal(marketDatasetCanExecutePortfolio({ ...ready, coverage: {}, symbols: [] }), false);
  assert.match(source, /next\.find\(marketDatasetReady\)/);
  assert.match(source, /type === "event" \? marketDatasetHasOhlc\(item\) : marketDatasetCanExecutePortfolio\(item\)/);
});

test("single-symbol eligibility reconciles legacy root and coverage representations", () => {
  const base = { id: "legacy", name: "legacy", path: "", total_events: 0, by_market: {}, by_type: {}, by_symbol: {} };
  assert.equal(marketDatasetHasExactlyOneSymbol({ ...base, symbols: ["000852.SH"], coverage: { symbols: [] } }), true);
  assert.equal(marketDatasetHasExactlyOneSymbol({ ...base, symbols: [], coverage: { symbols: ["000852.SH"] } }), true);
  assert.equal(marketDatasetHasExactlyOneSymbol({ ...base, symbols: [], coverage: { symbols: 1 } }), true);
  assert.equal(marketDatasetHasExactlyOneSymbol({ ...base, symbols: ["000852.SH"], coverage: { symbols: 2 } }), false);
  assert.equal(marketDatasetHasExactlyOneSymbol({ ...base, symbols: ["000852.SH"], coverage: { symbols: 0 } }), false);
  assert.equal(marketDatasetHasExactlyOneSymbol({ ...base, coverage: { symbols: 1.5 } }), false);
  assert.equal(marketDatasetHasExactlyOneSymbol({ ...base, symbols: ["A", "B"], coverage: { symbols: ["A"] } }), false);
  assert.equal(marketDatasetHasExactlyOneSymbol({ ...base, symbols: ["A", " A "] }), true);
});

test("Arena-safe Run cards use the public comparison hash when protocol_hash is redacted", () => {
  const publicHash = "c".repeat(64);
  assert.equal(getProtocolHash({
    protocol_hash: null,
    comparison_protocol_hash: publicHash,
    config: { arena_comparison: { comparison_protocol_hash: publicHash } },
  }), publicHash);
});
