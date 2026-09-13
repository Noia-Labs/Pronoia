import { useEffect, useRef, useState } from "react";
import { AlertCircle, FileUp, Loader2, Save, X } from "lucide-react";
import { api } from "../../api";
import type { BTStrategySpec, ModelLabProfile } from "../../types";
import { cls } from "../../utils";
import { createDefaultDraft, QuantEditor, quantConditionError, serializeQuantCondition } from "../BacktestList";
import EventModelConnectionEditor from "./EventModelConnectionEditor";
import ExternalEventServiceEditor, { defaultExternalService, serviceConfigError, saveServiceCredentials } from "./ExternalEventServiceEditor";
import { modelLibraryApi, type ModelCategory, type SavedModelEntry, type SaveModelInput } from "./modelLibraryApi";

const INPUT = "mt-1.5 w-full min-w-0 rounded-lg border border-edgeDark/70 bg-card px-3 py-2.5 text-[12px] text-ink outline-none focus:border-brand disabled:opacity-50";
type EditorKind = "pronoia" | "external_model" | "external_service" | "imported_predictions" | "return_forecast" | "declarative_rules" | "quant_api" | "json_spec";

export default function ModelLibraryEditor({ category, onClose, onSaved }: { category: ModelCategory; onClose: () => void; onSaved: (model: SavedModelEntry) => void }) {
  const [name, setName] = useState("");
  const [kind, setKind] = useState<EditorKind>(category === "event" ? "external_model" : "return_forecast");
  const [profiles, setProfiles] = useState<ModelLabProfile[]>([]);
  const [profileId, setProfileId] = useState("");
  const [profilesLoading, setProfilesLoading] = useState(false);
  const [profilesError, setProfilesError] = useState<string | null>(null);
  const [allowPrivate, setAllowPrivate] = useState(false);
  const [draft, setDraft] = useState(() => createDefaultDraft("quant", []));
  const [service, setService] = useState(defaultExternalService);
  const [importSpec, setImportSpec] = useState<BTStrategySpec | null>(null);
  const [importName, setImportName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [matchState, setMatchState] = useState<{ signature: string; model: SavedModelEntry | null; failed: boolean }>({ signature: "", model: null, failed: false });
  const autoName = useRef("");
  const [saving, setSaving] = useState(false);
  const savingRef = useRef(false);
  const modelIdentity = useRef(crypto.randomUUID().replace(/-/g, ""));
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  const credentialCache = useRef<{ signature: string; headers: Record<string, string> } | null>(null);

  useEffect(() => {
    let alive = true; setProfilesLoading(true);
    void api.modelLabProfiles().then((response) => { if (alive) setProfiles(response.items ?? []); }).catch((reason) => { if (alive) setProfilesError(reason instanceof Error ? reason.message : "无法读取模型连接"); }).finally(() => { if (alive) setProfilesLoading(false); });
    void api.modelLabEventCapabilities().then((response) => { if (alive) setAllowPrivate(response.allow_private_model_endpoints === true); }).catch(() => {});
    const previousFocus = document.activeElement;
    const keydown = (event: KeyboardEvent) => { if (event.key === "Escape" && !savingRef.current) { event.preventDefault(); closeRef.current(); } };
    document.addEventListener("keydown", keydown); dialogRef.current?.focus();
    return () => { alive = false; document.removeEventListener("keydown", keydown); if (previousFocus instanceof HTMLElement) previousFocus.focus(); };
  }, []);

  const options: Array<[EditorKind, string, string]> = category === "event" ? [
    ["external_model", "独立大模型", "连接 Qwen、DeepSeek 等 API"],
    ["pronoia", "Pronoia · 多 Agent", "保存使用指定基模的 Pronoia"],
    ["external_service", "第三方预测服务", "接入可直接返回预测的金融模型"],
    ["imported_predictions", "导入预测", "测试时上传 CSV / JSON 预测文件"],
  ] : [
    ["return_forecast", "收益率预测模型", "保存预测方法和参数"],
    ["declarative_rules", "量化交易规则", "配置完整开仓与平仓条件"],
    ["quant_api", "外部量化 API", "接收外部模型的目标仓位"],
    ["json_spec", "导入模型配置", "从本地 JSON 导入完整规则"],
  ];
  const profile = profiles.find((item) => item.id === profileId);
  const inputWithoutCredentials = (): SaveModelInput | null => {
    let payload: SaveModelInput = { name: name.trim(), category };
    if (kind === "pronoia" || kind === "external_model") payload = { ...payload, kind, runner: kind === "pronoia" ? "team_full" : "raw_model", profile_id: profileId };
    else if (kind === "imported_predictions") payload = { ...payload, kind, model_identity: modelIdentity.current, runner: "provided_analysis", strategy_spec: { type: "event", adapter: "imported_decisions", runner: "provided_analysis" } };
    else if (kind === "return_forecast") payload = { ...payload, kind: "quant", runner: "return_forecast", strategy_spec: { type: "quant", adapter: "builtin", kind: "return_forecast", source: "builtin", model_id: "historical_mean", name: name.trim(), version: "1", input_contract: "market_bars", parameters: { method: draft.quant_forecast.method, lookback: draft.quant_forecast.lookback } } };
    else if (kind === "declarative_rules") payload = { ...payload, kind: "quant", runner: "declarative_rules", strategy_spec: { type: "quant", adapter: "builtin", kind: "declarative_rules", model_id: "declarative_rules", version: draft.strategy_version.trim() || null, input_contract: "market_bars", parameters: { signal_timing: "bar_close", execution_timing: "next_open", entry: { combinator: draft.quant_entry_combinator, conditions: draft.quant_conditions.filter((item) => item.phase === "entry").map(serializeQuantCondition) }, exit: { combinator: draft.quant_exit_combinator, conditions: draft.quant_conditions.filter((item) => item.phase === "exit").map(serializeQuantCondition) } } } };
    else if (kind === "json_spec") payload = { ...payload, kind: "quant", runner: String(importSpec?.runner || importSpec?.kind || "external_http"), strategy_spec: importSpec! };

    else return null;
    return payload;
  };

  let invalid = !name.trim() ? "请填写模型名称。" : name.trim().length > 120 ? "模型名称不能超过 120 字。" : "";
  if (!invalid && ["external_model", "pronoia"].includes(kind) && !(kind === "pronoia" && profileId === "__platform_default__") && !profile) invalid = "请选择已保存的模型连接，或在下方添加 API。";
  if (!invalid && ["external_service", "quant_api"].includes(kind)) invalid = serviceConfigError(service) ?? "";
  if (!invalid && kind === "return_forecast" && (!Number.isInteger(draft.quant_forecast.lookback) || draft.quant_forecast.lookback < 1 || draft.quant_forecast.lookback > 10000)) invalid = "历史样本数应为 1–10000 的整数。";
  if (!invalid && kind === "declarative_rules") invalid = !draft.quant_conditions.some((item) => item.phase === "entry") || !draft.quant_conditions.some((item) => item.phase === "exit") ? "请至少配置一条开仓条件和一条平仓条件。" : draft.quant_conditions.map(quantConditionError).find(Boolean) ?? "";
  if (!invalid && kind === "json_spec" && !importSpec) invalid = "请先选择有效的 JSON 模型配置文件。";

  // Identity is resolved on the server using the same rules as the final save.
  const matchInput = inputWithoutCredentials();
  if (matchInput?.strategy_spec?.name !== undefined) matchInput.strategy_spec = { ...matchInput.strategy_spec, name: "模型" };
  const matchSignature = kind !== "imported_predictions" && matchInput && (kind !== "json_spec" || importSpec) && (!["pronoia", "external_model"].includes(kind) || profileId) ? JSON.stringify({ ...matchInput, name: "模型" }) : "";
  const matching = Boolean(matchSignature) && matchState.signature !== matchSignature;
  const existingModel = matchState.signature === matchSignature ? matchState.model : null;
  useEffect(() => {
    if (!matchSignature) return;
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      void modelLibraryApi.match(JSON.parse(matchSignature) as SaveModelInput, controller.signal).then(({ model }) => {
        if (controller.signal.aborted) return;
        setMatchState({ signature: matchSignature, model, failed: false });
        if (model) setName((current) => {
          if (current.trim() && current !== autoName.current) return current;
          autoName.current = model.name;
          return model.name;
        });
      }).catch(() => { if (!controller.signal.aborted) setMatchState({ signature: matchSignature, model: null, failed: true }); });
    }, 200);
    return () => { window.clearTimeout(timer); controller.abort(); };
  }, [matchSignature]);

  const readFile = async (file?: File) => {
    if (!file) return; setError(null); setImportSpec(null); setImportName("");
    try {
      if (file.size > 1024 * 1024) throw new Error("模型配置文件不能超过 1 MB。");
      const content: unknown = JSON.parse((await file.text()).replace(/^\uFEFF/, ""));
      const record = content && typeof content === "object" && !Array.isArray(content) ? content as Record<string, unknown> : null;
      const value = record?.strategy_spec ?? record;
      if (!value || typeof value !== "object" || Array.isArray(value) || typeof (value as Record<string, unknown>).type !== "string") throw new Error("文件须包含完整 strategy_spec 对象，至少声明 type、模型 kind 或 adapter 和 parameters。");
      const spec = value as BTStrategySpec;
      if (!["quant", "api", "signal_import"].includes(spec.type)) throw new Error("此处只导入量化模型配置。事件模型请在事件模型页添加。");
      if (!spec.kind && !spec.adapter) throw new Error("模型配置缺少 kind 或 adapter。");
      setImportSpec(spec); setImportName(file.name);
      if (!name.trim()) setName(typeof record?.name === "string" ? record.name.slice(0, 120) : file.name.replace(/\.json$/i, "").slice(0, 120));
    } catch (reason) { setError(reason instanceof Error ? reason.message : "无法读取模型配置文件。"); }
  };

  const save = async () => {
    if (invalid || matching || savingRef.current) return;
    savingRef.current = true; setSaving(true); setError(null);
    try {
      let payload = inputWithoutCredentials();
      if (!payload) {
        const signature = JSON.stringify([service.endpoint, service.auth_mode, service.api_key, service.header_name, service.secret_env_ref]);
        const headers = credentialCache.current?.signature === signature ? credentialCache.current.headers : await saveServiceCredentials(service);
        credentialCache.current = { signature, headers };
        payload = { name: name.trim(), category, kind: kind === "quant_api" ? "quant" : "external_service", runner: "external_http", strategy_spec: { type: kind === "quant_api" ? "api" : "event", adapter: "external_http", runner: "external_http", endpoint: service.endpoint.trim(), headers, timeout_seconds: service.timeout_seconds, version: service.version.trim() || null, ...(kind === "quant_api" ? { output_contract: "target_weight", input_contract: "external_decision_api" } : { input_contract: "events_only" }) } };
      }
      const saved = await modelLibraryApi.create(payload);
      setService((current) => ({ ...current, api_key: "" }));
      onSaved(saved);
    } catch (reason) { setError(reason instanceof Error ? (service.api_key ? reason.message.split(service.api_key).join("[密钥已隐藏]") : reason.message) : "保存模型失败，请重试。"); }
    finally { savingRef.current = false; setSaving(false); }
  };

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-3 backdrop-blur-sm sm:p-5"><div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="model-library-editor-title" tabIndex={-1} className="flex max-h-[94dvh] w-full max-w-[1050px] flex-col overflow-hidden rounded-2xl border border-edge bg-paper shadow-pop outline-none">
    <header className="flex items-start justify-between gap-3 border-b border-edge bg-card px-5 py-4"><div><h2 id="model-library-editor-title" className="font-serif text-[22px] font-semibold text-ink">添加{category === "event" ? "事件" : "量化"}模型</h2><p className="mt-1.5 text-[12px] text-mute">先保存模型配置；选择数据、标的和时间段将在发起测试时完成。</p></div><button type="button" onClick={onClose} disabled={saving} aria-label="关闭添加模型" className="rounded-lg p-2 text-mute disabled:opacity-40"><X size={18} /></button></header>
    <main className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5"><fieldset disabled={saving} className="space-y-4"><div className="grid gap-2 sm:grid-cols-2">{options.map(([value, label, description]) => <button key={value} type="button" onClick={() => { setKind(value); setError(null); if (value === "pronoia" && !profileId) setProfileId("__platform_default__"); if (value === "external_model" && profileId === "__platform_default__") setProfileId(""); }} aria-pressed={kind === value} className={cls("rounded-xl border p-3 text-left", kind === value ? "border-brand/35 bg-brand-soft/35" : "border-edge bg-card")}><span className="block text-[13px] font-semibold text-ink">{label}</span><span className="mt-1 block text-[11px] text-mute">{description}</span></button>)}</div>
      <section aria-live="polite" className={cls("rounded-xl border px-4 py-3 text-[12px] leading-relaxed", existingModel ? "border-jade/25 bg-jade-soft/25 text-jade" : "border-edge bg-card text-mute")}>
        {existingModel ? <><p className="font-semibold">此配置已存在：{existingModel.name}</p><p className="mt-1">当前仅修改这个模型的名称，不会新增模型卡片。模型配置和历史测试保持不变。</p></> : <><p>相同配置只保留一个模型；再次保存时仅更新原模型名称，不新增卡片，也不修改历史测试。</p>{matching ? <p className="mt-1">正在核对已有配置…</p> : matchState.failed && matchState.signature === matchSignature ? <p className="mt-1">暂时无法预先核对，保存时会再次检查相同配置。</p> : null}</>}
      </section>
      <label className="block rounded-xl border border-edge bg-card p-4 text-[12px] font-medium text-ink">{existingModel ? "新的模型名称" : "模型名称"}<input value={name} onChange={(event) => setName(event.target.value)} maxLength={120} className={INPUT} placeholder={category === "event" ? "例如：Qwen 金融预测" : "例如：均线趋势模型"} /><span className="mt-1.5 block text-[11px] font-normal text-mute">同一模型可测试不同事件、指数和时间段，历史记录统一归在此模型下。</span></label>
      {(kind === "pronoia" || kind === "external_model") && <section className="rounded-xl border border-edge bg-card p-4"><label className="text-[12px] font-medium text-ink">{kind === "pronoia" ? "Pronoia 使用的基模连接" : "独立模型 API 连接"}<select value={profileId} onChange={(event) => setProfileId(event.target.value)} disabled={profilesLoading} className={INPUT}><option value="">{profilesLoading ? "正在读取连接…" : "请选择模型连接"}</option>{kind === "pronoia" && <option value="__platform_default__">平台统一基模</option>}{profiles.map((item) => <option key={item.id} value={item.id}>{item.name} · {item.model_id}{!item.is_active ? " · 已停用" : !item.secret_configured ? " · 密钥未配置" : ""}</option>)}</select></label>{profilesError && <p className="mt-2 text-[12px] text-rise">{profilesError}</p>}<div className="mt-4"><EventModelConnectionEditor disabled={saving} onCreated={(item) => { setProfiles((current) => [item, ...current.filter((entry) => entry.id !== item.id)]); setProfileId(item.id); if (!name.trim()) setName(kind === "pronoia" ? `Pronoia（${item.name}）` : item.name); setProfilesError(null); }} /></div><p className="mt-3 text-[11px] leading-relaxed text-mute">{kind === "pronoia" ? "保存这一基模下的 Pronoia，沿用相同多 Agent 流程。此处不会更换其他页面的平台默认基模。" : "预测直接由所选 API 模型生成，不经过 Pronoia 多 Agent 流程。"}</p></section>}
      {kind === "external_service" && <ExternalEventServiceEditor value={service} onChange={setService} horizon="t3" disabled={saving} allowPrivateEndpoints={allowPrivate} />}
      {kind === "imported_predictions" && <section className="rounded-xl border border-edge bg-card p-4 text-[12px] leading-relaxed text-mute"><h3 className="font-semibold text-ink">保存“导入预测”模型</h3><p className="mt-2">发起测试时选择事件集，再上传包含 direction、confidence、rationale 和可选 expected_return_pct 的 CSV / JSON 预测文件。平台校验对应事件与预测窗口后计算结果。</p><p className="mt-2">文件中的预测只适用于对应对象，不会自动生成其他事件的预测；完成测试后可在 Arena 比较其已有结果。</p></section>}
      {kind === "return_forecast" && <section className="rounded-xl border border-edge bg-card p-4"><div className="grid gap-3 sm:grid-cols-2"><label className="text-[12px] text-mute">预测方法<select value={draft.quant_forecast.method} className={INPUT} onChange={() => undefined}><option value="historical_mean">历史收益均值</option></select></label><label className="text-[12px] text-mute">历史样本窗口<input type="number" min={1} max={10000} step={1} value={draft.quant_forecast.lookback} onChange={(event) => setDraft((current) => ({ ...current, quant_forecast: { ...current.quant_forecast, lookback: Number(event.target.value) } }))} className={INPUT} /><span className="mt-1.5 block text-[11px]">使用最近多少个已实现的历史收益样本。</span></label></div><p className="mt-3 text-[12px] leading-relaxed text-mute">此处保存预测方法与历史窗口。预测未来几个周期，在发起测试时选择。</p></section>}
      {kind === "declarative_rules" && <QuantEditor form={draft} setForm={setDraft} />}
      {kind === "quant_api" && <section className="grid gap-3 rounded-xl border border-edge bg-card p-4 sm:grid-cols-2"><p className="text-[12px] leading-relaxed text-mute sm:col-span-2">平台提交截至当前时点的历史行情；服务返回当前目标仓位 target_weight，再按统一成交规则模拟。这里保存接口，不调用模型。</p><label className="text-[12px] text-mute sm:col-span-2">预测接口地址<input className={INPUT} value={service.endpoint} onChange={(event) => setService((current) => ({ ...current, endpoint: event.target.value }))} placeholder="https://your-model.example/predict" /></label><label className="text-[12px] text-mute">认证方式<select value={service.auth_mode} onChange={(event) => setService((current) => ({ ...current, auth_mode: event.target.value as typeof current.auth_mode }))} className={INPUT}><option value="bearer">API Key · Bearer</option><option value="header">自定义请求头</option><option value="none">无需认证</option></select></label>{service.auth_mode !== "none" && <label className="text-[12px] text-mute">API Key<input type="password" autoComplete="off" spellCheck={false} value={service.api_key} onChange={(event) => setService((current) => ({ ...current, api_key: event.target.value }))} className={INPUT} placeholder="粘贴服务方提供的密钥" /></label>}{service.auth_mode === "header" && <label className="text-[12px] text-mute">请求头名称<input value={service.header_name} onChange={(event) => setService((current) => ({ ...current, header_name: event.target.value }))} className={INPUT} /></label>}<label className="text-[12px] text-mute">单次等待时间（秒）<input type="number" min={1} max={300} value={service.timeout_seconds} onChange={(event) => setService((current) => ({ ...current, timeout_seconds: Number(event.target.value) }))} className={INPUT} /></label></section>}
      {kind === "json_spec" && <section className="rounded-xl border border-edge bg-card p-4"><label className="flex cursor-pointer flex-col items-center rounded-xl border border-dashed border-edgeDark bg-paper px-4 py-8 text-center"><FileUp size={24} className="text-mute" /><span className="mt-3 text-[13px] font-semibold text-ink">选择本地 JSON 模型配置</span><span className="mt-1 text-[11px] text-mute">完整 strategy_spec，保留原参数与规则；最大 1 MB</span><input type="file" accept=".json,application/json" aria-label="导入量化模型 JSON" className="mt-4 max-w-full text-[12px]" onChange={(event) => void readFile(event.target.files?.[0])} /></label>{importSpec && <p className="mt-3 break-words text-[12px] text-jade">已读取 {importName} · {importSpec.kind || importSpec.adapter}。保存时由服务端校验完整配置。</p>}</section>}
    </fieldset>{error && <div role="alert" className="mt-4 flex items-start gap-2 rounded-xl border border-rise/20 bg-rise/5 p-3 text-[12px] leading-relaxed text-rise"><AlertCircle size={14} className="mt-0.5 shrink-0" /><p className="break-words">{error}</p></div>}</main>
    <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-edge bg-card px-5 py-4"><div><p className={cls("text-[12px]", invalid ? "text-amber-700" : "text-jade")}>{invalid || (matching ? "正在核对已有配置…" : existingModel ? "保存后更新原卡片名称，并定位到该模型。" : "配置已就绪，保存后可随时发起测试。") }</p><p className="mt-1 text-[11px] text-mute">保存模型不会启动测试，也不会调用模型 API。</p></div><div className="flex gap-2"><button type="button" onClick={onClose} disabled={saving} className="rounded-lg border border-edge px-3 py-2 text-[12px] text-mute">取消</button><button type="button" onClick={() => void save()} disabled={saving || matching || Boolean(invalid)} className="inline-flex items-center gap-2 rounded-lg bg-ink px-4 py-2.5 text-[12px] font-semibold text-card disabled:opacity-40">{saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}{saving ? "正在保存…" : existingModel ? "保存名称" : "保存模型"}</button></div></footer>
  </div></div>;
}
