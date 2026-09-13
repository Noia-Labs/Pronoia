import { useEffect, useMemo, useState } from "react";
import { AlertCircle, CheckCircle2, Clock3, Loader2, Plus, Radio, RefreshCw, X } from "lucide-react";
import { api } from "../api";
import { useStore } from "../store";
import type { ProspectiveRun } from "../types";
import { cls } from "../utils";

const statusLabel: Record<string, string> = {
  scheduled: "待抓取", capturing: "抓取中", candidates_ready: "候选已生成",
  selecting: "选择中", predicting: "预测中", waiting: "等待结算", settling: "结算中",
  completed: "已完成", partial: "部分完成", insufficient: "证据不足", failed: "失败", cancelled: "已取消",
};

const sourceLabel: Record<string, string> = {
  sample: "示例数据", cn_announcements: "CN 公告", us_sec: "US SEC", macro: "宏观日历",
};

function Status({ value }: { value: string }) {
  const running = ["capturing", "selecting", "predicting", "settling"].includes(value);
  return <span className={cls("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px]", value === "completed" ? "bg-jade-soft text-jade" : value === "failed" ? "bg-rise/10 text-rise" : "bg-amber-soft text-amber")}>
    {running ? <Loader2 size={11} className="animate-spin" /> : value === "completed" ? <CheckCircle2 size={11} /> : <Clock3 size={11} />}
    {statusLabel[value] ?? value}
  </span>;
}

function parseList(value: string): string[] {
  return [...new Set(value.split(/[，,;；\n]/).map(item => item.trim()).filter(Boolean))];
}

function CreateModal({ onClose, onCreated }: { onClose: () => void; onCreated: (r: ProspectiveRun) => void }) {
  const now = new Date(); now.setMinutes(now.getMinutes() + 1);
  const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
  const [name, setName] = useState("自动前瞻评测批次");
  const [captureAt, setCaptureAt] = useState(local);
  const [source, setSource] = useState("cn_announcements");
  const [lookbackDays, setLookbackDays] = useState("3");
  const [analysisLookbackDays, setAnalysisLookbackDays] = useState("20");
  const [keywords, setKeywords] = useState("业绩预告, 业绩快报, 回购, 减持, 定增, 并购");
  const [symbols, setSymbols] = useState("");
  const [candidateLimit, setCandidateLimit] = useState("30");
  const [selectionCount, setSelectionCount] = useState("5");
  const [settlementMode, setSettlementMode] = useState<"after_trade_days" | "absolute_trade_date">("after_trade_days");
  const [settleAfterDays, setSettleAfterDays] = useState("3");
  const [settlementDate, setSettlementDate] = useState("");
  const [epsilon, setEpsilon] = useState("0.005");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const submit = async () => {
    const parsedCandidateLimit = Number(candidateLimit);
    const parsedSelectionCount = Number(selectionCount);
    if (!captureAt) { setError("请选择抓取时间。"); return; }
    if (parsedSelectionCount > parsedCandidateLimit) { setError("选择公司数不能超过候选上限。"); return; }
    if (source === "cn_announcements" && Number(analysisLookbackDays) < Number(lookbackDays)) { setError("分析证据窗口不能小于候选发现窗口。"); return; }
    if (settlementMode === "absolute_trade_date" && !settlementDate) { setError("请选择结算交易日。"); return; }
    const parsedKeywords = parseList(keywords);
    if (source === "cn_announcements" && parsedKeywords.length === 0) { setError("请至少配置一个公告关键词。"); return; }
    const parsedSymbols = parseList(symbols).map(item => item.toUpperCase());
    if (source === "us_sec" && parsedSymbols.length === 0) { setError("请至少配置一个美股代码。"); return; }

    setBusy(true); setError("");
    try {
      const sourceConfig: Record<string, unknown> = {
        source,
        candidate_limit: parsedCandidateLimit,
        selection_count: parsedSelectionCount,
        max_items: parsedSelectionCount,
      };
      if (source === "cn_announcements") {
        sourceConfig.candidate_lookback_trade_days = Number(lookbackDays);
        sourceConfig.analysis_lookback_trade_days = Number(analysisLookbackDays);
        sourceConfig.evidence_cutoff_policy = "before_capture_date";
        sourceConfig.keywords = parsedKeywords;
        if (parsedSymbols.length) sourceConfig.symbols = parsedSymbols;
      }
      if (source === "us_sec") sourceConfig.symbols = parsedSymbols;

      const run = await api.prospectiveCreateRun({
        name: name.trim() || "自动前瞻评测批次",
        capture_at: new Date(captureAt).toISOString(),
        settle_after_days: settlementMode === "after_trade_days" ? Number(settleAfterDays) : 0,
        source_config: sourceConfig as Parameters<typeof api.prospectiveCreateRun>[0]["source_config"],
        predictor_config: { model_version: "prospective-team-prompt-v1", prompt_version: "as-of-v1" },
        metric_config: {
          settlement_mode: settlementMode,
          ...(settlementMode === "absolute_trade_date" ? { settlement_trade_date: settlementDate } : {}),
          epsilon: Number(epsilon),
        },
      });
      onCreated(run); onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建批次失败，请稍后重试。");
    } finally { setBusy(false); }
  };

  return <div className="absolute inset-0 z-40 flex items-center justify-center bg-ink/20 p-4 backdrop-blur-sm">
    <div className="flex max-h-[calc(100vh-2rem)] w-full max-w-2xl flex-col overflow-hidden rounded-card border border-edge bg-card shadow-pop">
      <div className="flex items-center justify-between border-b border-edge px-5 py-3"><div><h3 className="font-serif text-[16px] font-semibold">新建自动前瞻评测</h3><p className="mt-0.5 text-[11px] text-mute">从近期公告形成候选池，选择公司并冻结预测。</p></div><button title="关闭" onClick={onClose} className="rounded-md p-1 text-mute hover:bg-edge/50 hover:text-ink"><X size={16} /></button></div>
      <div className="flex-1 space-y-5 overflow-y-auto px-5 py-4 text-[13px]">
        <section className="space-y-3">
          <div className="text-[11px] font-medium text-mute">批次与抓取</div>
          <div className="grid grid-cols-2 gap-3"><label className="block">批次名称<input className="input mt-1" value={name} onChange={e => setName(e.target.value)} /></label><label className="block">抓取时间<input className="input mt-1" type="datetime-local" value={captureAt} onChange={e => setCaptureAt(e.target.value)} /></label></div>
          <label className="block">数据来源<select className="input mt-1" value={source} onChange={e => setSource(e.target.value)}><option value="cn_announcements">CN 公告</option><option value="us_sec">US SEC</option><option value="macro">宏观日历</option><option value="sample">示例数据（快速验收）</option></select></label>
          {source === "cn_announcements" && <>
            <div className="grid grid-cols-2 gap-3"><label>候选发现窗口（交易日）<input className="input mt-1" type="number" min="1" max="60" value={lookbackDays} onChange={e => setLookbackDays(e.target.value)} /></label><label>分析证据窗口（交易日）<input className="input mt-1" type="number" min={lookbackDays || "1"} max="250" value={analysisLookbackDays} onChange={e => setAnalysisLookbackDays(e.target.value)} /></label></div>
            <label>可选股票池<input className="input mt-1" placeholder="如 600519, 000001；留空为全部" value={symbols} onChange={e => setSymbols(e.target.value)} /></label>
            <label className="block">公告关键词<textarea className="input mt-1 min-h-16 resize-y" value={keywords} onChange={e => setKeywords(e.target.value)} placeholder="用逗号或换行分隔" /></label>
          </>}
          {source === "us_sec" && <label className="block">美股代码<input className="input mt-1" value={symbols} onChange={e => setSymbols(e.target.value)} placeholder="AAPL, NVDA, MSFT" /></label>}
          <div className="grid grid-cols-2 gap-3"><label>候选公司上限<input className="input mt-1" type="number" min="1" max="500" value={candidateLimit} onChange={e => setCandidateLimit(e.target.value)} /></label><label>模型选择公司数<input className="input mt-1" type="number" min="1" max="100" value={selectionCount} onChange={e => setSelectionCount(e.target.value)} /></label></div>
        </section>

        <section className="space-y-3 border-t border-edge pt-4">
          <div className="text-[11px] font-medium text-mute">结算</div>
          <div className="grid grid-cols-2 gap-3"><label>结算方式<select className="input mt-1" value={settlementMode} onChange={e => setSettlementMode(e.target.value as typeof settlementMode)}><option value="after_trade_days">抓取后 N 个交易日</option><option value="absolute_trade_date">指定交易日</option></select></label>{settlementMode === "after_trade_days" ? <label>结算周期（交易日）<input className="input mt-1" type="number" min="1" max="365" value={settleAfterDays} onChange={e => setSettleAfterDays(e.target.value)} /></label> : <label>结算交易日<input className="input mt-1" type="date" value={settlementDate} onChange={e => setSettlementDate(e.target.value)} /></label>}</div>
          <label>方向判定阈值<input className="input mt-1" type="number" min="0" max="1" step="0.001" value={epsilon} onChange={e => setEpsilon(e.target.value)} /></label>
        </section>
        {error && <div className="flex items-start gap-2 rounded-md bg-rise/10 px-3 py-2 text-[12px] text-rise"><AlertCircle size={14} className="mt-0.5 shrink-0" />{error}</div>}
      </div>
      <div className="border-t border-edge px-5 py-3"><button disabled={busy} onClick={() => void submit()} className="flex w-full items-center justify-center gap-1.5 rounded-card bg-brand px-3 py-2 text-[13px] font-medium text-card disabled:opacity-50">{busy ? <Loader2 size={14} className="animate-spin" /> : <Plus size={14} />}创建批次</button></div>
    </div>
  </div>;
}

export default function ProspectiveList() {
  const open = useStore(s => s.openProspectiveDetail);
  const [runs, setRuns] = useState<ProspectiveRun[]>([]);
  const [loading, setLoading] = useState(true);
  const [modal, setModal] = useState(false);
  const [error, setError] = useState("");
  const load = async () => { setLoading(true); setError(""); try { setRuns((await api.prospectiveRuns(200)).items ?? []); } catch (err) { setError(err instanceof Error ? err.message : "批次加载失败"); } finally { setLoading(false); } };
  useEffect(() => { void load(); }, []);
  const rows = useMemo(() => runs, [runs]);
  const startCapture = async (run: ProspectiveRun) => { try { await api.prospectiveCapture(run.id); await load(); } catch (err) { setError(err instanceof Error ? err.message : "抓取启动失败"); } };
  return <main className="relative flex min-w-0 flex-1 flex-col overflow-hidden bg-paper">
    <header className="flex items-center justify-between border-b border-edge px-6 py-4"><div><div className="flex items-center gap-2"><Radio size={18} className="text-amber" /><h2 className="font-serif text-[20px] font-semibold">前瞻评测</h2></div><p className="mt-1 text-[12px] text-mute">扫描近期公告，冻结候选证据和预测，并在目标交易日自动结算。</p></div><div className="flex gap-2"><button title="刷新" onClick={() => void load()} className="rounded-md border border-edge bg-card p-2 text-mute hover:text-ink"><RefreshCw size={15} className={cls(loading && "animate-spin")} /></button><button onClick={() => setModal(true)} className="flex items-center gap-1.5 rounded-card bg-brand px-3 py-2 text-[12px] font-medium text-card"><Plus size={14} />新建批次</button></div></header>
    <div className="flex-1 overflow-auto p-6">{error && <div className="mb-3 flex items-center gap-2 rounded-md bg-rise/10 px-3 py-2 text-[12px] text-rise"><AlertCircle size={14} />{error}</div>}<div className="overflow-hidden rounded-card border border-edge bg-card"><table className="w-full text-left text-[12px]"><thead className="bg-edge/30 text-mute"><tr><th className="px-4 py-3">批次</th><th>状态</th><th>抓取时间</th><th>目标结算时间</th><th>候选 / 预测</th><th>准确率</th><th /></tr></thead><tbody>{rows.map(r => <tr key={r.id} onDoubleClick={() => open(r.id)} className="cursor-pointer border-t border-edge hover:bg-paper/60"><td className="px-4 py-3 font-medium">{r.name}<div className="mt-0.5 text-[10px] text-mute">{sourceLabel[String(r.source_config?.source ?? "sample")] ?? String(r.source_config?.source ?? "sample")} · {r.id}</div></td><td><Status value={r.status} /></td><td className="text-mute">{new Date(r.capture_at).toLocaleString()}</td><td className="text-mute">{r.target_settle_at ? new Date(r.target_settle_at).toLocaleString() : r.settle_at ? new Date(r.settle_at).toLocaleString() : String(r.metric_config?.settlement_trade_date ?? "-")}</td><td>{r.candidate_items ?? r.total_items ?? 0} 候选 · {r.frozen_items ?? r.selected_items ?? 0} 冻结 · {r.settled_items ?? 0} 结算</td><td>{r.settled_items ? `${Math.round(((r.correct_items ?? 0) / r.settled_items) * 100)}%` : "-"}</td><td className="pr-4 text-right">{r.status === "scheduled" && <button onClick={e => { e.stopPropagation(); void startCapture(r); }} className="rounded-md border border-edge px-2 py-1 text-[11px] text-brand">立即抓取</button>}<button onClick={() => open(r.id)} className="ml-2 text-brand">详情</button></td></tr>)}</tbody></table>{!loading && rows.length === 0 && <div className="p-12 text-center text-mute">还没有前瞻评测批次。</div>}{loading && <div className="p-12 text-center text-mute"><Loader2 className="mx-auto animate-spin" size={18} /></div>}</div></div>
    {modal && <CreateModal onClose={() => setModal(false)} onCreated={r => setRuns(x => [r, ...x])} />}
  </main>;
}
