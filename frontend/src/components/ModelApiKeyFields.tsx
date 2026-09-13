export type ModelCredentialMode = "api_key" | "environment";

export const MODEL_API_KEY_HINT = "直接填写即可，无需修改环境变量。密钥会在本机加密保存；保存不代表连接已验证。";

export function isModelCredentialValid(mode: ModelCredentialMode, apiKey: string, secretEnvRef: string): boolean {
  return mode === "api_key" ? Boolean(apiKey.trim()) : /^PRONOIA_MODEL_SECRET_[A-Z0-9_]+$/.test(secretEnvRef.trim());
}

export function modelCredentialPayload(mode: ModelCredentialMode, apiKey: string, secretEnvRef: string): { api_key: string } | { secret_env_ref: string } {
  return mode === "api_key" ? { api_key: apiKey.trim() } : { secret_env_ref: secretEnvRef.trim() };
}

export function modelCredentialError(reason: unknown, apiKey: string): string {
  const message = reason instanceof Error ? reason.message : String(reason);
  return apiKey.trim() ? message.split(apiKey.trim()).join("[密钥已隐藏]") : message;
}

const INPUT = "mt-1.5 w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[11px] text-ink outline-none focus:border-violet/60 focus:ring-2 focus:ring-violet/10 disabled:opacity-50";

interface ModelApiKeyFieldsProps {
  mode: ModelCredentialMode;
  apiKey: string;
  secretEnvRef: string;
  onModeChange: (mode: ModelCredentialMode) => void;
  onApiKeyChange: (apiKey: string) => void;
  onSecretEnvRefChange: (secretEnvRef: string) => void;
  disabled?: boolean;
}

export default function ModelApiKeyFields({ mode, apiKey, secretEnvRef, onModeChange, onApiKeyChange, onSecretEnvRefChange, disabled = false }: ModelApiKeyFieldsProps) {
  const environmentMode = mode === "environment";
  const changeMode = () => {
    onApiKeyChange("");
    onModeChange(environmentMode ? "api_key" : "environment");
  };

  return (
    <div className="space-y-2">
      {!environmentMode && (
        <label className="block text-[10px] text-mute">
          API Key（服务商密钥）
          <input
            type="password"
            value={apiKey}
            onChange={(event) => onApiKeyChange(event.target.value)}
            disabled={disabled}
            autoComplete="new-password"
            autoCapitalize="none"
            autoCorrect="off"
            spellCheck={false}
            className={INPUT}
            placeholder="在此粘贴提供该接口的服务商 API Key"
          />
          <span className="mt-1 block text-[9px] leading-relaxed text-faint">{MODEL_API_KEY_HINT}</span>
        </label>
      )}
      <button type="button" disabled={disabled} onClick={changeMode} aria-expanded={environmentMode} className="text-[9px] text-mute underline decoration-edgeDark underline-offset-4 disabled:opacity-50">
        {environmentMode ? "返回直接填写 API Key" : "高级：使用已配置的环境变量"}
      </button>
      {environmentMode && (
        <label className="block text-[10px] text-mute">
          已配置的密钥变量名
          <input
            value={secretEnvRef}
            maxLength={256}
            onChange={(event) => onSecretEnvRefChange(event.target.value.toUpperCase())}
            disabled={disabled}
            autoCapitalize="characters"
            spellCheck={false}
            className={INPUT}
            placeholder="PRONOIA_MODEL_SECRET_RESEARCH"
          />
          <span className="mt-1 block text-[9px] leading-relaxed text-faint">
            适用于已在运行 Pronoia 的电脑或服务器上配置密钥的情况。这里填写变量名；直接粘贴密钥请切回 API Key。
            {secretEnvRef && !isModelCredentialValid(mode, apiKey, secretEnvRef) && " 变量名需以 PRONOIA_MODEL_SECRET_ 开头，仅含大写字母、数字和下划线。"}
          </span>
        </label>
      )}
    </div>
  );
}
