import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/simulationPresentation.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { observations, observationMarkdown, observationWindow, safeSourceUrl } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

test("old and new artifacts produce a deduplicated checklist with both kinds of conditions", () => {
  const old = { scenarios: [{ label: "融资恢复", triggers: ["承诺延期", "承诺延期", null], invalidation_conditions: ["承诺撤回"] }] };
  assert.equal(observations(old).length, 2);
  assert.match(observationMarkdown(old), /留意信号：承诺延期/);
  assert.match(observationMarkdown(old), /重新判断：承诺撤回/);
  assert.equal(observations({ scenarios: [{ trigger_conditions: ["新合同"] }] }).length, 1);
  assert.deepEqual(observations({}), []);
});

test("export preserves the cutoff and cited evidence without inventing monitoring", () => {
  const markdown = observationMarkdown({ source: { as_of: "2026-09-06", horizon_days: 7 }, scenarios: [{ triggers: ["公告"], evidence_refs: ["F1"] }], evidence: [{ id: "F1", statement: "原始公告", source_url: "https://example.org/notice" }, { id: "F2", statement: "无关证据" }] });
  assert.match(markdown, /自 2026-09-06 起 7 个自然日/);
  assert.match(markdown, /2026-09-06/);
  assert.match(markdown, /https:\/\/example.org\/notice/);
  assert.doesNotMatch(markdown, /无关证据/);
});

test("only navigable public source URLs become links", () => {
  assert.equal(safeSourceUrl("javascript:alert(1)"), undefined);
  assert.equal(safeSourceUrl("fever://artifact/g/evidence/1"), undefined);
  assert.equal(safeSourceUrl("https://example.org/notice"), "https://example.org/notice");
});

test("observation cards keep sources and windows without duplicate legacy conditions", () => {
  const payload = { scenarios: [{ label: "复核", triggers: ["旧信号"], observations: [{ kind: "trigger", signal: "监管公布补救要求", source: "监管决定", window: "未来30天", evidence_refs: ["F2"] }] }, { label: "复核", triggers: ["另一情景"] }] };
  const items = observations(payload);
  assert.equal(items.length, 2);
  assert.equal(items[0].source, "监管决定");
  const markdown = observationMarkdown(payload);
  assert.match(markdown, /核对渠道：监管决定/);
  assert.match(markdown, /复核窗口：未来30天/);
  assert.match(markdown, /依据：F2/);
  assert.doesNotMatch(markdown, /旧信号/);
  assert.equal(markdown.match(/## 复核/g).length, 2);
});

test("flagged conditions remain visible but are not exported as ready checkboxes", () => {
  const payload = { scenarios: [{ observations: [
    { kind: "trigger", signal: "48小时内增加50%持仓", source: "13F", review_status: "needs_review", review_findings: [{ message: "报告时点不匹配" }] },
    { kind: "invalidation", signal: "公开撤回方案", source: "公司公告", review_status: "no_rule_findings" },
  ] }] };
  assert.equal(observations(payload).length, 2);
  const markdown = observationMarkdown(payload);
  assert.match(markdown, /待核对后使用：48小时内增加50%持仓/);
  assert.match(markdown, /报告时点不匹配/);
  assert.doesNotMatch(markdown, /\[ \] 留意信号：48小时/);
  assert.match(markdown, /\[ \] 重新判断：公开撤回方案/);
});

test("historical scenarios retain their original dates when opened or copied later", () => {
  const payload = { source: { as_of: "2025-08-30T23:59:59+08:00", horizon: { kind: "calendar_days", value: 7, end_at: "2025-09-06T23:59:59+08:00" } } };
  assert.equal(observationWindow(payload), "2025-08-30 至 2025-09-06（7 个自然日）");
  assert.match(observationMarkdown(payload), /2025-08-30 至 2025-09-06/);
  assert.doesNotMatch(observationMarkdown(payload), /未来 7 天/);
});

test("observation windows preserve units and do not invent missing dates", () => {
  for (const [kind, unit] of [["trading_days", "个交易日"], ["rounds", "轮"]]) {
    assert.equal(observationWindow({ source: { horizon: { kind, value: 20 } } }), `20 ${unit}（未记录起点）`);
  }
  assert.equal(observationWindow({ source: { horizon_days: "7" } }), undefined);
  assert.equal(observationWindow({}), undefined);
});
