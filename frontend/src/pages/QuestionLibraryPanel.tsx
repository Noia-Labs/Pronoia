import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, ArrowRight, BookOpen, Check, ChevronDown, ChevronUp, Download, Loader2, Plus, RefreshCw, Save, Search, X } from "lucide-react";
import { api } from "../api";
import { useStore } from "../store";
import type { ModelLabQuestion, ModelLabQuestionSet } from "../types";
import { cls } from "../utils";
import { QUESTION_LIBRARY_SELECTION_KEY } from "./model-lab/questionLibrarySelection";

const INPUT = "w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[11.5px] text-ink outline-none transition placeholder:text-faint focus:border-violet/60 focus:ring-2 focus:ring-violet/10";
const BUTTON = "inline-flex items-center justify-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[10.5px] font-medium text-mute transition hover:border-violet/30 hover:text-violet disabled:cursor-not-allowed disabled:opacity-40";

function questionInput(question: ModelLabQuestion): Record<string, unknown> {
  return {
    code: question.code, experiment: question.experiment, category: question.category,
    role: question.role, method: question.method, scenario: question.scenario,
    prompt: question.prompt, skills: question.skills ?? [], deliverable: question.deliverable,
    gold_standard: question.gold_standard, metrics: question.metrics, risk: question.risk,
    priority: question.priority,
  };
}

function Details({ question }: { question: ModelLabQuestion }) {
  const fields = [
    ["场景", question.scenario], ["角色", question.role], ["分析方法", question.method],
    ["交付要求", question.deliverable], ["参考答案 / 评分依据", question.gold_standard],
    ["评价指标", question.metrics], ["风险提示", question.risk],
    ["相关能力", question.skills?.join("、")], ["优先级", question.priority],
  ].filter(([, value]) => Boolean(value));
  return <dl className="mt-4 grid gap-3 border-t border-edge pt-4 sm:grid-cols-2">{fields.map(([label, value]) => <div key={label} className={label === "参考答案 / 评分依据" ? "sm:col-span-2" : ""}><dt className="text-[9px] font-semibold text-faint">{label}</dt><dd className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-mute">{value}</dd></div>)}</dl>;
}

export default function QuestionLibraryPanel() {
  const setView = useStore((state) => state.setView);
  const [sets, setSets] = useState<ModelLabQuestionSet[]>([]);
  const [activeId, setActiveId] = useState("");
  const [activeSet, setActiveSet] = useState<ModelLabQuestionSet | null>(null);
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailRevision, setDetailRevision] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [editor, setEditor] = useState<"selection" | "custom" | null>(null);
  const [name, setName] = useState("");
  const [prompt, setPrompt] = useState("");
  const [scenario, setScenario] = useState("");
  const [customCategory, setCustomCategory] = useState("");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const listRequest = useRef(0);
  const mounted = useRef(true);
  const savingRef = useRef(false);

  const load = async () => {
    const request = ++listRequest.current;
    setLoading(true);
    setError(null);
    setDetailRevision((value) => value + 1);
    try {
      const response = await api.modelLabQuestionSets();
      if (!mounted.current || request !== listRequest.current) return;
      setSets(response.items);
      setActiveId((id) => response.items.some((item) => item.id === id) ? id : response.items.find((item) => item.id === "pronoia-finance-comprehensive-v1")?.id ?? response.items[0]?.id ?? "");
    } catch (reason) {
      if (mounted.current && request === listRequest.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      if (mounted.current && request === listRequest.current) setLoading(false);
    }
  };

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; ++listRequest.current; };
  }, []);

  useEffect(() => {
    setActiveSet(null);
    setSelected(new Set());
    setExpanded(new Set());
    setCategory("");
    setQuery("");
  }, [activeId]);

  useEffect(() => {
    let cancelled = false;
    setDetailError(null);
    if (!activeId) { setDetailLoading(false); return; }
    setDetailLoading(true);
    void api.modelLabQuestionSet(activeId).then((result) => {
      if (!cancelled) {
        setActiveSet(result);
        const validIds = new Set(result.questions?.map((question) => question.id));
        setSelected((current) => new Set([...current].filter((id) => validIds.has(id))));
      }
    }).catch((reason) => {
      if (!cancelled) setDetailError(reason instanceof Error ? reason.message : String(reason));
    }).finally(() => { if (!cancelled) setDetailLoading(false); });
    return () => { cancelled = true; };
  }, [activeId, detailRevision]);

  const questions = activeSet?.questions ?? [];
  const categories = useMemo(() => [...new Set(questions.map((question) => question.category || "未分类"))].sort(), [questions]);
  const visible = useMemo(() => {
    const term = query.trim().toLocaleLowerCase();
    return questions.filter((question) => (!category || (question.category || "未分类") === category) && (!term || [question.code, question.prompt, question.scenario, question.role, question.method].filter(Boolean).join(" ").toLocaleLowerCase().includes(term)));
  }, [questions, query, category]);
  const selectedQuestions = questions.filter((question) => selected.has(question.id));
  const allVisibleSelected = visible.length > 0 && visible.every((question) => selected.has(question.id));

  const toggle = (id: string) => setSelected((current) => { const next = new Set(current); if (next.has(id)) next.delete(id); else next.add(id); return next; });
  const toggleVisible = () => setSelected((current) => { const next = new Set(current); for (const question of visible) { if (allVisibleSelected) next.delete(question.id); else next.add(question.id); } return next; });
  const openEditor = (mode: "selection" | "custom") => {
    setEditor(mode);
    setName(mode === "selection" ? `${activeSet?.name ?? "问题库"} · 自选` : "");
    setPrompt(""); setScenario(""); setCustomCategory(""); setSaveError(null);
  };
  const save = async () => {
    if (savingRef.current || !name.trim() || !editor || (editor === "selection" ? !selectedQuestions.length : !prompt.trim())) return;
    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      const created = await api.modelLabCreateQuestionSet({
        name: name.trim(), version: "1",
        description: editor === "selection" ? `从「${activeSet?.name ?? "问题库"}」选编，共 ${selectedQuestions.length} 题。` : "自定义问答评测题库。",
        questions: editor === "selection" ? selectedQuestions.map(questionInput) : [{ code: "CUSTOM-001", prompt: prompt.trim(), scenario: scenario.trim() || null, category: customCategory.trim() || "自定义" }],
      });
      if (!mounted.current) return;
      ++listRequest.current;
      setLoading(false);
      setSets((current) => [created, ...current.filter((item) => item.id !== created.id)]);
      setActiveId(created.id);
      setEditor(null);
      setNotice(`已保存「${created.name}」，共 ${created.question_count} 题。`);
    } catch (reason) {
      if (mounted.current) setSaveError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      savingRef.current = false;
      if (mounted.current) setSaving(false);
    }
  };
  const exportJson = () => {
    if (!activeSet) return;
    const payload = { name: activeSet.name, description: activeSet.description, version: activeSet.version, questions: questions.map(questionInput) };
    const url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" }));
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `${activeSet.name.replace(/[\\/:*?"<>|]/g, "-")}.json`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  const useForQA = () => {
    if (!activeSet || !selectedQuestions.length) return;
    try {
      sessionStorage.setItem(QUESTION_LIBRARY_SELECTION_KEY, JSON.stringify({ questionSetId: activeSet.id, questionIds: selectedQuestions.map((question) => question.id) }));
      setView("backtest-model-lab");
    } catch {
      setDetailError("浏览器暂时无法保存选题，请允许此站点使用会话存储后重试。");
    }
  };

  return <section className="mt-4">
    <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
      <div><h2 className="font-serif text-[17px] font-semibold text-ink">问题库</h2><p className="mt-1 text-[10.5px] leading-relaxed text-mute">浏览 fevertest 迁入的研究问题，按场景选题，也可以整理自己的评测题库。</p></div>
      <div className="flex gap-2"><button type="button" className={BUTTON} onClick={() => void load()} disabled={loading || saving}><RefreshCw size={12} className={cls(loading && "animate-spin")} />刷新题库</button><button type="button" className={BUTTON} onClick={() => openEditor("custom")} disabled={saving}><Plus size={12} />新建问题</button></div>
    </div>
    {notice && <div role="status" className="mb-4 flex items-center justify-between gap-3 rounded-lg border border-jade/20 bg-jade-soft px-4 py-3 text-[11px] text-jade"><span className="inline-flex items-center gap-2"><Check size={13} />{notice}</span><button aria-label="关闭提示" type="button" onClick={() => setNotice(null)}><X size={12} /></button></div>}
    {error && <div role="alert" className="mb-4 flex items-start gap-2 rounded-lg border border-rise/20 bg-rise/5 px-4 py-3 text-[11px] text-rise"><AlertTriangle size={13} className="mt-0.5 shrink-0" />{error}</div>}
    <div className="grid items-start gap-4 md:grid-cols-[220px_minmax(0,1fr)] xl:grid-cols-[280px_minmax(0,1fr)]">
      <aside className="overflow-hidden rounded-xl border border-edge bg-card shadow-card">
        <div className="flex items-center justify-between border-b border-edge px-4 py-4"><span className="text-[11px] font-semibold text-ink">全部题库</span><span className="font-mono text-[10px] text-faint">{sets.length} 个</span></div>
        {loading && sets.length === 0 ? <div className="flex items-center gap-2 p-5 text-[11px] text-mute"><Loader2 size={13} className="animate-spin" />正在读取题库…</div> : sets.length === 0 ? <div className="p-5 text-[11px] leading-relaxed text-mute">还没有问题库，点击“新建问题”建立第一份题库。</div> : <div className="max-h-[260px] space-y-1 overflow-y-auto p-2 md:max-h-[620px]">{sets.map((set) => <button key={set.id} type="button" onClick={() => setActiveId(set.id)} disabled={saving} className={cls("w-full rounded-lg border px-3 py-3 text-left transition disabled:opacity-50", activeId === set.id ? "border-violet/25 bg-violet-soft/55" : "border-transparent hover:bg-paper")}><span className="flex items-start justify-between gap-2"><span className={cls("text-[11.5px] font-semibold", activeId === set.id ? "text-violet" : "text-ink")}>{set.name}</span><span className="shrink-0 rounded bg-card px-1.5 py-0.5 font-mono text-[9px] text-mute">{set.question_count} 题</span></span><span className="mt-1.5 block line-clamp-3 text-[9.5px] leading-relaxed text-mute">{set.description || "自定义问答评测题库"}</span><span className="mt-2 inline-flex items-center gap-1 text-[8.5px] text-faint"><BookOpen size={9} />{set.is_builtin ? "内置题库" : "自定义题库"} · v{set.version}</span></button>)}</div>}
      </aside>
      <div className="min-w-0 overflow-hidden rounded-xl border border-edge bg-card shadow-card">
        {detailLoading ? <div className="grid min-h-64 place-items-center text-[11px] text-mute"><span className="inline-flex items-center gap-2"><Loader2 size={14} className="animate-spin" />正在读取问题…</span></div> : <>
          {detailError && <div role="alert" className="m-4 flex gap-2 rounded-lg border border-rise/20 bg-rise/5 px-3 py-3 text-[11px] text-rise"><AlertTriangle size={13} className="shrink-0" />{detailError}</div>}
          {activeSet ? <>
            <div className="border-b border-edge px-5 py-4"><div className="flex flex-wrap items-start justify-between gap-3"><div className="min-w-0"><h3 className="font-serif text-[16px] font-semibold text-ink">{activeSet.name}</h3><p className="mt-1 max-w-3xl text-[10px] leading-relaxed text-mute">{activeSet.description}</p></div><button type="button" onClick={exportJson} className={BUTTON}><Download size={12} />导出题库</button></div><div className="mt-4 flex flex-col gap-2 sm:flex-row"><label className="relative flex-1"><Search size={13} className="absolute left-3 top-2.5 text-faint" /><input aria-label="搜索问题" value={query} onChange={(event) => setQuery(event.target.value)} className={cls(INPUT, "pl-8")} placeholder="搜索题号、问题、场景、角色…" /></label><select aria-label="问题分类" className={cls(INPUT, "sm:max-w-[220px]")} value={category} onChange={(event) => setCategory(event.target.value)}><option value="">全部分类</option>{categories.map((item) => <option key={item} value={item}>{item}</option>)}</select></div></div>
            <div className="flex flex-wrap items-center justify-between gap-2 border-b border-edge bg-paper/70 px-5 py-3"><div className="flex items-center gap-3"><label className="inline-flex cursor-pointer items-center gap-2 text-[10px] text-mute"><input type="checkbox" checked={allVisibleSelected} onChange={toggleVisible} disabled={!visible.length} className="accent-violet" />全选当前结果</label><span className="text-[9px] text-faint">显示 {visible.length} / {questions.length} 题</span></div><span className="text-[10px] font-medium text-violet">已选 {selectedQuestions.length} 题</span></div>
            <div className="max-h-[660px] divide-y divide-edge overflow-y-auto">{visible.length ? visible.map((question) => { const isExpanded = expanded.has(question.id); return <article key={question.id} className={cls("px-5 py-4 transition", selected.has(question.id) && "bg-violet-soft/15")}><div className="flex items-start gap-3"><input type="checkbox" checked={selected.has(question.id)} onChange={() => toggle(question.id)} aria-label={`选择 ${question.code}`} className="mt-1 accent-violet" /><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><span className="font-mono text-[10px] font-semibold text-violet">{question.code}</span>{question.category && <span className="rounded-full border border-edge bg-paper px-2 py-0.5 text-[9px] text-mute">{question.category}</span>}{question.scenario && <span className="text-[9px] text-faint">{question.scenario}</span>}</div><p className={cls("mt-2 whitespace-pre-wrap break-words text-[11.5px] leading-7 text-ink", !isExpanded && "line-clamp-3")}>{question.prompt}</p><button type="button" onClick={() => setExpanded((current) => { const next = new Set(current); if (next.has(question.id)) next.delete(question.id); else next.add(question.id); return next; })} aria-expanded={isExpanded} className="mt-2 inline-flex items-center gap-1 text-[10px] font-medium text-violet">{isExpanded ? <ChevronUp size={11} /> : <ChevronDown size={11} />}{isExpanded ? "收起详情" : "展开题目与评分依据"}</button>{isExpanded && <Details question={question} />}</div></div></article>; }) : <div className="py-14 text-center text-[11px] text-mute">没有匹配的问题，试试其他关键词或分类。</div>}</div>
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-edge bg-paper px-5 py-4"><p className="text-[9.5px] text-mute">选题后进入评测配置；此操作不会发起模型调用。</p><div className="flex flex-wrap gap-2"><button type="button" onClick={() => setSelected(new Set())} disabled={!selectedQuestions.length} className={BUTTON}>清空选择</button><button type="button" onClick={() => openEditor("selection")} disabled={!selectedQuestions.length || saving} className={BUTTON}><Save size={12} />另存为题库</button><button type="button" onClick={useForQA} disabled={!selectedQuestions.length || saving} className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-violet px-3 py-2 text-[10.5px] font-semibold text-white transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-40">用于问答评测<ArrowRight size={12} /></button></div></div>
          </> : !detailError && <div className="grid min-h-64 place-items-center text-[11px] text-mute">从左侧选择一个问题库。</div>}
        </>}
      </div>
    </div>
    {editor && <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/30 p-4" role="dialog" aria-modal="true" aria-labelledby="question-library-editor-title"><form onSubmit={(event) => { event.preventDefault(); void save(); }} className="max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-2xl border border-edge bg-card shadow-xl"><div className="flex items-center justify-between border-b border-edge px-6 py-5"><h3 id="question-library-editor-title" className="font-serif text-[18px] font-semibold text-ink">{editor === "selection" ? "保存自选题库" : "新建问题"}</h3><button type="button" onClick={() => setEditor(null)} disabled={saving} aria-label="关闭新建题库"><X size={16} className="text-mute" /></button></div><div className="space-y-4 p-6"><p className="text-[10.5px] leading-relaxed text-mute">{editor === "selection" ? `将已选的 ${selectedQuestions.length} 个问题及其评分依据保存为独立题库。` : "填写你的问题，保存后会建立一份可用于问答评测的自定义题库。"}</p><label className="block text-[11px] font-medium text-ink">题库名称<input required maxLength={120} value={name} onChange={(event) => setName(event.target.value)} className={cls(INPUT, "mt-1.5")} placeholder="例如：宏观研究 · 核心问题" /></label>{editor === "custom" && <><label className="block text-[11px] font-medium text-ink">问题内容<textarea required maxLength={100000} value={prompt} onChange={(event) => setPrompt(event.target.value)} rows={6} className={cls(INPUT, "mt-1.5 leading-relaxed")} placeholder="描述希望模型回答的问题，以及需要遵循的约束…" /></label><div className="grid gap-3 sm:grid-cols-2"><label className="block text-[11px] font-medium text-ink">场景 <span className="font-normal text-faint">可选</span><input maxLength={200} value={scenario} onChange={(event) => setScenario(event.target.value)} className={cls(INPUT, "mt-1.5")} placeholder="例如：降息周期" /></label><label className="block text-[11px] font-medium text-ink">分类 <span className="font-normal text-faint">可选</span><input maxLength={120} value={customCategory} onChange={(event) => setCustomCategory(event.target.value)} className={cls(INPUT, "mt-1.5")} placeholder="例如：宏观分析" /></label></div></>}{saveError && <div role="alert" className="rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[11px] text-rise">{saveError}</div>}</div><div className="flex justify-end gap-2 border-t border-edge px-6 py-4"><button type="button" className={BUTTON} onClick={() => setEditor(null)} disabled={saving}>取消</button><button type="submit" disabled={saving || !name.trim() || (editor === "custom" && !prompt.trim())} className="inline-flex items-center gap-2 rounded-lg bg-violet px-4 py-2 text-[11px] font-semibold text-white disabled:opacity-40">{saving ? <Loader2 size={12} className="animate-spin" /> : <Save size={12} />}{saving ? "保存中…" : "保存题库"}</button></div></form></div>}
  </section>;
}
