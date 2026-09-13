import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  CheckCircle2, CircleDashed, Download, FileText, FileJson2,
  Loader2, PencilLine, Save, X, ChevronDown, AlertTriangle,
} from "lucide-react";
import { api } from "../../api";
import { providerLabel } from "../../modelProviders";
import { useStore } from "../../store";
import type {
  ModelLabBatch, ModelLabManualScoreInput, ModelLabProfile,
  ModelLabQAResult, ModelLabResults, ModelLabScore, ModelLabQuestion,
} from "../../types";
import { cls } from "../../utils";
import Markdown from "../../components/Markdown";

const INPUT = "w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[12px] text-ink outline-none transition placeholder:text-faint focus:border-violet/50 focus:ring-2 focus:ring-violet/10 disabled:cursor-not-allowed disabled:bg-edge/35";

export default function ModelLabResultsPanel({
  results, profiles = [], onUpdated, currentRunId,
}: {
  results: ModelLabResults;
  profiles?: ModelLabProfile[];
  onUpdated: () => void;
  currentRunId?: string;
}) {
  const openBTDetail = useStore((state) => state.openBTDetail);
  const [manualResult, setManualResult] = useState<ModelLabQAResult | null>(null);
  const [selectedQuestionId, setSelectedQuestionId] = useState("");
  const [selectedRepeat, setSelectedRepeat] = useState(1);
  const profileFallback = (id: string) => profiles.find((profile) => profile.id === id);
  const comparisons = useMemo(() => aggregateResults(results.qa_results, results.summary.qa_comparison, profileFallback), [results.qa_results, results.summary.qa_comparison, profiles]);
  const predictionComparisons = buildPredictionComparisons(results, profileFallback);
  const predictionByTask = new Map((results.summary.prediction_runs ?? []).map((item) => [item.task_id, item]));
  const taskIdentity = (task: ModelLabResults["tasks"][number]) => {
    const frozen = predictionByTask.get(task.id) ?? results.qa_results.find((result) => result.task_id === task.id) ?? taskSnapshotProvenance(task);
    return modelIdentity(frozen, task.profile_id, profileFallback);
  };
  const qaIdentity = (item: ModelLabQAResult) => modelIdentity(item, item.profile_id, profileFallback);
  const questionIds = [...new Set(results.qa_results.map((item) => item.question_id))];
  const questionId = questionIds.includes(selectedQuestionId) ? selectedQuestionId : questionIds[0] ?? "";
  const questionAnswers = results.qa_results.filter((item) => item.question_id === questionId);
  const repeatNumbers = [...new Set(questionAnswers.map((item) => item.repeat_no))].sort((a, b) => a - b);
  const repeatNo = repeatNumbers.includes(selectedRepeat) ? selectedRepeat : repeatNumbers[0] ?? 1;
  const shownAnswers = questionAnswers.filter((item) => item.repeat_no === repeatNo);
  const question = questionAnswers[0] ? frozenQuestion(results, questionAnswers[0]) : null;
  const manualQuestion = manualResult ? frozenQuestion(results, manualResult) : null;
  const visibleTasks = currentRunId ? results.tasks.filter((task) => task.kind === "qa") : results.tasks;
  const qaEnabled = Boolean(results.batch.qa?.enabled || results.tasks.some((task) => task.kind === "qa"));
  const exportJson = () => download(new Blob([JSON.stringify(results, null, 2)], { type: "application/json;charset=utf-8" }), safeName(results.batch.name) + "-model-lab.json");
  const exportCsv = () => {
    const rows = [["profile_name", "model_id", "provider", "variant", "question_code", "question", "repeat", "status", "score", "latency_ms", "cost", "judge_profile_name", "judge_model_id", "judge_provider", "answer"], ...results.qa_results.map((item) => {
      const identity = qaIdentity(item);
      const judge = judgeIdentity(item, results.batch.qa?.judge_profile_id, profileFallback);
      const prompt = frozenQuestion(results, item);
      return [identity.name, identity.modelId ?? "", identity.provider ?? "", item.variant, prompt?.code ?? item.question_id, prompt?.prompt ?? "", item.repeat_no, item.status, scoreTotal(item.final_score) ?? "", item.latency_ms ?? "", item.cost ?? "", judge?.name ?? "", judge?.modelId ?? "", judge?.provider ?? "", item.answer ?? ""];
    })];
    download(new Blob(["\uFEFF" + rows.map((row) => row.map(csvCell).join(",")).join("\n")], { type: "text/csv;charset=utf-8" }), safeName(results.batch.name) + "-qa-results.csv");
  };
  const exportMarkdown = () => download(new Blob([createMarkdownReport(results, profileFallback)], { type: "text/markdown;charset=utf-8" }), safeName(results.batch.name) + "-评测报告.md");

  return (
    <div className="min-w-0">
      {!currentRunId && results.batch.scoring?.source !== "pronoia_base_evaluation" && <p className="border-b border-edge bg-paper px-5 py-3 text-xs leading-relaxed text-mute">历史模型评测：本批次保留创建时的预测流程与问答路径。旧单 Agent 或 Raw API 结果不会改写为新版 Pronoia 多 Agent 成绩。</p>}
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-edge px-5 py-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="font-serif text-[17px] font-semibold text-ink">{currentRunId ? "问答测试结果" : results.batch.name}</h2>
            <LabStatus status={results.batch.status} hasWarnings={Boolean(results.batch.warning_count || results.batch.completion_quality === "completed_with_warnings")} />
            {batchIsDemo(results.batch) && <span className="rounded bg-amber-50 px-2 py-0.5 text-[10px] font-semibold text-amber-700">编排演示 · 不计入正式评测</span>}
          </div>
          <p className="mt-1 text-[11px] text-mute">预测与问答独立出结果；问答不参与收益和 Arena 排名。</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" onClick={exportMarkdown} className="inline-flex items-center gap-1.5 rounded-lg border border-violet/20 bg-violet-soft/35 px-3 py-2 text-[11px] font-medium text-violet"><FileText size={12} />导出报告</button>
          <button type="button" onClick={exportCsv} disabled={!results.qa_results.length} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-paper px-3 py-2 text-[11px] font-medium text-mute disabled:opacity-35"><Download size={12} />CSV</button>
          <button type="button" onClick={exportJson} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-paper px-3 py-2 text-[11px] font-medium text-mute"><FileJson2 size={12} />JSON</button>
        </div>
      </div>

      <div className="space-y-6 p-4 sm:p-5">
        {!currentRunId && predictionComparisons.length > 0 && (
          <section aria-label="事件方向与收益率预测汇总">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div><h3 className="text-[13px] font-semibold text-ink">事件方向与收益率预测</h3><p className="mt-1 text-[11px] leading-relaxed text-mute">新评测只判断看涨或看跌。方向准确率仅统计有实际方向标签的有效预测，数据不足与技术错误单独列出；请结合有效样本数和输出有效率比较。历史批次保留原有口径。</p></div>
              <span className="text-[11px] text-mute">{predictionComparisons.length} 个预测任务</span>
            </div>
            <div className="mt-3 overflow-x-auto rounded-xl border border-edge">
              <table className="w-full min-w-[1050px] text-left text-[11px]">
                <thead className="border-b border-edge bg-paper text-[10px] text-mute"><tr><th className="px-4 py-3 font-medium">选手 / 基模</th><th className="px-3 py-3 font-medium">方向准确率</th><th className="px-3 py-3 font-medium">输出质量</th><th className="px-3 py-3 font-medium">收益率 MAE</th><th className="px-3 py-3 font-medium">收益率 RMSE</th><th className="px-3 py-3 font-medium">数值预测覆盖率</th><th className="px-3 py-3 font-medium">状态 / 结果</th></tr></thead>
                <tbody className="divide-y divide-edge">
                  {predictionComparisons.map((item) => <tr key={item.taskId} className="align-top">
                    <td className="max-w-[250px] px-4 py-3"><p className="break-words font-semibold text-ink">{item.label}</p><p className="mt-1 break-words text-[10px] text-mute">{item.identity.name} · {providerLabel(item.identity.provider)}</p><p className="mt-1 text-[10px] text-faint">{item.horizonLabel} · {item.runnerLabel}</p>{item.demo && <span className="mt-1.5 inline-block rounded bg-amber-50 px-1.5 py-0.5 text-[9px] text-amber-700">编排演示</span>}{item.datasetDemo && <span className="mt-1.5 inline-block rounded bg-amber-50 px-1.5 py-0.5 text-[9px] text-amber-700">合成数据 · 仅验证流程</span>}</td>
                    <td className="px-3 py-3"><p className="font-mono font-semibold text-ink">{predictionPercent(item.directionAccuracy)}</p><p className="mt-1 text-[10px] text-mute">{item.directionLabel}</p><p className="mt-1 text-[10px] text-faint">{item.directionCount === null ? "尚无样本统计" : `${item.directionCount} 个有效样本`}</p></td>
                    <td className="px-3 py-3"><p className="font-mono font-semibold text-ink">{predictionPercent(item.outputValidity)}</p><p className="mt-1 text-[10px] text-faint">{item.validCount !== null && item.outputCount !== null ? `${item.validCount} / ${item.outputCount} 条有效输出` : "尚无输出质量统计"}</p><p className={cls("mt-1 text-[10px]", (item.insufficientCount ?? 0) > 0 ? "text-amber-700" : "text-mute")}>数据不足 {item.insufficientCount ?? "—"}</p><p className={cls("mt-1 text-[10px]", (item.invalidCount ?? 0) > 0 ? "text-rise" : "text-mute")}>技术错误 / 无效输出 {item.invalidCount ?? "—"}</p>{(item.voluntaryCount ?? 0) > 0 && <p className="mt-1 text-[10px] text-mute">主动弃权 {item.voluntaryCount}</p>}</td>
                    <td className="px-3 py-3"><p className="font-mono font-semibold text-ink">{predictionPoints(item.mae)}</p><p className="mt-1 text-[10px] text-faint">{item.evaluatedCount === null ? "尚无可用成绩" : `${item.evaluatedCount} 个已评估样本`}</p></td>
                    <td className="px-3 py-3"><p className="font-mono font-semibold text-ink">{predictionPoints(item.rmse)}</p><p className="mt-1 text-[10px] text-faint">百分点</p></td>
                    <td className="px-3 py-3"><p className="font-mono font-semibold text-ink">{predictionPercent(item.coverage)}</p><p className="mt-1 text-[10px] text-faint">{item.forecastCount !== null && item.predictionCount !== null ? `${item.forecastCount} / ${item.predictionCount} 条预测` : "尚无数值预测统计"}</p></td>
                    <td className="px-3 py-3"><LabStatus status={item.status} hasWarnings={item.hasWarnings} />{item.metricsError && <p className="mt-2 max-w-[180px] break-words text-[10px] text-rise">指标计算失败：{item.metricsError}</p>}{item.runId && <button type="button" onClick={() => openBTDetail(item.runId!)} className="mt-2 block whitespace-nowrap text-[11px] font-medium text-brand">打开运行结果 →</button>}</td>
                  </tr>)}
                </tbody>
              </table>
            </div>
            <p className="mt-2 text-[10px] leading-relaxed text-faint">输出有效率 = 有效输出 / 已返回输出；数值预测覆盖率 = 有效收益率数值预测 / 预测总数。MAE、RMSE 以百分点计，仅统计已有对应实际收益的样本，越低越好。尚未评估、未记录和演示成绩显示“—”；不同数据版本、窗口或协议分别比较。</p>
            <div className="mt-3 space-y-2">
              {predictionComparisons.filter((item) => item.issues.length || (item.insufficientCount ?? 0) + (item.invalidCount ?? 0) + (item.voluntaryCount ?? 0) > 0).map((item) => <details key={item.taskId} className="rounded-lg border border-amber-200 bg-amber-50/50 px-3 py-2.5">
                <summary className="cursor-pointer text-[11px] font-medium text-ink">{item.label} · 查看数据不足及技术错误原因</summary>
                {item.issues.length ? <ul className="mt-2 space-y-2 text-[11px] leading-relaxed text-mute">{item.issues.map((issue, index) => <li key={`${issue.event_id ?? issue.symbol ?? "issue"}:${index}`}><span className={cls("font-medium", issue.prediction_status === "insufficient_data" ? "text-amber-700" : "text-rise")}>{predictionIssueLabel(issue.prediction_status)}</span><span> · {issue.symbol ?? issue.event_id ?? "事件"}：{issue.reason || "未记录详细原因，请打开运行结果查看日志。"}</span></li>)}</ul> : <p className="mt-2 text-[11px] text-mute">本批次未记录分项原因，请打开运行结果查看事件日志。</p>}
                {item.issuesTruncated && <p className="mt-2 text-[10px] text-faint">此处仅显示部分原因，完整明细请查看运行结果。</p>}
              </details>)}
            </div>
          </section>
        )}
        <section aria-label="独立任务状态">
          <h3 className="text-[12px] font-semibold text-ink">独立任务状态</h3>
          <div className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
            {visibleTasks.map((task) => {
              const progress = Math.min(100, Math.max(0, Math.round((task.progress ?? 0) * 100)));
              const identity = taskIdentity(task);
              const prediction = predictionComparisons.find((item) => item.taskId === task.id);
              return <div key={task.id} className="min-w-0 rounded-xl border border-edge bg-paper p-3.5">
                <div className="flex flex-wrap items-start justify-between gap-2"><div className="min-w-0"><p className="break-words text-[12px] font-semibold text-ink">{task.kind === "prediction" ? "预测" : "问答"} · {prediction?.label ?? identity.name}</p><p className="mt-1 break-all font-mono text-[10px] text-faint">{providerLabel(identity.provider)} · {identity.modelId || "未知模型"}</p></div><LabStatus status={task.status} hasWarnings={prediction?.hasWarnings} /></div>
                <div className="mt-3 flex items-center justify-between text-[11px] text-mute"><span>{task.done_items} / {task.total_items}</span><span>{progress}%</span></div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-edge"><div className={cls("h-full rounded-full", task.status === "done" ? "bg-jade" : task.status === "failed" ? "bg-rise" : "bg-violet")} style={{ width: progress + "%" }} /></div>
                {task.error_msg && <p className="mt-2 break-words text-[11px] text-rise">{task.error_msg}</p>}
                {task.backtest_run_id && task.backtest_run_id !== currentRunId && <button type="button" onClick={() => openBTDetail(task.backtest_run_id!)} className="mt-3 text-[11px] font-medium text-brand">打开预测运行结果 →</button>}
              </div>;
            })}
          </div>
        </section>

        {!results.qa_results.length ? <div className="rounded-xl border border-dashed border-edge px-5 py-8 text-center text-[12px] leading-relaxed text-mute">{qaEnabled ? "问答测试已创建，尚未产生回答。回答生成与评分完成后会分别更新结果。" : "本批次未选择问答测试，仅运行预测。"}</div> : <>
          <section aria-label="问答汇总">
            <div className="flex flex-wrap items-center justify-between gap-2"><div><h3 className="text-[13px] font-semibold text-ink">问答对比</h3><p className="mt-1 text-[11px] leading-relaxed text-mute">按八个维度评分，总分 100。已保存的人工评分优先；尚未评分的回答不计入平均分。</p></div><span className="text-[11px] text-mute">{results.qa_results.length} 条回答</span></div>
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              {comparisons.map((item) => <div key={item.profileId + ":" + item.variant} className="min-w-0 rounded-xl border border-edge bg-paper p-4">
                <div className="flex items-start justify-between gap-3"><div className="min-w-0"><p className="break-words text-[12px] font-semibold text-ink">{qaEntrantLabel(item.profileName, item.modelId, item.variant)}</p><p className="mt-1 break-all font-mono text-[10px] text-faint">{providerLabel(item.provider)} · {item.modelId || "未知模型"}</p>{item.judgeProfileName && <p className="mt-1 text-[10px] text-mute">评分模型：{item.judgeProfileName}{item.judgeModelId ? " · " + item.judgeModelId : ""}</p>}</div><p className="shrink-0 font-mono text-[19px] font-semibold text-ink">{item.averageScore == null ? "—" : item.averageScore.toFixed(1)}<span className="ml-0.5 text-[10px] font-normal text-faint">/100</span></p></div>
                <div className="mt-3 h-2 overflow-hidden rounded-full bg-edge"><div className={cls("h-full rounded-full", item.variant === "pronoia" ? "bg-violet" : "bg-mute")} style={{ width: Math.min(100, Math.max(0, item.averageScore ?? 0)) + "%" }} /></div>
                <div className="mt-2 flex flex-wrap justify-between gap-2 text-[11px] text-mute"><span>已评分 {item.scored}/{item.count}</span><span>{item.averageLatency == null ? "延迟 —" : "平均 " + (item.averageLatency / 1000).toFixed(1) + "s"}</span></div>
              </div>)}
            </div>
            <div className="mt-4 overflow-x-auto rounded-xl border border-edge">
              <table className="w-full min-w-[940px] border-collapse text-left">
                <caption className="border-b border-edge px-4 py-3 text-left text-[12px] font-semibold text-ink">八维平均得分 <span className="ml-2 text-[10px] font-normal text-mute">按模型与回答路径聚合</span></caption>
                <thead><tr className="border-b border-edge bg-paper text-[10px] text-mute"><th className="sticky left-0 z-10 min-w-[140px] bg-paper px-3 py-2.5 font-semibold">API / 路径</th>{DIMENSIONS.map((dimension) => <th key={dimension.key} className="min-w-[96px] px-2 py-2.5 text-center font-semibold">{dimension.label}<span className="ml-0.5 font-normal">/{dimension.max}</span></th>)}</tr></thead>
                <tbody>{comparisons.map((item) => <tr key={"dimensions:" + item.profileId + ":" + item.variant} className="border-b border-edge/60 last:border-b-0"><td className="sticky left-0 z-10 bg-card px-3 py-3"><p className="max-w-[160px] truncate text-[11px] font-medium text-ink" title={item.profileName}>{item.profileName}</p><p className="mt-0.5 text-[10px] text-faint">{variantLabel(item.variant)}</p></td>{DIMENSIONS.map((dimension) => { const value = item.dimensionAverages[dimension.key]; return <td key={dimension.key} className="px-2 py-3 text-center"><p className="font-mono text-[11px] font-semibold text-ink">{value == null ? "—" : value.toFixed(1)}</p><div className="mx-auto mt-1.5 h-1 w-full max-w-[64px] overflow-hidden rounded-full bg-edge"><div className={cls("h-full rounded-full", item.variant === "pronoia" ? "bg-violet" : "bg-mute")} style={{ width: (value == null ? 0 : Math.min(100, Math.max(0, value / dimension.max * 100))) + "%" }} /></div></td>; })}</tr>)}</tbody>
              </table>
            </div>
          </section>

          <section aria-label="回答明细">
            <h3 className="text-[13px] font-semibold text-ink">回答明细</h3>
            <div className="mt-3 overflow-x-auto rounded-xl border border-edge">
              <table className="w-full min-w-[780px] border-collapse text-left">
                <thead><tr className="border-b border-edge bg-paper text-[10px] text-mute"><th className="px-3 py-2.5">API / 路径</th><th className="px-3 py-2.5">问题</th><th className="px-3 py-2.5">状态</th><th className="px-3 py-2.5">评分</th><th className="px-3 py-2.5">耗时</th><th className="px-3 py-2.5 text-right">操作</th></tr></thead>
                <tbody>{results.qa_results.map((item) => {
                  const identity = qaIdentity(item);
                  const itemQuestion = frozenQuestion(results, item);
                  return <tr key={item.id} className={cls("border-b border-edge/60 last:border-b-0", item.question_id === questionId && "bg-violet-soft/15")}>
                    <td className="px-3 py-3"><p className="max-w-[180px] truncate text-[11px] font-medium text-ink" title={identity.name}>{identity.name}</p><p className="mt-0.5 text-[10px] text-faint">{variantLabel(item.variant)} · 第 {item.repeat_no} 次</p></td>
                    <td className="max-w-[280px] px-3 py-3"><p className="text-[10px] font-semibold text-mute">{itemQuestion?.code ?? item.question_id.split(":").pop()}</p><p className="mt-0.5 line-clamp-2 text-[11px] leading-relaxed text-mute">{itemQuestion?.prompt ?? "历史结果未记录题干"}</p></td>
                    <td className="px-3 py-3"><LabStatus status={item.status} /></td>
                    <td className="px-3 py-3"><p className="font-mono text-[12px] font-semibold text-ink">{scoreTotal(item.final_score) ?? "—"}</p><p className="mt-0.5 text-[10px] text-faint">{item.manual_score ? "人工评分" : item.auto_score ? "模型自动评分" : "未评分"}</p></td>
                    <td className="whitespace-nowrap px-3 py-3 font-mono text-[11px] text-mute">{item.latency_ms == null ? "—" : (item.latency_ms / 1000).toFixed(1) + "s"}</td>
                    <td className="px-3 py-3 text-right"><div className="flex justify-end gap-2"><button type="button" onClick={() => { setSelectedQuestionId(item.question_id); setSelectedRepeat(item.repeat_no); document.getElementById("qa-answer-comparison")?.scrollIntoView({ behavior: "smooth", block: "start" }); }} className="rounded-lg border border-violet/20 bg-violet-soft/30 px-2.5 py-1.5 text-[10px] font-medium text-violet">查看回答</button><button type="button" onClick={() => setManualResult(item)} disabled={!canManuallyScore(item)} className="rounded-lg border border-edge bg-paper px-2.5 py-1.5 text-[10px] font-medium text-mute disabled:opacity-35">评分</button></div></td>
                  </tr>;
                })}</tbody>
              </table>
            </div>
          </section>

          <section id="qa-answer-comparison" className="scroll-mt-5" aria-label="逐题回答对照">
            <div className="flex flex-wrap items-center justify-between gap-3"><div><h3 className="text-[13px] font-semibold text-ink">逐题回答对照</h3><p className="mt-1 text-[11px] text-mute">{results.batch.scoring?.source === "pronoia_base_evaluation" ? "同一道题、同一次重复，对照不同基模下 Pronoia 的完整回答。" : "同一道题、同一次重复，按实际模型与回答路径对照完整回答。"}</p></div><div className="flex min-w-0 flex-wrap gap-2"><select aria-label="选择查看的问题" value={questionId} onChange={(event) => setSelectedQuestionId(event.target.value)} className={cls(INPUT, "w-auto max-w-[260px]")}>{questionIds.map((id) => { const answer = results.qa_results.find((item) => item.question_id === id)!; const itemQuestion = frozenQuestion(results, answer); return <option key={id} value={id}>{itemQuestion?.code ?? id.split(":").pop()} · {(itemQuestion?.scenario ?? itemQuestion?.prompt ?? "历史题目").slice(0, 35)}</option>; })}</select><select aria-label="重复次数" value={repeatNo} onChange={(event) => setSelectedRepeat(Number(event.target.value))} className={cls(INPUT, "w-auto")}>{repeatNumbers.map((value) => <option key={value} value={value}>第 {value} 次</option>)}</select></div></div>
            <QuestionContext question={question} />
            <div className="mt-3 grid items-start gap-3 lg:grid-cols-2">
              {shownAnswers.map((item) => {
                const identity = qaIdentity(item);
                const judge = judgeIdentity(item, results.batch.qa?.judge_profile_id, profileFallback);
                return <article key={item.id} className={cls("min-w-0 overflow-hidden rounded-xl border bg-card", item.variant === "pronoia" ? "border-violet/25" : "border-edge")}>
                  <div className={cls("flex flex-wrap items-start justify-between gap-2 border-b border-edge px-4 py-3", item.variant === "pronoia" ? "bg-violet-soft/20" : "bg-paper")}><div className="min-w-0"><p className="text-[12px] font-semibold text-ink">{qaEntrantLabel(identity.name, identity.modelId, item.variant)}</p><p className="mt-1 break-all font-mono text-[10px] text-mute">{providerLabel(identity.provider)} · {identity.modelId}</p></div><LabStatus status={item.status} /></div>
                  <div className="max-h-[480px] overflow-auto break-words p-4">{item.answer ? <Markdown text={item.answer} compact /> : <p className="py-6 text-center text-[12px] text-mute">{item.status === "failed" ? "回答生成失败" : "尚未返回回答"}</p>}</div>
                  {item.error_msg && <div className="mx-4 mb-3 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[11px] leading-relaxed text-rise">{item.error_msg}{item.answer && <p className="mt-1">{item.manual_score ? "已由人工完成复核；保留原自动评测错误供审计。" : "已有回答保留，可进行人工评分。"}</p>}</div>}
                  <div className="border-t border-edge bg-paper/50 p-4"><div className="flex flex-wrap items-center justify-between gap-2"><div><span className="font-mono text-[18px] font-semibold text-ink">{scoreTotal(item.final_score) ?? "—"}<span className="ml-0.5 text-[10px] font-normal text-faint">/100</span></span><span className="ml-2 text-[10px] text-mute">{item.manual_score ? "人工评分" : item.auto_score ? "模型自动评分" : "未评分"}</span></div><button type="button" onClick={() => setManualResult(item)} disabled={!canManuallyScore(item)} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[11px] font-medium text-mute disabled:opacity-35"><PencilLine size={12} />{item.manual_score ? "修改评分" : "人工评分"}</button></div><p className="mt-1 text-[10px] text-faint">{judge ? "评分模型：" + judge.name + " · " + (judge.modelId ?? "") : "未记录评分模型"}</p>
                    <div className="mt-3 grid grid-cols-4 gap-2">{DIMENSIONS.map((dimension) => <div key={dimension.key} className="rounded-lg border border-edge bg-card px-2 py-2 text-center"><p className="text-[9px] text-mute">{dimension.label}</p><p className="mt-1 font-mono text-[11px] text-ink">{item.final_score?.[dimension.key] ?? "—"}<span className="text-[9px] text-faint">/{dimension.max}</span></p></div>)}</div>
                    {item.final_score?.major_error && <p className="mt-3 flex items-center gap-1.5 text-[11px] font-medium text-rise"><AlertTriangle size={12} />评分标记：存在重大错误</p>}
                    {item.final_score?.rationale && <p className="mt-3 whitespace-pre-wrap text-[11px] leading-relaxed text-mute">评分理由：{item.final_score.rationale}</p>}
                    {item.auto_score && item.manual_score && <details className="mt-3 text-[11px] text-mute"><summary className="cursor-pointer font-medium">查看原自动评分 · {scoreTotal(item.auto_score) ?? "—"} /100</summary><p className="mt-2 whitespace-pre-wrap leading-relaxed">{item.auto_score.rationale || "未记录理由"}</p></details>}
                  </div>
                </article>;
              })}
            </div>
          </section>
        </>}
      </div>
      {manualResult && <ManualScoreDialog result={manualResult} question={manualQuestion} onClose={() => setManualResult(null)} onSaved={() => { setManualResult(null); onUpdated(); }} />}
    </div>
  );
}

function variantLabel(variant: string): string {
  return variant === "pronoia" ? "Pronoia" : variant === "raw" ? "Raw API" : variant;
}

function qaEntrantLabel(name: string, modelId: string | null, variant: string): string {
  return variant === "pronoia" ? `Pronoia（${modelId ?? name}）` : `${name} · ${variantLabel(variant)}`;
}

function canManuallyScore(result: ModelLabQAResult): boolean {
  return !["pending", "running", "cancelled", "demo"].includes(result.status) && Boolean(result.answer?.trim());
}

function frozenQuestion(results: ModelLabResults, answer: ModelLabQAResult): ModelLabQuestion | null {
  const enriched = (answer as ModelLabQAResult & { question?: ModelLabQuestion | null }).question;
  if (enriched?.prompt) return enriched;
  const task = results.tasks.find((item) => item.id === answer.task_id);
  const frozen = task?.config?.questions;
  if (!Array.isArray(frozen)) return null;
  const found = frozen.find((item: unknown) => {
    if (!item || typeof item !== "object") return false;
    const value = item as Record<string, unknown>;
    return value.id === answer.question_id || value.code === answer.question_id;
  });
  return found && typeof found.prompt === "string" ? found as ModelLabQuestion : null;
}

function QuestionContext({ question }: { question: ModelLabQuestion | null }) {
  return <div className="mt-3 rounded-xl border border-edge bg-paper p-4">
    <p className="text-[10px] font-semibold text-violet">{question?.code ?? "历史题目"}{question?.category ? " · " + question.category : ""} · 创建时冻结</p>
    <p className="mt-2 whitespace-pre-wrap text-[12px] leading-relaxed text-ink">{question?.prompt ?? "该历史结果未保存题干，回答和分数仍可查看。"}</p>
    {question && <details className="mt-3 text-[11px] text-mute"><summary className="inline-flex cursor-pointer items-center gap-1.5 font-medium"><ChevronDown size={12} />评分依据与期望交付</summary><dl className="mt-3 grid gap-3 sm:grid-cols-2">{([["用户角色", question.role], ["方法 / 场景", [question.method, question.scenario].filter(Boolean).join(" · ")], ["期望交付", question.deliverable], ["核验 / 金标准", question.gold_standard], ["核心指标", question.metrics], ["风险等级", question.risk]] as const).filter(([, value]) => value).map(([label, value]) => <div key={label}><dt className="text-[10px] text-faint">{label}</dt><dd className="mt-1 whitespace-pre-wrap leading-relaxed">{value}</dd></div>)}</dl></details>}
  </div>;
}

const SCORE_DIMENSION_KEYS = ["fact", "evidence", "method", "reasoning", "risk", "usability", "reproducibility", "user_value"] as const;
type ScoreDimensionKey = typeof SCORE_DIMENSION_KEYS[number];

const DIMENSIONS: Array<{ key: ScoreDimensionKey; label: string; max: number }> = [
  { key: "fact", label: "事实准确", max: 30 }, { key: "evidence", label: "证据质量", max: 20 },
  { key: "method", label: "方法质量", max: 15 }, { key: "reasoning", label: "推理", max: 10 },
  { key: "risk", label: "风险意识", max: 10 }, { key: "usability", label: "可用性", max: 5 },
  { key: "reproducibility", label: "可复现", max: 5 }, { key: "user_value", label: "用户价值", max: 5 },
];

function ManualScoreDialog({
  result, question, onClose, onSaved,
}: {
  result: ModelLabQAResult;
  question: ModelLabQuestion | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const existing = result.manual_score ?? result.auto_score;
  const [score, setScore] = useState<ModelLabManualScoreInput>({
    fact: Number(existing?.fact ?? 0), evidence: Number(existing?.evidence ?? 0),
    method: Number(existing?.method ?? 0), reasoning: Number(existing?.reasoning ?? 0),
    risk: Number(existing?.risk ?? 0), usability: Number(existing?.usability ?? 0),
    reproducibility: Number(existing?.reproducibility ?? 0), user_value: Number(existing?.user_value ?? 0),
    major_error: Boolean(existing?.major_error), rationale: String(existing?.rationale ?? ""),
    reviewer: String(existing?.reviewer ?? ""),
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const previousFocus = useRef(typeof document === "undefined" ? null : document.activeElement);
  const closeRef = useRef(onClose);
  const savingRef = useRef(saving);
  closeRef.current = onClose;
  savingRef.current = saving;
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !savingRef.current) {
        event.preventDefault(); closeRef.current();
      }
      if (event.key !== "Tab") return;
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>("button:not(:disabled), input:not(:disabled), textarea:not(:disabled), select:not(:disabled), summary, a[href]") ?? []).filter((node) => node.offsetParent !== null);
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first && last) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last && first) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (previousFocus.current instanceof HTMLElement) previousFocus.current.focus();
    };
  }, []);
  const total = DIMENSIONS.reduce((sum, dimension) => sum + Number(score[dimension.key] ?? 0), 0);
  const save = async () => {
    setSaving(true); setError(null);
    try { await api.modelLabManualScore(result.id, score); onSaved(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setSaving(false); }
  };
  return <div ref={dialogRef} className="fixed inset-0 z-[90] grid place-items-center overflow-y-auto bg-ink/35 p-3 backdrop-blur-sm sm:p-5" role="dialog" aria-modal="true" aria-labelledby="qa-review-title">
    <div className="flex max-h-[90vh] w-full max-w-5xl flex-col overflow-hidden rounded-2xl border border-edge bg-card shadow-pop">
      <div className="flex shrink-0 items-center justify-between gap-3 border-b border-edge px-5 py-4"><div><h3 id="qa-review-title" className="font-serif text-[18px] font-semibold text-ink">人工八维评分 · {total}/100</h3><p className="mt-1 text-[11px] text-mute">先阅读题干与回答。保存后以人工评分为准，保留原自动评分。</p></div><button type="button" onClick={onClose} disabled={saving} aria-label="关闭人工评分" className="rounded-lg p-2 text-mute hover:bg-edge/60 disabled:opacity-40"><X size={16} /></button></div>
      <div className="min-h-0 overflow-y-auto p-4 sm:p-5">
        <div className="grid items-start gap-5 lg:grid-cols-2">
          <section className="min-w-0" aria-label="待评分回答">
            <h4 className="text-[12px] font-semibold text-ink">{variantLabel(result.variant)} · 第 {result.repeat_no} 次回答</h4>
            <QuestionContext question={question} />
            <div className="mt-3 max-h-[460px] overflow-auto break-words rounded-xl border border-edge bg-paper/30 p-4"><Markdown text={result.answer ?? "没有可供评分的回答"} compact /></div>
          </section>
          <section className="space-y-4" aria-label="评分表">
            {!existing && <p className="rounded-lg bg-violet-soft/40 px-3 py-2 text-[11px] text-mute">以下为本次人工评分草稿，点击保存后才会计入结果。</p>}
            <div className="grid grid-cols-2 gap-3">{DIMENSIONS.map((dimension) => <label key={dimension.key} className="rounded-xl border border-edge bg-paper px-3 py-2.5"><span className="flex items-center justify-between gap-2 text-[11px] text-mute"><span>{dimension.label}</span><span>0–{dimension.max}</span></span><input type="number" min={0} max={dimension.max} step={1} value={String(score[dimension.key])} onChange={(event) => setScore({ ...score, [dimension.key]: Math.min(dimension.max, Math.max(0, Math.round(Number(event.target.value) || 0))) })} className="mt-2 w-full rounded-md border border-edge bg-card px-2 py-1.5 font-mono text-[14px] font-semibold text-ink outline-none focus:border-violet" /></label>)}</div>
            <label className="flex items-start gap-2 rounded-xl border border-edge bg-paper px-3 py-3 text-[11px] text-ink"><input type="checkbox" checked={score.major_error} onChange={(event) => setScore({ ...score, major_error: event.target.checked })} className="mt-0.5" /><span>存在重大错误<span className="mt-1 block text-[10px] leading-relaxed text-mute">例如伪造来源、关键数值错误、把未来当事实、泄露隐私或危险的无条件交易指令。</span></span></label>
            <Field label="评分理由"><textarea autoFocus value={score.rationale} onChange={(event) => setScore({ ...score, rationale: event.target.value })} className={cls(INPUT, "min-h-24 resize-y")} placeholder="填写事实、证据及各维度的加减分依据" /></Field>
            <Field label="评分人（可选）"><input value={score.reviewer ?? ""} onChange={(event) => setScore({ ...score, reviewer: event.target.value })} className={INPUT} /></Field>
            {error && <div role="alert" className="rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[11px] text-rise">{error}</div>}
            <button type="button" onClick={() => void save()} disabled={saving || !score.rationale.trim()} className="inline-flex w-full items-center justify-center gap-2 rounded-xl bg-ink px-4 py-3 text-[12px] font-semibold text-card disabled:opacity-40">{saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}保存人工评分</button>
          </section>
        </div>
      </div>
    </div>
  </div>;
}

export function LabStatus({ status, prefix = "", hasWarnings = false }: { status: string; prefix?: string; hasWarnings?: boolean }) {
  const active = ["running", "queued", "starting", "cancelling"].includes(status);
  const completedWithWarnings = status === "done" && hasWarnings;
  const style = completedWithWarnings ? "bg-amber-50 text-amber-700" : status === "done" ? "bg-jade-soft text-jade" : active ? "bg-violet-soft text-violet" : status === "failed" ? "bg-rise/5 text-rise" : status === "cancelled" ? "bg-edge text-mute" : status === "partial" ? "bg-amber-50 text-amber-700" : "bg-paper text-mute";
  return <span className={cls("inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full px-2 py-1 text-[10px] font-medium", style)}>{active ? <Loader2 size={10} className="animate-spin" /> : completedWithWarnings ? <AlertTriangle size={10} /> : status === "done" ? <CheckCircle2 size={10} /> : <CircleDashed size={10} />}{prefix}{completedWithWarnings ? "已完成 · 有问题" : statusLabel(status)}</span>;
}

export function batchNeedsRefresh(status: string): boolean {
  return ["pending", "queued", "starting", "running", "cancelling"].includes(status);
}

function statusLabel(status: string) { return ({ pending: "待启动", queued: "排队中", starting: "启动中", running: "运行中", cancelling: "正在取消", done: "已完成", failed: "失败", partial: "部分完成", cancelled: "已取消", demo: "演示结果", awaiting_manual: "待人工评分" } as Record<string, string>)[status] ?? status; }

export function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) { return <label className="block"><span className="flex items-baseline justify-between gap-2 text-[9.5px] font-medium text-ink">{label}{hint && <span className="text-[8px] font-normal text-faint">{hint}</span>}</span><span className="mt-1.5 block">{children}</span></label>; }

export function Empty({ title, note, icon }: { title: string; note: string; icon: ReactNode }) { return <div className="grid min-h-64 place-items-center px-6 text-center"><div><span className="mx-auto grid h-10 w-10 place-items-center rounded-xl bg-edge/55 text-mute">{icon}</span><h3 className="mt-3 font-serif text-[13px] font-semibold text-ink">{title}</h3><p className="mt-1 max-w-sm text-[9.5px] leading-relaxed text-mute">{note}</p></div></div>; }

function scoreTotal(score?: ModelLabScore | null): number | null {
  if (!score) return null;
  if (typeof score.total === "number") return score.total;
  const keys = ["fact", "evidence", "method", "reasoning", "risk", "usability", "reproducibility", "user_value"] as const;
  const values = keys.map((key) => score[key]).filter((value): value is number => typeof value === "number");
  return values.length ? values.reduce((sum, value) => sum + value, 0) : null;
}

export function batchIsDemo(batch: ModelLabBatch): boolean {
  return Boolean(batch.demo || batch.scoring?.demo || batch.scoring?.dry_run || batch.tasks?.some((task) => task.demo || task.result_summary?.demo));
}

type ProfileFallback = (id: string) => ModelLabProfile | undefined;
type ProfileProvenance = { profile_id?: string | null; profile_name?: string | null; model_id?: string | null; provider?: string | null };
type JudgeProvenance = { judge_profile_id?: string | null; judge_profile_name?: string | null; judge_model_id?: string | null; judge_provider?: string | null };
type ProfileIdentity = { id: string; name: string; modelId: string | null; provider: string | null };
type PredictionComparison = {
  taskId: string;
  runId: string | null;
  identity: ProfileIdentity;
  label: string;
  runnerLabel: string;
  status: string;
  hasWarnings: boolean;
  demo: boolean;
  datasetDemo: boolean;
  horizonLabel: string;
  directionLabel: string;
  directionAccuracy: number | null;
  directionCount: number | null;
  outputValidity: number | null;
  validCount: number | null;
  outputCount: number | null;
  insufficientCount: number | null;
  invalidCount: number | null;
  voluntaryCount: number | null;
  metricsError: string | null;
  issues: NonNullable<NonNullable<ModelLabResults["summary"]["prediction_runs"]>[number]["prediction_issues"]>;
  issuesTruncated: boolean;
  mae: number | null;
  rmse: number | null;
  coverage: number | null;
  forecastCount: number | null;
  predictionCount: number | null;
  evaluatedCount: number | null;
};

function predictionRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function predictionNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function predictionPercent(value: number | null): string {
  return value === null ? "—" : `${(value * 100).toFixed(1)}%`;
}

function predictionPoints(value: number | null): string {
  return value === null ? "—" : value.toFixed(3);
}

function predictionIssueLabel(status: string): string {
  return status === "insufficient_data" ? "数据不足" : status === "voluntary_abstain" ? "主动弃权" : "技术错误 / 无效输出";
}

export function buildPredictionComparisons(results: ModelLabResults, profileFallback: ProfileFallback): PredictionComparison[] {
  return (results.summary.prediction_runs ?? []).map((item) => {
    const task = results.tasks.find((candidate) => candidate.id === item.task_id);
    const config = predictionRecord(task?.config);
    const summary = predictionRecord(task?.result_summary);
    const identity = modelIdentity(item, item.profile_id, profileFallback);
    const runner = nonEmptyString(config.runner) ?? nonEmptyString(summary.runner) ?? results.batch.prediction?.runner ?? "";
    const metrics = predictionRecord(item.metrics);
    const forecastMetric = predictionRecord(metrics.return_forecast);
    const forecastMeta = predictionRecord(forecastMetric.meta);
    const forecast = Object.keys(forecastMeta).length ? forecastMeta : forecastMetric;
    const primaryMeta = predictionRecord(predictionRecord(metrics.acc_primary_non_neutral).meta);
    const horizon = nonEmptyString(metrics.primary_oracle_horizon) ?? nonEmptyString(forecast.horizon) ?? nonEmptyString(primaryMeta.primary_horizon) ?? nonEmptyString(config.horizon) ?? results.batch.prediction?.horizon ?? null;
    const directionMode = nonEmptyString(item.direction_mode) ?? nonEmptyString(metrics.direction_mode) ?? nonEmptyString(summary.direction_mode) ?? nonEmptyString(config.direction_mode);
    const binary = directionMode === "binary";
    // 新批次仅比较有效的 up/down，不能回退到会把数据不足计为错误的旧严格指标。
    const direction = binary
      ? predictionRecord(metrics.acc_primary_directional_trade)
      : horizon ? predictionRecord(metrics[`acc_${horizon}_strict`]) : {};
    const directionMeta = predictionRecord(direction.meta);
    const directionCount = predictionNumber(directionMeta.n) ?? predictionNumber(direction.n);
    const directionAccuracy = directionCount === 0 ? null : predictionNumber(direction.value) ?? predictionNumber(direction.acc);
    const predictionCount = predictionNumber(forecast.n_predictions);
    const forecastCount = predictionNumber(forecast.n_forecasts);
    const evaluatedCount = predictionNumber(forecast.evaluated_count) ?? predictionNumber(forecast.n);
    const coverage = predictionCount === 0 ? null : predictionNumber(forecast.coverage);
    const demo = Boolean(item.demo || task?.demo || summary.demo || results.batch.demo);
    const datasetDemo = Boolean(item.dataset_demo || summary.dataset_demo);
    const hideAbilityScores = demo || datasetDemo;
    const metricsError = nonEmptyString(item.metrics_error) ?? nonEmptyString(summary.metrics_error);
    const validCount = predictionNumber(item.valid_output_count) ?? predictionNumber(metrics.valid_output_count);
    const outputCount = predictionNumber(item.n_outputs) ?? predictionNumber(metrics.n_outputs);
    const insufficientCount = predictionNumber(item.insufficient_data_count) ?? predictionNumber(metrics.insufficient_data_count);
    const invalidCount = predictionNumber(item.invalid_output_count) ?? predictionNumber(metrics.invalid_output_count);
    const voluntaryCount = predictionNumber(item.voluntary_abstain_count) ?? predictionNumber(metrics.voluntary_abstain_count);
    const pronoia = runner === "team_full" || runner === "team_prompt";
    const label = pronoia ? `Pronoia（${identity.modelId ?? identity.name}）`
      : runner === "raw_model" ? `${identity.name}（直接调用）`
      : runner === "external_http" ? `${identity.name}（第三方预测服务）`
      : runner === "provided_analysis" ? `${identity.name}（导入预测）`
      : identity.name;
    const runnerLabel = runner === "team_full" ? "多 Agent"
      : runner === "team_prompt" ? "历史单 Agent 流程"
      : runner === "raw_model" ? "外部独立模型"
      : runner === "external_http" ? "第三方预测服务"
      : runner === "provided_analysis" ? "已有预测结果"
      : "执行路径未记录";
    return {
      taskId: item.task_id,
      runId: nonEmptyString(item.backtest_run_id) ?? nonEmptyString(item.bt_run_id) ?? nonEmptyString(task?.backtest_run_id),
      identity,
      label,
      runnerLabel,
      status: nonEmptyString(item.task_status) ?? task?.status ?? item.status,
      hasWarnings: Boolean(metricsError || item.warning_count || item.completion_quality === "completed_with_warnings" || (insufficientCount ?? 0) + (invalidCount ?? 0) + (voluntaryCount ?? 0) > 0),
      demo,
      datasetDemo,
      horizonLabel: horizon && /^t\d+$/i.test(horizon) ? `T+${horizon.slice(1)}` : horizon ?? "窗口未记录",
      directionLabel: binary ? "看涨 / 看跌" : "历史三分类（严格）",
      directionAccuracy: hideAbilityScores || metricsError ? null : directionAccuracy,
      directionCount: hideAbilityScores || metricsError ? null : directionCount,
      outputValidity: demo || outputCount === null || outputCount === 0 || validCount === null ? null : validCount / outputCount,
      validCount: demo ? null : validCount,
      outputCount: demo ? null : outputCount,
      insufficientCount: demo ? null : insufficientCount,
      invalidCount: demo ? null : invalidCount,
      voluntaryCount: demo ? null : voluntaryCount,
      metricsError,
      issues: item.prediction_issues ?? [],
      issuesTruncated: Boolean(item.issues_truncated),
      mae: hideAbilityScores || metricsError || evaluatedCount === 0 ? null : predictionNumber(forecast.mae_pct) ?? predictionNumber(forecastMetric.value),
      rmse: hideAbilityScores || metricsError || evaluatedCount === 0 ? null : predictionNumber(forecast.rmse_pct),
      coverage: demo || metricsError ? null : coverage,
      forecastCount: demo ? null : forecastCount,
      predictionCount: demo ? null : predictionCount,
      evaluatedCount: hideAbilityScores ? null : evaluatedCount,
    };
  });
}

type AggregatedComparison = {
  profileId: string;
  profileName: string;
  modelId: string | null;
  provider: string | null;
  judgeProfileId: string | null;
  judgeProfileName: string | null;
  judgeModelId: string | null;
  judgeProvider: string | null;
  variant: string;
  count: number;
  scored: number;
  averageScore: number | null;
  averageLatency: number | null;
  dimensionAverages: Record<ScoreDimensionKey, number | null>;
};

function modelIdentity(source: ProfileProvenance | null | undefined, fallbackId: string, fallback: ProfileFallback): ProfileIdentity {
  const id = nonEmptyString(source?.profile_id) ?? fallbackId;
  const live = id ? fallback(id) : undefined;
  return {
    id,
    name: nonEmptyString(source?.profile_name) ?? live?.name ?? (id ? id.slice(0, 9) : "未知模型"),
    modelId: nonEmptyString(source?.model_id) ?? nonEmptyString(live?.model_id),
    provider: nonEmptyString(source?.provider) ?? nonEmptyString(live?.provider),
  };
}

function judgeIdentity(source: JudgeProvenance | null | undefined, fallbackId: string | null | undefined, fallback: ProfileFallback): ProfileIdentity | null {
  const id = nonEmptyString(source?.judge_profile_id) ?? nonEmptyString(fallbackId) ?? "";
  const frozenName = nonEmptyString(source?.judge_profile_name);
  const frozenModelId = nonEmptyString(source?.judge_model_id);
  const frozenProvider = nonEmptyString(source?.judge_provider);
  if (!id && !frozenName && !frozenModelId && !frozenProvider) return null;
  const live = id ? fallback(id) : undefined;
  return {
    id,
    name: frozenName ?? live?.name ?? (id ? id.slice(0, 9) : "未知评分模型"),
    modelId: frozenModelId ?? nonEmptyString(live?.model_id),
    provider: frozenProvider ?? nonEmptyString(live?.provider),
  };
}

function taskSnapshotProvenance(task: ModelLabResults["tasks"][number]): ProfileProvenance {
  const raw = task.config?.profile_snapshot;
  const snapshot = raw && typeof raw === "object" && !Array.isArray(raw) ? raw as Record<string, unknown> : {};
  return {
    profile_id: nonEmptyString(snapshot.id) ?? task.profile_id,
    profile_name: nonEmptyString(snapshot.name),
    model_id: nonEmptyString(snapshot.model_id),
    provider: nonEmptyString(snapshot.provider),
  };
}

function aggregateResults(results: ModelLabQAResult[], summaryRows: ModelLabResults["summary"]["qa_comparison"], profileFallback: ProfileFallback): AggregatedComparison[] {
  type AggregateGroup = { identity: ProfileIdentity; judge: ProfileIdentity | null; profileId: string; variant: string; count: number; scores: number[]; latencies: number[]; dimensions: Record<ScoreDimensionKey, number[]> };
  const emptyDimensions = (): Record<ScoreDimensionKey, number[]> => ({ fact: [], evidence: [], method: [], reasoning: [], risk: [], usability: [], reproducibility: [], user_value: [] });
  const groups = new Map<string, AggregateGroup>();
  for (const result of results) {
    const key = `${result.profile_id}:${result.variant}`;
    const group = groups.get(key) ?? { identity: modelIdentity(result, result.profile_id, profileFallback), judge: judgeIdentity(result, null, profileFallback), profileId: result.profile_id, variant: result.variant, count: 0, scores: [], latencies: [], dimensions: emptyDimensions() };
    group.count += 1;
    const score = scoreTotal(result.final_score);
    if (score !== null) group.scores.push(score);
    for (const dimension of SCORE_DIMENSION_KEYS) {
      const value = result.final_score?.[dimension];
      if (typeof value === "number") group.dimensions[dimension].push(value);
    }
    if (typeof result.latency_ms === "number") group.latencies.push(result.latency_ms);
    groups.set(key, group);
  }

  const local = [...groups.values()].map((group): AggregatedComparison => ({
    profileId: group.profileId,
    profileName: group.identity.name,
    modelId: group.identity.modelId,
    provider: group.identity.provider,
    judgeProfileId: group.judge?.id || null,
    judgeProfileName: group.judge?.name ?? null,
    judgeModelId: group.judge?.modelId ?? null,
    judgeProvider: group.judge?.provider ?? null,
    variant: group.variant,
    count: group.count,
    scored: group.scores.length,
    averageScore: average(group.scores),
    averageLatency: average(group.latencies),
    dimensionAverages: Object.fromEntries(SCORE_DIMENSION_KEYS.map((key) => [key, average(group.dimensions[key])])) as Record<ScoreDimensionKey, number | null>,
  }));
  if (!summaryRows?.length) return local;

  const localByKey = new Map(local.map((item) => [`${item.profileId}:${item.variant}`, item]));
  return summaryRows.map((row): AggregatedComparison => {
    const fallbackRow = localByKey.get(`${row.profile_id}:${row.variant}`);
    const frozenIdentity: ProfileProvenance = {
      profile_id: row.profile_id,
      profile_name: nonEmptyString(row.profile_name) ?? fallbackRow?.profileName,
      model_id: nonEmptyString(row.model_id) ?? fallbackRow?.modelId,
      provider: nonEmptyString(row.provider) ?? fallbackRow?.provider,
    };
    const frozenJudge: JudgeProvenance = {
      judge_profile_id: nonEmptyString(row.judge_profile_id) ?? fallbackRow?.judgeProfileId,
      judge_profile_name: nonEmptyString(row.judge_profile_name) ?? fallbackRow?.judgeProfileName,
      judge_model_id: nonEmptyString(row.judge_model_id) ?? fallbackRow?.judgeModelId,
      judge_provider: nonEmptyString(row.judge_provider) ?? fallbackRow?.judgeProvider,
    };
    const identity = modelIdentity(frozenIdentity, row.profile_id, profileFallback);
    const judge = judgeIdentity(frozenJudge, null, profileFallback);
    return {
      profileId: row.profile_id,
      profileName: identity.name,
      modelId: identity.modelId ?? fallbackRow?.modelId ?? null,
      provider: identity.provider ?? fallbackRow?.provider ?? null,
      judgeProfileId: judge?.id || fallbackRow?.judgeProfileId || null,
      judgeProfileName: judge?.name ?? fallbackRow?.judgeProfileName ?? null,
      judgeModelId: judge?.modelId ?? fallbackRow?.judgeModelId ?? null,
      judgeProvider: judge?.provider ?? fallbackRow?.judgeProvider ?? null,
      variant: row.variant,
      count: row.answer_count,
      scored: row.scored_count,
      averageScore: typeof row.average_total === "number" ? row.average_total : fallbackRow?.averageScore ?? null,
      averageLatency: typeof row.average_latency_ms === "number" ? row.average_latency_ms : fallbackRow?.averageLatency ?? null,
      dimensionAverages: Object.fromEntries(SCORE_DIMENSION_KEYS.map((key) => {
        const value = row.dimension_averages?.[key];
        return [key, typeof value === "number" ? value : fallbackRow?.dimensionAverages[key] ?? null];
      })) as Record<ScoreDimensionKey, number | null>,
    };
  });
}

function average(values: number[]): number | null {
  return values.length ? values.reduce((sum, value) => sum + value, 0) / values.length : null;
}

function createMarkdownReport(results: ModelLabResults, profileFallback: ProfileFallback): string {
  const comparisons = aggregateResults(results.qa_results, results.summary.qa_comparison, profileFallback);
  const predictionByTask = new Map((results.summary.prediction_runs ?? []).map((item) => [item.task_id, item]));
  const lines: string[] = [
    `# ${markdownHeading(results.batch.name)} · Pronoia 模型评测报告`,
    "",
    `- 批次 ID：\`${markdownInline(results.batch.id)}\``,
    `- 状态：${markdownInline(statusLabel(results.batch.status))}`,
    `- 报告生成时间：${new Date().toLocaleString("zh-CN")}`,
    `- 结果性质：${batchIsDemo(results.batch) ? "编排演示（Demo，不可进入 Arena）" : "正式评测"}`,
    `- 被测 API：${results.batch.profile_ids.length}`,
    "",
    "## 独立任务状态",
    "",
    "| 测试 | 模型 API | Model ID | Provider | 状态 | 进度 | 回测记录 |",
    "| --- | --- | --- | --- | --- | ---: | --- |",
  ];

  if (results.tasks.length === 0) {
    lines.push("| — | — | — | — | 尚未生成任务 | — | — |");
  } else {
    for (const task of results.tasks) {
      const frozen = predictionByTask.get(task.id) ?? results.qa_results.find((result) => result.task_id === task.id) ?? taskSnapshotProvenance(task);
      const identity = modelIdentity(frozen, task.profile_id, profileFallback);
      const linkedRun = predictionByTask.get(task.id)?.backtest_run_id ?? task.backtest_run_id;
      lines.push(`| ${task.kind === "prediction" ? "预测" : task.kind === "qa" ? "问答" : markdownInline(task.kind)} | ${markdownInline(identity.name)} | ${markdownInline(identity.modelId)} | ${markdownInline(identity.provider)} | ${markdownInline(statusLabel(task.status))} | ${task.done_items}/${task.total_items} | ${markdownInline(linkedRun ?? "—")} |`);
    }
  }

  const predictionComparisons = buildPredictionComparisons(results, profileFallback);
  if (predictionComparisons.length) {
    lines.push(
      "", "## 事件方向与收益率预测", "",
      "新批次仅判断看涨或看跌，方向准确率只统计有效预测与实际方向标签；数据不足、技术错误及主动弃权单列，历史批次保留原有三分类口径。输出有效率 = 有效输出 / 已返回输出。收益率 MAE、RMSE 以百分点计；数值覆盖率为有效数值预测数 / 预测总数。缺失与演示成绩不补零，不同数据版本、预测窗口和协议分别比较。", "",
      "| 选手 / 基模 | 预测窗口 | 方向口径 | 方向准确率 | 方向有效样本 | 输出有效率 | 数据不足 | 技术错误 / 无效输出 | 主动弃权 | 收益率 MAE | 收益率 RMSE | 数值预测覆盖率 | 运行记录 |",
      "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    );
    for (const item of predictionComparisons) {
      lines.push(`| ${markdownInline(item.label)}${item.demo ? "（编排演示）" : item.datasetDemo ? "（合成数据，仅验证流程）" : ""} | ${markdownInline(item.horizonLabel)} | ${markdownInline(item.directionLabel)} | ${predictionPercent(item.directionAccuracy)} | ${item.directionCount ?? "—"} | ${predictionPercent(item.outputValidity)} | ${item.insufficientCount ?? "—"} | ${item.invalidCount ?? "—"} | ${item.voluntaryCount ?? "—"} | ${predictionPoints(item.mae)} | ${predictionPoints(item.rmse)} | ${predictionPercent(item.coverage)} | ${markdownInline(item.runId ?? "—")} |`);
    }
    for (const item of predictionComparisons) {
      if (item.metricsError) lines.push("", `${markdownInline(item.label)}：指标计算失败 — ${markdownInline(item.metricsError)}`);
      if (item.issues.length) {
        lines.push("", `### ${markdownInline(item.label)} · 数据与技术问题`, "");
        for (const issue of item.issues) lines.push(`- ${predictionIssueLabel(issue.prediction_status)} · ${markdownInline(issue.symbol ?? issue.event_id ?? "事件")}：${markdownInline(issue.reason)}`);
        if (item.issuesTruncated) lines.push("", "此处仅列出部分原因，完整明细请查看运行记录。");
      }
    }
  }

  lines.push("", "## 问答汇总", "");
  if (results.qa_results.length === 0) {
    lines.push("本批次未启用问答，或问答任务尚未生成结果。", "");
  } else {
    lines.push(
      "| 模型 API | Model ID | Provider | 路径 | 评分模型 | 回答数 | 已评分 | 平均总分 /100 | 平均延迟 |",
      "| --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: |",
    );
    for (const item of comparisons) {
      const judgeLabel = item.judgeProfileName ? `${item.judgeProfileName}${item.judgeModelId ? ` · ${item.judgeModelId}` : ""}` : "—";
      lines.push(`| ${markdownInline(item.profileName)} | ${markdownInline(item.modelId)} | ${markdownInline(item.provider)} | ${item.variant === "pronoia" ? "Pronoia" : "Raw API"} | ${markdownInline(judgeLabel)} | ${item.count} | ${item.scored} | ${item.averageScore == null ? "—" : item.averageScore.toFixed(1)} | ${item.averageLatency == null ? "—" : `${(item.averageLatency / 1000).toFixed(1)} 秒`} |`);
    }

    lines.push(
      "",
      "## 八维评分对比",
      "",
      `| 模型 API | Model ID | 路径 | ${DIMENSIONS.map((dimension) => `${dimension.label} /${dimension.max}`).join(" | ")} |`,
      `| --- | --- | --- | ${DIMENSIONS.map(() => "---:").join(" | ")} |`,
    );
    for (const item of comparisons) {
      lines.push(`| ${markdownInline(item.profileName)} | ${markdownInline(item.modelId)} | ${item.variant === "pronoia" ? "Pronoia" : "Raw API"} | ${DIMENSIONS.map((dimension) => item.dimensionAverages[dimension.key]?.toFixed(1) ?? "—").join(" | ")} |`);
    }

    lines.push("", "## 回答明细", "");
    results.qa_results.forEach((result, index) => {
      const finalScore = result.final_score;
      const identity = modelIdentity(result, result.profile_id, profileFallback);
      const judge = judgeIdentity(result, results.batch.qa?.judge_profile_id, profileFallback);
      const question = frozenQuestion(results, result);
      lines.push(
        `### ${index + 1}. ${markdownHeading(identity.name)} · ${result.variant === "pronoia" ? "Pronoia" : "Raw API"} · ${markdownHeading(result.question_id)}`,
        "",
        `- 模型快照：${markdownInline(identity.provider)} · ${markdownInline(identity.modelId)}`,
        `- 评分模型快照：${judge ? `${markdownInline(judge.name)} · ${markdownInline(judge.provider)} · ${markdownInline(judge.modelId)}` : "—"}`,
        `- 重复：第 ${result.repeat_no} 次`,
        `- 状态：${markdownInline(statusLabel(result.status))}`,
        `- 总分：${scoreTotal(finalScore) ?? "—"} / 100`,
        `- 延迟：${typeof result.latency_ms === "number" ? `${(result.latency_ms / 1000).toFixed(2)} 秒` : "—"}`,
        `- 八维：${DIMENSIONS.map((dimension) => `${dimension.label} ${typeof finalScore?.[dimension.key] === "number" ? finalScore[dimension.key] : "—"}/${dimension.max}`).join("；")}`,
        `- 重大错误：${finalScore ? finalScore.major_error ? "是" : "否" : "未评分"}`,
        `- 评分来源：${result.manual_score ? "人工评分" : result.auto_score ? "模型自动评分" : "未评分"}`,
        "",
        "**创建时冻结的题目**",
        "",
        markdownQuote(question?.prompt ?? "该历史结果未保存题干"),
        "",
        `期望交付：${markdownInline(question?.deliverable)}`,
        "",
        `核验依据：${markdownInline(question?.gold_standard)}`,
        "",
        "**回答**",
        "",
        markdownQuote(result.answer || result.error_msg || "尚无回答"),
        "",
      );
      if (finalScore?.rationale) {
        lines.push("**评分理由**", "", markdownQuote(finalScore.rationale), "");
      }
    });
  }

  lines.push("---", "", "由 Pronoia Model Lab 导出。未评分项目不会按 0 分计入平均值。", "");
  return `\uFEFF${lines.join("\n")}`;
}

function markdownInline(value: unknown): string {
  return String(value ?? "—").replace(/\r?\n/g, " ").replace(/\|/g, "\\|").trim() || "—";
}

function markdownHeading(value: unknown): string {
  return String(value ?? "").replace(/[\r\n#]+/g, " ").trim() || "未命名";
}

function markdownQuote(value: unknown): string {
  return String(value ?? "").replace(/\r\n/g, "\n").split("\n").map((line) => `> ${line || " "}`).join("\n");
}

function nonEmptyString(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const normalized = value.trim();
  return normalized || null;
}

function csvCell(value: unknown) {
  const raw = String(value ?? "");
  const safe = /^[=+\-@\t\r]/.test(raw) ? "'" + raw : raw;
  return `"${safe.replace(/"/g, '""')}"`;
}
function safeName(value: string) { return value.replace(/[^\w\-\u4e00-\u9fa5]+/g, "_").slice(0, 80) || "model-lab"; }
function download(blob: Blob, filename: string) { const href = URL.createObjectURL(blob); const anchor = document.createElement("a"); anchor.href = href; anchor.download = filename; anchor.click(); window.setTimeout(() => URL.revokeObjectURL(href), 0); }
