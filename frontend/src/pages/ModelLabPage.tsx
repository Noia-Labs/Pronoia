import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  FlaskConical,
  Gauge,
  KeyRound,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Square,
  Trash2,
  X,
} from "lucide-react";
import { api } from "../api";
import BacktestNav from "../components/backtest/BacktestNav";
import ModelProviderFields from "../components/ModelProviderFields";
import PlatformBaseModelSettings from "../components/PlatformBaseModelSettings";
import ModelApiKeyFields, { isModelCredentialValid, MODEL_API_KEY_HINT, modelCredentialError, modelCredentialPayload, type ModelCredentialMode } from "../components/ModelApiKeyFields";
import { providerLabel } from "../modelProviders";
import {
  DEFAULT_MAX_OUTPUT_TOKENS,
  DEFAULT_MODEL_TIMEOUT_SECONDS,
  MAX_MAX_OUTPUT_TOKENS,
  MIN_MAX_OUTPUT_TOKENS,
  OUTPUT_TOKEN_LIMIT_HINT,
  parseOutputTokenLimit,
} from "../modelDefaults";
import type {
  ModelLabBatch,
  ModelLabProfile,
  ModelLabProfileInput,
  ModelLabResults,
} from "../types";
import { cls, relTime } from "../utils";
import ModelLabBatchComposer from "./model-lab/ModelLabBatchComposer";
import ModelLabResultsPanel, { LabStatus, batchNeedsRefresh, batchIsDemo, Field, Empty } from "./model-lab/ModelLabResultsPanel";

type LabTab = "compose" | "profiles" | "results" | "platform";

const INPUT = "w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[11px] text-ink outline-none transition placeholder:text-faint focus:border-violet/50 focus:ring-2 focus:ring-violet/10 disabled:cursor-not-allowed disabled:bg-edge/35";

export default function ModelLabPage() {
  const [tab, setTab] = useState<LabTab>(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.has("batch")) return "results";
    const requested = params.get("tab");
    return requested === "platform" || requested === "profiles" || requested === "results" ? requested : "compose";
  });
  const [profiles, setProfiles] = useState<ModelLabProfile[]>([]);
  const [defaultProfileId, setDefaultProfileId] = useState<string | null>(null);
  const [batches, setBatches] = useState<ModelLabBatch[]>([]);
  const [selectedBatchId, setSelectedBatchId] = useState<string | null>(() => new URLSearchParams(window.location.search).get("batch"));
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [refreshRevision, setRefreshRevision] = useState(0);
  const pageMountedRef = useRef(true);
  const loadInFlightRef = useRef(false);

  const load = useCallback(async (quiet = false) => {
    if (loadInFlightRef.current) return;
    loadInFlightRef.current = true;
    if (!quiet) setLoading(true);
    setError(null);
    try {
      const [profileResponse, batchResponse] = await Promise.all([api.modelLabProfiles(), api.modelLabBatches(100)]);
      if (!pageMountedRef.current) return;
      setProfiles(profileResponse.items);
      setDefaultProfileId(profileResponse.default_profile_id);
      setBatches(batchResponse.items.filter((batch) => batch.scoring?.source !== "event_experiment"));
    } catch (reason) {
      if (pageMountedRef.current) setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      loadInFlightRef.current = false;
      if (!quiet && pageMountedRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    pageMountedRef.current = true;
    void load();
    return () => { pageMountedRef.current = false; };
  }, [load]);
  useEffect(() => {
    if (!batches.some((batch) => batchNeedsRefresh(batch.status))) return;
    const timer = window.setInterval(() => void load(true), 3000);
    return () => window.clearInterval(timer);
  }, [batches, load]);

  const refreshAll = useCallback(() => {
    setRefreshRevision((revision) => revision + 1);
    void load();
  }, [load]);

  const openCreatedBatch = (batch: ModelLabBatch) => {
    setSelectedBatchId(batch.id);
    setTab("results");
    void load(true);
  };

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper">
      <BacktestNav actions={<button type="button" onClick={refreshAll} disabled={loading} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[10.5px] font-medium text-mute transition hover:text-ink disabled:opacity-50"><RefreshCw size={11} className={cls(loading && "animate-spin")} />刷新</button>} />
      <main className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1500px] px-5 py-6 sm:px-7 lg:px-9">
          <section className="flex flex-col justify-between gap-4 lg:flex-row lg:items-end">
            <div>
              <p className="text-[9px] font-semibold uppercase tracking-[0.2em] text-violet">Pronoia model evaluation</p>
              <h1 className="mt-2 font-serif text-[27px] font-semibold text-ink">Pronoia 基模评测</h1>
              <p className="mt-1.5 max-w-3xl text-[11px] leading-relaxed text-mute">让不同基模运行同一套 Pronoia，使用相同事件集和预测周期比较看涨 / 看跌判断、收益率预测与问答质量。基模选择只作用于本批次，其他页面沿用平台基模。</p>
            </div>
            <div className="grid grid-cols-3 gap-px overflow-hidden rounded-xl border border-edge bg-edge shadow-card">
              <SummaryCell label="模型 API" value={String(profiles.filter((profile) => profile.is_active).length)} />
              <SummaryCell label="评测批次" value={String(batches.length)} />
              <SummaryCell label="运行中" value={String(batches.filter((batch) => batchNeedsRefresh(batch.status)).length)} />
            </div>
          </section>

          <div className="mt-6 flex flex-wrap gap-1 rounded-xl border border-edge bg-card p-1.5 shadow-card">
            <TabButton active={tab === "compose"} onClick={() => setTab("compose")} icon={<Plus size={11} />} label="新建评测" />
            <TabButton active={tab === "profiles"} onClick={() => setTab("profiles")} icon={<KeyRound size={11} />} label="基模连接" badge={profiles.length} />
            <TabButton active={tab === "platform"} onClick={() => setTab("platform")} icon={<ShieldCheck size={11} />} label="平台统一基模" />
            <TabButton active={tab === "results"} onClick={() => setTab("results")} icon={<BarChart3 size={11} />} label="批次与结果" badge={batches.length} />
          </div>

          {error && <div className="mt-4 flex items-start gap-2 rounded-lg border border-rise/20 bg-rise/5 px-3.5 py-3 text-[10px] text-rise"><AlertTriangle size={12} className="mt-0.5 shrink-0" />模型评测服务连接失败：{error}</div>}

          <div className="mt-4">
            {tab === "compose" && <ModelLabBatchComposer onCreated={openCreatedBatch} onManageProfiles={() => setTab("profiles")} />}
            {tab === "platform" && <PlatformBaseModelSettings profiles={profiles} defaultProfileId={defaultProfileId} onChanged={refreshAll} />}
            {tab === "profiles" && <ProfileManager profiles={profiles} defaultProfileId={defaultProfileId} onChanged={refreshAll} />}
            {tab === "results" && <BatchRegistry batches={batches} profiles={profiles} selectedBatchId={selectedBatchId} refreshRevision={refreshRevision} onSelect={setSelectedBatchId} onChanged={refreshAll} />}
          </div>
        </div>
      </main>
    </div>
  );
}

function SummaryCell({ label, value }: { label: string; value: string }) {
  return <div className="min-w-[88px] bg-card px-3.5 py-2.5 text-center"><p className="text-[8px] text-faint">{label}</p><p className="mt-1 font-mono text-[15px] font-semibold text-ink">{value}</p></div>;
}

function TabButton({ active, onClick, icon, label, badge }: { active: boolean; onClick: () => void; icon: ReactNode; label: string; badge?: number }) {
  return <button type="button" onClick={onClick} className={cls("inline-flex items-center gap-1.5 rounded-lg px-3.5 py-2 text-[10.5px] font-semibold transition", active ? "bg-violet-soft text-violet" : "text-mute hover:bg-paper hover:text-ink")}>{icon}{label}{badge !== undefined && <span className={cls("rounded-full px-1.5 py-0.5 font-mono text-[8px]", active ? "bg-card/80" : "bg-edge/55")}>{badge}</span>}</button>;
}

type ModelLabProfileForm = Omit<ModelLabProfileInput, "max_output_tokens" | "api_key" | "secret_env_ref"> & { max_output_tokens: string; secret_env_ref: string };

const EMPTY_PROFILE: ModelLabProfileForm = {
  name: "",
  provider: "custom",
  base_url: "",
  model_id: "",
  secret_env_ref: "",
  max_output_tokens: String(DEFAULT_MAX_OUTPUT_TOKENS),
  timeout_seconds: DEFAULT_MODEL_TIMEOUT_SECONDS,
  thinking_mode: "auto",
  input_price_per_million: 0,
  output_price_per_million: 0,
  currency: "CNY",
  is_active: true,
};

function ProfileManager({ profiles, defaultProfileId, onChanged }: { profiles: ModelLabProfile[]; defaultProfileId: string | null; onChanged: () => void }) {
  const [showCreate, setShowCreate] = useState(profiles.length === 0);
  const [form, setForm] = useState<ModelLabProfileForm>(EMPTY_PROFILE);
  const [apiKey, setApiKey] = useState("");
  const [credentialMode, setCredentialMode] = useState<ModelCredentialMode>("api_key");
  const [keyProfileId, setKeyProfileId] = useState<string | null>(null);
  const [replacementApiKey, setReplacementApiKey] = useState("");
  const [keyError, setKeyError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const tokenLimit = parseOutputTokenLimit(form.max_output_tokens);
  const valid = Boolean(form.name.trim() && form.base_url.trim() && form.model_id.trim() && isModelCredentialValid(credentialMode, apiKey, form.secret_env_ref) && tokenLimit !== null);

  const create = async () => {
    if (busy !== null || !valid || tokenLimit === null) return;
    setBusy("create");
    setError(null);
    try {
      const { secret_env_ref: secretEnvRef, ...settings } = form;
      await api.modelLabCreateProfile({ ...settings, max_output_tokens: tokenLimit, ...modelCredentialPayload(credentialMode, apiKey, secretEnvRef) });
      setApiKey("");
      setCredentialMode("api_key");
      setForm(EMPTY_PROFILE);
      setShowCreate(false);
      onChanged();
    } catch (reason) {
      setError(modelCredentialError(reason, apiKey));
    } finally { setBusy(null); }
  };

  const saveApiKey = async () => {
    if (busy !== null || !keyProfileId || !replacementApiKey.trim()) return;
    setBusy(`key:${keyProfileId}`);
    setKeyError(null);
    try {
      await api.modelLabUpdateProfile(keyProfileId, { api_key: replacementApiKey.trim() });
      setReplacementApiKey("");
      setKeyProfileId(null);
      onChanged();
    } catch (reason) {
      setKeyError(modelCredentialError(reason, replacementApiKey));
    } finally { setBusy(null); }
  };

  const closeApiKeyEditor = () => {
    setReplacementApiKey("");
    setKeyError(null);
    setKeyProfileId(null);
  };

  const validate = async (id: string) => {
    setBusy(`validate:${id}`);
    setError(null);
    try { await api.modelLabValidateProfile(id); onChanged(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(null); }
  };

  const toggleActive = async (profile: ModelLabProfile) => {
    setBusy(`active:${profile.id}`);
    setError(null);
    try { await api.modelLabUpdateProfile(profile.id, { is_active: !profile.is_active }); onChanged(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(null); }
  };

  const remove = async (profile: ModelLabProfile) => {
    if (!window.confirm(`确认删除模型连接“${profile.name}”？历史批次仍保留其配置快照。`)) return;
    setBusy(`delete:${profile.id}`);
    setError(null);
    try { await api.modelLabDeleteProfile(profile.id); onChanged(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(null); }
  };

  return (
    <div className="grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_430px]">
      <section className="overflow-hidden rounded-xl border border-edge bg-card shadow-card">
        <div className="flex items-center justify-between border-b border-edge px-5 py-4"><div><h2 className="font-serif text-[15px] font-semibold text-ink">模型 API 目录</h2><p className="mt-1 text-[9.5px] text-mute">这些连接可用于本批次基模对比或外部模型调用。添加、选择评测连接不会更换平台基模；全站切换请进入“平台统一基模”。</p></div><button type="button" onClick={() => setShowCreate(true)} className="inline-flex items-center gap-1 rounded-lg bg-ink px-3 py-2 text-[10px] font-semibold text-card"><Plus size={10} />添加</button></div>
        {profiles.length === 0 ? <Empty title="还没有模型连接" note="在右侧选择模型服务商，或填写自定义聊天接口。保存后即可用于预测、问答和模型比较。" icon={<KeyRound size={18} />} /> : (
          <div className="divide-y divide-edge">
            {profiles.map((profile) => {
              const isDefault = profile.id === defaultProfileId;
              return (
                <article key={profile.id} className="px-5 py-4">
                  <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
                    <div className="min-w-0">
                      <div className="flex flex-wrap items-center gap-2"><h3 className="text-[11.5px] font-semibold text-ink">{profile.name}</h3>{isDefault && <span className="rounded-full border border-violet/20 bg-violet-soft px-2 py-0.5 text-[8.5px] font-semibold text-violet">平台统一基模</span>}{!profile.is_active && <span className="rounded-full bg-edge px-2 py-0.5 text-[8.5px] text-mute">已停用</span>}<span className={cls("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[8.5px]", profile.secret_configured ? "bg-jade-soft text-jade" : "bg-amber-50 text-amber-700")}>{profile.secret_configured ? <CheckCircle2 size={9} /> : <AlertTriangle size={9} />}{profile.secret_configured ? "密钥已配置" : "密钥未配置"}</span></div>
                      <p className="mt-1 truncate font-mono text-[9px] text-mute">{providerLabel(profile.provider)} · {profile.model_id}</p>
                      <p className="mt-1 truncate font-mono text-[8px] text-faint">{profile.base_url}</p>
                      {profile.last_validation_message && <p className={cls("mt-2 text-[8.5px]", profile.last_validation_status === "ok" ? "text-jade" : "text-rise")}>{profile.last_validation_message}</p>}
                    </div>
                    <div className="flex shrink-0 flex-wrap items-center gap-1.5">
                      <button type="button" onClick={() => void validate(profile.id)} disabled={busy !== null} className="inline-flex items-center gap-1 rounded-md border border-edge bg-paper px-2.5 py-1.5 text-[9px] font-medium text-mute hover:text-ink disabled:opacity-50">{busy === `validate:${profile.id}` ? <Loader2 size={9} className="animate-spin" /> : <Gauge size={9} />}验证连接</button>
                      <button type="button" onClick={() => { setKeyProfileId(profile.id); setReplacementApiKey(""); setKeyError(null); }} disabled={busy !== null} className="inline-flex items-center gap-1 rounded-md border border-edge bg-paper px-2.5 py-1.5 text-[9px] font-medium text-mute hover:text-ink disabled:opacity-50"><KeyRound size={9} />修改 API Key</button>
                      {!isDefault && <button type="button" onClick={() => void toggleActive(profile)} disabled={busy !== null} className="rounded-md border border-edge bg-paper px-2 py-1 text-[8.5px] font-medium text-mute disabled:opacity-50">{busy === `active:${profile.id}` ? "保存中" : profile.is_active ? "停用" : "启用"}</button>}
                      <button type="button" onClick={() => void remove(profile)} disabled={busy !== null || isDefault} title={isDefault ? "请先切换默认模型" : "删除"} className="rounded-md p-1.5 text-faint hover:bg-rise/5 hover:text-rise disabled:opacity-35"><Trash2 size={10} /></button>
                    </div>
                  </div>
                  {keyProfileId === profile.id && (
                    <div className="mt-3 rounded-lg border border-violet/20 bg-violet-soft/20 p-3">
                      <label className="block text-[10px] text-mute">
                        新 API Key
                        <input
                          type="password"
                          value={replacementApiKey}
                          onChange={(event) => setReplacementApiKey(event.target.value)}
                          disabled={busy !== null}
                          autoComplete="new-password"
                          autoCapitalize="none"
                          autoCorrect="off"
                          spellCheck={false}
                          placeholder="粘贴此模型服务商的新 API Key"
                          className={cls(INPUT, "mt-1.5")}
                        />
                      </label>
                      <p className="mt-1 text-[9px] leading-relaxed text-faint">{MODEL_API_KEY_HINT} 已保存的密钥不会回填到页面。</p>
                      {keyError && <p role="alert" className="mt-2 text-[9px] text-rise">{keyError}</p>}
                      <div className="mt-3 flex justify-end gap-2">
                        <button type="button" disabled={busy !== null} onClick={closeApiKeyEditor} className="rounded-md border border-edge px-3 py-1.5 text-[9px] text-mute disabled:opacity-50">取消</button>
                        <button type="button" disabled={busy !== null || !replacementApiKey.trim()} onClick={() => void saveApiKey()} className="inline-flex items-center gap-1 rounded-md bg-violet px-3 py-1.5 text-[9px] font-medium text-white disabled:opacity-50">{busy === `key:${profile.id}` ? <Loader2 size={9} className="animate-spin" /> : <Save size={9} />}保存 API Key</button>
                      </div>
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className={cls("overflow-hidden rounded-xl border bg-card shadow-card", showCreate ? "border-violet/25" : "border-edge")}>
        <div className="flex items-center justify-between border-b border-edge bg-violet-soft/20 px-5 py-4"><div><p className="text-[9px] font-semibold uppercase tracking-[0.16em] text-violet">Provider profile</p><h2 className="mt-1 font-serif text-[14px] font-semibold text-ink">添加模型连接</h2></div>{showCreate && profiles.length > 0 && <button type="button" disabled={busy !== null} onClick={() => { setApiKey(""); setError(null); setShowCreate(false); }} className="rounded-md p-1.5 text-mute hover:bg-card"><X size={12} /></button>}</div>
        {!showCreate ? <div className="grid min-h-52 place-items-center px-6 text-center"><div><KeyRound size={20} className="mx-auto text-faint" /><p className="mt-3 text-[10px] text-mute">点击“添加”登记另一个 API。</p></div></div> : (
          <div className="space-y-3 p-5">
            <Field label="显示名称"><input value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} disabled={busy !== null} className={INPUT} placeholder="例如：Qwen 事件测试" /></Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <ModelProviderFields
                value={form}
                onChange={(patch) => {
                  if (patch.provider !== undefined && patch.provider !== form.provider) {
                    setApiKey("");
                    setError(null);
                  }
                  setForm((current) => ({ ...current, ...patch }));
                }}
                disabled={busy !== null}
              />
            </div>
            <ModelApiKeyFields
              mode={credentialMode}
              apiKey={apiKey}
              secretEnvRef={form.secret_env_ref}
              onModeChange={setCredentialMode}
              onApiKeyChange={setApiKey}
              onSecretEnvRefChange={(secretEnvRef) => setForm((current) => ({ ...current, secret_env_ref: secretEnvRef }))}
              disabled={busy !== null}
            />
            <Field label="单次模型输出上限（tokens）" hint={`默认 ${DEFAULT_MAX_OUTPUT_TOKENS}`}>
              <input
                type="number"
                min={MIN_MAX_OUTPUT_TOKENS}
                max={MAX_MAX_OUTPUT_TOKENS}
                step={1}
                value={form.max_output_tokens}
                onChange={(event) => setForm((current) => ({ ...current, max_output_tokens: event.target.value }))}
                disabled={busy !== null}
                aria-invalid={tokenLimit === null}
                className={INPUT}
              />
              <p className="mt-1 text-[9px] leading-relaxed text-faint">可填 {MIN_MAX_OUTPUT_TOKENS}–{MAX_MAX_OUTPUT_TOKENS} 的整数。{OUTPUT_TOKEN_LIMIT_HINT}</p>
              {tokenLimit === null && <p className="mt-1 text-[9px] text-rise">请输入 {MIN_MAX_OUTPUT_TOKENS}–{MAX_MAX_OUTPUT_TOKENS} 之间的整数。</p>}
            </Field>
            <Field label="思考模式"><select value={form.thinking_mode} onChange={(event) => setForm({ ...form, thinking_mode: event.target.value as ModelLabProfileInput["thinking_mode"] })} disabled={busy !== null} className={INPUT}><option value="auto">自动</option><option value="enabled">启用</option><option value="disabled">关闭</option></select></Field>
            {error && <div className="rounded-lg border border-rise/20 bg-rise/5 px-3 py-2 text-[9px] text-rise">{error}</div>}
            <button type="button" onClick={() => void create()} disabled={!valid || busy !== null} className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-ink px-3.5 py-2.5 text-[10.5px] font-semibold text-card disabled:cursor-not-allowed disabled:opacity-40">{busy === "create" ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />}保存模型连接</button>
          </div>
        )}
      </section>
    </div>
  );
}

function BatchRegistry({ batches, profiles, selectedBatchId, refreshRevision, onSelect, onChanged }: { batches: ModelLabBatch[]; profiles: ModelLabProfile[]; selectedBatchId: string | null; refreshRevision: number; onSelect: (id: string | null) => void; onChanged: () => void }) {
  const [results, setResults] = useState<ModelLabResults | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [actionId, setActionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const mountedRef = useRef(true);
  const resultInFlightRef = useRef(false);
  const desiredBatchIdRef = useRef<string | null>(selectedBatchId);
  const queuedRefreshRef = useRef<{ batchId: string; showLoading: boolean } | null>(null);
  const fetchRunnerRef = useRef<(batchId: string, showLoading: boolean) => Promise<void>>(async () => undefined);

  const fetchResults = useCallback(async (batchId: string, showLoading: boolean) => {
    desiredBatchIdRef.current = batchId;
    if (resultInFlightRef.current) {
      queuedRefreshRef.current = { batchId, showLoading };
      return;
    }
    resultInFlightRef.current = true;
    if (showLoading && mountedRef.current) setLoadingId(batchId);
    if (mountedRef.current) setError(null);
    try {
      const next = await api.modelLabResults(batchId);
      if (!mountedRef.current || desiredBatchIdRef.current !== batchId) return;
      setResults(next);
    } catch (reason) {
      if (!mountedRef.current || desiredBatchIdRef.current !== batchId) return;
      if (showLoading) setResults(null);
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      resultInFlightRef.current = false;
      if (mountedRef.current && desiredBatchIdRef.current === batchId) setLoadingId(null);
      const queued = queuedRefreshRef.current;
      queuedRefreshRef.current = null;
      if (mountedRef.current && queued) void fetchRunnerRef.current(queued.batchId, queued.showLoading);
    }
  }, []);
  fetchRunnerRef.current = fetchResults;

  const open = (batchId: string) => {
    if (batchId === selectedBatchId) void fetchResults(batchId, true);
    else onSelect(batchId);
  };

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      desiredBatchIdRef.current = null;
      queuedRefreshRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (selectedBatchId) void fetchResults(selectedBatchId, true);
  }, [fetchResults, refreshRevision, selectedBatchId]);

  const selectedBatch = batches.find((batch) => batch.id === selectedBatchId);
  useEffect(() => {
    if (!selectedBatchId || !selectedBatch || !batchNeedsRefresh(selectedBatch.status)) return;
    const timer = window.setInterval(() => void fetchResults(selectedBatchId, false), 2500);
    return () => window.clearInterval(timer);
  }, [fetchResults, selectedBatch?.status, selectedBatchId]);

  const action = async (batch: ModelLabBatch, kind: "start" | "cancel") => {
    setActionId(batch.id);
    setError(null);
    try {
      if (kind === "start") await api.modelLabStartBatch(batch.id); else await api.modelLabCancelBatch(batch.id);
      onChanged();
      await fetchResults(batch.id, false);
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setActionId(null); }
  };

  return (
    <div className="grid items-start gap-4 min-[1680px]:grid-cols-[320px_minmax(0,1fr)]">
      <section className="overflow-hidden rounded-xl border border-edge bg-card shadow-card">
        <div className="border-b border-edge px-5 py-4"><h2 className="font-serif text-[15px] font-semibold text-ink">评测批次</h2><p className="mt-1 text-[9px] text-mute">选择批次查看预测与问答各自的进度和结果。</p></div>
        {batches.length === 0 ? <Empty title="还没有评测批次" note="从“新建评测”创建第一组真实测试。" icon={<FlaskConical size={18} />} /> : (
          <div className="max-h-[690px] divide-y divide-edge overflow-y-auto">
            {batches.map((batch) => {
              const selected = selectedBatchId === batch.id;
              const counts = batch.task_counts ?? {};
              const demo = batchIsDemo(batch);
              return (
                <article key={batch.id} className={cls("p-4 transition", selected ? "bg-violet-soft/25" : "hover:bg-paper/70")}>
                  <button type="button" onClick={() => open(batch.id)} className="block w-full text-left">
                    <div className="flex items-start justify-between gap-2"><div className="min-w-0"><div className="flex flex-wrap items-center gap-1.5"><h3 className="truncate text-[10.5px] font-semibold text-ink">{batch.name}</h3>{demo && <span className="rounded bg-amber-50 px-1.5 py-0.5 text-[8px] text-amber-700">DEMO</span>}</div><p className="mt-1 font-mono text-[8px] text-faint">{batch.id.slice(0, 10)} · {relTime(batch.updated_at ?? batch.created_at ?? "")}</p></div><LabStatus status={batch.status} hasWarnings={Boolean(batch.warning_count || batch.completion_quality === "completed_with_warnings")} /></div>
                    <div className="mt-3 flex flex-wrap gap-1.5 text-[8px] text-mute"><span className="rounded bg-paper px-2 py-1">API {batch.profile_ids.length}</span><span className="rounded bg-paper px-2 py-1">完成 {counts.done ?? 0}</span><span className="rounded bg-paper px-2 py-1">运行 {counts.running ?? 0}</span><span className="rounded bg-paper px-2 py-1">失败 {counts.failed ?? 0}</span></div>
                  </button>
                  <div className="mt-3 flex items-center gap-1.5 border-t border-edge/60 pt-3">
                    {["pending", "failed", "cancelled"].includes(batch.status) && <button type="button" onClick={() => void action(batch, "start")} disabled={actionId !== null} className="inline-flex items-center gap-1 rounded-md border border-jade/20 bg-jade-soft px-2 py-1 text-[8.5px] font-medium text-jade">{actionId === batch.id ? <Loader2 size={9} className="animate-spin" /> : <Play size={9} />}启动</button>}
                    {batch.status === "running" && <button type="button" onClick={() => void action(batch, "cancel")} disabled={actionId !== null} className="inline-flex items-center gap-1 rounded-md border border-rise/15 bg-rise/5 px-2 py-1 text-[8.5px] font-medium text-rise"><Square size={9} />取消</button>}
                    <button type="button" onClick={() => open(batch.id)} className="ml-auto inline-flex items-center gap-1 text-[8.5px] font-medium text-violet">查看结果 <ChevronDown size={9} /></button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </section>

      <section className="min-h-[420px] overflow-hidden rounded-xl border border-edge bg-card shadow-card">
        {error && <div className="m-4 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[9.5px] text-rise">{error}</div>}
        {loadingId ? <div className="grid min-h-[420px] place-items-center text-[10px] text-mute"><span className="inline-flex items-center gap-2"><Loader2 size={13} className="animate-spin" />读取预测和问答结果…</span></div> : results ? <ModelLabResultsPanel results={results} profiles={profiles} onUpdated={() => void fetchResults(results.batch.id, false)} /> : <Empty title="选择一个评测批次" note="结果区会显示独立任务进度、API 对比图、明细表与导出。" icon={<BarChart3 size={18} />} />}
      </section>
    </div>
  );
}
