import {
  ArrowLeft,
  BarChart3,
  Clock3,
  Database,
  FlaskConical,
  Newspaper,
  Swords,
} from "lucide-react";
import { useStore, type ViewName } from "../../store";
import { cls } from "../../utils";

const PRIMARY_TABS: Array<{
  view: ViewName;
  label: string;
  index: string;
  icon: typeof Newspaper;
  activeClass: string;
}> = [
  { view: "backtest-runs", label: "运行记录", index: "01", icon: Clock3, activeClass: "bg-edge/70 text-ink" },
  { view: "backtest-event", label: "事件模型", index: "02", icon: Newspaper, activeClass: "bg-brand-soft text-brand" },
  { view: "backtest-quant", label: "量化模型", index: "03", icon: BarChart3, activeClass: "bg-jade-soft text-jade" },
  { view: "backtest-model-lab", label: "Pronoia 基模评测", index: "04", icon: FlaskConical, activeClass: "bg-violet-soft text-violet" },
];

export default function BacktestNav({ actions }: { actions?: React.ReactNode }) {
  const view = useStore((state) => state.view);
  const setView = useStore((state) => state.setView);

  return (
    <header className="shrink-0 border-b border-edge bg-card/90 backdrop-blur">
      <div className="flex min-h-12 items-center justify-between gap-3 px-5 py-2 sm:px-7">
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            onClick={() => setView("chat")}
            className="inline-flex shrink-0 items-center gap-1 rounded-md px-1.5 py-1 text-[10.5px] text-mute transition hover:bg-edge/50 hover:text-ink"
          >
            <ArrowLeft size={12} /> 研究工作台
          </button>
          <span className="hidden text-faint sm:block">/</span>
          <span className="hidden text-[10.5px] font-semibold text-ink sm:block">回测</span>
        </div>
        <div className="flex items-center gap-2">
          {actions}
          <button
            type="button"
            onClick={() => setView("arena-list")}
            className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[11px] font-medium text-mute transition hover:border-violet/25 hover:bg-violet-soft/45 hover:text-violet"
          >
            <Swords size={12} /> Arena
          </button>
        </div>
      </div>

      <div className="overflow-x-auto overflow-y-hidden px-5 sm:px-7">
        <nav aria-label="回测主页面" className="flex min-w-max items-end gap-1">
          {PRIMARY_TABS.map((tab) => {
            const active = view === tab.view || (tab.view === "backtest-runs" && (view === "backtest-list" || view === "backtest-detail"));
            const Icon = tab.icon;
            return (
              <button
                key={tab.view}
                type="button"
                onClick={() => setView(tab.view)}
                aria-current={active ? "page" : undefined}
                className={cls(
                  "group relative flex items-center gap-2 rounded-t-lg border-x border-t px-3.5 py-2.5 text-left transition",
                  active
                    ? `border-edge ${tab.activeClass}`
                    : "border-transparent text-mute hover:border-edge/70 hover:bg-paper hover:text-ink",
                )}
              >
                <span className="font-mono text-[8.5px] opacity-55">{tab.index}</span>
                <Icon size={13} />
                <span className="text-[11.5px] font-semibold">{tab.label}</span>
                {active && <span className="absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-current opacity-65" />}
              </button>
            );
          })}
          <span className="mx-1 mb-2.5 h-4 w-px bg-edge" />
          <button
            type="button"
            onClick={() => setView("backtest-data")}
            aria-current={view === "backtest-data" ? "page" : undefined}
            className={cls(
              "mb-1 inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-[10.5px] font-medium transition",
              view === "backtest-data" ? "bg-ink text-card" : "text-mute hover:bg-edge/60 hover:text-ink",
            )}
          >
            <Database size={11} /> 数据管理
          </button>
        </nav>
      </div>
    </header>
  );
}
