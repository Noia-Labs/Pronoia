import { useEffect, useMemo, useState } from "react";
import { Clock3, RefreshCw, SlidersHorizontal, X } from "lucide-react";
import BacktestList from "../components/BacktestList";
import UnifiedRunHistory from "../components/backtest/UnifiedRunHistory";
import BacktestNav from "../components/backtest/BacktestNav";
import { cls } from "../utils";

type ModelScope = { id: string; name: string; run_ids?: string[]; batch_ids?: string[] };

export default function BacktestRunsPage() {
  const [tab, setTab] = useState<"history" | "metrics">("history");
  const [refreshVersion, setRefreshVersion] = useState(0);
  const [modelId, setModelId] = useState(() => new URLSearchParams(window.location.search).get("model") || "");
  const [model, setModel] = useState<ModelScope | null>(null);
  const [scopeLoading, setScopeLoading] = useState(Boolean(modelId));
  const [scopeError, setScopeError] = useState("");
  useEffect(() => {
    let stale = false;
    if (!modelId) { setModel(null); setScopeLoading(false); setScopeError(""); return; }
    setScopeLoading(true);
    void fetch("/api/bt/models").then(async (response) => {
      if (!response.ok) throw new Error("暂时无法读取模型，请刷新重试。");
      return response.json() as Promise<{ items: ModelScope[] }>;
    }).then(({ items }) => {
      if (stale) return;
      const found = items.find((item) => item.id === modelId);
      setModel(found ?? null);
      setScopeError(found ? "" : "未找到这个模型，可能已经移除。可以清除筛选查看全部记录。");
    }).catch((error) => { if (!stale) setScopeError(error instanceof Error ? error.message : String(error)); })
      .finally(() => { if (!stale) setScopeLoading(false); });
    return () => { stale = true; };
  }, [modelId, refreshVersion]);
  const modelRunIds = useMemo(() => modelId ? new Set(model?.run_ids ?? []) : undefined, [modelId, model]);
  const modelBatchIds = useMemo(() => modelId ? new Set(model?.batch_ids ?? []) : undefined, [modelId, model]);
  const clearModel = () => {
    setModelId("");
    const url = new URL(window.location.href);
    url.searchParams.delete("model");
    window.history.replaceState(window.history.state, "", url);
  };
  return <div className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-paper">
    <BacktestNav actions={<button type="button" onClick={() => setRefreshVersion((value) => value + 1)} className="inline-flex items-center gap-2 rounded-lg border border-edge px-3 py-2 text-xs text-mute"><RefreshCw size={13} />刷新</button>} />
    <main className="min-h-0 min-w-0 flex-1 overflow-y-auto overscroll-contain px-5 py-7 sm:px-8 lg:px-10">
      <div className="mx-auto w-full max-w-[1500px]">
        <p className="text-[10px] font-semibold tracking-[.24em] text-faint">EXPERIMENT HISTORY</p>
        <h1 className="mt-2 font-serif text-3xl text-ink">运行记录</h1>
        <p className="mt-3 text-sm leading-relaxed text-mute">同一个模型的每次测试分别记录，保留当时的数据、参数与结果。Pronoia 基模评测按批次归档。</p>
        <div role="tablist" aria-label="运行记录视图" className="mt-6 flex flex-wrap gap-1 rounded-xl border border-edge bg-card p-1.5">
          {([{ id: "history", label: "全部记录", Icon: Clock3 }, { id: "metrics", label: "指标与管理", Icon: SlidersHorizontal }] as const).map(({ id, label, Icon }) => <button key={id} role="tab" aria-selected={tab === id} aria-controls={`run-${id}`} id={`tab-${id}`} type="button" onClick={() => setTab(id)} className={cls("inline-flex items-center gap-2 rounded-lg px-4 py-2.5 text-xs font-medium transition", tab === id ? "bg-ink text-card" : "text-mute hover:bg-paper hover:text-ink")}><Icon size={14} />{label}</button>)}
        </div>
        {modelId && <div className="mt-4 flex flex-wrap items-center gap-3 rounded-xl border border-brand/20 bg-brand-soft/35 px-4 py-3 text-xs"><span className="text-mute">当前模型</span><span className="font-semibold text-ink">{model?.name || (scopeLoading ? "正在读取…" : modelId)}</span><button type="button" onClick={clearModel} className="ml-auto inline-flex items-center gap-1 text-brand"><X size={12} />查看全部模型</button></div>}
        {scopeError && <div role="alert" className="mt-4 rounded-xl border border-rise/20 bg-rise/5 p-4 text-sm text-rise">{scopeError}</div>}
        {scopeLoading ? <p className="py-12 text-center text-sm text-mute">正在读取模型的测试记录…</p> : <div className="mt-6" role="tabpanel" id={`run-${tab}`} aria-labelledby={`tab-${tab}`}>
          {tab === "history" ? <UnifiedRunHistory embedded refreshVersion={refreshVersion} modelId={modelId} modelRunIds={modelRunIds} modelBatchIds={modelBatchIds} /> : <><p className="mb-4 text-xs leading-relaxed text-mute">查看每次回测的指标并管理任务；指标列可切换。不同数据或时间段的成绩仅供查阅，统一条件的模型比较请进入 Arena。问答评分请在记录或评测批次中查看。</p><BacktestList embedded refreshVersion={refreshVersion} modelId={modelId} modelRunIds={modelRunIds} /></>}
        </div>}
      </div>
    </main>
  </div>;
}
