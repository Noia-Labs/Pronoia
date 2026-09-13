export interface ModelProviderFieldsValue {
  provider: string;
  base_url: string;
  model_id: string;
  secret_env_ref: string;
}

export interface ModelProviderPreset {
  id: string;
  label: string;
  baseUrl: string;
  secretEnvRef: string;
  modelPlaceholder: string;
  addressHint: string;
}

export const MODEL_PROVIDER_PRESETS: readonly ModelProviderPreset[] = [
  {
    id: "custom",
    label: "自定义 / 其他服务商",
    baseUrl: "",
    secretEnvRef: "",
    modelPlaceholder: "填写服务商控制台提供的模型 ID",
    addressHint: "可填写聊天 API 的基础地址或完整 /chat/completions 地址；地址是否允许访问由服务端连接策略校验。",
  },
  {
    id: "qwen",
    label: "Qwen / 阿里云百炼",
    baseUrl: "",
    secretEnvRef: "PRONOIA_MODEL_SECRET_QWEN",
    modelPlaceholder: "填写百炼控制台提供的模型 ID",
    addressHint: "请填写百炼北京地域地址；其他地域或代理服务请修改地址，并使用匹配的 API Key。",
  },
  {
    id: "deepseek",
    label: "DeepSeek",
    baseUrl: "",
    secretEnvRef: "PRONOIA_MODEL_SECRET_DEEPSEEK",
    modelPlaceholder: "填写服务商控制台提供的模型 ID",
    addressHint: "请填写 DeepSeek 官方地址；使用其他服务商提供的 DeepSeek 模型时，请修改地址和模型 ID。",
  },
  {
    id: "doubao",
    label: "豆包 / 火山方舟",
    baseUrl: "",
    secretEnvRef: "PRONOIA_MODEL_SECRET_DOUBAO",
    modelPlaceholder: "填写模型 ID 或推理接入点 ID",
    addressHint: "请填写火山方舟北京地域地址；模型 ID、接入点和 API Key 需与所选服务及地域匹配。",
  },
  {
    id: "kimi",
    label: "Kimi / Moonshot",
    baseUrl: "",
    secretEnvRef: "PRONOIA_MODEL_SECRET_KIMI",
    modelPlaceholder: "填写 Moonshot 控制台提供的模型 ID",
    addressHint: "请填写 Moonshot 中国区地址；其他区域或代理服务请修改地址，并使用匹配的 API Key。",
  },
];

export function getModelProviderPreset(provider: string): ModelProviderPreset | undefined {
  return MODEL_PROVIDER_PRESETS.find((preset) => preset.id === provider.trim().toLowerCase());
}

export function providerLabel(provider: string | null | undefined): string {
  const normalized = provider?.trim().toLowerCase() ?? "";
  if (!normalized) return "未标注服务商";
  if (["openai", "openai-compatible", "openai_compatible", "compatible", "chat-completions"].includes(normalized)) {
    return "通用聊天接口";
  }
  return getModelProviderPreset(normalized)?.label ?? provider!.trim();
}
