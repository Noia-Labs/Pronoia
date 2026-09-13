import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  ArrowLeft,
  ArrowRight,
  BarChart3,
  CalendarRange,
  Check,
  CheckCircle2,
  Database,
  Layers3,
  Loader2,
  RefreshCw,
  Search,
  X,
} from "lucide-react";
import type { ArenaItem } from "../../types";
import { cls } from "../../utils";
import { comparisonKindLabel } from "./arenaComparison";
import {
  arenaWorkspaceApi,
  newWorkspaceRequestId,
  workspaceFrequencyOptions,
  workspaceModelTrack,
  workspaceTargetAvailabilityReason,
  workspaceTargetKindsForModel,
  workspaceTargetScope,
  type ArenaTrack,
  type WorkspaceCatalog,
  type WorkspaceModel,
  type WorkspaceTarget,
  type WorkspaceTargetScope,
} from "./arenaWorkspace";
import { ArenaFieldArt, ArenaLineup, ArenaSeal } from "./ArenaMotif";

const INPUT = "mt-1.5 w-full min-w-0 rounded-lg border border-edgeDark/70 bg-card px-3 py-2.5 text-[13px] text-ink outline-none focus:border-brand disabled:opacity-50";
const BUTTON = "inline-flex items-center justify-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[12px] font-medium text-mute hover:border-brand/40 hover:text-ink disabled:cursor-not-allowed disabled:opacity-45";
const PANEL = "rounded-2xl border border-edgeDark/65 bg-card p-4 sm:p-5";
const NO_INITIAL_MODELS: string[] = [];
const EVENT_HORIZONS = [1, 3, 5, 7, 15, 30, 60];

type WizardStep = 1 | 2 | 3 | 4;
type Form = {
  track: ArenaTrack | null;
  modelIds: string[];
  targetScope: WorkspaceTargetScope;
  targetId: string;
  frequency: string;
  startDate: string;
  endDate: string;
  holdingBars: string;
  feeBps: string;
  slippageBps: string;
  initialCapital: string;
  name: string;
};

const initialForm = (modelIds: string[] = []): Form => ({
  track: null,
  modelIds,
  targetScope: "asset",
  targetId: "",
  frequency: "",
  startDate: "",
  endDate: "",
  holdingBars: "3",
  feeBps: "3",
  slippageBps: "2",
  initialCapital: "100000",
  name: "",
});

const day = (value?: string | null) => value?.slice(0, 10) ?? "";
const isDaily = (value?: string | null) => !value || ["1d", "d", "day", "daily"].includes(value.toLocaleLowerCase());
const frequencyLabel = (value: string) => ({
  "1m": "1 分钟 K",
  "5m": "5 分钟 K",
  "15m": "15 分钟 K",
  "30m": "30 分钟 K",
  "60m": "60 分钟 K",
  "1h": "1 小时 K",
  "1d": "日 K",
  "1w": "周 K",
} as Record<string, string>)[value] ?? value;

function targetDates(target: WorkspaceTarget): { startDate: string; endDate: string } {
  const startDate = day(target.start_date) || day(target.event_time);
  let endDate = day(target.end_date);
  if (!endDate && startDate) {
    const end = new Date(`${startDate}T00:00:00Z`);
    if (Number.isFinite(end.valueOf())) {
      end.setUTCDate(end.getUTCDate() + 30);
      endDate = end.toISOString().slice(0, 10);
    }
  }
  return { startDate, endDate };
}

function assetKey(target: WorkspaceTarget): string {
  return `${target.market}\u0000${target.symbol}`;
}

function targetEventCount(target: WorkspaceTarget): number | null {
  const count = target.event_count ?? target.total_events;
  return typeof count === "number" && Number.isFinite(count) ? count : null;
}

function targetEventHorizons(target: WorkspaceTarget | null): number[] {
  const values = (target?.oracle?.available_horizons ?? []).map((value) => Number(String(value).toLocaleLowerCase().replace(/^t\+?/, "")));
  const supported = [...new Set(values.filter((value) => EVENT_HORIZONS.includes(value)))];
  return supported.length ? supported : EVENT_HORIZONS;
}

function StepTitle({ number, title, note }: { number: number; title: string; note: string }) {
  return <div className="flex items-start gap-3">
    <span className="grid h-8 w-8 shrink-0 place-items-center rounded-t-full rounded-b-md border border-brand/20 bg-brand-soft/40 font-mono text-[11px] text-brand">{String(number).padStart(2, "0")}</span>
    <div>
      <h3 id={`arena-workspace-step-${number}`} className="font-serif text-[17px] font-semibold text-ink">{title}</h3>
      <p className="mt-1 text-[12px] leading-relaxed text-mute">{note}</p>
    </div>
  </div>;
}

export default function ArenaWorkspaceComposer({
  open,
  onClose,
  onCreated,
  initialModelIds = NO_INITIAL_MODELS,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (arena: ArenaItem) => void;
  initialModelIds?: string[];
}) {
  const [catalog, setCatalog] = useState<WorkspaceCatalog | null>(null);
  const [loading, setLoading] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  const [activeStep, setActiveStep] = useState<WizardStep>(1);
  const [form, setForm] = useState<Form>(() => initialForm());
  const [modelQuery, setModelQuery] = useState("");
  const [targetQuery, setTargetQuery] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const mainRef = useRef<HTMLElement>(null);
  const savingRef = useRef(false);
  const requestIdRef = useRef("");
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    if (!open) return;
    const initialIds = [...new Set(initialModelIds)].slice(0, 8);
    setForm(initialForm(initialIds));
    setActiveStep(1);
    setModelQuery("");
    setTargetQuery("");
    setError(null);
    setSaving(false);
    requestIdRef.current = newWorkspaceRequestId();
    const previousFocus = document.activeElement;
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !savingRef.current) {
        event.preventDefault();
        closeRef.current();
      }
      if (event.key !== "Tab") return;
      const controls = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href]",
      ) ?? []).filter((node) => node.offsetParent !== null);
      const first = controls[0];
      const last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first && last) {
        event.preventDefault();
        last.focus();
      }
      if (!event.shiftKey && document.activeElement === last && first) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", keydown);
    dialogRef.current?.focus();
    return () => {
      document.removeEventListener("keydown", keydown);
      if (previousFocus instanceof HTMLElement) previousFocus.focus();
    };
  }, [open, initialModelIds]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setLoading(true);
    setCatalogError(null);
    void arenaWorkspaceApi.catalog(controller.signal).then((response) => {
      if (controller.signal.aborted) return;
      setCatalog(response);
      setForm((current) => {
        const availableInitial = current.modelIds
          .map((id) => response.models.find((model) => model.id === id && model.available))
          .filter((model): model is WorkspaceModel => Boolean(model));
        const track = availableInitial[0] ? workspaceModelTrack(availableInitial[0]) : current.track;
        const modelIds = track
          ? availableInitial.filter((model) => workspaceModelTrack(model) === track).map((model) => model.id)
          : [];
        return { ...current, track, targetScope: track === "event" ? "single_event" : "asset", modelIds };
      });
    }).catch((reason) => {
      if (!controller.signal.aborted) setCatalogError(reason instanceof Error ? reason.message : "无法读取已保存模型，请重试。");
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [open, revision]);

  useEffect(() => {
    mainRef.current?.scrollTo({ top: 0, behavior: "smooth" });
  }, [activeStep]);

  const update = (patch: Partial<Form>) => {
    setForm((current) => ({ ...current, ...patch }));
    setError(null);
    requestIdRef.current = newWorkspaceRequestId();
  };

  const chooseTrack = (track: ArenaTrack) => {
    if (form.track === track) return;
    const retainedIds = (catalog?.models ?? [])
      .filter((model) => form.modelIds.includes(model.id) && workspaceModelTrack(model) === track)
      .map((model) => model.id);
    setModelQuery("");
    setTargetQuery("");
    update({
      track,
      modelIds: retainedIds,
      targetScope: track === "quant" ? "asset" : "single_event",
      targetId: "",
      frequency: "",
      startDate: "",
      endDate: "",
    });
  };

  const allModels = catalog?.models ?? [];
  const laneModels = useMemo(() => {
    if (!form.track) return [];
    const needle = modelQuery.trim().toLocaleLowerCase();
    return allModels.filter((model) => workspaceModelTrack(model) === form.track && [
      model.name,
      model.description,
      model.model_id,
      comparisonKindLabel(model.kind),
    ].filter(Boolean).join(" ").toLocaleLowerCase().includes(needle));
  }, [allModels, form.track, modelQuery]);

  const selectedModels = allModels.filter((model) => form.modelIds.includes(model.id));
  const modelSelectionValid = Boolean(form.track)
    && form.modelIds.length >= 2
    && form.modelIds.length <= 8
    && selectedModels.length === form.modelIds.length
    && selectedModels.every((model) => model.available && workspaceModelTrack(model) === form.track);

  const targetBlockReason = (item: WorkspaceTarget) => {
    const unavailable = workspaceTargetAvailabilityReason(item);
    if (unavailable) return unavailable;
    const acceptedKinds = workspaceTargetKindsForModel(item);
    const incompatible = selectedModels.find((model) => (
      model.supported_target_kinds
      && !model.supported_target_kinds.some((kind) => acceptedKinds.includes(kind))
    ) || (model.compatible_target_ids && !model.compatible_target_ids.includes(item.id)));
    if (incompatible) return `${incompatible.name} 不支持此目标，请换选目标或参赛者。`;
    const requiresDaily = selectedModels.some((model) => ["pronoia", "external_model", "raw_model", "raw"].includes(model.kind));
    if (form.track === "event" && requiresDaily && !isDaily(item.frequency)) return "事件模型与 Pronoia 目前使用日 K，请选择日线事件数据。";
    return "";
  };

  const targetChoices = useMemo(() => {
    const needle = targetQuery.trim().toLocaleLowerCase();
    const matches = (catalog?.targets ?? []).filter((item) => (
      workspaceTargetScope(item) === form.targetScope
      && [item.name, item.symbol, item.market, item.event_time].filter(Boolean).join(" ").toLocaleLowerCase().includes(needle)
    ));
    if (form.targetScope !== "asset") return matches;
    const groups = new Map<string, WorkspaceTarget>();
    for (const item of matches) if (!groups.has(assetKey(item))) groups.set(assetKey(item), item);
    return [...groups.values()];
  }, [catalog, form.targetScope, targetQuery]);

  const target = catalog?.targets.find((item) => item.id === form.targetId) ?? null;
  const assetVariants = useMemo(() => {
    if (!target || workspaceTargetScope(target) !== "asset") return [];
    const key = assetKey(target);
    return (catalog?.targets ?? []).filter((item) => workspaceTargetScope(item) === "asset" && assetKey(item) === key);
  }, [catalog, target]);
  const availableFrequencies = useMemo(() => [...new Set(assetVariants.flatMap((item) => [
    ...workspaceFrequencyOptions(item).map((option) => option.frequency),
  ]))], [assetVariants]);
  const selectedFrequency = form.frequency || target?.frequency || "";
  const eventHorizons = targetEventHorizons(target);

  const chooseTarget = (item: WorkspaceTarget) => {
    const variants = workspaceTargetScope(item) === "asset"
      ? (catalog?.targets ?? []).filter((candidate) => workspaceTargetScope(candidate) === "asset" && assetKey(candidate) === assetKey(item))
      : [item];
    const chosen = variants.find((candidate) => candidate.frequency === form.frequency) ?? item;
    const options = workspaceFrequencyOptions(chosen);
    const frequency = chosen.frequency || chosen.default_frequency || options[0]?.frequency || "";
    const option = options.find((candidate) => candidate.frequency === frequency);
    const horizons = targetEventHorizons(chosen);
    update({
      targetId: chosen.id,
      frequency,
      startDate: day(option?.start_date) || targetDates(chosen).startDate,
      endDate: day(option?.end_date) || targetDates(chosen).endDate,
      ...(workspaceTargetScope(chosen) !== "asset" && !horizons.includes(Number(form.holdingBars)) ? { holdingBars: String(horizons[0]) } : {}),
    });
  };

  const chooseFrequency = (frequency: string) => {
    if (!target) return;
    const variant = assetVariants.find((item) => item.frequency === frequency
      || workspaceFrequencyOptions(item).some((option) => option.frequency === frequency)) ?? target;
    const option = workspaceFrequencyOptions(variant).find((item) => item.frequency === frequency);
    update({
      targetId: variant.id,
      frequency,
      startDate: day(option?.start_date) || targetDates(variant).startDate,
      endDate: day(option?.end_date) || targetDates(variant).endDate,
    });
  };

  const chooseTargetScope = (targetScope: WorkspaceTargetScope) => {
    if (form.targetScope === targetScope) return;
    setTargetQuery("");
    update({ targetScope, targetId: "", frequency: "", startDate: "", endDate: "" });
  };

  const targetSelectionValid = Boolean(target)
    && workspaceTargetScope(target as WorkspaceTarget) === form.targetScope
    && !targetBlockReason(target as WorkspaceTarget);
  const frequencyValid = form.track !== "quant" || Boolean(selectedFrequency);
  const dateSelectionValid = /^\d{4}-\d{2}-\d{2}$/.test(form.startDate)
    && /^\d{4}-\d{2}-\d{2}$/.test(form.endDate)
    && form.startDate <= form.endDate;
  const horizonValid = form.track !== "event" || eventHorizons.includes(Number(form.holdingBars));
  const rulesValid = form.holdingBars.trim() !== ""
    && Number.isInteger(Number(form.holdingBars))
    && Number(form.holdingBars) >= 1
    && Number(form.holdingBars) <= 1000
    && horizonValid
    && form.feeBps.trim() !== ""
    && Number.isFinite(Number(form.feeBps))
    && Number(form.feeBps) >= 0
    && Number(form.feeBps) <= 1000
    && form.slippageBps.trim() !== ""
    && Number.isFinite(Number(form.slippageBps))
    && Number(form.slippageBps) >= 0
    && Number(form.slippageBps) <= 1000
    && form.initialCapital.trim() !== ""
    && Number.isFinite(Number(form.initialCapital))
    && Number(form.initialCapital) > 0
    && Number(form.initialCapital) <= 1e12;

  const stepComplete: Record<WizardStep, boolean> = {
    1: Boolean(form.track),
    2: modelSelectionValid,
    3: targetSelectionValid,
    4: frequencyValid && dateSelectionValid && rulesValid,
  };
  const disabledReason = loading
    ? "正在读取参赛者与目标…"
    : catalogError
      ? "请先重新读取参赛者与目标。"
      : !form.track
        ? "请选择 Quant Arena 或 Event Arena。"
        : form.modelIds.length < 2
          ? `请${form.modelIds.length ? "再" : ""}选择 ${2 - form.modelIds.length} 个同类参赛者。`
          : !modelSelectionValid
            ? "所选参赛者类型不同或暂不可用，请调整选择。"
            : !target
              ? form.track === "quant" ? "请选择共享指数或标的。" : `请选择${form.targetScope === "event_set" ? "事件集" : "单事件"}。`
              : targetBlockReason(target)
                ? targetBlockReason(target)
                : !frequencyValid
                  ? "请选择本场所有量化策略共用的 K 线周期。"
                  : !form.startDate || !form.endDate
                    ? "请选择开始日期和结束日期。"
                    : !dateSelectionValid
                      ? "结束日期不能早于开始日期。"
                      : !rulesValid
                        ? "请检查预测窗口、成本和资金设置。"
                        : "";
  const canStart = !saving && !disabledReason;

  const goToStep = (step: WizardStep) => {
    const prerequisites = ([1, 2, 3, 4] as WizardStep[]).filter((item) => item < step);
    if (prerequisites.every((item) => stepComplete[item])) setActiveStep(step);
  };

  const start = async () => {
    if (!canStart || savingRef.current || !target || !form.track) return;
    savingRef.current = true;
    setSaving(true);
    setError(null);
    try {
      const arena = await arenaWorkspaceApi.create({
        track: form.track,
        model_ids: [...form.modelIds],
        target_id: target.id,
        target_scope: form.targetScope,
        ...(form.track === "quant" && selectedFrequency ? { frequency: selectedFrequency } : {}),
        start_date: form.startDate,
        end_date: form.endDate,
        ...(form.name.trim() ? { name: form.name.trim() } : {}),
        holding_bars: Number(form.holdingBars),
        fee_bps: Number(form.feeBps),
        slippage_bps: Number(form.slippageBps),
        initial_capital: Number(form.initialCapital),
        request_id: requestIdRef.current,
      });
      onCreated(arena);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "未能启动比较，请重试。");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  };

  if (!open) return null;

  const steps: Array<{ id: WizardStep; label: string; note: string }> = [
    { id: 1, label: "选择赛道", note: form.track === "quant" ? "Quant" : form.track === "event" ? "Event" : "不可混排" },
    { id: 2, label: "选择参赛者", note: `${form.modelIds.length}/8` },
    { id: 3, label: "共享目标", note: target?.name || "单事件 / 事件集" },
    { id: 4, label: "周期与规则", note: stepComplete[4] ? "已确认" : "待设置" },
  ];
  const trackCounts = {
    quant: allModels.filter((model) => workspaceModelTrack(model) === "quant" && model.available).length,
    event: allModels.filter((model) => workspaceModelTrack(model) === "event" && model.available).length,
  };
  const currentStepReason = activeStep === 1
    ? (form.track ? `已选择 ${form.track === "quant" ? "Quant Arena" : "Event Arena"}。` : "两类参赛者独立比较、独立排名。")
    : activeStep === 2
      ? (modelSelectionValid ? `${form.modelIds.length} 个同类参赛者已就绪。` : `请选择 2–8 个${form.track === "quant" ? "量化策略" : "事件模型"}。`)
      : activeStep === 3
        ? (targetSelectionValid ? `已选择共享目标：${target?.name}。` : disabledReason)
        : (disabledReason || `${form.modelIds.length} 个参赛者已就绪，结果将自动保存。`);

  return <div
    className="fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-2 backdrop-blur-sm sm:p-5"
    onMouseDown={(event) => { if (event.target === event.currentTarget && !savingRef.current) onClose(); }}
  >
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-labelledby="arena-workspace-title"
      tabIndex={-1}
      className="flex max-h-[94dvh] w-full max-w-[1080px] flex-col overflow-hidden rounded-[22px] border border-edgeDark bg-paper shadow-pop outline-none"
    >
      <header className="relative shrink-0 overflow-hidden border-b border-edge bg-[linear-gradient(110deg,#ffffff,#faf6ef)] px-5 py-4 sm:px-7">
        <ArenaFieldArt className="absolute -right-3 -top-20 hidden h-[225px] w-[380px] text-brand/15 sm:block" />
        <div className="relative flex items-start justify-between gap-4">
          <div className="flex items-start gap-3.5">
            <ArenaSeal className="mt-0.5 hidden h-11 w-11 text-brand sm:block" />
            <div>
              <p className="mb-1 text-[9px] tracking-[.25em] text-brand">PREPARE THE ARENA</p>
              <h2 id="arena-workspace-title" className="font-serif text-[23px] font-semibold text-ink">新建 Arena</h2>
              <p className="mt-1.5 text-[11px] leading-relaxed text-mute">同赛道、同目标、同快照和同规则，结果才可公平比较。</p>
            </div>
          </div>
          <button type="button" onClick={onClose} disabled={saving} aria-label="关闭新建 Arena" className="rounded-full border border-edge bg-card/70 p-2 text-mute hover:bg-paper disabled:opacity-40"><X size={18} /></button>
        </div>
      </header>

      <nav aria-label="Arena 设置步骤" className="grid shrink-0 grid-cols-4 border-b border-edge bg-card/80">
        {steps.map((step) => {
          const selected = activeStep === step.id;
          const reachable = ([1, 2, 3, 4] as WizardStep[]).filter((item) => item < step.id).every((item) => stepComplete[item]);
          return <button
            key={step.id}
            type="button"
            aria-current={selected ? "step" : undefined}
            disabled={!reachable || saving}
            onClick={() => goToStep(step.id)}
            className={cls(
              "min-w-0 border-l border-edge px-2 py-3 text-left first:border-l-0 sm:px-4",
              selected ? "bg-ink text-card" : "text-mute hover:bg-edge/25 disabled:cursor-not-allowed disabled:opacity-55",
            )}
          >
            <span className="flex items-center gap-2">
              <span className={cls(
                "grid h-6 w-6 shrink-0 place-items-center rounded-full border font-mono text-[9px]",
                selected ? "border-card/30 bg-card/10" : stepComplete[step.id] ? "border-jade/25 bg-jade-soft text-jade" : "border-edge bg-paper",
              )}>{stepComplete[step.id] && !selected ? <Check size={11} /> : `0${step.id}`}</span>
              <span className="min-w-0">
                <span className="block truncate text-[10px] font-semibold sm:text-[11.5px]">{step.label}</span>
                <span className={cls("mt-0.5 hidden truncate text-[9px] sm:block", selected ? "text-card/55" : "text-faint")}>{step.note}</span>
              </span>
            </span>
          </button>;
        })}
      </nav>

      <main ref={mainRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-7">
        {loading ? <div className="flex min-h-64 items-center justify-center gap-2 text-[13px] text-mute"><Loader2 size={18} className="animate-spin" />正在读取已保存的参赛者与目标…</div> : catalogError ? <div role="alert" className="rounded-xl border border-rise/20 bg-rise/5 p-5 text-[13px] text-rise">
          <p className="break-words">{catalogError}</p>
          <button type="button" onClick={() => setRevision((value) => value + 1)} className={cls(BUTTON, "mt-3")}><RefreshCw size={14} />重新读取</button>
        </div> : <fieldset disabled={saving} className="min-w-0">
          {activeStep === 1 && <section aria-labelledby="arena-workspace-step-1" className={PANEL}>
            <StepTitle number={1} title="选择独立赛道" note="Quant 与 Event 使用不同真值和指标，不会互相混选或混合排名。" />
            <div className="mt-5 grid gap-3 sm:grid-cols-2">
              {([
                { track: "quant" as const, title: "Quant Arena", subtitle: "量化策略 vs 量化策略", icon: <BarChart3 size={21} />, body: "在同一指数 / 标的、K 线周期、日期、行情快照、基准与交易成本上比较。", count: trackCounts.quant },
                { track: "event" as const, title: "Event Arena", subtitle: "事件模型 vs 事件模型", icon: <Layers3 size={21} />, body: "在同一单事件或事件集、标签、Oracle、预测窗口与行情口径上比较。", count: trackCounts.event },
              ]).map((item) => <button
                key={item.track}
                type="button"
                aria-pressed={form.track === item.track}
                onClick={() => chooseTrack(item.track)}
                className={cls(
                  "relative min-h-44 rounded-2xl border p-5 text-left transition",
                  form.track === item.track ? "border-brand/50 bg-brand-soft/25 shadow-[inset_4px_0_0_#B45309]" : "border-edge bg-paper/40 hover:border-brand/30 hover:bg-brand-soft/10",
                )}
              >
                <span className={cls("grid h-10 w-10 place-items-center rounded-xl", item.track === "quant" ? "bg-jade-soft text-jade" : "bg-violet-soft text-violet")}>{item.icon}</span>
                <span className="mt-4 flex items-start justify-between gap-3">
                  <span>
                    <span className="block font-serif text-[18px] font-semibold text-ink">{item.title}</span>
                    <span className="mt-1 block text-[12px] font-medium text-brand">{item.subtitle}</span>
                  </span>
                  {form.track === item.track && <CheckCircle2 size={19} className="shrink-0 text-brand" />}
                </span>
                <span className="mt-3 block text-[11px] leading-relaxed text-mute">{item.body}</span>
                <span className="mt-3 block text-[10px] text-faint">{item.count} 个当前可用参赛者</span>
              </button>)}
            </div>
          </section>}

          {activeStep === 2 && <section aria-labelledby="arena-workspace-step-2" className={PANEL}>
            <div className="flex flex-wrap items-start justify-between gap-3">
              <StepTitle number={2} title={form.track === "quant" ? "选择量化策略" : "选择事件模型"} note="选择 2–8 个同类参赛者；另一赛道的配置不会出现在本页。" />
              <span className={cls("rounded-full border px-3 py-1.5 text-[11px]", modelSelectionValid ? "border-jade/20 bg-jade-soft text-jade" : "border-edge bg-paper text-mute")}>已选 {form.modelIds.length} / 8</span>
            </div>
            {(allModels.filter((model) => form.track && workspaceModelTrack(model) === form.track).length > 6) && <label className="relative mt-4 block">
              <Search size={14} className="absolute left-3 top-3 text-faint" />
              <input aria-label="搜索同赛道参赛者" value={modelQuery} onChange={(event) => setModelQuery(event.target.value)} placeholder="搜索名称或 API 模型 ID" className={cls(INPUT, "mt-0 pl-9")} />
            </label>}
            <div className="mt-4 grid gap-2 sm:grid-cols-2">{laneModels.map((model) => {
              const selected = form.modelIds.includes(model.id);
              const disabled = !model.available || (form.modelIds.length >= 8 && !selected);
              return <label key={model.id} className={cls(
                "relative flex min-w-0 items-start gap-3 rounded-xl border p-3.5 transition",
                disabled ? "cursor-not-allowed border-edge bg-paper opacity-65" : selected ? "cursor-pointer border-brand/45 bg-brand-soft/25 shadow-[inset_3px_0_0_#B45309]" : "cursor-pointer border-edge bg-paper/25 hover:border-brand/30 hover:bg-brand-soft/15",
              )}>
                <input type="checkbox" checked={selected} disabled={disabled} onChange={() => update({ modelIds: selected ? form.modelIds.filter((id) => id !== model.id) : [...form.modelIds, model.id] })} className="mt-1 h-4 w-4 shrink-0 accent-brand" />
                <span className="min-w-0">
                  <span className="block break-words text-[13px] font-semibold text-ink">{model.name}</span>
                  <span className="mt-1 block break-words text-[11px] text-mute">{comparisonKindLabel(model.kind)}{model.model_id ? ` · ${model.model_id}` : ""}</span>
                  {model.description && <span className="mt-1 block break-words text-[11px] leading-relaxed text-mute">{model.description}</span>}
                  {!model.available && <span className="mt-1.5 block break-words text-[11px] leading-relaxed text-amber-700">{model.reason || "当前不可用"}</span>}
                </span>
              </label>;
            })}</div>
            {!laneModels.length && <p className="py-8 text-center text-[12px] text-mute">{modelQuery ? "没有匹配的同赛道参赛者。" : form.track === "quant" ? "暂无可用量化策略，请先完成并保存量化回测。" : "暂无可用事件模型，请先保存事件模型连接或预测配置。"}</p>}
            {form.modelIds.length === 8 && <p className="mt-2 text-[11px] text-mute">已选满 8 个，如需替换请先取消一个。</p>}
            {selectedModels.length >= 2 && <div className="mt-4 rounded-xl border border-brand/15 bg-[linear-gradient(90deg,#fbf6ed,#ffffff,#f0f7f5)] px-4 py-3">
              <p className="mb-2 text-center text-[9px] tracking-[.2em] text-faint">本场阵容 · {form.track === "quant" ? "QUANT" : "EVENT"}</p>
              <ArenaLineup names={selectedModels.map((model) => model.name)} count={selectedModels.length} />
            </div>}
          </section>}

          {activeStep === 3 && <section aria-labelledby="arena-workspace-step-3" className={PANEL}>
            <StepTitle number={3} title={form.track === "quant" ? "选择共享标的" : "选择共享事件目标"} note={form.track === "quant" ? "本场所有策略使用同一指数 / 标的；K 线周期在下一步统一设置。" : "单事件用于聚焦复核；事件集保留冻结快照和整体评测，可在结果中下钻。"} />
            {form.track === "event" && <div className="mt-4 inline-flex rounded-xl border border-edge bg-paper p-1" role="group" aria-label="事件目标类型">
              <button type="button" aria-pressed={form.targetScope === "single_event"} onClick={() => chooseTargetScope("single_event")} className={cls(BUTTON, "border-0", form.targetScope === "single_event" && "bg-ink text-card hover:text-card")}>单事件</button>
              <button type="button" aria-pressed={form.targetScope === "event_set"} onClick={() => chooseTargetScope("event_set")} className={cls(BUTTON, "border-0", form.targetScope === "event_set" && "bg-ink text-card hover:text-card")}>事件集</button>
            </div>}
            <label className="relative mt-4 block">
              <Search size={14} className="absolute left-3 top-3 text-faint" />
              <input aria-label="搜索共享目标" value={targetQuery} onChange={(event) => setTargetQuery(event.target.value)} placeholder={form.track === "quant" ? "搜索指数 / 标的名称或代码" : form.targetScope === "event_set" ? "搜索事件集名称、市场或代码" : "搜索事件名称、代码或日期"} className={cls(INPUT, "mt-0 pl-9")} />
            </label>
            <div className="mt-4 space-y-2">{targetChoices.slice(0, 100).map((item) => {
              const selected = target ? (form.targetScope === "asset" ? assetKey(target) === assetKey(item) : target.id === item.id) : false;
              const blocked = targetBlockReason(item);
              const relatedFrequencies = form.targetScope === "asset" ? [...new Set((catalog?.targets ?? []).filter((candidate) => workspaceTargetScope(candidate) === "asset" && assetKey(candidate) === assetKey(item)).flatMap((candidate) => workspaceFrequencyOptions(candidate).map((option) => option.frequency)))] : [];
              const count = targetEventCount(item);
              return <button
                type="button"
                key={item.id}
                aria-pressed={selected}
                disabled={Boolean(blocked)}
                onClick={() => chooseTarget(item)}
                className={cls("flex w-full items-center justify-between gap-3 rounded-xl border px-3.5 py-3 text-left disabled:cursor-not-allowed disabled:opacity-60", selected ? "border-brand/40 bg-brand-soft/25" : "border-edge bg-paper/50 hover:border-brand/30")}
              >
                <span className="min-w-0">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="break-words text-[13px] font-semibold text-ink">{item.name}</span>
                    {form.targetScope === "event_set" && count !== null && <span className="rounded bg-violet-soft px-1.5 py-0.5 text-[9px] text-violet">{count} 个事件</span>}
                  </span>
                  <span className="mt-1 block text-[11px] text-mute">{item.symbol || "多标的"} · {item.market || "多市场"}{item.event_time ? ` · ${day(item.event_time)}` : ""}</span>
                  {relatedFrequencies.length > 0 && <span className="mt-1.5 block text-[10px] text-faint">可用周期：{relatedFrequencies.map(frequencyLabel).join(" / ")}</span>}
                  {form.targetScope === "event_set" && (item.event_start_date || item.event_end_date || item.start_date || item.end_date) && <span className="mt-1.5 block text-[10px] text-faint">事件范围：{day(item.event_start_date) || day(item.start_date) || "—"} → {day(item.event_end_date) || day(item.end_date) || "—"}</span>}
                  {form.targetScope === "event_set" && item.oracle?.available_horizons?.length ? <span className="mt-1 block text-[10px] text-faint">Oracle 标签：{item.oracle.available_horizons.map((value) => value.toUpperCase()).join(" / ")}</span> : null}
                  {blocked && <span className="mt-1.5 block text-[11px] leading-relaxed text-amber-700">{blocked}</span>}
                </span>
                {selected && <CheckCircle2 size={17} className="shrink-0 text-brand" />}
              </button>;
            })}</div>
            {!targetChoices.length && <p className="py-8 text-center text-[12px] text-mute">{targetQuery ? "没有匹配的共享目标，请调整搜索。" : form.track === "quant" ? "暂无可用行情，请先在数据管理中导入指数 / 标的行情。" : form.targetScope === "event_set" ? "暂无可用事件集，请先在数据管理中导入并冻结事件集。" : "暂无可用单事件，请先在数据管理中导入事件集。"}</p>}
            {targetChoices.length > 100 && <p className="mt-2 text-[11px] text-mute">已展示前 100 项，请搜索缩小范围。</p>}
            {target && <div className="mt-4 flex items-start gap-2 rounded-xl border border-jade/20 bg-jade-soft px-3.5 py-3 text-[11px] leading-relaxed text-jade">
              <Database size={14} className="mt-0.5 shrink-0" />
              <span>已选：{target.name}{(workspaceFrequencyOptions(target).find((item) => item.frequency === selectedFrequency)?.snapshot_hash || target.snapshot_hash) ? ` · 冻结快照 ${(workspaceFrequencyOptions(target).find((item) => item.frequency === selectedFrequency)?.snapshot_hash || target.snapshot_hash)?.slice(0, 12)}` : ""}</span>
            </div>}
          </section>}

          {activeStep === 4 && <section aria-labelledby="arena-workspace-step-4" className={PANEL}>
            <StepTitle number={4} title="设置周期、时间和统一规则" note={form.track === "quant" ? "K 线周期由本场统一选择，不再固化在量化策略上。" : "事件模型保留日 K 约束，并共用同一预测窗口、标签与 Oracle 口径。"} />
            <div className="mt-5 grid gap-3 sm:grid-cols-2">
              {form.track === "quant" ? <label className="text-[12px] font-medium text-mute">共享 K 线周期
                <select value={selectedFrequency} onChange={(event) => chooseFrequency(event.target.value)} className={INPUT} disabled={!availableFrequencies.length}>
                  {!selectedFrequency && <option value="">请选择周期</option>}
                  {availableFrequencies.map((frequency) => <option key={frequency} value={frequency}>{frequencyLabel(frequency)}</option>)}
                </select>
                <span className="mt-1.5 block text-[10px] font-normal leading-relaxed text-faint">日 K、1m、5m 等周期均来自当前标的的已冻结行情；所有策略共用所选周期。</span>
              </label> : <label className="text-[12px] font-medium text-mute">共享预测窗口
                <select value={form.holdingBars} onChange={(event) => update({ holdingBars: event.target.value })} className={INPUT}>
                  {eventHorizons.map((value) => <option key={value} value={value}>T+{value} · {value} 根日 K</option>)}
                </select>
                <span className="mt-1.5 block text-[10px] font-normal leading-relaxed text-faint">所有事件模型使用同一标签期限和 Oracle 预测窗口。</span>
              </label>}
              {form.track === "quant" && <label className="text-[12px] font-medium text-mute">统一持有周期（K 线根数）
                <input type="number" min={1} max={1000} step={1} value={form.holdingBars} onChange={(event) => update({ holdingBars: event.target.value })} className={INPUT} />
              </label>}
              <label className="text-[12px] font-medium text-mute">开始日期
                <input type="date" value={form.startDate} onChange={(event) => update({ startDate: event.target.value })} className={INPUT} />
              </label>
              <label className="text-[12px] font-medium text-mute">结束日期
                <input type="date" value={form.endDate} onChange={(event) => update({ endDate: event.target.value })} className={INPUT} />
              </label>
            </div>
            {target?.start_date && target.end_date && <p className="mt-2 flex items-center gap-1.5 text-[10px] leading-relaxed text-mute"><CalendarRange size={11} />当前数据范围：{day(target.start_date)} → {day(target.end_date)}</p>}
            {form.startDate && form.endDate && !dateSelectionValid && <p className="mt-2 text-[12px] text-rise">结束日期不能早于开始日期。</p>}

            <div className="mt-5 border-t border-edge pt-4">
              <h4 className="text-[12px] font-semibold text-ink">共享资金与交易成本</h4>
              <p className="mt-1 text-[11px] leading-relaxed text-mute">{form.track === "event" ? "看涨的方向收益等于实际收益，看跌按反向收益计算，中性为空仓；手续费和滑点统一扣除。" : "策略使用相同行情快照、基准、成交口径和成本，周期不会因策略配置而变化。"}</p>
              <div className="mt-3 grid gap-3 sm:grid-cols-3">
                <label className="text-[12px] text-mute">初始资金<input type="number" min={1} max={1e12} step="any" value={form.initialCapital} onChange={(event) => update({ initialCapital: event.target.value })} className={INPUT} /></label>
                <label className="text-[12px] text-mute">单边手续费（bps）<input type="number" min={0} max={1000} step="any" value={form.feeBps} onChange={(event) => update({ feeBps: event.target.value })} className={INPUT} /></label>
                <label className="text-[12px] text-mute">单边滑点（bps）<input type="number" min={0} max={1000} step="any" value={form.slippageBps} onChange={(event) => update({ slippageBps: event.target.value })} className={INPUT} /></label>
              </div>
              <p className="mt-2 text-[10px] text-faint">1 bps = 0.01%。所有参赛者使用同一套资金和成本。</p>
              {!rulesValid && <p className="mt-2 text-[12px] text-rise">周期须为有效整数，手续费和滑点为 0–1000，资金须大于 0 且不超过 1 万亿。</p>}
            </div>

            <label className="mt-4 block text-[12px] text-mute">Arena 名称（可选）
              <input value={form.name} maxLength={120} onChange={(event) => update({ name: event.target.value })} placeholder="自动按赛道、目标与日期命名" className={INPUT} />
            </label>

            <div className="mt-5 grid gap-2 rounded-xl border border-edge bg-paper/70 p-3 text-[11px] sm:grid-cols-2">
              <p><span className="text-faint">赛道：</span><span className="font-medium text-ink">{form.track === "quant" ? "Quant Arena" : "Event Arena"}</span></p>
              <p><span className="text-faint">参赛者：</span><span className="font-medium text-ink">{selectedModels.length} 个同类配置</span></p>
              <p><span className="text-faint">共享目标：</span><span className="font-medium text-ink">{target?.name || "—"}</span></p>
              <p><span className="text-faint">共享周期：</span><span className="font-medium text-ink">{form.track === "quant" ? frequencyLabel(selectedFrequency) : `T+${form.holdingBars} 日 K`}</span></p>
            </div>
          </section>}
        </fieldset>}

        {error && <div role="alert" className="mt-4 flex items-start gap-2 rounded-xl border border-rise/20 bg-rise/5 p-4 text-[12px] leading-relaxed text-rise">
          <AlertCircle size={15} className="mt-0.5 shrink-0" /><p className="break-words">{error}</p>
        </div>}
      </main>

      <footer className="flex shrink-0 items-center justify-between gap-3 border-t border-edge bg-card px-4 py-3 sm:px-7 sm:py-4">
        <div className="hidden min-w-0 sm:block">
          <p id="arena-step-reason" aria-live="polite" className={cls("truncate text-[12px]", stepComplete[activeStep] ? "text-jade" : "text-amber-700")}>{saving ? "正在创建比较任务…" : currentStepReason}</p>
          <p className="mt-1 truncate text-[10px] text-mute">选择会在步骤间保留；开始后冻结本场输入与规则。</p>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-2">
          <button type="button" onClick={onClose} disabled={saving} className={BUTTON}>取消</button>
          {activeStep > 1 && <button type="button" onClick={() => setActiveStep((activeStep - 1) as WizardStep)} disabled={saving} className={BUTTON}><ArrowLeft size={14} />上一步</button>}
          {activeStep < 4 ? <button
            type="button"
            onClick={() => goToStep((activeStep + 1) as WizardStep)}
            disabled={!stepComplete[activeStep] || saving || loading || Boolean(catalogError)}
            aria-describedby="arena-step-reason"
            className="inline-flex items-center justify-center gap-2 rounded-lg bg-ink px-4 py-2.5 text-[12px] font-semibold text-card disabled:cursor-not-allowed disabled:opacity-40"
          >下一步<ArrowRight size={14} /></button> : <button
            type="button"
            onClick={() => void start()}
            disabled={!canStart}
            aria-describedby="arena-step-reason"
            className="inline-flex items-center justify-center gap-2 rounded-lg bg-ink px-5 py-2.5 text-[13px] font-semibold text-card disabled:cursor-not-allowed disabled:opacity-40"
          >{saving ? <Loader2 size={15} className="animate-spin" /> : <ArrowRight size={15} />}{saving ? "正在启动…" : "开始比较"}</button>}
        </div>
      </footer>
    </div>
  </div>;
}
