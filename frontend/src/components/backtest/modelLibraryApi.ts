import type { BTStrategySpec } from "../../types";

export type ModelCategory = "event" | "quant";
export interface SavedModelSetup {
  saved_model_id: string;
  category: ModelCategory;
  kind: string;
  runner: string;
  profile_id?: string | null;
  strategy_spec?: BTStrategySpec | null;
}
export interface SavedModelEntry {
  save_disposition?: "created" | "existing" | "renamed" | "restored";
  id: string;
  name: string;
  kind: string;
  category: ModelCategory;
  available: boolean;
  can_test?: boolean;
  reason?: string | null;
  description?: string | null;
  run_count: number;
  run_ids: string[];
  batch_ids?: string[];
  latest_run?: { id: string; name: string; status: string; created_at: string } | null;
  created_at?: string;
  setup: SavedModelSetup;
}
export interface SaveModelInput {
  model_identity?: string;
  name: string;
  category: ModelCategory;
  kind?: string;
  runner?: string;
  profile_id?: string | null;
  strategy_spec?: BTStrategySpec;
}
function message(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(message).join("；");
  if (detail && typeof detail === "object") { const value = detail as Record<string, unknown>; return [value.msg, value.message, value.reason].filter((part) => typeof part === "string").join("；") || "模型操作未成功，请检查配置。"; }
  return "模型操作未成功，请重试。";
}
async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/bt/models${path}`, init);
  if (!(response.headers.get("content-type") ?? "").includes("json")) throw new Error("模型列表服务尚未就绪，请刷新页面后重试。");
  const data = await response.json();
  if (!response.ok) throw new Error(response.status === 404 && path.startsWith("?") ? "模型列表接口尚未启用，请重启 Pronoia 服务。" : message(data.detail ?? data));
  return data as T;
}
export const modelLibraryApi = {
  list: (category?: ModelCategory, signal?: AbortSignal) => request<{ items: SavedModelEntry[] }>(category ? `?category=${category}` : "", { signal }),
  match: (input: SaveModelInput, signal?: AbortSignal) => request<{ model: SavedModelEntry | null }>("/match", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input), signal }),
  create: (input: SaveModelInput) => request<SavedModelEntry>("", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) }),
  setup: (id: string) => request<SavedModelSetup>(`/${encodeURIComponent(id)}/setup`),
  rename: (id: string, name: string) => request<SavedModelEntry>(`/${encodeURIComponent(id)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ name }) }),
  remove: (modelIds: string[]) => request<{ deleted_ids: string[] }>("/delete", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model_ids: modelIds }) }),
  configuration: (id: string) => request<SavedModelEntry>(`/${encodeURIComponent(id)}/configuration`),
  updateConfiguration: (id: string, input: { name: string; strategy_spec?: BTStrategySpec }) => request<SavedModelEntry>(`/${encodeURIComponent(id)}/configuration`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(input) }),
};
export const modelKindLabel = (kind: string) => ({ pronoia: "Pronoia · 多 Agent", external_model: "独立大模型", raw_model: "独立大模型", external_service: "第三方预测服务", imported_predictions: "导入预测", quant: "量化模型" } as Record<string, string>)[kind] || kind;
