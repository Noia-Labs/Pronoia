import {
  getModelProviderPreset,
  MODEL_PROVIDER_PRESETS,
  providerLabel,
  type ModelProviderFieldsValue,
} from "../modelProviders";

const INPUT = "mt-1.5 w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[11px] text-ink outline-none focus:border-violet/60 focus:ring-2 focus:ring-violet/10 disabled:opacity-50";

interface ModelProviderFieldsProps {
  value: ModelProviderFieldsValue;
  onChange: (patch: Partial<ModelProviderFieldsValue>) => void;
  disabled?: boolean;
}

export default function ModelProviderFields({ value, onChange, disabled = false }: ModelProviderFieldsProps) {
  const preset = getModelProviderPreset(value.provider);
  const changeProvider = (provider: string) => {
    const nextPreset = getModelProviderPreset(provider);
    if (!nextPreset || provider === "custom") {
      onChange({ provider });
      return;
    }
    onChange({
      provider,
      base_url: nextPreset.baseUrl,
      model_id: "",
      secret_env_ref: nextPreset.secretEnvRef,
    });
  };

  return (
    <>
      <label className="text-[10px] text-mute">
        API 服务商
        <select
          value={value.provider}
          onChange={(event) => changeProvider(event.target.value)}
          disabled={disabled}
          className={INPUT}
        >
          {!preset && <option value={value.provider}>{providerLabel(value.provider)}（已保存）</option>}
          {MODEL_PROVIDER_PRESETS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
        </select>
        <span className="mt-1 block text-[9px] leading-relaxed text-faint">选择向你提供接口和 API Key 的服务商，可预填地址。</span>
      </label>
      <label className="text-[10px] text-mute">
        模型 ID
        <input
          value={value.model_id}
          maxLength={200}
          onChange={(event) => onChange({ model_id: event.target.value })}
          disabled={disabled}
          className={INPUT}
          placeholder={preset?.modelPlaceholder ?? "填写服务商提供的模型 ID"}
          spellCheck={false}
        />
        <span className="mt-1 block text-[9px] leading-relaxed text-faint">填写服务商控制台提供的模型名称或接入点 ID。</span>
      </label>
      <label className="text-[10px] text-mute sm:col-span-2">
        API 地址（Base URL）
        <input
          value={value.base_url}
          maxLength={4000}
          onChange={(event) => onChange({ base_url: event.target.value })}
          disabled={disabled}
          className={INPUT}
          placeholder="https://api.example.com/v1"
          autoCapitalize="none"
          spellCheck={false}
        />
        <span className="mt-1 block text-[9px] leading-relaxed text-faint">
          填写该服务商的模型接口地址，API Key 在下方单独填写。使用代理服务时，请按代理服务商提供的信息填写。
        </span>
      </label>
      <details className="text-[9px] leading-relaxed text-faint sm:col-span-2">
        <summary className="cursor-pointer text-mute">接口格式与地址填写说明</summary>
        <p className="mt-1.5">{preset?.addressHint ?? MODEL_PROVIDER_PRESETS[0].addressHint}</p>
        <p className="mt-1">当前支持 Chat Completions 聊天接口（/chat/completions），可填写基础地址或完整接口地址。服务商选项只是填写预设，实际请求使用你填写的地址和模型 ID。</p>
      </details>
    </>
  );
}
