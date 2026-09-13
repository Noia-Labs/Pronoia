import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, ArrowUpRight, CheckCircle2, Clock3, Loader2, RefreshCw, Search, Trash2, X } from "lucide-react";
import { api } from "../../api";
import { useStore } from "../../store";
import type { BTRun, ModelLabBatch } from "../../types";
import { cls } from "../../utils";
import BacktestNav from "./BacktestNav";
import {
  activeHistoryStatuses,
  canDeleteHistoryEntry,
  historyEntryKey,
  toggleVisibleHistorySelection,
  type HistoryRecordType,
} from "./runHistorySelection";

type Kind = "event" | "quant" | "pronoia";
type Entry = { id: string; recordType: HistoryRecordType; name: string; kind: Kind; status: string; created: string; detail: string; legacy?: boolean; qaStatus?: string; run?: BTRun; batch?: ModelLabBatch };
const kinds: Record<Kind, string> = { event: "事件模型", quant: "量化模型", pronoia: "Pronoia 基模评测" };
const statuses: Record<string, string> = { pending: "待运行", queued: "排队中", starting: "启动中", running: "运行中", paused: "已暂停", interrupted: "待恢复", cancelling: "取消中", done: "已完成", failed: "失败", cancelled: "已取消", partial: "部分完成" };

export default function UnifiedRunHistory({ onOpenMetrics, embedded = false, refreshVersion = 0, modelId, modelRunIds, modelBatchIds }: {
  onOpenMetrics?: () => void;
  embedded?: boolean;
  refreshVersion?: number;
  modelId?: string;
  modelRunIds?: ReadonlySet<string>;
  modelBatchIds?: ReadonlySet<string>;
}) {
  const openBTDetail = useStore((state) => state.openBTDetail);
  const [runs, setRuns] = useState<BTRun[]>([]);
  const [batches, setBatches] = useState<ModelLabBatch[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [kind, setKind] = useState<"all" | Kind>("all");
  const [status, setStatus] = useState("all");
  const [query, setQuery] = useState("");
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState<Entry[] | null>(null);
  const [deleteSaving, setDeleteSaving] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [notice, setNotice] = useState("");
  const requestSequence = useRef(0);
  const deleteLock = useRef(false);
  const selectAllInput = useRef<HTMLInputElement>(null);
  const deleteDialog = useRef<HTMLDivElement>(null);
  const load = useCallback(async (quiet = false) => {
    const requestId = ++requestSequence.current;
    if (!quiet) setLoading(true);
    try {
      const [runResponse, batchResponse] = await Promise.all([api.btRuns(500), api.modelLabBatches(500)]);
      if (requestId !== requestSequence.current) return;
      setRuns(runResponse.items); setBatches(batchResponse.items); setError("");
    } catch (reason) { if (requestId === requestSequence.current) setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { if (requestId === requestSequence.current) setLoading(false); }
  }, []);
  useEffect(() => () => { requestSequence.current += 1; }, []);
  useEffect(() => { void load(); }, [load, refreshVersion]);
  const hasActiveWork = runs.some((run) => activeHistoryStatuses.has(run.status))
    || batches.some((batch) => activeHistoryStatuses.has(batch.status));
  useEffect(() => {
    if (!hasActiveWork || deleteSaving) return;
    let stopped = false;
    let timer = window.setTimeout(poll, 5000);
    async function poll() {
      await load(true);
      if (!stopped) timer = window.setTimeout(poll, 5000);
    }
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [hasActiveWork, deleteSaving, load]);
  const entries = useMemo(() => {
    const relatedRuns = modelRunIds ? runs.filter((run) => modelRunIds.has(run.id) || run.config?.saved_model_id === modelId) : runs;
    const relatedBatchIds = new Set([...modelBatchIds ?? [], ...relatedRuns.map((run) => String(run.config?.model_lab_batch_id ?? ""))]);
    const evaluations = batches.filter((batch) => batch.scoring?.source !== "event_experiment" && (!modelRunIds || relatedBatchIds.has(batch.id)));
    const batchIds = new Set(evaluations.map((batch) => batch.id));
    const qaById = new Map(batches.filter((batch) => batch.scoring?.source === "event_experiment").map((batch) => [batch.id, batch]));
    const rows: Entry[] = relatedRuns.filter((run) => !batchIds.has(String(run.config?.model_lab_batch_id ?? ""))).map((run) => ({
      id: run.id, recordType: "run", name: run.name, kind: run.config?.evaluation_source === "model_lab" ? "pronoia" : run.strategy_type === "quant" || run.engine_mode === "portfolio" ? "quant" : "event",
      status: run.status, created: run.created_at, run,
      qaStatus: run.config?.event_qa_batch_id ? qaById.get(String(run.config.event_qa_batch_id))?.status ?? "未读取" : undefined,
      detail: `${run.dataset_name || "已冻结数据"} · ${run.done_events ?? 0}/${run.total_events ?? 0} 项 · ${run.model_version || run.strategy_spec?.kind || run.runner}`,
    }));
    evaluations.forEach((batch) => rows.push({
      id: batch.id, recordType: "batch", name: batch.name, kind: "pronoia", legacy: batch.scoring?.source !== "pronoia_base_evaluation", status: batch.status, created: batch.created_at ?? "", batch,
      detail: batch.scoring?.source !== "pronoia_base_evaluation" ? `历史流程 · ${batch.prediction?.enabled ? batch.prediction.runner : "无预测任务"} · ${batch.qa?.enabled ? batch.qa.variants.join(" / ") : "无问答任务"}` : `${batch.profile_ids.length} 个基模 · ${[batch.prediction?.enabled && "事件方向 + 收益率预测", batch.qa?.enabled && "问答测试"].filter(Boolean).join(" · ") || "历史评测"}${batch.demo ? " · 编排演练" : ""}`,
    }));
    return rows.sort((a, b) => b.created.localeCompare(a.created));
  }, [runs, batches, modelId, modelRunIds, modelBatchIds]);
  const filtered = useMemo(() => entries.filter((entry) => (kind === "all" || entry.kind === kind) && (status === "all" || (status === "active" ? activeHistoryStatuses.has(entry.status) || activeHistoryStatuses.has(entry.qaStatus ?? "") : entry.status === status || entry.qaStatus === status)) && `${entry.name} ${entry.id} ${entry.detail}`.toLowerCase().includes(query.toLowerCase())), [entries, kind, status, query]);
  const selectedEntries = useMemo(() => entries.filter((entry) => selectedKeys.has(historyEntryKey(entry))), [entries, selectedKeys]);
  const selectableFiltered = useMemo(() => filtered.filter(canDeleteHistoryEntry), [filtered]);
  const allVisibleSelected = selectableFiltered.length > 0 && selectableFiltered.every((entry) => selectedKeys.has(historyEntryKey(entry)));
  const someVisibleSelected = selectableFiltered.some((entry) => selectedKeys.has(historyEntryKey(entry)));
  const selectedOutsideFilter = selectedEntries.some((entry) => !filtered.includes(entry));
  useEffect(() => {
    const valid = new Set(entries.filter(canDeleteHistoryEntry).map(historyEntryKey));
    setSelectedKeys((current) => {
      const next = new Set([...current].filter((key) => valid.has(key)));
      return next.size === current.size && [...next].every((key) => current.has(key)) ? current : next;
    });
  }, [entries]);
  useEffect(() => {
    if (selectAllInput.current) selectAllInput.current.indeterminate = someVisibleSelected && !allVisibleSelected;
  }, [someVisibleSelected, allVisibleSelected]);
  useEffect(() => {
    if (!deleting) return;
    const previousFocus = document.activeElement;
    deleteDialog.current?.querySelector<HTMLButtonElement>('[data-cancel-delete]')?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !deleteLock.current) { event.preventDefault(); setDeleting(null); }
      if (event.key !== "Tab") return;
      const controls = deleteDialog.current?.querySelectorAll<HTMLElement>('button:not([disabled]),input:not([disabled]),a[href],[tabindex="0"]');
      if (!controls?.length) { event.preventDefault(); return; }
      const first = controls[0], last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => {
      document.removeEventListener("keydown", keydown);
      if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus();
    };
  }, [deleting]);
  const toggleSelection = (entry: Entry) => setSelectedKeys((current) => {
    const key = historyEntryKey(entry); const next = new Set(current);
    if (next.has(key)) next.delete(key); else next.add(key);
    return next;
  });
  const deleteSelected = async () => {
    if (!deleting?.length || deleteLock.current) return;
    deleteLock.current = true; setDeleteSaving(true); setDeleteError(""); setNotice("");
    try {
      const response = await api.btDeleteHistoryRecords({
        run_ids: deleting.filter((entry) => entry.recordType === "run").map((entry) => entry.id),
        batch_ids: deleting.filter((entry) => entry.recordType === "batch").map((entry) => entry.id),
      });
      if (!response.ok) throw new Error(response.message || "删除未完成");
      const removed = new Set(deleting.map(historyEntryKey));
      setSelectedKeys((current) => new Set([...current].filter((key) => !removed.has(key))));
      setRuns((current) => current.filter((run) => !response.deleted_run_ids.includes(run.id)));
      setBatches((current) => current.filter((batch) => !response.deleted_batch_ids.includes(batch.id)));
      const deletedRunIds = new Set(response.deleted_run_ids);
      useStore.setState((state) => ({ btRuns: state.btRuns.filter((run) => !deletedRunIds.has(run.id)) }));
      setNotice(`已从应用中删除 ${response.requested_count} 条记录；本地源数据与运行输出文件保持不变。`);
      setDeleting(null);
      await load(true);
    } catch (reason) {
      setDeleteError(reason instanceof Error ? reason.message : "删除未完成，请重试");
    } finally {
      deleteLock.current = false; setDeleteSaving(false);
    }
  };
  return <div className={embedded ? "min-w-0" : "flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-paper"}>
    {!embedded && <BacktestNav actions={<button type="button" onClick={() => void load()} disabled={loading} className="flex items-center gap-2 rounded-lg border border-edge px-3 py-2 text-xs text-mute"><RefreshCw size={13} className={loading ? "animate-spin" : ""} />刷新</button>} />}
    <div className={embedded ? "min-w-0" : "min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain px-5 py-8 sm:px-8 lg:px-10"}>
      <div className="mx-auto w-full max-w-[1500px] space-y-6">
        {!embedded && <div className="flex flex-col gap-4 lg:flex-row lg:items-end lg:justify-between"><div className="min-w-0"><p className="text-[10px] font-semibold tracking-[.24em] text-faint">EXPERIMENT HISTORY</p><h1 className="mt-2 font-serif text-3xl text-ink">运行记录</h1><p className="mt-3 text-sm leading-relaxed text-mute">事件模型、量化模型和 Pronoia 基模评测的历史结果。基模评测按批次归档，包含仅问答的评测。</p></div>{onOpenMetrics && <button type="button" onClick={onOpenMetrics} className="shrink-0 self-start whitespace-nowrap rounded-xl border border-edge bg-card px-4 py-3 text-xs text-mute lg:self-auto">回测指标与管理 <ArrowUpRight size={12} className="ml-1 inline" /></button>}</div>}
        <div className="flex flex-wrap gap-2">{(["all", "event", "quant", "pronoia"] as const).map((value) => <button key={value} type="button" onClick={() => setKind(value)} className={cls("rounded-xl border px-4 py-2.5 text-xs font-medium", value === kind ? "border-ink bg-ink text-card" : "border-edge bg-card text-mute")}>{value === "all" ? "全部记录" : kinds[value]} <span className="ml-2 font-mono opacity-60">{entries.filter((row) => value === "all" || row.kind === value).length}</span></button>)}</div>
        <div className="flex flex-col gap-3 sm:flex-row"><label className="flex min-w-0 flex-1 items-center gap-2 rounded-xl border border-edge bg-card px-4"><Search size={15} className="shrink-0 text-faint" /><input aria-label="搜索运行记录" placeholder="搜索名称、模型或记录 ID" value={query} onChange={(event) => setQuery(event.target.value)} className="min-w-0 w-full bg-transparent py-3 text-sm outline-none" /></label><select aria-label="运行状态" value={status} onChange={(event) => setStatus(event.target.value)} className="shrink-0 rounded-xl border border-edge bg-card px-4 py-3 text-sm"><option value="all">全部状态</option><option value="active">未结束</option>{Object.entries(statuses).filter(([id]) => !activeHistoryStatuses.has(id)).map(([id, label]) => <option key={id} value={id}>{label}</option>)}</select></div>
        {entries.length > 0 && <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-edge bg-card px-4 py-3">
          <div className="flex flex-wrap items-center gap-4 text-xs">
            <label className="flex cursor-pointer items-center gap-2 text-ink"><input ref={selectAllInput} type="checkbox" checked={allVisibleSelected} disabled={deleteSaving || selectableFiltered.length === 0} onChange={() => setSelectedKeys((current) => toggleVisibleHistorySelection(current, filtered))} className="h-4 w-4 cursor-pointer rounded accent-brand disabled:cursor-not-allowed" />全选当前可删除记录</label>
            <span aria-live="polite" className="text-mute">已选 {selectedEntries.length} 条{selectedOutsideFilter ? "（包含其他筛选下的选择）" : ""}</span>
            {selectedEntries.length > 0 && <button type="button" disabled={deleteSaving} onClick={() => setSelectedKeys(new Set())} className="text-mute underline underline-offset-4 disabled:opacity-40">清空选择</button>}
          </div>
          <button type="button" disabled={!selectedEntries.length || deleteSaving} onClick={() => { setDeleting(selectedEntries); setDeleteError(""); setNotice(""); }} className="inline-flex items-center gap-2 rounded-lg border border-rise/25 bg-rise/5 px-3 py-2 text-xs font-medium text-rise transition hover:bg-rise/10 disabled:opacity-35"><Trash2 size={14} />删除所选{selectedEntries.length ? `（${selectedEntries.length}）` : ""}</button>
        </div>}
        {notice && <div role="status" className="flex items-center justify-between gap-3 rounded-xl border border-jade/20 bg-jade-soft/25 px-4 py-3 text-xs text-jade"><p className="flex items-center gap-2"><CheckCircle2 size={15} />{notice}</p><button type="button" onClick={() => setNotice("")} aria-label="关闭删除提示" className="rounded-lg p-1 text-mute"><X size={14} /></button></div>}
        {error && <div role="alert" className="flex items-start gap-2 rounded-xl border border-rise/20 bg-rise/5 p-4 text-sm text-rise"><AlertCircle size={16} className="mt-0.5 shrink-0" /><span>{error}</span></div>}
        <div className="overflow-hidden rounded-2xl border border-edge bg-card">
          {filtered.map((entry) => {
            const key = historyEntryKey(entry);
            const deletable = canDeleteHistoryEntry(entry);
            const selected = selectedKeys.has(key);
            return <div key={key} className={cls("grid grid-cols-[2.5rem_2.5rem_minmax(0,1fr)] items-start gap-x-3 gap-y-3 border-b border-edge px-4 py-5 transition last:border-0 sm:px-5 xl:grid-cols-[2.5rem_2.5rem_minmax(0,1fr)_auto] xl:items-center", selected && "bg-brand-soft/20")}>
              <label title={deletable ? `选择 ${entry.name}` : "运行中的记录需先取消并等待任务停止"} tabIndex={deletable ? undefined : 0} aria-label={deletable ? undefined : `${entry.name} 仍在运行，需先取消并等待任务停止`} className={cls("grid h-10 w-10 place-items-center rounded-lg", deletable ? "cursor-pointer" : "cursor-not-allowed")}><input type="checkbox" checked={selected} disabled={!deletable || deleteSaving} onChange={() => toggleSelection(entry)} aria-label={`选择运行记录 ${entry.name}`} className="h-4 w-4 shrink-0 rounded accent-brand disabled:cursor-not-allowed disabled:opacity-35" /></label>
              <div className="grid h-10 w-10 place-items-center rounded-xl bg-paper text-faint"><Clock3 size={18} /></div>
              <div className="min-w-0"><div className="flex flex-wrap items-center gap-2"><span className="min-w-0 break-words text-sm font-semibold text-ink">{entry.name}</span><span className="shrink-0 rounded-md bg-violet-soft px-2 py-1 text-[10px] text-violet">{entry.legacy ? "历史模型评测（旧流程）" : kinds[entry.kind]}</span></div><p className="mt-2 break-words text-xs leading-relaxed text-mute">{entry.detail}</p><p className="mt-1 break-words text-[10px] text-faint">{entry.created ? new Date(entry.created).toLocaleString("zh-CN", { hour12: false }) : "时间未记录"} · {entry.id}</p></div>
              <div className="col-start-3 flex min-w-0 flex-wrap items-center gap-2 [&>*]:shrink-0 [&>*]:whitespace-nowrap xl:col-start-auto xl:justify-end">
                <span className={cls("rounded-full px-3 py-1 text-xs", entry.status === "failed" ? "bg-rise/10 text-rise" : activeHistoryStatuses.has(entry.status) ? "bg-violet-soft text-violet" : "bg-paper text-mute")}>{entry.qaStatus ? "预测 · " : ""}{statuses[entry.status] ?? entry.status}</span>
                {entry.qaStatus && <span className={cls("rounded-full px-3 py-1 text-xs", entry.qaStatus === "failed" ? "bg-rise/10 text-rise" : activeHistoryStatuses.has(entry.qaStatus) ? "bg-violet-soft text-violet" : "bg-paper text-mute")}>问答 · {statuses[entry.qaStatus] ?? entry.qaStatus}</span>}
                {entry.recordType === "batch" ? <a href={`/backtest/model-lab?batch=${encodeURIComponent(entry.id)}`} className="rounded-lg border border-edge px-3 py-2 text-xs font-medium text-ink">查看批次</a> : <button type="button" onClick={() => openBTDetail(entry.id)} className="rounded-lg border border-edge px-3 py-2 text-xs font-medium text-ink">查看结果</button>}
              </div>
            </div>;
          })}
          {!filtered.length && <div className="p-12 text-center text-sm text-mute">{loading ? "正在读取记录…" : "没有符合条件的运行记录"}</div>}
        </div>
        <p className="text-[10px] text-faint">显示最近 500 条回测及 500 个评测批次；全选只作用于当前已加载并符合筛选的结果。</p>
      </div>
    </div>
    {deleting && <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-4 backdrop-blur-sm"><div ref={deleteDialog} role="dialog" aria-modal="true" aria-labelledby="delete-history-title" aria-describedby="delete-history-description" className="flex max-h-[85dvh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-edge bg-card shadow-pop">
      <header className="flex items-start justify-between gap-3 border-b border-edge px-5 py-4"><div><div className="mb-3 inline-flex rounded-xl bg-rise/5 p-2.5 text-rise"><Trash2 size={20} /></div><h2 id="delete-history-title" className="font-serif text-[22px] font-semibold text-ink">删除这 {deleting.length} 条记录？</h2><p id="delete-history-description" className="mt-2 text-xs leading-relaxed text-mute">将从应用中删除所选运行或评测批次及数据库内的结果明细，此操作不可撤销。</p></div><button type="button" disabled={deleteSaving} onClick={() => setDeleting(null)} aria-label="关闭删除确认" className="rounded-lg p-2 text-mute disabled:opacity-40"><X size={17} /></button></header>
      <div className="min-h-0 overflow-y-auto p-5"><ul className="divide-y divide-edge overflow-hidden rounded-xl border border-edge bg-paper/50">{deleting.slice(0, 20).map((entry) => <li key={historyEntryKey(entry)} className="px-4 py-3"><p className="break-words text-[13px] font-medium text-ink">{entry.name}</p><p className="mt-1 text-[11px] text-mute">{entry.recordType === "batch" ? "评测批次" : kinds[entry.kind]} · {statuses[entry.status] ?? entry.status}</p></li>)}</ul>{deleting.length > 20 && <p className="mt-2 text-center text-[11px] text-mute">另有 {deleting.length - 20} 条已选择记录</p>}<p className="mt-4 text-xs leading-relaxed text-mute">行情数据集、保存的模型、源文件及已有运行输出文件不会被删除。运行中的记录以及仍被 Arena 引用的记录会被系统阻止，整批选择不会只删除一部分。</p>{deleteError && <p role="alert" className="mt-4 rounded-lg border border-rise/20 bg-rise/5 p-3 text-xs text-rise">{deleteError}</p>}</div>
      <footer className="flex justify-end gap-2 border-t border-edge px-5 py-4"><button data-cancel-delete type="button" disabled={deleteSaving} onClick={() => setDeleting(null)} className="rounded-lg border border-edge px-4 py-2.5 text-xs text-mute disabled:opacity-40">取消</button><button type="button" onClick={() => void deleteSelected()} disabled={deleteSaving} className="inline-flex items-center gap-2 rounded-lg bg-rise px-4 py-2.5 text-xs font-semibold text-white disabled:opacity-40">{deleteSaving ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}{deleteSaving ? "正在删除…" : "确认删除"}</button></footer>
    </div></div>}
  </div>;
}
