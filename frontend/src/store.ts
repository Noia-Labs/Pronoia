import { create } from "zustand";
import { api, streamChat, StreamAbortedError } from "./api";
import type {
  AgentMeta,
  ArenaItem,
  Artifact,
  ArtifactKind,
  BTRun,
  CaseItem,
  HistoryMessage,
  LogicCheckEntry,
  LogicItem,
  LogEntry,
  Message,
  Mode,
  Part,
  PlanItem,
  RightTab,
  SkillMeta,
  SSEEvent,
} from "./types";
import { uid } from "./utils";

/* ---------------- logic library 持久化 ---------------- */

const LOGIC_KEY = "fever.logic_library.v1";
const UI_KEY = "fever.ui.v1";

interface UIPrefs {
  rightOpen?: boolean;
  leftOpen?: boolean;
  rightTab?: RightTab;
  mode?: Mode;
  selectedAgent?: string;
  /** team 模式时调度的专家白名单（不含 deep_researcher，硬规则） */
  teamMembers?: string[];
  teamMembersSet?: boolean;
}

function loadUIPrefs(): UIPrefs {
  try {
    const raw = localStorage.getItem(UI_KEY);
    if (!raw) return {};
    return JSON.parse(raw) as UIPrefs;
  } catch {
    return {};
  }
}

function saveUIPrefs(p: UIPrefs) {
  try {
    localStorage.setItem(UI_KEY, JSON.stringify(p));
  } catch {
    /* quota / private mode — silently ignore */
  }
}

function loadLogicLibrary(): LogicItem[] {
  try {
    const raw = localStorage.getItem(LOGIC_KEY);
    if (!raw) return [];
    const j = JSON.parse(raw) as LogicItem[];
    return Array.isArray(j) ? j : [];
  } catch {
    return [];
  }
}

function saveLogicLibrary(items: LogicItem[]) {
  try {
    localStorage.setItem(LOGIC_KEY, JSON.stringify(items));
  } catch {
    /* ignore quota errors */
  }
}

/* ---------------- parts 工具 ---------------- */

/** thinking / token delta：与上一同类型同 agent part 合并追加 */
function appendDelta(parts: Part[], type: "thinking" | "text", agent: string | undefined, delta: string): Part[] {
  const last = parts[parts.length - 1];
  if (last && last.type === type && last.agent === agent) {
    return [...parts.slice(0, -1), { ...last, text: last.text + delta }];
  }
  return [...parts, { type, agent, text: delta } as Part];
}

function sortArtifacts(xs: Artifact[]): Artifact[] {
  return [...xs].sort((a, b) => {
    const p = (b.pinned ?? 0) - (a.pinned ?? 0);
    if (p !== 0) return p;
    return (b.created_at ?? "").localeCompare(a.created_at ?? "");
  });
}

function sortCases(xs: CaseItem[]): CaseItem[] {
  return [...xs].sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
}

function firstArtifactId(it: Record<string, unknown>): string | undefined {
  if (Array.isArray(it.artifact_ids) && it.artifact_ids.length > 0) return String(it.artifact_ids[0]);
  if (it.artifact_id) return String(it.artifact_id);
  if (it.artifactId) return String(it.artifactId);
  return undefined;
}

/**
 * 历史消息重渲染：把后端落库的 tool_trace 还原成 parts 卡片流。
 * 后端形态（见 backend/app/llm.py · agents/team.py）：
 *   {"type":"tool", agent, id, skill, args, ok, preview, artifact_ids[]}
 *   {"type":"plan", plan:[{agent, task, agent_name}]}
 *   {"type":"agent_findings", agent, findings}
 *   {"type":"verify", verdict, issues[], corrected}
 * 同时兼容 tool_call / tool_result / artifact / thinking / token / agent_step 条目。
 */
export function partsFromHistory(m: HistoryMessage): Part[] {
  const parts: Part[] = [];
  let trace: unknown = m.tool_trace ?? null;
  if (typeof trace === "string") {
    try {
      trace = JSON.parse(trace);
    } catch {
      trace = null;
    }
  }
  const list: unknown[] = Array.isArray(trace)
    ? trace
    : trace && typeof trace === "object" && Array.isArray((trace as { parts?: unknown[] }).parts)
      ? (trace as { parts: unknown[] }).parts
      : [];

  for (const raw of list) {
    if (!raw || typeof raw !== "object") continue;
    const it = raw as Record<string, unknown>;
    const t = String(it.type ?? "");
    const agent = (it.agent as string) ?? undefined;

    if (t === "tool" || t === "tool_call" || t === "tool_result" || (!t && (it.skill || it.name))) {
      const ok = it.ok !== false && it.status !== "error";
      const artifactId = firstArtifactId(it);
      if (t === "tool_result") {
        const id = String(it.id ?? "");
        const idx = parts.findIndex((p) => p.type === "tool_call" && p.id === id);
        if (idx >= 0) {
          const p = parts[idx] as Extract<Part, { type: "tool_call" }>;
          parts[idx] = {
            ...p,
            status: ok ? "done" : "error",
            preview: (it.preview as string) ?? p.preview,
            artifactId: artifactId ?? p.artifactId,
          };
          continue;
        }
      }
      parts.push({
        type: "tool_call",
        id: String(it.id ?? uid()),
        agent,
        skill: String(it.skill ?? it.name ?? it.tool ?? "tool"),
        args: (it.args ?? it.arguments) as Record<string, unknown> | undefined,
        status: ok ? "done" : "error",
        preview: (it.preview ?? it.result_preview) as string | undefined,
        artifactId,
      });
    } else if (t === "thinking" || t === "thought") {
      const text = String(it.text ?? it.delta ?? it.content ?? "");
      if (text) parts.push({ type: "thinking", agent, text });
    } else if (t === "plan") {
      parts.push({ type: "agent_step", phase: "plan", plan: (it.plan as PlanItem[]) ?? [] });
    } else if (t === "agent_findings") {
      const findings = String(it.findings ?? "");
      parts.push({
        type: "agent_step",
        phase: "agent_done",
        agent,
        note: findings.length > 200 ? findings.slice(0, 200) + "…" : findings,
      });
    } else if (t === "verify") {
      const issues = (it.issues as string[]) ?? [];
      parts.push({
        type: "agent_step",
        phase: "verified",
        agent: agent ?? "verifier",
        verdict: String(it.verdict ?? ""),
        note: issues.length > 0 ? issues.join("；") : "未发现事实性错误",
      });
    } else if (t === "artifact") {
      const artifactId = firstArtifactId(it) ?? String(it.id ?? "");
      if (artifactId) {
        parts.push({
          type: "artifact",
          agent,
          artifactId,
          kind: (it.kind ?? "table") as ArtifactKind,
          title: String(it.title ?? "产出物"),
        });
      }
    } else if (t === "logic_items") {
      const rawItems = (it.items as Array<Record<string, unknown>>) ?? [];
      if (rawItems.length > 0) {
        const items: LogicItem[] = rawItems.map((x) => ({
          id: String(x.id ?? uid()),
          case_id: (x.case_id as string | null | undefined) ?? null,
          message_id: (x.message_id as string | null | undefined) ?? null,
          question: (x.question as string | undefined) ?? "",
          hypothesis: String(x.hypothesis ?? ""),
          category: String(x.category ?? ""),
          probability: String(x.probability ?? ""),
          scope: String(x.scope ?? ""),
          horizon: String(x.horizon ?? ""),
          check: String(x.check ?? ""),
          status: ((x.status as LogicItem["status"]) ?? "pending"),
          created_at: String(x.created_at ?? new Date().toISOString()),
          verified_at: (x.verified_at as string | null | undefined) ?? null,
          verification_note: (x.verification_note as string | undefined) ?? "",
        }));
        parts.push({ type: "logic_items", items });
      }
    } else if (t === "token" || t === "text") {
      const text = String(it.text ?? it.delta ?? it.content ?? "");
      if (text) parts.push({ type: "text", agent, text });
    } else if (t === "agent_step") {
      parts.push({
        type: "agent_step",
        phase: String(it.phase ?? ""),
        agent,
        note: it.note as string | undefined,
        plan: it.plan as PlanItem[] | undefined,
        verdict: it.verdict as string | undefined,
        simulationJobId: it.simulation_job_id as string | undefined,
      });
    }
  }

  if (m.content) {
    const last = parts[parts.length - 1];
    if (!(last?.type === "text" && last.text === m.content)) {
      parts.push({ type: "text", agent: m.agent ?? undefined, text: m.content });
    }
  }
  return parts;
}

/* ---------------- store ---------------- */

/**
 * 页面路由的 store 表示。`backtest-list` 保留为旧代码兼容别名，
 * setView 会将它统一导航到回测首页“运行记录”。
 */
export type ViewName =
  | "chat"
  | "backtest-event"
  | "backtest-quant"
  | "backtest-runs"
  | "backtest-list"
  | "backtest-model-lab"
  | "backtest-data"
  | "backtest-detail"
  | "arena-list"
  | "arena-detail"
  | "prospective-list"
  | "prospective-detail";

interface RouteSnapshot {
  view: Exclude<ViewName, "backtest-list">;
  currentBTRunId: string | null;
  currentArenaId: string | null;
  currentProspectiveRunId: string | null;
}

function routeFromLocation(): RouteSnapshot {
  if (typeof window === "undefined") {
    return {
      view: "chat",
      currentBTRunId: null,
      currentArenaId: null,
      currentProspectiveRunId: null,
    };
  }
  const path = window.location.pathname.replace(/\/+$/, "") || "/";
  const btDetail = path.match(/^\/backtest\/runs\/([^/]+)$/);
  if (btDetail) {
    return {
      view: "backtest-detail",
      currentBTRunId: decodeURIComponent(btDetail[1]),
      currentArenaId: null,
      currentProspectiveRunId: null,
    };
  }
  const arenaDetail = path.match(/^\/arena\/([^/]+)$/);
  if (arenaDetail) {
    return {
      view: "arena-detail",
      currentBTRunId: null,
      currentArenaId: decodeURIComponent(arenaDetail[1]),
      currentProspectiveRunId: null,
    };
  }
  const prospectiveDetail = path.match(/^\/prospective\/([^/]+)$/);
  if (prospectiveDetail) {
    return {
      view: "prospective-detail",
      currentBTRunId: null,
      currentArenaId: null,
      currentProspectiveRunId: decodeURIComponent(prospectiveDetail[1]),
    };
  }
  const views: Record<string, RouteSnapshot["view"]> = {
    "/backtest/event": "backtest-event",
    "/backtest/quant": "backtest-quant",
    "/backtest/runs": "backtest-runs",
    "/backtest/model-lab": "backtest-model-lab",
    "/backtest/data": "backtest-data",
    "/backtest": "backtest-runs",
    "/arena": "arena-list",
    "/prospective": "prospective-list",
  };
  return {
    view: views[path] ?? "chat",
    currentBTRunId: null,
    currentArenaId: null,
    currentProspectiveRunId: null,
  };
}

function pathForView(
  view: ViewName,
  state: Pick<RouteSnapshot, "currentBTRunId" | "currentArenaId" | "currentProspectiveRunId">,
): string {
  const normalized = view === "backtest-list" ? "backtest-runs" : view;
  const paths: Partial<Record<ViewName, string>> = {
    chat: "/",
    "backtest-event": "/backtest/event",
    "backtest-quant": "/backtest/quant",
    "backtest-runs": "/backtest/runs",
    "backtest-model-lab": "/backtest/model-lab",
    "backtest-data": "/backtest/data",
    "arena-list": "/arena",
    "prospective-list": "/prospective",
  };
  if (normalized === "backtest-detail" && state.currentBTRunId) {
    return `/backtest/runs/${encodeURIComponent(state.currentBTRunId)}`;
  }
  if (normalized === "arena-detail" && state.currentArenaId) {
    return `/arena/${encodeURIComponent(state.currentArenaId)}`;
  }
  if (normalized === "prospective-detail" && state.currentProspectiveRunId) {
    return `/prospective/${encodeURIComponent(state.currentProspectiveRunId)}`;
  }
  return paths[normalized] ?? "/";
}

function pushRoute(path: string, replace = false) {
  if (typeof window === "undefined") return;
  if (window.location.pathname === path) return;
  window.history[replace ? "replaceState" : "pushState"]({}, "", path);
}
interface FeverState {
  cases: CaseItem[];
  currentCaseId: string | null;
  messages: Message[];
  artifacts: Artifact[];
  skills: SkillMeta[];
  agents: AgentMeta[];
  rightTab: RightTab;
  rightOpen: boolean;
  leftOpen: boolean;
  selectedArtifactId: string | null;
  streaming: boolean;
  mode: Mode;
  /** mode="agent" 时选定要直接调用的 Agent id（predictor / market_analyst / event_scout ...） */
  selectedAgent: string;
  /** team 模式时可调度的专家白名单（不含 deep_researcher，硬规则）；
   *  空数组 = 仅 deep_researcher 跑（"只留深度研究"）。 */
  teamMembers: string[];
  teamMembersSet: boolean;
  loadingCase: boolean;
  generatingReport: boolean;
  initialized: boolean;

  /** 研究逻辑库（design.md §6.4） */
  logicLibrary: LogicItem[];
  /** 右栏：是否显示逻辑库浮层（独立于 artifacts/skills/team） */
  logicLibOpen: boolean;

  /* ===================================== Backtest (P0) ===================================== */

  /** 当前页：chat 工作台 / 回测列表 / 回测详情 / Arena 列表 / Arena 详情 */
  view: ViewName;
  /** 回测详情页当前 run_id（view=backtest-detail 时使用） */
  currentBTRunId: string | null;
  /** 回测列表页缓存（懒加载） */
  btRuns: BTRun[];
  btRunsLoading: boolean;

  /* ===================================== Arena ===================================== */

  /** Arena 列表缓存 */
  arenaItems: ArenaItem[];
  arenaLoading: boolean;
  /** Arena 详情页当前 arena_id */
  currentArenaId: string | null;
  currentProspectiveRunId: string | null;

  init: () => Promise<void>;
  sendMessage: (text: string, mode?: Mode, agent?: string) => Promise<void>;
  stop: () => void;
  retryLastMessage: () => Promise<void>;
  loadCase: (id: string) => Promise<void>;
  newCase: () => void;
  deleteCase: (id: string) => Promise<void>;
  pinArtifact: (artifactId: string) => Promise<void>;
  genReport: () => Promise<void>;
  selectArtifact: (id: string | null) => void;
  setRightTab: (t: RightTab) => void;
  setRightOpen: (v: boolean) => void;
  setLeftOpen: (v: boolean) => void;
  setMode: (m: Mode) => void;
  setSelectedAgent: (id: string) => void;
  setTeamMembers: (ids: string[]) => void;
  /** 由 chip / hot topic 触发的 prompt 种子；Composer 监听变化后填到 textarea 并清空 */
  promptSeed: string;
  setPromptSeed: (s: string) => void;

  /** 库操作：新增/更新/忽略 */
  addLogicItems: (items: LogicItem[]) => void;
  updateLogicItem: (id: string, patch: Partial<LogicItem>) => void;
  dismissLogicItem: (id: string) => void;
  /** 深度验证（调后端 /api/logic/auto_check，自动入档） */
  autoCheckLogic: (id: string) => Promise<LogicItem | null>;
  /** 把一条 check entry 追加到 check_history（用于手动标记） */
  markLogicCheck: (id: string, status: LogicItem["status"], note?: string) => void;
  /** 重新追踪：以某条 logic 为种子开启新研究 */
  reverifyLogic: (item: LogicItem) => void;
  setLogicLibOpen: (v: boolean) => void;
  /** 正在被深度验证的 logic id（用于 UI loading 态） */
  logicChecking: Set<string>;

  /* ===================================== Backend status / Live Log (debug) ===================================== */

  /** 后端运行状态：每 5 秒轮询 /api/health；online=绿点 offline=红点 */
  backendStatus: "online" | "offline";
  /** LLM 调用日志（来自 EventSource /api/admin/live-log，最多 200 条，最新在前） */
  liveLogs: LogEntry[];
  /** 底部浮动日志面板是否展开 */
  liveLogOpen: boolean;
  /** 切换日志面板开合（开时建立 EventSource 连接，关时断开） */
  toggleLiveLog: () => void;
  /** 清空已收集的日志 */
  clearLiveLogs: () => void;

  /* ---- Backtest 方法 ---- */
  setView: (v: ViewName) => void;
  /** 响应 popstate，只同步 store，不再写入 history。 */
  syncRouteFromLocation: () => void;
  /** 进入某个回测详情页（自动 setView("backtest-detail")） */
  openBTDetail: (runId: string) => void;
  /** 返回回测列表或 chat 工作台 */
  backFromBTDetail: () => void;
  /** 拉取回测列表（强制刷新） */
  loadBTRuns: (force?: boolean) => Promise<BTRun[]>;
  /** 列表里单独更新某条 run（start/cancel 后调用） */
  patchBTRun: (runId: string, patch: Partial<BTRun>) => void;

  /* ---- Arena 方法 ---- */
  /** 进入某个 Arena 详情页（自动 setView("arena-detail")） */
  openArenaDetail: (arenaId: string) => void;
  /** 返回 Arena 列表页 */
  backFromArenaDetail: () => void;
  /** 拉取 Arena 列表（强制刷新） */
  loadArenas: (force?: boolean) => Promise<ArenaItem[]>;
  /** 列表里单独更新某条 Arena */
  patchArena: (arenaId: string, patch: Partial<ArenaItem>) => void;
  openProspectiveDetail: (runId: string) => void;
  backFromProspectiveDetail: () => void;
}

let abortCtl: AbortController | null = null;
/** 当前流式所属 case / message / question：logic_items 事件入库存档时使用 */
let currentCtx: { caseId: string; messageId: string; question: string } | null = null;

/** live-log 的 EventSource 连接（liveLogOpen=true 时建立，关闭时 close） */
let liveLogEs: EventSource | null = null;

// Only a real page unload should stop the attached stream. Merely switching
// tabs/windows must not cancel a multi-minute team research run.
if (typeof window !== "undefined") {
  const silentAbort = () => {
    if (abortCtl) {
      try { abortCtl.abort(); } catch { /* ignore */ }
    }
  };
  window.addEventListener("pagehide", silentAbort, { capture: true });
  window.addEventListener("beforeunload", silentAbort, { capture: true });
}

export const useStore = create<FeverState>((set, get) => {
  const prefs = loadUIPrefs();
  const initialRoute = routeFromLocation();
  const initialTeamMembersSet =
    prefs.teamMembersSet === true ||
    (prefs.teamMembersSet == null && Object.prototype.hasOwnProperty.call(prefs, "teamMembers"));
  /** 更新流式中的 assistant 消息 */
  const patchPending = (fn: (m: Message) => Message) => {
    set((s) => {
      const idx = s.messages.findIndex((m) => m.pending);
      if (idx < 0) return s;
      const next = [...s.messages];
      next[idx] = fn(next[idx]);
      return { messages: next };
    });
  };

  const patchParts = (fn: (parts: Part[]) => Part[]) => {
    patchPending((m) => ({ ...m, parts: fn(m.parts ?? []) }));
  };

  const finalizePending = (fn?: (m: Message) => Message) => {
    set((s) => ({
      messages: s.messages.map((m) => (m.pending ? { ...m, ...(fn?.(m) ?? {}), pending: false } : m)),
      streaming: false,
    }));
  };

  const handleEvent = (ev: SSEEvent) => {
    switch (ev.type) {
      case "meta":
        if (ev.mode) patchPending((m) => ({ ...m, mode: ev.mode }));
        // 后端可能新建了 case（前端传入的 case_id 无效时），同步到前端
        if (ev.case_id && ev.case_id !== get().currentCaseId) {
          set({ currentCaseId: ev.case_id });
        }
        break;
      case "thinking":
        if (ev.delta) patchParts((p) => appendDelta(p, "thinking", ev.agent, ev.delta!));
        break;
      case "token":
        if (ev.delta) patchParts((p) => appendDelta(p, "text", ev.agent, ev.delta!));
        break;
      case "tool_call":
        patchParts((p) => [
          ...p,
          {
            type: "tool_call",
            id: ev.id ?? uid(),
            agent: ev.agent,
            skill: ev.skill ?? "tool",
            args: ev.args,
            status: "running",
          },
        ]);
        break;
      case "tool_result":
        patchParts((parts) => {
          const idx = parts.findIndex((p) => p.type === "tool_call" && p.id === ev.id);
          if (idx < 0) return parts;
          const next = [...parts];
          const p = next[idx] as Extract<Part, { type: "tool_call" }>;
          next[idx] = {
            ...p,
            status: ev.ok === false ? "error" : "done",
            preview: ev.preview,
            artifactId: ev.artifact_id ?? p.artifactId,
          };
          return next;
        });
        break;
      case "artifact": {
        const a = ev.artifact;
        if (!a) break;
        set((st) => ({
          artifacts: sortArtifacts([...st.artifacts.filter((x) => x.id !== a.id), a]),
        }));
        patchParts((p) => [
          ...p,
          { type: "artifact", agent: ev.agent, artifactId: a.id, kind: a.kind, title: a.title },
        ]);
        break;
      }
      case "agent_step":
        patchParts((p) => [
          ...p,
          {
            type: "agent_step",
            phase: ev.phase ?? "",
            agent: ev.agent,
            note: ev.note,
            plan: ev.plan,
            verdict: ev.verdict,
            simulationJobId: ev.simulation_job_id,
          },
        ]);
        break;
      case "logic_items": {
        const items = ev.items ?? [];
        if (items.length === 0) break;
        // 1) 渲染到当前消息（part 流中追加）
        patchParts((p) => [...p, { type: "logic_items", items }]);
        // 2) 持久化入库（补全 case / message / question 上下文）
        const ctx = currentCtx;
        const enriched = items.map((x) => ({
          ...x,
          case_id: x.case_id ?? ctx?.caseId ?? null,
          message_id: x.message_id ?? ctx?.messageId ?? null,
          question: x.question || ctx?.question || "",
        }));
        get().addLogicItems(enriched);
        break;
      }
      case "case_title":
        if (ev.title) {
          set((st) => ({
            cases: st.cases.map((c) => (c.id === st.currentCaseId ? { ...c, title: ev.title! } : c)),
          }));
        }
        break;
      case "done":
        finalizePending((m) => ({ ...m, id: ev.message_id ?? m.id }));
        // 兜底：确保 currentCaseId 与后端一致
        if (ev.case_id && ev.case_id !== get().currentCaseId) {
          set({ currentCaseId: ev.case_id });
        }
        // 刷新 case 列表（updated_at / message_count）
        api
          .cases()
          .then((cases) => set({ cases: sortCases(cases) }))
          .catch(() => void 0);
        break;
      case "error":
        finalizePending((m) => ({ ...m, error: true, errorMessage: ev.message ?? "发生未知错误" }));
        break;
    }
  };

  return {
    cases: [],
    currentCaseId: null,
    messages: [],
    artifacts: [],
    skills: [],
    agents: [],
    rightTab: prefs.rightTab ?? "artifacts",
    // 默认折叠右栏；上一次状态持久化到 localStorage（用户主动展开/收起后记住）
    rightOpen: prefs.rightOpen ?? false,
    // 左栏默认展开（案例列表是主入口）；持久化记忆
    leftOpen: prefs.leftOpen ?? true,
    selectedArtifactId: null,
    streaming: false,
    mode: prefs.mode ?? "auto",
    selectedAgent: prefs.selectedAgent ?? "predictor",
    // 默认全选（首次进站无缓存时=空数组 → 在 setSkills 拉完 agents 后再补全）
    teamMembers: Array.isArray(prefs.teamMembers) ? prefs.teamMembers : [],
    teamMembersSet: initialTeamMembersSet,
    promptSeed: "",
    loadingCase: false,
    generatingReport: false,
    initialized: false,
    logicLibrary: loadLogicLibrary(),
    logicLibOpen: false,
    logicChecking: new Set<string>(),

    /* ---- Backend status / Live Log 默认值 ---- */
    backendStatus: "online",
    liveLogs: [],
    liveLogOpen: false,

    /* ---- Backtest 默认值 ---- */
    view: initialRoute.view,
    currentBTRunId: initialRoute.currentBTRunId,
    btRuns: [],
    btRunsLoading: false,

    /* ---- Arena 默认值 ---- */
    arenaItems: [],
    arenaLoading: false,
    currentArenaId: initialRoute.currentArenaId,
    currentProspectiveRunId: initialRoute.currentProspectiveRunId,

    init: async () => {
      if (get().initialized) return;
      set({ initialized: true });
      const [cases, skills, agents] = await Promise.allSettled([
        api.cases(),
        api.skills(),
        api.agents(),
      ]);
      const loadedAgents = agents.status === "fulfilled" ? agents.value : [];
      // 首次加载：把 teamMembers 默认填成"全部可调度专家"
      // 内部调度辅助（router / planner / synthesizer / verifier / report_writer）不参与
      const teamableIds = loadedAgents
        .filter((a) => !["router", "planner", "synthesizer", "verifier", "report_writer"].includes(a.id) && a.id !== "deep_researcher")
        .map((a) => a.id);
      const persisted = loadUIPrefs();
      const teamMembersSet =
        persisted.teamMembersSet === true ||
        (persisted.teamMembersSet == null && Object.prototype.hasOwnProperty.call(persisted, "teamMembers"));
      const rawMembers = Array.isArray(persisted.teamMembers) ? persisted.teamMembers : [];
      const allow = new Set(teamableIds);
      const filteredMembers = rawMembers.filter((id) => allow.has(id));
      const teamMembers = !teamMembersSet
        ? teamableIds
        : (rawMembers.length > 0 && filteredMembers.length === 0 ? teamableIds : filteredMembers);
      set({
        cases: cases.status === "fulfilled" ? sortCases(cases.value) : [],
        skills: skills.status === "fulfilled" ? skills.value : [],
        agents: loadedAgents,
        teamMembers,
        teamMembersSet,
      });
    },

    sendMessage: async (text, mode, agent) => {
      const content = text.trim();
      if (!content || get().streaming) return;
      // 防御性：进入时先 abort 任何残留的旧 controller（HMR / 异常路径留下的孤儿）
      abortCtl?.abort();
      const useMode = mode ?? get().mode;
      // agent 模式下：取调用方传入的 agent；fallback 到 state.selectedAgent
      const useAgent = agent ?? (useMode === "agent" ? get().selectedAgent : null);

      let caseId = get().currentCaseId;
      if (!caseId) {
        try {
          const c = await api.createCase();
          caseId = c.id;
          set((s) => ({ currentCaseId: c.id, cases: sortCases([c, ...s.cases]) }));
        } catch (e) {
          set((s) => ({
            messages: [
              ...s.messages,
              {
                id: uid(),
                role: "assistant",
                content: "",
                error: true,
                errorMessage: `创建研究失败：${e instanceof Error ? e.message : String(e)}`,
              },
            ],
          }));
          return;
        }
      }

      const now = new Date().toISOString();
      const userMsg: Message = { id: uid(), role: "user", content, created_at: now };
      const asstMsg: Message = {
        id: uid(),
        role: "assistant",
        content: "",
        parts: [],
        pending: true,
        mode: useMode,
        created_at: now,
      };
      set((s) => ({
        messages: [...s.messages, userMsg, asstMsg],
        streaming: true,
        mode: useMode,
      }));

      abortCtl = new AbortController();
      currentCtx = { caseId, messageId: asstMsg.id, question: content };
      try {
        await streamChat(
          { case_id: caseId, message: content, mode: useMode,
            agent: useAgent,
            team_members: useMode === "team" ? get().teamMembers : undefined },
          { onEvent: handleEvent, signal: abortCtl.signal },
        );
        // 流正常结束但未收到 done/error 时兜底收尾
        if (get().streaming) finalizePending();
      } catch (e) {
        if (e instanceof StreamAbortedError) {
          // 页面隐藏/切 tab/关 preview 触发的 abort：不写"已停止生成"，让用户无感
          const silent = document.visibilityState === "hidden";
          if (!silent) {
            finalizePending((m) => ({ ...m, error: true, errorMessage: "已停止生成" }));
          } else {
            finalizePending();
          }
        } else {
          finalizePending((m) => ({
            ...m,
            error: true,
            errorMessage: `请求失败：${e instanceof Error ? e.message : String(e)}`,
          }));
        }
      } finally {
        abortCtl = null;
        currentCtx = null;
      }
    },

    stop: () => {
      abortCtl?.abort();
    },

    retryLastMessage: async () => {
      if (get().streaming) return;
      const msgs = get().messages;
      // 找最后一条 user 消息
      let lastUserIdx = -1;
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i].role === "user") { lastUserIdx = i; break; }
      }
      if (lastUserIdx < 0) return;
      const lastUser = msgs[lastUserIdx];
      // 在 lastUser 之后找 error 的 assistant，优先保留它的 mode 用于重试
      let errorMode: Message["mode"] | undefined;
      for (let i = lastUserIdx + 1; i < msgs.length; i++) {
        const m = msgs[i];
        if (m.role === "assistant" && m.error && m.mode) {
          errorMode = m.mode;
          break;
        }
      }
      // 移除 lastUser 之后所有 error assistant 消息
      const newMsgs = msgs.filter((m, i) => {
        if (m.role === "assistant" && m.error) {
          return i <= lastUserIdx;
        }
        return true;
      });
      set({ messages: newMsgs });
      await get().sendMessage(lastUser.content, errorMode ?? lastUser.mode);
    },

    loadCase: async (id) => {
      if (get().streaming) get().stop();
      set({ loadingCase: true, currentCaseId: id, selectedArtifactId: null });
      try {
        const d = await api.caseDetail(id);
        const messages: Message[] = d.messages.map((m) => ({
          id: m.id,
          role: m.role,
          content: m.content,
          agent: m.agent,
          created_at: m.created_at,
          parts: m.role === "assistant" ? partsFromHistory(m) : undefined,
        }));
        set({
          messages,
          artifacts: sortArtifacts(d.artifacts ?? []),
          cases: sortCases(
            get().cases.some((c) => c.id === id)
              ? get().cases.map((c) => (c.id === id ? { ...c, ...d.case } : c))
              : [...get().cases, d.case],
          ),
        });
      } catch (e) {
        set({
          messages: [
            {
              id: uid(),
              role: "assistant",
              content: `⚠️ 加载研究失败：${e instanceof Error ? e.message : String(e)}`,
              error: true,
            },
          ],
          artifacts: [],
        });
      } finally {
        set({ loadingCase: false });
      }
    },

    newCase: () => {
      if (get().streaming) get().stop();
      set({
        currentCaseId: null,
        messages: [],
        artifacts: [],
        selectedArtifactId: null,
        rightTab: "artifacts",
      });
    },

    deleteCase: async (id) => {
      try {
        await api.deleteCase(id);
      } catch {
        /* 即使失败也从列表移除 */
      }
      set((s) => {
        const cases = s.cases.filter((c) => c.id !== id);
        if (s.currentCaseId === id) {
          return {
            cases,
            currentCaseId: null,
            messages: [],
            artifacts: [],
            selectedArtifactId: null,
          };
        }
        return { cases };
      });
    },

    pinArtifact: async (artifactId) => {
      const caseId = get().currentCaseId;
      if (!caseId) return;
      // 乐观更新
      set((s) => ({
        artifacts: sortArtifacts(
          s.artifacts.map((a) =>
            a.id === artifactId ? { ...a, pinned: a.pinned ? 0 : 1 } : a,
          ),
        ),
      }));
      try {
        const r = await api.pinArtifact(caseId, artifactId);
        set((s) => ({
          artifacts: sortArtifacts(
            s.artifacts.map((a) => (a.id === artifactId ? { ...a, pinned: r.pinned } : a)),
          ),
        }));
      } catch {
        /* 保持乐观状态 */
      }
    },

    genReport: async () => {
      const caseId = get().currentCaseId;
      if (!caseId || get().generatingReport) return;
      set({ generatingReport: true, rightTab: "artifacts", rightOpen: true });
      try {
        const artifact = await api.genReport(caseId);
        set((s) => ({
          artifacts: sortArtifacts([artifact, ...s.artifacts.filter((a) => a.id !== artifact.id)]),
          selectedArtifactId: artifact.id,
        }));
      } catch (e) {
        set((s) => ({
          messages: [
            ...s.messages,
            {
              id: uid(),
              role: "assistant",
              content: `⚠️ 生成研究报告失败：${e instanceof Error ? e.message : String(e)}`,
              error: true,
            },
          ],
        }));
      } finally {
        set({ generatingReport: false });
      }
    },

    selectArtifact: (id) => {
      set({ selectedArtifactId: id, rightTab: "artifacts", rightOpen: true });
      const s = get();
      saveUIPrefs({ rightTab: "artifacts", rightOpen: true, leftOpen: s.leftOpen,
                    mode: s.mode, selectedAgent: s.selectedAgent, teamMembers: s.teamMembers, teamMembersSet: s.teamMembersSet });
    },

    /** 通用持久化：传入要修改的字段，回填其它字段当前值后整体保存 */
    setRightTab: (t) => {
      set({ rightTab: t, rightOpen: true });
      const s = get();
      saveUIPrefs({ rightTab: t, rightOpen: true, leftOpen: s.leftOpen,
                    mode: s.mode, selectedAgent: s.selectedAgent, teamMembers: s.teamMembers, teamMembersSet: s.teamMembersSet });
    },
    setRightOpen: (v) => {
      set({ rightOpen: v });
      const s = get();
      saveUIPrefs({ rightTab: s.rightTab, rightOpen: v, leftOpen: s.leftOpen,
                    mode: s.mode, selectedAgent: s.selectedAgent, teamMembers: s.teamMembers, teamMembersSet: s.teamMembersSet });
    },
    setLeftOpen: (v) => {
      set({ leftOpen: v });
      const s = get();
      saveUIPrefs({ rightTab: s.rightTab, rightOpen: s.rightOpen, leftOpen: v,
                    mode: s.mode, selectedAgent: s.selectedAgent, teamMembers: s.teamMembers, teamMembersSet: s.teamMembersSet });
    },
    setMode: (m) => {
      set({ mode: m });
      const s = get();
      saveUIPrefs({ rightTab: s.rightTab, rightOpen: s.rightOpen, leftOpen: s.leftOpen,
                    mode: m, selectedAgent: s.selectedAgent, teamMembers: s.teamMembers, teamMembersSet: s.teamMembersSet });
    },
    setSelectedAgent: (id) => {
      set({ selectedAgent: id });
      const s = get();
      saveUIPrefs({ rightTab: s.rightTab, rightOpen: s.rightOpen, leftOpen: s.leftOpen,
                    mode: s.mode, selectedAgent: id, teamMembers: s.teamMembers, teamMembersSet: s.teamMembersSet });
    },
    setTeamMembers: (ids) => {
      set({ teamMembers: ids, teamMembersSet: true });
      const s = get();
      saveUIPrefs({ rightTab: s.rightTab, rightOpen: s.rightOpen, leftOpen: s.leftOpen,
                    mode: s.mode, selectedAgent: s.selectedAgent, teamMembers: ids, teamMembersSet: true });
    },

    setPromptSeed: (s) => {
      set({ promptSeed: s });
    },

    /* ---------------- research logic library ---------------- */
    addLogicItems: (items) => {
      set((s) => {
        const seen = new Set(s.logicLibrary.map((x) => x.id));
        const merged: LogicItem[] = [...s.logicLibrary];
        for (const it of items) {
          if (seen.has(it.id)) continue;
          // 兜底字段
          merged.push({
            ...it,
            check_history: it.check_history ?? [],
            next_check_at: it.next_check_at ?? null,
            last_check_at: it.last_check_at ?? null,
          });
        }
        saveLogicLibrary(merged);
        return { logicLibrary: merged };
      });
    },
    updateLogicItem: (id, patch) => {
      set((s) => {
        const next = s.logicLibrary.map((x) =>
          x.id === id ? { ...x, ...patch } : x,
        );
        saveLogicLibrary(next);
        return { logicLibrary: next };
      });
    },
    dismissLogicItem: (id) => {
      get().updateLogicItem(id, {
        status: "dismissed",
        verified_at: new Date().toISOString(),
      });
    },
    markLogicCheck: (id, status, note) => {
      const now = new Date().toISOString();
      const entry: LogicCheckEntry = {
        at: now,
        verdict: status,
        reasoning: note ?? `人工标记为「${status}」`,
        source: "manual",
      };
      set((s) => {
        const next = s.logicLibrary.map((x) => {
          if (x.id !== id) return x;
          return {
            ...x,
            status,
            verified_at: now,
            verification_note: note ?? x.verification_note,
            last_check_at: now,
            check_history: [entry, ...(x.check_history ?? [])],
          };
        });
        saveLogicLibrary(next);
        return { logicLibrary: next };
      });
    },
    autoCheckLogic: async (id) => {
      const item = get().logicLibrary.find((x) => x.id === id);
      if (!item) return null;
      // 标记 loading
      set((s) => {
        const ns = new Set(s.logicChecking);
        ns.add(id);
        return { logicChecking: ns };
      });
      try {
        const res = await api.logicAutoCheck({
          hypothesis: item.hypothesis,
          category: item.category,
          scope: item.scope,
          horizon: item.horizon,
          check: item.check,
          question: item.question,
        });
        const now = new Date().toISOString();
        const entry: LogicCheckEntry = {
          at: now,
          verdict: res.verdict,
          reasoning: res.reasoning,
          data_summary: res.data_summary,
          next_check_at: res.next_check_at,
          evidence: res.evidence,
          source: "auto",
        };
        // verdict → status 映射
        const status: LogicItem["status"] =
          res.verdict === "verified" ? "verified"
          : res.verdict === "rejected" ? "rejected"
          : res.verdict === "pending_scheduled" ? "pending_scheduled"
          : res.verdict === "error" ? "inconclusive"
          : "inconclusive";
        let nextItem: LogicItem | null = null;
        set((s) => {
          const next = s.logicLibrary.map((x) => {
            if (x.id !== id) return x;
            nextItem = {
              ...x,
              status,
              verified_at: status === "verified" || status === "rejected" ? now : x.verified_at,
              last_check_at: now,
              next_check_at: res.next_check_at ?? null,
              verification_note:
                status === "verified" || status === "rejected"
                  ? (res.data_summary || res.reasoning).slice(0, 200)
                  : x.verification_note,
              check_history: [entry, ...(x.check_history ?? [])],
            };
            return nextItem;
          });
          saveLogicLibrary(next);
          return { logicLibrary: next };
        });
        return nextItem;
      } catch (e) {
        // 错误落档到 check_history 但不改变 status
        const now = new Date().toISOString();
        const entry: LogicCheckEntry = {
          at: now,
          verdict: "error",
          reasoning: `深度验证异常: ${e instanceof Error ? e.message : String(e)}`,
          source: "auto",
        };
        set((s) => {
          const next = s.logicLibrary.map((x) => {
            if (x.id !== id) return x;
            return {
              ...x,
              last_check_at: now,
              check_history: [entry, ...(x.check_history ?? [])],
            };
          });
          saveLogicLibrary(next);
          return { logicLibrary: next };
        });
        return null;
      } finally {
        set((s) => {
          const ns = new Set(s.logicChecking);
          ns.delete(id);
          return { logicChecking: ns };
        });
      }
    },
    reverifyLogic: (item) => {
      // 把 hypothesis + horizon 注入新 case，作为再次验证的种子问题
      const scope = item.scope ? `（涉及 ${item.scope}）` : "";
      const horizon = item.horizon ? `；验证窗口：${item.horizon}` : "";
      const check = item.check ? `；如何验证：${item.check}` : "";
      const seed = `请复盘/验证以下研究逻辑：\n「${item.hypothesis}」${scope}${horizon}${check}\n\n请调取最近市场数据，给出当前是否被证实/证伪，列出关键证据。`;
      // 切换到新研究并直接发问
      get().newCase();
      void get().sendMessage(seed, "team");
    },
    setLogicLibOpen: (v) => set({ logicLibOpen: v }),

    /* ---------------- Backend status / Live Log (debug) ---------------- */
    toggleLiveLog: () => {
      const open = !get().liveLogOpen;
      set({ liveLogOpen: open });
      if (open) {
        // 建立连接：已有连接则复用，避免重复订阅
        if (!liveLogEs) {
          try {
            const es = new EventSource("/api/admin/live-log");
            es.onmessage = (e) => {
              try {
                const data = JSON.parse(e.data) as { ts?: string; msg?: string };
                const entry: LogEntry = {
                  ts: data.ts ?? new Date().toISOString(),
                  msg: data.msg ?? "",
                };
                // 最新在前，最多保留 200 条
                useStore.setState((s) => ({
                  liveLogs: [entry, ...s.liveLogs].slice(0, 200),
                }));
              } catch {
                /* 忽略坏帧 */
              }
            };
            // 连接异常时浏览器会自动重连，这里只标记断开态
            es.onerror = () => {
              /* EventSource 自动重连，无需手动处理 */
            };
            liveLogEs = es;
          } catch {
            /* EventSource 不可用（如 SSR 环境），忽略 */
          }
        }
      } else {
        if (liveLogEs) {
          try { liveLogEs.close(); } catch { /* ignore */ }
          liveLogEs = null;
        }
      }
    },
    clearLiveLogs: () => set({ liveLogs: [] }),

    /* ===================================== Backtest 方法 ===================================== */

    setView: (v) => {
      // 切页面前如果 chat 在 streaming，则先停掉，避免挂起的 SSE 泄漏
      if (v !== "chat" && get().streaming) get().stop();
      const view = v === "backtest-list" ? "backtest-runs" : v;
      const next = {
        view,
        currentBTRunId: view === "backtest-detail" ? get().currentBTRunId : null,
        currentArenaId: view === "arena-detail" ? get().currentArenaId : null,
        currentProspectiveRunId:
          view === "prospective-detail" ? get().currentProspectiveRunId : null,
      };
      set(next);
      pushRoute(pathForView(view, next));
    },

    syncRouteFromLocation: () => {
      const route = routeFromLocation();
      if (route.view !== "chat" && get().streaming) get().stop();
      set(route);
    },

    openBTDetail: (runId) => {
      if (get().streaming) get().stop();
      const next = {
        view: "backtest-detail" as const,
        currentBTRunId: runId,
        currentArenaId: null,
        currentProspectiveRunId: null,
      };
      set(next);
      pushRoute(pathForView(next.view, next));
    },

    backFromBTDetail: () => {
      // 详情页返回列表页；列表数据以最新 DB 为准（详情页运行期间的进度不再向 store 双写）
      set({ view: "backtest-runs", currentBTRunId: null });
      pushRoute("/backtest/runs");
      void get().loadBTRuns(true);
    },

    loadBTRuns: async (force) => {
      const s = get();
      if (!force && s.btRuns.length > 0 && !s.btRunsLoading) return s.btRuns;
      set({ btRunsLoading: true });
      try {
        const res = await api.btRuns(200);
        const items = [...res.items].sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
        set({ btRuns: items, btRunsLoading: false });
        return items;
      } catch (e) {
        set({ btRunsLoading: false });
        throw e;
      }
    },

    patchBTRun: (runId, patch) => {
      set((s) => ({
        btRuns: s.btRuns.map((r) => (r.id === runId ? { ...r, ...patch } : r)),
      }));
    },

    /* ===================================== Arena 方法 ===================================== */

    openArenaDetail: (arenaId) => {
      if (get().streaming) get().stop();
      const next = {
        view: "arena-detail" as const,
        currentArenaId: arenaId,
        currentBTRunId: null,
        currentProspectiveRunId: null,
      };
      set(next);
      pushRoute(pathForView(next.view, next));
    },

    backFromArenaDetail: () => {
      set({ view: "arena-list", currentArenaId: null });
      pushRoute("/arena");
      void get().loadArenas(true);
    },

    loadArenas: async (force) => {
      if (!force && get().arenaItems.length > 0) return get().arenaItems;
      set({ arenaLoading: true });
      try {
        const res = await api.arenaList(200);
        set({ arenaItems: res.items ?? [], arenaLoading: false });
        return res.items ?? [];
      } catch (e) {
        set({ arenaLoading: false });
        throw e;
      }
    },

    patchArena: (arenaId, patch) => {
      set((s) => ({
        arenaItems: s.arenaItems.map((a) => (a.id === arenaId ? { ...a, ...patch } : a)),
      }));
    },

    openProspectiveDetail: (runId) => {
      if (get().streaming) get().stop();
      const next = {
        view: "prospective-detail" as const,
        currentProspectiveRunId: runId,
        currentBTRunId: null,
        currentArenaId: null,
      };
      set(next);
      pushRoute(pathForView(next.view, next));
    },

    backFromProspectiveDetail: () => {
      set({ view: "prospective-list", currentProspectiveRunId: null });
      pushRoute("/prospective");
    },
  };
});

/* ---------------- 后端健康检查轮询（每 3 秒） ----------------
 * 不走 api.ts 的 req（health 端点只需知道是否 reachable，无需解析 body）：
 * fetch 成功且 res.ok → online；网络失败 / 非 2xx → offline。
 * 初始值 "online"（乐观），首次轮询失败立刻改 offline。 */
if (typeof window !== "undefined") {
  const checkHealth = async () => {
    try {
      const res = await fetch("/api/health", { method: "GET" });
      useStore.setState({ backendStatus: res.ok ? "online" : "offline" });
    } catch {
      useStore.setState({ backendStatus: "offline" });
    }
  };
  void checkHealth();
  window.setInterval(checkHealth, 3000);
}
