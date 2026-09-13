import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { test } from "node:test";

const require = createRequire(import.meta.url);
const ts = require("typescript");
const source = readFileSync(new URL("../src/components/backtest/runHistorySelection.ts", import.meta.url), "utf8");
const code = ts.transpileModule(source, {
  compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS },
}).outputText;
const compiled = { exports: {} };
new Function("exports", "module", code)(compiled.exports, compiled);

const {
  canDeleteHistoryEntry,
  historyEntryKey,
  toggleVisibleHistorySelection,
} = compiled.exports;

const run = (id, status = "done", qaStatus) => ({ id, recordType: "run", status, qaStatus });
const batch = (id, status = "done") => ({ id, recordType: "batch", status });

test("history keys keep runs and batches with the same id distinct", () => {
  assert.equal(historyEntryKey(run("same")), "run:same");
  assert.equal(historyEntryKey(batch("same")), "batch:same");
});

test("running work and a running QA sidecar cannot be selected for deletion", () => {
  for (const status of ["running", "paused", "interrupted", "starting", "queued", "cancelling"])
    assert.equal(canDeleteHistoryEntry(run(status, status)), false, status);
  assert.equal(canDeleteHistoryEntry(run("terminal", "done", "running")), false);
  assert.equal(canDeleteHistoryEntry(run("pending", "pending")), true);
  assert.equal(canDeleteHistoryEntry(batch("failed", "failed")), true);
});

test("select all only changes selectable records in the current filtered result", () => {
  const visible = [run("one"), batch("two"), run("active", "running")];
  const initial = new Set(["run:hidden"]);
  const selected = toggleVisibleHistorySelection(initial, visible);
  assert.deepEqual([...selected].sort(), ["batch:two", "run:hidden", "run:one"]);
  const cleared = toggleVisibleHistorySelection(selected, visible);
  assert.deepEqual([...cleared], ["run:hidden"]);
});
