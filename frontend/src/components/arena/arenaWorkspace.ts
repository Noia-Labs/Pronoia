import type { ArenaItem } from "../../types";

export type ArenaTrack = "quant" | "event";
export type WorkspaceTargetKind = "asset" | "event" | "event_set";
export type WorkspaceTargetScope = "asset" | "single_event" | "event_set";

export interface WorkspaceModel {
  id: string;
  name: string;
  kind: string;
  track?: ArenaTrack;
  model_id?: string | null;
  available: boolean;
  reason?: string | null;
  description?: string | null;
  supported_target_kinds?: WorkspaceTargetKind[];
  supported_frequencies?: string[];
  compatible_target_ids?: string[];
}

export interface WorkspaceFrequencyOption {
  frequency: string;
  dataset_id?: string | null;
  dataset_version?: string | null;
  snapshot_hash?: string | null;
  name?: string | null;
  start_date?: string | null;
  end_date?: string | null;
  adjustment?: string | null;
  calendar?: string | null;
}

export interface WorkspaceOracleSummary {
  status?: string | null;
  labels_sha256?: string | null;
  available_horizons?: string[];
  return_unit?: string | null;
  truth_basis?: string | null;
  reason?: string | null;
}

export interface WorkspaceTarget {
  id: string;
  kind: WorkspaceTargetKind;
  track?: ArenaTrack;
  target_scope?: WorkspaceTargetScope;
  name: string;
  symbol: string;
  market: string;
  start_date?: string | null;
  end_date?: string | null;
  event_time?: string | null;
  frequency?: string | null;
  default_frequency?: string | null;
  available_frequencies?: Array<string | WorkspaceFrequencyOption>;
  supported_frequencies?: string[];
  event_count?: number | null;
  total_events?: number | null;
  snapshot_hash?: string | null;
  labels_snapshot_hash?: string | null;
  label_count?: number | null;
  event_start_date?: string | null;
  event_end_date?: string | null;
  dataset_id?: string | null;
  dataset_version?: string | null;
  oracle?: WorkspaceOracleSummary | null;
  available?: boolean;
  reason?: string | null;
}

export interface WorkspaceCatalog {
  tracks?: Array<{ id: ArenaTrack; target_scopes: WorkspaceTargetScope[]; frequencies?: string[] }>;
  models: WorkspaceModel[];
  targets: WorkspaceTarget[];
}
export interface WorkspaceRequest {
  track: ArenaTrack;
  model_ids: string[];
  target_id: string;
  target_scope: WorkspaceTargetScope;
  frequency?: string;
  start_date: string;
  end_date: string;
  name?: string;
  holding_bars: number;
  fee_bps: number;
  slippage_bps: number;
  initial_capital: number;
  request_id: string;
}

export function workspaceModelTrack(model: Pick<WorkspaceModel, "kind" | "track">): ArenaTrack {
  if (model.track === "quant" || model.track === "event") return model.track;
  return model.kind === "quant" ? "quant" : "event";
}

export function workspaceTargetScope(target: Pick<WorkspaceTarget, "kind" | "target_scope">): WorkspaceTargetScope {
  if (target.target_scope) return target.target_scope;
  if (target.kind === "event_set") return "event_set";
  return target.kind === "event" ? "single_event" : "asset";
}

export function workspaceTargetKindsForModel(target: Pick<WorkspaceTarget, "kind">): WorkspaceTargetKind[] {
  // Existing saved event models advertise `event`; an event set runs that same
  // contract repeatedly, so keep the legacy capability compatible.
  return target.kind === "event_set" ? ["event_set", "event"] : [target.kind];
}

export function workspaceTargetAvailabilityReason(
  target: Pick<WorkspaceTarget, "available" | "reason">,
): string {
  return target.available === false ? target.reason || "该目标的冻结数据或 Oracle 当前不可用。" : "";
}

export function workspaceFrequencyOptions(
  target: Pick<WorkspaceTarget, "available_frequencies" | "frequency" | "default_frequency" | "start_date" | "end_date">,
): WorkspaceFrequencyOption[] {
  const options = (target.available_frequencies ?? []).map((item) => (
    typeof item === "string" ? { frequency: item } : item
  ));
  if (target.frequency && !options.some((item) => item.frequency === target.frequency)) {
    options.push({ frequency: target.frequency, start_date: target.start_date, end_date: target.end_date });
  }
  if (target.default_frequency && !options.some((item) => item.frequency === target.default_frequency)) {
    options.push({ frequency: target.default_frequency, start_date: target.start_date, end_date: target.end_date });
  }
  return options.filter((item, index) => Boolean(item.frequency)
    && options.findIndex((candidate) => candidate.frequency === item.frequency) === index);
}
export interface WorkspaceModelProgress {
  id: string;
  name: string;
  status: string;
  done?: number;
  total?: number;
  error?: string | null;
}
export interface WorkspaceProgress {
  stage?: string;
  message?: string;
  models?: WorkspaceModelProgress[];
}
export interface WorkspaceConfig {
  track?: ArenaTrack;
  target_scope?: WorkspaceTargetScope;
  frequency?: string;
  model_ids?: string[];
  models?: Array<Partial<WorkspaceModel>>;
  target_id?: string;
  target?: Partial<WorkspaceTarget>;
  start_date?: string;
  end_date?: string;
  holding_bars?: number;
  fee_bps?: number;
  slippage_bps?: number;
  initial_capital?: number;
  rules?: { holding_bars?: number; fee_bps?: number; slippage_bps?: number; initial_capital?: number };
}

export function isArenaWorkspace(arena: ArenaItem): boolean {
  return Boolean(arena.config?.arena_workspace && typeof arena.config.arena_workspace === "object");
}
export function workspaceConfig(arena: ArenaItem): WorkspaceConfig {
  return isArenaWorkspace(arena) ? arena.config!.arena_workspace as WorkspaceConfig : {};
}
export function workspaceProgress(arena: ArenaItem): WorkspaceProgress {
  const value = arena.config?.progress;
  return value && typeof value === "object" ? value as WorkspaceProgress : {};
}
export const workspaceIsRunning = (status: string) => ["pending", "running", "ready", "computing", "cancelling"].includes(status);
export const workspaceStatusLabel = (status: string) => ({ pending: "等待开始", ready: "等待开始", running: "比较中", computing: "计算中", done: "已完成", partial: "部分完成", failed: "未完成", cancelled: "已取消", cancelling: "正在取消", skipped: "已跳过" } as Record<string, string>)[status] ?? status;

function errorMessage(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map(errorMessage).join("；");
  if (detail && typeof detail === "object") {
    const record = detail as Record<string, unknown>;
    return [record.message, record.msg, record.reason, record.hint].filter((part): part is string => typeof part === "string" && Boolean(part)).join("；") || "请求未能完成，请重试。";
  }
  return "请求未能完成，请重试。";
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/arena/workspace${path}`, init);
  const type = response.headers.get("content-type") ?? "";
  if (path === "/catalog" && (response.status === 404 || type.includes("text/html"))) {
    throw new Error("Arena 服务尚未加载当前版本，请重启 Pronoia 服务后重新读取。");
  }
  if (!type.includes("json")) throw new Error("Arena 服务暂时不可用，请稍后重试。");
  const body = await response.json();
  if (!response.ok) throw new Error(errorMessage(body?.detail ?? body));
  return body as T;
}

export function newWorkspaceRequestId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (value) => value.toString(16).padStart(2, "0")).join("");
}
export const arenaWorkspaceApi = {
  catalog: (signal?: AbortSignal) => request<WorkspaceCatalog>("/catalog", { signal }),
  create: (payload: WorkspaceRequest) => request<ArenaItem>("", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }),
  get: (id: string, signal?: AbortSignal) => request<ArenaItem>(`/${encodeURIComponent(id)}`, { signal }),
  cancel: (id: string) => request<ArenaItem>(`/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
};
