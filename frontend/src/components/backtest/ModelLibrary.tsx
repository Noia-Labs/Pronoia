import { useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, ArrowRight, CheckCircle2, History, Loader2, Pencil, Play, RefreshCw, Search, Settings2, Swords, Trash2, X } from "lucide-react";
import { cls, relTime } from "../../utils";
import { modelLibraryApi, modelKindLabel, type ModelCategory, type SavedModelEntry } from "./modelLibraryApi";

function configSummary(model: SavedModelEntry): string {
  const spec = model.setup?.strategy_spec;
  const params = spec?.parameters ?? {};
  if (spec?.kind === "return_forecast") return `历史窗口 ${params.lookback ?? "未设置"} 个周期 · 测试时选择预测周期`;
  if (spec?.kind === "declarative_rules") {
    const entry = params.entry as { conditions?: unknown[] } | undefined; const exit = params.exit as { conditions?: unknown[] } | undefined;
    return `${entry?.conditions?.length ?? 0} 条开仓条件 · ${exit?.conditions?.length ?? 0} 条平仓条件`;
  }
  return model.description || (spec?.kind ? String(spec.kind) : modelKindLabel(model.kind));
}

export default function ModelLibrary({ category, revision, highlightedModelId, onTest, onAdd, onEdit }: { category: ModelCategory; revision: number; highlightedModelId?: string; onTest: (model: SavedModelEntry) => Promise<void>; onAdd: () => void; onEdit: (model: SavedModelEntry) => void }) {
  const cards = useRef(new Map<string, HTMLElement>());
  const lastLocated = useRef("");
  const [items, setItems] = useState<SavedModelEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const [refresh, setRefresh] = useState(0);
  const [renaming, setRenaming] = useState<SavedModelEntry | null>(null);
  const [renameText, setRenameText] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [renameSaving, setRenameSaving] = useState(false);
  const renameLock = useRef(false);
  const [starting, setStarting] = useState<string | null>(null);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [deleting, setDeleting] = useState<SavedModelEntry[] | null>(null);
  const [deleteSaving, setDeleteSaving] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const deleteLock = useRef(false);
  const deleteDialog = useRef<HTMLDivElement>(null);
  const selectAllInput = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(null);
    void modelLibraryApi.list(category, controller.signal).then((response) => {
      if (controller.signal.aborted) return;
      const next = response.items ?? []; setItems(next);
      setSelectedIds((current) => new Set([...current].filter((id) => next.some((model) => model.id === id))));
    }).catch((reason) => { if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "无法读取模型列表"); }).finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [category, revision, refresh]);
  const filtered = useMemo(() => items.filter((model) => (filter === "all" || model.kind === filter) && [model.name, model.description, modelKindLabel(model.kind), configSummary(model)].filter(Boolean).join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())), [items, query, filter]);
  useEffect(() => {
    if (highlightedModelId) { setQuery(""); setFilter("all"); }
  }, [highlightedModelId, revision]);
  useEffect(() => {
    const locationKey = `${revision}:${highlightedModelId}`;
    if (!highlightedModelId || loading || lastLocated.current === locationKey || query || filter !== "all") return;
    const card = cards.current.get(highlightedModelId);
    if (!card) return;
    const frame = requestAnimationFrame(() => {
      card.scrollIntoView({ block: "center", behavior: "smooth" });
      lastLocated.current = locationKey;
    });
    return () => cancelAnimationFrame(frame);
  }, [highlightedModelId, revision, loading, items, query, filter]);
  const kinds = [...new Set(items.map((model) => model.kind))];
  useEffect(() => {
    if (!loading && filter !== "all" && !items.some((model) => model.kind === filter)) setFilter("all");
  }, [items, filter, loading]);
  const selected = items.filter((model) => selectedIds.has(model.id));
  const allVisibleSelected = filtered.length > 0 && filtered.every((model) => selectedIds.has(model.id));
  const someVisibleSelected = filtered.some((model) => selectedIds.has(model.id));
  const selectionBusy = loading || Boolean(starting) || deleteSaving;
  useEffect(() => { if (selectAllInput.current) selectAllInput.current.indeterminate = someVisibleSelected && !allVisibleSelected; }, [someVisibleSelected, allVisibleSelected]);
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
    return () => { document.removeEventListener("keydown", keydown); if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus(); };
  }, [deleting]);
  const toggleSelection = (id: string) => setSelectedIds((current) => {
    const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next;
  });
  const deleteModels = async () => {
    if (!deleting?.length || deleteLock.current) return;
    deleteLock.current = true; setDeleteSaving(true); setDeleteError(null);
    try {
      const response = await modelLibraryApi.remove(deleting.map((model) => model.id));
      const removed = new Set(response.deleted_ids);
      setItems((current) => current.filter((model) => !removed.has(model.id)));
      setSelectedIds((current) => new Set([...current].filter((id) => !removed.has(id))));
      setNotice(`已删除 ${removed.size} 个模型，历史测试和 API 连接已保留。`);
      setDeleting(null); setRefresh((value) => value + 1);
    } catch (reason) { setDeleteError(reason instanceof Error ? reason.message : "删除未完成，请重试。"); }
    finally { deleteLock.current = false; setDeleteSaving(false); }
  };
  const start = async (model: SavedModelEntry) => { if (starting) return; setStarting(model.id); setError(null); try { await onTest(model); } catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取模型设置"); } finally { setStarting(null); } };
  const saveName = async () => {
    if (!renaming || !renameText.trim() || renameText.trim().length > 120 || renameLock.current) return;
    renameLock.current = true; setRenameSaving(true); setRenameError(null);
    try { const model = await modelLibraryApi.rename(renaming.id, renameText.trim()); setItems((current) => current.map((item) => item.id === model.id ? model : item)); setRenaming(null); }
    catch (reason) { setRenameError(reason instanceof Error ? reason.message : "名称保存失败，请重试。"); }
    finally { renameLock.current = false; setRenameSaving(false); }
  };
  return <section aria-label={category === "event" ? "已保存事件模型" : "已保存量化模型"} className="space-y-4">
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-edge bg-card p-3"><label className="relative min-w-0 flex-1 sm:max-w-sm"><Search size={14} className="absolute left-3 top-3 text-faint" /><input value={query} onChange={(event) => setQuery(event.target.value)} aria-label="搜索模型" placeholder="搜索模型名称或配置" className="w-full rounded-lg border border-edge bg-paper py-2.5 pl-9 pr-3 text-[12px] text-ink outline-none focus:border-brand" /></label><div className="flex flex-wrap items-center gap-2"><span className="text-[12px] text-mute">{items.length} 个模型</span><button type="button" aria-label="刷新模型列表" onClick={() => setRefresh((value) => value + 1)} disabled={loading || deleteSaving} className="rounded-lg border border-edge p-2 text-mute disabled:opacity-40"><RefreshCw size={14} className={cls(loading && "animate-spin")} /></button></div></div>
    {kinds.length > 1 && <div className="flex flex-wrap gap-2" role="group" aria-label="模型分类">{["all", ...kinds].map((kind) => <button type="button" key={kind} onClick={() => setFilter(kind)} aria-pressed={filter === kind} className={cls("rounded-lg border px-3 py-2 text-[11px] font-medium", filter === kind ? "border-ink bg-ink text-card" : "border-edge bg-card text-mute")}>{kind === "all" ? "全部模型" : modelKindLabel(kind)}</button>)}</div>}
    {items.length > 0 && <div className="flex flex-wrap items-center justify-between gap-3 rounded-xl border border-edge bg-card px-4 py-3">
      <div className="flex flex-wrap items-center gap-4 text-[12px]">
        <label className="flex cursor-pointer items-center gap-2 text-ink"><input ref={selectAllInput} type="checkbox" checked={allVisibleSelected} disabled={selectionBusy || filtered.length === 0} onChange={() => setSelectedIds((current) => {
          const next = new Set(current); filtered.forEach((model) => allVisibleSelected ? next.delete(model.id) : next.add(model.id)); return next;
        })} className="h-4 w-4 cursor-pointer rounded accent-brand disabled:cursor-not-allowed" />全选当前结果</label>
        <span aria-live="polite" className="text-mute">已选 {selected.length} 个模型{selected.some((model) => !filtered.some((item) => item.id === model.id)) ? "（包含其他筛选下的选择）" : ""}</span>
        {selected.length > 0 && <button type="button" disabled={selectionBusy} onClick={() => setSelectedIds(new Set())} className="text-mute underline underline-offset-4 disabled:opacity-40">清空选择</button>}
      </div>
      <button type="button" disabled={!selected.length || selectionBusy} onClick={() => { setDeleting(selected); setDeleteError(null); setNotice(null); }} className="inline-flex items-center gap-2 rounded-lg border border-rise/25 bg-rise/5 px-3 py-2 text-[12px] font-medium text-rise hover:bg-rise/10 disabled:opacity-35"><Trash2 size={14} />删除所选{selected.length ? `（${selected.length}）` : ""}</button>
    </div>}
    {notice && <div role="status" className="flex items-center justify-between gap-3 rounded-xl border border-jade/20 bg-jade-soft/25 px-4 py-3 text-[12px] text-jade"><p className="flex items-center gap-2"><CheckCircle2 size={15} />{notice}</p><button type="button" onClick={() => setNotice(null)} aria-label="关闭删除提示" className="rounded-lg p-1 text-mute"><X size={14} /></button></div>}
    {error && <div role="alert" className="flex items-start gap-2 rounded-xl border border-rise/20 bg-rise/5 p-4 text-[12px] text-rise"><AlertCircle size={15} className="mt-0.5 shrink-0" /><p className="break-words">{error}</p></div>}
    {loading && !items.length ? <div className="flex min-h-64 items-center justify-center gap-2 text-[12px] text-mute"><Loader2 size={17} className="animate-spin" />正在读取模型…</div> : filtered.length ? <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">{filtered.map((model) => {
      const canTest = model.can_test ?? model.available;
      const runCount = model.run_count ?? model.run_ids.length;
      const batchCount = model.batch_ids?.length ?? 0;
      return <article key={model.id} data-model-id={model.id} data-highlighted={highlightedModelId === model.id || undefined} ref={(node) => { if (node) cards.current.set(model.id, node); else cards.current.delete(model.id); }} className={cls("flex min-w-0 flex-col rounded-xl border bg-card p-4 shadow-card transition", highlightedModelId === model.id ? "border-jade/60 ring-2 ring-jade/25" : selectedIds.has(model.id) ? "border-brand/60 ring-1 ring-brand/20" : "border-edge")}><div className="flex items-start justify-between gap-2"><label className="flex cursor-pointer items-center gap-2"><input type="checkbox" checked={selectedIds.has(model.id)} disabled={selectionBusy} onChange={() => toggleSelection(model.id)} aria-label={`选择模型 ${model.name}`} className="h-4 w-4 shrink-0 cursor-pointer rounded accent-brand disabled:cursor-not-allowed" /><span className="rounded-full bg-paper px-2.5 py-1 text-[10px] font-semibold text-mute">{modelKindLabel(model.kind)}</span></label><span className={cls("shrink-0 text-[10px]", canTest ? "text-jade" : "text-amber-700")}>{canTest ? "可发起测试" : "配置待完成"}</span></div><div className="mt-3 flex items-start justify-between gap-2"><h2 className="min-w-0 break-words font-serif text-[18px] font-semibold text-ink">{model.name}</h2><button type="button" aria-label={`重命名模型 ${model.name}`} onClick={() => { setRenaming(model); setRenameText(model.name); setRenameError(null); }} className="shrink-0 rounded-lg p-1.5 text-faint hover:bg-paper hover:text-ink"><Pencil size={13} /></button></div><p className="mt-2 break-words text-[12px] leading-relaxed text-mute">{configSummary(model)}</p>{model.reason && <p className="mt-2 break-words text-[11px] leading-relaxed text-amber-700">{model.reason}</p>}
        {model.setup.strategy_spec && <details className="mt-3 rounded-lg border border-edge bg-paper/50 px-3 py-2 text-[11px] text-mute"><summary className="cursor-pointer">高级：查看完整配置</summary><pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-all text-[10px] leading-relaxed">{JSON.stringify(model.setup.strategy_spec, null, 2)}</pre></details>}
        <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-edge pt-3 text-[11px] text-mute"><span>{runCount} 条运行 · {batchCount} 个评测批次</span><span>{model.latest_run ? `最近运行 ${relTime(model.latest_run.created_at)}` : batchCount > 0 ? "已有评测记录" : runCount > 0 ? "已有运行记录" : "尚无测试记录"}</span></div>
        <div className="mt-auto flex flex-wrap items-center gap-2 pt-4"><button type="button" onClick={() => void start(model)} disabled={!canTest || Boolean(starting) || loading || deleteSaving} className="inline-flex items-center gap-1.5 rounded-lg bg-ink px-3 py-2 text-[11px] font-semibold text-card disabled:opacity-40">{starting === model.id ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}发起测试</button><a href={`/backtest/runs?model=${encodeURIComponent(model.id)}`} className="inline-flex items-center gap-1.5 rounded-lg border border-edge px-3 py-2 text-[11px] text-mute"><History size={12} />查看历史</a><button type="button" onClick={() => onEdit(model)} disabled={Boolean(starting) || loading || deleteSaving} aria-label={`修改配置 ${model.name}`} className="inline-flex items-center gap-1.5 rounded-lg border border-edge px-3 py-2 text-[11px] text-mute hover:border-edgeDark hover:text-ink disabled:opacity-40"><Settings2 size={12} />修改配置</button><a href={`/arena?models=${encodeURIComponent(model.id)}`} className="inline-flex items-center gap-1.5 rounded-lg border border-edge px-3 py-2 text-[11px] text-mute"><Swords size={12} />去 Arena</a></div>
      </article>;
    })}</div> : !loading && !error && <div className="rounded-2xl border border-dashed border-edgeDark bg-card px-6 py-14 text-center"><h2 className="font-serif text-[19px] font-semibold text-ink">{query || filter !== "all" ? "没有匹配的模型" : "先添加一个模型"}</h2><p className="mx-auto mt-2 max-w-lg text-[12px] leading-relaxed text-mute">{query || filter !== "all" ? "调整搜索或分类后重试。" : "保存模型 API 或量化规则后，就可以对不同对象发起测试。无需先创建实验。"}</p>{!query && filter === "all" && <button type="button" onClick={onAdd} className="mt-4 inline-flex items-center gap-2 rounded-lg bg-ink px-4 py-2 text-[12px] font-semibold text-card">添加模型 <ArrowRight size={13} /></button>}</div>}
    {renaming && <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-4 backdrop-blur-sm"><div role="dialog" aria-modal="true" aria-labelledby="rename-model-title" className="w-full max-w-md rounded-2xl border border-edge bg-card p-5 shadow-pop"><div className="flex items-center justify-between gap-3"><h2 id="rename-model-title" className="font-serif text-[20px] font-semibold text-ink">重命名模型</h2><button type="button" disabled={renameSaving} onClick={() => setRenaming(null)} aria-label="关闭重命名模型" className="rounded-lg p-1.5 text-mute"><X size={17} /></button></div><label className="mt-4 block text-[12px] text-mute">模型名称<input autoFocus value={renameText} maxLength={120} disabled={renameSaving} onChange={(event) => setRenameText(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void saveName(); if (event.key === "Escape" && !renameSaving) setRenaming(null); }} className="mt-1.5 w-full rounded-lg border border-edgeDark bg-paper px-3 py-2.5 text-[13px] text-ink outline-none focus:border-brand" /></label><p className="mt-2 text-[11px] leading-relaxed text-mute">仅修改模型列表中的名称，已有实验名称与结果保持不变。</p>{renameError && <p role="alert" className="mt-3 text-[12px] text-rise">{renameError}</p>}<div className="mt-5 flex justify-end gap-2"><button type="button" disabled={renameSaving} onClick={() => setRenaming(null)} className="rounded-lg border border-edge px-3 py-2 text-[12px] text-mute">取消</button><button type="button" onClick={() => void saveName()} disabled={renameSaving || !renameText.trim()} className="inline-flex items-center gap-2 rounded-lg bg-ink px-4 py-2 text-[12px] font-semibold text-card disabled:opacity-40">{renameSaving && <Loader2 size={13} className="animate-spin" />}保存名称</button></div></div></div>}
    {deleting && <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-4 backdrop-blur-sm"><div ref={deleteDialog} role="dialog" aria-modal="true" aria-labelledby="delete-model-title" aria-describedby="delete-model-description" className="flex max-h-[85dvh] w-full max-w-lg flex-col overflow-hidden rounded-2xl border border-edge bg-card shadow-pop">
      <header className="flex items-start justify-between gap-3 border-b border-edge px-5 py-4"><div><div className="mb-3 inline-flex rounded-xl bg-rise/5 p-2.5 text-rise"><Trash2 size={20} /></div><h2 id="delete-model-title" className="font-serif text-[22px] font-semibold text-ink">删除这 {deleting.length} 个模型？</h2><p id="delete-model-description" className="mt-2 text-[12px] leading-relaxed text-mute">确认后，它们会从模型列表和 Arena 的可选模型中移除。</p></div><button type="button" disabled={deleteSaving} onClick={() => setDeleting(null)} aria-label="关闭删除确认" className="rounded-lg p-2 text-mute disabled:opacity-40"><X size={17} /></button></header>
      <div className="min-h-0 overflow-y-auto p-5"><ul className="divide-y divide-edge overflow-hidden rounded-xl border border-edge bg-paper/50">{deleting.map((model) => <li key={model.id} className="px-4 py-3"><p className="break-words text-[13px] font-medium text-ink">{model.name}</p><p className="mt-1 text-[11px] text-mute">{modelKindLabel(model.kind)} · {model.run_count ?? model.run_ids.length} 条运行 · {model.batch_ids?.length ?? 0} 个评测批次</p></li>)}</ul><p className="mt-4 text-[12px] leading-relaxed text-mute">已有测试记录、结果及 API 连接会保留，平台统一基模也不会改变。需要再次使用时，可以重新添加模型。</p>{deleteError && <p role="alert" className="mt-4 rounded-lg border border-rise/20 bg-rise/5 p-3 text-[12px] text-rise">{deleteError}</p>}</div>
      <footer className="flex justify-end gap-2 border-t border-edge px-5 py-4"><button data-cancel-delete type="button" disabled={deleteSaving} onClick={() => setDeleting(null)} className="rounded-lg border border-edge px-4 py-2.5 text-[12px] text-mute disabled:opacity-40">取消</button><button type="button" onClick={() => void deleteModels()} disabled={deleteSaving} className="inline-flex items-center gap-2 rounded-lg bg-rise px-4 py-2.5 text-[12px] font-semibold text-white disabled:opacity-40">{deleteSaving ? <Loader2 size={14} className="animate-spin" /> : <Trash2 size={14} />}{deleteSaving ? "正在删除…" : "确认删除"}</button></footer>
    </div></div>}
  </section>;
}
