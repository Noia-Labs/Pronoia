import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  CheckCircle2,
  CircleDashed,
  FlaskConical,
  Loader2,
  Play,
  ShieldCheck,
  Sparkles,
} from "lucide-react";
import { api } from "../../api";
import { providerLabel } from "../../modelProviders";
import { useStore } from "../../store";
import type {
  BTDataset,
  ModelLabBatch,
  ModelLabBatchInput,
  ModelLabProfile,
  ModelLabQuestion,
  ModelLabQuestionSet,
} from "../../types";
import { cls } from "../../utils";
import { QUESTION_LIBRARY_SELECTION_KEY, readQuestionLibrarySelection } from "./questionLibrarySelection";

const INPUT = "w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[11px] text-ink outline-none transition placeholder:text-faint focus:border-violet/50 focus:ring-2 focus:ring-violet/10 disabled:cursor-not-allowed disabled:bg-edge/35 disabled:text-faint";

function usableEventDataset(dataset: BTDataset) {
  if (dataset.dataset_kind === "market") return false;
  const state = String(dataset.quality_status ?? dataset.status ?? dataset.oracle_status ?? "").toLowerCase();
  return ["available", "passed", "partial"].includes(state) || (!dataset.status && Boolean(dataset.path));
}

function eventDatasetQuality(dataset: BTDataset) {
  const semantic = dataset.semantic_quality;
  const quality = semantic && typeof semantic === "object" ? semantic : {};
  const status = typeof semantic === "string" ? semantic : String(quality.status ?? "");
  const demo = /demo|synthetic|mechanism/i.test(status) || quality.formal_evaluation_eligible === false;
  const label = demo ? "合成演示" : status === "research_ready" ? "正文与来源齐全" : status === "partial" ? "事件事实待补充" : "事件事实未核实";
  const reasons = Array.isArray(quality.reasons) ? quality.reasons.filter((reason): reason is string => typeof reason === "string") : [];
  return { status, demo, label, reasons, priority: demo ? 3 : status === "research_ready" ? 0 : status === "partial" ? 1 : 2 };
}

function sortedEventDatasets(datasets: BTDataset[]) {
  return datasets.filter(usableEventDataset).sort((a, b) => eventDatasetQuality(a).priority - eventDatasetQuality(b).priority);
}

export interface ModelLabBatchComposerProps {
  compact?: boolean;
  predictionRequired?: boolean;
  onCreated?: (batch: ModelLabBatch) => void;
  onManageProfiles?: () => void;
}

export default function ModelLabBatchComposer({ compact = false, predictionRequired = false, onCreated, onManageProfiles }: ModelLabBatchComposerProps) {
  const setView = useStore((state) => state.setView);
  const [libraryDraft] = useState(() => compact ? null : readQuestionLibrarySelection());
  const pendingLibraryDraft = useRef(libraryDraft);
  const [libraryNotice, setLibraryNotice] = useState<string | null>(null);
  const [profiles, setProfiles] = useState<ModelLabProfile[]>([]);
  const [defaultProfileId, setDefaultProfileId] = useState<string | null>(null);
  const [questionSets, setQuestionSets] = useState<ModelLabQuestionSet[]>([]);
  const [datasets, setDatasets] = useState<BTDataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [selectedProfiles, setSelectedProfiles] = useState<string[]>([]);
  const [predictionEnabled, setPredictionEnabled] = useState(true);
  const [datasetId, setDatasetId] = useState("");
  const runner = "team_full" as const;
  const [horizon, setHorizon] = useState<ModelLabBatchInput["prediction"]["horizon"]>("t3");
  const [qaEnabled, setQaEnabled] = useState(false);
  const [questionSetId, setQuestionSetId] = useState("");
  const [questionSetDetail, setQuestionSetDetail] = useState<ModelLabQuestionSet | null>(null);
  const [questionLoading, setQuestionLoading] = useState(false);
  const [questionError, setQuestionError] = useState<string | null>(null);
  const [selectedQuestionIds, setSelectedQuestionIds] = useState<string[]>([]);
  const [repeats, setRepeats] = useState(1);
  const variants: Array<"pronoia" | "raw"> = ["pronoia"];
  const [scoringMode, setScoringMode] = useState<"auto" | "manual" | "mixed">("auto");
  const [judgeProfileId, setJudgeProfileId] = useState("");
  const [dryRun, setDryRun] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const load = async () => {
    setLoading(true);
    setLoadError(null);
    try {
      const [profileResponse, questionResponse, datasetResponse] = await Promise.all([
        api.modelLabProfiles(),
        api.modelLabQuestionSets(),
        api.btListDatasets(),
      ]);
      const active = profileResponse.items.filter((profile) => profile.is_active);
      setProfiles(active);
      setDefaultProfileId(profileResponse.default_profile_id);
      setQuestionSets(questionResponse.items);
      setDatasets(datasetResponse);
      setSelectedProfiles((current) => current.length
        ? current.filter((id) => active.some((profile) => profile.id === id))
        : profileResponse.default_profile_id && active.some((profile) => profile.id === profileResponse.default_profile_id)
          ? [profileResponse.default_profile_id]
          : active[0] ? [active[0].id] : []);
      setJudgeProfileId((current) => active.some((profile) => profile.id === current)
        ? current
        : profileResponse.default_profile_id && active.some((profile) => profile.id === profileResponse.default_profile_id)
          ? profileResponse.default_profile_id
          : active[0]?.id ?? "");
      const pending = pendingLibraryDraft.current;
      const pendingSet = pending && questionResponse.items.find((item) => item.id === pending.questionSetId);
      if (pendingSet) {
        setQuestionSetId(pendingSet.id);
        setQaEnabled(true);
      } else {
        setQuestionSetId((current) => questionResponse.items.some((item) => item.id === current) ? current : questionResponse.items[0]?.id ?? "");
        if (pending) {
          setLibraryNotice("之前选择的题库已不可用，请回到问题库重新选题。未启动任何评测。");
          pendingLibraryDraft.current = null;
          try { window.sessionStorage.removeItem(QUESTION_LIBRARY_SELECTION_KEY); } catch { /* Storage may be disabled. */ }
        }
      }
      const usable = sortedEventDatasets(datasetResponse);
      setDatasetId((current) => usable.some((item) => item.id === current) ? current : usable[0]?.id ?? "");
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);

  useEffect(() => {
    if (!questionSetId) {
      setQuestionSetDetail(null);
      setSelectedQuestionIds([]);
      setQuestionError(null);
      return;
    }
    let active = true;
    setQuestionLoading(true);
    setQuestionError(null);
    setQuestionSetDetail(null);
    setSelectedQuestionIds([]);
    void api.modelLabQuestionSet(questionSetId).then((detail) => {
      if (!active) return;
      const questions = detail.questions ?? [];
      setQuestionSetDetail(detail);
      const pending = pendingLibraryDraft.current;
      if (pending?.questionSetId === detail.id) {
        const selected = pending.questionIds.filter((id) => questions.some((question) => question.id === id));
        setSelectedQuestionIds(selected);
        setLibraryNotice(selected.length
          ? `已从问题库带入 ${selected.length} 道题（${detail.name}）。${selected.length < pending.questionIds.length ? "部分题目已不可用，未加入本次测试。" : ""}请确认模型与评分方式后手动启动。`
          : "所选题目已不可用，请重新选题。未启动任何评测。");
        // Keep the draft while the user goes to configure their first API.
        if (profiles.length) {
          pendingLibraryDraft.current = null;
          try { window.sessionStorage.removeItem(QUESTION_LIBRARY_SELECTION_KEY); } catch { /* Storage may be disabled. */ }
        }
      } else {
        // Enabling QA never silently schedules the entire library.
        setSelectedQuestionIds(questions[0] ? [questions[0].id] : []);
      }
    }).catch((reason) => {
      if (!active) return;
      setQuestionError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => {
      if (active) setQuestionLoading(false);
    });
    return () => { active = false; };
  }, [questionSetId, profiles.length]);

  const openQuestionLibrary = () => {
    setView("backtest-data");
    window.history.replaceState({}, "", "/backtest/data?tab=questions");
  };

  const eventDatasets = useMemo(() => sortedEventDatasets(datasets), [datasets]);
  const selectedDataset = eventDatasets.find((dataset) => dataset.id === datasetId);
  const selectedQuality = selectedDataset ? eventDatasetQuality(selectedDataset) : null;
  const questions = questionSetDetail?.questions ?? [];
  const selectedQuestionCount = selectedQuestionIds.length;
  const needsJudge = qaEnabled && scoringMode !== "manual";
  const effectivePrediction = predictionRequired || predictionEnabled;
  const candidateCalls = qaEnabled ? selectedProfiles.length * selectedQuestionCount * repeats * variants.length : 0;
  const judgeCalls = needsJudge ? candidateCalls : 0;
  const totalQaCalls = candidateCalls + judgeCalls;
  const highVolume = qaEnabled && (selectedQuestionCount > 10 || totalQaCalls > 100);
  const validation = useMemo(() => {
    const issues: string[] = [];
    if (!name.trim()) issues.push("请填写评测名称");
    if (!selectedProfiles.length) issues.push("至少选择一个被测 API");
    if (effectivePrediction && !datasetId) issues.push("预测测试需要一个已冻结事件集");
    if (qaEnabled && !questionSetId) issues.push("问答测试需要题库");
    if (qaEnabled && questionSetId && selectedQuestionIds.length === 0) issues.push("问答测试至少选择一道题");
    if (qaEnabled && variants.length === 0) issues.push("至少选择一种问答路径");
    if (needsJudge && !judgeProfileId) issues.push("自动评分需要选择评分模型");
    if (!dryRun) {
      const missingSecrets = selectedProfiles
        .map((id) => profiles.find((profile) => profile.id === id))
        .filter((profile): profile is ModelLabProfile => profile !== undefined && !profile.secret_configured)
        .map((profile) => profile.name);
      if (missingSecrets.length) issues.push(`请先在模型连接中填写 API Key：${missingSecrets.join("、")}`);
      const judge = profiles.find((profile) => profile.id === judgeProfileId);
      if (needsJudge && judge && !judge.secret_configured && !missingSecrets.includes(judge.name)) {
        issues.push(`评分模型“${judge.name}”的服务端密钥未配置`);
      }
    }
    if (!effectivePrediction && !qaEnabled) issues.push("预测与问答至少启用一项");
    return issues;
  }, [datasetId, dryRun, effectivePrediction, judgeProfileId, name, needsJudge, profiles, qaEnabled, questionSetId, selectedProfiles, selectedQuestionIds.length, variants.length]);

  const toggleProfile = (profileId: string) => {
    setSelectedProfiles((items) => items.includes(profileId) ? items.filter((id) => id !== profileId) : [...items, profileId]);
  };

  const toggleQuestion = (questionId: string) => {
    setSelectedQuestionIds((items) => items.includes(questionId) ? items.filter((id) => id !== questionId) : [...items, questionId]);
  };

  const submit = async () => {
    if (validation.length || submitting) return;
    if (highVolume && !window.confirm(`本次问答将选择 ${selectedQuestionCount} 道题，预计发起 ${candidateCalls} 项回答任务和 ${judgeCalls} 项评分任务，共 ${totalQaCalls} 项任务。Pronoia 每项任务内部可能多次调用模型。\n\n确认继续创建吗？`)) return;
    setSubmitting(true);
    setSubmitError(null);
    try {
      const payload: ModelLabBatchInput = {
        name: name.trim(),
        profile_ids: selectedProfiles,
        auto_start: true,
        dry_run: dryRun,
        prediction: {
          enabled: effectivePrediction,
          dataset_id: effectivePrediction ? datasetId : null,
          dataset_version: effectivePrediction ? selectedDataset?.dataset_version ?? selectedDataset?.version ?? null : null,
          runner,
          horizon,
          concurrency: 2,
          prompt_variant: "v0",
        },
        qa: {
          enabled: qaEnabled,
          question_set_id: qaEnabled ? questionSetId : null,
          question_ids: qaEnabled ? selectedQuestionIds : [],
          repeats,
          variants,
          scoring_mode: scoringMode,
          judge_profile_id: needsJudge ? judgeProfileId : null,
          concurrency: 2,
        },
      };
      const batch = await api.modelLabCreateBatch(payload);
      setName("");
      onCreated?.(batch);
    } catch (reason) {
      setSubmitError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSubmitting(false);
    }
  };

  if (loading) {
    return <div className="grid min-h-48 place-items-center rounded-xl border border-edge bg-card text-[10.5px] text-mute shadow-card"><span className="inline-flex items-center gap-2"><Loader2 size={14} className="animate-spin" />正在读取模型、题库与事件集…</span></div>;
  }

  if (loadError) {
    return (
      <div className="rounded-xl border border-rise/20 bg-card p-5 shadow-card">
        <div className="flex items-start gap-2 text-[10.5px] text-rise"><AlertTriangle size={13} className="mt-0.5 shrink-0" /><span>模型评测服务暂时不可用：{loadError}</span></div>
        <button type="button" onClick={() => void load()} className="mt-3 rounded-lg border border-edge bg-paper px-3 py-2 text-[10px] font-medium text-ink">重新连接</button>
      </div>
    );
  }

  if (profiles.length === 0) {
    return (
      <div className="flex min-h-64 items-center justify-center rounded-xl border border-dashed border-violet/25 bg-card px-6 py-8 text-center shadow-card">
        <div className="w-full max-w-xl">
          <span className="mx-auto grid h-11 w-11 place-items-center rounded-xl bg-violet-soft text-violet"><FlaskConical size={18} /></span>
          <h3 className="mt-4 font-serif text-[18px] font-semibold leading-relaxed text-ink">先添加一个 Pronoia 基模连接</h3>
          <p className="mt-2 text-[12px] leading-6 text-mute">
            <span className="block">填写模型接口地址、模型 ID 和 API Key。</span>
            <span className="block">保存后，即可选择基模创建预测或问答评测。</span>
          </p>
          {libraryNotice && <p role="status" className="mt-4 rounded-lg bg-violet-soft/45 px-3 py-2 text-[12px] leading-relaxed text-violet">{libraryNotice} 选题已保留，添加模型后可继续。</p>}
          <div className="mt-5 flex flex-col items-stretch justify-center gap-3 sm:flex-row sm:items-center">
            {onManageProfiles && <button type="button" onClick={onManageProfiles} className="inline-flex min-h-10 items-center justify-center whitespace-nowrap rounded-lg border border-transparent bg-violet px-4 py-2.5 text-[12px] font-medium leading-5 text-white transition hover:opacity-90">添加基模连接</button>}
            {compact && !onManageProfiles && <button type="button" onClick={() => setView("backtest-model-lab")} className="inline-flex min-h-10 items-center justify-center gap-1.5 whitespace-nowrap rounded-lg border border-transparent bg-violet px-4 py-2.5 text-[12px] font-medium leading-5 text-white transition hover:opacity-90">前往添加模型 <ArrowRight size={12} /></button>}
            <button type="button" onClick={openQuestionLibrary} className="inline-flex min-h-10 items-center justify-center whitespace-nowrap rounded-lg border border-violet/20 bg-card px-4 py-2.5 text-[12px] font-medium leading-5 text-violet transition hover:bg-violet-soft">管理问题库</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <section className="overflow-hidden rounded-xl border border-violet/20 bg-card shadow-card">
      <div className="flex flex-col justify-between gap-3 border-b border-violet/10 bg-violet-soft/30 px-5 py-4 sm:flex-row sm:items-center">
        <div>
          <p className="text-[9px] font-semibold uppercase tracking-[0.18em] text-violet">Evaluation composer</p>
          <h2 className="mt-1 font-serif text-[15px] font-semibold text-ink">选择预测与问答测试</h2>
          <p className="mt-1 text-[9.5px] text-mute">每个基模按所选测试生成独立任务；预测与问答分别运行、分别查看结果。</p>
        </div>
        <span className="inline-flex items-center gap-1.5 self-start rounded-full border border-violet/15 bg-card px-2.5 py-1 text-[9px] font-medium text-violet"><Sparkles size={10} /> {profiles.length} 个可用 API</span>
      </div>
      <div className={cls("grid gap-5 p-5", compact ? "xl:grid-cols-[minmax(0,1fr)_360px]" : "xl:grid-cols-[minmax(0,1.1fr)_minmax(360px,.9fr)]")}>
        <div className="space-y-4">
          <label className="block">
            <span className="text-[10px] font-medium text-ink">评测名称</span>
            <input value={name} onChange={(event) => setName(event.target.value)} className={cls(INPUT, "mt-1.5")} placeholder="例如：Q3 事件模型 API 横评" maxLength={120} />
          </label>
          <div>
            <div className="flex items-center justify-between gap-2"><span className="text-[10px] font-medium text-ink">被测 API</span><span className="text-[8.5px] text-faint">可多选，同题同数据比较</span></div>
            <div className="mt-1.5 grid gap-2 sm:grid-cols-2">
              {profiles.map((profile) => {
                const checked = selectedProfiles.includes(profile.id);
                return (
                  <button key={profile.id} type="button" onClick={() => toggleProfile(profile.id)} className={cls("rounded-lg border px-3 py-2.5 text-left transition", checked ? "border-violet/30 bg-violet-soft/45" : "border-edge bg-paper hover:border-edgeDark")}>
                    <div className="flex items-start justify-between gap-2"><span className="truncate text-[10.5px] font-semibold text-ink">{profile.name}</span>{checked ? <CheckCircle2 size={12} className="shrink-0 text-violet" /> : <CircleDashed size={12} className="shrink-0 text-faint" />}</div>
                    <p className="mt-1 truncate font-mono text-[8.5px] text-mute">{providerLabel(profile.provider)} · {profile.model_id}</p>
                    <p className={cls("mt-1 text-[8px]", profile.secret_configured ? "text-jade" : "text-amber-700")}>{profile.secret_configured ? "API Key 已配置" : "API Key 未配置"}{profile.id === defaultProfileId ? " · 当前默认" : ""}</p>
                  </button>
                );
              })}
            </div>
          </div>

          <div className="rounded-xl border border-brand/15 bg-brand-soft/20 p-4">
            <div className="flex items-center justify-between gap-3">
              <div><p className="text-[10.5px] font-semibold text-ink">预测测试</p><p className="mt-0.5 text-[8.5px] text-mute">事件方向与收益率预测 · 每个基模运行相同的 Pronoia 多 Agent 流程</p></div>
              <label className="inline-flex items-center gap-2 text-[9.5px] font-medium text-brand"><input type="checkbox" checked={effectivePrediction} disabled={predictionRequired} onChange={(event) => setPredictionEnabled(event.target.checked)} />{predictionRequired ? "本页必选" : "启用"}</label>
            </div>
            {effectivePrediction && (
              <div className="mt-3 grid gap-2 sm:grid-cols-3">
                <label className="sm:col-span-3"><span className="text-[9px] text-mute">共同事件集 · 全部基模使用同一份数据</span><select value={datasetId} onChange={(event) => setDatasetId(event.target.value)} className={cls(INPUT, "mt-1")}><option value="">请选择事件集</option>{eventDatasets.map((dataset) => <option key={dataset.id} value={dataset.id}>{dataset.name} · {dataset.total_events ?? 0} 条 · {eventDatasetQuality(dataset).label}</option>)}</select></label>
                {selectedQuality && <div className={cls("sm:col-span-3 rounded-lg border px-3 py-2 text-[10px] leading-relaxed", selectedQuality.status === "research_ready" && !selectedQuality.demo ? "border-jade/20 bg-jade-soft/30 text-jade" : "border-amber-200 bg-amber-50 text-amber-800")}><p className="font-medium">{selectedQuality.label}{selectedQuality.demo ? " · 仅验证流程，不用于评价模型准确性" : ""}</p>{selectedQuality.status !== "research_ready" && selectedQuality.reasons.length > 0 && <p className="mt-1">{selectedQuality.reasons.slice(0, 3).join("；")}</p>}</div>}
                <div className="rounded-lg border border-edge bg-card px-3 py-2 text-[10px] text-mute">统一 Pronoia 多 Agent 流程 · 只改变本批次基模；不会修改其他页面的 Pronoia。</div>
                <label><span className="text-[9px] text-mute">共同预测周期</span><select value={horizon} onChange={(event) => setHorizon(event.target.value as typeof horizon)} className={cls(INPUT, "mt-1")}><option value="t1">T+1</option><option value="t3">T+3</option><option value="t5">T+5</option><option value="t7">T+7</option><option value="t15">T+15</option><option value="t30">T+30</option><option value="t60">T+60</option></select></label>
                <div className="rounded-lg border border-edge bg-card px-3 py-2"><p className="text-[8.5px] text-faint">有效预测输出</p><p className="mt-1 text-[9.5px] font-medium text-ink">看涨 / 看跌 · 置信度 · 理由 · 收益率</p></div>
                <p className="sm:col-span-3 text-[10px] leading-relaxed text-mute">批次创建时统一保存事件集版本、预测周期及评测口径。数据不足会显示具体缺少的依据，技术错误单独记录，均不作为中性或方向未命中计分。</p>
              </div>
            )}
          </div>
        </div>

        <div className="space-y-4">
          <div className="rounded-xl border border-violet/15 bg-violet-soft/20 p-4">
            <div className="flex items-center justify-between gap-3">
              <div><p className="text-[10.5px] font-semibold text-ink">问答测试 · 可选</p><p className="mt-0.5 text-[8.5px] text-mute">与预测分别设置，单独记录回答和评分结果</p></div>
              <label className="inline-flex items-center gap-2 text-[9.5px] font-medium text-violet"><input type="checkbox" checked={qaEnabled} onChange={(event) => setQaEnabled(event.target.checked)} />启用</label>
            </div>
            <button type="button" onClick={openQuestionLibrary} className="mt-2 text-[9px] font-medium text-violet hover:underline">从问题库选题 / 新增自定义题目 <ArrowRight size={10} className="inline" /></button>
            {libraryNotice && <p role="status" className="mt-2 rounded-lg border border-violet/15 bg-card px-3 py-2 text-[9px] leading-relaxed text-violet">{libraryNotice}</p>}
            {qaEnabled && (
              <div className="mt-3 space-y-3">
                <label className="block"><span className="text-[9px] text-mute">题库</span><select value={questionSetId} onChange={(event) => setQuestionSetId(event.target.value)} className={cls(INPUT, "mt-1")}><option value="">请选择题库</option>{questionSets.map((set) => <option key={set.id} value={set.id}>{set.name} · {set.question_count} 题</option>)}</select></label>
                <div>
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="text-[9px] text-mute">测试题目 · 已选 {selectedQuestionCount}/{questions.length}</span>
                    {questions.length > 0 && (
                      <div className="flex items-center gap-2 text-[8px] font-medium">
                        <button type="button" onClick={() => setSelectedQuestionIds(questions[0] ? [questions[0].id] : [])} className="text-violet hover:underline">仅第 1 题</button>
                        <span className="text-edgeDark">·</span>
                        <button type="button" onClick={() => setSelectedQuestionIds(questions.map((question) => question.id))} className="text-violet hover:underline">全选 {questions.length} 题</button>
                      </div>
                    )}
                  </div>
                  {questionLoading ? (
                    <div className="mt-1.5 flex min-h-20 items-center justify-center rounded-lg border border-edge bg-card text-[8.5px] text-mute"><Loader2 size={10} className="mr-1.5 animate-spin" />读取题目…</div>
                  ) : questionError ? (
                    <div className="mt-1.5 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[8.5px] leading-relaxed text-rise">题库读取失败：{questionError}</div>
                  ) : questions.length === 0 ? (
                    <div className="mt-1.5 rounded-lg border border-dashed border-edge px-3 py-5 text-center text-[8.5px] text-mute">该题库没有可选题目。</div>
                  ) : (
                    <div className="mt-1.5 max-h-52 space-y-1.5 overflow-y-auto rounded-lg border border-edge bg-card p-2">
                      {questions.map((question, index) => <QuestionChoice key={question.id} question={question} index={index} checked={selectedQuestionIds.includes(question.id)} onChange={() => toggleQuestion(question.id)} />)}
                    </div>
                  )}
                  <p className="mt-1.5 text-[8px] leading-relaxed text-faint">直接切换题库时默认仅选第一题；从问题库进入时保留勾选。每个基模下的 Pronoia 回答同一组题目，并按相同次数重复。</p>
                </div>
                <p className="rounded-lg border border-violet/20 bg-card px-3 py-2 text-[10px] text-mute">各基模均通过 Pronoia 完整流程回答。独立大模型对比请使用事件模型栏目。</p>
                <div className="grid gap-2 sm:grid-cols-2">
                  <label><span className="text-[9px] text-mute">重复次数</span><select value={repeats} onChange={(event) => setRepeats(Number(event.target.value))} className={cls(INPUT, "mt-1")}><option value={1}>1 次</option><option value={2}>2 次</option><option value={3}>3 次</option></select></label>
                  <label><span className="text-[9px] text-mute">评分方式</span><select value={scoringMode} onChange={(event) => setScoringMode(event.target.value as typeof scoringMode)} className={cls(INPUT, "mt-1")}><option value="auto">模型自动评分</option><option value="manual">人工评分</option><option value="mixed">模型自动评分 + 人工复核</option></select></label>
                  {scoringMode !== "manual" && <label><span className="text-[9px] text-mute">评分模型</span><select value={judgeProfileId} onChange={(event) => setJudgeProfileId(event.target.value)} className={cls(INPUT, "mt-1")}><option value="">请选择评分模型</option>{profiles.map((profile) => <option key={profile.id} value={profile.id}>{profile.name}</option>)}</select></label>}
                </div>
                <div className="space-y-1 rounded-lg border border-edge bg-card px-3 py-2 text-[9px] leading-relaxed text-mute">
                  {scoringMode !== "manual" && <p>所有回答使用同一个评分模型，按八个维度评分，总分 100。评分模型只评价已生成的答案，被测答案由各基模下的 Pronoia 生成。</p>}
                  <p>{scoringMode === "manual" ? "生成回答后等待你在结果页填写分数与理由，此方式不调用评分模型。" : scoringMode === "mixed" ? "先显示自动评分并标记待人工评分；保存人工评分后以人工分为准，同时保留原自动分。" : "自动评分后显示分数；后续也可补充人工评分，保存后以人工分为准。"}</p>
                </div>
                <div className="grid grid-cols-3 gap-px overflow-hidden rounded-lg border border-edge bg-edge">
                  <CallEstimate label="回答任务" value={candidateCalls} note={`${selectedProfiles.length} API × ${selectedQuestionCount} 题 × ${repeats} 次 × ${variants.length} 路径`} />
                  <CallEstimate label="自动评分" value={judgeCalls} note={needsJudge ? "每条回答评分 1 次" : "人工评分不调用评分模型"} />
                  <CallEstimate label="回答与评分任务" value={totalQaCalls} note="多 Agent 内部还会调用模型与工具" emphasized />
                </div>
                {highVolume && (
                  <div className="flex items-start gap-2 rounded-lg border border-rise/25 bg-rise/5 px-3 py-2.5 text-[8.5px] leading-relaxed text-rise">
                    <AlertTriangle size={11} className="mt-0.5 shrink-0" />
                    <span><strong className="font-semibold">高调用量：</strong>当前选择 {selectedQuestionCount} 道题，共 {totalQaCalls} 项回答与评分任务。点击启动后仍会要求二次确认。</span>
                  </div>
                )}
              </div>
            )}
          </div>

          <label className={cls("flex items-start gap-2 rounded-lg border px-3 py-2.5", dryRun ? "border-amber-200 bg-amber-50" : "border-edge bg-paper")}>
            <input type="checkbox" checked={dryRun} onChange={(event) => setDryRun(event.target.checked)} className="mt-0.5" />
            <span><span className="block text-[9.5px] font-semibold text-ink">编排演练（dry run）</span><span className="mt-0.5 block text-[8.5px] leading-relaxed text-mute">只验证流程，结果明确标记 Demo，不能进入 Arena，也不计入正式结论。</span></span>
          </label>

          {validation.length > 0 && <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2.5 text-[9px] leading-relaxed text-amber-800">{validation.join("；")}</div>}
          {submitError && <div className="flex items-start gap-2 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[9.5px] text-rise"><AlertTriangle size={11} className="mt-0.5 shrink-0" />{submitError}</div>}
          <button type="button" onClick={() => void submit()} disabled={Boolean(validation.length) || submitting} className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-ink px-4 py-2.5 text-[11px] font-semibold text-card transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:opacity-40">
            {submitting ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}{submitting ? "正在创建独立任务…" : dryRun ? "启动编排演练" : "启动正式评测"}
          </button>
          <p className="flex items-start gap-1.5 text-[8.5px] leading-relaxed text-faint"><ShieldCheck size={10} className="mt-0.5 shrink-0" />创建时快照模型配置、数据版本、题库与评分方式；之后修改默认 API 不会污染历史结果。</p>
        </div>
      </div>
    </section>
  );
}

function QuestionChoice({ question, index, checked, onChange }: { question: ModelLabQuestion; index: number; checked: boolean; onChange: () => void }) {
  return (
    <label className={cls("flex cursor-pointer items-start gap-2 rounded-md border px-2.5 py-2 transition", checked ? "border-violet/25 bg-violet-soft/35" : "border-transparent bg-paper hover:border-edgeDark")}>
      <input type="checkbox" checked={checked} onChange={onChange} className="mt-0.5 shrink-0" />
      <span className="min-w-0">
        <span className="flex flex-wrap items-center gap-1.5">
          <span className="font-mono text-[8px] font-semibold text-violet">{question.code || `Q${index + 1}`}</span>
          {question.category && <span className="rounded bg-card px-1.5 py-0.5 text-[7.5px] text-faint">{question.category}</span>}
        </span>
        <span className="mt-1 block line-clamp-2 text-[8.5px] leading-relaxed text-mute" title={question.prompt}>{question.prompt}</span>
      </span>
    </label>
  );
}

function CallEstimate({ label, value, note, emphasized = false }: { label: string; value: number; note: string; emphasized?: boolean }) {
  return (
    <div className={cls("min-w-0 bg-card px-2.5 py-2 text-center", emphasized && "bg-violet-soft/35")}>
      <p className="text-[7.5px] text-faint">{label}</p>
      <p className={cls("mt-0.5 font-mono text-[13px] font-semibold", emphasized ? "text-violet" : "text-ink")}>{value}</p>
      <p className="mt-0.5 truncate text-[7px] text-faint" title={note}>{note}</p>
    </div>
  );
}
