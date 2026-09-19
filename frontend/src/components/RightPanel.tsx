import { useMemo, useState } from "react";
import {
  ArrowLeft,
  Beaker,
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  ExternalLink,
  FileText,
  FolderOpen,
  Layers,
  Loader2,
  PanelRightClose,
  PanelRightOpen,
  Pin,
  PinOff,
  Users,
  Wrench,
  Eye,
  EyeOff,
  BarChart3,
  Network,
  Newspaper,
} from "lucide-react";
import type { Artifact, EvidencePayload, ReportPayload, SkillMeta } from "../types";
import { KIND_CN, skillCn } from "../names";
import { cls, relTime } from "../utils";
import { useStore } from "../store";
import { KindIcon } from "./ArtifactCard";
import KlineChart from "./KlineChart";
import CarChart from "./CarChart";
import DataTable from "./DataTable";
import Markdown from "./Markdown";
import GraphView from "./GraphView";
import SimulationLaunch from "./SimulationLaunch";
import SimulationView from "./SimulationView";
import LogicLibraryTab from "./LogicLibraryTab";

/* ---------------- 证据卡列表 ---------------- */

function EvidenceView({ payload }: { payload: EvidencePayload }) {
  const items = payload?.items ?? [];
  return (
    <div className="space-y-2.5">
      {items.map((it, i) => (
        <div key={i} className="rounded-card border border-edge bg-card px-3.5 py-3 shadow-card transition-colors hover:border-edgeDark">
          {it.url ? (
            <a
              href={it.url}
              target="_blank"
              rel="noreferrer"
              className="group flex items-start gap-1 text-[13px] font-medium leading-snug text-ink hover:text-jade transition-colors"
            >
              <span className="flex-1">{it.title}</span>
              <ExternalLink size={12} className="mt-1 shrink-0 text-faint group-hover:text-jade" />
            </a>
          ) : (
            <p className="text-[13px] font-medium leading-snug text-ink">{it.title}</p>
          )}
          <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-faint">
            {it.date && <span>{it.date}</span>}
            <span className="rounded bg-[#F4F2EE] px-1.5 py-px font-mono text-[10.5px] text-mute">{it.source}</span>
          </div>
          {it.snippet && <p className="mt-1.5 line-clamp-3 text-[12px] leading-relaxed text-mute">{it.snippet}</p>}
        </div>
      ))}
      {items.length === 0 && <p className="py-8 text-center text-[12.5px] text-faint">暂无证据条目</p>}
    </div>
  );
}

/* ---------------- 报告视图（markdown + 复制） ---------------- */

function ReportView({ payload }: { payload: ReportPayload }) {
  const [copied, setCopied] = useState(false);
  const md = payload?.markdown ?? "";
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(md);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = md;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1800);
  };
  return (
    <div>
      <div className="mb-3 flex justify-end">
        <button
          onClick={copy}
          className={cls(
            "flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[12px] font-medium transition-colors",
            copied
              ? "border-jade/40 bg-jade-soft text-jade"
              : "border-edge bg-card text-mute hover:border-edgeDark hover:text-ink",
          )}
        >
          {copied ? <Check size={13} /> : <Copy size={13} />}
          {copied ? "已复制" : "复制 markdown"}
        </button>
      </div>
      <div className="rounded-card border border-edge bg-card px-5 py-4 shadow-card">
        <Markdown text={md} />
      </div>
    </div>
  );
}

/* ---------------- artifact 大视图 ---------------- */

function ArtifactDetail({ artifact }: { artifact: Artifact }) {
  const selectArtifact = useStore((s) => s.selectArtifact);
  const pinArtifact = useStore((s) => s.pinArtifact);
  const pinned = !!artifact.pinned;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 px-4 pb-3 pt-3.5">
        <button
          onClick={() => selectArtifact(null)}
          className="flex h-7 w-7 items-center justify-center rounded-lg border border-edge bg-card text-mute transition-colors hover:text-ink"
          title="返回列表"
        >
          <ArrowLeft size={14} />
        </button>
        <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-jade-soft text-jade">
          <KindIcon kind={artifact.kind} size={14} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="truncate text-[13px] font-semibold text-ink">{artifact.title}</p>
          <p className="text-[11px] text-faint">
            {KIND_CN[artifact.kind] ?? artifact.kind} · {relTime(artifact.created_at)}
          </p>
        </div>
        <button
          onClick={() => void pinArtifact(artifact.id)}
          title={pinned ? "取消置顶" : "置顶"}
          className={cls(
            "flex h-7 w-7 items-center justify-center rounded-lg border transition-colors",
            pinned
              ? "border-brand/40 bg-brand-soft text-brand"
              : "border-edge bg-card text-faint hover:text-brand",
          )}
        >
          {pinned ? <PinOff size={13} /> : <Pin size={13} />}
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto px-4 pb-4">
        {artifact.kind === "kline" && (
          <div className="rounded-card border border-edge bg-card p-2 shadow-card">
            <KlineChart payload={artifact.payload} />
          </div>
        )}
        {artifact.kind === "line" && (
          <div className="rounded-card border border-edge bg-card p-2 shadow-card">
            <CarChart payload={artifact.payload} />
          </div>
        )}
        {artifact.kind === "table" && <DataTable payload={artifact.payload} />}
        {artifact.kind === "evidence" && <EvidenceView payload={artifact.payload} />}
        {artifact.kind === "report" && <ReportView payload={artifact.payload} />}
        {artifact.kind === "graph" && <><SimulationLaunch artifact={artifact} /><GraphView payload={artifact.payload} /></>}
        {artifact.kind === "simulation" && <><a href={`/simulations?case=${encodeURIComponent(artifact.case_id)}&artifact=${encodeURIComponent(artifact.id)}`} target="_blank" rel="noreferrer" className="mb-3 block rounded-lg border border-jade/30 bg-jade-soft px-3 py-2 text-center text-xs font-semibold text-jade">在独立推演窗口查看与复核 ↗</a><SimulationView payload={artifact.payload} /></>}
      </div>
    </div>
  );
}

/* ---------------- 产出物列表 ---------------- */

function ArtifactList() {
  const artifacts = useStore((s) => s.artifacts);
  const currentCaseId = useStore((s) => s.currentCaseId);
  const selectedId = useStore((s) => s.selectedArtifactId);
  const selectArtifact = useStore((s) => s.selectArtifact);
  const pinArtifact = useStore((s) => s.pinArtifact);
  const genReport = useStore((s) => s.genReport);
  const generatingReport = useStore((s) => s.generatingReport);
  const streaming = useStore((s) => s.streaming);

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between px-4 pb-2.5 pt-3.5">
        <span className="text-[12px] font-medium text-mute">
          {artifacts.length > 0 ? `${artifacts.length} 件产出物` : "产出物"}
        </span>
        <button
          onClick={() => void genReport()}
          disabled={!currentCaseId || generatingReport || streaming}
          title={currentCaseId ? "基于本研究的全部产出物与对话生成研究报告" : "先开始一个研究"}
          className={cls(
            "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] font-medium transition-all",
            !currentCaseId || generatingReport || streaming
              ? "cursor-not-allowed bg-edge/50 text-faint"
              : "bg-brand text-card shadow-sm hover:bg-brand-hover",
          )}
        >
          {generatingReport ? <Loader2 size={13} className="animate-spin" /> : <FileText size={13} />}
          {generatingReport ? "撰写中…" : "生成研究报告"}
        </button>
      </div>

      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-4 pb-4">
        {(() => {
          // 分组：置顶 → 图表 → 表格 → 证据 → 报告/图谱/推演 → 其它
          const pinned = artifacts.filter((a) => a.pinned);
          const byKind: Record<string, typeof artifacts> = { kline: [], line: [], table: [], evidence: [], graph: [], report: [], simulation: [] };
          for (const a of artifacts) {
            if (a.pinned) continue;
            (byKind[a.kind] ||= []).push(a);
          }
          const renderCard = (a: any) => (
            <div key={a.id} className="group relative">
              <button
                onClick={() => selectArtifact(a.id)}
                className={cls(
                  "flex w-full items-center gap-3 rounded-card border bg-card px-3 py-2.5 text-left shadow-card transition-all duration-150 hover:-translate-y-px hover:shadow-pop",
                  selectedId === a.id ? "border-jade/60 ring-1 ring-jade/30" : "border-edge hover:border-edgeDark",
                )}
              >
                <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-jade-soft text-jade">
                  <KindIcon kind={a.kind} size={15} />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[12.5px] font-medium text-ink">
                    {!!a.pinned && <Pin size={10} className="mr-1 inline -translate-y-px text-brand" />}
                    {a.title}
                  </span>
                  <span className="mt-0.5 block text-[11px] text-faint">
                    {KIND_CN[a.kind] ?? a.kind} · {relTime(a.created_at)}
                  </span>
                </span>
              </button>
              <button
                onClick={() => void pinArtifact(a.id)}
                title={a.pinned ? "取消置顶" : "置顶"}
                className={cls(
                  "absolute right-2 top-2 rounded-md p-1 transition-all",
                  a.pinned
                    ? "text-brand opacity-100"
                    : "text-faint opacity-0 hover:bg-edge/60 hover:text-brand group-hover:opacity-100",
                )}
              >
                {a.pinned ? <PinOff size={12} /> : <Pin size={12} />}
              </button>
            </div>
          );

          return (
            <>
              <GroupSection
                label="深度研究"
                icon={<Network size={12} className="text-brand" />}
                count={(byKind.graph.length || 0) + (byKind.report.length || 0) + (byKind.simulation.length || 0)}
                defaultOpen={true}
              >
                {[...byKind.simulation, ...byKind.graph, ...byKind.report].map(renderCard)}
                {byKind.graph.length + byKind.report.length + byKind.simulation.length === 0 && (
                  <EmptyHint text="证据图、情景推演与研究报告" />
                )}
              </GroupSection>

              {pinned.length > 0 && (
                <GroupSection
                  label="已置顶"
                  icon={<Pin size={12} className="text-brand" />}
                  count={pinned.length}
                  defaultOpen
                >
                  {pinned.map(renderCard)}
                </GroupSection>
              )}

              <GroupSection
                label="图表"
                icon={<BarChart3 size={12} className="text-jade" />}
                count={(byKind.kline.length || 0) + (byKind.line.length || 0)}
                defaultOpen={pinned.length === 0}
              >
                {[...byKind.kline, ...byKind.line].map(renderCard)}
                {byKind.kline.length + byKind.line.length === 0 && (
                  <EmptyHint text="K线 / 走势 / 资金曲线等图表" />
                )}
              </GroupSection>

              <GroupSection
                label="数据表"
                icon={<FolderOpen size={12} className="text-mute" />}
                count={byKind.table.length}
                defaultOpen={false}
              >
                {byKind.table.map(renderCard)}
                {byKind.table.length === 0 && <EmptyHint text="板块、股东、财务等结构化数据" />}
              </GroupSection>

              <GroupSection
                label="资讯证据"
                icon={<Newspaper size={12} className="text-mute" />}
                count={byKind.evidence.length}
                defaultOpen={false}
              >
                {byKind.evidence.map(renderCard)}
                {byKind.evidence.length === 0 && <EmptyHint text="新闻、公告、快讯" />}
              </GroupSection>
            </>
          );
        })()}

        {artifacts.length === 0 && (
          <div className="flex h-full min-h-[240px] flex-col items-center justify-center px-6 text-center">
            <span className="flex h-12 w-12 items-center justify-center rounded-card border border-dashed border-edgeDark text-faint">
              <FolderOpen size={20} />
            </span>
            <p className="mt-3 text-[12.5px] leading-relaxed text-faint">
              对话中产生的图表、数据与证据
              <br />
              会出现在这里
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/** 分组折叠：可独立开合，头部含图标 + 标题 + 数量徽章。 */
function GroupSection({
  label,
  icon,
  count,
  defaultOpen,
  children,
}: {
  label: string;
  icon?: React.ReactNode;
  count: number;
  defaultOpen: boolean;
  children: React.ReactNode;
}) {
  // 始终渲染标题；如果组为空且 defaultOpen，children 区域里会有 EmptyHint
  return (
    <details className="group" open={defaultOpen}>
      <summary
        className={cls(
          "sticky top-0 z-10 -mx-1 mb-1.5 flex cursor-pointer list-none items-center gap-2 rounded-md bg-page/85 px-1 py-1.5 backdrop-blur-sm transition-colors hover:bg-edge/40",
          "select-none",
        )}
      >
        <ChevronRight
          size={12}
          className="text-faint transition-transform group-open:rotate-90"
        />
        {icon}
        <span className="text-[11.5px] font-medium uppercase tracking-wide text-ink">
          {label}
        </span>
        <span className="rounded-full bg-edge/70 px-1.5 py-px text-[10.5px] font-medium text-mute">
          {count}
        </span>
      </summary>
      <div className="space-y-2 pl-1">{children}</div>
    </details>
  );
}

function EmptyHint({ text }: { text: string }) {
  return (
    <div className="rounded-lg border border-dashed border-edge/80 bg-card/40 px-3 py-2 text-[11px] text-faint">
      暂无 · {text}
    </div>
  );
}

/* ---------------- 技能 tab ----------------
 * 展示三层模型：skill（对 LLM 可见）为主，atomic 工具（internal）
 * 默认折叠，让"工具-tool → 技能-skill → 智能体-agent → 团队-team"关系一目了然。
 */

function SkillsTab() {
  const skills = useStore((s) => s.skills);
  const [showAtomic, setShowAtomic] = useState(false);

  // 后端不再返 internal=True 的 skill（如 evidence_graph 内部 dispatcher 本身是
  // skill 但有 composes 列表，逻辑上仍是 high-level）
  const topLevel = skills.filter((s) => s.category === "skill");
  const atomic = skills.filter((s) => s.category === "atomic");
  // atomic 工具按数据域分组，便于浏览
  const atomicByDomain = atomic.reduce<Record<string, SkillMeta[]>>((acc, s) => {
    const domain = s.composes && s.composes.length > 0
      ? "其它"  // 不太可能发生
      : atomicDomainOf(s.name);
    (acc[domain] ||= []).push(s);
    return acc;
  }, {});

  return (
    <div className="h-full space-y-2 overflow-y-auto px-4 py-3.5">
      {/* 顶部说明：三层模型 */}
      <div className="rounded-card border border-brand/20 bg-brand-soft/40 px-3 py-2.5 text-[11.5px] leading-relaxed text-brand">
        <div className="flex items-center gap-1.5 font-semibold">
          <Layers size={12} /> 三层调度模型
        </div>
        <div className="mt-1.5 text-mute">
          <span className="font-mono">tool(52)</span> →{" "}
          <span className="font-mono">skill({topLevel.length})</span> →{" "}
          <span className="font-mono">agent(5)</span> → <span className="font-mono">team</span>
        </div>
        <div className="mt-1 text-faint">
          工具（atomic）是 akshare/yfinance 取数；技能（skill）把多个工具拼成可研究的视角；智能体只看到 skill。
        </div>
      </div>

      {/* 1. skill 标题块（卡片化，跟三层模型 / 底层工具 视觉对齐） */}
      <div className="flex items-center justify-between rounded-card border border-jade/30 bg-jade-soft/30 px-3 py-2.5 shadow-card">
        <div className="flex items-center gap-2">
          <Wrench size={13} className="text-jade" />
          <span className="text-[12.5px] font-semibold text-ink">对外技能</span>
          <span className="rounded-full bg-jade px-1.5 py-0.5 text-[10.5px] font-mono font-semibold text-card">
            {topLevel.length}
          </span>
        </div>
        <span className="text-[11px] text-faint">Agent 实际可调用</span>
      </div>
      {topLevel.map((s) => (
        <SkillCard key={s.name} skill={s} />
      ))}

      {/* 2. atomic 工具（折叠） */}
      <div className="mt-3 flex items-center justify-between rounded-card border border-edge bg-card px-3 py-2 shadow-card">
        <button
          onClick={() => setShowAtomic((v) => !v)}
          className="flex flex-1 items-center gap-2 text-left"
        >
          {showAtomic ? <ChevronDown size={14} className="text-faint" /> : <ChevronRight size={14} className="text-faint" />}
          <span className="text-[12.5px] font-semibold text-ink">底层工具</span>
          <span className="text-[11px] text-faint">({atomic.length} 个 atomic · 仅供 skill 内部调用)</span>
        </button>
        <button
          onClick={() => setShowAtomic((v) => !v)}
          className="text-faint hover:text-mute"
          title={showAtomic ? "隐藏" : "展开"}
        >
          {showAtomic ? <EyeOff size={12} /> : <Eye size={12} />}
        </button>
      </div>
      {showAtomic && (
        <div className="space-y-2 pl-3">
          {Object.entries(atomicByDomain).map(([domain, items]) => (
            <details key={domain} className="rounded-lg border border-edge bg-card/60 px-3 py-2" open={false}>
              <summary className="flex cursor-pointer items-center gap-2 text-[12px] font-medium text-mute">
                <ChevronRight size={12} className="domain-caret transition-transform" />
                <span className="text-ink">{domain}</span>
                <span className="text-faint">· {items.length}</span>
              </summary>
              <ul className="mt-2 space-y-1.5 pl-5">
                {items.map((s) => (
                  <li key={s.name} className="flex items-baseline gap-1.5 text-[11.5px]">
                    <code className="rounded bg-[#F4F2EE] px-1.5 py-px font-mono text-mute">
                      {s.name}
                    </code>
                    <span className="line-clamp-1 text-faint" title={s.description}>
                      {s.description}
                    </span>
                  </li>
                ))}
              </ul>
            </details>
          ))}
        </div>
      )}
      {skills.length === 0 && <p className="py-8 text-center text-[12.5px] text-faint">技能清单加载失败或后端未启动</p>}
    </div>
  );
}

function SkillCard({ skill }: { skill: SkillMeta }) {
  const [expanded, setExpanded] = useState(false);
  const props = (skill.parameters as { properties?: Record<string, unknown> } | undefined)?.properties;
  const params = props ? Object.keys(props) : [];
  const hasChildren = (skill.composes?.length || 0) > 0;

  return (
    <div className="rounded-card border border-jade/30 bg-card px-3.5 py-3 shadow-card transition-colors hover:border-jade/50">
      <div className="flex items-center gap-2">
        <Wrench size={13} className="text-jade" />
        <span className="text-[13px] font-semibold text-ink">{skillCn(skill.name)}</span>
        <code className="rounded bg-jade-soft/60 px-1.5 py-0.5 font-mono text-[10.5px] text-jade">
          skill
        </code>
        {hasChildren && (
          <button
            onClick={() => setExpanded((v) => !v)}
            className="ml-auto flex items-center gap-1 rounded-md px-1.5 py-0.5 text-[10.5px] text-faint hover:bg-jade-soft hover:text-jade"
            title="查看内部 skill 调用的 atomic 工具"
          >
            {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            {skill.composes!.length} 子能力
          </button>
        )}
      </div>
      <p className="mt-1.5 text-[12px] leading-relaxed text-mute">{skill.description}</p>
      {params.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {params.map((p) => (
            <span
              key={p}
              className="rounded bg-[#F4F2EE] px-1.5 py-px font-mono text-[10.5px] text-mute"
            >
              {p}
            </span>
          ))}
        </div>
      )}
      {expanded && hasChildren && (
        <ul className="mt-2 space-y-1 border-t border-edge/60 pt-2 text-[11.5px]">
          {skill.composes!.map((sub) => (
            <li key={sub} className="flex items-center gap-1.5 font-mono text-mute">
              <span className="text-faint">└</span>
              <code className="rounded bg-[#F4F2EE] px-1 py-px text-[10.5px]">
                {sub}
              </code>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/** 根据 atomic skill 名字归到数据域。简单字符串匹配。 */
function atomicDomainOf(name: string): string {
  if (name.startsWith("get_stock_") || name === "get_stock_daily" || name === "search_stock") return "行情/检索";
  if (name.startsWith("get_financial_") || name === "get_income_statement" ||
      name === "get_profit_forecast" || name === "get_research_report") return "财务/研报";
  if (name.startsWith("get_holder_") || name === "get_restricted_release") return "股东/解禁";
  if (name.startsWith("get_industry_") || name.startsWith("get_sector_") ||
      name.startsWith("list_industry_") || name === "get_board_change") return "板块";
  if (name.startsWith("get_fund_flow") || name.startsWith("get_capital_") ||
      name === "get_lhb_detail" || name === "get_margin_trade") return "资金/龙虎榜";
  if (name.startsWith("get_global_") || name === "get_macro") return "全球/宏观";
  if (name === "event_study" || name === "get_current_date") return "工具";
  if (name.startsWith("_eg_")) return "证据图（sub-tool）";
  return "其它";
}

/* ---------------- 团队 tab ---------------- */

function TeamTab() {
  const agents = useStore((s) => s.agents);
  const skills = useStore((s) => s.skills);

  // 计算每个 agent 实际可见的 skill（过滤 internal）
  // 与后端 tools_for_agent 行为一致：internal=True 的 atomic 不算在 agent 视角里
  const visibleByAgent: Record<string, string[]> = useMemo(() => {
    const map: Record<string, string[]> = {};
    for (const a of agents) {
      const set = new Set((a.skills || []).filter(Boolean));
      const visible: string[] = [];
      for (const s of skills) {
        if (s.internal) continue;  // atomic internal 不算
        if (set.has(s.name) || set.size === 0) {
          visible.push(s.name);
        }
      }
      // 严格按 agent.skills 顺序
      map[a.id] = (a.skills || []).filter((n) => visible.includes(n));
    }
    return map;
  }, [agents, skills]);

  return (
    <div className="h-full space-y-2 overflow-y-auto px-4 py-3.5">
      <div className="rounded-card border border-brand/20 bg-brand-soft/40 px-3 py-2.5 text-[11.5px] leading-relaxed text-brand">
        <div className="font-semibold">「深度研究团队」</div>
        <div className="mt-1 text-mute">
          Planner 拆解任务 → 5 位专家（event_scout / market_analyst / fundamentals_analyst / deep_researcher / predictor）串行执行
          → 复核员（verifier）把关事实 → 报告员（report_writer）成文。
        </div>
      </div>
      {agents.map((a) => {
        const visible = visibleByAgent[a.id] || [];
        return (
          <div
            key={a.id}
            className="rounded-card border border-edge bg-card px-3.5 py-3 shadow-card transition-colors hover:border-edgeDark"
          >
            <div className="flex items-center gap-2.5">
              <span
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[13px] font-bold text-white"
                style={{ backgroundColor: a.avatar_color || "#6B6862" }}
              >
                {(a.name || a.id).slice(0, 1)}
              </span>
              <div className="min-w-0 flex-1">
                <p className="text-[13px] font-semibold text-ink">{a.name}</p>
                <p className="font-mono text-[10.5px] text-faint">{a.id}</p>
              </div>
              <span
                className="rounded-full px-2 py-0.5 text-[10.5px] font-medium"
                style={{ color: a.avatar_color || "#6B6862", backgroundColor: `${a.avatar_color || "#6B6862"}14` }}
              >
                {visible.length > 0 ? `${visible.length} 技能` : "无技能"}
              </span>
            </div>
            <p className="mt-2 text-[12px] leading-relaxed text-mute">{a.description}</p>
            {visible.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {visible.map((sk) => (
                  <span
                    key={sk}
                    className="rounded bg-jade-soft/60 px-1.5 py-0.5 text-[10.5px] text-jade"
                    title="skill（对 LLM 可见）"
                  >
                    {skillCn(sk)}
                  </span>
                ))}
              </div>
            )}
          </div>
        );
      })}
      {agents.length === 0 && <p className="py-8 text-center text-[12.5px] text-faint">团队信息加载失败或后端未启动</p>}
    </div>
  );
}

/* ---------------- 面板主体 ---------------- */

const TABS = [
  { id: "artifacts" as const, label: "产出物", icon: <FolderOpen size={14} /> },
  { id: "skills" as const, label: "技能", icon: <Wrench size={14} /> },
  { id: "team" as const, label: "团队", icon: <Users size={14} /> },
  { id: "logic" as const, label: "逻辑库", icon: <Beaker size={14} /> },
];

export default function RightPanel() {
  const rightTab = useStore((s) => s.rightTab);
  const setRightTab = useStore((s) => s.setRightTab);
  const rightOpen = useStore((s) => s.rightOpen);
  const setRightOpen = useStore((s) => s.setRightOpen);
  const selectedArtifactId = useStore((s) => s.selectedArtifactId);
  const artifact = useStore((s) => s.artifacts.find((a) => a.id === s.selectedArtifactId));
  const artifactCount = useStore((s) => s.artifacts.length);
  const logicCount = useStore((s) => s.logicLibrary.length);
  const pendingLogicCount = useStore(
    (s) => s.logicLibrary.filter((x) => x.status === "pending").length,
  );

  // 折叠态：细栏
  if (!rightOpen) {
    return (
      <aside className="flex h-full w-11 shrink-0 flex-col items-center gap-1 border-l border-edge bg-[#F4F2EE] py-2">
        <button
          onClick={() => setRightOpen(true)}
          title="展开工作台面板"
          className="rounded-lg p-2 text-mute transition-colors hover:bg-card hover:text-ink"
        >
          <PanelRightOpen size={16} />
        </button>
        <div className="my-1 h-px w-6 bg-edge" />
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setRightTab(t.id)}
            title={t.label}
            className={cls(
              "relative rounded-lg p-2 transition-colors",
              rightTab === t.id ? "bg-card text-jade shadow-card" : "text-mute hover:bg-card hover:text-ink",
            )}
          >
            {t.icon}
            {t.id === "artifacts" && artifactCount > 0 && (
              <span className="absolute right-0.5 top-0.5 h-1.5 w-1.5 rounded-full bg-brand" />
            )}
            {t.id === "logic" && pendingLogicCount > 0 && (
              <span className="absolute right-0.5 top-0.5 flex h-3.5 min-w-[14px] items-center justify-center rounded-full bg-brand px-1 text-[9px] font-semibold text-card">
                {pendingLogicCount}
              </span>
            )}
          </button>
        ))}
      </aside>
    );
  }

  return (
    <aside className="absolute right-0 top-0 z-30 flex h-full w-[400px] flex-col border-l border-edge bg-[#FBFAF8] shadow-[-8px_0_24px_-12px_rgba(0,0,0,0.18)]">
      {/* tab 头 */}
      <div className="flex items-center gap-1 border-b border-edge px-2.5 py-2">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setRightTab(t.id)}
            className={cls(
              "relative flex flex-1 items-center justify-center gap-1.5 rounded-lg border border-transparent px-2 py-1.5 text-[12px] font-medium transition-colors",
              rightTab === t.id
                ? "bg-card text-ink shadow-card border-edge"
                : "text-mute hover:bg-card/60 hover:text-ink",
            )}
          >
            {t.icon}
            <span className="truncate">{t.label}</span>
            {t.id === "artifacts" && artifactCount > 0 && (
              <span className="rounded-full bg-brand-soft px-1.5 text-[10px] font-semibold text-brand">
                {artifactCount}
              </span>
            )}
            {t.id === "logic" && logicCount > 0 && (
              <span
                className={cls(
                  "rounded-full px-1.5 text-[10px] font-semibold",
                  pendingLogicCount > 0 ? "bg-brand text-card" : "bg-[#F4F2EE] text-faint",
                )}
              >
                {pendingLogicCount > 0 ? `${pendingLogicCount}/${logicCount}` : logicCount}
              </span>
            )}
          </button>
        ))}
        <button
          onClick={() => setRightOpen(false)}
          title="折叠面板"
          className="ml-auto rounded-lg p-1.5 text-faint transition-colors hover:bg-card hover:text-ink"
        >
          <PanelRightClose size={15} />
        </button>
      </div>

      {/* 内容 */}
      <div className="min-h-0 flex-1">
        {rightTab === "artifacts" &&
          (artifact && selectedArtifactId ? <ArtifactDetail artifact={artifact} /> : <ArtifactList />)}
        {rightTab === "skills" && <SkillsTab />}
        {rightTab === "team" && <TeamTab />}
        {rightTab === "logic" && <LogicLibraryTab />}
      </div>
    </aside>
  );
}
