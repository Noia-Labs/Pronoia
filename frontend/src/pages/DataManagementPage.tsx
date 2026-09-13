import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  AlertTriangle,
  BookOpen,
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  Database,
  FileJson2,
  FileSpreadsheet,
  Loader2,
  Plus,
  RefreshCw,
  ShieldCheck,
} from "lucide-react";
import { api } from "../api";
import BacktestNav from "../components/backtest/BacktestNav";
import QuestionLibraryPanel from "./QuestionLibraryPanel";
import type { BTDataset, BTDatasetRegisterInput } from "../types";
import { cls } from "../utils";

type DatasetKind = "event" | "market";
type DataTab = DatasetKind | "questions";

const INPUT = "w-full rounded-lg border border-edgeDark/80 bg-card px-3 py-2 text-[11.5px] text-ink outline-none transition placeholder:text-faint focus:border-brand/60 focus:ring-2 focus:ring-brand/10";

function datasetState(dataset: BTDataset): "available" | "pending" | "failed" {
  const value = String(dataset.quality_status ?? dataset.status ?? dataset.oracle_status ?? "pending").toLowerCase();
  if (["failed", "invalid", "unavailable"].includes(value)) return "failed";
  if (["passed", "partial", "available"].includes(value)) return "available";
  return "pending";
}

function StateBadge({ dataset }: { dataset: BTDataset }) {
  const state = datasetState(dataset);
  const config = state === "available"
    ? { label: "已就绪", icon: <CheckCircle2 size={10} />, style: "border-jade/20 bg-jade-soft text-jade" }
    : state === "failed"
      ? { label: "校验失败", icon: <AlertTriangle size={10} />, style: "border-rise/20 bg-rise/5 text-rise" }
      : { label: "待校验", icon: <CircleDashed size={10} />, style: "border-amber-200 bg-amber-50 text-amber-700" };
  return <span className={cls("inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[9px] font-medium", config.style)}>{config.icon}{config.label}</span>;
}

function Field({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="flex items-center justify-between gap-2 text-[10.5px] font-medium text-ink">
        {label}{hint && <span className="text-[9px] font-normal text-faint">{hint}</span>}
      </span>
      <span className="mt-1.5 block">{children}</span>
    </label>
  );
}

function ImportPanel({ kind, onCreated }: { kind: DatasetKind; onCreated: (dataset: BTDataset) => void }) {
  const [name, setName] = useState("");
  const [path, setPath] = useState("");
  const [market, setMarket] = useState("CN");
  const [frequency, setFrequency] = useState("1d");
  const [assetType, setAssetType] = useState("index");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setName("");
    setPath("");
    setError(null);
  }, [kind]);

  const canSave = Boolean(name.trim() && path.trim()) && !saving;
  const schemaMapping = kind === "event"
    ? { event_id: "event_id", occurred_at: "event_time", available_time: "available_time", market: "market", symbol: "symbol", title: "title", event_text: "event_text" }
    : { timestamp: "timestamp", open: "open", high: "high", low: "low", close: "close", volume: "volume" };

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    setError(null);
    try {
      const source = { type: "local_file", provider: "local", ref: path.trim() };
      const registration: BTDatasetRegisterInput = {
        name: name.trim(),
        dataset_kind: kind,
        path: path.trim(),
        source,
        markets: [market],
        asset_type: kind === "market" ? assetType : null,
        frequency: kind === "market" ? frequency : null,
        adjustment: kind === "market" ? "qfq" : null,
        calendar: kind === "market" ? "exchange" : null,
        schema_mapping: schemaMapping,
        capabilities: kind === "market" ? { ohlc: true, volume: true, supported_frequencies: [frequency] } : null,
        status: "pending",
      };
      const registered = await api.btRegisterDataset(registration);
      const resolved = await api.btCreateDatasetVersion(registered.id, {
        path: path.trim(),
        source,
        schema_mapping: schemaMapping,
      });
      onCreated(resolved);
      setName("");
      setPath("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="overflow-hidden rounded-xl border border-edge bg-card shadow-card">
      <div className={cls("border-b px-5 py-4", kind === "event" ? "border-brand/15 bg-brand-soft/30" : "border-jade/15 bg-jade-soft/35")}>
        <div className="flex items-start justify-between gap-3">
          <div>
            <p className={cls("text-[9px] font-semibold uppercase tracking-[0.18em]", kind === "event" ? "text-brand" : "text-jade")}>Import wizard</p>
            <h2 className="mt-1 font-serif text-[15px] font-semibold text-ink">导入{kind === "event" ? "事件库" : "交易数据"}</h2>
            <p className="mt-1 text-[9.5px] text-mute">注册来源后由后端校验并冻结不可变版本。</p>
          </div>
          <span className={cls("grid h-9 w-9 place-items-center rounded-xl", kind === "event" ? "bg-brand-soft text-brand" : "bg-jade-soft text-jade")}>
            {kind === "event" ? <FileJson2 size={17} /> : <FileSpreadsheet size={17} />}
          </span>
        </div>
      </div>
      <div className="p-5">
        <div className="mb-5 grid grid-cols-4 gap-1.5">
          {["选择来源", "字段契约", "数据校验", "冻结版本"].map((step, index) => (
            <div key={step} className="flex min-w-0 items-center gap-1.5">
              <span className={cls("grid h-5 w-5 shrink-0 place-items-center rounded-full font-mono text-[8px]", index === 0 ? "bg-ink text-card" : "bg-edge/60 text-mute")}>{index + 1}</span>
              <span className="truncate text-[9px] text-mute">{step}</span>
              {index < 3 && <ChevronRight size={9} className="ml-auto shrink-0 text-faint" />}
            </div>
          ))}
        </div>

        <div className="mb-4 flex items-center gap-2.5 rounded-lg border border-edge bg-paper px-3.5 py-3">
          <span className="grid h-8 w-8 shrink-0 place-items-center rounded-lg bg-card text-mute shadow-card"><FileSpreadsheet size={13} /></span>
          <div><p className="text-[10.5px] font-semibold text-ink">后端可读文件</p><p className="mt-0.5 text-[9px] text-faint">当前只展示已接通校验与冻结流程的导入方式</p></div>
        </div>

        <div className="space-y-3.5">
          <Field label="数据集名称"><input value={name} onChange={(event) => setName(event.target.value)} className={INPUT} placeholder={kind === "event" ? "例如：中美事件库 · 2026Q3" : "例如：沪深300日线 · 2026Q3"} /></Field>
          <Field label="后端可读的绝对路径" hint={kind === "event" ? "JSONL" : "CSV"}>
            <input value={path} onChange={(event) => setPath(event.target.value)} className={INPUT} placeholder={kind === "event" ? "/data/events.jsonl" : "/data/market-bars.csv"} />
          </Field>
          <div className="grid gap-3 sm:grid-cols-3">
            <Field label="市场"><select value={market} onChange={(event) => setMarket(event.target.value)} className={INPUT}><option value="CN">中国 A 股</option><option value="US">美国</option><option value="HK">中国香港</option><option value="FUTURES">期货</option></select></Field>
            {kind === "market" && <Field label="频率"><select value={frequency} onChange={(event) => setFrequency(event.target.value)} className={INPUT}><option value="1d">日线</option><option value="1m">1 分钟</option><option value="5m">5 分钟</option><option value="60m">60 分钟</option></select></Field>}
            {kind === "market" && <Field label="资产类型"><select value={assetType} onChange={(event) => setAssetType(event.target.value)} className={INPUT}><option value="index">指数</option><option value="equity">股票</option><option value="futures">期货</option></select></Field>}
          </div>
          <div className="rounded-lg border border-edge bg-paper px-3.5 py-3">
            <p className="text-[9px] font-semibold uppercase tracking-wider text-faint">当前字段契约</p>
            <p className="mt-1.5 font-mono text-[9.5px] leading-relaxed text-mute">{Object.entries(schemaMapping).map(([field, source]) => `${field}←${source}`).join("  ·  ")}</p>
          </div>
          {error && <div className="flex items-start gap-2 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[10px] text-rise"><AlertTriangle size={12} className="mt-0.5 shrink-0" />{error}</div>}
          <button type="button" onClick={() => void save()} disabled={!canSave} className="inline-flex w-full items-center justify-center gap-1.5 rounded-lg bg-ink px-4 py-2.5 text-[11.5px] font-semibold text-card transition hover:bg-ink/90 disabled:cursor-not-allowed disabled:opacity-40">
            {saving ? <Loader2 size={13} className="animate-spin" /> : <ShieldCheck size={13} />}
            {saving ? "正在注册并校验…" : "注册、校验并冻结版本"}
          </button>
        </div>
      </div>
    </section>
  );
}

export default function DataManagementPage() {
  const [kind, setKind] = useState<DataTab>(() => {
    const tab = new URLSearchParams(window.location.search).get("tab");
    return tab === "questions" || tab === "market" ? tab : "event";
  });
  const [datasets, setDatasets] = useState<BTDataset[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const selectTab = (tab: DataTab) => {
    setKind(tab);
    const url = new URL(window.location.href);
    if (tab === "event") url.searchParams.delete("tab");
    else url.searchParams.set("tab", tab);
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
  };

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setDatasets(await api.btListDatasets());
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { void load(); }, []);
  const visible = useMemo(() => datasets.filter((item) => kind === "market" ? item.dataset_kind === "market" : item.dataset_kind !== "market"), [datasets, kind]);
  const available = visible.filter((item) => datasetState(item) === "available").length;

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col overflow-hidden bg-paper">
      <BacktestNav actions={kind !== "questions" ? <button type="button" onClick={() => void load()} disabled={loading} className="inline-flex items-center gap-1.5 rounded-lg border border-edge bg-card px-3 py-2 text-[11px] font-medium text-mute transition hover:text-ink disabled:opacity-50"><RefreshCw size={12} className={cls(loading && "animate-spin")} />刷新</button> : undefined} />
      <main className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto w-full max-w-[1500px] px-5 py-6 sm:px-7 lg:px-9">
          <div className="flex flex-col justify-between gap-4 md:flex-row md:items-end">
            <div>
              <p className="text-[9px] font-semibold uppercase tracking-[0.2em] text-mute">Dataset registry</p>
              <h1 className="mt-2 font-serif text-[27px] font-semibold text-ink">数据管理</h1>
              <p className="mt-1.5 max-w-2xl text-[11px] leading-relaxed text-mute">统一管理事件库、交易数据和问答问题库，为预测回测与模型评测准备数据。</p>
            </div>
            <div className="flex items-center gap-2 rounded-xl border border-edge bg-card px-3.5 py-2.5 shadow-card"><Database size={14} className="text-jade" /><div><p className="text-[9px] text-faint">已注册</p><p className="font-mono text-[13px] font-semibold text-ink">{datasets.length} 个数据集</p></div></div>
          </div>

          <div role="tablist" aria-label="数据类型" className="mt-6 grid grid-cols-3 gap-2 rounded-xl border border-edge bg-card p-1.5 shadow-card">
            <button role="tab" aria-selected={kind === "event"} type="button" onClick={() => selectTab("event")} className={cls("flex items-center justify-between rounded-lg px-3 py-3 text-left transition sm:px-4", kind === "event" ? "bg-brand-soft/70 text-brand" : "text-mute hover:bg-paper hover:text-ink")}><span className="inline-flex items-center gap-2 text-[12px] font-semibold"><FileJson2 size={14} />事件库</span><span className="hidden font-mono text-[10px] sm:inline">{datasets.filter((item) => item.dataset_kind !== "market").length}</span></button>
            <button role="tab" aria-selected={kind === "market"} type="button" onClick={() => selectTab("market")} className={cls("flex items-center justify-between rounded-lg px-3 py-3 text-left transition sm:px-4", kind === "market" ? "bg-jade-soft/80 text-jade" : "text-mute hover:bg-paper hover:text-ink")}><span className="inline-flex items-center gap-2 text-[12px] font-semibold"><FileSpreadsheet size={14} />交易数据</span><span className="hidden font-mono text-[10px] sm:inline">{datasets.filter((item) => item.dataset_kind === "market").length}</span></button>
            <button role="tab" aria-selected={kind === "questions"} type="button" onClick={() => selectTab("questions")} className={cls("flex items-center justify-between rounded-lg px-3 py-3 text-left transition sm:px-4", kind === "questions" ? "bg-violet-soft/80 text-violet" : "text-mute hover:bg-paper hover:text-ink")}><span className="inline-flex items-center gap-2 text-[12px] font-semibold"><BookOpen size={14} />问题库</span><span className="hidden text-[9px] sm:inline">问答评测</span></button>
          </div>

          {kind === "questions" ? <QuestionLibraryPanel /> : <div className="mt-4 grid items-start gap-4 xl:grid-cols-[minmax(0,1fr)_420px]">
            <section className="overflow-hidden rounded-xl border border-edge bg-card shadow-card">
              <div className="flex items-center justify-between border-b border-edge px-5 py-4">
                <div><h2 className="font-serif text-[15px] font-semibold text-ink">{kind === "event" ? "事件库" : "交易数据"}版本</h2><p className="mt-0.5 text-[9.5px] text-faint">已就绪 {available} / {visible.length}</p></div>
                <span className="inline-flex items-center gap-1 rounded-full border border-edge bg-paper px-2 py-1 text-[9px] text-mute"><ShieldCheck size={9} />版本不可变</span>
              </div>
              {loading ? (
                <div className="grid min-h-48 place-items-center text-[11px] text-mute"><span className="inline-flex items-center gap-2"><Loader2 size={14} className="animate-spin" />正在读取数据目录…</span></div>
              ) : error ? (
                <div className="m-5 flex items-start gap-2 rounded-lg border border-rise/20 bg-rise/5 px-3 py-3 text-[10.5px] text-rise"><AlertTriangle size={13} />{error}</div>
              ) : visible.length === 0 ? (
                <div className="grid min-h-52 place-items-center px-6 text-center"><div><span className="mx-auto grid h-10 w-10 place-items-center rounded-xl bg-edge/55 text-mute"><Plus size={16} /></span><p className="mt-3 text-[12px] font-semibold text-ink">还没有{kind === "event" ? "事件库" : "交易数据"}</p><p className="mt-1 text-[10px] text-mute">使用右侧导入向导注册第一个来源。</p></div></div>
              ) : (
                <div className="divide-y divide-edge">
                  {visible.map((dataset) => (
                    <article key={dataset.id} className="px-5 py-4 transition hover:bg-paper/70">
                      <div className="flex items-start justify-between gap-3">
                        <div className="min-w-0"><div className="flex items-center gap-2"><h3 className="truncate text-[11.5px] font-semibold text-ink">{dataset.name}</h3><StateBadge dataset={dataset} /></div><p className="mt-1 truncate font-mono text-[9px] text-faint">{dataset.id}</p></div>
                        <span className="shrink-0 rounded-md border border-edge bg-paper px-2 py-1 font-mono text-[9px] text-mute">{dataset.dataset_version ?? dataset.version ?? "未冻结"}</span>
                      </div>
                      <div className="mt-3 grid gap-2 sm:grid-cols-4">
                        <DataCell label="市场" value={(dataset.markets ?? Object.keys(dataset.by_market ?? {})).join(" / ") || "未声明"} />
                        <DataCell label={kind === "event" ? "事件" : "频率"} value={kind === "event" ? String(dataset.total_events ?? 0) : dataset.frequency ?? "未声明"} />
                        <DataCell label="覆盖" value={dataset.coverage?.start_at && dataset.coverage?.end_at ? `${String(dataset.coverage.start_at).slice(0, 10)} → ${String(dataset.coverage.end_at).slice(0, 10)}` : dataset.date_range?.min && dataset.date_range?.max ? `${dataset.date_range.min} → ${dataset.date_range.max}` : "未返回"} />
                        <DataCell label="快照" value={(dataset.snapshot_hash ?? dataset.snapshot ?? "未生成").slice(0, 12)} mono />
                      </div>
                    </article>
                  ))}
                </div>
              )}
            </section>
            <ImportPanel kind={kind} onCreated={(dataset) => setDatasets((items) => [dataset, ...items.filter((item) => item.id !== dataset.id)])} />
          </div>}
        </div>
      </main>
    </div>
  );
}

function DataCell({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return <div className="min-w-0 rounded-lg border border-edge bg-paper px-2.5 py-2"><p className="text-[8px] uppercase tracking-wider text-faint">{label}</p><p title={value} className={cls("mt-1 truncate text-[9.5px] font-medium text-ink", mono && "font-mono")}>{value}</p></div>;
}
