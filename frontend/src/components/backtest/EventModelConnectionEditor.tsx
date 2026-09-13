import { useRef, useState } from "react";
import { Loader2, Plus, Save, X } from "lucide-react";
import { api } from "../../api";
import type { ModelLabProfile } from "../../types";
import type { ModelProviderFieldsValue } from "../../modelProviders";
import {
  DEFAULT_MAX_OUTPUT_TOKENS,
  DEFAULT_MODEL_TIMEOUT_SECONDS,
  MAX_MAX_OUTPUT_TOKENS,
  MIN_MAX_OUTPUT_TOKENS,
  OUTPUT_TOKEN_LIMIT_HINT,
  parseOutputTokenLimit,
} from "../../modelDefaults";
import ModelProviderFields from "../ModelProviderFields";
import ModelApiKeyFields, { isModelCredentialValid, modelCredentialError, modelCredentialPayload, type ModelCredentialMode } from "../ModelApiKeyFields";

const INPUT = "mt-1.5 w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[11px] text-ink outline-none focus:border-violet/60 focus:ring-2 focus:ring-violet/10";
const EMPTY_CONNECTION: ModelProviderFieldsValue = {
  provider: "custom",
  base_url: "",
  model_id: "",
  secret_env_ref: "",
};

interface EventModelConnectionEditorProps {
  onCreated: (profile: ModelLabProfile) => void;
  disabled?: boolean;
}

export default function EventModelConnectionEditor({ onCreated, disabled = false }: EventModelConnectionEditorProps) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [connection, setConnection] = useState<ModelProviderFieldsValue>({ ...EMPTY_CONNECTION });
  const [apiKey, setApiKey] = useState("");
  const [credentialMode, setCredentialMode] = useState<ModelCredentialMode>("api_key");
  const [maxOutputTokens, setMaxOutputTokens] = useState(String(DEFAULT_MAX_OUTPUT_TOKENS));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const lock = useRef(false);
  const secretValid = isModelCredentialValid(credentialMode, apiKey, connection.secret_env_ref);
  const tokenLimit = parseOutputTokenLimit(maxOutputTokens);
  const valid = Boolean(name.trim() && connection.base_url.trim() && connection.model_id.trim() && secretValid && tokenLimit !== null);

  const save = async () => {
    if (lock.current || disabled || !valid || tokenLimit === null) return;
    lock.current = true;
    setSaving(true);
    setError(null);
    try {
      const result = await api.modelLabCreateProfile({
        name: name.trim(),
        provider: connection.provider,
        base_url: connection.base_url.trim(),
        model_id: connection.model_id.trim(),
        ...modelCredentialPayload(credentialMode, apiKey, connection.secret_env_ref),
        max_output_tokens: tokenLimit,
        timeout_seconds: DEFAULT_MODEL_TIMEOUT_SECONDS,
        thinking_mode: "auto",
        input_price_per_million: 0,
        output_price_per_million: 0,
        currency: "CNY",
        is_active: true,
      });
      setApiKey("");
      setCredentialMode("api_key");
      onCreated(result);
      setOpen(false);
      setName("");
      setConnection({ ...EMPTY_CONNECTION });
      setMaxOutputTokens(String(DEFAULT_MAX_OUTPUT_TOKENS));
    } catch (reason) {
      setError(modelCredentialError(reason, apiKey));
    } finally {
      lock.current = false;
      setSaving(false);
    }
  };

  if (!open) {
    return (
      <button
        type="button"
        disabled={disabled}
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 text-[10px] font-medium text-violet disabled:opacity-40"
      >
        <Plus size={11} />添加外部大模型连接
      </button>
    );
  }

  return (
    <div className="rounded-xl border border-violet/20 bg-violet-soft/25 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-[12px] font-semibold text-ink">添加外部大模型连接</p>
          <p className="mt-1 text-[10px] leading-relaxed text-mute">填写 Qwen、DeepSeek 等模型的接口地址、模型 ID 和 API Key。保存后，可在事件预测、问答或评分中选择这条连接；保存时不会调用模型或验证连接。</p>
        </div>
        <button type="button" disabled={saving || disabled} onClick={() => { setApiKey(""); setError(null); setOpen(false); }} aria-label="关闭新增模型连接">
          <X size={13} className="text-mute" />
        </button>
      </div>
      <fieldset disabled={saving || disabled} className="mt-4 grid gap-3 sm:grid-cols-2">
        <label className="text-[10px] text-mute sm:col-span-2">
          连接显示名称
          <input value={name} maxLength={120} onChange={(event) => setName(event.target.value)} className={INPUT} placeholder="例如：Qwen 事件测试" />
        </label>
        <ModelProviderFields
          value={connection}
          onChange={(patch) => {
            if (patch.provider !== undefined && patch.provider !== connection.provider) {
              setApiKey("");
              setError(null);
            }
            setConnection((current) => ({ ...current, ...patch }));
          }}
          disabled={saving || disabled}
        />
        <div className="sm:col-span-2">
          <ModelApiKeyFields
            mode={credentialMode}
            apiKey={apiKey}
            secretEnvRef={connection.secret_env_ref}
            onModeChange={setCredentialMode}
            onApiKeyChange={setApiKey}
            onSecretEnvRefChange={(secretEnvRef) => setConnection((current) => ({ ...current, secret_env_ref: secretEnvRef }))}
            disabled={saving || disabled}
          />
        </div>
        <label className="text-[10px] text-mute sm:col-span-2">
          单次模型输出上限（tokens）
          <input
            type="number"
            min={MIN_MAX_OUTPUT_TOKENS}
            max={MAX_MAX_OUTPUT_TOKENS}
            step={1}
            value={maxOutputTokens}
            onChange={(event) => setMaxOutputTokens(event.target.value)}
            disabled={saving || disabled}
            aria-invalid={tokenLimit === null}
            className={INPUT}
          />
          <span className="mt-1 block text-[9px] leading-relaxed text-faint">
            默认 {DEFAULT_MAX_OUTPUT_TOKENS}，可填 {MIN_MAX_OUTPUT_TOKENS}–{MAX_MAX_OUTPUT_TOKENS} 的整数。{OUTPUT_TOKEN_LIMIT_HINT}
          </span>
          {tokenLimit === null && <span className="mt-1 block text-[9px] text-rise">请输入 {MIN_MAX_OUTPUT_TOKENS}–{MAX_MAX_OUTPUT_TOKENS} 之间的整数。</span>}
        </label>
      </fieldset>
      {error && <p role="alert" className="mt-3 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2 text-[10px] text-rise">{error}</p>}
      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={() => void save()}
          disabled={disabled || saving || !valid}
          className="inline-flex items-center gap-1.5 rounded-lg bg-violet px-3 py-2 text-[10px] font-semibold text-white disabled:opacity-40"
        >
          {saving ? <Loader2 size={11} className="animate-spin" /> : <Save size={11} />}
          {saving ? "保存中…" : "保存模型连接"}
        </button>
      </div>
    </div>
  );
}
