import { useState } from "react";
import { Beaker, LineChart, MessageSquareText, PanelLeftClose, PanelLeftOpen, Plus, Swords, Terminal, Trash2, Users, Wrench, Radio } from "lucide-react";
import { relTime, cls } from "../utils";
import { useStore, ViewName } from "../store";
import { VERSION } from "../version";

/** 左栏：logo + 新研究 + case 列表 + 底部技能/团队入口。
 *  支持折叠为 w-11 细栏（与右栏对称）：保留 logo 缩写 / 新研究 / 案例计数 / 底部 tab 入口。 */
export default function Sidebar() {
  const cases = useStore((s) => s.cases);
  const currentCaseId = useStore((s) => s.currentCaseId);
  const loadCase = useStore((s) => s.loadCase);
  const newCase = useStore((s) => s.newCase);
  const deleteCase = useStore((s) => s.deleteCase);
  const setRightTab = useStore((s) => s.setRightTab);
  const rightTab = useStore((s) => s.rightTab);
  const setRightOpen = useStore((s) => s.setRightOpen);
  const leftOpen = useStore((s) => s.leftOpen);
  const setLeftOpen = useStore((s) => s.setLeftOpen);
  const streaming = useStore((s) => s.streaming);
  const pendingLogicCount = useStore(
    (s) => s.logicLibrary.filter((x) => x.status === "pending").length,
  );
  const view = useStore((s) => s.view);
  const setView = useStore((s) => s.setView);
  const backendStatus = useStore((s) => s.backendStatus);
  const liveLogOpen = useStore((s) => s.liveLogOpen);
  const toggleLiveLog = useStore((s) => s.toggleLiveLog);
  const [confirmId, setConfirmId] = useState<string | null>(null);

  const inBacktestView = view.startsWith("backtest-");
  const inArenaView = view.startsWith("arena-");
  const inProspectiveView = view.startsWith("prospective-");
  const inLabView = inBacktestView || inArenaView || inProspectiveView;
  const goView = (v: ViewName) => {
    if (streaming && v !== "chat") return; // 流式中禁止切走
    setView(v);
    // 实验页面需要宽幅图表与协议配置；进入时收起全局案例栏，回到研究时恢复。
    setLeftOpen(v === "chat");
  };

  const goTab = (t: "skills" | "team" | "logic") => {
    setRightTab(t);
    setRightOpen(true);
  };

  const statusTip = backendStatus === "online" ? "运行中" : "后端断开";

  // 折叠态：细栏
  if (!leftOpen) {
    return (
      <aside className="flex h-full w-11 shrink-0 flex-col items-center gap-1 border-r border-edge bg-[#F4F2EE] py-2">
        <button
          onClick={() => setLeftOpen(true)}
          title="展开侧栏"
          className="rounded-lg p-2 text-mute transition-colors hover:bg-card hover:text-ink"
        >
          <PanelLeftOpen size={16} />
        </button>
        <div className="my-1 h-px w-6 bg-edge" />
        {/* logo 缩写 */}
        <div
          className="font-serif text-[15px] font-bold text-ink"
          title={`Pronoia · ${VERSION}`}
        >
          P
        </div>
        {/* 研究工作台 */}
        <button
          onClick={() => goView("chat")}
          title={inLabView ? "切换到研究工作台" : "研究工作台"}
          className={cls(
            "rounded-lg p-2 transition-colors",
            !inLabView ? "bg-card text-brand shadow-card" : "text-mute hover:bg-card hover:text-ink",
          )}
        >
          <MessageSquareText size={16} />
        </button>
        {/* 回测中心 */}
        <button
          onClick={() => goView("backtest-runs")}
          title={inBacktestView ? "回测中心（当前）" : "切换到回测中心"}
          className={cls(
            "rounded-lg p-2 transition-colors",
            inBacktestView ? "bg-card text-jade shadow-card" : "text-mute hover:bg-card hover:text-ink",
          )}
        >
          <LineChart size={16} />
        </button>
        {/* Arena 比对 */}
        <button
          onClick={() => goView("arena-list")}
          title={inArenaView ? "Arena 比对（当前）" : "切换到 Arena 横向比对"}
          className={cls(
            "rounded-lg p-2 transition-colors",
            inArenaView ? "bg-card text-violet shadow-card" : "text-mute hover:bg-card hover:text-ink",
          )}
        >
          <Swords size={16} />
        </button>
        <button
          onClick={() => goView("prospective-list")}
          title={inProspectiveView ? "前瞻评测（当前）" : "切换到前瞻评测"}
          className={cls(
            "rounded-lg p-2 transition-colors",
            inProspectiveView ? "bg-card text-amber shadow-card" : "text-mute hover:bg-card hover:text-ink",
          )}
        >
          <Radio size={16} />
        </button>
        {/* 新研究 */}
        <button
          onClick={newCase}
          title="新研究（仅研究工作台）"
          className={cls(
            "rounded-lg p-2 transition-colors",
            !inLabView ? "text-brand hover:bg-card" : "text-faint/50",
          )}
          disabled={inLabView}
        >
          <Plus size={16} />
        </button>
        {/* 案例计数 */}
        {cases.length > 0 && !inLabView && (
          <span
            className="rounded-full bg-brand-soft px-1.5 text-[9.5px] font-semibold text-brand"
            title={`${cases.length} 个研究案例`}
          >
            {cases.length}
          </span>
        )}
        <div className="flex-1" />
        {/* 底部 tab 入口（与右栏折叠态对齐） */}
        <button
          onClick={() => goTab("skills")}
          title="技能"
          className={cls(
            "rounded-lg p-2 transition-colors",
            rightTab === "skills" ? "bg-card text-jade shadow-card" : "text-mute hover:bg-card hover:text-ink",
          )}
        >
          <Wrench size={14} />
        </button>
        <button
          onClick={() => goTab("team")}
          title="团队"
          className={cls(
            "rounded-lg p-2 transition-colors",
            rightTab === "team" ? "bg-card text-jade shadow-card" : "text-mute hover:bg-card hover:text-ink",
          )}
        >
          <Users size={14} />
        </button>
        <button
          onClick={() => goTab("logic")}
          title={`逻辑库${pendingLogicCount > 0 ? ` · ${pendingLogicCount} 条待验证` : ""}`}
          className={cls(
            "relative rounded-lg p-2 transition-colors",
            rightTab === "logic" ? "bg-card text-jade shadow-card" : "text-mute hover:bg-card hover:text-ink",
          )}
        >
          <Beaker size={14} />
          {pendingLogicCount > 0 && (
            <span className="absolute -right-0.5 -top-0.5 flex h-3 min-w-[12px] items-center justify-center rounded-full bg-brand px-1 text-[8.5px] font-semibold text-card">
              {pendingLogicCount}
            </span>
          )}
        </button>
        {/* LLM 调用日志 */}
        <button
          onClick={toggleLiveLog}
          title="LLM 调用日志"
          className={cls(
            "rounded-lg p-2 transition-colors",
            liveLogOpen ? "bg-card text-brand shadow-card" : "text-mute hover:bg-card hover:text-ink",
          )}
        >
          <Terminal size={14} />
        </button>
        {/* 后端运行状态圆点 */}
        <span
          title={statusTip}
          className={cls(
            "mb-1 mt-1.5 inline-block h-2 w-2 rounded-full",
            backendStatus === "online" ? "bg-jade" : "bg-rise",
          )}
        />
      </aside>
    );
  }

  return (
    <aside className="flex h-full w-[260px] shrink-0 flex-col border-r border-edge bg-[#F4F2EE]">
      {/* Logo */}
      <div className="flex items-start justify-between px-5 pb-4 pt-5">
        <div>
          <div className="flex items-baseline gap-2">
            <h1 className="font-serif text-[22px] font-bold tracking-wide text-ink">Pronoia</h1>
            <span className="rounded bg-jade-soft px-1.5 py-px text-[10px] font-semibold text-jade">{VERSION}</span>
          </div>
          <p className="mt-0.5 font-serif text-[11.5px] italic text-mute">Financial Research Workbench</p>
        </div>
        <button
          onClick={() => setLeftOpen(false)}
          title="折叠侧栏"
          className="rounded-lg p-1.5 text-faint transition-colors hover:bg-card hover:text-ink"
        >
          <PanelLeftClose size={15} />
        </button>
      </div>

      {/* 视图切换：研究工作台 ↔ 回测中心 ↔ Arena 比对 */}
      <div className="mb-2 px-3.5">
        <div className="grid grid-cols-4 gap-1 rounded-lg bg-edge/50 p-1">
          <button
            onClick={() => goView("chat")}
            className={cls(
              "flex items-center justify-center gap-1 rounded-md px-1 py-1.5 text-[11px] font-medium transition-all",
              !inLabView ? "bg-card text-brand shadow-card" : "text-mute hover:bg-card/60 hover:text-ink",
            )}
          >
            <MessageSquareText size={11} />
            研究
          </button>
          <button
            onClick={() => goView("backtest-runs")}
            className={cls(
              "flex items-center justify-center gap-1 rounded-md px-1 py-1.5 text-[11px] font-medium transition-all",
              inBacktestView ? "bg-card text-jade shadow-card" : "text-mute hover:bg-card/60 hover:text-ink",
            )}
          >
            <LineChart size={11} />
            回测
          </button>
          <button
            onClick={() => goView("arena-list")}
            className={cls(
              "flex items-center justify-center gap-1 rounded-md px-1 py-1.5 text-[11px] font-medium transition-all",
              inArenaView ? "bg-card text-violet shadow-card" : "text-mute hover:bg-card/60 hover:text-ink",
            )}
          >
            <Swords size={11} />
            Arena
          </button>
          <button
            onClick={() => goView("prospective-list")}
            className={cls(
              "flex items-center justify-center gap-1 rounded-md px-1 py-1.5 text-[11px] font-medium transition-all",
              inProspectiveView ? "bg-card text-amber shadow-card" : "text-mute hover:bg-card/60 hover:text-ink",
            )}
          >
            <Radio size={11} />
            前瞻
          </button>
        </div>
      </div>

      {/* 新研究（仅 chat 视图显示）*/}
      {!inLabView && (
        <div className="px-3.5">
          <button
            onClick={newCase}
            className="flex w-full items-center justify-center gap-1.5 rounded-card border border-brand/30 bg-card px-3 py-2.5 text-[13px] font-medium text-brand shadow-card transition-all duration-200 hover:bg-brand hover:text-card hover:shadow-pop"
          >
            <Plus size={15} />
            新研究
          </button>
        </div>
      )}

      {/* Case 列表（仅 chat 视图显示）*/}
      {!inLabView && (
        <div className="mt-4 flex-1 overflow-y-auto px-2.5 pb-2">
        <div className="px-2 pb-1.5 text-[11px] font-medium uppercase tracking-wider text-faint">研究案例</div>
        {cases.length === 0 && (
          <div className="mx-2 mt-2 rounded-lg border border-dashed border-edgeDark/70 px-3 py-4 text-center text-[12px] leading-relaxed text-faint">
            还没有研究案例。
            <br />
            提出第一个问题即可自动建档。
          </div>
        )}
        <ul className="space-y-0.5">
          {cases.map((c) => {
            const active = c.id === currentCaseId;
            return (
              <li key={c.id} className="group relative">
                <button
                  onClick={() => {
                    if (!active && !streaming) void loadCase(c.id);
                  }}
                  className={cls(
                    "flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2.5 text-left transition-colors duration-150",
                    active ? "bg-card shadow-card border border-edge" : "border border-transparent hover:bg-card/70",
                  )}
                >
                  <MessageSquareText
                    size={14}
                    className={cls("mt-0.5 shrink-0", active ? "text-brand" : "text-faint")}
                  />
                  <span className="min-w-0 flex-1">
                    <span
                      className={cls(
                        "block truncate text-[13px] leading-snug",
                        active ? "font-medium text-ink" : "text-ink/85",
                      )}
                    >
                      {c.title || "未命名研究"}
                    </span>
                    <span className="mt-0.5 block text-[11px] text-faint">
                      {relTime(c.updated_at)}
                      {(c.message_count ?? 0) > 0 && ` · ${c.message_count} 条`}
                    </span>
                  </span>
                </button>
                {/* 删除（hover 出现，二次确认） */}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    if (confirmId === c.id) {
                      setConfirmId(null);
                      void deleteCase(c.id);
                    } else {
                      setConfirmId(c.id);
                      setTimeout(() => setConfirmId((v) => (v === c.id ? null : v)), 2500);
                    }
                  }}
                  title={confirmId === c.id ? "再次点击确认删除" : "删除"}
                  className={cls(
                    "absolute right-1.5 top-1.5 rounded-md p-1 transition-all",
                    confirmId === c.id
                      ? "bg-rise text-card opacity-100"
                      : "text-faint opacity-0 hover:bg-edge/60 hover:text-rise group-hover:opacity-100",
                  )}
                >
                  <Trash2 size={12.5} />
                </button>
              </li>
            );
          })}
        </ul>
        </div>
      )}

      {/* 回测/Arena 视图下的主体占位：让底部技能入口下沉到下方 */}
      {inLabView && <div className="flex-1" />}

      {/* 底部：技能 / 团队 / 逻辑库 入口 */}
      <div className="border-t border-edge px-2.5 py-2.5">
        <div className="grid grid-cols-3 gap-1.5">
          <button
            onClick={() => goTab("skills")}
            className={cls(
              "flex items-center justify-center gap-1 rounded-lg px-1.5 py-2 text-[11.5px] font-medium transition-colors",
              rightTab === "skills" ? "bg-card text-jade shadow-card border border-edge" : "text-mute hover:bg-card/60",
            )}
          >
            <Wrench size={12} />
            技能
          </button>
          <button
            onClick={() => goTab("team")}
            className={cls(
              "flex items-center justify-center gap-1 rounded-lg px-1.5 py-2 text-[11.5px] font-medium transition-colors",
              rightTab === "team" ? "bg-card text-jade shadow-card border border-edge" : "text-mute hover:bg-card/60",
            )}
          >
            <Users size={12} />
            团队
          </button>
          <button
            onClick={() => goTab("logic")}
            className={cls(
              "relative flex items-center justify-center gap-1 rounded-lg px-1.5 py-2 text-[11.5px] font-medium transition-colors",
              rightTab === "logic" ? "bg-card text-jade shadow-card border border-edge" : "text-mute hover:bg-card/60",
            )}
            title="研究逻辑库：可证伪推演的闭环追踪"
          >
            <Beaker size={12} />
            逻辑库
            {pendingLogicCount > 0 && (
              <span className="absolute -right-0.5 -top-0.5 flex h-3.5 min-w-[14px] items-center justify-center rounded-full bg-brand px-1 text-[9px] font-semibold text-card">
                {pendingLogicCount}
              </span>
            )}
          </button>
        </div>
        <div className="mt-2 flex items-center justify-between px-1">
          <div className="flex items-center gap-1.5" title={statusTip}>
            <span
              className={cls(
                "inline-block h-2 w-2 rounded-full",
                backendStatus === "online" ? "bg-jade" : "bg-rise",
              )}
            />
            <span className="text-[10.5px] text-faint">{statusTip}</span>
          </div>
          <button
            onClick={toggleLiveLog}
            title="LLM 调用日志"
            className={cls(
              "flex items-center gap-1 rounded-md px-1.5 py-1 text-[10.5px] font-medium transition-colors",
              liveLogOpen ? "bg-card text-brand shadow-card" : "text-faint hover:bg-card/60 hover:text-ink",
            )}
          >
            <Terminal size={12} />
            日志
          </button>
        </div>
        <p className="mt-1.5 px-1 text-center text-[10.5px] text-faint">仅供研究，不构成投资建议</p>
      </div>
    </aside>
  );
}
