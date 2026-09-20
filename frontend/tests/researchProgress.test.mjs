import { readFileSync } from "node:fs";
import { test } from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

const source = readFileSync(new URL("../src/lib/researchProgress.ts", import.meta.url), "utf8");
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { researchProgressParts, friendlySimulationError, simulationNeedsNewRun } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString("base64")}`);

const start = { type: "agent_step", phase: "hypotheses", verdict: "running", note: "正在提炼…" };
test("empty and failed extraction replaces the running label, including history replay", () => {
  for (const verdict of ["empty", "completed", "failed", "timeout"]) {
    const end = { ...start, verdict, note: "阶段已结束" };
    assert.deepEqual(researchProgressParts([start, end], false), [end]);
    assert.deepEqual(researchProgressParts([start, end], true), [end]);
  }
});

test("stream termination without a result does not pretend extraction succeeded", () => {
  assert.deepEqual(researchProgressParts([start], true), [start]);
  const parts = researchProgressParts([start], false);
  assert.equal(parts[0].verdict, "interrupted");
  assert.match(parts[0].note, /未收到/);
});

test("old 13-character thinking label stops when its response has finished", () => {
  const old = { type: "thinking", agent: "router", text: "正在提炼可证伪的研究假设…" };
  assert.deepEqual(researchProgressParts([old], true), [old]);
  assert.equal(researchProgressParts([old], false)[0].verdict, "ended");
  assert.doesNotMatch(researchProgressParts([old], false)[0].note, /正在/);
});

test("both simulation views explain nested budget failures without filesystem paths", () => {
  const message = "RuntimeError: structured decision collection failed: local smoke token budget exhausted at 211293 tokens";
  assert.match(friendlySimulationError(message), /用量上限/);
  assert.match(friendlySimulationError(message), /重新推演/);
  assert.doesNotMatch(friendlySimulationError(message), /FileNotFoundError|211293/);
  assert.equal(simulationNeedsNewRun(message), true);
  assert.equal(simulationNeedsNewRun("temporary network failure"), false);
});

test("missing tokenizer explains startup failure and requires a fresh run", () => {
  const error = "RuntimeError: tokenizer_unavailable: Required tokenizer asset is unavailable";
  assert.match(friendlySimulationError(error), /分词器文件未就绪/);
  assert.doesNotMatch(friendlySimulationError(error), /RuntimeError/);
  assert.equal(simulationNeedsNewRun(error), true);
});

test("waiting for reserved capacity is not described as spent quota", () => {
  const error = "structured decision collection failed: simulation_capacity_timeout";
  assert.match(friendlySimulationError(error), /等待已超时/);
  assert.doesNotMatch(friendlySimulationError(error), /用量上限|额度已用完/);
  assert.equal(simulationNeedsNewRun(error), true);
});

test("provider connection failures keep the real cause ahead of the decision-stage label", () => {
  for (const error of ["structured decision collection failed: Connection error.", "model_connection_failed: unable to reach model provider"]) {
    assert.match(friendlySimulationError(error), /无法连接模型服务/);
    assert.match(friendlySimulationError(error), /代理/);
    assert.doesNotMatch(friendlySimulationError(error), /用量上限|结构化决策未能生成/);
  }
  assert.match(friendlySimulationError("model_auth_failed: rejected"), /密钥/);
  assert.match(friendlySimulationError("model_unavailable"), /模型名称/);
  assert.match(friendlySimulationError("model_rate_limited"), /暂时限制请求/);
});

test("exited and legacy smoke failures never offer to resume a dead process", () => {
  for (const error of ["simulation_process_failed", "simulation_process_stopped", "RuntimeError: MiroFish smoke run ended as failed"]) {
    assert.match(friendlySimulationError(error), /进程已退出/);
    assert.equal(simulationNeedsNewRun(error), true);
  }
});
