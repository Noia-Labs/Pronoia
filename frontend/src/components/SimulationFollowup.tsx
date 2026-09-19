import { useEffect, useState } from "react";
import { api } from "../api";
import type { FollowupEntry, SimulationFollowup as Followup } from "../types";
import { safeSourceUrl } from "../lib/simulationPresentation";

const labels: Record<string, string> = {pending: "待观察", observed: "已出现", ruled_out: "已排除", unavailable: "无法核查"};
const date = (value: string) => new Date(value).toLocaleString("zh-CN");
const input = "w-full rounded-lg border border-edge bg-card px-3 py-2 text-sm outline-none focus:border-jade";

export default function SimulationFollowup({jobId}: {jobId: string}) {
  const [data, setData] = useState<Followup | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState<string | null>(null);
  const [revision, refresh] = useState(0);
  useEffect(() => {
    let active = true;
    setError("");
    void api.simulationFollowup(jobId).then(d => {if (active) setData(d);}).catch(e => {if (active) setError(String(e.message));});
    return () => {active = false;};
  }, [jobId, revision]);
  const register = async () => {
    setBusy(true); setError("");
    try {setData(await api.registerSimulationFollowup(jobId));} catch(e) {setError(e instanceof Error ? e.message : String(e));} finally {setBusy(false);}
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
    const a = document.createElement("a"); a.href = url; a.download = `research-followup-${jobId}.json`; a.click(); URL.revokeObjectURL(url);
  };
  const latest = new Map(data?.journal.map(entry => [entry.observation_id, entry]) ?? []);
  const resolved = [...latest.values()].filter(e => ["observed", "ruled_out"].includes(e.state)).length;
  const rated = [...latest.values()].filter(e => e.useful !== null).length;
  const useful = [...latest.values()].filter(e => e.useful === true).length;
  return <div className="space-y-5">
    {error && <div role="alert" className="rounded-lg border border-rose-200 bg-rose-50 p-3 text-sm text-rose-800">{error}<button onClick={() => refresh(v => v + 1)} className="ml-3 underline">刷新记录</button></div>}
    {!data && !error && <p role="status" className="text-sm text-mute">正在读取复核记录…</p>}
    {data && !data.snapshot && <div className="rounded-card border border-edge bg-card p-6">
      <h2 className="font-serif text-xl">把情景变成可持续复核的清单</h2>
      <p className="my-3 max-w-2xl text-sm leading-6 text-mute">保存此刻的原始观察条件，再逐条补充公开证据、使用感受和复核耗时。清单的截止日期沿用本次推演；更长的研究需要建立新版本。</p>
      <button disabled={busy} onClick={() => void register()} className="rounded-lg bg-jade px-4 py-2 text-sm text-white disabled:opacity-50">{busy ? "正在保存…" : "保存观察清单，开始复核"}</button>
    </div>}
    {data?.snapshot && <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div><h2 className="font-serif text-xl">观察与复核</h2><p className="mt-1 text-xs text-mute">{data.snapshot.mode === "prospective" ? "前瞻登记" : "历史复盘"} · 登记于 {date(data.snapshot.frozen_at)} · 原窗口截至 {date(data.snapshot.window_end)}</p></div>
        <button onClick={download} className="rounded-lg border border-edge bg-card px-3 py-2 text-xs">导出原始清单与复核记录</button>
      </div>
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        {[ ["观察条目", `${data.snapshot.items.length}`], ["已查明", `${resolved} / ${data.snapshot.items.length}`], ["值得跟踪", rated ? `${useful} / ${rated} 条已评价` : "尚未评价"], ["累计复核用时", data.journal.length ? `${data.journal.reduce((sum, e) => sum + e.minutes_spent, 0)} 分钟` : "尚未记录"] ].map(([label, value]) => <div key={label} className="rounded-lg border border-edge bg-card p-3"><p className="text-xs text-mute">{label}</p><p className="mt-2 text-lg font-semibold">{value}</p></div>)}
      </div>
      <p className="text-xs leading-5 text-mute">这些数值反映复核进度和你的使用评价。条件出现或被排除，都能帮助缩小研究范围；它们不代表情景预测准确率。</p>
      <div className="flex flex-wrap gap-2">{data.checkpoints.map(c => <div key={c.day} className="rounded-lg border border-edge px-3 py-2 text-xs">第 {c.day} 天 · {c.status === "outside_original_window" ? "超出原窗口，不计入" : c.status === "not_due" ? "尚未到期" : c.resolution_rate === null ? "尚无当期复核记录" : `当期已查明 ${Math.round(c.resolution_rate * 100)}%`}</div>)}</div>
      {data.snapshot.items.map(item => {
        const last = latest.get(item.id);
        return <article key={item.id} className="rounded-card border border-edge bg-card p-4">
          <div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="text-[11px] text-jade">{item.kind === "trigger" ? "情景触发条件" : "情景失效条件"} · {item.occurrences.length} 处情景引用</p><h3 className="mt-1 text-sm font-semibold leading-6">{item.signal}</h3><p className="mt-1 text-xs leading-5 text-mute">核查来源：{item.source}</p></div><span className="shrink-0 rounded-full bg-paper px-2 py-1 text-xs">{labels[last?.state ?? "pending"]}</span></div>
          {data.item_reviews?.[item.id]?.length ? <p className="mt-2 rounded bg-amber-50 p-2 text-xs leading-5 text-amber-900">先核对：{data.item_reviews[item.id].map(f => f.message).join("；")}</p> : null}
          {last && <p className="mt-3 text-xs text-mute">最近复核：{date(last.recorded_at)} · {last.reviewer} · {last.note}</p>}
          {editing === item.id ? <ReviewForm jobId={jobId} observationId={item.id} revision={data.journal.length} onCancel={() => setEditing(null)} onSaved={d => {setData(d);setEditing(null);setError("");}} /> : <button onClick={() => setEditing(item.id)} className="mt-3 text-xs font-semibold text-jade underline">{last ? "追加复核" : "记录复核"}</button>}
        </article>;
      })}
      <details className="rounded-lg border border-edge p-4"><summary className="cursor-pointer text-sm">完整复核历史 · {data.journal.length} 条</summary><div className="mt-3 space-y-3">{data.journal.length === 0 && <p className="text-xs text-mute">尚未添加复核。</p>}{data.journal.map(e => <div key={e.sequence} className="border-t border-edge pt-3 text-xs leading-5"><p>{date(e.recorded_at)} · {e.reviewer} · {labels[e.state]} · {e.minutes_spent} 分钟</p><p>{data.snapshot?.items.find(i => i.id === e.observation_id)?.signal}</p><p className="text-mute">{e.note}</p>{safeSourceUrl(e.evidence_url) && <a href={e.evidence_url} target="_blank" rel="noreferrer" className="text-jade underline">查看复核证据（{date(e.evidence_published_at)}）</a>}</div>)}</div></details>
    </>}
  </div>;
}

function ReviewForm({jobId, observationId, revision, onSaved, onCancel}: {jobId: string; observationId: string; revision: number; onSaved: (data: Followup) => void; onCancel: () => void}) {
  const [state, setState] = useState<FollowupEntry["state"]>("pending");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const resolved = state === "observed" || state === "ruled_out";
  const save = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault(); if (busy) return;
    const form = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      const published = String(form.get("published") || "");
      onSaved(await api.reviewSimulation(jobId, {expected_revision: revision, observation_id: observationId, state, reviewer: String(form.get("reviewer")), note: String(form.get("note")), minutes_spent: Number(form.get("minutes")), useful: form.get("useful") === "unknown" ? null : form.get("useful") === "yes", evidence_url: resolved ? String(form.get("url")) : "", evidence_published_at: resolved && published ? new Date(published).toISOString() : ""}));
    } catch(e) {setError(e instanceof Error ? e.message : String(e));} finally {setBusy(false);}
  };
  return <form onSubmit={e => void save(e)} className="mt-4 space-y-3 border-t border-edge pt-4">
    <div className="grid gap-3 md:grid-cols-2"><label className="space-y-1 text-xs">观察状态<select aria-label="观察状态" value={state} onChange={e => setState(e.target.value as FollowupEntry["state"])} className={input}>{Object.entries(labels).map(([key, label]) => <option key={key} value={key}>{label}</option>)}</select></label><label className="space-y-1 text-xs">是否值得继续跟踪<select name="useful" className={input}><option value="unknown">暂不评价</option><option value="yes">值得跟踪</option><option value="no">对我帮助不大</option></select></label></div>
    {resolved && <div className="grid gap-3 md:grid-cols-2"><label className="space-y-1 text-xs">公开证据链接<input name="url" type="url" required className={input} placeholder="https://…" /></label><label className="space-y-1 text-xs">证据发布时间（本地时间）<input name="published" type="datetime-local" required className={input} /></label></div>}
    <label className="block space-y-1 text-xs">复核说明<textarea name="note" required maxLength={4000} rows={2} className={input} placeholder="看到了什么证据？它如何支持或排除这个条件？" /></label>
    <div className="grid gap-3 md:grid-cols-2"><label className="space-y-1 text-xs">复核人<input name="reviewer" required maxLength={100} className={input} /></label><label className="space-y-1 text-xs">本次用时（分钟）<input name="minutes" type="number" min="0" max="10080" step="0.5" required className={input} /></label></div>
    {error && <p role="alert" className="text-xs text-rose-700">{error}</p>}
    <div className="flex gap-2"><button disabled={busy} className="rounded-lg bg-jade px-3 py-2 text-xs text-white disabled:opacity-50">{busy ? "正在保存…" : "保存复核"}</button><button type="button" onClick={onCancel} disabled={busy} className="px-3 py-2 text-xs text-mute">取消</button></div>
  </form>;
}
