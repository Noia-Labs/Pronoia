import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, Check, CheckCircle2, Database, Loader2, RefreshCw, Save, Search, X } from "lucide-react";
import type { ArenaItem } from "../../types";
import { cls } from "../../utils";
import { useStore } from "../../store";
import ModelComparisonResults from "./ModelComparisonResults";
import { arenaComparisonApi, comparisonKindLabel, type ComparisonCatalog, type ComparisonPreview, type ComparisonRequest, type ComparisonSubject } from "./arenaComparison";

const INPUT = "mt-1.5 w-full min-w-0 rounded-lg border border-edgeDark/70 bg-card px-3 py-2.5 text-[12px] text-ink outline-none focus:border-brand disabled:opacity-50";
const BUTTON = "inline-flex items-center justify-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[12px] font-medium text-mute transition hover:border-brand/40 hover:text-ink disabled:cursor-not-allowed disabled:opacity-45";

type Form = { subjectKey: string; runIds: string[]; sourceId: string; startDate: string; endDate: string; holdingBars: string; feeBps: string; slippageBps: string; initialCapital: string };
const emptyForm = (): Form => ({ subjectKey: "", runIds: [], sourceId: "", startDate: "", endDate: "", holdingBars: "3", feeBps: "3", slippageBps: "2", initialCapital: "100000" });

function previewRequest(form: Form): ComparisonRequest {
  return { subject_key: form.subjectKey, run_ids: form.runIds, market_source_id: form.sourceId, ...(form.startDate ? { start_date: form.startDate } : {}), ...(form.endDate ? { end_date: form.endDate } : {}), holding_bars: Number(form.holdingBars), fee_bps: Number(form.feeBps), slippage_bps: Number(form.slippageBps), initial_capital: Number(form.initialCapital), position_rule: "long_flat" };
}

export default function ArenaComparisonComposer({ open, onClose, onCreated }: { open: boolean; onClose: () => void; onCreated: (arena: ArenaItem) => void }) {
  const setView = useStore((state) => state.setView);
  const [catalog, setCatalog] = useState<ComparisonCatalog | null>(null);
  const [loading, setLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [kind, setKind] = useState<"event" | "asset">("event");
  const [query, setQuery] = useState("");
  const [form, setForm] = useState<Form>(emptyForm);
  const [name, setName] = useState("");
  const [preview, setPreview] = useState<ComparisonPreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [phase, setPhase] = useState<"idle" | "preview" | "save">("idle");
  const generation = useRef(0);
  const requestController = useRef<AbortController | null>(null);
  const savingRef = useRef(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const resultRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true); setCatalogError(null);
    void arenaComparisonApi.catalog(controller.signal).then((response) => {
      if (controller.signal.aborted) return;
      setCatalog(response);
      if (!response.subjects.some((subject) => subject.kind === "event") && response.subjects.some((subject) => subject.kind === "asset")) setKind("asset");
    }).catch((reason) => { if (!controller.signal.aborted) setCatalogError(reason instanceof Error ? reason.message : "无法读取可比较的事件和标的，请重试。"); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [open, revision]);

  useEffect(() => {
    if (!open) return;
    setForm(emptyForm()); setName(""); setQuery(""); setPreview(null); setError(null); setPhase("idle");
    const previousFocus = document.activeElement;
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !savingRef.current) { event.preventDefault(); closeRef.current(); }
      if (event.key !== "Tab") return;
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), select:not(:disabled), summary, a[href]") ?? []).filter((node) => node.offsetParent !== null);
      const first = controls[0]; const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first && last) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last && first) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown);
    dialogRef.current?.focus();
    return () => { generation.current += 1; requestController.current?.abort(); document.removeEventListener("keydown", keydown); if (previousFocus instanceof HTMLElement) previousFocus.focus(); };
  }, [open]);

  const subject = catalog?.subjects.find((item) => item.key === form.subjectKey) ?? null;
  const source = subject?.sources.find((item) => item.id === form.sourceId) ?? null;
  const subjects = useMemo(() => (catalog?.subjects ?? []).filter((item) => item.kind === kind && (!query.trim() || [item.title, item.symbol, item.market, item.event_time].filter(Boolean).join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))), [catalog, kind, query]);
  const candidates = useMemo(() => {
    if (!subject || !catalog) return [];
    const ids = new Set(subject.run_ids);
    return catalog.runs.filter((run) => ids.has(run.id) || run.subject_keys.includes(subject.key));
  }, [catalog, subject]);
  const eligible = candidates.filter((run) => run.eligible);
  const busy = phase !== "idle";
  const numericRulesValid = form.holdingBars.trim() !== "" && Number.isInteger(Number(form.holdingBars)) && Number(form.holdingBars) >= 1 && Number(form.holdingBars) <= 1000
    && form.feeBps.trim() !== "" && Number.isFinite(Number(form.feeBps)) && Number(form.feeBps) >= 0 && Number(form.feeBps) <= 1000
    && form.slippageBps.trim() !== "" && Number.isFinite(Number(form.slippageBps)) && Number(form.slippageBps) >= 0 && Number(form.slippageBps) <= 1000
    && form.initialCapital.trim() !== "" && Number.isFinite(Number(form.initialCapital)) && Number(form.initialCapital) > 0 && Number(form.initialCapital) <= 1e12;
  const datesValid = !form.startDate || !form.endDate || form.startDate <= form.endDate;
  const selectionValid = form.runIds.length >= 2 && form.runIds.length <= 8 && form.runIds.every((id) => eligible.some((run) => run.id === id));
  const canPreview = !busy && Boolean(subject && source && selectionValid && numericRulesValid && datesValid);

  const invalidate = () => { generation.current += 1; requestController.current?.abort(); setPreview(null); setError(null); setPhase("idle"); };
  const update = (patch: Partial<Form>) => { invalidate(); setForm((current) => ({ ...current, ...patch })); };
  const selectSubject = (next: ComparisonSubject) => {
    update({ subjectKey: next.key, runIds: [], sourceId: next.sources[0]?.id ?? "", startDate: "", endDate: "" });
    const suffix = " · 模型对比";
    setName(`${next.title.slice(0, 120 - suffix.length)}${suffix}`);
  };
  const changeKind = (next: "event" | "asset") => { setKind(next); setQuery(""); update({ subjectKey: "", runIds: [], sourceId: "", startDate: "", endDate: "" }); };
  const goBacktest = () => { onClose(); setView("backtest-list"); };
  const goData = () => { onClose(); setView("backtest-data"); window.history.replaceState(window.history.state, "", "/backtest/data?tab=market"); };

  const runPreview = async () => {
    if (!canPreview) return;
    const requestId = ++generation.current;
    const controller = new AbortController(); requestController.current = controller;
    setPhase("preview"); setError(null); setPreview(null);
    try {
      const response = await arenaComparisonApi.preview(previewRequest(form), controller.signal);
      if (requestId !== generation.current) return;
      setPreview(response);
      window.requestAnimationFrame(() => resultRef.current?.scrollIntoView({ behavior: "smooth", block: "start" }));
    } catch (reason) { if (requestId === generation.current && !controller.signal.aborted) setError(reason instanceof Error ? reason.message : "无法生成比较预览，请检查所选行情与覆盖范围。"); }
    finally { if (requestId === generation.current) setPhase("idle"); }
  };

  const save = async () => {
    if (savingRef.current || busy || !preview || !name.trim() || name.trim().length > 120) return;
    savingRef.current = true; setPhase("save"); setError(null);
    try { const arena = await arenaComparisonApi.save(name.trim(), preview.preview_id); onCreated(arena); }
    catch (reason) { setError(reason instanceof Error ? reason.message : "保存失败，请重试。"); }
    finally { savingRef.current = false; setPhase("idle"); }
  };

  if (!open) return null;
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-3 backdrop-blur-sm sm:p-5" role="dialog" aria-modal="true" aria-labelledby="arena-comparison-title" ref={dialogRef} tabIndex={-1}>
    <div className="flex max-h-[94vh] w-full max-w-[1120px] min-w-0 flex-col overflow-hidden rounded-2xl border border-edge bg-paper shadow-pop">
      <header className="flex shrink-0 items-start justify-between gap-4 border-b border-edge bg-card px-5 py-4"><div><h2 id="arena-comparison-title" className="font-serif text-[21px] font-semibold text-ink">新建模型对比</h2><p className="mt-1.5 text-[12px] leading-relaxed text-mute">选同一事件或指数，再选择模型记录，用同一份行情和模拟规则比较。</p></div><button type="button" aria-label="关闭模型对比" disabled={phase === "save"} onClick={onClose} className="rounded-lg p-2 text-mute hover:bg-edge disabled:opacity-40"><X size={18} /></button></header>
      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6">
        {loading ? <div className="flex min-h-56 items-center justify-center gap-2 text-[12px] text-mute"><Loader2 size={17} className="animate-spin" />正在读取可比较的对象与模型记录…</div> : catalogError ? <div role="alert" className="rounded-xl border border-rise/20 bg-rise/5 p-5 text-[12px] text-rise"><p className="break-words">{catalogError}</p><button type="button" onClick={() => setRevision((value) => value + 1)} className={cls(BUTTON, "mt-3")}><RefreshCw size={13} />重新读取</button></div> : <>
          <fieldset disabled={busy} className="min-w-0 space-y-5">
            <section className="rounded-xl border border-edge bg-card p-4 sm:p-5"><h3 className="text-[13px] font-semibold text-ink">1. 选择比较对象</h3>
              <div className="mt-3 flex flex-wrap gap-2"><button type="button" onClick={() => changeKind("event")} className={cls(BUTTON, kind === "event" && "border-brand/40 bg-brand-soft/35 text-brand")}>同一事件</button><button type="button" onClick={() => changeKind("asset")} className={cls(BUTTON, kind === "asset" && "border-brand/40 bg-brand-soft/35 text-brand")}>同一指数 / 标的</button></div>
              <label className="relative mt-3 block"><Search size={14} className="absolute left-3 top-3 text-faint" /><input aria-label="搜索比较对象" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={kind === "event" ? "搜索事件标题、代码或时间" : "搜索指数名称或代码"} className={cls(INPUT, "mt-0 pl-9")} /></label>
              <div className="mt-3 max-h-64 space-y-2 overflow-y-auto">{subjects.slice(0, 100).map((item) => <button type="button" key={item.key} onClick={() => selectSubject(item)} className={cls("flex w-full items-start justify-between gap-3 rounded-lg border px-3 py-3 text-left", subject?.key === item.key ? "border-brand/35 bg-brand-soft/30" : "border-edge bg-paper hover:border-edgeDark")}><span className="min-w-0"><span className="block break-words text-[12px] font-semibold text-ink">{item.title}</span><span className="mt-1 block text-[11px] text-mute">{item.symbol} · {item.market}{item.event_time ? ` · ${item.event_time.replace("T", " ")}` : ""}</span></span><span className="inline-flex shrink-0 items-center gap-2 text-[11px] text-mute">{item.run_ids.length} 条记录{subject?.key === item.key && <CheckCircle2 size={14} className="text-brand" />}</span></button>)}</div>
              {subjects.length > 100 && <p className="mt-2 text-[11px] text-mute">已展示前 100 个对象，请搜索缩小范围。</p>}
              {subjects.length === 0 && <div className="py-5 text-center text-[12px] text-mute"><p>{query.trim() ? "没有匹配的事件或标的。" : "尚未找到可比较的对象。先在回测完成该事件或标的的模型测试。"}</p><button type="button" onClick={goBacktest} className={cls(BUTTON, "mt-3")}>前往回测 <ArrowRight size={13} /></button></div>}
            </section>

            {subject && <section className="rounded-xl border border-edge bg-card p-4 sm:p-5"><div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-[13px] font-semibold text-ink">2. 选择模型记录</h3><button type="button" disabled={!eligible.length} onClick={() => update({ runIds: eligible.slice(0, 8).map((run) => run.id) })} className="text-[12px] font-medium text-brand disabled:opacity-40">{eligible.length > 8 ? "选择前 8 条可用记录" : "选择全部可用记录"}</button></div><p className="mt-1.5 text-[12px] leading-relaxed text-mute">每次选择 2–8 条记录。事件模型和量化模型可在同一对象上使用统一规则模拟。</p>
              <div className="mt-3 grid gap-2 sm:grid-cols-2">{candidates.map((run) => <label key={run.id} className={cls("flex min-w-0 items-start gap-3 rounded-lg border px-3 py-3", (!run.eligible || (form.runIds.length >= 8 && !form.runIds.includes(run.id))) ? "cursor-not-allowed border-edge bg-paper opacity-65" : form.runIds.includes(run.id) ? "cursor-pointer border-brand/35 bg-brand-soft/30" : "cursor-pointer border-edge bg-card")}><input type="checkbox" className="mt-1 accent-brand" disabled={!run.eligible || (form.runIds.length >= 8 && !form.runIds.includes(run.id))} checked={form.runIds.includes(run.id)} onChange={() => update({ runIds: form.runIds.includes(run.id) ? form.runIds.filter((id) => id !== run.id) : [...form.runIds, run.id] })} /><span className="min-w-0"><span className="block break-words text-[12px] font-semibold text-ink">{run.model_name || run.name}</span><span className="mt-1 block break-words text-[11px] text-mute">{comparisonKindLabel(run.model_kind)} · {run.name}</span>{run.reason && <span className="mt-1.5 block break-words text-[11px] leading-relaxed text-amber-700">{run.reason}</span>}</span></label>)}</div>
              {form.runIds.length >= 8 && <p className="mt-2 text-[12px] text-amber-700">已选满 8 条记录；如需换选，请先取消一条。</p>}
              {eligible.length < 2 && <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 px-3 py-3 text-[12px] leading-relaxed text-amber-800"><p>当前对象少于两条可用模型记录。先在回测完成该事件或标的的模型测试，再回来比较。</p><button type="button" onClick={goBacktest} className="mt-2 inline-flex items-center gap-1 font-semibold">前往回测 <ArrowRight size={13} /></button></div>}
            </section>}

            {subject && <section className="rounded-xl border border-edge bg-card p-4 sm:p-5"><h3 className="text-[13px] font-semibold text-ink">3. 选择共同行情与模拟规则</h3>
              <label className="mt-3 block text-[12px] font-medium text-ink">共同行情来源<select value={form.sourceId} onChange={(event) => update({ sourceId: event.target.value, startDate: "", endDate: "" })} className={INPUT}><option value="">请选择行情来源</option>{subject.sources.map((item) => <option key={item.id} value={item.id}>{item.label}{item.frequency ? ` · ${item.frequency}` : ""}</option>)}</select></label>
              {source && <p className="mt-2 text-[11px] leading-relaxed text-mute">{source.start_at && source.end_at ? `已保存范围：${source.start_at.replace("T", " ")} → ${source.end_at.replace("T", " ")}` : "预览时读取所选来源的真实 OHLC 行情。"}</p>}
              {subject.kind === "event" && <p className="mt-2 text-[12px] leading-relaxed text-mute">事件自动行情在预览时获取。若获取失败，可改选已保存的行情，或先导入本地行情文件。</p>}
              <button type="button" onClick={goData} className="mt-2 inline-flex items-center gap-1.5 text-[12px] font-medium text-brand"><Database size={13} />前往数据管理导入行情</button>
              <div className="mt-4 grid gap-3 sm:grid-cols-2"><label className="text-[12px] text-mute">开始日期（可选）<input type="date" value={form.startDate} onChange={(event) => update({ startDate: event.target.value })} className={INPUT} /></label><label className="text-[12px] text-mute">结束日期（可选）<input type="date" value={form.endDate} onChange={(event) => update({ endDate: event.target.value })} className={INPUT} /></label></div><p className="mt-1.5 text-[11px] text-faint">留空使用所选来源的可用区间；覆盖不足会在预览中说明。</p>
              <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><label className="text-[12px] text-mute">持有周期（K 线根数）<input type="number" min={1} max={1000} step={1} value={form.holdingBars} onChange={(event) => update({ holdingBars: event.target.value })} className={INPUT} /></label><label className="text-[12px] text-mute">单边手续费（bps）<input type="number" min={0} max={1000} step="any" value={form.feeBps} onChange={(event) => update({ feeBps: event.target.value })} className={INPUT} /></label><label className="text-[12px] text-mute">单边滑点（bps）<input type="number" min={0} max={1000} step="any" value={form.slippageBps} onChange={(event) => update({ slippageBps: event.target.value })} className={INPUT} /></label><label className="text-[12px] text-mute">初始资金<input type="number" min={0} max={1e12} step="any" value={form.initialCapital} onChange={(event) => update({ initialCapital: event.target.value })} className={INPUT} /></label></div>
              <div className="mt-4 rounded-lg border border-brand/20 bg-brand-soft/20 px-3 py-3 text-[12px] leading-relaxed text-mute"><p className="font-semibold text-ink">统一做多 / 空仓 · 下一根开盘执行</p><p className="mt-1">正收益预测或看涨方向做多，其余有效信号空仓。新预测更新方向并重计持有期；无新预测时保持至到期。每次买卖按单边手续费和滑点扣费，1 bps = 0.01%。</p></div>
              {(!numericRulesValid || !datesValid) && <p className="mt-3 text-[12px] text-rise">{!datesValid ? "结束日期不能早于开始日期。" : "持有周期须为 1–1000 的整数；手续费和滑点须在 0–1000 bps；资金须大于 0 且不超过 1 万亿。"}</p>}
            </section>}
          </fieldset>

          {error && <div role="alert" className="mt-4 flex min-w-0 items-start gap-2 rounded-xl border border-rise/20 bg-rise/5 px-4 py-3 text-[12px] leading-relaxed text-rise"><AlertTriangle size={15} className="mt-0.5 shrink-0" /><div className="min-w-0"><p className="break-words">{error}</p>{phase === "idle" && !preview && <button type="button" onClick={goData} className="mt-2 font-semibold underline underline-offset-2">检查或导入共同行情</button>}</div></div>}
          <div ref={resultRef} className="mt-5 scroll-mt-4">{preview && <ModelComparisonResults result={preview.result} preview />}</div>
          {preview && <label className="mt-5 block rounded-xl border border-edge bg-card p-4 text-[12px] font-semibold text-ink">保存名称<input value={name} maxLength={120} disabled={phase === "save"} onChange={(event) => setName(event.target.value)} className={INPUT} placeholder="为本次比较命名" /><span className="mt-2 block text-[11px] font-normal text-mute">名称最多 120 字。保存当前预览的结果与规则，之后可从 Arena 列表查看。</span></label>}
        </>}
      </main>
      <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-edge bg-card px-5 py-3.5"><p className="text-[11px] leading-relaxed text-mute">{preview ? "更改对象、记录或规则后，需要重新预览。" : `${form.runIds.length} 条记录已选 · 预览使用已有预测，不重新调用模型。`}</p><div className="flex flex-wrap gap-2"><button type="button" disabled={phase === "save"} onClick={onClose} className={BUTTON}>取消</button>{preview && <button type="button" onClick={() => void runPreview()} disabled={!canPreview} className={BUTTON}><RefreshCw size={13} />重新预览</button>}{preview ? <button type="button" onClick={() => void save()} disabled={busy || !name.trim() || name.trim().length > 120} className="inline-flex items-center gap-2 rounded-lg bg-ink px-4 py-2.5 text-[12px] font-semibold text-card disabled:opacity-40">{phase === "save" ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}{phase === "save" ? "正在保存…" : "保存到 Arena"}</button> : <button type="button" onClick={() => void runPreview()} disabled={!canPreview || loading || Boolean(catalogError)} className="inline-flex items-center gap-2 rounded-lg bg-ink px-4 py-2.5 text-[12px] font-semibold text-card disabled:opacity-40">{phase === "preview" ? <Loader2 size={14} className="animate-spin" /> : <Check size={14} />}{phase === "preview" ? "正在生成预览…" : "生成比较预览"}</button>}</div></footer>
    </div>
  </div>;
}
