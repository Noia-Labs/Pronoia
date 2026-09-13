import { useEffect } from "react";
import Sidebar from "./components/Sidebar";
import ChatPanel from "./components/ChatPanel";
import RightPanel from "./components/RightPanel";
import BacktestDetail from "./components/BacktestDetail";
import ArenaList from "./components/ArenaList";
import ArenaDetail from "./components/ArenaDetail";
import LiveLogPanel from "./components/LiveLogPanel";
import ProspectiveList from "./components/ProspectiveList";
import ProspectiveDetail from "./components/ProspectiveDetail";
import { useStore } from "./store";
import BacktestEventPage from "./pages/BacktestEventPage";
import BacktestQuantPage from "./pages/BacktestQuantPage";
import BacktestRunsPage from "./pages/BacktestRunsPage";
import DataManagementPage from "./pages/DataManagementPage";
import ModelLabPage from "./pages/ModelLabPage";

export default function App() {
  const init = useStore((s) => s.init);
  const view = useStore((s) => s.view);
  const syncRouteFromLocation = useStore((s) => s.syncRouteFromLocation);
  const liveLogOpen = useStore((s) => s.liveLogOpen);
  useEffect(() => {
    void init();
  }, [init]);

  useEffect(() => {
    // `/backtest` 是旧入口：保留书签兼容，同时把地址规范到运行记录首页。
    if (window.location.pathname.replace(/\/+$/, "") === "/backtest") {
      window.history.replaceState({}, "", "/backtest/runs");
      syncRouteFromLocation();
    }
    const onPopState = () => syncRouteFromLocation();
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [syncRouteFromLocation]);

  return (
    <div className="flex h-screen w-full overflow-hidden bg-paper font-sans text-ink antialiased">
      <Sidebar />
      <div className="relative flex min-w-0 flex-1 overflow-hidden">
        {view === "chat" && (
          <>
            <ChatPanel />
            <RightPanel />
          </>
        )}
        {view === "backtest-event" && <BacktestEventPage />}
        {view === "backtest-quant" && <BacktestQuantPage />}
        {(view === "backtest-runs" || view === "backtest-list") && <BacktestRunsPage />}
        {view === "backtest-model-lab" && <ModelLabPage />}
        {view === "backtest-data" && <DataManagementPage />}
        {view === "backtest-detail" && <BacktestDetail />}
        {view === "arena-list" && <ArenaList />}
        {view === "arena-detail" && <ArenaDetail />}
        {view === "prospective-list" && <ProspectiveList />}
        {view === "prospective-detail" && <ProspectiveDetail />}
        {liveLogOpen && <LiveLogPanel />}
      </div>
    </div>
  );
}
