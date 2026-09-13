import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, BookOpen, Loader2, MessageSquare, RefreshCw, Search } from "lucide-react";
import { api } from "../../api";
import type { ModelLabProfile, ModelLabQuestionSet } from "../../types";
import { cls } from "../../utils";
import EventModelConnectionEditor from "./EventModelConnectionEditor";

export interface EventQAConfig {
  enabled: boolean;
  question_set_id?: string | null;
  question_ids: string[];
  repeats: number;
  variants: Array<"pronoia" | "raw">;
  scoring_mode: "auto" | "manual" | "mixed";
  judge_profile_id?: string | null;
  candidate_profile_id?: string | null;
  concurrency: number;
}

export interface EventQAStatus {
  issues: string[];
  answerCalls: number;
  judgeCalls: number;
  totalCalls: number;
  highVolume: boolean;
}

export function defaultEventQA(): EventQAConfig {
  return { enabled: false, question_set_id: null, question_ids: [], repeats: 1, variants: ["pronoia", "raw"], scoring_mode: "auto", candidate_profile_id: null, judge_profile_id: "__platform_default__", concurrency: 2 };
}

const INPUT = "w-full min-w-0 rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[11px] text-ink outline-none focus:border-violet/60 focus:ring-2 focus:ring-violet/10 disabled:opacity-50";

export default function EventQAEditor({ value, onChange, profiles, platformDefault, pronoiaModelDefault, customPrediction, disabled, onStatusChange, onProfileCreated }: {
  value: EventQAConfig;
  onChange: (next: EventQAConfig) => void;
  profiles: ModelLabProfile[];
  platformDefault: { available: boolean; name: string; model_id: string | null } | null;
  pronoiaModelDefault?: { available: boolean; name: string; model_id: string | null } | null;
  customPrediction: boolean;
  disabled: boolean;
  onStatusChange: (status: EventQAStatus) => void;
  onProfileCreated: (profile: ModelLabProfile) => void;
}) {
  const pronoiaModel = pronoiaModelDefault === undefined ? platformDefault : pronoiaModelDefault;
  const [sets, setSets] = useState<ModelLabQuestionSet[]>([]);
  const [detail, setDetail] = useState<ModelLabQuestionSet | null>(null);
  const [loading, setLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [query, setQuery] = useState("");
  const configRef = useRef(value);
  const changeRef = useRef(onChange);
  configRef.current = value;
  changeRef.current = onChange;

  useEffect(() => {
    if (!value.enabled) return;
    let alive = true;
    setLoading(true); setError(null);
    void api.modelLabQuestionSets().then((response) => {
      if (!alive) return;
      setSets(response.items);
      const current = configRef.current;
      if (!response.items.some((item) => item.id === current.question_set_id)) {
        changeRef.current({ ...current, question_set_id: response.items[0]?.id ?? null, question_ids: [] });
      }
    }).catch((reason) => { if (alive) setError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (alive) setLoading(false); });
    return () => { alive = false; };
  }, [value.enabled, revision]);

  useEffect(() => {
    if (!value.enabled || !value.question_set_id) { setDetail(null); return; }
    let alive = true;
    const questionSetId = value.question_set_id;
    setDetailLoading(true); setDetailError(null); setDetail(null);
    void api.modelLabQuestionSet(questionSetId).then((result) => {
      if (!alive) return;
      setDetail(result);
      const current = configRef.current;
      if (current.question_set_id !== questionSetId) return;
      const questions = result.questions ?? [];
      const retained = current.question_ids.filter((id) => questions.some((question) => question.id === id));
      const selected = current.question_ids.length ? retained : questions[0] ? [questions[0].id] : [];
      if (JSON.stringify(selected) !== JSON.stringify(current.question_ids)) changeRef.current({ ...current, question_ids: selected });
    }).catch((reason) => { if (alive) setDetailError(reason instanceof Error ? reason.message : String(reason)); })
      .finally(() => { if (alive) setDetailLoading(false); });
    return () => { alive = false; };
  }, [value.enabled, value.question_set_id, revision]);

  const questions = detail?.questions ?? [];
  const visible = questions.filter((question) => !query.trim() || [question.code, question.scenario, question.category, question.prompt].filter(Boolean).join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()));
  const status = useMemo<EventQAStatus>(() => {
    if (!value.enabled) return { issues: [], answerCalls: 0, judgeCalls: 0, totalCalls: 0, highVolume: false };
    const issues: string[] = [];
    if (loading || detailLoading) issues.push("问答题库正在加载");
    if (error || detailError) issues.push("问答题库读取失败，请重试");
    if (!value.question_set_id) issues.push("请选择问答题库");
    if (!value.question_ids.length) issues.push("请至少选择一道问答题");
    if (!value.variants.length) issues.push("请至少选择一个问答模型");
    const checkProfile = (id: string | null | undefined, label: string, allowPlatformDefault = false) => {
      if (!id) { issues.push(`请选择${label}`); return; }
      if (id === "__platform_default__") {
        if (!allowPlatformDefault) issues.push(`请为${label}显式选择一个独立模型连接`);
        else if (!platformDefault?.available) issues.push(`${label}的当前平台接口尚未就绪`);
      } else {
        const profile = profiles.find((item) => item.id === id && item.is_active);
        if (!profile) issues.push(`${label}连接已不可用`);
        else if (!profile.secret_configured) issues.push(`${label}“${profile.name}”尚未配置 API Key`);
      }
    };
    if (value.variants.includes("pronoia") && !pronoiaModel?.available) issues.push("当前 Pronoia 回答模型尚未就绪");
    if (value.variants.includes("raw")) checkProfile(value.candidate_profile_id, "外部问答模型");
    if (value.scoring_mode !== "manual") checkProfile(value.judge_profile_id, "评分模型", true);
    const answerCalls = value.question_ids.length * value.repeats * value.variants.length;
    const judgeCalls = value.scoring_mode === "manual" ? 0 : answerCalls;
    return { issues, answerCalls, judgeCalls, totalCalls: answerCalls + judgeCalls, highVolume: value.question_ids.length > 10 || answerCalls + judgeCalls > 100 };
  }, [value, loading, detailLoading, error, detailError, platformDefault, pronoiaModel, profiles]);
  useEffect(() => { onStatusChange(status); }, [status, onStatusChange]);

  const profileOptions = profiles.filter((profile) => profile.is_active).map((profile) => <option key={profile.id} value={profile.id}>{profile.name} · {profile.model_id}{!profile.secret_configured ? " · 密钥未配置" : ""}</option>);
  const toggleQuestion = (id: string) => onChange({ ...value, question_ids: value.question_ids.includes(id) ? value.question_ids.filter((item) => item !== id) : [...value.question_ids, id] });

  return <section id="create-event-qa" tabIndex={-1} className="overflow-hidden rounded-xl border border-violet/20 bg-card outline-none">
    <div className="flex items-start justify-between gap-3 bg-violet-soft/30 px-4 py-4"><div><h3 className="flex items-center gap-2 text-[12px] font-semibold text-ink"><MessageSquare size={14} className="text-violet" />问答测试 <span className="text-[9px] font-normal text-mute">可选</span></h3><p className="mt-1.5 text-[10px] leading-relaxed text-mute">从问题库选题，比较回答质量。问答模型与评分方式在这里单独设置，结果与事件预测分别记录。</p></div><label className="inline-flex shrink-0 cursor-pointer items-center gap-2 text-[11px] font-medium text-violet"><input type="checkbox" checked={value.enabled} disabled={disabled} onChange={(event) => onChange({ ...value, enabled: event.target.checked })} className="accent-violet" />启用问答</label></div>
    {value.enabled && <fieldset disabled={disabled} className="space-y-4 p-4">
      {customPrediction && <p className="rounded-lg border border-violet/15 bg-violet-soft/25 px-3 py-2 text-[10px] leading-relaxed text-mute">上方第三方预测服务或导入文件提供事件预测；这里另选问答模型回答题库中的问题。</p>}
      <div>
        <p className="text-[10px] font-medium text-ink">问答模型（可多选）</p>
        <div className="mt-2 grid gap-3 sm:grid-cols-2">
          {([
            ["pronoia", "Pronoia（多 Agent）", pronoiaModelDefault === undefined ? "统一平台基模与固定多 Agent 流程" : "本次保存的基模与固定多 Agent 流程"],
            ["raw", "外部大模型（直接回答）", "使用下方独立模型连接直接回答题目"],
          ] as const).map(([variant, label, note]) => (
            <label key={variant} className={cls("flex cursor-pointer items-start gap-2 rounded-lg border px-3 py-3", value.variants.includes(variant) ? "border-violet/30 bg-violet-soft/25" : "border-edge bg-paper")}>
              <input type="checkbox" checked={value.variants.includes(variant)} onChange={() => onChange({ ...value, variants: value.variants.includes(variant) ? value.variants.filter((item) => item !== variant) : [...value.variants, variant] })} className="mt-0.5 accent-violet" />
              <span><span className="block text-[11px] font-medium text-ink">{label}</span><span className="mt-1 block text-[9px] leading-relaxed text-mute">{note}</span></span>
            </label>
          ))}
        </div>
        <p className="mt-2 text-[10px] leading-relaxed text-mute">两个都勾选时，会回答同一组题目，并按相同的重复次数生成回答，便于逐题对照。</p>
      </div>
      {value.variants.includes("pronoia") && <p className="rounded-lg border border-edge bg-paper px-3 py-2.5 text-[10px] leading-relaxed text-mute">Pronoia 基模：<span className="font-medium text-ink">{pronoiaModel?.model_id ?? "尚未读取到回答基模"}</span>。{pronoiaModelDefault === undefined ? "由平台统一配置，外部问答模型的选择不会更换它。" : "使用本次已保存模型的基模配置。评分模型在下方单独选择，不随回答基模切换。"}</p>}
      {value.variants.includes("raw") && (
        <div className="space-y-2">
          <label className="block text-[10px] text-mute">外部问答模型连接
            <select value={value.candidate_profile_id === "__platform_default__" ? "" : value.candidate_profile_id ?? ""} onChange={(event) => onChange({ ...value, candidate_profile_id: event.target.value || null })} className={cls(INPUT, "mt-1.5")}><option value="">请选择独立模型连接</option>{profileOptions}</select>
          </label>
          <p className="text-[10px] leading-relaxed text-mute">此连接用于外部模型直接回答问题，需要单独选择；可与事件预测复用同一连接。</p>
          {!profiles.length && <p className="text-[9px] text-mute">先添加外部模型连接；也可以只勾选 Pronoia 完成问答测试。</p>}
        </div>
      )}
      <EventModelConnectionEditor disabled={disabled} onCreated={(profile) => { onProfileCreated(profile); if (value.variants.includes("raw")) onChange({ ...value, candidate_profile_id: profile.id }); }} />
      <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-[10px] text-mute">评分方式<select value={value.scoring_mode} onChange={(event) => onChange({ ...value, scoring_mode: event.target.value as EventQAConfig["scoring_mode"] })} className={cls(INPUT, "mt-1.5")}><option value="auto">模型自动评分</option><option value="manual">人工评分</option><option value="mixed">模型自动评分 + 人工复核</option></select></label>
        {value.scoring_mode !== "manual" && <label className="text-[10px] text-mute">评分模型<select value={value.judge_profile_id ?? ""} onChange={(event) => onChange({ ...value, judge_profile_id: event.target.value })} className={cls(INPUT, "mt-1.5")}><option value="">请选择评分模型</option><option value="__platform_default__">平台基模（直接评分）{platformDefault?.model_id ? ` · ${platformDefault.model_id}` : ""}{platformDefault && !platformDefault.available ? " · 未就绪" : ""}</option>{profileOptions}</select></label>}
        <label className="text-[10px] text-mute">每题重复次数<select value={value.repeats} onChange={(event) => onChange({ ...value, repeats: Number(event.target.value) })} className={cls(INPUT, "mt-1.5")}><option value={1}>1 次</option><option value={2}>2 次</option><option value={3}>3 次</option></select></label>
      </div>
      <div className="space-y-1.5 rounded-lg border border-edge bg-paper px-3 py-2.5 text-[10px] leading-relaxed text-mute">
        {value.scoring_mode !== "manual" && <p>评分模型在回答生成后，读取题目与答案，按八个维度评分，总分 100。这里选择的是评分用模型；被测答案由上方问答模型生成。</p>}
        <p>{value.scoring_mode === "manual"
          ? "生成回答后等待人工评分。请到结果页填写各维度分数与理由；此方式不调用评分模型。"
          : value.scoring_mode === "mixed"
            ? "先显示自动评分，并标记为待人工评分。人工评分保存后作为最终分数，原自动评分仍可查看。"
            : "自动评分完成后显示分数。之后也可在结果页补充人工评分；保存后以人工评分为准。"}</p>
      </div>
      <div><div className="mb-1.5 flex items-center justify-between gap-2"><label htmlFor="create-qa-question-set" className="text-[10px] text-mute">问题库</label><a href="/backtest/data?tab=questions" target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 text-[10px] text-violet"><BookOpen size={11} />管理问题库（新窗口）</a></div><select id="create-qa-question-set" value={value.question_set_id ?? ""} disabled={disabled || loading} onChange={(event) => { setQuery(""); onChange({ ...value, question_set_id: event.target.value, question_ids: [] }); }} className={INPUT}><option value="">{loading ? "加载题库中…" : "请选择题库"}</option>{sets.map((set) => <option key={set.id} value={set.id}>{set.name} · {set.question_count} 题</option>)}</select></div>
      {(error || detailError) && <div role="alert" className="rounded-lg border border-rise/20 bg-rise/5 px-3 py-2 text-[10px] text-rise"><p>{error || detailError}</p><button type="button" onClick={() => setRevision((current) => current + 1)} className="mt-2 inline-flex items-center gap-1 font-medium"><RefreshCw size={10} />重新读取题库</button></div>}
      {detailLoading ? <p className="inline-flex items-center gap-2 text-[10px] text-mute"><Loader2 size={12} className="animate-spin" />正在读取题目…</p> : detail && <div className="overflow-hidden rounded-lg border border-edge"><div className="flex flex-wrap items-center justify-between gap-2 bg-paper p-2.5"><span className="text-[10px] text-violet">已选 {value.question_ids.length} / {questions.length} 题</span><div className="flex gap-2 text-[9px]"><button type="button" onClick={() => onChange({ ...value, question_ids: [...new Set([...value.question_ids, ...visible.map((question) => question.id)])] })} className="text-mute hover:text-ink">选择筛选结果</button><button type="button" onClick={() => onChange({ ...value, question_ids: [] })} className="text-mute hover:text-ink">清空</button></div></div><label className="relative m-2 block"><Search size={11} className="absolute left-2.5 top-2.5 text-faint" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索题号、分类或问题" aria-label="搜索问答题目" className={cls(INPUT, "pl-7 text-[10px]")} /></label><div className="max-h-56 divide-y divide-edge overflow-y-auto">{visible.map((question) => <label key={question.id} className={cls("flex cursor-pointer items-start gap-2.5 px-3 py-2.5", value.question_ids.includes(question.id) && "bg-violet-soft/20")}><input type="checkbox" checked={value.question_ids.includes(question.id)} onChange={() => toggleQuestion(question.id)} className="mt-1 accent-violet" /><span className="min-w-0"><span className="text-[9px] font-semibold text-violet">{question.code} <span className="font-normal text-faint">{question.category}</span></span><span className="mt-1 block line-clamp-2 text-[10px] leading-relaxed text-ink" title={question.prompt}>{question.prompt}</span></span></label>)}{!visible.length && <p className="p-4 text-center text-[10px] text-mute">没有匹配题目</p>}</div></div>}
      <div className={cls("rounded-lg border px-3 py-2.5 text-[10px] leading-relaxed", status.highVolume ? "border-amber-200 bg-amber-50 text-amber-800" : "border-edge bg-paper text-mute")}><p>预计 {status.answerCalls} 个回答任务 + {status.judgeCalls} 个自动评分任务，共 {status.totalCalls} 个任务。</p><p className="mt-1 text-[9px]">按选中题目数 × 每题重复次数 × 问答模型数计算回答任务；每条成功生成的回答再按所选方式评分。Pronoia 的一个回答任务内会多次调用模型，任务数不等于 API 请求数。{status.highVolume && "启动前需确认本次测试规模。"}</p></div>
      {status.issues.length > 0 && <p className="flex items-start gap-1.5 text-[10px] leading-relaxed text-amber-700"><AlertTriangle size={11} className="mt-0.5 shrink-0" />{status.issues.join("；")}</p>}
    </fieldset>}
  </section>;
}
