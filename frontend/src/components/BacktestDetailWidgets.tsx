import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  FileText,
  Loader2,
  Minus,
  XCircle,
} from "lucide-react";
import type { BTRun, BTMetricsV2, BTPredAccStat, BTPredictionItem, BTSSEEvent, BTEventCatalogItem } from "../types";
import { cls, relTime } from "../utils";
import { DirBadge, EventStatusBadge, fmtPct } from "./BacktestDetailShared";

/* ===================================== 进度条卡片 ===================================== */

export function ProgressCard({ run, progress, unitLabel = "事件" }: { run: BTRun | null; progress: number; unitLabel?: string }) {
  if (!run) return null;
  const doneWithWarnings = run.status === "done" && (
    /warning/i.test(String(run.completion_quality ?? "")) ||
    Number(run.warning_count ?? 0) > 0 ||
    Number(run.invalid_output_count ?? 0) > 0 ||
    Number(run.voluntary_abstain_count ?? 0) > 0 ||
    Number(run.insufficient_data_count ?? 0) > 0
  );
  const barColor =
    run.status === "failed" ? "bg-rise"
    : doneWithWarnings ? "bg-amber"
    : run.status === "done" ? "bg-jade"
    : run.status === "running" ? "bg-brand"
    : run.status === "paused" ? "bg-amber"
    : "bg-faint";

  return (
    <div className="rounded-card border border-edge bg-card shadow-card p-5">
      <div className="flex items-center justify-between">
        <div className="flex items-baseline gap-3">
          <span className="font-mono text-[28px] font-bold tabular-nums text-ink">{progress}<span className="text-[16px] text-faint">%</span></span>
          <div className="text-[12.5px] text-mute">
            已完成 <span className="font-mono font-semibold text-ink">{run.done_events}</span>
            {" / "}
            <span className="font-mono">{run.total_events}</span> {unitLabel}
            {doneWithWarnings && (
              <span className="ml-2 inline-flex items-center gap-1 rounded bg-amber-50 px-1.5 py-0.5 text-[10.5px] font-medium text-amber-700">
                <AlertTriangle size={10.5} /> 完成但有警告
              </span>
            )}
          </div>
        </div>
        <div className="flex flex-col items-end text-[11.5px] text-mute">
          <div>创建于 {run.created_at?.slice(5, 16).replace("T", " ")}</div>
          {run.started_at && <div>启动于 {run.started_at.slice(5, 16).replace("T", " ")}</div>}
          {run.finished_at && <div>结束于 {run.finished_at.slice(5, 16).replace("T", " ")}</div>}
        </div>
      </div>
      <div className="mt-3 h-2 overflow-hidden rounded-full bg-edge/80">
        <div
          className={cls("h-full rounded-full transition-all duration-300", barColor)}
          style={{ width: `${progress}%` }}
        />
      </div>
    </div>
  );
}

/* ===================================== 指标网格 ===================================== */

export type PredictionOutcomeKind =
  | "correct"
  | "incorrect"
  | "valid_neutral"
  | "invalid_output"
  | "insufficient_data"
  | "voluntary_abstain"
  | "missing_oracle";

export interface PredictionFailureInfo {
  kind: string;
  label: string;
  detail: string;
}

function firstText(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  if (Array.isArray(value)) {
    const joined = value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())).join("；");
    return joined || null;
  }
  return null;
}

/**
 * 新记录使用 strategy_metadata.output_failure_kind；旧记录只留下通用 fallback rationale，
 * 因此保留一个非常窄的 legacy 识别分支，避免把模型主动弃权误报成技术故障。
 */
export function predictionFailureInfo(prediction?: BTPredictionItem | null): PredictionFailureInfo | null {
  if (!prediction) return null;
  const metadata = prediction.strategy_metadata ?? {};
  const kind = firstText(
    metadata.output_failure_kind ??
    metadata.failure_kind ??
    prediction.output_failure_kind ??
    prediction.failure_kind,
  );
  const errors = firstText(metadata.output_validation_errors ?? prediction.output_validation_errors);
  const rationale = String(prediction.rationale ?? "").trim();
  const legacyInvalid = prediction.abstain && (
    /^team_prompt\s+benchmark=.*\s+variant=/i.test(rationale) ||
    /(invalid\s*json|json\s*parse|empty\s*(?:response|content)|模型返回为空|输出校验失败|fallback\s*neutral)/i.test(rationale)
  );
  if (!kind && !errors && !legacyInvalid) return null;

  const normalized = String(kind ?? (legacyInvalid ? "legacy_invalid_output" : "validation_error")).toLowerCase();
  const labels: Record<string, { label: string; detail: string }> = {
    empty_content: { label: "模型返回空内容", detail: "模型接口返回成功，但响应正文为空。" },
    empty_response: { label: "模型返回空内容", detail: "模型接口返回成功，但响应正文为空。" },
    invalid_json: { label: "模型 JSON 无效", detail: "返回内容无法解析为要求的 JSON 对象。" },
    json_decode_error: { label: "模型 JSON 无效", detail: "返回内容无法解析为要求的 JSON 对象。" },
    json_parse_error: { label: "模型 JSON 无效", detail: "返回内容无法解析为要求的 JSON 对象。" },
    schema_validation_error: { label: "输出字段不完整", detail: "模型返回内容未通过 Decision 输出契约校验。" },
    validation_error: { label: "输出校验失败", detail: "模型返回内容未通过 Decision 输出契约校验。" },
    legacy_invalid_output: { label: "旧记录：输出无效", detail: "该旧记录仅保存了兜底结果，未保留可评分的模型输出。" },
  };
  const mapped = labels[normalized] ?? {
    label: "模型输出无效",
    detail: `输出失败类型：${normalized}`,
  };
  return {
    kind: normalized,
    label: mapped.label,
    detail: errors ? `${mapped.detail} ${errors}` : mapped.detail,
  };
}

export function predictionOutcome(prediction?: BTPredictionItem | null): PredictionOutcomeKind | null {
  if (!prediction) return null;
  if (predictionFailureInfo(prediction)) return "invalid_output";
  if (String(prediction.strategy_metadata?.prediction_status ?? prediction.prediction_status ?? "").toLowerCase() === "insufficient_data") return "insufficient_data";
  if (prediction.abstain) return "voluntary_abstain";
  if (!prediction.oracle_label_t3) return "missing_oracle";
  if (String(prediction.pred_direction).toLowerCase() === "neutral") return "valid_neutral";
  if (prediction.is_correct_t3 === true) return "correct";
  if (prediction.is_correct_t3 === false) return "incorrect";
  return String(prediction.pred_direction).toLowerCase() === String(prediction.oracle_label_t3).toLowerCase()
    ? "correct"
    : "incorrect";
}

function metricStat(m: BTMetricsV2, id: string): BTPredAccStat | null {
  const metric = m.metrics?.[id];
  const value = typeof metric?.value === "number" && Number.isFinite(metric.value) ? metric.value : null;
  if (value == null) return null;
  const meta = metric?.meta ?? {};
  const breakdown = metric?.breakdown ?? {};
  const wilson = breakdown.wilson && typeof breakdown.wilson === "object"
    ? breakdown.wilson as Record<string, unknown>
    : {};
  const n = Number(meta.n ?? meta.n_valid ?? 0);
  const k = Number(meta.k ?? (Number.isFinite(n) ? Math.round(value * n) : 0));
  const lo = Number(wilson.lo_95 ?? meta.wilson_lo_95);
  const hi = Number(wilson.hi_95 ?? meta.wilson_hi_95);
  return {
    acc: value,
    n: Number.isFinite(n) ? n : 0,
    k: Number.isFinite(k) ? k : 0,
    wilson_lo_95: Number.isFinite(lo) ? lo : null,
    wilson_hi_95: Number.isFinite(hi) ? hi : null,
  };
}

function statFromCatalog(items: BTEventCatalogItem[], mode: "directional" | "three_class"): BTPredAccStat | null {
  if (!items.length) return null;
  const eligible = items.flatMap((item) => {
    const prediction = item.prediction;
    const outcome = predictionOutcome(prediction);
    if (!prediction || outcome === "invalid_output" || outcome === "insufficient_data" || outcome === "voluntary_abstain" || outcome === "missing_oracle") return [];
    const pred = String(prediction.pred_direction).toLowerCase();
    const oracle = String(prediction.oracle_label_t3).toLowerCase();
    if (mode === "directional" && (!["up", "down"].includes(pred) || !["up", "down"].includes(oracle))) return [];
    if (!["up", "down", "neutral"].includes(pred) || !["up", "down", "neutral"].includes(oracle)) return [];
    return [pred === oracle];
  });
  if (!eligible.length) return { acc: 0, k: 0, n: 0, wilson_lo_95: null, wilson_hi_95: null };
  const k = eligible.filter(Boolean).length;
  return { acc: k / eligible.length, k, n: eligible.length, wilson_lo_95: null, wilson_hi_95: null };
}

export function MetricsGrid({
  m,
  horizon = "t3",
  catalogItems = [],
  runInvalidOutputCount,
  runInsufficientDataCount,
  directionMode,
}: {
  m: BTMetricsV2;
  horizon?: string;
  catalogItems?: BTEventCatalogItem[];
  runInvalidOutputCount?: number | null;
  runInsufficientDataCount?: number | null;
  directionMode?: string | null;
}) {
  const binary = (directionMode ?? m.direction_mode) === "binary";
  const horizonLabel = /^t\d+$/i.test(horizon) ? `T+${horizon.slice(1)}` : horizon.toUpperCase();
  const directional = metricStat(m, "acc_primary_directional_trade")
    ?? statFromCatalog(catalogItems, "directional")
    ?? m.acc_t3_non_neutral
    ?? null;
  const threeClass = metricStat(m, "acc_primary_three_class")
    ?? statFromCatalog(catalogItems, "three_class");
  const catalogPredictions = catalogItems.map((item) => item.prediction).filter((p): p is BTPredictionItem => Boolean(p));
  const catalogInvalid = catalogPredictions.filter((p) => predictionOutcome(p) === "invalid_output").length;
  const catalogVoluntary = catalogPredictions.filter((p) => predictionOutcome(p) === "voluntary_abstain").length;
  const catalogInsufficient = catalogPredictions.filter((p) => predictionOutcome(p) === "insufficient_data").length;
  const catalogMissingOracle = catalogPredictions.filter((p) => predictionOutcome(p) === "missing_oracle").length;
  const catalogValid = catalogPredictions.filter((p) => {
    const outcome = predictionOutcome(p);
    return outcome !== "invalid_output" && outcome !== "insufficient_data" && outcome !== "voluntary_abstain";
  });
  const reportedTotal = Number(m.n_outputs ?? m.total ?? m.n_total ?? 0);
  // 输出有效率的分母是已返回的全部预测；metrics.n_total 只是有
  // Oracle 的可评分交集，标签缺失时会更小，不能用来计算输出有效率。
  const total = catalogPredictions.length || reportedTotal;
  const invalid = catalogPredictions.length
    ? catalogInvalid
    : Number(m.invalid_output_count ?? runInvalidOutputCount ?? 0);
  const insufficient = catalogPredictions.length
    ? catalogInsufficient
    : Number(m.insufficient_data_count ?? runInsufficientDataCount ?? 0);
  const voluntary = catalogPredictions.length
    ? catalogVoluntary
    : Number(m.voluntary_abstain_count ?? Math.max(0, Number(m.abstain_count ?? 0) - invalid - insufficient));
  const missingOracle = catalogPredictions.length
    ? catalogMissingOracle
    : Number(m.missing_oracle_count ?? 0);
  const validOutputN = catalogPredictions.length
    ? catalogValid.length
    : Math.max(0, total - invalid - voluntary - insufficient);
  const neutralCount = catalogPredictions.length
    ? catalogValid.filter((p) => String(p.pred_direction).toLowerCase() === "neutral").length
    : Number(m.neutral_count ?? 0);
  const outputQuality = total > 0 ? validOutputN / total : null;
  const cards: MetricCardProps[] = binary ? [
    {
      title: `${horizonLabel} 预测方向准确率`,
      hint: "仅比较有效看涨 / 看跌预测与实际方向；数据不足、无效输出、弃权和缺少实际标签不计入准确率",
      acc: directional?.n ? directional.acc : null,
      k: directional?.k ?? 0,
      n: directional?.n ?? 0,
      wilsonLo: directional?.wilson_lo_95,
      tone: !directional?.n ? "neutral" : directional.acc >= 0.65 ? "good" : directional.acc >= 0.5 ? "warn" : "bad",
    },
    {
      title: "数据不足占比",
      hint: `数据不足 ${insufficient} 条 / 已返回 ${total} 条；表示缺少判断所需资料，不作为中性预测或预测错误`,
      acc: total > 0 ? insufficient / total : null,
      k: insufficient,
      n: total,
      percentOnly: true,
      tone: insufficient ? "warn" : "neutral",
    },
    {
      title: "模型输出无效率",
      hint: `无效输出 ${invalid} 条；接口异常、空内容、格式错误等技术问题单独统计，不归为数据不足`,
      acc: total > 0 ? invalid / total : null,
      k: invalid,
      n: total,
      percentOnly: true,
      tone: invalid ? "bad" : "good",
    },
    {
      title: "有效方向预测率",
      hint: `有效 ${validOutputN} · 数据不足 ${insufficient} · 输出无效 ${invalid} · 主动弃权 ${voluntary} · 实际标签缺失 ${missingOracle}`,
      acc: outputQuality,
      k: validOutputN,
      n: total,
      percentOnly: true,
      tone: invalid ? "bad" : insufficient || voluntary ? "warn" : "good",
    },
  ] : [
    {
      title: `${horizonLabel} 方向交易准确率`,
      hint: "仅统计有效 up/down 预测与 up/down Oracle；中性、弃权、无效输出和缺失 Oracle 均不进入分母",
      acc: directional?.n ? directional.acc : null,
      k: directional?.k ?? 0,
      n: directional?.n ?? 0,
      wilsonLo: directional?.wilson_lo_95,
      tone: !directional?.n ? "neutral" : directional.acc >= 0.65 ? "good" : directional.acc >= 0.5 ? "warn" : "bad",
    },
    {
      title: `${horizonLabel} 三分类准确率`,
      hint: "有效输出按 up / down / neutral 精确匹配；主动弃权、无效输出和缺失 Oracle 不进入分母",
      acc: threeClass?.n ? threeClass.acc : null,
      k: threeClass?.k ?? 0,
      n: threeClass?.n ?? 0,
      wilsonLo: threeClass?.wilson_lo_95,
      tone: !threeClass?.n ? "neutral" : threeClass.acc >= 0.65 ? "good" : threeClass.acc >= 0.5 ? "warn" : "bad",
    },
    {
      title: "有效输出中的中性占比",
      hint: "分母仅为通过结构校验且未主动弃权的模型输出；不再把输出故障计作中性判断",
      acc: validOutputN > 0 ? neutralCount / validOutputN : null,
      k: neutralCount,
      n: validOutputN,
      percentOnly: true,
      tone: "neutral",
    },
    {
      title: "模型输出有效率",
      hint: `有效 ${validOutputN} · 输出无效 ${invalid} · 数据不足 ${insufficient} · 主动弃权 ${voluntary} · Oracle 缺失 ${missingOracle}；技术故障与模型判断分开统计`,
      acc: outputQuality,
      k: validOutputN,
      n: total,
      percentOnly: true,
      tone: invalid === 0 && voluntary === 0 ? "good" : invalid === 0 ? "warn" : "bad",
    },
  ];

  return (
    <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
      {cards.map((c, i) => (
        <MetricCard key={i} {...c} />
      ))}
    </div>
  );
}

interface MetricCardProps {
  title: string;
  hint: string;
  acc: number | null;
  k: number;
  n: number;
  wilsonLo?: number | null;
  threshold?: number;
  tone: "good" | "warn" | "bad" | "neutral";
  percentOnly?: boolean;
}

function MetricCard({ title, hint, acc, k, n, wilsonLo, threshold, tone, percentOnly }: MetricCardProps) {
  const toneCls = {
    good: "border-jade/30 bg-jade-soft/30",
    warn: "border-brand/30 bg-brand-soft/30",
    bad:  "border-rise/30 bg-rise/5",
    neutral: "border-edge",
  }[tone];
  const pctColor = {
    good: "text-jade",
    warn: "text-brand",
    bad:  "text-rise",
    neutral: "text-ink",
  }[tone];

  return (
    <div className={cls("rounded-card border bg-card shadow-card p-4", toneCls)}>
      <div className="flex items-start justify-between gap-2">
        <div>
          <div className="text-[12px] font-medium text-ink">{title}</div>
          <p className="mt-0.5 max-w-[240px] text-[10.5px] leading-relaxed text-faint">{hint}</p>
        </div>
      </div>
      <div className="mt-2.5 flex items-baseline gap-2">
        <span className={cls("font-mono text-[26px] font-bold tabular-nums", pctColor)}>
          {acc != null && Number.isFinite(acc) ? fmtPct(acc) : "—"}
        </span>
        {!percentOnly && wilsonLo != null && !Number.isNaN(wilsonLo) && (
          <span className="font-mono text-[11px] text-faint tabular-nums">
            Wilson lo {fmtPct(wilsonLo)}
            {threshold != null && (
              <span className={cls("ml-1", (wilsonLo ?? 0) >= threshold ? "text-jade" : "text-rise")}>
                / ≥ {fmtPct(threshold)}
              </span>
            )}
          </span>
        )}
      </div>
      <div className="mt-1 font-mono text-[10.5px] text-mute tabular-nums">
        k={k} / n={n}
        {!percentOnly && threshold != null && wilsonLo != null && (
          (wilsonLo ?? 0) >= threshold
            ? <span className="ml-2 text-jade">✓ 达标</span>
            : <span className="ml-2 text-rise">✗ 未达标</span>
        )}
      </div>
    </div>
  );
}

/* ===================================== SSE 事件行 ===================================== */

export function SSEEventRow({ ev }: { ev: BTSSEEvent }) {
  const cfg: Record<string, { label: string; color: string }> = {
    hello:            { label: "订阅就绪",    color: "text-jade" },
    run_started:      { label: "回测启动",    color: "text-brand" },
    run_info:         { label: "状态同步",    color: "text-mute" },
    stage_progress:   { label: "阶段进度",    color: "text-brand" },
    progress:         { label: "进度更新",    color: "text-violet" },
    prediction:       { label: "预测完成",    color: "text-violet" },
    metrics_snapshot: { label: "指标快照",    color: "text-jade" },
    run_done:         { label: "回测完成",    color: "text-jade" },
    run_failed:       { label: "回测失败",    color: "text-rise" },
    run_cancelled:    { label: "回测取消",    color: "text-violet" },
  };
  const c = cfg[ev.type] ?? { label: ev.type, color: "text-mute" };
  const summary = summarizeSSE(ev);
  return (
    <li className="flex items-start gap-2 text-[11.5px]">
      <span className={cls("mt-0.5 shrink-0 font-mono text-[10px] font-medium uppercase tracking-wider", c.color)}>
        {c.label}
      </span>
      <span className="min-w-0 flex-1 truncate text-ink/85">{summary}</span>
    </li>
  );
}

function summarizeSSE(ev: BTSSEEvent): string {
  switch (ev.type) {
    case "hello":
      return `初始状态=${ev.status}，已完成 ${ev.done_events}/${ev.total_events}`;
    case "prediction":
      return [
        ev.symbol,
        ev.event_id ? `ev=${ev.event_id.slice(0, 8)}` : null,
        ev.prediction ? `dir=${ev.prediction.pred_direction} conf=${(ev.prediction.confidence ?? 0).toFixed(2)}` : null,
      ].filter(Boolean).join(" · ");
    case "stage_progress":
      return [
        ev.stage_label || ev.stage,
        ev.detail,
        typeof ev.event_elapsed_seconds === "number" ? `已运行 ${formatSSEElapsed(ev.event_elapsed_seconds)}` : null,
      ].filter(Boolean).join(" · ");
    case "progress":
      return typeof ev.done_count === "number" ? `已完成 ${ev.done_count} 条` : "完成数已更新";
    case "metrics_snapshot":
      return `done=${ev.done_count} · strict=${fmtPct(ev.acc_t3_strict)} (lo ${fmtPct(ev.acc_t3_strict_lo)}) · non_neutral=${fmtPct(ev.acc_t3_non_neutral)}${ev.from_catchup ? " · 补发" : ""}`;
    case "run_done":
    case "run_failed":
    case "run_cancelled":
      return ev.message ?? ev.error ?? ev.type;
    default:
      return ev.message ?? "";
  }
}

function formatSSEElapsed(raw: number): string {
  const seconds = Math.max(0, Math.floor(raw));
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return minutes > 0 ? `${minutes}分${rest}秒` : `${rest}秒`;
}

/* ===================================== 事件目录行（CatalogRow） ===================================== */

export function CatalogRow({
  item,
  rowIdx,
  isOpen,
  onToggle,
}: {
  item: BTEventCatalogItem;
  rowIdx: number;
  isOpen: boolean;
  onToggle: () => void;
}) {
  const p = item.prediction;
  const failure = predictionFailureInfo(p);
  const outcome = predictionOutcome(p);
  const hasCkpt = Boolean(
    p?.trajectory_available === true ||
    p?.trajectory_file_exists === true ||
    p?.strategy_metadata?.trajectory_available === true ||
    p?.strategy_metadata?.trajectory_file_exists === true ||
    // 新后端会先校验文件并把失效路径归一为 null，因此该回退不会再把幽灵路径标成日志。
    Boolean(p?.trajectory_ckpt),
  );
  const rowClickable = true;
  const rowTitle =
    item.status === "pending"
      ? "点击查看事件元信息；等待回测调度执行"
      : item.status === "processing"
      ? "点击查看事件元信息；正在执行 Team 多 Agent 决策，等待中..."
      : hasCkpt
      ? "点击查看完整 trajectory 执行日志（含决策链）"
      : "点击查看本 case 详细信息";

  return (
    <tr
      className={cls(
        "border-b border-edge/60 transition",
        rowClickable ? "cursor-pointer" : "",
        isOpen ? "bg-brand-soft/15" : "hover:bg-edge/20",
        item.status === "processing" ? "bg-amber-soft/5" : "",
      )}
      onClick={onToggle}
      title={rowTitle}
    >
      <td className="px-2 py-2 text-center align-top">
        {isOpen
          ? <ChevronUp size={14} className="mx-auto text-brand" />
          : <ChevronDown size={14} className={cls("mx-auto", hasCkpt ? "text-brand/70" : "text-faint")} />}
      </td>
      <td className="px-2 py-2 font-mono text-[11px] text-faint tabular-nums align-top">{rowIdx}</td>
      <td className="px-4 py-2 align-top">
        <EventStatusBadge status={item.status} />
      </td>
      <td className="px-4 py-2 align-top">
        <div className="font-mono text-[12.5px] font-medium text-ink">{item.symbol ?? "—"}</div>
        <div className="font-mono text-[10px] text-faint">ev {item.event_id.slice(0, 14)}</div>
        {item.title && (
          <div className="mt-0.5 max-w-[360px] truncate text-[10.5px] text-faint" title={item.title}>
            {item.title}
          </div>
        )}
      </td>
      <td className="px-4 py-2 align-top">
        {item.market
          ? <span className="rounded bg-edge/60 px-1.5 py-0.5 font-mono text-[10.5px] text-mute">{item.market}</span>
          : <span className="text-faint">—</span>
        }
      </td>
      <td className="px-4 py-2 text-[12px] text-mute align-top">{item.event_type_l2 ?? "—"}</td>
      <td className="px-4 py-2 align-top">
        {p ? (
          <div className="flex items-center gap-1.5 flex-wrap">
            {outcome === "invalid_output" ? (
              <span className="inline-flex items-center gap-1 rounded bg-rise/10 px-1.5 py-0.5 text-[10px] font-medium text-rise" title={failure?.detail}>
                <AlertTriangle size={10} /> 无有效输出
              </span>
            ) : outcome === "insufficient_data" ? (
              <span className="inline-flex items-center gap-1 rounded bg-amber-soft px-1.5 py-0.5 text-[10px] font-medium text-amber" title={p.rationale || "缺少判断所需资料；展开查看原因"}>
                <AlertTriangle size={10} /> 数据不足
              </span>
            ) : outcome === "voluntary_abstain" ? (
              <span className="inline-flex items-center gap-1 rounded bg-violet-soft px-1.5 py-0.5 text-[10px] font-medium text-violet">
                <Minus size={10} /> 主动弃权
              </span>
            ) : (
              <DirBadge d={p.pred_direction} />
            )}
            {hasCkpt && (
              <span className="inline-flex items-center gap-0.5 rounded bg-jade-soft/40 px-1 py-0.5 text-[9.5px] text-jade" title="已确认存在可读取的 trajectory">
                <FileText size={10} /> log
              </span>
            )}
            {failure && <span className="basis-full max-w-[220px] truncate text-[9px] text-rise" title={failure.detail}>{failure.label}</span>}
          </div>
        ) : item.status === "processing" ? (
          <span className="inline-flex items-center gap-1 text-[10.5px] text-amber">
            <Loader2 size={10.5} className="animate-spin" />
            决策中...
          </span>
        ) : (
          <span className="text-[10.5px] text-faint">TBD</span>
        )}
      </td>
      <td className="px-4 py-2 font-mono text-[12px] tabular-nums align-top">
        {p && outcome !== "invalid_output" && outcome !== "insufficient_data" && outcome !== "voluntary_abstain" && p.confidence != null
          ? (p.confidence * 100).toFixed(0) + "%"
          : <span className="text-faint" title={outcome === "invalid_output" || outcome === "insufficient_data" || outcome === "voluntary_abstain" ? "该记录不计入置信度统计" : undefined}>
              {outcome === "invalid_output" || outcome === "insufficient_data" || outcome === "voluntary_abstain" ? "不计分" : "—"}
            </span>}
      </td>
      <td className="px-4 py-2 align-top">
        {p ? <DirBadge d={p.oracle_label_t3} /> : <span className="text-faint">—</span>}
      </td>
      <td className="px-4 py-2 font-mono text-[12px] tabular-nums align-top">
        {p && p.oracle_car_t3 != null
          ? <span className={cls((p.oracle_car_t3 ?? 0) >= 0 ? "text-fall" : "text-rise")}>
              {(p.oracle_car_t3 >= 0 ? "+" : "") + (p.oracle_car_t3 * 100).toFixed(2)}%
            </span>
          : <span className="text-faint">—</span>
        }
      </td>
      <td className="px-4 py-2 align-top">
        {outcome === "invalid_output" ? (
          <span className="inline-flex max-w-[180px] flex-col text-[11px] text-rise" title={failure?.detail}>
            <span className="inline-flex items-center gap-1 font-medium"><AlertTriangle size={12}/> 模型输出无效</span>
            <span className="mt-0.5 truncate text-[9px] text-rise/75">{failure?.label ?? "未通过输出校验"}</span>
          </span>
        ) : outcome === "insufficient_data" ? (
          <span className="inline-flex flex-col text-[11px] text-amber" title={p?.rationale || "缺少判断所需资料"}>
            <span className="inline-flex items-center gap-1 font-medium"><AlertTriangle size={12}/> 数据不足</span>
            <span className="mt-0.5 text-[9px]">未形成方向判断 · 不计准确率</span>
          </span>
        ) : outcome === "voluntary_abstain" ? (
          <span className="inline-flex items-center gap-1 text-[11.5px] text-violet"><Minus size={12}/> 主动弃权</span>
        ) : outcome === "missing_oracle" ? (
          <span className="inline-flex items-center gap-1 text-[11.5px] text-amber"><AlertTriangle size={12}/> Oracle 缺失</span>
        ) : outcome === "valid_neutral" ? (
          <span
            className="inline-flex flex-col text-[11px] text-mute"
            title={String(p?.pred_direction).toLowerCase() === String(p?.oracle_label_t3).toLowerCase() ? "三分类命中；不进入方向交易准确率" : "三分类未命中；不进入方向交易准确率"}
          >
            <span className="inline-flex items-center gap-1 font-medium"><Minus size={12}/> 有效中性</span>
            <span className={cls("mt-0.5 text-[9px]", String(p?.oracle_label_t3).toLowerCase() === "neutral" ? "text-jade" : "text-faint")}>三分类{String(p?.oracle_label_t3).toLowerCase() === "neutral" ? "命中" : "未命中"}</span>
          </span>
        ) : outcome === "correct" ? (
          <span className="inline-flex items-center gap-0.5 text-[11.5px] text-jade"><CheckCircle2 size={12}/> 正确</span>
        ) : outcome === "incorrect" ? (
          <span className="inline-flex items-center gap-0.5 text-[11.5px] text-rise"><XCircle size={12}/> 错误</span>
        ) : (
          <span className="text-[11.5px] text-faint">—</span>
        )}
      </td>
      <td className="px-4 py-2 text-[11.5px] text-mute align-top">
        {p?.created_at ? (
          <>
            <div>{relTime(p.created_at)}</div>
            <div className="font-mono text-[10px] text-faint">{p.created_at.slice(5, 16).replace("T", " ")}</div>
          </>
        ) : item.event_time ? (
          <span className="text-faint">as-of {item.event_time.slice(0, 16).replace("T", " ")}</span>
        ) : (
          <span className="text-faint">—</span>
        )}
      </td>
    </tr>
  );
}
