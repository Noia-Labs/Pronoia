import { useEffect, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  ChevronUp,
  CircleDashed,
  FileText,
  Loader2,
  Maximize2,
  Timer,
  X,
} from "lucide-react";
import type {
  BTEventCatalogItem,
  BTEventStatus,
  BTPerformanceKline,
  BTPredictionDetail,
  BTPredictionItem,
  BTTrajectoryCkpt,
} from "../types";
import { cls, clsx } from "../utils";
import { api } from "../api";
import KlineChart from "./KlineChart";
import { CloseOnlyChart } from "./BacktestPerformanceDashboard";
import { DirBadge, isFeverSyntheticSourceUrl, PreTextBlock } from "./BacktestDetailShared";
import { predictionFailureInfo, predictionOutcome } from "./BacktestDetailWidgets";

/* ===================================== 单 case 详情展开面板 ===================================== */

export type DetailTab = "log" | "rationale" | "packet" | "prompt" | "logic" | "market";

export function CaseDetailPanel({
  loading,
  detail,
  activeTab,
  onTabChange,
  itemStatus,
  catalogItem,
  runId,
  allowSensitive = true,
}: {
  loading: boolean;
  detail: BTPredictionDetail | null;
  activeTab: DetailTab;
  onTabChange: (t: DetailTab) => void;
  itemStatus: BTEventStatus;
  catalogItem: BTEventCatalogItem;
  runId: string;
  /** arena-safe Run 不应把完整 Prompt、数据包和 Agent 轨迹暴露给共享视图。 */
  allowSensitive?: boolean;
}) {
  const traj: BTTrajectoryCkpt | null = detail?.trajectory ?? null;
  const stats = traj?.llm_trajectory_stats ?? null;
  // 合并事件说明：优先级 detail.event_meta（后端补全，含 event_text）> catalogItem（列表页已有字段）
  const em: Record<string, unknown> = { ...catalogItem };
  const dem = detail?.event_meta;
  if (dem && typeof dem === "object") {
    for (const [k, v] of Object.entries(dem)) {
      if (v != null && v !== "" && v !== undefined) em[k] = v;
    }
  }
  const titleStr =
    (em.title as string | undefined) ??
    (em.event_title as string | undefined) ??
    (em.headline as string | undefined) ??
    null;
  const eventTextStr = (em.event_text as string | undefined) ?? null;
  const sourceUrlStr = (em.source_url as string | undefined) ?? null;
  const symbolStr = (em.symbol as string | undefined) ?? catalogItem.symbol ?? null;
  const marketStr = (em.market as string | undefined) ?? catalogItem.market ?? null;
  const typeStr = (em.event_type_l2 as string | undefined) ?? catalogItem.event_type_l2 ?? null;
  const occurredAtStr =
    (em.occurred_at as string | undefined) ?? catalogItem.occurred_at ?? null;
  const availableTimeStr =
    (em.available_time as string | undefined) ??
    catalogItem.available_time ??
    (em.event_time as string | undefined) ??
    catalogItem.event_time ??
    null;

  // ---------------- 完整事件说明 Modal（解决：占位 source_url 点出去 404 的问题） ----------------
  const [fullEventOpen, setFullEventOpen] = useState(false);
  const synthetic = sourceUrlStr ? isFeverSyntheticSourceUrl(sourceUrlStr) : false;
  const manualSource = Boolean(sourceUrlStr?.toLowerCase().startsWith("manual://"));
  useEffect(() => {
    if (!fullEventOpen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setFullEventOpen(false); };
    window.addEventListener("keydown", onKey);
    // 禁 body 滚动（避免页面超长时，打开 modal 背后还能滚）
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
    };
  }, [fullEventOpen]);

  const openFullEvent = (e?: React.MouseEvent) => {
    e?.preventDefault();
    e?.stopPropagation();
    setFullEventOpen(true);
  };
  // 尝试解析 as_of_packet JSON（字符串存储）
  let packetJson: Record<string, unknown> | null = null;
  if (traj?.as_of_packet) {
    try { packetJson = JSON.parse(traj.as_of_packet) as Record<string, unknown>; } catch { packetJson = null; }
  }
  const allTabs: Array<{ id: DetailTab; label: string; disabled?: boolean; sensitive?: boolean }> = [
    { id: "log",       label: "完整 Team Log", disabled: !traj?.team_final_state?.content_full, sensitive: true },
    { id: "rationale", label: "决策·结论与理由" },
    { id: "logic",     label: "逻辑链·Agent 协作", disabled: !traj?.team_final_state, sensitive: true },
    { id: "market",    label: "标的视图·行情 & 预测" },
    { id: "packet",    label: "As-of Packet", disabled: !traj?.as_of_packet, sensitive: true },
    { id: "prompt",    label: "给 Team 的 Prompt", disabled: !traj?.question_to_team, sensitive: true },
  ];
  const tabs = allTabs.filter((tab) => allowSensitive || !tab.sensitive);
  const visibleTab: DetailTab = tabs.some((tab) => tab.id === activeTab && !tab.disabled) ? activeTab : "rationale";
  const pred = detail?.prediction;
  const outputFailure = predictionFailureInfo(pred);
  const outputOutcome = predictionOutcome(pred);
  return (
    <div className="px-5 py-4">
      {/* ================================================= 事件信息 · Event Info 区块（始终展示） */}
      <div className="mb-4 rounded-lg border border-edge bg-paper p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h4 className="flex items-center gap-1.5 text-[13px] font-semibold text-ink">
            <FileText size={13} className="text-brand" /> 事件说明 · Event Info
          </h4>
          <div className="flex flex-wrap items-center gap-2 text-[11px]">
            {marketStr && (
              <span className="rounded bg-edge/60 px-1.5 py-0.5 font-mono text-[10.5px] text-mute">{marketStr}</span>
            )}
            {symbolStr && (
              <span className="rounded bg-brand-soft/40 px-1.5 py-0.5 font-mono text-[10.5px] text-brand">{symbolStr}</span>
            )}
            {typeStr && (
              <span className="rounded bg-violet-soft/40 px-1.5 py-0.5 text-[10.5px] text-violet">{typeStr}</span>
            )}
            {occurredAtStr && occurredAtStr !== availableTimeStr && (
              <span className="inline-flex items-center gap-1 text-mute">
                <Timer size={11} /> 发生 {occurredAtStr.slice(0, 19).replace("T", " ")}
              </span>
            )}
            {availableTimeStr && (
              <span className="inline-flex items-center gap-1 text-mute">
                <Timer size={11} /> 可得 {availableTimeStr.slice(0, 19).replace("T", " ")}
              </span>
            )}
          </div>
        </div>
        {titleStr ? (
          <div className="text-[13.5px] font-semibold leading-relaxed text-ink">{titleStr}</div>
        ) : (
          <div className="text-[12px] text-faint">（未命名事件）event_id {catalogItem.event_id}</div>
        )}
        {eventTextStr ? (
          <div className="whitespace-pre-wrap break-words rounded-md border border-edge/60 bg-card/60 p-3 text-[12.5px] leading-relaxed text-ink/90 max-h-[240px] overflow-y-auto">
            {eventTextStr}
          </div>
        ) : itemStatus !== "done" ? (
          <div className="text-[11.5px] text-faint">
            事件正文加载中…（team_full 执行完成后会从 as_of_packet 中提取完整公告正文，或见 As-of Packet Tab 的原始 JSON）
          </div>
        ) : (
          <div className="text-[11.5px] text-faint">
            此 runner 未保存 event_text；可切换到「As-of Packet」Tab（若启用）查看原始输入。
          </div>
        )}
        <div className="flex flex-wrap items-center gap-3 pt-1">
          {sourceUrlStr && manualSource ? (
            <span
              className="inline-flex items-center gap-1 rounded-md border border-amber-200 bg-amber-50 px-2.5 py-1 text-[11.5px] text-amber-800"
              title="该事件由用户手工录入，未提供可追溯的外部原文链接"
            >
              <FileText size={11} /> 手工录入（无外链）
            </span>
          ) : sourceUrlStr ? (
            <a
              href={sourceUrlStr}
              target={synthetic ? undefined : "_blank"}
              rel="noopener noreferrer"
              onClick={synthetic ? openFullEvent : undefined}
              className={cls(
                "inline-flex items-center gap-1 rounded-md border px-2.5 py-1 text-[11.5px] hover:bg-edge/40",
                synthetic ? "border-violet/30 bg-violet-soft/20 text-violet" : "border-edge bg-card text-brand"
              )}
              title={synthetic ? `（Pronoia 合成数据，点击查看完整原文）${sourceUrlStr}` : sourceUrlStr}
            >
              <ChevronRight size={11} />
              原文链接
              <span className="font-mono truncate max-w-[220px] text-mute">
                {sourceUrlStr.replace(/^https?:\/\//, "")}
              </span>
              {synthetic && (
                <span className="ml-0.5 rounded bg-violet/15 px-1 text-[9.5px] text-violet">Pronoia 合成</span>
              )}
            </a>
          ) : null}
          <button
            type="button"
            onClick={openFullEvent}
            className="inline-flex items-center gap-1 rounded-md border border-edge bg-card px-2.5 py-1 text-[11.5px] text-ink/80 hover:bg-edge/40 disabled:opacity-50 disabled:cursor-not-allowed"
            disabled={!eventTextStr && !titleStr}
            title="展开查看完整事件说明（不限高）"
          >
            <Maximize2 size={11} /> 查看完整事件
          </button>
          <span className="font-mono text-[10.5px] text-faint">
            eid {catalogItem.event_id}
          </span>
        </div>
      </div>

      {/* -------- 完整事件 Modal：点击「原文链接（合成）」或「查看完整事件」打开 -------- */}
      {fullEventOpen && (
        <div
          className="fixed inset-0 z-[200] flex items-start justify-center overflow-y-auto bg-ink/55 backdrop-blur-sm px-4 py-10"
          onClick={() => setFullEventOpen(false)}
        >
          <div
            className="relative w-full max-w-3xl rounded-xl border border-edge bg-paper shadow-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex flex-wrap items-start justify-between gap-3 border-b border-edge/70 px-5 py-3.5">
              <div className="min-w-0 flex-1">
                <h3 className="flex items-center gap-1.5 text-[13.5px] font-semibold text-ink">
                  <FileText size={13} className="text-brand" /> 完整事件说明
                </h3>
                <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[11px]">
                  {marketStr && (
                    <span className="rounded bg-edge/60 px-1.5 py-0.5 font-mono text-[10.5px] text-mute">{marketStr}</span>
                  )}
                  {symbolStr && (
                    <span className="rounded bg-brand-soft/40 px-1.5 py-0.5 font-mono text-[10.5px] text-brand">{symbolStr}</span>
                  )}
                  {typeStr && (
                    <span className="rounded bg-violet-soft/40 px-1.5 py-0.5 text-[10.5px] text-violet">{typeStr}</span>
                  )}
                  {occurredAtStr && occurredAtStr !== availableTimeStr && (
                    <span className="inline-flex items-center gap-1 text-mute">
                      <Timer size={10.5} /> 发生 {occurredAtStr.slice(0, 19).replace("T", " ")}
                    </span>
                  )}
                  {availableTimeStr && (
                    <span className="inline-flex items-center gap-1 text-mute">
                      <Timer size={10.5} /> 可得 {availableTimeStr.slice(0, 19).replace("T", " ")}
                    </span>
                  )}
                  <span className="ml-1 font-mono text-[10px] text-faint">eid {catalogItem.event_id}</span>
                </div>
              </div>
              <button
                type="button"
                onClick={() => setFullEventOpen(false)}
                className="rounded-md p-1.5 text-mute hover:bg-edge/50 hover:text-ink"
                title="关闭 (Esc)"
              >
                <X size={15} />
              </button>
            </div>

            <div className="space-y-3 px-5 py-4">
              {titleStr ? (
                <div className="text-[15px] font-semibold leading-relaxed text-ink">{titleStr}</div>
              ) : (
                <div className="text-[12px] text-faint">（未命名事件）event_id {catalogItem.event_id}</div>
              )}

              {eventTextStr ? (
                <div className="whitespace-pre-wrap break-words rounded-lg border border-edge/70 bg-card/70 p-4 text-[13px] leading-7 text-ink/95">
                  {eventTextStr}
                </div>
              ) : (
                <div className="rounded-lg border border-edge/60 bg-edge/20 p-4 text-[12px] text-mute">
                  暂无事件正文。等该事件执行完毕（status = done）后，as_of_packet 中会保存完整公告正文，届时再打开查看即可。
                </div>
              )}

              {sourceUrlStr && (
                <div className="flex flex-wrap items-center gap-2 rounded-md border border-edge/60 bg-edge/20 px-3 py-2 text-[11.5px]">
                  <span className="text-mute">来源 URL：</span>
                  {manualSource ? (
                    <>
                      <span className="rounded bg-amber-100 px-1 text-[10px] text-amber-800">手工录入</span>
                      <span className="text-mute">未提供可追溯的外部原文链接</span>
                    </>
                  ) : synthetic ? (
                    <>
                      <span className="rounded bg-violet/15 px-1 text-[10px] text-violet">Pronoia 合成事件</span>
                      <span className="font-mono break-all text-faint">{sourceUrlStr}</span>
                      <span className="text-mute">（原文即上方事件正文，此 URL 仅作追溯引用）</span>
                    </>
                  ) : (
                    <a
                      href={sourceUrlStr}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-mono break-all text-brand hover:underline"
                    >
                      {sourceUrlStr}
                    </a>
                  )}
                </div>
              )}
            </div>

            <div className="flex items-center justify-between gap-2 border-t border-edge/70 px-5 py-3 text-[11.5px]">
              <div className="text-mute">提示：按 <kbd className="rounded bg-edge/70 px-1 font-mono text-[10px] text-ink/80">Esc</kbd> 或点击遮罩可关闭</div>
              <button
                type="button"
                onClick={() => setFullEventOpen(false)}
                className="rounded-md border border-edge bg-card px-3 py-1.5 text-[11.5px] text-ink hover:bg-edge/40"
              >
                关闭
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 顶部执行摘要 / 状态说明 */}
      {itemStatus !== "done" && (
        <div className={cls(
          "mb-3 flex flex-wrap items-center gap-3 rounded-md border px-3 py-2 text-[11.5px]",
          itemStatus === "processing"
            ? "border-amber/30 bg-amber/5 text-amber"
            : "border-edge bg-edge/30 text-mute",
        )}>
          {itemStatus === "processing" ? (
            <>
              <Loader2 size={12} className="animate-spin shrink-0" />
              <span>Team 多 Agent 正在对该事件进行决策（通常 30~90s），可切换其他 Tab 查看已有的事件元信息；完成后这里会自动刷新为执行结果。</span>
            </>
          ) : (
            <>
              <CircleDashed size={12} className="shrink-0" />
              <span>该事件尚未被调度执行。等待执行队列空闲后会自动开始，完成后会自动进入「已完成」状态。</span>
            </>
          )}
        </div>
      )}
      <div className="mb-3 flex flex-wrap items-center gap-3 text-[11.5px]">
        <span className="inline-flex items-center gap-1 text-mute">
          <Timer size={12} className="text-brand" />
          耗时：{traj?.wall_seconds != null ? `${traj.wall_seconds.toFixed(1)}s` : itemStatus === "processing" ? "进行中..." : "—"}
        </span>
        {stats?.n_tokens_total != null && (
          <span className="rounded bg-edge/60 px-1.5 py-0.5 font-mono">tokens {stats.n_tokens_total}</span>
        )}
        {stats?.n_tool_calls != null && (
          <span className="rounded bg-edge/60 px-1.5 py-0.5 font-mono">tool_calls {stats.n_tool_calls}</span>
        )}
        {stats?.n_sse_events_stored != null && (
          <span className="rounded bg-edge/60 px-1.5 py-0.5 font-mono">steps {stats.n_sse_events_stored}</span>
        )}
        {traj?.model_version && (
          <span className="rounded bg-violet-soft/40 px-1.5 py-0.5 font-mono text-[10.5px] text-violet">
            model {traj.model_version}
          </span>
        )}
        {traj?.system_prompt_variant && (
          <span className="rounded bg-brand-soft/40 px-1.5 py-0.5 font-mono text-[10.5px] text-brand">
            variant {traj.system_prompt_variant}
          </span>
        )}
        {loading && (
          <span className="inline-flex items-center gap-1 text-brand">
            <Loader2 size={12} className="animate-spin" /> 加载 trajectory...
          </span>
        )}
        {!loading && !traj && itemStatus === "done" && (
          <span className="text-faint">本事件没有保存可读取的 trajectory；仍可查看已保存的决策与校验结果。</span>
        )}
      </div>

      {/* Tab 切换 */}
      <div className="mb-2 flex flex-wrap items-center gap-1 border-b border-edge">
        {tabs.map((t) => (
          <button
            key={t.id}
            onClick={() => !t.disabled && onTabChange(t.id)}
            disabled={t.disabled}
            className={cls(
              "-mb-px rounded-t px-3 py-1.5 text-[11.5px] transition",
              visibleTab === t.id
                ? "border border-b-0 border-edge bg-card text-ink"
                : t.disabled
                ? "text-faint cursor-not-allowed opacity-60"
                : "text-mute hover:bg-edge/40 hover:text-ink",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {/* Tab 内容 */}
      <div className="max-h-[62vh] overflow-auto rounded-lg border border-edge bg-paper p-4">
        {!allowSensitive && (
          <div className="mb-3 rounded-md border border-jade/25 bg-jade-soft/35 px-3 py-2 text-[11.5px] leading-relaxed text-jade">
            此 Run 使用 Arena-safe 可见性。完整 Prompt、As-of 数据包与 Agent 执行轨迹已隐藏，只展示可公开的决策摘要和市场结果。
          </div>
        )}
        {visibleTab === "log" && (
          <PreTextBlock
            empty={!traj?.team_final_state?.content_full}
            emptyHint={detail?.prediction.rationale ?? "（runner 未生成 Team Log，查看「决策·结论与理由」。）"}
            text={traj?.team_final_state?.content_full ?? ""}
          />
        )}
        {visibleTab === "rationale" && (
          <div className="space-y-3 text-[12.5px] leading-relaxed text-ink">
            <div>
              <div className="mb-1 text-[10.5px] uppercase tracking-wider text-faint">Final Prediction</div>
              <div className="flex flex-wrap items-center gap-2">
                {outputOutcome === "invalid_output" ? (
                  <span className="rounded bg-rise/10 px-1.5 py-0.5 text-[10.5px] font-medium text-rise">无有效预测</span>
                ) : outputOutcome === "insufficient_data" ? (
                  <span className="rounded bg-amber-soft px-1.5 py-0.5 text-[10.5px] font-medium text-amber">数据不足</span>
                ) : outputOutcome === "voluntary_abstain" ? (
                  <span className="rounded bg-violet-soft px-1.5 py-0.5 text-[10.5px] font-medium text-violet">主动弃权</span>
                ) : (
                  <DirBadge d={traj?.structured_extract?.direction ?? pred?.pred_direction} />
                )}
                <span className="font-mono text-mute">
                  {outputOutcome === "invalid_output" || outputOutcome === "insufficient_data" || outputOutcome === "voluntary_abstain"
                    ? "confidence 不计分"
                    : `confidence ${((traj?.structured_extract?.confidence ?? pred?.confidence ?? 0) * 100).toFixed(0)}%`}
                </span>
                {traj?.structured_extract?.conf_gate_applied ? (
                  <span className="rounded bg-brand-soft/50 px-1.5 py-0.5 text-[10.5px] text-brand">conf 闸 已生效</span>
                ) : null}
                {outputOutcome === "invalid_output" ? (
                  <span className="rounded bg-rise/10 px-1.5 py-0.5 text-[10.5px] text-rise">模型输出无效 · 未计分</span>
                ) : outputOutcome === "insufficient_data" ? (
                  <span className="rounded bg-amber-soft px-1.5 py-0.5 text-[10.5px] text-amber">未形成方向判断 · 不计准确率</span>
                ) : outputOutcome === "voluntary_abstain" ? (
                  <span className="rounded bg-violet-soft px-1.5 py-0.5 text-[10.5px] text-violet">模型主动弃权 · 未计分</span>
                ) : null}
              </div>
            </div>
            {outputFailure && (
              <div className="rounded-md border border-rise/20 bg-rise/5 px-3 py-2.5 text-[11.5px] text-rise">
                <div className="font-semibold">{outputFailure.label}</div>
                <div className="mt-1 leading-relaxed text-rise/80">{outputFailure.detail}</div>
              </div>
            )}
            {outputOutcome === "insufficient_data" && (
              <div className="rounded-md border border-amber/25 bg-amber-soft/30 px-3 py-2.5 text-[11.5px] text-amber">
                <div className="font-semibold">数据不足：缺少判断所需资料</div>
                <div className="mt-1 whitespace-pre-wrap leading-relaxed">{pred?.rationale || "模型未说明具体缺少的资料，请查看执行日志。"}</div>
              </div>
            )}
            <div>
              <div className="mb-1 text-[10.5px] uppercase tracking-wider text-faint">Rationale（DB prediction）</div>
              <div className="whitespace-pre-wrap break-words rounded-md bg-edge/30 p-3 text-[12.5px] text-ink">
                {pred?.rationale ?? (itemStatus === "processing" ? "等待执行完成..." : "（无）")}
              </div>
            </div>
            {traj?.structured_extract?.rationale &&
              traj.structured_extract.rationale !== pred?.rationale ? (
              <div>
                <div className="mb-1 text-[10.5px] uppercase tracking-wider text-faint">Structured Extract Rationale</div>
                <div className="whitespace-pre-wrap break-words rounded-md bg-edge/30 p-3 text-[12.5px] text-ink">
                  {traj.structured_extract.rationale}
                </div>
              </div>
            ) : null}
          </div>
        )}
        {visibleTab === "logic" && (
          <LogicChainPanel traj={traj} />
        )}
        {visibleTab === "market" && (
          <MarketViewPanel
            traj={traj}
            packet={packetJson}
            pred={pred ?? null}
            itemStatus={itemStatus}
            runId={runId}
            eventId={catalogItem.event_id}
          />
        )}
        {visibleTab === "packet" && (
          <PreTextBlock
            empty={!traj?.as_of_packet}
            emptyHint="（runner 未保存 as_of_packet）"
            text={traj?.as_of_packet ?? ""}
            code
          />
        )}
        {visibleTab === "prompt" && (
          <PreTextBlock
            empty={!traj?.question_to_team}
            emptyHint="（runner 未保存 question_to_team）"
            text={traj?.question_to_team ?? ""}
          />
        )}
      </div>
    </div>
  );
}

/* ---- 逻辑链 Panel：把 tool_trace 按 phase 分组 + hypotheses 展示 ---- */

function LogicChainPanel({ traj }: { traj: BTTrajectoryCkpt | null }) {
  const [openPhase, setOpenPhase] = useState<string | null>("plan");
  if (!traj?.team_final_state) {
    return (
      <div className="text-[12px] text-mute">
        没有 Team 协作记录（baseline/team_prompt 只产出单 agent rationale，没有 tool_trace）。
      </div>
    );
  }
  const tfs = traj.team_final_state;
  const toolTrace = (tfs.tool_trace ?? []) as Array<Record<string, unknown>>;
  const hypotheses = (tfs.hypotheses ?? []) as Array<Record<string, unknown>>;
  const phaseOrder = ["plan", "fan_out", "synthesize", "verify", "extract"];
  const phaseLabel: Record<string, string> = {
    plan: "① Plan（主理人拆解任务）",
    fan_out: "② Fan-out（各专家 Agent 并行调研）",
    synthesize: "③ Synthesize（合成初版结论）",
    verify: "④ Verify（复核 & 二次检索）",
    extract: "⑤ Extract（结构化成 direction/confidence/rationale）",
  };
  const byPhase: Record<string, Array<Record<string, unknown>>> = {};
  for (const t of toolTrace) {
    const ph = String(t.phase ?? t.stage ?? "其他");
    if (!byPhase[ph]) byPhase[ph] = [];
    byPhase[ph].push(t);
  }
  const orderedPhases = phaseOrder.filter((p) => (byPhase[p]?.length ?? 0) > 0);
  const others = Object.keys(byPhase).filter((p) => !phaseOrder.includes(p));
  const phases = [...orderedPhases, ...others];

  if (!phases.length && !hypotheses.length) {
    return (
      <PreTextBlock
        empty
        emptyHint="（tool_trace / hypotheses 为空；说明该 trajectory 只保留了 content_full，结构化逻辑链暂不可用，去「完整 Team Log」看全文。）"
        text=""
      />
    );
  }

  return (
    <div className="space-y-4 text-[12.5px]">
      {/* 顶部 Hypotheses 摘要 */}
      {hypotheses.length > 0 && (
        <div className="rounded-md border border-edge bg-edge/10 p-3">
          <div className="mb-2 flex items-center gap-1 text-[11px] font-medium uppercase tracking-wider text-faint">
            📝 Fan-out Hypotheses（各 Agent 形成的候选观点）
            <span className="font-mono text-[10px]">共 {hypotheses.length} 条</span>
          </div>
          <ul className="space-y-1.5 pl-1">
            {hypotheses.map((h: Record<string, unknown>, i: number) => {
              const label = String((h as any).agent_id ?? (h as any).from ?? (h as any).label ?? `H${i + 1}`);
              const body = String((h as any).text ?? (h as any).content ?? (h as any).summary ?? JSON.stringify(h));
              return (
                <li key={i} className="flex gap-2 items-start">
                  <span className="mt-0.5 shrink-0 rounded bg-brand-soft/50 px-1.5 py-0.5 text-[9.5px] text-brand font-medium">{label}</span>
                  <span className="whitespace-pre-wrap break-words text-ink/90">{body}</span>
                </li>
              );
            })}
          </ul>
        </div>
      )}

      {/* Phase 折叠面板 */}
      <div>
        <div className="mb-2 text-[11px] font-medium uppercase tracking-wider text-faint">
          🔗 Step-by-step 决策链（tool_trace）
        </div>
        <div className="space-y-2">
          {phases.map((p) => {
            const rows = byPhase[p] ?? [];
            const open = openPhase === p;
            return (
              <div key={p} className="rounded-md border border-edge overflow-hidden">
                <button
                  onClick={() => setOpenPhase((cur) => (cur === p ? null : p))}
                  className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left bg-edge/20 hover:bg-edge/40 transition"
                >
                  <span className="text-[11.5px] font-semibold text-ink">
                    {phaseLabel[p] ?? p}
                    <span className="ml-2 font-mono text-[10px] font-normal text-faint">{rows.length} 条</span>
                  </span>
                  {open ? <ChevronUp size={13} className="text-mute" /> : <ChevronDown size={13} className="text-mute" />}
                </button>
                {open && (
                  <ol className="divide-y divide-edge/60">
                    {rows.map((t, idx) => {
                      const tp = String(t.type ?? t.kind ?? "step");
                      const agent = String(t.agent_id ?? t.agent ?? "");
                      const name = String(t.name ?? t.tool ?? t.title ?? "");
                      const note = String(t.note ?? t.summary ?? t.phase_note ?? "");
                      const content =
                        t.content != null && String(t.content).length > 0
                          ? String(t.content)
                          : t.result != null
                          ? String(t.result)
                          : t.args != null && typeof t.args === "object"
                          ? JSON.stringify(t.args, null, 2)
                          : "";
                      return (
                        <li key={idx} className="px-3 py-2 text-[11.5px]">
                          <div className="flex flex-wrap items-center gap-1.5 text-[10.5px] uppercase tracking-wider text-faint">
                            <span className="rounded bg-edge/60 px-1.5 py-0.5 tracking-normal text-ink/80">{tp}</span>
                            {agent && <span className="font-mono">· agent={agent}</span>}
                            {name && <span className="font-mono">· {name}</span>}
                            {note && <span className="ml-1 tracking-normal text-mute">{note}</span>}
                          </div>
                          {content && (
                            <div className="mt-1.5 rounded bg-edge/20 p-2 text-[11.5px] leading-relaxed whitespace-pre-wrap break-words">
                              {content.length > 4000 ? content.slice(0, 4000) + `\n\n…(truncated, 原文长 ${content.length} chars)` : content}
                            </div>
                          )}
                        </li>
                      );
                    })}
                  </ol>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* ---- 标的视图 Panel：as_of 行情（从 as_of_packet / event_meta 取）+ 预测方向/置信度/强度可视化 ---- */

function MarketViewPanel({
  traj,
  packet,
  pred,
  itemStatus,
  runId,
  eventId,
}: {
  traj: BTTrajectoryCkpt | null;
  packet: Record<string, unknown> | null;
  pred: BTPredictionItem | null;
  itemStatus: BTEventStatus;
  runId: string;
  eventId: string;
}) {
  const any = packet ?? traj?.event_meta ?? {};
  const g = <K extends string>(ks: K[]): unknown => {
    for (const k of ks) if (k in any) return (any as Record<string, unknown>)[k];
    return undefined;
  };
  const symbol = (g(["symbol"]) as string | undefined) ?? pred?.symbol ?? "—";
  const market = (g(["market"]) as string | undefined) ?? pred?.market ?? "—";
  const eventTime = (g(["event_time", "as_of", "eventTime"]) as string | undefined) ?? "";
  const sourceUrl = (g(["source_url", "sourceUrl", "url"]) as string | undefined) ?? "";
  const sourceIsExternal = /^https?:\/\//i.test(sourceUrl);
  const t0Ar = g(["t0_ar", "t0AR", "t0_excess_return"]);
  const t0Car = g(["t0_car", "t0CAR", "t0_car_vs_benchmark"]);
  const pre5 = g(["pre5_ar", "pre5drift", "pre_5d_ar", "drift_pre_5"]);
  const pre20 = g(["pre20_ar", "pre20drift", "pre_20d_ar", "drift_pre_20"]);
  const benchmark = (g(["benchmark", "benchmark_symbol", "bench"]) as string | undefined) ?? "";
  const sectorEtf = (g(["sector_etf", "sectorEtf"]) as string | undefined) ?? "";
  const title = (g(["title", "event_title", "headline"]) as string | undefined) ?? "";
  const eventType = (g(["event_type_l2", "eventType"]) as string | undefined) ?? pred?.event_type_l2 ?? "";

  const predDir = traj?.structured_extract?.direction ?? pred?.pred_direction ?? "";
  const predConf = traj?.structured_extract?.confidence ?? pred?.confidence ?? null;
  const confGate = !!traj?.structured_extract?.conf_gate_applied;
  const outputFailure = predictionFailureInfo(pred);
  const outputOutcome = predictionOutcome(pred);

  // ---- K 线行情：懒加载（本 Panel 仅在「标的视图」Tab 激活时挂载），失败优雅降级为占位提示 ----
  const [klineData, setKlineData] = useState<BTPerformanceKline | null>(null);
  const [klineLoading, setKlineLoading] = useState(false);
  const [klineError, setKlineError] = useState<string | null>(null);
  useEffect(() => {
    if (!runId || !eventId) return;
    let cancelled = false;
    setKlineLoading(true);
    setKlineError(null);
    api.btGetEventKline(runId, eventId)
      .then((res) => {
        if (cancelled) return;
        if (res.ok && res.payload && Array.isArray(res.payload.dates)) {
          setKlineData(res.payload);
          setKlineError(null);
        } else {
          setKlineData(null);
          setKlineError(res.error ?? "行情数据为空");
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        setKlineData(null);
        setKlineError(err instanceof Error ? err.message : String(err));
      })
      .finally(() => {
        if (!cancelled) setKlineLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [runId, eventId]);

  const fullKline = klineData && Array.isArray(klineData.dates) && Array.isArray(klineData.ohlc) &&
    Array.isArray(klineData.volumes) && klineData.dates.length > 0 &&
    klineData.ohlc.length === klineData.dates.length && klineData.volumes.length === klineData.dates.length
    ? {
        symbol: klineData.symbol,
        dates: klineData.dates,
        ohlc: klineData.ohlc,
        volumes: klineData.volumes,
        event_date: klineData.event_date,
      }
    : null;
  const closeOnlyDates = Array.isArray(klineData?.dates) ? klineData.dates : [];
  const closeOnlyValues = Array.isArray(klineData?.closes)
    ? klineData.closes.filter((value): value is number => typeof value === "number" && Number.isFinite(value))
    : Array.isArray(klineData?.close)
      ? klineData.close.filter((value): value is number => typeof value === "number" && Number.isFinite(value))
      : [];

  let strengthLabel = itemStatus === "processing" ? "决策中" : itemStatus === "pending" ? "待执行" : "未形成方向判断";
  let strengthColor = "bg-edge text-mute";
  if (outputOutcome === "invalid_output") {
    strengthLabel = "输出无效";
    strengthColor = "bg-rise/10 text-rise";
  } else if (outputOutcome === "insufficient_data") {
    strengthLabel = "数据不足";
    strengthColor = "bg-amber-soft text-amber";
  } else if (outputOutcome === "voluntary_abstain") {
    strengthLabel = "主动弃权";
    strengthColor = "bg-violet-soft text-violet";
  } else if (predDir === "up" && predConf != null) {
    if (predConf >= 0.8) { strengthLabel = "强多 ↑↑"; strengthColor = "bg-fall/15 text-fall"; }
    else if (predConf >= 0.6) { strengthLabel = "偏多 ↑"; strengthColor = "bg-fall/10 text-fall"; }
    else { strengthLabel = "轻多 ↗"; strengthColor = "bg-fall/5 text-fall"; }
  } else if (predDir === "down" && predConf != null) {
    if (predConf >= 0.8) { strengthLabel = "强空 ↓↓"; strengthColor = "bg-rise/15 text-rise"; }
    else if (predConf >= 0.6) { strengthLabel = "偏空 ↓"; strengthColor = "bg-rise/10 text-rise"; }
    else { strengthLabel = "轻空 ↘"; strengthColor = "bg-rise/5 text-rise"; }
  } else if (predDir === "neutral") {
    strengthLabel = "中性 ⇄";
    strengthColor = "bg-edge text-mute";
  }

  const StatRow = ({ label, v, pct = false, hint }: { label: string; v: unknown; pct?: boolean; hint?: string }) => {
    const raw = v as number | string | undefined;
    let display: string;
    let cls = "";
    if (typeof raw === "number") {
      if (pct) display = (raw >= 0 ? "+" : "") + (raw * 100).toFixed(2) + "%";
      else display = raw.toString();
      if (pct) cls = raw >= 0 ? "text-fall" : raw < 0 ? "text-rise" : "";
    } else if (raw == null) {
      display = "—";
    } else {
      display = String(raw);
    }
    return (
      <div>
        <div className="text-[10.5px] uppercase tracking-wider text-faint" title={hint}>{label}</div>
        <div className={clsx("mt-0.5 font-mono text-[13px] tabular-nums text-ink", cls)}>{display}</div>
      </div>
    );
  };

  return (
    <div className="space-y-5">
      <section className="grid grid-cols-1 gap-4 md:grid-cols-[auto_1fr] items-start">
        <div className="flex flex-col items-center justify-center gap-2 rounded-xl border border-edge bg-edge/10 px-5 py-4 text-center">
          <div className="text-[10.5px] uppercase tracking-wider text-faint">方向预测 · {(() => {
            const horizon = String(pred?.horizon || "t3");
            return /^t\d+$/i.test(horizon) ? `T+${horizon.slice(1)}` : horizon.toUpperCase();
          })()}</div>
          {outputOutcome === "invalid_output" ? (
            <span className="rounded bg-rise/10 px-2 py-0.5 text-[11px] font-medium text-rise">无有效预测</span>
          ) : outputOutcome === "insufficient_data" ? (
            <span className="rounded bg-amber-soft px-2 py-0.5 text-[11px] font-medium text-amber">数据不足</span>
          ) : outputOutcome === "voluntary_abstain" ? (
            <span className="rounded bg-violet-soft px-2 py-0.5 text-[11px] font-medium text-violet">主动弃权</span>
          ) : predDir || itemStatus !== "done" ? (
            <DirBadge d={predDir || (itemStatus === "processing" ? "决策中" : "TBD")} />
          ) : (
            <span className="text-faint text-[12px]">（无）</span>
          )}
          <span className={cls("mt-1 rounded px-2 py-0.5 text-[11px] font-semibold", strengthColor)}>{strengthLabel}</span>
        </div>
        <div className="space-y-3">
          <div>
            <div className="flex items-baseline justify-between">
              <span className="text-[10.5px] uppercase tracking-wider text-faint">置信度 Confidence</span>
              <span className="font-mono text-[11px] text-ink">
                {outputOutcome === "invalid_output" || outputOutcome === "insufficient_data" || outputOutcome === "voluntary_abstain"
                  ? "不计分"
                  : predConf != null ? (predConf * 100).toFixed(0) + "%" : itemStatus === "processing" ? "计算中" : "—"}
              </span>
            </div>
            <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-edge/80">
              {predConf != null && outputOutcome !== "invalid_output" && outputOutcome !== "insufficient_data" && outputOutcome !== "voluntary_abstain" && (
                <div
                  className={cls(
                    "h-full rounded-full transition-all",
                    predDir === "up" ? "bg-fall" : predDir === "down" ? "bg-rise" : "bg-faint",
                  )}
                  style={{ width: `${Math.max(4, (predConf ?? 0) * 100)}%` }}
                />
              )}
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2 text-[10.5px]">
            {confGate && <span className="rounded bg-brand-soft/60 px-2 py-0.5 text-brand">✓ Confidence Gate 已生效（低于 50% 强降 neutral）</span>}
            {outputOutcome === "invalid_output" && (
              <span className="rounded bg-rise/10 px-2 py-0.5 text-rise" title={outputFailure?.detail}>⚠ 模型输出无效 · 未计分</span>
            )}
            {outputOutcome === "voluntary_abstain" && (
              <span className="rounded bg-violet-soft px-2 py-0.5 text-violet">主动弃权 · 未计分</span>
            )}
            {outputOutcome === "insufficient_data" && (
              <span className="rounded bg-amber-soft px-2 py-0.5 text-amber" title={pred?.rationale || "缺少判断所需资料"}>数据不足 · 不计准确率</span>
            )}
            {pred?.oracle_label_t3 && (
              <span className="rounded bg-edge/70 px-2 py-0.5 text-mute">
                Oracle T+3：{(pred.oracle_label_t3 as string).toUpperCase()}
                {pred.oracle_car_t3 != null
                  ? ` · CAR = ${(pred.oracle_car_t3 * 100).toFixed(2)}%`
                  : ""}
              </span>
            )}
          </div>
        </div>
      </section>

      <section className="rounded-md border border-edge p-3 space-y-2 text-[12px]">
        <div className="text-[10.5px] uppercase tracking-wider text-faint">事件元信息 · As-of 视角（无未来函数）</div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-2 md:grid-cols-4">
          <StatRow label="标的 Symbol" v={symbol} />
          <StatRow label="市场 Market" v={market} />
          <StatRow label="事件类型" v={eventType} />
          <StatRow label="事件时间" v={eventTime ? eventTime.slice(0, 19).replace("T", " ") : undefined} />
        </div>
        {title && <div className="text-[12px] text-ink/90 whitespace-pre-wrap break-words">{title}</div>}
        {sourceUrl && sourceIsExternal ? (
          <a
            href={sourceUrl}
            target="_blank"
            rel="noreferrer"
            className="text-[11px] text-brand truncate hover:underline"
            title={sourceUrl}
          >
            🔗 {sourceUrl}
          </a>
        ) : sourceUrl ? (
          <div className="text-[11px] text-mute">📝 手工录入（无可用外链）</div>
        ) : null}
      </section>

      {/* K 线图表：事件日前后日K（复权） */}
      <section className="rounded-md border border-edge p-3 space-y-3">
        <div className="flex items-baseline justify-between">
          <div className="text-[10.5px] uppercase tracking-wider text-faint">事件日前后真实行情 · 完整 OHLC 或 close-only</div>
          {klineData?.symbol && (
            <span className="rounded bg-brand-soft/40 px-1.5 py-0.5 font-mono text-[10.5px] text-brand">{klineData.symbol}</span>
          )}
        </div>
        {klineLoading && (
          <div className="flex items-center gap-2 py-8 text-[12px] text-mute">
            <Loader2 size={13} className="animate-spin text-brand" /> 正在拉取行情数据（akshare/yfinance，首次可能较慢）...
          </div>
        )}
        {!klineLoading && klineError && (
          <div className="rounded-md bg-edge/30 px-3 py-4 text-[12px] text-mute">
            行情加载失败：{klineError}
            <div className="mt-1 text-[11px] text-faint">（不影响上方 as-of 漂移/基准先验指标，仅 K 线图不可用。）</div>
          </div>
        )}
        {!klineLoading && fullKline && <KlineChart payload={fullKline} height={380} />}
        {!klineLoading && klineData && !fullKline && closeOnlyDates.length === closeOnlyValues.length && closeOnlyDates.length > 0 && (
          <div>
            <div className="mb-2 inline-flex rounded bg-edge/50 px-2 py-1 text-[9.5px] text-mute">close-only · 不补造 OHLC</div>
            <CloseOnlyChart dates={closeOnlyDates} closes={closeOnlyValues} height={340} />
          </div>
        )}
      </section>

      <section className="rounded-md border border-edge p-3 space-y-3">
        <div className="flex items-baseline justify-between">
          <div className="text-[10.5px] uppercase tracking-wider text-faint">行情先验（As-of 计算，避免未来函数）</div>
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-3 md:grid-cols-4">
          <StatRow label="T0 超额 AR (vs 基准)" v={t0Ar} pct hint="事件日开盘~当前时间点的超额收益" />
          <StatRow label="T0 累计 CAR" v={t0Car} pct />
          <StatRow label="Pre T-5 漂移" v={pre5} pct hint="事件前 5 日的超额收益累积" />
          <StatRow label="Pre T-20 漂移" v={pre20} pct hint="事件前 20 日的超额收益累积（预期泄露信号）" />
        </div>
        <div className="flex flex-wrap gap-2 text-[11px]">
          {benchmark && <span className="rounded bg-edge/50 px-2 py-0.5 text-mute">benchmark: {benchmark}</span>}
          {sectorEtf && <span className="rounded bg-edge/50 px-2 py-0.5 text-mute">sector ETF: {sectorEtf}</span>}
        </div>
      </section>
    </div>
  );
}
