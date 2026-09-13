import { useEffect, useMemo, useState } from "react";
import {
  AlertCircle, ArrowLeft, CheckCircle2, ChevronDown, Clock3, FileText,
  RefreshCw, RotateCcw, ShieldCheck, Users,
} from "lucide-react";
import { api } from "../api";
import { useStore } from "../store";
import type { ProspectiveCandidate, ProspectiveDetailResponse, ProspectiveItemDetail } from "../types";
import { cls } from "../utils";

const statusLabel: Record<string, string> = {
  frozen: "预测已冻结", settled: "已结算", waiting: "持续观察", insufficient: "证据不足",
  prediction_failed: "预测失败", failed: "失败",
};
const stageLabel: Record<string, string> = {
  candidate_discovered: "候选入选", evidence_hydrated: "历史证据补全",
  evidence_frozen: "证据冻结", model_input_frozen: "分析输入冻结",
  market_features_computed: "行情特征", prediction_generated: "预测生成",
  settlement_waiting: "等待结算", judge_completed: "Judge 完成",
};
const formatDate = (value?: string | null) => value ? new Date(value).toLocaleString() : "-";
const percent = (value: unknown) => typeof value === "number" ? `${(value * 100).toFixed(2)}%` : "数据缺失";
const selected = (candidate: ProspectiveCandidate) => candidate.selected === true || candidate.selected === 1 || candidate.status === "selected";
const outcomeClass = (correct?: number | null) => correct == null ? "text-mute" : correct ? "text-jade" : "text-rise";
const outcomeLabel = (correct?: number | null) => correct == null ? "" : correct ? "正确" : "错误";

function ConfigSummary({ run }: { run: ProspectiveDetailResponse["run"] }) {
  const source = run.source_config ?? {};
  const metric = run.metric_config ?? {};
  const keywords = Array.isArray(source.keywords) ? source.keywords.join("、") : "-";
  const symbols = Array.isArray(source.symbols) ? source.symbols.join(", ") : "全部/默认股票池";
  const candidateDays = source.candidate_lookback_trade_days ?? source.lookback_trade_days ?? "-";
  const analysisDays = source.analysis_lookback_trade_days ?? "-";
  return <section className="mb-5 border-y border-edge bg-card px-4 py-4 text-[12px]">
    <div className="mb-3 flex items-center gap-2 font-medium"><ShieldCheck size={14} className="text-jade" />严格事前证据边界</div>
    <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-mute lg:grid-cols-4">
      <div>候选发现：<span className="text-ink">前 {String(candidateDays)} 个交易日</span></div>
      <div>分析证据：<span className="text-ink">前 {String(analysisDays)} 个交易日</span></div>
      <div>证据范围：<span className="text-ink">{run.evidence_start_date || "-"} 至 {run.evidence_end_date || "-"}</span></div>
      <div>截止时间：<span className="text-ink">{formatDate(run.evidence_cutoff_at)}</span></div>
      <div>当天公告：<span className="text-ink">{run.evidence_policy === "before_capture_date" ? "排除无精确时间记录" : "精确时间校验"}</span></div>
      <div>关键词：<span className="text-ink">{keywords}</span></div>
      <div>股票池：<span className="text-ink">{symbols}</span></div>
      <div>结算目标：<span className="text-ink">{metric.settlement_mode === "absolute_trade_date" ? String(metric.settlement_trade_date ?? "-") : `${run.settle_after_days ?? "-"} 个交易日`}</span></div>
    </div>
  </section>;
}

function MarketFeatures({ snapshot }: { snapshot?: Record<string, unknown> }) {
  const features = snapshot?.market_features as Record<string, Record<string, unknown>> | undefined;
  if (!features) return null;
  const asset = features.asset ?? {};
  const car = features.car ?? {};
  const quality = features.data_quality ?? {};
  return <div className="mt-2 grid grid-cols-2 gap-x-5 gap-y-1 text-[11px] text-mute sm:grid-cols-4">
    <span>个股 T-5 <strong className="text-ink">{percent(asset.pre5_return)}</strong></span>
    <span>个股 T-20 <strong className="text-ink">{percent(asset.pre20_return)}</strong></span>
    <span>超额 T-5 <strong className="text-ink">{percent(car.pre5_return)}</strong></span>
    <span>超额 T-20 <strong className="text-ink">{percent(car.pre20_return)}</strong></span>
    {quality.asset_available === false && <span className="col-span-full text-amber">冻结时行情数据不可用，模型输入已记录该缺失。</span>}
  </div>;
}

function CompanyTrack({ row, candidate }: { row: ProspectiveItemDetail; candidate?: ProspectiveCandidate }) {
  const trace = row.trace ?? [];
  const inputTrace = trace.find(entry => entry.stage === "model_input_frozen");
  const legacy = trace.some(entry => entry.trace_version === "legacy-reconstructed");
  const latestSettlement = row.settlements[row.settlements.length - 1];
  return <details open className="group border-b border-edge last:border-b-0">
    <summary className="flex cursor-pointer list-none items-center justify-between gap-4 px-4 py-4 hover:bg-paper/60">
      <div className="min-w-0">
        <div className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{candidate?.company_name || row.item.symbol || "未知公司"}</span>
          <span className="text-[11px] text-mute">{row.item.symbol}</span>
          <span className="rounded-sm bg-jade-soft px-1.5 py-0.5 text-[10px] text-jade">{statusLabel[row.item.status] ?? row.item.status}</span>
          {legacy && <span className="rounded-sm bg-amber-soft px-1.5 py-0.5 text-[10px] text-amber">历史兼容轨迹</span>}
        </div>
        <div className="mt-1 text-[11px] text-mute">{candidate?.event_count ?? row.evidence.length} 条公告证据 · 冻结 {formatDate(row.prediction?.as_of_at)}</div>
      </div>
        <div className="flex shrink-0 items-center gap-4 text-right">
        <div><div className="text-[10px] text-mute">预测</div><div className="text-[13px] font-medium">{row.prediction?.pred_direction ?? "-"} · {row.prediction?.confidence ?? "-"}</div></div>
        <div><div className="text-[10px] text-mute">真实结果</div><div className={cls("text-[13px] font-medium", outcomeClass(row.item.is_correct))}>{row.item.actual_label ?? ""}{row.item.actual_label && outcomeLabel(row.item.is_correct) ? ` · ${outcomeLabel(row.item.is_correct)}` : ""}</div></div>
        <ChevronDown size={15} className="text-mute transition-transform group-open:rotate-180" />
      </div>
    </summary>
    <div className="border-t border-edge bg-paper/35 px-4 py-4">
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {trace.map((entry, index) => <div key={entry.id} className="flex items-center gap-2">
          {index > 0 && <span className="h-px w-5 bg-edge" />}
          <span className={cls("rounded-sm border px-2 py-1 text-[10px]", entry.stage === "judge_completed" ? "border-jade/30 bg-jade-soft text-jade" : "border-edge bg-card text-ink")}>{stageLabel[entry.stage] ?? entry.stage_title}</span>
        </div>)}
        {!row.item.actual_label && <><span className="h-px w-5 bg-edge" /><span className="rounded-sm border border-dashed border-edge px-2 py-1 text-[10px] text-mute">等待目标交易日</span></>}
      </div>
      {legacy && <div className="mb-3 text-[11px] text-amber">该批次创建于轨迹功能启用前，只能从已冻结证据和最终预测还原可验证阶段，未伪造中间分析。</div>}
      <MarketFeatures snapshot={inputTrace?.input_snapshot} />
      {row.prediction && <div className="mt-3 border-l-2 border-brand pl-3 text-[12px]"><div className="font-medium">最终判断：{row.prediction.pred_direction}，置信度 {row.prediction.confidence ?? "未返回"}</div><div className="mt-1 text-mute">{row.prediction.rationale || "模型未返回分析理由"}</div><div className="mt-1 text-[10px] text-mute">模型 {row.prediction.model_version || "-"} · prompt {row.prediction.prompt_version || "-"} · evidence hash {row.prediction.evidence_hash?.slice(0, 16) || "-"}</div></div>}
      <div className="mt-4">
        <div className="mb-2 flex items-center gap-1 text-[11px] font-medium"><FileText size={12} />冻结公告时间线</div>
        <div className="divide-y divide-edge border-y border-edge">
          {row.evidence.map(ev => <div key={ev.id} className="py-2.5 text-[11px]"><div className="flex flex-wrap justify-between gap-2"><span className="font-medium">{ev.title || "公告证据"}</span><span className="text-mute">发布 {formatDate(ev.published_at)}</span></div><div className="mt-1 text-mute">抓取 {formatDate(ev.retrieved_at)} · {ev.source_url || "无来源链接"}</div></div>)}
          {row.evidence.length === 0 && <div className="py-3 text-[11px] text-mute">没有可展示的冻结证据。</div>}
        </div>
      </div>
      {latestSettlement && <div className="mt-3 border-t border-edge pt-3 text-[11px]"><div className="flex flex-wrap justify-between gap-2"><span className={cls("font-medium", outcomeClass(row.item.is_correct))}>真实结果：{latestSettlement.actual_label ?? "待定"}{outcomeLabel(row.item.is_correct) ? ` · 预测${outcomeLabel(row.item.is_correct)}` : ""}</span><span className="text-mute">{formatDate(latestSettlement.settled_at || latestSettlement.created_at)}</span></div><div className="mt-1 text-mute">{latestSettlement.reasoning || "结果尚未判定"}</div>{typeof latestSettlement.actual_value === "number" && <div className="mt-1 text-mute">实际超额收益：<span className="text-ink">{percent(latestSettlement.actual_value)}</span> · 阈值 {percent((latestSettlement.result_source as Record<string, unknown> | undefined)?.epsilon)} · 基准日 {String((latestSettlement.result_source as Record<string, unknown> | undefined)?.base_trade_date ?? "-")} · 结算日 {String((latestSettlement.result_source as Record<string, unknown> | undefined)?.actual_trade_date ?? "-")}</div>}</div>}
    </div>
  </details>;
}

export default function ProspectiveDetail() {
  const id = useStore(s => s.currentProspectiveRunId);
  const back = useStore(s => s.backFromProspectiveDetail);
  const [data, setData] = useState<ProspectiveDetailResponse | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = async () => { if (!id) return; try { setError(""); setData(await api.prospectiveRun(id)); } catch (err) { setError(err instanceof Error ? err.message : "详情加载失败"); } };
  useEffect(() => { void load(); const timer = window.setInterval(() => void load(), 5000); return () => window.clearInterval(timer); }, [id]);
  const candidateById = useMemo(() => new Map((data?.candidates ?? []).map(candidate => [candidate.id, candidate])), [data?.candidates]);
  if (!id || !data) return <main className="flex flex-1 items-center justify-center text-mute">{error || "正在加载前瞻评测..."}</main>;
  const run = data.run;
  const metrics = data.metrics;
  const candidates = data.candidates ?? [];
  const metricValue = (key: string, fallback: string | number = 0) => metrics[key] == null ? fallback : String(metrics[key]);
  const action = async (fn: () => Promise<ProspectiveDetailResponse["run"]>) => { setBusy(true); setNotice(""); try { const result = await fn(); setNotice(result.settlement_summary?.message || "操作已完成"); await load(); } catch (err) { setError(err instanceof Error ? err.message : "操作失败"); } finally { setBusy(false); } };
  const summary = data.settlement_summary;
  const available = run.result_available_at ? new Date(run.result_available_at) : null;
  const canSettle = !available || available.getTime() <= Date.now();
  return <main className="flex min-w-0 flex-1 flex-col overflow-hidden bg-paper">
    <header className="flex items-center justify-between border-b border-edge px-4 py-4 sm:px-6">
      <div className="flex min-w-0 items-center gap-3"><button title="返回" onClick={back} className="rounded-md p-1.5 text-mute hover:bg-edge/50 hover:text-ink"><ArrowLeft size={17} /></button><div className="min-w-0"><h2 className="truncate font-serif text-[19px] font-semibold">{run.name}</h2><p className="mt-1 text-[11px] text-mute">{run.id} · {run.status}</p></div></div>
      <div className="flex shrink-0 gap-2">{run.status === "scheduled" && <button disabled={busy} onClick={() => void action(() => api.prospectiveCapture(run.id))} className="flex items-center gap-1 rounded-md bg-brand px-3 py-2 text-[12px] text-card"><RotateCcw size={13} />立即抓取</button>}{["waiting", "partial", "insufficient"].includes(run.status) && <button disabled={busy} title={!canSettle ? `预计 ${formatDate(run.result_available_at)} 后可结算` : "立即获取目标交易日行情并计算结果"} onClick={() => void action(() => api.prospectiveSettle(run.id, true))} className="flex items-center gap-1 rounded-md border border-edge px-3 py-2 text-[12px]"><CheckCircle2 size={13} />{canSettle ? "立即结算" : "检查结果"}</button>}<button title="刷新" onClick={() => void load()} className="rounded-md border border-edge p-2 text-mute"><RefreshCw size={14} /></button></div>
    </header>
    <div className="flex-1 overflow-auto p-4 sm:p-6">
      {(error || run.error_message) && <div className="mb-4 flex items-start gap-2 rounded-md bg-rise/10 px-3 py-2 text-[12px] text-rise"><AlertCircle size={14} className="mt-0.5 shrink-0" />{error || run.error_message}</div>}
      {notice && <div className="mb-4 rounded-md bg-jade-soft px-3 py-2 text-[12px] text-jade">{notice}</div>}
      <div className="mb-5 grid grid-cols-2 gap-3 lg:grid-cols-5">{[["候选公司", candidates.length],["入选公司", candidates.filter(selected).length],["冻结预测", metricValue("n_predicted")],["已结算", metricValue("n_settled")],["准确率", metrics.accuracy == null ? "-" : `${Math.round(Number(metrics.accuracy) * 100)}%`]].map(([label, value]) => <div key={String(label)} className="border-t-2 border-edge bg-card px-3 py-3"><div className="text-[11px] text-mute">{label}</div><div className="mt-1 text-[20px] font-semibold">{value}</div></div>)}</div>
      <ConfigSummary run={run} />
      <section className="mb-5 grid grid-cols-2 gap-3 border-y border-edge bg-card px-4 py-3 text-[12px] sm:grid-cols-5"><div><span className="text-mute">结算进度</span><div className="mt-1 font-medium">{summary?.settled ?? 0}/{summary?.total ?? data.items.length}</div></div><div><span className="text-mute">等待行情</span><div className="mt-1 font-medium">{summary?.waiting ?? 0}</div></div><div><span className="text-mute">失败</span><div className="mt-1 font-medium">{summary?.failed ?? 0}</div></div><div><span className="text-mute">覆盖率</span><div className="mt-1 font-medium">{`${Math.round(Number(summary?.coverage ?? 0) * 100)}%`}</div></div><div><span className="text-mute">结果可用时间</span><div className="mt-1 font-medium">{formatDate(run.result_available_at || summary?.result_available_at)}</div></div></section>
      <section className="mb-5 overflow-hidden rounded-card border border-edge bg-card">
        <div className="flex items-center justify-between border-b border-edge px-4 py-3"><span className="flex items-center gap-2 font-medium"><Users size={14} className="text-brand" />入选公司分析轨迹</span><span className="text-[11px] text-mute">{data.items.length} 家</span></div>
        {data.items.length ? data.items.map(row => <CompanyTrack key={row.item.id} row={row} candidate={candidateById.get(row.item.candidate_id ?? "")} />) : <div className="p-10 text-center text-[12px] text-mute">尚未生成公司分析轨迹。等待抓取时间到达，或点击“立即抓取”。</div>}
      </section>
      <section className="border-y border-edge bg-card px-4 py-3 text-[12px]"><div className="mb-2 flex items-center gap-2 font-medium"><Clock3 size={14} className="text-amber" />批次时间</div><div className="grid grid-cols-1 gap-2 text-mute sm:grid-cols-3"><div>计划抓取：<span className="text-ink">{formatDate(run.capture_at)}</span></div><div>实际冻结：<span className="text-ink">{formatDate(run.actual_capture_at)}</span></div><div>目标结算：<span className="text-ink">{formatDate(run.target_settle_at || run.settle_at)}</span></div></div></section>
    </div>
  </main>;
}
