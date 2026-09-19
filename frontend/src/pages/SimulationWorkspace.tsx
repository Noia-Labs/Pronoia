import { useCallback, useEffect, useState } from "react";
import { ExternalLink, GitBranch, RefreshCw } from "lucide-react";
import { api } from "../api";
import { useStore } from "../store";
import type { Artifact, SimulationJob } from "../types";
import { SimulationLaunchPanel } from "../components/SimulationLaunch";
import SimulationView from "../components/SimulationView";
import SimulationFollowup from "../components/SimulationFollowup";
import { friendlySimulationError } from "../lib/researchProgress";

const running = new Set(["queued", "running", "cancelling"]);
const labels: Record<string, string> = {queued: "等待执行", running: "正在推演", cancelling: "正在停止", completed: "已完成", partial: "部分完成", failed: "未完成", cancelled: "已取消"};

export default function SimulationWorkspace() {
  const cases = useStore(s => s.cases);
  const currentCaseId = useStore(s => s.currentCaseId);
  const loadCase = useStore(s => s.loadCase);
  const setView = useStore(s => s.setView);
  const [caseId, setCaseId] = useState(() => new URLSearchParams(location.search).get("case") || currentCaseId || "");
  const [jobId, setJobId] = useState(() => new URLSearchParams(location.search).get("job") || "");
  const [jobs, setJobs] = useState<SimulationJob[]>([]);
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [graphId, setGraphId] = useState("");
  const [tab, setTab] = useState("scenarios");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [revision, refresh] = useState(0);
  const [samples, setSamples] = useState<Awaited<ReturnType<typeof api.simulationSamples>>>([]);
  const [sampleId, setSampleId] = useState("");
  const [openingSample, setOpeningSample] = useState(false);
  useEffect(() => {let active = true; void api.simulationSamples().then(list => {if (active) {setSamples(list);setSampleId(list[0]?.id || "");}}).catch(() => {});return () => {active = false;};}, []);
  useEffect(() => {if (!caseId && cases.length) setCaseId(cases[0].id);}, [caseId, cases]);
  useEffect(() => {
    if (!caseId) return;
    let active = true;
    setLoading(true); setError("");
    void Promise.all([api.caseDetail(caseId), api.simulations(caseId)]).then(([detail, list]) => {
      if (!active) return;
      setArtifacts(detail.artifacts); setJobs(list);
      setGraphId(old => detail.artifacts.some(a => a.id === old) ? old : [...detail.artifacts].reverse().find(a => a.kind === "graph")?.id || "");
      setJobId(old => list.some(j => j.id === old) ? old : list.find(j => j.artifact_id === new URLSearchParams(location.search).get("artifact"))?.id || list[0]?.id || "");
    }).catch(e => {if (active) setError(e.message);}).finally(() => {if (active) setLoading(false);});
    return () => {active = false;};
  }, [caseId, revision]);
  useEffect(() => {
    const params = new URLSearchParams(); if (caseId) params.set("case", caseId); if (jobId) params.set("job", jobId); else if (new URLSearchParams(location.search).get("artifact")) params.set("artifact", new URLSearchParams(location.search).get("artifact")!);
    history.replaceState({}, "", `/simulations?${params}`);
  }, [caseId, jobId]);
  const changed = useCallback((next: SimulationJob, select = false) => {
    if (next.case_id !== caseId) return;
    if (select) setJobId(next.id);
    setJobs(old => old.some(j => j.id === next.id) ? old.map(j => j.id === next.id ? next : j) : [next, ...old]);
    if (next.artifact_id) refresh(v => v + 1);
  }, [caseId]);
  const job = jobs.find(j => j.id === jobId);
  const activeJobIds = jobs.filter(j => running.has(j.status)).map(j => j.id).join(",");
  useEffect(() => {
    if (!activeJobIds) return;
    let active = true;
    let timer: number;
    const poll = async () => {
      const results = await Promise.allSettled(activeJobIds.split(",").map(id => api.simulation(id)));
      if (!active) return;
      for (const result of results) if (result.status === "fulfilled") changed(result.value);
      if (active) timer = window.setTimeout(() => void poll(), 3000);
    };
    timer = window.setTimeout(() => void poll(), 3000);
    return () => {active = false;window.clearTimeout(timer);};
  }, [activeJobIds, changed]);
  const artifact = artifacts.find(a => a.id === job?.artifact_id);
  const graphs = artifacts.filter(a => a.kind === "graph");
  const graph = graphs.find(a => a.id === graphId);
  const back = async () => {if (caseId) await loadCase(caseId);setView("chat");};
  const chooseCase = (id: string) => {setCaseId(id);setJobId("");setJobs([]);setArtifacts([]);setGraphId("");setTab("scenarios");};
  const openSample = async () => {
    if (openingSample || !sampleId) return;
    setOpeningSample(true);setError("");
    try {const result = await api.openSimulationSample(sampleId);await loadCase(result.case.id);chooseCase(result.case.id);}
    catch (e) {setError(e instanceof Error ? e.message : String(e));}
    finally {setOpeningSample(false);}
  };
  return <main className="flex h-full min-w-0 flex-1 flex-col">
    <header className="flex flex-wrap items-center justify-between gap-3 border-b border-edge bg-card px-6 py-4">
      <div><div className="flex items-center gap-2"><GitBranch size={20} className="text-jade" /><h1 className="font-serif text-2xl">事件推演</h1><span className="rounded-full bg-jade-soft px-2 py-1 text-[10px] text-jade">研究工作区</span></div><p className="mt-1 text-xs text-mute">梳理多方行动，发现遗漏路径，持续复核观察条件。</p></div>
      <div className="flex items-center gap-4 text-xs"><button onClick={() => void back()} className="text-mute hover:text-ink">返回研究对话</button><a href={`/simulations?case=${encodeURIComponent(caseId)}&job=${encodeURIComponent(jobId)}`} target="_blank" rel="noreferrer" className="flex items-center gap-1 text-jade"><ExternalLink size={13} />独立打开</a></div>
    </header>
    <div className="flex min-h-0 flex-1 flex-col overflow-auto md:flex-row md:overflow-hidden">
      <aside className="shrink-0 border-b border-edge bg-paper p-4 md:w-64 md:overflow-y-auto md:border-b-0 md:border-r">
        <label className="block text-xs font-semibold">研究案例<select aria-label="研究案例" value={caseId} onChange={e => chooseCase(e.target.value)} className="mt-2 w-full rounded-lg border border-edge bg-card p-2 text-xs"><option value="">选择研究案例</option>{cases.map(c => <option key={c.id} value={c.id}>{c.title}</option>)}</select></label>
        <div className="mb-2 mt-6 flex items-center justify-between"><h2 className="text-xs font-semibold">推演版本 · {jobs.length}</h2><button aria-label="刷新推演列表" onClick={() => refresh(v => v + 1)} className="text-mute"><RefreshCw size={13} /></button></div>
        <div className="space-y-2">{jobs.map((j, i) => <button key={j.id} onClick={() => {setJobId(j.id);setTab("scenarios");}} className={`w-full rounded-lg border p-3 text-left text-xs ${j.id === jobId ? "border-jade bg-jade-soft" : "border-edge bg-card hover:border-jade/40"}`}><span className="font-semibold">{labels[j.status]}{running.has(j.status) ? ` · ${Math.round(j.progress * 100)}%` : ""}</span><span className="mt-1 block text-[10px] text-mute">{new Date(j.created_at).toLocaleString("zh-CN")} · 版本 {jobs.length - i}</span></button>)}</div>
        {!loading && !jobs.length && <p className="mt-3 text-xs leading-5 text-mute">该案例还没有推演。请从已有证据图开始。</p>}
      </aside>
      <section className="min-w-0 flex-1 p-5 md:overflow-y-auto lg:p-8"><div className="mx-auto max-w-5xl space-y-5">
        {samples.length > 0 && <details className="rounded-card border border-jade/25 bg-jade-soft/40 p-4"><summary className="cursor-pointer text-sm font-semibold">试用样例 · 6 个 A 股历史事件</summary><p className="mt-3 text-xs leading-5 text-mute">载入已整理的公开资料节选，保留当时的截止日期。样例用于历史复盘，资料并非完整；载入不调用模型，点击开始推演后才运行。</p><label className="mt-3 block text-xs">选择样例<select aria-label="选择试用样例" value={sampleId} onChange={e => setSampleId(e.target.value)} className="mt-1 w-full rounded-lg border border-edge bg-card p-2">{samples.map(s => <option key={s.id} value={s.id}>{s.title} · {s.as_of.slice(0,10)}</option>)}</select></label><p className="mt-2 text-xs leading-5 text-mute">重点检查：{samples.find(s => s.id === sampleId)?.check}</p><button disabled={openingSample} onClick={() => void openSample()} className="mt-3 rounded-lg bg-jade px-3 py-2 text-xs text-white disabled:opacity-50">{openingSample ? "正在载入…" : "载入为新案例"}</button></details>}
        {error && <p role="alert" className="rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{error}</p>}
        {loading && !artifacts.length && <p role="status" className="text-sm text-mute">正在读取研究与推演…</p>}
        {!loading && !graphs.length && <div className="rounded-card border border-edge bg-card p-6"><h2 className="font-serif text-xl">从一份有证据的研究开始</h2><p className="my-3 text-sm text-mute">在研究对话中分析事件并生成证据图，再回来选择参与方和观察期限。</p><button onClick={() => void back()} className="rounded-lg bg-jade px-4 py-2 text-sm text-white">前往研究对话</button></div>}
        {graphs.length > 0 && <details className="rounded-card border border-edge bg-card p-4" open={!jobs.length}><summary className="cursor-pointer text-sm font-semibold">发起新推演 · 选择证据与观察窗口</summary><div className="mt-4"><label className="mb-3 block text-xs">证据图<select aria-label="证据图" value={graphId} onChange={e => setGraphId(e.target.value)} className="ml-2 max-w-full rounded border border-edge bg-paper p-2">{graphs.map(g => <option key={g.id} value={g.id}>{g.title} · {new Date(g.created_at).toLocaleDateString("zh-CN")}</option>)}</select></label>{graph && <SimulationLaunchPanel key={`${caseId}:${graph.id}`} caseId={caseId} artifact={graph} managedJob={jobs.find(j => j.graph_artifact_id === graph.id) ?? null} onJobChange={changed} />}</div></details>}
        {job && !artifact && <div className="rounded-card border border-edge bg-card p-6"><h2 className="font-serif text-xl">{labels[job.status]}</h2><p className="mt-3 text-sm text-mute">{job.error ? friendlySimulationError(job.error) : running.has(job.status) ? "各方正在形成行动与条件情景，完成后结果会自动出现在这里。" : "尚无可展示情景，可在上方恢复任务或重新推演。"}</p>{running.has(job.status) && <div className="mt-4 h-2 rounded-full bg-edge"><div className="h-full rounded-full bg-jade" style={{width: `${Math.max(3, job.progress * 100)}%`}} /></div>}</div>}
        {artifact && job && <>
          <div><h2 className="font-serif text-xl leading-8">{cases.find(c => c.id === caseId)?.title || artifact.title}</h2><details className="mt-2 text-xs text-mute"><summary className="cursor-pointer">原始研究问题</summary><p className="mt-2 whitespace-pre-wrap leading-6">{artifact.payload?.source?.question}</p></details><p className="mt-1 text-xs text-mute">观察窗口：{artifact.payload?.source?.horizon_days ?? "—"} 天 · {artifact.payload?.execution?.scenario_compilation?.compiler_version === "v12" ? "核心进展与受阻路径（试用）" : "当前稳定版"}</p></div>
          <nav className="flex gap-6 border-b border-edge">{[["scenarios", "条件情景"], ["followup", "观察与长期复核"]].map(([id, label]) => <button key={id} onClick={() => setTab(id)} className={`border-b-2 pb-3 text-sm ${tab === id ? "border-jade font-semibold text-jade" : "border-transparent text-mute"}`}>{label}</button>)}</nav>
          {tab === "scenarios" ? <SimulationView payload={artifact.payload} /> : <SimulationFollowup key={job.id} jobId={job.id} />}
        </>}
      </div></section>
    </div>
  </main>;
}
