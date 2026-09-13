import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  CheckCircle2,
  ChevronRight,
  Database,
  Loader2,
  LockKeyhole,
  Plus,
  RefreshCw,
  Search,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../api";
import { useStore } from "../store";
import type { ArenaItem, BTRun, BTPerformanceResponse } from "../types";
import { cls, relTime } from "../utils";
import { isModelComparisonResult } from "./arena/arenaComparison";
import ArenaWorkspaceComposer from "./arena/ArenaWorkspaceComposer";
import { isArenaWorkspace, workspaceConfig, workspaceIsRunning, workspaceProgress, workspaceStatusLabel } from "./arena/arenaWorkspace";
import { ArenaFieldArt, ArenaLineup, ArenaSeal, arenaMatchNumber } from "./arena/ArenaMotif";

type ProtocolGroup = {
  key: string;
  strict: boolean;
  engineMode: string;
  resultNature: string;
  datasetVersion: string;
  snapshot: string;
  protocolHash: string;
  datasetName: string;
  runs: BTRun[];
  notes: string[];
  exploratory: boolean;
  eventContentVerified: boolean;
};

type AlignmentMode = "intersection" | "union" | "manual";
type AlignmentFrequency = "native" | "day" | "week" | "month";
type ArenaTrack = "performance" | "forecast";
type CoveragePreview = {
  runId: string;
  name: string;
  start: number | null;
  end: number | null;
  observations: number;
  frequency: string;
};

const asRecord = (value: unknown): Record<string, unknown> =>
  value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};

const text = (value: unknown): string => typeof value === "string" ? value.trim() : value === null || value === undefined ? "" : String(value);
const short = (value: string) => value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-5)}` : value || "—";
const isoDay = (value: number | null) => value === null ? "—" : new Date(value).toISOString().slice(0, 10);

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const record = value as Record<string, unknown>;
    return `{${Object.keys(record).sort().map((key) => `${JSON.stringify(key)}:${stableJson(record[key])}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

function isArenaSafeRun(run: BTRun): boolean {
  return ["arena_safe", "arena-safe"].includes(text(run.visibility).toLowerCase());
}

/**
 * Arena-safe Run DTOs expose only this strategy-free contract. Private/legacy
 * Runs keep their historical signature as a compatibility fallback, but a
 * redacted Run must never fall back to fields the server intentionally hid.
 */
export function arenaComparisonForRun(run: BTRun): Record<string, unknown> {
  const config = asRecord(run.config);
  const publicComparison = asRecord(config.arena_comparison);
  if (Object.keys(publicComparison).length) return publicComparison;
  return isArenaSafeRun(run) ? {} : asRecord(run.comparison_signature);
}

function arenaEligibilityForRun(run: BTRun): Record<string, unknown> {
  return asRecord(asRecord(run.config).arena_eligibility);
}

export function eventContentBasis(run: BTRun): { key: string; snapshot: string } | null {
  const signature = arenaComparisonForRun(run);
  const version = text(signature.version);
  // Only server-generated content signatures permit different catalogue
  // versions to compete. Never infer equivalence from import lineage or IDs.
  if (!["pronoia-event-arena-comparison-v2", "pronoia-event-arena-comparison-v3-aligned-time"].includes(version)
    || text(signature.engine_mode) !== "event_proxy") return null;
  const dataset = asRecord(signature.dataset);
  const snapshot = text(dataset.snapshot_hash);
  const labels = text(dataset.labels_sha256);
  if (!/^[a-f0-9]{64}$/i.test(snapshot) || !/^[a-f0-9]{64}$/i.test(labels)) return null;
  return { key: stableJson({ snapshot_hash: snapshot, labels_sha256: labels }), snapshot };
}

/** Match the server's explicit-time-alignment comparison contract.
 *
 * A completed Run exposes its native signature, whose window/frequency differ
 * for strategies with unequal histories. Performance Arena deliberately moves
 * only those fields into its own alignment contract; all data, benchmark,
 * execution, cost and evaluator fields remain part of this grouping key.
 */
export function alignedComparisonKey(run: BTRun): string {
  const source = arenaComparisonForRun(run);
  if (!Object.keys(source).length) return "";
  const signature = JSON.parse(JSON.stringify(source)) as Record<string, unknown>;
  // This opaque native hash can include the source time window. Performance
  // Arena deliberately aligns time on the server, so it cannot split UI groups.
  delete signature.comparison_protocol_hash;
  delete signature.window;
  delete signature.frequency;
  delete signature.time_axis;
  const evaluation = asRecord(signature.evaluation);
  if (Object.keys(evaluation).length) {
    const alignedEvaluation = { ...evaluation };
    delete alignedEvaluation.annualization_periods;
    signature.evaluation = alignedEvaluation;
  }
  signature.version = eventContentBasis(run)
    ? "pronoia-event-arena-comparison-v3-aligned-time"
    : "pronoia-arena-comparison-v2-aligned-time";
  signature.time_axis = {
    comparison_basis: "arena_explicit_calendar_alignment",
    source_window_externalized: true,
    source_frequency_externalized: true,
  };
  return stableJson(signature);
}

function nativeComparisonKey(run: BTRun): string {
  const comparison = arenaComparisonForRun(run);
  return text(comparison.comparison_protocol_hash)
    || text(run.comparison_protocol_hash)
    || (Object.keys(comparison).length ? stableJson(comparison) : "");
}

function performancePreview(run: BTRun, performance: BTPerformanceResponse): CoveragePreview {
  const points = Array.isArray(performance.equity_curve) ? performance.equity_curve : [];
  const times = points.map((point) => {
    const raw = point as unknown as Record<string, unknown>;
    const stamp = text(raw.timestamp) || text(raw.date) || text(raw.event_time);
    const parsed = stamp ? Date.parse(stamp) : Number.NaN;
    return Number.isFinite(parsed) ? parsed : null;
  }).filter((value): value is number => value !== null).sort((a, b) => a - b);
  return {
    runId: run.id,
    name: run.name,
    start: times[0] ?? null,
    end: times[times.length - 1] ?? null,
    observations: points.length,
    frequency: text(asRecord(performance.dataset).frequency) || runMeta(run).frequency || "原始频率",
  };
}

function previewCoverage(item: CoveragePreview, start: number | null, end: number | null): number | null {
  if (item.start === null || item.end === null || start === null || end === null || end <= start) return null;
  const covered = Math.max(0, Math.min(item.end, end) - Math.max(item.start, start));
  return Math.max(0, Math.min(1, covered / (end - start)));
}

export function runMeta(run: BTRun) {
  const config = asRecord(run.config);
  const protocol = asRecord(config.protocol);
  const signature = arenaComparisonForRun(run);
  const arenaEligibility = arenaEligibilityForRun(run);
  const hasArenaEligibility = Object.keys(arenaEligibility).length > 0;
  const signatureDataset = asRecord(signature.dataset);
  const signatureWindow = asRecord(signature.window);
  const signatureTiming = asRecord(signature.execution_timing);
  const signatureCosts = asRecord(signature.costs);
  const signatureConstraints = asRecord(signature.execution_constraints);
  const datasetSnapshot = asRecord(config.dataset_snapshot);
  const execution = { ...asRecord(config.execution_spec), ...asRecord(run.execution_spec) };
  const appliedExecution = Object.keys(asRecord(execution.applied)).length ? asRecord(execution.applied) : execution;
  const snapshot = text(signatureDataset.snapshot_hash)
    || text((run as BTRun & { snapshot_hash?: string | null }).snapshot_hash)
    || text(datasetSnapshot.snapshot_hash)
    || text(config.snapshot_hash) || text(asRecord(config.data_basis).snapshot_hash);
  const version = text(signatureDataset.dataset_version) || text(run.dataset_version) || text(config.dataset_version) || snapshot;
  const protocolHash = text(signature.comparison_protocol_hash) || text(run.comparison_protocol_hash) || text(config.comparison_protocol_hash)
    || text(run.protocol_hash) || text(config.protocol_hash);
  const engineMode = text(run.engine_mode) || text(config.engine_mode) || "event_proxy";
  const resultNature = text(signature.result_nature) || text(run.result_nature) || text(config.result_nature) || (engineMode === "portfolio" ? "simulated_from_real_bars" : "proxy");
  const dateWindow = [text(signatureWindow.start_date), text(signatureWindow.end_date)].filter(Boolean).join(" → ")
    || text(config.date_window) || [text(appliedExecution.start_date), text(appliedExecution.end_date)].filter(Boolean).join(" → ")
    || [text(config.start_date), text(config.end_date)].filter(Boolean).join(" → ");
  const universe = text(config.universe) || text(config.asset_universe) || text(appliedExecution.universe)
    || (Array.isArray(datasetSnapshot.symbols) ? datasetSnapshot.symbols.map(text).filter(Boolean).join(", ") : "");
  const frequency = text(signature.frequency) || text(datasetSnapshot.frequency) || text(appliedExecution.frequency) || text(config.frequency) || text(config.bar_frequency);
  const benchmark = text(signature.benchmark) || text(appliedExecution.benchmark);
  const evaluationHorizon = text(asRecord(signature.evaluation).evaluation_horizon);
  const timing = [text(signatureTiming.signal_timing), text(signatureTiming.execution_delay), text(signatureTiming.price_field)].filter(Boolean).join(" / ");
  const costs = Object.entries(signatureCosts).map(([key, value]) => `${key}=${text(value)}`).join(" · ");
  const constraints = Object.entries(signatureConstraints).map(([key, value]) => `${key}=${text(value)}`).join(" · ");
  const hasLabelsSnapshot = hasArenaEligibility
    ? arenaEligibility.has_labels_snapshot === true
    : !isArenaSafeRun(run) && Boolean(text(run.labels_path) || text(config.labels_path) || text(protocol.labels_path));
  const hasEventSnapshot = hasArenaEligibility
    ? arenaEligibility.has_event_snapshot === true
    : !isArenaSafeRun(run) && Boolean(text(run.events_path) || text(config.events_path) || text(protocol.events_path));
  const hasResultArtifact = hasArenaEligibility
    ? arenaEligibility.has_result_artifact === true
    : !isArenaSafeRun(run) && Boolean(text(run.result_path) || text(config.result_path) || text(protocol.result_path));
  const strategy = Object.keys(asRecord(run.strategy_spec)).length ? asRecord(run.strategy_spec) : asRecord(config.strategy_spec);
  const metrics = asRecord(run.metrics);
  const forecastMetric = asRecord(metrics.return_forecast);
  const forecastMeta = asRecord(forecastMetric.meta);
  const forecast = Object.keys(forecastMeta).length ? forecastMeta : forecastMetric;
  const portfolioMeta = asRecord(asRecord(metrics.portfolio_metrics).meta);
  const inferredPredictionOnly = strategy.prediction_only === true || text(strategy.kind) === "return_forecast" || run.runner === "return_forecast"
    || config.prediction_only === true || asRecord(run).prediction_only === true || portfolioMeta.prediction_only === true;
  const predictionOnly = typeof arenaEligibility.prediction_only === "boolean"
    ? arenaEligibility.prediction_only
    : inferredPredictionOnly;
  const forecastCount = typeof forecast.n_forecasts === "number" && Number.isFinite(forecast.n_forecasts) ? forecast.n_forecasts : null;
  const inferredEvaluatedCount = typeof forecast.n === "number" && Number.isFinite(forecast.n) ? forecast.n
    : typeof forecast.evaluated_count === "number" && Number.isFinite(forecast.evaluated_count) ? forecast.evaluated_count : null;
  const evaluatedCount = typeof arenaEligibility.evaluated_forecast_count === "number" && Number.isFinite(arenaEligibility.evaluated_forecast_count)
    ? arenaEligibility.evaluated_forecast_count
    : inferredEvaluatedCount;
  const inferredHasNumericForecast = (forecastCount !== null && forecastCount > 0)
    || (typeof forecastMetric.value === "number" && Number.isFinite(forecastMetric.value) && evaluatedCount !== null && evaluatedCount > 0);
  const hasNumericForecast = typeof arenaEligibility.has_numeric_forecast === "boolean"
    ? arenaEligibility.has_numeric_forecast
    : inferredHasNumericForecast;
  const modelLab = asRecord(config.model_lab);
  const profile = Object.keys(asRecord(config.model_profile_snapshot)).length ? asRecord(config.model_profile_snapshot) : asRecord(modelLab.profile_snapshot);
  const source = (text(config.source) || text(config.origin)).toLowerCase().replace(/-/g, "_");
  const modelLabSource = source === "model_lab" || Boolean(Object.keys(modelLab).length) || Boolean(config.model_lab_batch_id);
  const inferredModelLabDemo = modelLabSource && [config.dry_run, config.demo, config.result_mode, modelLab.dry_run, modelLab.demo, modelLab.result_mode].some((value) => value === true || ["demo", "dry_run", "demo_only"].includes(text(value).toLowerCase()));
  const modelLabDemo = typeof arenaEligibility.model_lab_demo === "boolean"
    ? arenaEligibility.model_lab_demo
    : inferredModelLabDemo;
  const lineage = [
    text(profile.name) || text(profile.id) ? `模型 ${text(profile.name) || short(text(profile.id))}` : "",
    text(config.experiment_id) || text(modelLab.experiment_id) ? `实验 ${short(text(config.experiment_id) || text(modelLab.experiment_id))}` : "",
    text(config.model_lab_batch_id) || text(modelLab.batch_id) ? `批次 ${short(text(config.model_lab_batch_id) || text(modelLab.batch_id))}` : "",
  ].filter(Boolean).join(" · ");
  const externalPortfolio = typeof arenaEligibility.external_portfolio === "boolean"
    ? arenaEligibility.external_portfolio
    : engineMode === "portfolio" && (text(strategy.type) === "api" || text(strategy.adapter) === "external_http" || text(strategy.kind) === "external_http");
  const pointInTimeEnforced = typeof arenaEligibility.point_in_time_enforced === "boolean"
    ? arenaEligibility.point_in_time_enforced
    : strategy.point_in_time_enforced === true;
  const publicEligible = typeof arenaEligibility.eligible === "boolean" ? arenaEligibility.eligible : null;
  const formalEligible = typeof arenaEligibility.formal_eligible === "boolean" ? arenaEligibility.formal_eligible : null;
  const eventContentVerified = typeof arenaEligibility.event_content_verified === "boolean" ? arenaEligibility.event_content_verified : null;
  const performanceSampleSufficient = typeof arenaEligibility.performance_sample_sufficient === "boolean"
    ? arenaEligibility.performance_sample_sufficient
    : null;
  const forecastSampleSufficient = typeof arenaEligibility.forecast_sample_sufficient === "boolean"
    ? arenaEligibility.forecast_sample_sufficient
    : null;
  return { config, strategy, predictionOnly, hasNumericForecast, evaluatedCount, forecast, execution: appliedExecution, snapshot, version, protocolHash, engineMode, resultNature, dateWindow, universe, frequency, benchmark, evaluationHorizon, timing, costs, constraints, hasArenaEligibility, hasLabelsSnapshot, hasEventSnapshot, hasResultArtifact, externalPortfolio, pointInTimeEnforced, publicEligible, formalEligible, eventContentVerified, performanceSampleSufficient, forecastSampleSufficient, modelLabDemo, lineage };
}

export function runEligibility(run: BTRun, exploratory = false, track: ArenaTrack = "performance"): { eligible: boolean; reason: string | null } {
  if (run.status !== "done") return { eligible: false, reason: `运行状态为 ${run.status}，仅已完成 Run 可参赛` };
  const meta = runMeta(run);
  if (meta.modelLabDemo) return { eligible: false, reason: "Model Lab dry-run / 演示结果不会进入 Arena；请先完成正式预测评测" };
  if (meta.hasArenaEligibility && meta.publicEligible !== true) return { eligible: false, reason: "后端未将该 Run 标记为可审计的 Arena 候选" };
  if (track === "performance" && meta.predictionOnly) {
    return { eligible: false, reason: "纯收益率预测不生成交易表现，请使用预测能力赛道" };
  }
  if (track === "performance" && meta.hasArenaEligibility && meta.performanceSampleSufficient !== true) {
    return { eligible: false, reason: "实际参与收益计算的交易或活跃 K 线不足 5 个，暂不公开或排名该表现" };
  }
  if (track === "forecast" && meta.hasArenaEligibility && meta.forecastSampleSufficient !== true) {
    return { eligible: false, reason: "实际参与预测评分的有效样本不足 5 个，暂不公开或排名该结果" };
  }
  if (track === "forecast" && meta.engineMode === "portfolio" && !meta.hasNumericForecast) {
    return { eligible: false, reason: "该量化结果未提供有效的数值收益率预测；交易策略可使用投资表现赛道" };
  }
  if (track === "forecast" && meta.engineMode === "portfolio" && meta.evaluatedCount === 0) {
    return { eligible: false, reason: "收益率预测尚无已兑现的可评分样本，暂不能比较预测误差" };
  }
  if (meta.engineMode === "event_proxy" && (!meta.hasEventSnapshot || !meta.hasLabelsSnapshot)) {
    return { eligible: false, reason: "事件代理缺少可核验的事件或 Oracle 快照，无法核验收益与正确率" };
  }
  if (!exploratory && meta.hasArenaEligibility && meta.formalEligible !== true) return { eligible: false, reason: "后端已将该 Run 标记为仅可用于探索对照" };
  if (!exploratory && meta.hasArenaEligibility && meta.engineMode === "event_proxy" && meta.eventContentVerified !== true) {
    return { eligible: false, reason: "事件事实或 Oracle 的内容指纹未通过服务端核验，暂不能建立公平排名" };
  }
  if (!exploratory && text(arenaComparisonForRun(run).version).startsWith("pronoia-event-arena-comparison-") && !eventContentBasis(run)) {
    return { eligible: false, reason: "事件事实或 Oracle 的内容指纹不完整，暂不能建立公平排名" };
  }
  if (meta.engineMode === "portfolio" && !meta.hasResultArtifact) {
    return { eligible: false, reason: "量化运行缺少可核验的冻结结果，无法读取可审计的预测或交易结果" };
  }
  if (!(["event_proxy", "portfolio"].includes(meta.engineMode))) {
    return { eligible: false, reason: `不支持的 engine_mode=${meta.engineMode || "(empty)"}` };
  }
  if (!exploratory && meta.engineMode === "portfolio" && (
    meta.resultNature === "unverified" || (meta.externalPortfolio && !meta.pointInTimeEnforced)
  )) {
    return {
      eligible: false,
      reason: "外部 HTTP 模型一次接收完整历史 bars，未通过 point-in-time 验证；只能进入“探索对照”，不能参加公平排名",
    };
  }
  if (meta.resultNature === "unavailable") return { eligible: false, reason: "后端明确标记 result_nature=unavailable" };
  return { eligible: true, reason: null };
}

export function groupRuns(runs: BTRun[], exploratory = false, track: ArenaTrack = "performance"): ProtocolGroup[] {
  const map = new Map<string, ProtocolGroup>();
  for (const run of runs.filter((item) => runEligibility(item, exploratory, track).eligible)) {
    const m = runMeta(run);
    const content = m.engineMode === "event_proxy" ? eventContentBasis(run) : null;
    // 正式组使用服务端策略无关签名。探索组让所有已有可审计收益
    // 的事件/量化/API Run 可见；数据或引擎不兼容不会被静默过滤，
    // 而是在结果中保留差异和“非公平排名”标识。
    const comparisonKey = track === "performance" ? alignedComparisonKey(run)
      : content ? nativeComparisonKey(run) : m.protocolHash;
    const strict = !exploratory && Boolean((content || m.version) && comparisonKey && m.engineMode);
    const key = exploratory
      ? `explore:${track}:all-auditable-runs`
      : strict
      ? content
        ? `formal:event-content:${comparisonKey}:${m.engineMode}:${m.resultNature}`
        : `formal:${m.version}:${m.snapshot || "no-snapshot"}:${comparisonKey}:${m.engineMode}:${m.resultNature}`
      : `legacy:${run.dataset_id || m.version || run.id}:${m.engineMode}:${m.resultNature}`;
    const current = map.get(key);
    if (current) {
      current.runs.push(run);
      if (current.engineMode !== m.engineMode) current.engineMode = "mixed";
      if (current.resultNature !== m.resultNature) current.resultNature = "mixed";
      if (current.datasetVersion !== m.version) current.datasetVersion = "mixed";
      if (current.snapshot !== (content?.snapshot ?? m.snapshot)) current.snapshot = "mixed";
      current.eventContentVerified = current.eventContentVerified && Boolean(content);
      if (current.exploratory) {
        current.datasetName = "跨周期 · 可审计结果集合";
        current.notes = ["允许不同数据长度与频率", "结果不构成公平排名"];
      } else if (track === "performance" && current.protocolHash !== m.protocolHash) {
        // Native Run hashes include source time axes. Once more than one is
        // present, do not send a misleading native hash; the server derives
        // and freezes the aligned hash during Arena creation.
        current.protocolHash = "";
        current.notes = ["时间轴由 Arena 对齐", "数据、成交、成本与基准仍严格一致"];
      }
      continue;
    }
    const notes = [
      m.frequency && `频率 ${m.frequency}`,
      m.universe && `资产池 ${m.universe}`,
      m.dateWindow && `区间 ${m.dateWindow}`,
      m.benchmark && `基准 ${m.benchmark}`,
      content && "事件事实与 Oracle 按实际内容核验",
    ].filter(Boolean) as string[];
    map.set(key, {
      key, strict, engineMode: m.engineMode, resultNature: m.resultNature,
      datasetVersion: m.version, snapshot: content?.snapshot ?? m.snapshot, protocolHash: m.protocolHash,
      datasetName: run.dataset_name || run.dataset_id || "未命名数据基准", runs: [run], notes,
      exploratory, eventContentVerified: Boolean(content),
    });
  }
  return [...map.values()].sort((a, b) => Number(b.strict) - Number(a.strict) || b.runs.length - a.runs.length);
}

function mismatchReason(run: BTRun, allRuns: BTRun[], track: ArenaTrack): string {
  const current = runMeta(run);
  const currentContent = eventContentBasis(run);
  const peer = allRuns.find((candidate) => {
    if (candidate.id === run.id || !runEligibility(candidate, false, track).eligible) return false;
    const meta = runMeta(candidate);
    const sameData = currentContent
      ? eventContentBasis(candidate)?.key === currentContent.key
      : meta.version === current.version;
    return sameData && meta.engineMode === current.engineMode;
  });
  if (!peer) return currentContent ? "没有使用相同事件事实与 Oracle 的第二个已完成 Run" : "没有使用同一数据版本与引擎的第二个已完成 Run";
  const other = runMeta(peer);
  const differences: string[] = [];
  const add = (label: string, a: string, b: string) => { if (a !== b) differences.push(`${label} ${a || "未设"} ↔ ${b || "未设"}`); };
  if (track === "forecast") {
    add("日期", current.dateWindow, other.dateWindow);
    add("频率", current.frequency, other.frequency);
    add("预测周期", current.evaluationHorizon, other.evaluationHorizon);
  }
  add("基准", current.benchmark, other.benchmark);
  add("成交", current.timing, other.timing);
  add("成本", current.costs, other.costs);
  add("仓位约束", current.constraints, other.constraints);
  add("结果性质", current.resultNature, other.resultNature);
  return differences.length
    ? `与“${peer.name}”不可进入正式组：${differences.slice(0, 3).join("；")}`
    : `与“${peer.name}”的服务端比较签名不同（请刷新或在探索模式查看）`;
}

function ResultBadge({ group, track }: { group: ProtocolGroup; track: ArenaTrack }) {
  if (group.exploratory) return <span className="rounded-full bg-amber-500/10 px-2 py-0.5 text-[9px] font-semibold text-amber-700">探索对照 · 非公平排名</span>;
  if (track === "forecast") return <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[9px] font-semibold text-brand">{group.engineMode === "portfolio" ? "数值收益率预测" : "事件预测"}</span>;
  const formal = group.strict && group.engineMode === "portfolio" && group.resultNature === "simulated_from_real_bars";
  if (formal) return <span className="rounded-full bg-jade/10 px-2 py-0.5 text-[9px] font-semibold text-jade">正式组合回测</span>;
  if (group.engineMode === "event_proxy") return <span className="rounded-full bg-brand/10 px-2 py-0.5 text-[9px] font-semibold text-brand">事件收益代理</span>;
  return <span className="rounded-full bg-edge px-2 py-0.5 text-[9px] font-semibold text-mute">历史兼容结果</span>;
}

export function CreateArena({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (id: string) => void }) {
  const loadBTRuns = useStore((s) => s.loadBTRuns);
  const [runs, setRuns] = useState<BTRun[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [comparisonMode, setComparisonMode] = useState<"formal" | "exploration">("formal");
  const [arenaTrack, setArenaTrack] = useState<ArenaTrack>("performance");
  const [groupKey, setGroupKey] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [name, setName] = useState("");
  const [alignmentMode, setAlignmentMode] = useState<AlignmentMode>("intersection");
  const [alignmentFrequency, setAlignmentFrequency] = useState<AlignmentFrequency>("native");
  const [manualStart, setManualStart] = useState("");
  const [manualEnd, setManualEnd] = useState("");
  const [coveragePreview, setCoveragePreview] = useState<CoveragePreview[]>([]);
  const [previewLoading, setPreviewLoading] = useState(false);

  useEffect(() => {
    if (!open) return;
    let live = true;
    setLoading(true); setError(null); setSelected([]); setGroupKey("");
    setComparisonMode("formal");
    setArenaTrack("performance");
    setAlignmentMode("intersection"); setAlignmentFrequency("native"); setManualStart(""); setManualEnd(""); setCoveragePreview([]);
    const stamp = new Date().toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
    setName(`投资表现对比 · ${stamp}`);
    void loadBTRuns(true).then((items) => live && setRuns(items)).catch((e) => live && setError(e instanceof Error ? e.message : "无法读取回测记录")).finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [open, loadBTRuns]);

  const groups = useMemo(() => groupRuns(runs, comparisonMode === "exploration", arenaTrack), [runs, comparisonMode, arenaTrack]);
  const active = groups.find((g) => g.key === groupKey) ?? null;
  const eligible = groups.filter((g) => g.runs.length >= 2);
  const excluded = useMemo(() => runs.map((run) => ({ run, ...runEligibility(run, comparisonMode === "exploration", arenaTrack) })).filter((item) => !item.eligible), [runs, comparisonMode, arenaTrack]);
  const waitingGroups = groups.filter((group) => group.runs.length < 2);
  const chooseGroup = (group: ProtocolGroup) => { setGroupKey(group.key); setSelected(group.runs.map((r) => r.id)); setError(null); };
  const toggle = (id: string) => setSelected((ids) => ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]);

  useEffect(() => {
    if (!open || arenaTrack !== "performance" || !active || selected.length === 0) { setCoveragePreview([]); return; }
    let live = true;
    const selectedRuns = active.runs.filter((run) => selected.includes(run.id));
    setPreviewLoading(true);
    void Promise.allSettled(selectedRuns.map(async (run) => performancePreview(run, await api.btGetPerformance(run.id, false))))
      .then((items) => {
        if (!live) return;
        setCoveragePreview(items.flatMap((item) => item.status === "fulfilled" ? [item.value] : []));
      })
      .finally(() => live && setPreviewLoading(false));
    return () => { live = false; };
  }, [open, arenaTrack, active, selected.join("|")]);

  const previewWindow = useMemo(() => {
    const dated = coveragePreview.filter((item) => item.start !== null && item.end !== null);
    if (!dated.length) return null;
    const unionStart = Math.min(...dated.map((item) => item.start as number));
    const unionEnd = Math.max(...dated.map((item) => item.end as number));
    const intersectionStart = Math.max(...dated.map((item) => item.start as number));
    const intersectionEnd = Math.min(...dated.map((item) => item.end as number));
    const manualStartValue = manualStart ? Date.parse(`${manualStart}T00:00:00Z`) : null;
    const manualEndValue = manualEnd ? Date.parse(`${manualEnd}T23:59:59Z`) : null;
    const start = alignmentMode === "union" ? unionStart : alignmentMode === "manual" ? manualStartValue : intersectionStart;
    const end = alignmentMode === "union" ? unionEnd : alignmentMode === "manual" ? manualEndValue : intersectionEnd;
    return { dated, unionStart, unionEnd, intersectionStart, intersectionEnd, start, end, noCommon: intersectionStart > intersectionEnd };
  }, [coveragePreview, alignmentMode, manualStart, manualEnd]);

  const create = async () => {
    if (!active || selected.length < 2) { setError(comparisonMode === "formal" ? "请选择同一公平比较口径下至少两个已完成策略。" : "请选择至少两个拥有可审计结果的策略。"); return; }
    if (arenaTrack === "performance" && alignmentMode === "manual" && (!manualStart || !manualEnd)) { setError("手动时间对齐需要同时设置开始和结束日期。"); return; }
    if (arenaTrack === "performance" && alignmentMode === "manual" && manualStart > manualEnd) { setError("手动时间对齐的开始日期不能晚于结束日期。"); return; }
    setSaving(true); setError(null);
    try {
      const formal = !active.exploratory && active.strict && active.engineMode === "portfolio" && active.resultNature === "simulated_from_real_bars";
      const arena = await api.arenaCreate({
        name: name.trim() || (arenaTrack === "forecast" ? "预测能力对比" : "投资表现对比"),
        description: arenaTrack === "forecast"
          ? (active.exploratory ? "跨协议预测探索对照，不构成公平排名" : active.eventContentVerified ? "相同事件事实、Oracle、预测周期与评价口径下的预测能力对比" : "同一数据版本、预测周期与评价口径下的预测能力对比")
          : active.exploratory ? "跨周期探索对照；保留真实覆盖区间，不补造缺失数据，不构成公平排名" : formal ? "对齐真实时间窗口后的组合收益与风险对比" : "对齐事件窗口后的收益代理对比（不与正式组合回测混排）",
        dataset_id: active.runs[0]?.dataset_id ?? undefined,
        arena_type: arenaTrack,
        run_ids: selected,
        allow_mixed_protocols: active.exploratory,
        config: {
          arena_type: arenaTrack, comparison_mode: active.exploratory ? "exploration" : "strict", protocol_hash: active.protocolHash || undefined,
          dataset_version: active.eventContentVerified && active.datasetVersion === "mixed" ? undefined : active.datasetVersion || undefined,
          snapshot_hash: active.snapshot || undefined,
          comparison_protocol_hash: active.protocolHash || undefined,
          engine_mode: active.engineMode, result_nature: active.resultNature, protocol_source: active.strict ? "native" : "legacy",
          allow_mixed_protocols: active.exploratory,
          ...(arenaTrack === "performance" ? { time_alignment: {
            mode: alignmentMode,
            frequency: alignmentFrequency,
            start_date: alignmentMode === "manual" ? manualStart : undefined,
            end_date: alignmentMode === "manual" ? manualEnd : undefined,
            min_observations: 20,
          } } : {}),
        },
      });
      onCreated(arena.id);
    } catch (e) { setError(e instanceof Error ? e.message : "创建失败"); }
    finally { setSaving(false); }
  };
  if (!open) return null;
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-4 backdrop-blur-sm">
    <div className="flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl border border-edgeDark bg-paper shadow-pop">
      <div className="flex items-center justify-between border-b border-edgeDark/70 bg-card px-6 py-4">
        <div><h2 className="font-serif text-[18px] font-semibold text-ink">新建 Arena</h2><p className="mt-1 text-[11px] text-mute">选择预测能力或投资表现赛道，再加入已成功完成的实验。</p></div>
        <button onClick={onClose} className="rounded-lg p-2 text-mute hover:bg-edge"><X size={16} /></button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-6 py-5">
        {loading ? <div className="grid h-56 place-items-center text-[12px] text-mute"><Loader2 className="animate-spin" size={17} /></div> : <>
          <div className="mb-5">
            <div className="text-[11px] font-semibold text-ink">比较赛道</div>
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              <button onClick={() => { setArenaTrack("performance"); setGroupKey(""); setSelected([]); setName(`投资表现对比 · ${new Date().toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })}`); }} className={cls("rounded-xl border p-3 text-left", arenaTrack === "performance" ? "border-jade/40 bg-jade/5" : "border-edgeDark bg-card")}><span className="block text-[11px] font-semibold text-ink">投资表现</span><span className="mt-1 block text-[9.5px] text-mute">事件、量化与外部 API 的收益、年化、回撤和风险</span></button>
              <button onClick={() => { setArenaTrack("forecast"); setGroupKey(""); setSelected([]); setName(`预测能力对比 · ${new Date().toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })}`); }} className={cls("rounded-xl border p-3 text-left", arenaTrack === "forecast" ? "border-brand/40 bg-brand/5" : "border-edgeDark bg-card")}><span className="block text-[11px] font-semibold text-ink">预测能力</span><span className="mt-1 block text-[9.5px] text-mute">事件模型比较方向判断与收益率误差；量化预测比较数值收益率误差</span></button>
            </div>
          </div>
          <label className="mb-5 block"><span className="text-[11px] font-semibold text-ink">Arena 名称</span><input value={name} onChange={(e) => setName(e.target.value)} className="mt-2 w-full rounded-lg border border-edgeDark bg-card px-3 py-2 text-[12px] outline-none focus:border-brand" /></label>
          <div className="mb-3 flex flex-wrap items-end justify-between gap-3"><div><div className="text-[12px] font-semibold text-ink">选择比较模式</div><p className="mt-1 text-[10.5px] text-mute">{arenaTrack === "forecast" ? "事件预测锁定相同事件事实与 Oracle；量化预测锁定数据版本。预测周期与评价口径须一致，模型可以不同。" : "公平组锁定执行语义，并在选定的真实共同区间重算指标；策略历史长度可以不同。"}</p></div><div className="flex rounded-lg border border-edgeDark bg-paper p-0.5"><button onClick={() => { setComparisonMode("formal"); setGroupKey(""); setSelected([]); setError(null); }} className={cls("rounded-md px-3 py-1.5 text-[10px] font-semibold", comparisonMode === "formal" ? "bg-card text-jade shadow-card" : "text-mute")}>公平排名</button><button onClick={() => { setComparisonMode("exploration"); setGroupKey(""); setSelected([]); setError(null); }} className={cls("rounded-md px-3 py-1.5 text-[10px] font-semibold", comparisonMode === "exploration" ? "bg-card text-amber-700 shadow-card" : "text-mute")}>探索对照</button></div></div>
          {comparisonMode === "exploration" && <div className="mb-3 rounded-lg border border-amber-500/25 bg-amber-50 px-3 py-2 text-[10px] leading-relaxed text-amber-800">探索模式会列出当前赛道内所有可审计的已完成 Run。协议不同的结果可以并列观察，但不会标记为公平排名；投资表现曲线只裁剪或降采样真实观测。</div>}
          <div className="mb-2 flex items-center justify-between"><div className="text-[11px] font-semibold text-ink">{comparisonMode === "formal" ? "统一比较协议" : "可审计结果集合"}</div><span className="text-[10px] text-mute">{eligible.length} 组可比较</span></div>
          <div className="space-y-2">
            {eligible.map((g) => <button key={g.key} onClick={() => chooseGroup(g)} className={cls("flex w-full items-center gap-3 rounded-xl border p-3.5 text-left", g.key === groupKey ? "border-jade/40 bg-jade/5" : "border-edgeDark/70 bg-card hover:border-jade/25")}>
              <span className={cls("grid h-9 w-9 place-items-center rounded-lg", g.strict ? "bg-jade/10 text-jade" : "bg-amber-500/10 text-amber-700")}>{g.strict ? <ShieldCheck size={17} /> : <ShieldAlert size={17} />}</span>
              <div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="font-semibold text-[12px] text-ink">{g.datasetName}</span><ResultBadge group={g} track={arenaTrack} /></div><div className="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-[9.5px] text-mute"><span>{g.eventContentVerified && g.datasetVersion === "mixed" ? "多个来源版本 · 相同事件基准" : `版本 ${short(g.datasetVersion)}`}</span>{g.protocolHash && <span>协议 {short(g.protocolHash)}</span>}{g.notes.map((n) => <span key={n}>{n}</span>)}</div></div>
              <span className="text-[11px] font-semibold tabular-nums text-ink">{g.runs.length} <span className="text-[9px] font-normal text-mute">策略</span></span>{g.key === groupKey ? <CheckCircle2 className="text-jade" size={16} /> : <ChevronRight className="text-faint" size={15} />}
            </button>)}
            {eligible.length === 0 && <div className="rounded-xl border border-dashed border-edgeDark px-6 py-10 text-center text-[11px] text-mute">{comparisonMode === "formal" ? "尚没有可建立公平排名的策略组。可切换探索对照查看不同周期或协议的结果。" : "尚没有两个符合当前赛道要求且拥有可审计结果的已完成 Run。"}</div>}
          </div>
          {(excluded.length > 0 || (comparisonMode === "formal" && waitingGroups.length > 0)) && (
            <div className="mt-4 overflow-hidden rounded-xl border border-edgeDark/70 bg-card">
              <div className="flex items-center justify-between border-b border-edge px-3.5 py-2.5"><div className="flex items-center gap-1.5 text-[10.5px] font-semibold text-ink"><ShieldAlert size={12} className="text-amber-700" />未进入候选</div><span className="text-[9px] text-faint">逐 Run 校验结果文件</span></div>
              <div className="max-h-48 divide-y divide-edge/70 overflow-y-auto">
                {excluded.map(({ run, reason }) => <div key={run.id} className="grid gap-1 px-3.5 py-2.5 sm:grid-cols-[minmax(150px,.7fr)_minmax(0,1.3fr)]"><div className="truncate text-[10px] font-medium text-ink" title={run.name}>{run.name}</div><div className="text-[9.5px] leading-relaxed text-rise">{reason}</div></div>)}
                {comparisonMode === "formal" && waitingGroups.flatMap((group) => group.runs.map((run) => <div key={`waiting-${run.id}`} className="grid gap-1 px-3.5 py-2.5 sm:grid-cols-[minmax(150px,.7fr)_minmax(0,1.3fr)]"><div className="truncate text-[10px] font-medium text-ink" title={run.name}>{run.name}</div><div className="text-[9.5px] leading-relaxed text-amber-700">{mismatchReason(run, runs, arenaTrack)}</div></div>))}
              </div>
            </div>
          )}
          {arenaTrack === "performance" && <section className="mt-5 rounded-xl border border-edgeDark/70 bg-card p-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><div><div className="text-[12px] font-semibold text-ink">时间对齐</div><p className="mt-1 text-[10px] leading-relaxed text-mute">只裁剪真实观测并允许向低频聚合；不会拉伸曲线、插值或用月线补成日线。</p></div><label className="text-[10px] text-mute">展示频率<select value={alignmentFrequency} onChange={(e) => setAlignmentFrequency(e.target.value as AlignmentFrequency)} className="ml-2 rounded-md border border-edgeDark bg-paper px-2 py-1.5 text-[10px] font-medium text-ink"><option value="native">原始频率</option><option value="day">日</option><option value="week">周</option><option value="month">月</option></select></label></div>
            <div className="mt-3 grid gap-2 sm:grid-cols-3">{([
              ["intersection", "共同区间", "取所有策略真实覆盖的交集，适合观察同一段历史。"],
              ["union", "完整区间", "展示全部真实历史；没有数据的前后区间保持空白。"],
              ["manual", "手动区间", "指定开始与结束日期；超出覆盖的部分明确标记。"],
            ] as Array<[AlignmentMode, string, string]>).map(([value, label, description]) => <button key={value} onClick={() => setAlignmentMode(value)} className={cls("rounded-lg border p-3 text-left", alignmentMode === value ? "border-brand/35 bg-brand/5" : "border-edgeDark bg-paper")}><span className="block text-[10.5px] font-semibold text-ink">{label}{value === "intersection" && <i className="ml-1.5 rounded bg-jade/10 px-1.5 py-0.5 text-[8px] not-italic text-jade">推荐</i>}</span><span className="mt-1 block text-[9px] leading-relaxed text-mute">{description}</span></button>)}</div>
            {alignmentMode === "manual" && <div className="mt-3 grid gap-2 sm:grid-cols-2"><label className="text-[10px] text-mute">开始日期<input type="date" value={manualStart} onChange={(e) => setManualStart(e.target.value)} className="mt-1 block w-full rounded-md border border-edgeDark bg-paper px-2.5 py-2 text-[10.5px] text-ink" /></label><label className="text-[10px] text-mute">结束日期<input type="date" value={manualEnd} onChange={(e) => setManualEnd(e.target.value)} className="mt-1 block w-full rounded-md border border-edgeDark bg-paper px-2.5 py-2 text-[10.5px] text-ink" /></label></div>}
            {active && selected.length > 0 && <div className="mt-4 overflow-hidden rounded-lg border border-edge bg-paper/70">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-edge px-3 py-2"><div><div className="text-[10px] font-semibold text-ink">自动覆盖预检</div><div className="mt-0.5 text-[8.5px] text-faint">前端快速预估；创建后由后端按冻结结果重新计算。</div></div>{previewWindow && !previewWindow.noCommon && <div className="flex items-center gap-2 text-[9px] text-mute"><span>共同区间 {isoDay(previewWindow.intersectionStart)} → {isoDay(previewWindow.intersectionEnd)}</span>{alignmentMode === "manual" && <button onClick={() => { setManualStart(isoDay(previewWindow.intersectionStart)); setManualEnd(isoDay(previewWindow.intersectionEnd)); }} className="rounded border border-brand/25 px-2 py-1 font-semibold text-brand">填入共同区间</button>}</div>}</div>
              {previewLoading ? <div className="flex items-center gap-2 px-3 py-4 text-[9.5px] text-mute"><Loader2 size={11} className="animate-spin" />读取真实净值覆盖范围…</div> : !previewWindow ? <div className="px-3 py-4 text-[9.5px] text-amber-700">部分结果没有可读取的真实日期；系统会保留原始观察序号，不会猜测或拉伸时间轴。</div> : previewWindow.noCommon && alignmentMode === "intersection" ? <div className="px-3 py-4 text-[9.5px] leading-relaxed text-rise">所选策略没有共同历史区间。请选择“完整区间”查看各自真实覆盖，或手动指定区间；无数据部分会留白。</div> : <div className="divide-y divide-edge/70">{previewWindow.dated.map((item) => { const ratio = previewCoverage(item, previewWindow.start, previewWindow.end); const shortHistory = item.observations < 20; return <div key={item.runId} className="grid gap-2 px-3 py-2.5 sm:grid-cols-[minmax(150px,.9fr)_minmax(210px,1.2fr)_90px]"><div className="min-w-0"><div className="truncate text-[9.5px] font-semibold text-ink">{item.name}</div><div className="mt-0.5 text-[8.5px] text-faint">{item.frequency} · {item.observations} 点</div></div><div><div className="flex justify-between text-[8.5px] text-mute"><span>{isoDay(item.start)} → {isoDay(item.end)}</span><span>{ratio === null ? "—" : `${(ratio * 100).toFixed(1)}%`}</span></div><div className="mt-1 h-1.5 overflow-hidden rounded-full bg-edge"><div className={cls("h-full rounded-full", ratio === 1 ? "bg-jade" : "bg-amber-500")} style={{ width: `${Math.max(0, Math.min(100, (ratio ?? 0) * 100))}%` }} /></div>{shortHistory && <div className="mt-1 text-[8.5px] text-amber-700">样本偏短；指数/ETF 的实际存续期也可能限制历史长度</div>}</div><div className="text-right text-[8.5px] text-faint">{ratio === 1 ? "完整覆盖" : ratio === 0 ? "无覆盖" : "部分覆盖"}</div></div>; })}</div>}
              {coveragePreview.length < selected.length && !previewLoading && <div className="border-t border-edge px-3 py-2 text-[8.5px] text-amber-700">{selected.length - coveragePreview.length} 个 Run 无法预载曲线；后端计算时会明确标记。</div>}
            </div>}
          </section>}
          {active && <div className="mt-6"><div className="mb-2 flex items-center justify-between"><div className="text-[12px] font-semibold text-ink">参赛实验与模型</div><button onClick={() => setSelected(active.runs.map((r) => r.id))} className="text-[10px] text-jade">全选</button></div><div className="grid gap-2 sm:grid-cols-2">{active.runs.map((r) => { const meta = runMeta(r); return <button key={r.id} onClick={() => toggle(r.id)} className={cls("flex items-center gap-3 rounded-lg border p-3 text-left", selected.includes(r.id) ? "border-brand/35 bg-brand/5" : "border-edgeDark/70 bg-card")}><span className={cls("grid h-5 w-5 place-items-center rounded border", selected.includes(r.id) ? "border-brand bg-brand text-white" : "border-edgeDark text-transparent")}><Check size={12} /></span><span className="min-w-0"><span className="block truncate text-[11px] font-semibold text-ink">{r.name}</span><span className="mt-0.5 block truncate text-[9px] text-mute">{meta.lineage || `${r.model_version || r.runner || r.strategy_type || "策略"} · ${r.engine_mode || "—"}`}</span></span></button>; })}</div></div>}
          {error && <div className="mt-4 rounded-lg border border-rise/25 bg-rise/5 px-3 py-2 text-[10.5px] text-rise">{error}</div>}
        </>}
      </div>
      <div className="flex items-center justify-between border-t border-edgeDark/70 bg-card px-6 py-3"><div className="inline-flex items-center gap-1.5 text-[10px] text-mute"><LockKeyhole size={12} />{comparisonMode === "formal" ? "正式组由后端再次校验公平口径" : "探索组有醒目标识，不冒充公平排名"}</div><div className="flex gap-2"><button onClick={onClose} className="rounded-lg border border-edgeDark px-3 py-2 text-[11px] text-mute">取消</button><button onClick={() => void create()} disabled={saving || selected.length < 2} className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-4 py-2 text-[11px] font-semibold text-white disabled:opacity-40">{saving && <Loader2 size={12} className="animate-spin" />}创建 Arena</button></div></div>
    </div>
  </div>;
}

function ArenaCard({ arena, onOpen, onCompute, onDelete, busy }: { arena: ArenaItem; onOpen: () => void; onCompute: () => void; onDelete: () => void; busy: boolean }) {
  const config = asRecord(arena.config); const protocol = asRecord(config.comparison_protocol); const alignment = asRecord(config.time_alignment); const engine = text(config.engine_mode) || text(protocol.engine_mode) || "—"; const nature = text(config.result_nature) || "";
  const exploratory = Boolean(config.allow_mixed_protocols) || text(config.comparison_mode) === "exploration";
  const forecast = ["forecast", "prediction"].includes(text(arena.arena_type || config.arena_type).toLowerCase());
  // 老 Arena 的 config 未保存 result_nature；已锁定的 portfolio 引擎仍可准确归类为组合回测。
  const formal = !exploratory && engine === "portfolio" && (!nature || nature === "simulated_from_real_bars");
  if (isArenaWorkspace(arena)) {
    const workspace = workspaceConfig(arena);
    const progress = workspaceProgress(arena);
    const active = workspaceIsRunning(arena.status);
    const modelCount = workspace.model_ids?.length ?? progress.models?.length ?? 0;
    const modelNames = (workspace.models?.length ? workspace.models : progress.models ?? []).map((model) => model.name).filter((name): name is string => Boolean(name));
    return <article className="group flex min-h-[280px] min-w-0 flex-col overflow-hidden rounded-2xl border border-edgeDark/75 bg-card shadow-card transition duration-200 hover:border-brand/30 hover:shadow-pop">
      <div className="flex items-center justify-between gap-3 border-b border-edge bg-paper/70 px-5 py-3"><span className="font-mono text-[10px] tracking-[.13em] text-faint">场次 · {arenaMatchNumber(arena.id)}</span><span className={cls("inline-flex items-center gap-1.5 text-[10px]", active ? "text-brand" : arena.status === "failed" ? "text-rise" : arena.status === "done" ? "text-jade" : "text-mute")}>{active ? <Loader2 size={11} className="animate-spin" /> : <span className="h-1 w-1 rounded-full bg-current" />}{workspaceStatusLabel(arena.status)}</span></div>
      <div className="flex min-h-0 flex-1 flex-col p-5"><div className="flex items-start justify-between gap-3"><button type="button" onClick={onOpen} className="block min-w-0 text-left"><h3 className="break-words font-serif text-[17px] font-semibold leading-relaxed text-ink group-hover:text-brand">{arena.name}</h3></button><button type="button" onClick={onOpen} aria-label="查看模型比较" className="shrink-0 rounded-full border border-edge p-2 text-mute transition hover:border-brand/30 hover:text-brand"><ArrowRight size={14} /></button></div>
      <div className="my-4 border-y border-edge/80 py-3.5"><ArenaLineup names={modelNames} count={modelCount} compact /></div>
      <p className="break-words text-[12px] font-medium text-ink">{workspace.target?.name || arena.dataset_name || "预测与收益对比"}{workspace.target?.symbol ? <span className="ml-1.5 font-mono text-[10px] font-normal text-mute">{workspace.target.symbol}</span> : null}</p>
      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-[10px] text-faint"><span>{modelCount} 个模型</span>{workspace.start_date && workspace.end_date && <span>{workspace.start_date} — {workspace.end_date}</span>}</div>
      {progress.message && <p className="mt-2 line-clamp-2 break-words text-[11px] leading-relaxed text-mute">{progress.message}</p>}
      <div className="mt-auto flex items-center justify-between gap-2 pt-5"><span className="text-[10px] text-faint">{relTime(arena.updated_at)}</span><div className="flex items-center gap-2"><button type="button" onClick={onOpen} className="inline-flex items-center gap-1 rounded-lg px-2 py-1.5 text-[11px] font-semibold text-jade hover:bg-jade/5">{active ? "查看进度" : "查看结果"}<ArrowRight size={12} /></button>{!active && <button type="button" onClick={onDelete} aria-label="删除模型比较" className="rounded-lg p-1.5 text-faint hover:bg-rise/5 hover:text-rise"><Trash2 size={13} /></button>}</div></div></div>
    </article>;
  }
  if (isModelComparisonResult(arena.result)) {
    const comparison = arena.result;
    return <article className="flex min-h-[240px] min-w-0 flex-col rounded-2xl border border-edgeDark/75 bg-card p-5 shadow-card transition hover:border-brand/30 hover:shadow-pop">
      <p className="mb-3 border-b border-edge pb-3 font-mono text-[10px] tracking-[.13em] text-faint">场次 · {arenaMatchNumber(arena.id)}</p>
      <div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="text-[11px] font-semibold text-jade">{comparison.subject.kind === "event" ? "同一事件" : "同一指数 / 标的"} · 模型对比</p><button type="button" onClick={onOpen} className="mt-2 block max-w-full text-left"><h3 className="break-words font-serif text-[16px] font-semibold text-ink">{arena.name}</h3></button><p className="mt-2 break-words text-[12px] text-mute">{comparison.subject.title} · {comparison.market.symbol}</p></div><button type="button" onClick={onOpen} aria-label="查看模型对比" className="rounded-lg border border-edge p-2 text-mute hover:text-ink"><ArrowRight size={15} /></button></div>
      <div className="my-4 border-y border-edge py-3"><ArenaLineup names={comparison.models.map((model) => model.name)} count={comparison.models.length} compact /></div><div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-mute"><span>{comparison.models.length} 个模型</span><span>{comparison.market.frequency} · {comparison.market.bar_count.toLocaleString("zh-CN")} 条行情</span><span>统一做多 / 空仓</span></div>
      <div className="mt-auto flex items-center justify-between gap-2 pt-4"><span className="text-[11px] text-faint">{relTime(arena.updated_at)}</span><div className="flex items-center gap-2"><button type="button" onClick={onOpen} className="rounded-lg border border-jade/25 px-3 py-1.5 text-[11px] font-semibold text-jade">查看比较</button><button type="button" onClick={onDelete} aria-label="删除模型对比" className="rounded-lg border border-edge p-1.5 text-faint hover:text-rise"><Trash2 size={13} /></button></div></div>
    </article>;
  }
  return <article className="group flex min-h-[240px] flex-col rounded-2xl border border-edgeDark/70 bg-card p-5 shadow-card transition hover:border-brand/30 hover:shadow-pop">
    <p className="mb-3 border-b border-edge pb-3 font-mono text-[10px] tracking-[.13em] text-faint">场次 · {arenaMatchNumber(arena.id)}<span className="ml-3 font-sans tracking-normal">历史比较</span></p>
    <div className="flex items-start justify-between gap-3"><div className="min-w-0"><div className="mb-2 flex items-center gap-2"><span className={cls("rounded-full px-2 py-0.5 text-[9px] font-semibold", exploratory ? "bg-amber-500/10 text-amber-700" : formal ? "bg-jade/10 text-jade" : "bg-brand/10 text-brand")}>{exploratory ? "探索对照 · 非公平排名" : forecast ? "预测能力" : formal ? "组合回测" : "事件收益代理"}</span><span className="text-[9px] text-faint">{arena.status === "done" ? "已完成" : arena.status === "failed" ? "失败" : "待计算"}</span></div><button onClick={onOpen} className="block max-w-full text-left"><h3 className="truncate font-serif text-[15px] font-semibold text-ink group-hover:text-brand">{arena.name}</h3></button><p className="mt-1 line-clamp-2 text-[10.5px] leading-relaxed text-mute">{arena.description || (forecast ? "事件方向或数值收益率的预测能力对比" : "统一行情数据基准上的策略表现对比")}</p></div><button onClick={onOpen} className="rounded-lg border border-edgeDark p-2 text-mute hover:text-ink"><ArrowRight size={14} /></button></div>
    <div className="mt-4 grid grid-cols-3 divide-x divide-edge rounded-lg border border-edge bg-paper/70 py-2"><div className="px-3"><div className="text-[8.5px] text-faint">策略 / 模型</div><div className="mt-0.5 text-[13px] font-semibold text-ink">{arena.run_ids?.length || 0}</div></div><div className="px-3"><div className="text-[8.5px] text-faint">引擎 · 时间</div><div className="mt-0.5 truncate text-[10px] font-medium text-ink">{engine}{text(alignment.mode) ? ` · ${{ intersection: "交集", union: "并集", manual: "手动" }[text(alignment.mode)] || text(alignment.mode)}` : ""}</div></div><div className="px-3"><div className="text-[8.5px] text-faint">更新</div><div className="mt-0.5 text-[10px] text-ink">{relTime(arena.updated_at)}</div></div></div>
    <div className="mt-auto flex items-center justify-between pt-3"><div className="flex min-w-0 items-center gap-1.5 text-[9px] text-mute"><Database size={10} /><span className="truncate">{arena.dataset_name || arena.dataset_id || "数据基准待解析"}</span></div><div className="flex gap-1"><button onClick={onCompute} disabled={busy} className="rounded-md border border-jade/25 px-2 py-1 text-[9px] font-semibold text-jade disabled:opacity-50">{busy ? "计算中" : arena.result ? "重算" : "计算"}</button><button onClick={onDelete} className="rounded-md border border-edgeDark p-1 text-faint hover:text-rise"><Trash2 size={11} /></button></div></div>
  </article>;
}

export default function ArenaList() {
  const goBTList = useStore((s) => () => s.setView("backtest-list"));
  const openArenaDetail = useStore((s) => s.openArenaDetail); const arenaItems = useStore((s) => s.arenaItems);
  const arenaLoading = useStore((s) => s.arenaLoading); const loadArenas = useStore((s) => s.loadArenas); const patchArena = useStore((s) => s.patchArena);
  const [initialModelIds, setInitialModelIds] = useState<string[]>(() => {
    if (typeof window === "undefined") return [];
    return [...new Set(new URL(window.location.href).searchParams.getAll("models").flatMap((value) => value.split(",")).map((value) => value.trim()).filter(Boolean))].slice(0, 8);
  });
  const [createOpen, setCreateOpen] = useState(initialModelIds.length > 0); const [query, setQuery] = useState(""); const [busy, setBusy] = useState<string | null>(null); const [error, setError] = useState<string | null>(null);
  useEffect(() => { void loadArenas(true); }, [loadArenas]);
  const list = useMemo(() => arenaItems.filter((a) => [a.name, a.description, a.dataset_name, a.dataset_id].some((v) => text(v).toLowerCase().includes(query.toLowerCase()))).sort((a,b) => b.updated_at.localeCompare(a.updated_at)), [arenaItems, query]);
  const compute = async (id: string) => { setBusy(id); setError(null); patchArena(id, { status: "computing" }); try { await api.arenaCompute(id); await loadArenas(true); openArenaDetail(id); } catch (e) { patchArena(id, { status: "failed" }); setError(e instanceof Error ? e.message : "计算失败"); } finally { setBusy(null); } };
  const remove = async (id: string) => { if (!window.confirm("删除此 Arena？其回测 Run 不会被删除。")) return; try { await api.arenaDelete(id); useStore.setState((s) => ({ arenaItems: s.arenaItems.filter((a) => a.id !== id) })); } catch (e) { setError(e instanceof Error ? e.message : "删除失败"); } };
  const done = arenaItems.filter((a) => a.status === "done").length;
  const hasRunningWorkspace = arenaItems.some((item) => isArenaWorkspace(item) && workspaceIsRunning(item.status));
  useEffect(() => { if (!hasRunningWorkspace) return; const timer = setInterval(() => void loadArenas(true), 5000); return () => clearInterval(timer); }, [hasRunningWorkspace, loadArenas]);
  return <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper">
    <header className="flex shrink-0 items-center justify-between border-b border-edge bg-card/80 px-5 py-3 sm:px-8"><button onClick={goBTList} className="inline-flex items-center gap-1.5 text-[11px] text-mute hover:text-ink"><ArrowLeft size={13} />回测中心</button><span className="font-mono text-[9px] tracking-[.22em] text-faint">PRONOIA / ARENA</span></header>
    <main className="min-h-0 flex-1 overflow-y-auto"><div className="mx-auto max-w-[1380px] px-4 py-5 sm:px-7 sm:py-7">
      <section className="relative mb-8 overflow-hidden rounded-[22px] border border-edgeDark/70 bg-[linear-gradient(115deg,#fff_0%,#fcfaf6_54%,#f4ede2_100%)]">
        <ArenaFieldArt className="absolute -right-12 top-1 hidden h-[290px] w-[510px] text-brand/25 lg:block" />
        <div className="relative px-6 py-7 sm:px-9 sm:py-9 lg:max-w-[68%]"><div className="flex items-center gap-3"><ArenaSeal className="h-11 w-11 text-brand" /><p className="text-[10px] font-semibold tracking-[.27em] text-brand">MODEL RESEARCH ARENA</p></div><h1 className="mt-4 font-serif text-[32px] font-medium tracking-tight text-ink sm:text-[39px]">Arena<span className="mx-3 font-light text-edgeDark">/</span>模型竞技场</h1><p className="mt-3 max-w-[540px] text-[13px] leading-7 text-mute">让不同模型站在同一片场地。选择模型、事件或指数与时间段，比较预测的准确性，以及真实行情下的模拟收益。</p><button onClick={() => setCreateOpen(true)} className="mt-6 inline-flex items-center gap-3 rounded-lg bg-ink px-5 py-3 text-[12px] font-semibold text-card shadow-card transition hover:bg-brand"><Plus size={15} />新建 Arena<ArrowRight size={14} className="ml-3 opacity-60" /></button></div>
        <div className="relative grid grid-cols-3 divide-x divide-edge border-t border-edgeDark/60 bg-card/55 px-2 py-4 sm:px-5"><div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4"><span className="font-serif text-[25px] text-ink">{arenaItems.length.toString().padStart(2, "0")}</span><span className="text-[10px] text-mute">累计场次</span></div><div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 px-4"><span className="font-serif text-[25px] text-jade">{done.toString().padStart(2, "0")}</span><span className="text-[10px] text-mute">已完成</span></div><div className="flex items-center gap-2 px-4"><ShieldCheck size={17} className="hidden shrink-0 text-jade sm:block" /><div className="text-[10px] leading-relaxed text-mute">同一输入<span className="mt-0.5 block text-ink">预测能力 · 投资表现</span></div></div></div>
      </section>
      <div className="mb-5 flex flex-wrap items-end justify-between gap-4"><div><p className="mb-1 text-[9px] tracking-[.23em] text-faint">THE MATCH ARCHIVE</p><h2 className="font-serif text-[21px] text-ink">场次记录<span className="ml-3 font-sans text-[11px] text-faint">{list.length} 场</span></h2></div><div className="flex min-w-0 flex-wrap items-center gap-2"><div className="relative min-w-0"><Search size={14} className="absolute left-3 top-1/2 -translate-y-1/2 text-faint" /><input aria-label="搜索 Arena 或数据基准" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索 Arena 或数据基准" className="w-64 max-w-full rounded-lg border border-edgeDark/70 bg-card py-2 pl-9 pr-3 text-[11px] outline-none focus:border-brand" /></div><button onClick={() => void loadArenas(true)} aria-label="刷新 Arena 列表" className="rounded-lg border border-edgeDark/70 bg-card p-2.5 text-mute transition hover:border-brand/30 hover:text-brand"><RefreshCw size={13} className={cls(arenaLoading && "animate-spin")} /></button></div></div>
      {error && <div className="mb-4 rounded-lg border border-rise/25 bg-rise/5 px-3 py-2 text-[10.5px] text-rise">{error}</div>}
      {arenaLoading && !arenaItems.length ? <div className="grid h-56 place-items-center text-[11px] text-mute"><Loader2 size={16} className="animate-spin" /></div> : list.length ? <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{list.map((a) => <ArenaCard key={a.id} arena={a} busy={busy === a.id} onOpen={() => openArenaDetail(a.id)} onCompute={() => void compute(a.id)} onDelete={() => void remove(a.id)} />)}</div> : <div className="relative overflow-hidden rounded-2xl border border-dashed border-edgeDark bg-card px-6 py-12 text-center"><ArenaFieldArt className="absolute -bottom-20 left-1/2 w-[450px] -translate-x-1/2 text-brand/10" /><div className="relative"><ArenaSeal className="mx-auto h-16 w-16 text-brand/70" /><h2 className="mt-4 font-serif text-[20px] font-semibold text-ink">{query ? "没有找到匹配的场次" : "场地已备，等待模型入场"}</h2><p className="mx-auto mt-2 max-w-md text-[12px] leading-relaxed text-mute">{query ? "试试其他名称或数据基准。" : "选择两个或更多模型，再选事件或指数与时间段，开始第一场 Arena。"}</p><button onClick={() => setCreateOpen(true)} className="mt-5 rounded-lg bg-ink px-5 py-2.5 text-[12px] font-semibold text-white">新建 Arena</button></div></div>}
      <div className="mt-5 flex items-start gap-2 rounded-xl border border-brand/20 bg-brand/5 px-4 py-3 text-[10px] leading-relaxed text-mute"><LockKeyhole size={13} className="mt-0.5 shrink-0 text-brand" /><span><b className="text-ink">同一输入，分别衡量：</b>预测能力比较方向准确率与收益率误差；投资表现使用同一套行情与交易规则模拟收益、回撤。数据不足会说明样本和覆盖情况。</span></div>
    </div></main><ArenaWorkspaceComposer open={createOpen} initialModelIds={initialModelIds} onClose={() => { setCreateOpen(false); setInitialModelIds([]); }} onCreated={(arena) => { setCreateOpen(false); setInitialModelIds([]); void loadArenas(true); openArenaDetail(arena.id); }} /></div>;
}
