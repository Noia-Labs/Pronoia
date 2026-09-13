import { useEffect, useRef, useState } from "react";
import { CheckCircle2, Loader2, RadioTower } from "lucide-react";

export interface ExternalServiceConfig {
  endpoint: string;
  version: string;
  auth_mode: "bearer" | "header" | "none" | "env";
  api_key: string;
  header_name: string;
  secret_env_ref: string;
  timeout_seconds: number;
}

export const defaultExternalService = (): ExternalServiceConfig => ({
  endpoint: "", version: "", auth_mode: "bearer", api_key: "",
  header_name: "X-API-Key", secret_env_ref: "", timeout_seconds: 30,
});

const input = "mt-1.5 w-full min-w-0 rounded-lg border border-edgeDark/70 bg-card px-3 py-2.5 text-[12px] text-ink outline-none focus:border-violet disabled:opacity-50";
const button = "inline-flex items-center justify-center gap-2 rounded-lg border border-violet/25 bg-card px-3 py-2.5 text-[11px] font-medium text-violet disabled:opacity-50";

export function serviceConfigError(value: ExternalServiceConfig): string | null {
  if (!value.endpoint.trim()) return "请填写预测服务地址";
  if (["bearer", "header"].includes(value.auth_mode) && !value.api_key.trim()) return "请填写预测服务的 API Key，或选择无需认证";
  if ((value.auth_mode === "header" || value.auth_mode === "env") && !value.header_name.trim()) return "请填写认证请求头名称";
  if (value.auth_mode === "env" && !/^PRONOIA_STRATEGY_SECRET_[A-Z0-9_]+$/.test(value.secret_env_ref)) return "请填写以 PRONOIA_STRATEGY_SECRET_ 开头的已配置密钥变量名";
  if (!Number.isInteger(value.timeout_seconds) || value.timeout_seconds < 1 || value.timeout_seconds > 300) return "单次请求超时需为 1–300 秒的整数";
  return null;
}

async function request<T>(url: string, body?: unknown): Promise<T> {
  const response = await fetch(url, body === undefined ? undefined : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) {
    const detail = result.detail;
    throw new Error(typeof detail === "string" ? detail : Array.isArray(detail) ? detail.map((item: { msg?: string }) => item.msg ?? "参数无效").join("；") : "接口请求失败，请检查配置");
  }
  return result as T;
}

export async function saveServiceCredentials(value: ExternalServiceConfig): Promise<Record<string, string>> {
  return (await request<{ headers: Record<string, string> }>("/api/bt/external-service/credentials", value)).headers;
}

type Example = { synthetic: boolean; request: Record<string, unknown>; response: Record<string, unknown> };
type Probe = { ok: boolean; synthetic: boolean; elapsed_seconds: number; prediction: Record<string, unknown> };

export default function ExternalEventServiceEditor({ value, onChange, datasetId, horizon, disabled, allowPrivateEndpoints }: {
  value: ExternalServiceConfig;
  onChange: (value: ExternalServiceConfig) => void;
  datasetId?: string;
  horizon: string;
  disabled?: boolean;
  allowPrivateEndpoints?: boolean;
}) {
  const [example, setExample] = useState<Example | null>(null);
  const [exampleError, setExampleError] = useState("");
  const [busy, setBusy] = useState(false);
  const [probe, setProbe] = useState<Probe | null>(null);
  const [error, setError] = useState("");
  const revision = useRef(0);
  const patch = (next: Partial<ExternalServiceConfig>) => onChange({ ...value, ...next });
  useEffect(() => {
    let alive = true;
    setExample(null); setExampleError("");
    const query = new URLSearchParams({ horizon });
    if (datasetId) query.set("dataset_id", datasetId);
    request<Example>(`/api/bt/external-service/example?${query}`).then((result) => {
      if (alive) setExample(result);
    }).catch((err: Error) => { if (alive) setExampleError(err.message); });
    return () => { alive = false; };
  }, [datasetId, horizon]);
  useEffect(() => { revision.current += 1; setProbe(null); setError(""); }, [value, datasetId, horizon]);
  useEffect(() => () => { revision.current += 1; }, []);
  const test = async () => {
    const id = revision.current;
    setBusy(true); setError(""); setProbe(null);
    try {
      const result = await request<Probe>("/api/bt/external-service/test", { ...value, dataset_id: datasetId || null, horizon });
      if (id === revision.current) setProbe(result);
    } catch (err) { if (id === revision.current) setError(err instanceof Error ? err.message : "试连失败"); }
    finally { setBusy(false); }
  };
  return <div id="create-external-service" tabIndex={-1} className="min-w-0 space-y-4 rounded-xl border border-violet/20 bg-violet-soft/25 p-4 outline-none">
    <div className="flex items-start gap-2.5"><RadioTower size={17} className="mt-0.5 shrink-0 text-violet" /><div>
      <p className="text-[12px] font-semibold text-ink">接入第三方预测服务</p>
      <p className="mt-1 text-[11px] leading-5 text-mute">适用于已经能输出预测结果的自建或第三方模型。填写服务地址和认证信息，平台会发送事件，再评估返回的超额方向及可选资产收益率。Qwen、DeepSeek 等聊天 API 请使用上方“外部大模型（直接预测）”。</p>
    </div></div>
    <fieldset disabled={disabled || busy} className="grid min-w-0 gap-4 sm:grid-cols-2">
      <label className="text-[11px] font-medium sm:col-span-2">预测服务地址 <span className="text-rise">*</span>
        <input id="create-external-endpoint" className={input} placeholder="https://your-service.example/predict" value={value.endpoint} maxLength={4000} onChange={(e) => patch({ endpoint: e.target.value })} />
        <span className="mt-1 block text-[10px] font-normal text-mute">填写服务方提供的完整预测地址，API Key 在下方单独填写。{allowPrivateEndpoints ? "当前可连接本机或私网 HTTP 服务；公网使用 HTTPS。" : "公网地址使用 HTTPS。"}</span>
      </label>
      <label className="text-[11px] font-medium">认证方式
        <select className={input} value={value.auth_mode} onChange={(e) => patch({ auth_mode: e.target.value as ExternalServiceConfig["auth_mode"] })}>
          <option value="bearer">API Key（Bearer 认证）</option><option value="header">API Key（自定义请求头）</option><option value="none">无需认证</option><option value="env">使用已配置的环境变量</option>
        </select>
      </label>
      <label className="text-[11px] font-medium">显示名称 / 版本标签（可选）
        <input className={input} value={value.version} maxLength={80} onChange={(e) => patch({ version: e.target.value })} placeholder="例如：金融预测模型 v1" />
        <span className="mt-1 block text-[10px] font-normal text-mute">仅用于区分运行记录，实际模型版本由该服务决定。</span>
      </label>
      {(value.auth_mode === "header" || value.auth_mode === "env") && <label className="text-[11px] font-medium">认证请求头名称<input className={input} value={value.header_name} onChange={(e) => patch({ header_name: e.target.value })} placeholder="例如：X-API-Key" /><span className="mt-1 block text-[10px] font-normal text-mute">按服务方要求填写，例如 X-API-Key 或 Authorization。</span></label>}
      {value.auth_mode !== "none" && <label className="text-[11px] font-medium">{value.auth_mode === "env" ? "已配置的密钥变量名" : "API Key（服务密钥）"} <span className="text-rise">*</span>
        {value.auth_mode === "env" ? <input className={input} value={value.secret_env_ref} onChange={(e) => patch({ secret_env_ref: e.target.value })} placeholder="PRONOIA_STRATEGY_SECRET_VENDOR" autoComplete="off" /> : <input className={input} type="password" value={value.api_key} onChange={(e) => patch({ api_key: e.target.value })} placeholder="直接粘贴该服务的 API Key" autoComplete="off" spellCheck={false} />}
        <span className="mt-1 block text-[10px] font-normal leading-5 text-mute">{value.auth_mode === "bearer" ? "自动添加 Bearer 前缀；创建实验时在本机加密保存。" : value.auth_mode === "env" ? "变量值原样发送；使用 Authorization 时需自行包含 Bearer 前缀。" : "按原值发送到指定请求头；创建实验时在本机加密保存。"}</span>
      </label>}
      <label className="text-[11px] font-medium">等待单条预测的最长时间（秒）<input className={input} type="number" min={1} max={300} value={value.timeout_seconds} onChange={(e) => patch({ timeout_seconds: Number(e.target.value) })} /><span className="mt-1 block text-[10px] font-normal text-mute">逐条调用、逐条保存。超时或失败后，可在运行记录中重试未完成的事件。</span></label>
    </fieldset>
    <details className="min-w-0 rounded-lg border border-violet/15 bg-card p-3">
      <summary className="cursor-pointer text-[11px] font-medium text-violet">查看接口格式与示例（供服务开发者接入）</summary>
      <p className="mt-3 text-[10px] leading-5 text-mute">使用 POST JSON。direction 仅接受 up（看涨）或 down（看跌），表示扣除市场基准影响后的超额方向。confidence 为 0–1，rationale 填预测理由；horizon 对应当前窗口 {horizon.toUpperCase()}。expected_return_pct 是资产自身收益率预测，填 2 表示资产收益 +2%，不提供时不计算收益率预测误差。示例数值用于说明格式；历史评测要求服务仅使用事件当时已知的信息，平台无法核实服务内部的数据使用。</p>
      {exampleError && <p className="mt-2 break-words text-[11px] text-rise">{exampleError}</p>}
      {example ? <div className="mt-3 grid min-w-0 gap-3 xl:grid-cols-2">{([['请求 JSON', example.request], ['返回 JSON（示例）', example.response]] as const).map(([label, data]) => <div key={label} className="min-w-0"><p className="mb-1 text-[10px] font-medium">{label}</p><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-paper p-3 text-[10px] leading-5">{JSON.stringify(data, null, 2)}</pre></div>)}</div> : !exampleError && <p className="mt-2 text-[11px] text-mute">正在生成示例…</p>}
    </details>
    <div className="flex flex-wrap items-center gap-3"><button type="button" className={button} disabled={disabled || busy || !!serviceConfigError(value) || !example} onClick={test}>{busy ? <Loader2 size={14} className="animate-spin" /> : <RadioTower size={14} />}{busy ? "正在测试一条事件…" : "测试连接与返回格式"}</button><p className="max-w-xl text-[10px] leading-5 text-mute">{example?.synthetic ? "使用一条虚拟事件" : "发送所选事件集的第一条事件"}，真实调用一次服务，检查能否返回符合格式要求的预测。预测准确性需创建实验后评估。</p></div>
    {error && <p role="alert" className="break-words rounded-lg border border-rise/20 bg-rise/5 p-3 text-[11px] leading-5 text-rise">{error}</p>}
    {probe && <div role="status" className="min-w-0 rounded-lg border border-jade/25 bg-jade-soft/30 p-3"><p className="flex items-center gap-2 text-[11px] font-medium text-jade"><CheckCircle2 size={14} />本次调用成功，返回格式符合要求 · {probe.elapsed_seconds} 秒</p><pre className="mt-2 max-h-52 overflow-auto whitespace-pre-wrap break-all text-[10px] leading-5">{JSON.stringify(probe.prediction, null, 2)}</pre></div>}
  </div>;
}
