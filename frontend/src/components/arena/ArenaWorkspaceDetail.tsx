import { useEffect, useRef, useState } from "react";
import { AlertCircle, ArrowLeft, CheckCircle2, Clock3, Loader2, RefreshCw, Square, XCircle } from "lucide-react";
import type { ArenaItem } from "../../types";
import { useStore } from "../../store";
import { cls } from "../../utils";
import ModelComparisonResults from "./ModelComparisonResults";
import { isModelComparisonResult } from "./arenaComparison";
import { arenaWorkspaceApi, workspaceConfig, workspaceIsRunning, workspaceProgress, workspaceStatusLabel } from "./arenaWorkspace";
import { ArenaFieldArt, ArenaSeal, arenaMatchNumber } from "./ArenaMotif";

const count = (value: unknown) => typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.floor(value) : null;

export default function ArenaWorkspaceDetail({ initialArena }: { initialArena: ArenaItem }) {
  const back = useStore((state) => state.backFromArenaDetail);
  const patchArena = useStore((state) => state.patchArena);
  const [arena, setArena] = useState(initialArena);
  const [refreshing, setRefreshing] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const cancellationRef = useRef(false);
  const id = initialArena.id;

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let lastStatus = initialArena.status;
    const controller = new AbortController();
    const read = async () => {
      if (disposed) return;
      setRefreshing(true);
      try {
        const response = await arenaWorkspaceApi.get(id, controller.signal);
        if (disposed || cancellationRef.current) return;
        lastStatus = response.status;
        setArena(response); patchArena(id, response); setError(null);
      } catch (reason) {
        if (!disposed && !controller.signal.aborted) setError(reason instanceof Error ? reason.message : "暂时无法更新状态，请刷新重试。");
      } finally {
        if (!disposed) {
          setRefreshing(false);
          if (workspaceIsRunning(lastStatus)) timer = setTimeout(() => void read(), 3000);
        }
      }
    };
    void read();
    return () => { disposed = true; controller.abort(); if (timer) clearTimeout(timer); };
  }, [id, revision, patchArena, initialArena.status]);

  const cancel = async () => {
    if (cancellationRef.current || !workspaceIsRunning(arena.status)) return;
    cancellationRef.current = true; setCancelling(true); setError(null);
    try {
      const response = await arenaWorkspaceApi.cancel(id);
      setArena(response); patchArena(id, response);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "取消未成功，请重试。"); }
    finally { cancellationRef.current = false; setCancelling(false); setRevision((value) => value + 1); }
  };

  const active = workspaceIsRunning(arena.status);
  const config = workspaceConfig(arena);
  const progress = workspaceProgress(arena);
  const models = Array.isArray(progress.models) ? progress.models : [];
  const result = isModelComparisonResult(arena.result) ? arena.result : null;
  const failure = arena.status === "failed";
  const track = result?.track ?? config.track;
  const participantLabel = track === "quant" ? "量化策略" : track === "event" ? "事件模型" : "模型";
  const targetName = config.target?.name || result?.subject.title || arena.dataset_name;
  const modelCount = config.model_ids?.length ?? models.length;
  return <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper">
    <header className="relative shrink-0 overflow-hidden border-b border-edgeDark/70 bg-[linear-gradient(110deg,#ffffff,#faf6ef)] px-5 py-4 sm:px-7"><ArenaFieldArt className="absolute -right-3 -top-24 hidden h-[280px] w-[450px] text-brand/10 lg:block" /><div className="relative flex flex-wrap items-center justify-between gap-4"><div className="min-w-0 flex-1"><div className="mb-3 flex flex-wrap items-center gap-4"><button type="button" onClick={back} className="inline-flex items-center gap-1.5 text-[11px] text-mute hover:text-ink"><ArrowLeft size={13} />Arena</button><span className="font-mono text-[9px] tracking-[.16em] text-faint">场次 · {arenaMatchNumber(arena.id)}</span>{track && <span className="rounded-full border border-edge bg-card/70 px-2 py-0.5 text-[9px] font-semibold tracking-[.12em] text-brand">{track === "quant" ? "QUANT" : "EVENT"}</span>}</div><div className="flex items-start gap-3"><ArenaSeal className="hidden h-11 w-11 text-brand/80 sm:block" /><div className="min-w-0"><div className="flex flex-wrap items-center gap-3"><h1 className="break-words font-serif text-[24px] font-semibold text-ink">{arena.name}</h1><span className={cls("inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[10px] font-medium", active ? "border-brand/15 bg-brand-soft text-brand" : failure ? "border-rise/15 bg-rise/5 text-rise" : arena.status === "done" ? "border-jade/15 bg-jade/5 text-jade" : "border-edge bg-paper text-mute")}>{active ? <Loader2 size={12} className="animate-spin" /> : arena.status === "done" ? <CheckCircle2 size={12} /> : failure ? <XCircle size={12} /> : <Clock3 size={12} />}{workspaceStatusLabel(arena.status)}</span></div><p className="mt-2 text-[11px] leading-relaxed text-mute">{modelCount} 个{participantLabel}{targetName ? ` · ${targetName}` : ""}{config.start_date && config.end_date ? ` · ${config.start_date} — ${config.end_date}` : ""}</p></div></div></div><div className="flex flex-wrap gap-2"><button type="button" disabled={refreshing || cancelling} onClick={() => setRevision((value) => value + 1)} className="inline-flex items-center gap-2 rounded-lg border border-edge bg-card/80 px-3 py-2 text-[12px] text-mute disabled:opacity-50"><RefreshCw size={14} className={cls(refreshing && "animate-spin")} />刷新</button>{active && <button type="button" disabled={cancelling} onClick={() => void cancel()} className="inline-flex items-center gap-2 rounded-lg border border-rise/25 bg-card/80 px-3 py-2 text-[12px] text-rise disabled:opacity-50">{cancelling ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} />}{cancelling ? "正在取消…" : "取消比较"}</button>}</div></div></header>
    <main className="min-h-0 min-w-0 flex-1 overflow-y-auto"><div className="mx-auto w-full max-w-[1400px] space-y-5 px-4 py-5 sm:px-7">
      {error && <div role="alert" className="flex items-start gap-2 rounded-xl border border-rise/20 bg-rise/5 px-4 py-3 text-[12px] leading-relaxed text-rise"><AlertCircle size={15} className="mt-0.5 shrink-0" /><p className="break-words">{error}</p></div>}
      <section className="rounded-2xl border border-edgeDark/70 bg-card p-4 sm:p-6"><div className="flex flex-wrap items-center justify-between gap-2"><div><p className="mb-1.5 text-[9px] tracking-[.22em] text-faint">THE CONTESTANTS</p><h2 className="font-serif text-[18px] font-semibold text-ink">{active ? "模型正在比较" : "本次比较进度"}</h2></div>{active && <span className="inline-flex items-center gap-1.5 text-[11px] text-jade"><span className="h-1.5 w-1.5 rounded-full bg-jade" />状态自动更新</span>}</div><p aria-live="polite" className="mt-2 break-words text-[12px] leading-relaxed text-mute">{progress.message || (active ? "依次准备相同输入、生成预测，再计算准确率与模拟收益。" : result ? "比较结果已自动保存，可随时从 Arena 查看。" : failure ? "本次比较未能完成，请查看各模型的错误原因。" : arena.status === "cancelled" ? "本次比较已取消，已完成的记录仍保留。" : "部分模型未完成，请查看下方进度。")}</p>
        {models.length > 0 && <div className="relative mt-5 grid gap-5 sm:grid-cols-2">{models.length === 2 && <span aria-hidden="true" className="absolute left-1/2 top-6 z-10 hidden h-8 w-8 -translate-x-1/2 items-center justify-center rounded-full border border-edge bg-card font-serif text-[13px] italic text-faint sm:flex">vs</span>}{models.map((model, index) => {
          const done = count(model.done); const total = count(model.total); const ratio = total !== null && total > 0 && done !== null ? Math.min(1, done / total) : null;
          const running = workspaceIsRunning(model.status); const failed = model.status === "failed";
          return <div key={model.id} className={cls("min-w-0 rounded-xl border p-4", index % 2 === 0 ? "border-brand/20 bg-brand-soft/15" : "border-jade/20 bg-jade-soft/15")}><div className="mb-3 flex items-center justify-between"><span className={cls("font-mono text-[10px] tracking-[.2em]", index % 2 === 0 ? "text-brand" : "text-jade")}>MODEL / {String(index + 1).padStart(2, "0")}</span><span className={cls("inline-flex shrink-0 items-center gap-1 text-[10px]", failed ? "text-rise" : model.status === "done" ? "text-jade" : "text-mute")}>{running && <Loader2 size={12} className="animate-spin" />}{workspaceStatusLabel(model.status)}</span></div><h3 className="min-w-0 break-words font-serif text-[16px] font-semibold text-ink">{model.name}</h3>{ratio !== null && <><div className="mt-4 h-1 overflow-hidden rounded-full bg-edge" role="progressbar" aria-label={`${model.name} 比较进度`} aria-valuemin={0} aria-valuemax={total!} aria-valuenow={Math.min(done!, total!)}><div className={cls("h-full rounded-full", failed ? "bg-rise/60" : index % 2 === 0 ? "bg-brand/70" : "bg-jade")} style={{ width: `${ratio * 100}%` }} /></div><p className="mt-2 font-mono text-[10px] text-mute">已完成 {done} / {total}</p></>}{model.error && <p className="mt-2 break-words text-[12px] leading-relaxed text-rise">{model.error}</p>}</div>;
        })}</div>}
        {active && <p className="mt-3 text-[11px] leading-relaxed text-faint">可以离开此页面，任务会在后台继续；模型 API 响应较慢时，进度可能暂时保持不变。</p>}
      </section>
      {result && result.models.length < 2 && <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-[12px] leading-relaxed text-amber-800">目前仅 {result.models.length} 个模型成功，暂不能进行模型之间的比较。下方保留已完成模型的数据；缺失结果不会按零分处理。</div>}
      {result ? <ModelComparisonResults result={result} /> : !active && <div className="rounded-xl border border-dashed border-edgeDark bg-card px-5 py-10 text-center"><AlertCircle size={22} className="mx-auto text-mute" /><h2 className="mt-3 text-[14px] font-semibold text-ink">暂无可比较的完整结果</h2><p className="mx-auto mt-2 max-w-xl text-[12px] leading-relaxed text-mute">至少两个模型成功完成预测后，才能进行模型之间的比较。成功部分会尽量保留；失败、缺失或取消的模型不会记为零分。</p><button type="button" onClick={back} className="mt-4 rounded-lg border border-edge px-4 py-2 text-[12px] text-mute">返回 Arena</button></div>}
    </div></main>
  </div>;
}
