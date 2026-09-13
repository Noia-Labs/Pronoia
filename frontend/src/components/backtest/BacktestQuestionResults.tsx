import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, Loader2, MessageSquareText, Play, RefreshCw, Square } from "lucide-react";
import { api } from "../../api";
import type { ModelLabBacktestRunResults, ModelLabResults } from "../../types";
import { cls } from "../../utils";
import ModelLabResultsPanel, { LabStatus } from "../../pages/model-lab/ModelLabResultsPanel";

/** QA owns its lifecycle: a completed prediction must not stop this polling. */
export function useBacktestQuestionResults(runId: string | null | undefined, eventModel: boolean) {
  const [lookup, setLookup] = useState<{ runId: string; value: ModelLabBacktestRunResults } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const refresh = useCallback(() => setRevision((value) => value + 1), []);

  useEffect(() => {
    if (!runId || !eventModel) {
      setLookup(null); setLoading(false); setError(null);
      return;
    }
    let alive = true;
    let timer: number | undefined;
    setError(null);
    const load = async (initial: boolean) => {
      if (initial) setLoading(true);
      try {
        const response = await api.modelLabBacktestRunResults(runId);
        if (!alive) return;
        setLookup({ runId, value: response });
        setError(null);
        // Pending tasks can be started elsewhere. Partial batches with a finish
        // time are terminal; do not keep polling solely because of that label.
        if (response.enabled && response.results && qaNeedsRefresh(response.results)) {
          timer = window.setTimeout(() => void load(false), 3000);
        }
      } catch (reason) {
        if (!alive) return;
        setError(reason instanceof Error ? reason.message : String(reason));
        // A transient fetch error must not silently stop a live QA experiment.
        timer = window.setTimeout(() => void load(false), 6000);
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load(true);
    return () => { alive = false; if (timer !== undefined) window.clearTimeout(timer); };
  }, [runId, eventModel, revision]);

  const value = lookup && lookup.runId === runId ? lookup.value : null;
  return { value, loading, error, refresh };
}

function qaNeedsRefresh(results: ModelLabResults): boolean {
  return results.tasks.some((task) => task.kind === "qa" && ["pending", "queued", "starting", "running", "cancelling"].includes(task.status));
}

export function questionTaskStatus(results: ModelLabResults | null | undefined): string {
  const tasks = results?.tasks.filter((task) => task.kind === "qa") ?? [];
  if (!tasks.length) return "pending";
  if (tasks.some((task) => task.status === "cancelling")) return "cancelling";
  if (tasks.some((task) => ["running", "starting", "queued"].includes(task.status))) return "running";
  if (tasks.some((task) => task.status === "pending")) return "pending";
  if (tasks.every((task) => task.status === "done")) {
    return results?.qa_results.some((answer) => answer.status === "awaiting_manual") ? "awaiting_manual" : "done";
  }
  if (tasks.every((task) => task.status === "failed")) return "failed";
  if (tasks.every((task) => task.status === "cancelled")) return "cancelled";
  return "partial";
}

type QuestionState = ReturnType<typeof useBacktestQuestionResults>;

export function BacktestQuestionSummary({ state, onOpen }: { state: QuestionState; onOpen: () => void }) {
  if (!state.value?.enabled) {
    if (state.error) return <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-rise/20 bg-rise/5 px-4 py-3 text-[11px] text-rise"><span className="inline-flex items-center gap-2"><AlertTriangle size={13} />问答关联信息读取失败，预测结果不受影响。</span><button type="button" onClick={state.refresh} className="inline-flex items-center gap-1.5 font-medium"><RefreshCw size={12} />重试</button></div>;
    return null;
  }
  const tasks = state.value.results?.tasks.filter((task) => task.kind === "qa") ?? [];
  const done = tasks.reduce((sum, task) => sum + task.done_items, 0);
  const total = tasks.reduce((sum, task) => sum + task.total_items, 0);
  const status = questionTaskStatus(state.value.results);
  return <section className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-violet/20 bg-violet-soft/20 px-4 py-3.5" aria-label="问答测试进度">
    <div className="flex min-w-0 items-center gap-3"><span className="grid h-9 w-9 shrink-0 place-items-center rounded-xl bg-card text-violet"><MessageSquareText size={17} /></span><div><div className="flex flex-wrap items-center gap-2"><h3 className="text-[12px] font-semibold text-ink">已附加问答测试</h3><LabStatus status={status} /><span className="font-mono text-[11px] text-mute">{done} / {total}</span></div><p className="mt-1 text-[11px] text-mute">独立于预测运行，回答和评分会逐条更新。</p></div></div>
    <button type="button" onClick={onOpen} className="rounded-lg border border-violet/20 bg-card px-3 py-2 text-[11px] font-semibold text-violet">{status === "pending" ? "查看 / 启动问答" : "查看问答结果"} →</button>
  </section>;
}

export default function BacktestQuestionResults({ runId, state }: { runId: string; state: QuestionState }) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const results = state.value?.results;
  const batchId = state.value?.batch_id ?? results?.batch.id;
  const tasks = results?.tasks ?? [];
  const sidecarOnly = tasks.length > 0 && tasks.every((task) => task.kind === "qa");
  const cancelling = tasks.some((task) => task.status === "cancelling");
  const running = tasks.some((task) => ["running", "queued", "starting", "cancelling"].includes(task.status));
  const canStart = results && ["pending", "failed", "cancelled", "partial"].includes(results.batch.status) && !running;
  const action = async (kind: "start" | "cancel") => {
    if (!batchId) return;
    if (kind === "start" && !window.confirm(sidecarOnly ? "启动问答测试会调用问答模型；如启用了自动评分，还会调用评分模型。可能产生费用，确认继续？" : "启动整个评测批次会调用未完成的预测与问答模型 API，可能产生费用。确认继续？")) return;
    if (kind === "cancel" && !window.confirm(sidecarOnly ? "取消尚未完成的问答任务？已产生的回答会保留。" : "这是一个联合评测批次，取消将同时停止其未完成的预测和问答任务。确认继续？")) return;
    setBusy(true); setActionError(null);
    try {
      if (kind === "start") await api.modelLabStartBatch(batchId);
      else await api.modelLabCancelBatch(batchId);
      state.refresh();
    } catch (reason) { setActionError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };

  return <section className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card shadow-card">
    <div className="flex flex-wrap items-center justify-between gap-3 border-b border-edge bg-paper px-5 py-3"><p className="text-[11px] text-mute">{sidecarOnly ? "这里的启动与取消只影响问答，预测任务保持独立。" : "此回测属于联合评测批次；批次操作会同时影响其未完成任务。"}</p><div className="flex flex-wrap gap-2">
      {canStart && <button type="button" onClick={() => void action("start")} disabled={busy} className="inline-flex items-center gap-1.5 rounded-lg bg-violet px-3 py-2 text-[11px] font-medium text-white disabled:opacity-40">{busy ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}{sidecarOnly ? "启动问答测试" : "启动评测批次"}</button>}
      {running && <button type="button" onClick={() => void action("cancel")} disabled={busy || cancelling} className="inline-flex items-center gap-1.5 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2 text-[11px] font-medium text-rise disabled:opacity-40">{cancelling ? <Loader2 size={12} className="animate-spin" /> : <Square size={12} />}{cancelling ? "正在取消…" : sidecarOnly ? "取消问答" : "取消整个批次"}</button>}
      <button type="button" onClick={state.refresh} disabled={state.loading} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[11px] text-mute disabled:opacity-40"><RefreshCw size={12} className={cls(state.loading && "animate-spin")} />刷新问答</button>
    </div></div>
    {(state.error || actionError) && <div role="alert" className="m-4 rounded-xl border border-rise/20 bg-rise/5 px-4 py-3 text-[11px] leading-relaxed text-rise">{actionError ?? state.error}</div>}
    {results ? <ModelLabResultsPanel results={results} currentRunId={runId} onUpdated={state.refresh} /> : <div className="flex min-h-48 items-center justify-center gap-2 px-5 text-center text-[12px] text-mute">{state.loading && <Loader2 size={15} className="animate-spin" />}{state.loading ? "读取独立问答结果…" : "关联的问答结果暂不可用。预测数据仍可正常查看。"}</div>}
  </section>;
}
