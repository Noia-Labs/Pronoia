import { useState } from "react";
import { AlertTriangle, ArrowRight, CheckCircle2, Database, History, Plus } from "lucide-react";
import { api } from "../api";
import { BacktestCreateModal, type CreateRequest, type StrategyType } from "../components/BacktestList";
import BacktestNav from "../components/backtest/BacktestNav";
import ModelLibrary from "../components/backtest/ModelLibrary";
import ModelLibraryEditor from "../components/backtest/ModelLibraryEditor";
import ModelConfigurationEditor from "../components/backtest/ModelConfigurationEditor";
import { modelLibraryApi, type SavedModelEntry, type SavedModelSetup } from "../components/backtest/modelLibraryApi";
import { useStore } from "../store";
import { cls } from "../utils";

export type ExperimentKind = "event" | "quant";

/** Models are reusable configuration. Tests select data and produce separate runs. */
export default function BacktestExperimentPage({ kind }: { kind: ExperimentKind }) {
  const openBTDetail = useStore((state) => state.openBTDetail);
  const loadBTRuns = useStore((state) => state.loadBTRuns);
  const setView = useStore((state) => state.setView);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<SavedModelEntry | null>(null);
  const [revision, setRevision] = useState(0);
  const [saved, setSaved] = useState<SavedModelEntry | null>(null);
  const [testing, setTesting] = useState<{ model: SavedModelEntry; setup: SavedModelSetup } | null>(null);
  const [savedWithWarning, setSavedWithWarning] = useState<{ id: string; message: string } | null>(null);
  const event = kind === "event";

  const beginTest = async (model: SavedModelEntry) => {
    const setup = await modelLibraryApi.setup(model.id);
    setTesting({ model, setup }); setSavedWithWarning(null); setSaved(null);
  };
  const createRun = async (payload: CreateRequest) => {
    const { event_qa, prediction_profile_id, auto_start, ...prediction } = payload;
    let runId: string; let warning = "";
    if (event) {
      const created = await api.btCreateEventExperiment({ prediction, prediction_profile_id, qa: event_qa, auto_start: auto_start ?? true });
      runId = created.run.id;
      warning = Object.entries(created.start_errors ?? {}).map(([task, message]) => (task === "qa" ? "问答" : "预测") + "：" + message).join("；");
    } else {
      const run = await api.btCreateRun(prediction); runId = run.id;
      if (auto_start) { try { await api.btStartRun(runId); } catch (reason) { warning = reason instanceof Error ? reason.message : String(reason); } }
    }
    try { await loadBTRuns(true); } catch { /* The saved run can be read directly. */ }
    setTesting(null); setRevision((value) => value + 1);
    if (warning) setSavedWithWarning({ id: runId, message: warning });
    else openBTDetail(runId);
  };
  const testType: StrategyType = event ? "event" : testing?.setup.strategy_spec?.adapter === "external_http" || testing?.setup.strategy_spec?.type === "api" ? "signal_import" : "quant";

  return <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper">
    <BacktestNav actions={<button type="button" onClick={() => setView("backtest-runs")} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[11px] font-medium text-mute hover:text-ink"><History size={12} />运行记录</button>} />
    <main className="min-h-0 flex-1 overflow-y-auto"><div className="mx-auto w-full max-w-[1450px] px-4 py-5 sm:px-7 lg:px-9">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-4"><div><p className={cls("text-[9px] font-semibold uppercase tracking-[0.18em]", event ? "text-brand" : "text-jade")}>MODEL LIBRARY</p><h1 className="mt-1.5 font-serif text-[27px] font-semibold text-ink">{event ? "事件模型" : "量化模型"}</h1><p className="mt-2 max-w-3xl text-[12px] leading-relaxed text-mute">{event ? "管理 Pronoia、独立大模型和第三方预测服务。同一模型可以测试不同事件，结果保存在统一历史中。" : "管理收益率预测方法、完整交易规则和外部量化服务。先保存模型，再选择指数、行情和时间段发起测试。"}</p></div><div className="flex shrink-0 flex-wrap gap-2"><a href="/backtest/data" className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2.5 text-[12px] text-mute"><Database size={13} />数据管理</a><button type="button" onClick={() => { setAdding(true); setSaved(null); }} className="inline-flex items-center gap-2 rounded-lg bg-ink px-4 py-2.5 text-[12px] font-semibold text-card"><Plus size={15} />添加模型</button></div></header>
      {saved && <div role="status" className="mb-4 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-jade/25 bg-jade-soft/30 px-4 py-3"><p className="flex items-center gap-2 text-[12px] text-jade"><CheckCircle2 size={15} />{saved.save_disposition === "renamed" ? `已将原模型更名为「${saved.name}」，已定位到原卡片。配置和历史测试未改变。` : saved.save_disposition === "existing" ? `名称未改变，已定位到已有模型「${saved.name}」。` : saved.save_disposition === "restored" ? `已恢复模型「${saved.name}」，已定位到该卡片。` : `已保存模型「${saved.name}」，已定位到该卡片。`}</p><button type="button" onClick={() => setSaved(null)} className="text-[11px] text-mute">收起</button></div>}
      {savedWithWarning && <section className="mb-4 rounded-xl border border-amber-200 bg-card p-5"><h2 className="flex items-center gap-2 text-[13px] font-semibold text-ink"><AlertTriangle size={15} className="text-amber-600" />实验已保存，部分任务未启动</h2><p className="mt-2 text-[12px] leading-relaxed text-mute">{savedWithWarning.message}</p><p className="mt-1 text-[11px] text-faint">不需要重复创建。进入详情查看原因后，可启动相应任务。</p><button type="button" onClick={() => openBTDetail(savedWithWarning.id)} className="mt-3 inline-flex items-center gap-2 rounded-lg bg-ink px-3 py-2 text-[11px] font-semibold text-card">查看已保存实验 <ArrowRight size={12} /></button></section>}
      <ModelLibrary key={kind} category={kind} revision={revision} highlightedModelId={saved?.id} onTest={beginTest} onAdd={() => setAdding(true)} onEdit={(model) => { setEditing(model); setSaved(null); }} />
    </div></main>
    {adding && <ModelLibraryEditor key={kind} category={kind} onClose={() => setAdding(false)} onSaved={(model) => { setAdding(false); setSaved(model); setRevision((value) => value + 1); }} />}
    {editing && <ModelConfigurationEditor key={editing.id} model={editing} onClose={() => setEditing(null)} onSaved={(model) => { setEditing(null); setSaved(model); setRevision((value) => value + 1); }} />}
    {testing && <BacktestCreateModal key={testing.model.id} open initialType={testType} allowedTypes={[testType]} savedModelSetup={testing.setup} savedModelName={testing.model.name} onClose={() => setTesting(null)} onCreate={createRun} />}
  </div>;
}
