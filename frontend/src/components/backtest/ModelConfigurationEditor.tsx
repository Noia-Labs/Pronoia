import { useEffect, useRef, useState } from "react";
import { AlertCircle, ArrowUpRight, Loader2, Save, Settings2, X } from "lucide-react";
import { api } from "../../api";
import type { BTStrategySpec, ModelLabProfile, ModelLabProfileInput } from "../../types";
import type { ModelProviderFieldsValue } from "../../modelProviders";
import { DEFAULT_MAX_OUTPUT_TOKENS, DEFAULT_MODEL_TIMEOUT_SECONDS, MAX_MAX_OUTPUT_TOKENS, MIN_MAX_OUTPUT_TOKENS, OUTPUT_TOKEN_LIMIT_HINT, parseOutputTokenLimit } from "../../modelDefaults";
import { cls } from "../../utils";
import ModelProviderFields from "../ModelProviderFields";
import { createDefaultDraft, QuantEditor, quantConditionError, serializeQuantCondition } from "../BacktestList";
import { defaultExternalService, saveServiceCredentials, serviceConfigError, type ExternalServiceConfig } from "./ExternalEventServiceEditor";
import { modelLibraryApi, modelKindLabel, type SavedModelEntry } from "./modelLibraryApi";
import { hydrateQuantConfiguration, mergeQuantConfiguration, objectRecord, parseStrategyConfiguration } from "./modelConfiguration";

const INPUT = "mt-1.5 w-full min-w-0 rounded-lg border border-edgeDark/70 bg-card px-3 py-2.5 text-[12px] text-ink outline-none focus:border-brand disabled:opacity-50";
const SECTION = "rounded-xl border border-edge bg-card p-4";
const apiOrigin = (address: string) => { try { return new URL(address.trim()).origin; } catch { return ""; } };
const isPlatformModel = (model: SavedModelEntry) => model.id === "pronoia:platform" || (model.kind === "pronoia" && model.setup.profile_id === "__platform_default__");
const hasProfile = (model: SavedModelEntry) => !isPlatformModel(model) && ["pronoia", "external_model", "raw_model"].includes(model.kind);
type Props = { model: SavedModelEntry; onClose: () => void; onSaved: (model: SavedModelEntry) => void };

export default function ModelConfigurationEditor({ model, onClose, onSaved }: Props) {
  const [latest, setLatest] = useState(model);
  const [name, setName] = useState(model.name);
  const [loading, setLoading] = useState(true);
  const [loadVersion, setLoadVersion] = useState(0);
  const [loadError, setLoadError] = useState("");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [profile, setProfile] = useState<ModelLabProfile | null>(null);
  const [platformProfile, setPlatformProfile] = useState(false);
  const [connection, setConnection] = useState<ModelProviderFieldsValue>({ provider: "custom", base_url: "", model_id: "", secret_env_ref: "" });
  const [apiKey, setApiKey] = useState("");
  const [tokens, setTokens] = useState(String(DEFAULT_MAX_OUTPUT_TOKENS));
  const [timeout, setTimeout] = useState(String(DEFAULT_MODEL_TIMEOUT_SECONDS));
  const [thinking, setThinking] = useState<ModelLabProfile["thinking_mode"]>("auto");
  const [spec, setSpec] = useState<BTStrategySpec | null>(null);
  const [draft, setDraft] = useState(() => createDefaultDraft("quant", []));
  const [draftDirty, setDraftDirty] = useState(false);
  const [rulesEditable, setRulesEditable] = useState(true);
  const [advanced, setAdvanced] = useState(false);
  const [json, setJson] = useState("");
  const [service, setService] = useState(defaultExternalService);
  const [authMode, setAuthMode] = useState<"keep" | ExternalServiceConfig["auth_mode"]>("keep");
  const credentialCache = useRef<{ signature: string; headers: Record<string, string> } | null>(null);
  const lock = useRef(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;

  useEffect(() => {
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    const keydown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !lock.current) { event.preventDefault(); event.stopPropagation(); closeRef.current(); }
      if (event.key !== "Tab") return;
      const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]') ?? []).filter((element) => element.getClientRects().length > 0 && !element.closest("fieldset:disabled"));
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (!first) { event.preventDefault(); dialogRef.current?.focus(); }
      else if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && (document.activeElement === last || document.activeElement === dialogRef.current)) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", keydown);
    return () => { document.removeEventListener("keydown", keydown); document.body.style.overflow = previousOverflow; if (previousFocus instanceof HTMLElement && previousFocus.isConnected) previousFocus.focus(); };
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true); setLoadError(""); setError("");
    void (async () => {
      try {
        const fresh = await modelLibraryApi.configuration(model.id);
        const profiles = hasProfile(fresh) ? await api.modelLabProfiles() : null;
        if (!active) return;
        setLatest(fresh); setName(fresh.name);
        const currentProfile = profiles?.items.find((item) => item.id === fresh.setup.profile_id) ?? null;
        if (hasProfile(fresh) && !currentProfile) throw new Error("该模型的 API 连接已不存在，请先在基模连接中恢复它。");
        setProfile(currentProfile);
        setPlatformProfile(Boolean(currentProfile && profiles?.default_profile_id === currentProfile.id));
        if (currentProfile) {
          setConnection({ provider: currentProfile.provider, base_url: currentProfile.base_url, model_id: currentProfile.model_id, secret_env_ref: "" });
          setTokens(String(currentProfile.max_output_tokens)); setTimeout(String(currentProfile.timeout_seconds)); setThinking(currentProfile.thinking_mode);
        }
        const currentSpec = fresh.setup.strategy_spec ?? null;
        setSpec(currentSpec); setDraftDirty(false); setAdvanced(false); setJson(JSON.stringify(currentSpec, null, 2));
        const restored = currentSpec?.kind === "declarative_rules" ? hydrateQuantConfiguration(currentSpec, createDefaultDraft("quant", [])) : null;
        setRulesEditable(currentSpec?.kind !== "declarative_rules" || restored !== null);
        if (restored) setDraft(restored);
        else if (currentSpec?.kind === "declarative_rules") setAdvanced(true);
        setService({ ...defaultExternalService(), endpoint: String(currentSpec?.endpoint ?? ""), version: String(currentSpec?.version ?? ""), timeout_seconds: Number(currentSpec?.timeout_seconds ?? 30) });
        setAuthMode("keep"); setApiKey(""); credentialCache.current = null;
      } catch (reason) { if (active) setLoadError(reason instanceof Error ? reason.message : "配置读取失败，请重试。"); }
      finally { if (active) setLoading(false); }
    })();
    return () => { active = false; };
  }, [model.id, loadVersion]);

  const platform = isPlatformModel(latest);
  const profileModel = hasProfile(latest);
  const serviceModel = Boolean(spec && (spec.endpoint || spec.adapter === "external_http" || spec.source === "external_http" || spec.kind === "external_http"));
  const currentSpec = () => spec && draftDirty && spec.kind === "declarative_rules" ? mergeQuantConfiguration(spec, draft, serializeQuantCondition) : spec;
  const specFromForm = () => {
    const value = currentSpec();
    return value && serviceModel ? { ...value, endpoint: service.endpoint.trim(), version: service.version.trim() || null, timeout_seconds: service.timeout_seconds } : value;
  };
  const parameters = objectRecord(spec?.parameters);
  const setParameter = (key: string, value: number) => setSpec((current) => current ? { ...current, parameters: { ...objectRecord(current.parameters), [key]: value } } : null);
  const tokenLimit = parseOutputTokenLimit(tokens);
  const needsNewApiKey = Boolean(profile?.secret_configured && (connection.provider.trim().toLowerCase() !== profile.provider.trim().toLowerCase() || apiOrigin(connection.base_url) !== apiOrigin(profile.base_url)));
  let invalid = !name.trim() ? "请填写模型名称。" : name.trim().length > 120 ? "模型名称不能超过 120 字。" : "";
  if (!invalid && profileModel) {
    if (!connection.base_url.trim() || !connection.model_id.trim()) invalid = "请填写 API 地址和模型 ID。";
    else if (!apiOrigin(connection.base_url) || !/^https?:\/\//i.test(connection.base_url.trim())) invalid = "请填写完整的 HTTP 或 HTTPS API 地址。";
    else if (tokenLimit === null) invalid = `输出上限应为 ${MIN_MAX_OUTPUT_TOKENS}–${MAX_MAX_OUTPUT_TOKENS} 的整数。`;
    else if (!Number.isFinite(Number(timeout)) || Number(timeout) <= 0 || Number(timeout) > 900) invalid = "最长等待时间应大于 0 且不超过 900 秒。";
    else if (!profile?.secret_configured && !apiKey.trim()) invalid = "请填写 API Key。";
    else if (needsNewApiKey && !apiKey.trim()) invalid = "更换 API 服务商或服务地址的域名、端口、协议后，请填写匹配的新 API Key。";
  }
  if (!invalid && !profileModel && !platform && spec) {
    if (advanced) { try { parseStrategyConfiguration(json); } catch (reason) { invalid = reason instanceof Error ? `JSON 配置无效：${reason.message}` : "请填写有效的 JSON 配置。"; } }
    else if (serviceModel) {
      if (authMode !== "keep") invalid = serviceConfigError({ ...service, auth_mode: authMode }) ?? "";
      else if (!service.endpoint.trim()) invalid = "请填写预测服务地址。";
      else if (!Number.isInteger(service.timeout_seconds) || service.timeout_seconds < 1 || service.timeout_seconds > 300) invalid = "最长等待时间应为 1–300 秒的整数。";
    } else if (spec.kind === "declarative_rules") invalid = !draft.quant_conditions.some((item) => item.phase === "entry") || !draft.quant_conditions.some((item) => item.phase === "exit") ? "请保留至少一条开仓条件和一条平仓条件。" : draft.quant_conditions.map(quantConditionError).find(Boolean) ?? "";
    else if (["return_forecast", "momentum"].includes(String(spec.kind))) {
      const lookback = Number(parameters.lookback ?? 20);
      if (!Number.isInteger(lookback) || lookback < 1 || lookback > 10000) invalid = "历史窗口应为 1–10000 的整数。";
      else if (spec.kind === "momentum" && !Number.isFinite(Number(parameters.negative_weight ?? -1))) invalid = "请填写有效的目标仓位数值。";
    } else if (spec.kind === "ma_cross") {
      const short = Number(parameters.short_window ?? 5), long = Number(parameters.long_window ?? 20);
      if (!Number.isInteger(short) || !Number.isInteger(long) || short < 1 || short >= long || long > 10000) invalid = "均线周期应满足 1 ≤ 短期 < 长期 ≤ 10000。";
      else if (!Number.isFinite(Number(parameters.short_weight ?? -1))) invalid = "请填写有效的目标仓位数值。";
    }
  }
  if (!invalid && !advanced && spec && ["ma_cross", "momentum"].includes(String(spec.kind))) {
    const weightKey = spec.kind === "ma_cross" ? "short_weight" : "negative_weight";
    if (!Number.isFinite(Number(parameters[weightKey] ?? -1))) invalid = "请填写有效的目标仓位。";
  }
  if (!invalid && advanced && serviceModel && authMode !== "keep") {
    const parsed = parseStrategyConfiguration(json);
    invalid = serviceConfigError({ ...service, endpoint: String(parsed.endpoint ?? ""), timeout_seconds: Number(parsed.timeout_seconds ?? 30), auth_mode: authMode }) ?? "";
  }

  const switchAdvanced = () => {
    setError("");
    if (!advanced) { setJson(JSON.stringify(specFromForm(), null, 2)); setAdvanced(true); return; }
    try {
      const next = parseStrategyConfiguration(json);
      const restored = next.kind === "declarative_rules" ? hydrateQuantConfiguration(next, createDefaultDraft("quant", [])) : null;
      if (next.kind === "declarative_rules" && !restored) throw new Error("这份规则包含表单尚未支持的条件，请继续在完整配置中修改。");
      setSpec(next); setDraftDirty(false); setRulesEditable(true); if (restored) setDraft(restored);
      setService((current) => ({ ...current, endpoint: String(next.endpoint ?? ""), version: String(next.version ?? ""), timeout_seconds: Number(next.timeout_seconds ?? 30) }));
      setAdvanced(false);
    } catch (reason) { setError(reason instanceof Error ? reason.message : "配置无法转换为表单。"); }
  };

  const save = async () => {
    if (invalid || loading || loadError || lock.current) return;
    lock.current = true; setSaving(true); setError("");
    let profileUpdated = false;
    try {
      if (profileModel && profile && tokenLimit !== null) {
        const values: Partial<ModelLabProfileInput> = { provider: connection.provider, base_url: connection.base_url.trim(), model_id: connection.model_id.trim(), max_output_tokens: tokenLimit, timeout_seconds: Number(timeout), thinking_mode: thinking };
        const changed = Object.fromEntries(Object.entries(values).filter(([key, value]) => profile[key as keyof ModelLabProfile] !== value)) as Partial<ModelLabProfileInput>;
        if (apiKey.trim()) changed.api_key = apiKey.trim();
        if (Object.keys(changed).length) { const updated = await api.modelLabUpdateProfile(profile.id, changed); profileUpdated = true; setProfile(updated); setApiKey(""); }
      }
      let nextSpec = !profileModel && !platform ? advanced ? parseStrategyConfiguration(json) : specFromForm() : null;
      if (nextSpec && serviceModel && authMode !== "keep") {
        const value = { ...service, endpoint: String(nextSpec.endpoint ?? service.endpoint), auth_mode: authMode };
        const signature = JSON.stringify([value.endpoint, value.auth_mode, value.api_key, value.header_name, value.secret_env_ref]);
        const headers = credentialCache.current?.signature === signature ? credentialCache.current.headers : await saveServiceCredentials(value);
        credentialCache.current = { signature, headers };
        nextSpec = { ...nextSpec, headers };
      }
      const saved = await modelLibraryApi.updateConfiguration(model.id, { name: name.trim(), ...(nextSpec ? { strategy_spec: nextSpec } : {}) });
      setService((current) => ({ ...current, api_key: "" }));
      onSaved(saved);
    } catch (reason) {
      let message = reason instanceof Error ? reason.message : "配置保存失败，请重试。";
      for (const secret of [apiKey, service.api_key].filter(Boolean)) message = message.split(secret).join("[密钥已隐藏]");
      setError(profileUpdated ? `API 连接配置已保存，但模型名称保存未完成：${message}。可重试保存名称。` : message);
    } finally { lock.current = false; setSaving(false); }
  };

  const parameterField = (key: string, label: string, fallback: number, help: string, integer = true) => <label className="block text-[12px] font-medium text-ink">{label}<input className={INPUT} type="number" step={integer ? 1 : 0.1} min={integer ? 1 : undefined} max={integer ? 10000 : undefined} value={Number.isFinite(Number(parameters[key] ?? fallback)) ? Number(parameters[key] ?? fallback) : ""} onChange={(event) => setParameter(key, event.target.value === "" ? Number.NaN : Number(event.target.value))} /><span className="mt-1.5 block text-[11px] font-normal leading-relaxed text-mute">{help}</span></label>;

  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/35 p-3 backdrop-blur-sm sm:p-5">
    <div ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="model-configuration-title" tabIndex={-1} className="flex max-h-[94dvh] w-full max-w-[1050px] flex-col overflow-hidden rounded-2xl border border-edge bg-paper shadow-pop outline-none">
      <header className="flex items-start justify-between gap-3 border-b border-edge bg-card px-5 py-4"><div><p className="text-[10px] font-semibold uppercase tracking-[.16em] text-faint">MODEL SETTINGS</p><h2 id="model-configuration-title" className="mt-1 font-serif text-[22px] font-semibold text-ink">修改模型配置</h2><p className="mt-1.5 text-[12px] text-mute">保存后用于这个模型的新测试；已创建实验保留各自的配置。</p></div><button type="button" onClick={onClose} disabled={saving} aria-label="关闭修改模型配置" className="rounded-lg p-2 text-mute disabled:opacity-40"><X size={18} /></button></header>
      <main className="min-h-0 flex-1 overflow-y-auto p-4 sm:p-5">
        {loading ? <div role="status" className="flex items-center justify-center gap-2 py-16 text-sm text-mute"><Loader2 size={18} className="animate-spin" />正在读取已保存的配置…</div> : loadError ? <div role="alert" className="rounded-xl border border-rise/20 bg-rise/5 p-4 text-sm text-rise"><p>{loadError}</p><button type="button" onClick={() => setLoadVersion((value) => value + 1)} className="mt-3 rounded-lg border border-edge bg-card px-3 py-2 text-ink">重新读取</button></div> : <fieldset disabled={saving} className="space-y-4">
          <section className={SECTION}><span className="rounded-full bg-paper px-2.5 py-1 text-[11px] text-mute">{modelKindLabel(latest.kind)}</span><label className="mt-4 block text-[12px] font-medium text-ink">模型名称<input className={INPUT} value={name} maxLength={120} onChange={(event) => setName(event.target.value)} /></label></section>
          {platform && <section className="rounded-xl border border-jade/20 bg-jade-soft/25 p-4"><h3 className="flex items-center gap-2 text-[13px] font-semibold text-ink"><Settings2 size={16} />平台统一基模</h3><p className="mt-2 text-[12px] leading-relaxed text-mute">这个模型使用平台统一基模。需要更换其 API 连接时，请到专门的设置页面；该操作会同步应用到研究工作台和其他使用平台默认基模的功能。</p><a href="/backtest/model-lab?tab=platform" className="mt-3 inline-flex items-center gap-1 text-[12px] font-medium text-jade">打开平台统一基模设置<ArrowUpRight size={14} /></a></section>}
          {profileModel && <section className={SECTION}><h3 className="text-[13px] font-semibold text-ink">API 连接：{profile?.name}</h3><div className="mt-2 rounded-lg bg-violet-soft/30 p-3 text-[12px] leading-relaxed text-mute"><p>使用这条 API 连接的独立模型与 Pronoia 版本会同步更新连接配置，包括未来的预测、问答与评分调用。已创建的实验保留原配置。</p>{platformProfile && <p className="mt-2 font-medium text-ink">这条连接也是当前的平台统一基模，修改会同步影响研究工作台和其他使用默认基模的页面。</p>}</div><div className="mt-4 grid gap-4 sm:grid-cols-2">
            <ModelProviderFields value={connection} onChange={(patch) => setConnection((current) => ({ ...current, ...patch }))} disabled={saving} />
            <label className="text-[12px] font-medium text-ink sm:col-span-2">API Key<input className={INPUT} type="password" autoComplete="new-password" spellCheck={false} value={apiKey} onChange={(event) => setApiKey(event.target.value)} placeholder={needsNewApiKey ? "请填写新服务地址对应的 API Key" : profile?.secret_configured ? "已保存密钥；留空继续使用，填写新密钥则替换" : "填写服务商提供的 API Key"} /><span className={cls("mt-1.5 block text-[11px] font-normal", needsNewApiKey ? "text-amber-700" : "text-mute")}>{needsNewApiKey ? "已更换服务商或接口来源，需要重新填写匹配的密钥。" : "已保存的密钥不会显示在页面上。只修改模型 ID、输出上限或等待时间时，留空保留原密钥。"}</span></label>
            <label className="text-[12px] font-medium text-ink">单次输出上限（tokens）<input className={INPUT} type="number" min={MIN_MAX_OUTPUT_TOKENS} max={MAX_MAX_OUTPUT_TOKENS} step={1} value={tokens} onChange={(event) => setTokens(event.target.value)} /><span className="mt-1.5 block text-[11px] font-normal leading-relaxed text-mute">{OUTPUT_TOKEN_LIMIT_HINT}</span></label>
            <label className="text-[12px] font-medium text-ink">单次请求最长等待（秒）<input className={INPUT} type="number" min={0.1} max={900} value={timeout} onChange={(event) => setTimeout(event.target.value)} /></label>
            <label className="text-[12px] font-medium text-ink">思考模式<select className={INPUT} value={thinking} onChange={(event) => setThinking(event.target.value as ModelLabProfile["thinking_mode"])}><option value="auto">自动（由模型决定）</option><option value="disabled">关闭思考</option><option value="enabled">开启思考</option></select><span className="mt-1.5 block text-[11px] font-normal text-mute">服务商和模型需支持所选模式。</span></label>
          </div></section>}
          {!platform && !profileModel && spec && <>
            <div className="flex flex-wrap items-center justify-between gap-2"><h3 className="text-[13px] font-semibold text-ink">{latest.category === "quant" ? "量化模型参数" : "预测模型参数"}</h3><button type="button" onClick={switchAdvanced} className="text-[12px] font-medium text-violet">{advanced ? "返回参数表单" : "高级：编辑完整配置"}</button></div>
            {advanced ? <section className={SECTION}><p className="text-[12px] leading-relaxed text-mute">直接编辑完整 JSON 配置。已保存的认证信息由服务端保留；密钥请使用下方认证设置填写。</p>{!rulesEditable && <p className="mt-2 text-[12px] text-amber-700">此模型包含参数表单尚未支持的规则，已完整载入，避免用默认值覆盖。</p>}<label className="mt-3 block text-[12px] font-medium text-ink">完整模型配置<textarea className={cls(INPUT, "min-h-[320px] font-mono text-[11px] leading-5")} spellCheck={false} value={json} onChange={(event) => setJson(event.target.value)} /></label></section> : <>
              {spec.kind === "declarative_rules" && !serviceModel && <QuantEditor form={draft} setForm={(next) => { setDraft(next); setDraftDirty(true); }} showAssetClass={false} />}
              {spec.kind === "return_forecast" && !serviceModel && spec.source !== "signal_file" && spec.adapter !== "signal_file" && <section className={SECTION}><p className="mb-4 text-[12px] leading-relaxed text-mute">使用历史收益均值预测未来收益率。预测周期、标的和数据范围在发起测试时选择。</p>{parameterField("lookback", "历史样本窗口", 20, "使用最近多少个已实现的历史收益样本。")}</section>}
              {spec.kind === "ma_cross" && !serviceModel && <section className={cls(SECTION, "grid gap-4 sm:grid-cols-2")}>{parameterField("short_window", "短期均线周期", 5, "单位为 K 线根数，应小于长期均线。")}{parameterField("long_window", "长期均线周期", 20, "单位为 K 线根数。")}{parameterField("short_weight", "短期均线不高于长期均线时的仓位", -1, "1 表示满仓多头，0 表示空仓，−1 表示满仓空头；成交仍受测试规则约束。", false)}</section>}
              {spec.kind === "momentum" && !serviceModel && <section className={cls(SECTION, "grid gap-4 sm:grid-cols-2")}>{parameterField("lookback", "动量回看周期", 20, "比较当前收盘价与此前 N 根 K 线的收盘价。")}{parameterField("negative_weight", "动量不为正时的仓位", -1, "1 表示满仓多头，0 表示空仓，−1 表示满仓空头；成交仍受测试规则约束。", false)}</section>}
              {spec.kind === "buy_hold" && <section className={SECTION}><h4 className="text-[13px] font-semibold text-ink">买入并持有</h4><p className="mt-2 text-[12px] leading-relaxed text-mute">第一根 K 线收盘后发出满仓买入信号，在下一根 K 线开盘成交并持有。此模型没有额外算法参数，资金与时间范围在发起测试时设置。</p></section>}
              {(spec.kind === "signal_file" || spec.source === "signal_file" || spec.adapter === "signal_file") && <section className={SECTION}><label className="block text-[12px] font-medium text-ink">默认预测文件路径<input className={INPUT} value={String(spec.path ?? "")} onChange={(event) => setSpec((current) => current ? { ...current, path: event.target.value } : null)} placeholder="本机 CSV / JSON 预测文件路径" /></label><p className="mt-2 text-[11px] text-mute">发起测试时仍可按本次标的替换文件。</p></section>}
              {latest.kind === "imported_predictions" && <section className={SECTION}><p className="text-[12px] leading-relaxed text-mute">这个模型使用导入的预测结果。发起测试时上传对应事件、标的与预测窗口的文件；模型名称和历史归属保持独立。</p></section>}
              {serviceModel && <section className={cls(SECTION, "grid gap-4 sm:grid-cols-2")}><label className="text-[12px] font-medium text-ink sm:col-span-2">预测服务地址<input className={INPUT} value={service.endpoint} onChange={(event) => setService((current) => ({ ...current, endpoint: event.target.value }))} placeholder="https://your-service.example/predict" autoCapitalize="none" spellCheck={false} /></label><label className="text-[12px] font-medium text-ink">单次请求最长等待（秒）<input className={INPUT} type="number" min={1} max={300} step={1} value={service.timeout_seconds} onChange={(event) => setService((current) => ({ ...current, timeout_seconds: Number(event.target.value) }))} /></label><label className="text-[12px] font-medium text-ink">版本标签<input className={INPUT} value={service.version} maxLength={80} onChange={(event) => setService((current) => ({ ...current, version: event.target.value }))} placeholder="例如：金融预测 v2" /></label></section>}
              {!serviceModel && !["declarative_rules", "return_forecast", "ma_cross", "momentum", "buy_hold", "signal_file"].includes(String(spec.kind)) && latest.kind !== "imported_predictions" && <section className={SECTION}><p className="text-[12px] leading-relaxed text-mute">这个模型使用自定义配置。点击“高级：编辑完整配置”可直接修改已保存的参数。</p></section>}
            </>}
            {serviceModel && <section className={cls(SECTION, "grid gap-4 sm:grid-cols-2")}><label className="text-[12px] font-medium text-ink sm:col-span-2">服务认证<select className={INPUT} value={authMode} onChange={(event) => { setAuthMode(event.target.value as typeof authMode); setService((current) => ({ ...current, api_key: "" })); }}><option value="keep">保留已保存的认证信息</option><option value="bearer">使用新的 API Key（Bearer）</option><option value="header">使用新的 API Key（自定义请求头）</option><option value="env">使用已配置的密钥变量</option><option value="none">无需认证</option></select><span className="mt-1.5 block text-[11px] font-normal leading-relaxed text-mute">修改等待时间或版本时，可保留原认证。更换服务地址时，请按新服务要求选择认证方式。</span></label>
              {(authMode === "bearer" || authMode === "header") && <label className="text-[12px] font-medium text-ink">新 API Key<input className={INPUT} type="password" value={service.api_key} autoComplete="new-password" spellCheck={false} onChange={(event) => setService((current) => ({ ...current, api_key: event.target.value }))} /></label>}
              {(authMode === "header" || authMode === "env") && <label className="text-[12px] font-medium text-ink">认证请求头名称<input className={INPUT} value={service.header_name} onChange={(event) => setService((current) => ({ ...current, header_name: event.target.value }))} placeholder="例如：X-API-Key" /></label>}
              {authMode === "env" && <label className="text-[12px] font-medium text-ink sm:col-span-2">密钥变量名<input className={INPUT} value={service.secret_env_ref} onChange={(event) => setService((current) => ({ ...current, secret_env_ref: event.target.value }))} placeholder="PRONOIA_STRATEGY_SECRET_VENDOR" /></label>}
            </section>}
          </>}
        </fieldset>}
        {error && <div role="alert" className="mt-4 flex items-start gap-2 rounded-xl border border-rise/20 bg-rise/5 p-3 text-[12px] leading-relaxed text-rise"><AlertCircle size={14} className="mt-0.5 shrink-0" /><p className="break-words">{error}</p></div>}
      </main>
      <footer className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-t border-edge bg-card px-5 py-4"><div><p className={cls("text-[12px]", invalid ? "text-amber-700" : "text-mute")}>{!loading && !loadError ? invalid || "保存后继续归在当前模型下。" : ""}</p><p className="mt-1 text-[11px] text-faint">保存配置不会调用模型或启动测试。</p></div><div className="flex gap-2"><button type="button" onClick={onClose} disabled={saving} className="rounded-lg border border-edge px-3 py-2 text-[12px] text-mute disabled:opacity-40">取消</button><button type="button" onClick={() => void save()} disabled={saving || loading || Boolean(loadError) || Boolean(invalid)} className="inline-flex items-center gap-2 rounded-lg bg-ink px-4 py-2.5 text-[12px] font-semibold text-card disabled:opacity-40">{saving ? <Loader2 size={14} className="animate-spin" /> : <Save size={14} />}{saving ? "正在保存…" : "保存修改"}</button></div></footer>
    </div>
  </div>;
}
