import { useEffect, useRef, useState } from "react";
import { AlertTriangle, Check, ClipboardList, Copy, GitBranch, Users } from "lucide-react";
import { observationMarkdown, observationWindow, observations, safeSourceUrl, textItems } from "../lib/simulationPresentation";

function Stat({ label, value }: { label: string; value: number | string }) {
  return <div className="rounded-lg border border-edge bg-card px-3 py-2"><p className="text-[10.5px] text-faint">{label}</p><p className="mt-0.5 text-[15px] font-semibold text-ink">{value}</p></div>;
}

export default function SimulationView({ payload }: { payload: any }) {
  const execution = payload?.execution ?? {};
  const scenarios = Array.isArray(payload?.scenarios) ? payload.scenarios : [];
  const warnings = textItems(payload?.warnings);
  const notices = textItems(payload?.notices);
  const configuredActors = Array.isArray(execution.configured_actors) ? execution.configured_actors : [];
  const evidence = Array.isArray(payload?.evidence) ? payload.evidence : [];
  const actorLabels = new Map<string, string>(configuredActors.map((actor: any) => [actor.id, actor.label]));
  const coveredActors = new Set(scenarios.flatMap((scenario: any) => textItems(scenario.actor_ids)));
  const configuredCount = execution.configured_actor_count ?? configuredActors.length;
  const coverageRecorded = scenarios.length > 0 && scenarios.every((scenario: any) => Array.isArray(scenario.actor_ids));
  const checklist = observations(payload);
  const withheld = Array.isArray(payload?.scenario_review?.withheld_branches) ? payload.scenario_review.withheld_branches : [];
  const [copied, setCopied] = useState(false);
  const [manualCopy, setManualCopy] = useState(false);
  const copiedTimer = useRef<number>();
  useEffect(() => () => window.clearTimeout(copiedTimer.current), []);
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(observationMarkdown(payload));
      setCopied(true);
      setManualCopy(false);
      window.clearTimeout(copiedTimer.current);
      copiedTimer.current = window.setTimeout(() => setCopied(false), 2500);
    } catch { setManualCopy(true); }
  };
  return (
    <div className="space-y-3">
      <div className="rounded-card border border-edge bg-card p-3.5 shadow-card">
        <div className="flex items-center gap-2 text-[13px] font-semibold text-ink"><Users size={15} className="text-jade" />事件推演</div>
        {observationWindow(payload) && <p className="mt-1.5 text-[11px] text-faint">观察窗口：{observationWindow(payload)}</p>}
        {payload?.research_focus?.title && <p className="mt-2 text-xs text-jade">本轮主线：{payload.research_focus.title} · 观察条件由系统整理，各方响应为模拟假设</p>}
        <p className="mt-1.5 text-[11.5px] leading-relaxed text-mute">查看各方可能采取的行动，并把触发与失效条件加入后续研究。</p>
        <div className="mt-3 grid grid-cols-2 gap-2">
          <Stat label={coverageRecorded ? "进入情景的参与方" : "参与方"} value={coverageRecorded ? (configuredCount ? `${coveredActors.size} / ${configuredCount}` : coveredActors.size) : (configuredCount || "未记录")} />
          <Stat label="有效决策" value={execution.valid_decision_count ?? 0} />
          <Stat label="条件情景" value={scenarios.length} />
          <Stat label="观察条件 / 需先核对" value={`${checklist.length} / ${checklist.filter((item) => item.needsReview).length}`} />
        </div>
        {configuredActors.length > 0 && <details className="mt-3 border-t border-edge pt-2.5 text-[11px] leading-relaxed text-mute">
          <summary className="cursor-pointer font-medium text-ink">参与方与关注点</summary>
          <ul className="mt-2 space-y-2">{configuredActors.map((actor: any) => <li key={actor.id}><span className="font-medium text-ink">{actor.label}</span>：{actor.focus || actor.selection_reason}</li>)}</ul>
          <p className="mt-2 text-faint">关注点是角色建模假设，可结合具体事件复核。</p>
        </details>}
      </div>
      <p className="px-1 text-[11px] leading-relaxed text-faint">以下是条件情景，供研究与复核；模拟频率和内部一致性不代表发生概率。</p>
      {notices.map((notice, index) => <p key={index} className="rounded-lg border border-[#E8D7A5] bg-[#FFF9E8] px-3 py-2 text-[11px] text-[#775B19]">{notice}</p>)}
      {withheld.length > 0 && <details className="rounded-lg border border-[#E8D7A5] bg-[#FFF9E8] px-3 py-2 text-[11px] leading-relaxed text-[#775B19]"><summary className="cursor-pointer">{withheld.length} 条候选路径需要核对，暂未加入观察清单</summary><ul className="mt-2 space-y-2">{withheld.map((item: any, index: number) => <li key={index}><span className="font-medium">{item.label}</span>：{item.reason}</li>)}</ul></details>}
      {checklist.length > 0 && <div className="rounded-card border border-jade/25 bg-jade-soft/50 p-3.5">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="flex items-center gap-1.5 text-[12px] font-semibold text-ink"><ClipboardList size={14} />后续观察清单 · {checklist.length} 项</p>
          <button onClick={() => void copy()} className="flex items-center gap-1.5 rounded-md border border-jade/25 bg-card px-2.5 py-1.5 text-[11px] font-medium text-jade hover:bg-white">{copied ? <Check size={12} /> : <Copy size={12} />}{copied ? "已复制" : "复制观察清单"}</button>
        </div>
        <p className="mt-1.5 text-[11px] text-mute">包含观察信号、重新判断条件和引用资料，方便带入研究笔记。</p>
        {manualCopy && <div className="mt-2"><p role="status" className="mb-1 text-[11px] text-mute">浏览器暂不允许自动复制，请选中下方文本复制。</p><textarea aria-label="观察清单文本" readOnly value={observationMarkdown(payload)} onFocus={(event) => event.currentTarget.select()} className="h-48 w-full rounded-md border border-edge bg-card p-2 text-[11px] text-ink" /></div>}
      </div>}
      {scenarios.map((scenario: any, index: number) => {
        const refs = textItems(scenario.evidence_refs);
        const cited = evidence.filter((fact: any) => refs.includes(fact.id));
        const cards = checklist.filter((item) => item.scenarioIndex === index);
        const responses = Array.isArray(scenario.conditional_responses) ? scenario.conditional_responses : [];
        const starts = Array.isArray(scenario.starting_decisions) ? scenario.starting_decisions : [];
        return <div key={scenario.id ?? index} className="rounded-card border border-edge bg-card p-4 shadow-card">
          <div className="flex items-start gap-2.5">
            <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-jade-soft text-jade"><GitBranch size={12} /></span>
            <div className="min-w-0 flex-1">
              <p className="text-[13px] font-semibold text-ink">{scenario.label || `情景 ${index + 1}`}</p>
              <p className="mt-1.5 text-[12px] leading-relaxed text-mute">{scenario.summary}</p>
              <div className="mt-2 flex flex-wrap gap-1">{textItems(scenario.actor_ids).map((id) => <span key={id} className="rounded-full bg-jade-soft px-2 py-0.5 text-[10px] text-jade">{actorLabels.get(id) || id}</span>)}</div>
            </div>
          </div>
          {textItems(scenario.review_notices).map((notice, itemIndex) => <p key={itemIndex} className="mt-3 rounded-md bg-[#FFF9E8] px-2.5 py-2 text-[11px] leading-relaxed text-[#775B19]">{notice}</p>)}
          {starts.length > 0 && <details className="mt-3 border-t border-edge pt-2.5 text-[11px] leading-relaxed text-mute">
            <summary className="cursor-pointer font-medium text-ink">模拟中的当前决策</summary>
            <ul className="mt-2 space-y-2">{starts.map((decision: any) => <li key={decision.actor_id}><span className="font-medium text-ink">{actorLabels.get(decision.actor_id) || decision.actor_id} · {actionLabel(decision.action_type)}</span><p>{decision.rationale}</p></li>)}</ul>
          </details>}
          {responses.length > 0 && <div className="mt-3 border-t border-edge pt-2.5">
            <p className="text-[10.5px] font-semibold text-ink">条件变化后的可能响应</p>
            <p className="mt-1 text-[10.5px] text-faint">以下是待验证的假设，不表示角色已经采取行动。</p>
            <ol className="mt-2 space-y-2 text-[11.5px] leading-relaxed text-mute">{responses.map((response: any) => <li key={response.actor_id}><span className="font-medium text-ink">{actorLabels.get(response.actor_id) || response.actor_id}</span><p>{response.condition} → {response.response}</p></li>)}</ol>
          </div>}
          <Section title="这条路径依赖的假设" items={textItems(scenario.assumptions)} tone="ink" />
          <ObservationSection title="留意这些信号" items={cards.filter((item) => item.kind === "trigger")} tone="jade" />
          <Section title="可能后果" items={textItems(scenario.consequences)} tone="ink" />
          <ObservationSection title="出现这些情况时重新判断" items={cards.filter((item) => item.kind === "invalidation")} tone="warn" />
          {refs.length > 0 && <details className="mt-3 border-t border-edge pt-2.5 text-[11px] text-mute">
            <summary className="cursor-pointer text-faint">依据资料 · {refs.join("、")}</summary>
            {cited.length ? <ul className="mt-2 space-y-2">{cited.map((fact: any) => <li key={fact.id}><p className="leading-relaxed">{fact.id}：{fact.statement}</p>{safeSourceUrl(fact.source_url) && <a href={safeSourceUrl(fact.source_url)} target="_blank" rel="noreferrer" className="mt-1 inline-block text-jade underline">查看来源</a>}</li>)}</ul> : <p className="mt-2">请在本次推演的原始证据图中核对以上编号。</p>}
          </details>}
        </div>;
      })}
      {warnings.length > 0 && <details className="rounded-lg border border-edge px-3 py-2.5 text-[11px] text-mute"><summary className="cursor-pointer">运行记录与限制 · {warnings.length} 条</summary><ul className="mt-2 space-y-2">{warnings.map((warning, index) => <li key={index} className="flex items-start gap-2"><AlertTriangle size={12} className="mt-0.5 shrink-0 text-faint" /><span className="break-words">{warning}</span></li>)}</ul></details>}
      {scenarios.length === 0 && <div className="rounded-card border border-edge p-5 text-[12px] leading-relaxed text-mute">本次没有形成可展示的情景。请查看运行记录；补充证据或调整参与方后，可以从证据图重新推演。</div>}
    </div>
  );
}

function actionLabel(value: string): string {
  return ({ WAIT: "等待", COMMUNICATE: "沟通", REGULATE: "监管行动", NEGOTIATE: "协商", OPERATE: "运营调整", ALLOCATE: "配置调整" } as Record<string, string>)[value] || value;
}

function ObservationSection({ title, items, tone }: { title: string; items: ReturnType<typeof observations>; tone: "jade" | "warn" }) {
  if (!items.length) return null;
  return <div className="mt-3 border-t border-edge pt-2.5">
    <p className={`mb-1 text-[10.5px] font-semibold ${tone === "jade" ? "text-jade" : "text-[#A15C22]"}`}>{title}</p>
    <ul className="space-y-2 text-[11.5px] leading-relaxed text-mute">{items.map((item) => <li key={item.id}>
      <p>{item.text}</p>
      {item.needsReview && <div className="mt-1 rounded-md bg-[#FFF9E8] px-2 py-1.5 text-[10.5px] text-[#775B19]"><p className="font-medium">先核对，再加入跟进</p>{item.reviewReasons?.map((reason, index) => <p key={index}>{reason}</p>)}</div>}
      {(item.source || item.window) && <p className="mt-1 text-[10.5px] text-faint">{item.source && `核对渠道：${item.source}`}{item.source && item.window && " · "}{item.window && `复核窗口：${item.window}`}{item.evidenceRefs?.length ? ` · ${item.evidenceRefs.join("、")}` : ""}</p>}
    </li>)}</ul>
  </div>;
}

function Section({ title, items, tone }: { title: string; items: string[]; tone: "jade" | "ink" | "warn" }) {
  if (!items.length) return null;
  const color = tone === "jade" ? "text-jade" : tone === "warn" ? "text-[#A15C22]" : "text-ink";
  return <div className="mt-3 border-t border-edge pt-2.5"><p className={`mb-1 text-[10.5px] font-semibold ${color}`}>{title}</p><ul className="space-y-1 text-[11.5px] leading-relaxed text-mute">{items.map((item, i) => <li key={i} className="flex gap-1.5"><span className="text-faint">•</span><span>{item}</span></li>)}</ul></div>;
}
