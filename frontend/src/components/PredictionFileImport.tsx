import { useEffect, useRef, useState, type DragEvent } from "react";
import { AlertTriangle, CheckCircle2, Download, FileSpreadsheet, Loader2, Upload, X } from "lucide-react";
import type { BTDataset } from "../types";
import { cls } from "../utils";

type PredictionRow = {
  event_id: string;
  direction: string;
  confidence: number | null;
  rationale: string;
  horizon: string;
  expected_return_pct?: number | null;
};

type ImportIssue = {
  row: number | null;
  event_id: string | null;
  field: string | null;
  message: string;
};

type PredictionPreview = {
  valid: boolean;
  dataset_id: string;
  horizon: string;
  event_count: number;
  total_rows: number;
  matched_count: number;
  return_forecast_count: number;
  missing_count: number;
  unknown_count: number;
  duplicate_count: number;
  missing_ids: string[];
  unknown_ids: string[];
  duplicate_ids: string[];
  format_error_count: number;
  error_count: number;
  errors: ImportIssue[];
  preview: PredictionRow[];
  preview_token: string | null;
  unit: "percent";
};

export type PredictionFileImportProps = {
  datasetId: string;
  horizon: string;
  onImported: (dataset: BTDataset) => void;
  disabled?: boolean;
};

const API_ROOT = "/api/bt/prediction-import";
const BUTTON = "inline-flex min-h-9 items-center justify-center gap-1.5 rounded-lg border border-edgeDark/70 bg-card px-3 py-2 text-[11.5px] font-medium text-ink transition hover:border-brand/40 hover:bg-brand-soft/30 disabled:cursor-not-allowed disabled:opacity-45";

async function readResponse<T>(response: Response): Promise<T> {
  const isJSON = (response.headers.get("content-type") ?? "").includes("json");
  if (!isJSON) {
    throw new Error(response.status === 413 ? "文件过大，请缩小文件后重新选择。" : "导入服务暂时不可用，请刷新页面后重试。");
  }
  const body = await response.json();
  if (!response.ok) {
    const detail = body?.detail;
    if (typeof detail === "string") throw new Error(detail);
    if (Array.isArray(detail)) {
      throw new Error(detail.map((item: { msg?: string }) => item.msg ?? "参数错误").join("；"));
    }
    throw new Error("文件处理失败，请重新检查文件内容后重试。");
  }
  return body as T;
}

function fileSize(size: number): string {
  return size < 1024 * 1024 ? `${Math.max(1, Math.round(size / 1024))} KiB` : `${(size / (1024 * 1024)).toFixed(1)} MiB`;
}

function Stat({ label, value, warning = false }: { label: string; value: number; warning?: boolean }) {
  return <div className="min-w-0 rounded-lg border border-edge bg-card px-3 py-2.5"><p className="text-[10px] text-mute">{label}</p><p className={cls("mt-1 font-mono text-[17px] font-semibold", warning && value > 0 ? "text-rise" : "text-ink")}>{value}</p></div>;
}

function IdList({ label, ids }: { label: string; ids: string[] }) {
  if (ids.length === 0) return null;
  return <p className="break-all text-[11px] leading-relaxed"><span className="font-semibold">{label}：</span>{ids.slice(0, 8).join("、")}{ids.length > 8 && ` 等 ${ids.length} 个`}</p>;
}

export default function PredictionFileImport({ datasetId, horizon, onImported, disabled = false }: PredictionFileImportProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const requestRef = useRef<AbortController | null>(null);
  const generationRef = useRef(0);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<PredictionPreview | null>(null);
  const [phase, setPhase] = useState<"idle" | "preview" | "commit">("idle");
  const [error, setError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [templateFormat, setTemplateFormat] = useState<"csv" | "json" | "jsonl">("csv");
  const [dragging, setDragging] = useState(false);
  const [datasetName, setDatasetName] = useState("");

  useEffect(() => {
    generationRef.current += 1;
    requestRef.current?.abort();
    setFile(null);
    setPreview(null);
    setError(null);
    setDownloadError(null);
    setPhase("idle");
    setDragging(false);
    setDatasetName("");
    if (inputRef.current) inputRef.current.value = "";
    return () => {
      generationRef.current += 1;
      requestRef.current?.abort();
    };
  }, [datasetId, horizon]);

  const busy = phase !== "idle";
  const unavailable = disabled || !datasetId || busy;
  const alignedPreview = preview?.dataset_id === datasetId && preview.horizon === horizon ? preview : null;

  const clearFile = () => {
    generationRef.current += 1;
    requestRef.current?.abort();
    setFile(null);
    setPreview(null);
    setError(null);
    setPhase("idle");
    setDatasetName("");
    if (inputRef.current) inputRef.current.value = "";
  };

  const makeForm = (selectedFile: File) => {
    const form = new FormData();
    form.append("file", selectedFile);
    form.append("dataset_id", datasetId);
    form.append("horizon", horizon);
    return form;
  };

  const chooseFile = async (selectedFile: File) => {
    if (unavailable) return;
    const generation = ++generationRef.current;
    requestRef.current?.abort();
    const controller = new AbortController();
    requestRef.current = controller;
    setFile(selectedFile);
    setPreview(null);
    setError(null);
    setDatasetName(`${selectedFile.name.replace(/\.(csv|jsonl?|ndjson)$/i, "")} · 预测结果`);
    if (!/\.(csv|json|jsonl)$/i.test(selectedFile.name)) {
      setError("请选择 CSV、JSON 或 JSONL 文件。Excel 表格请先另存为 CSV UTF-8。");
      return;
    }
    if (selectedFile.size === 0) {
      setError("这个文件是空的，请选择包含预测结果的文件。");
      return;
    }
    if (selectedFile.size > 10 * 1024 * 1024) {
      setError("文件超过 10 MiB 上限，请减少文件大小后重新选择。");
      return;
    }
    setPhase("preview");
    try {
      const response = await fetch(`${API_ROOT}/preview`, { method: "POST", body: makeForm(selectedFile), signal: controller.signal });
      const result = await readResponse<PredictionPreview>(response);
      if (generation !== generationRef.current) return;
      setPreview(result);
    } catch (reason) {
      if (generation !== generationRef.current || controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : "无法读取预测文件，请重新选择。");
    } finally {
      if (generation === generationRef.current) setPhase("idle");
    }
  };

  const downloadTemplate = async () => {
    if (unavailable || downloading) return;
    setDownloading(true);
    setDownloadError(null);
    const generation = generationRef.current;
    try {
      const params = new URLSearchParams({ dataset_id: datasetId, horizon, format: templateFormat });
      const response = await fetch(`${API_ROOT}/template?${params}`);
      if (!response.ok || (response.headers.get("content-type") ?? "").includes("text/html")) {
        await readResponse(response);
        throw new Error("暂时无法下载模板，请稍后重试。");
      }
      const blob = await response.blob();
      if (generation !== generationRef.current) return;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `predictions-${datasetId.slice(0, 12)}-${horizon}.${templateFormat}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (reason) {
      if (generation === generationRef.current) setDownloadError(reason instanceof Error ? reason.message : "模板下载失败，请重试。");
    } finally {
      setDownloading(false);
    }
  };

  const commit = async () => {
    if (unavailable || !file || !alignedPreview?.valid || !alignedPreview.preview_token) return;
    const generation = ++generationRef.current;
    const controller = new AbortController();
    requestRef.current = controller;
    setPhase("commit");
    setError(null);
    const form = makeForm(file);
    form.append("preview_token", alignedPreview.preview_token);
    if (datasetName.trim()) form.append("name", datasetName.trim());
    try {
      const response = await fetch(`${API_ROOT}/commit`, { method: "POST", body: form, signal: controller.signal });
      if (generation !== generationRef.current) return;
      if (response.status === 409) setPreview(null);
      const dataset = await readResponse<BTDataset>(response);
      if (generation !== generationRef.current) return;
      onImported(dataset);
    } catch (reason) {
      if (generation !== generationRef.current || controller.signal.aborted) return;
      setError(reason instanceof Error ? reason.message : "导入失败，请重试。");
    } finally {
      if (generation === generationRef.current) setPhase("idle");
    }
  };

  const dropFile = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDragging(false);
    if (unavailable) return;
    if (event.dataTransfer.files.length !== 1) {
      clearFile();
      setError("一次请选择一个预测文件。");
      return;
    }
    void chooseFile(event.dataTransfer.files[0]);
  };

  return (
    <section aria-label="导入本地预测文件" className="min-w-0 space-y-4 rounded-xl border border-brand/20 bg-brand-soft/15 p-4 sm:p-5">
      <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-start">
        <div className="min-w-0">
          <h3 className="flex items-center gap-2 text-[13px] font-semibold text-ink"><FileSpreadsheet size={16} className="shrink-0 text-brand" />导入本地预测文件</h3>
          <p className="mt-1.5 max-w-2xl text-[11px] leading-relaxed text-mute">用于评估模型已经生成的预测结果。下载模板、填写预测并上传，平台会按事件 ID 匹配第 1 步选择的事件集，再预览校验。</p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <select aria-label="预测模板文件格式" value={templateFormat} disabled={unavailable || downloading} onChange={(event) => setTemplateFormat(event.target.value as typeof templateFormat)} className="min-h-9 rounded-lg border border-edgeDark/70 bg-card px-2 py-2 text-[11px] text-ink outline-none focus:border-brand disabled:opacity-45">
            <option value="csv">CSV</option><option value="json">JSON</option><option value="jsonl">JSONL</option>
          </select>
          <button type="button" onClick={() => void downloadTemplate()} disabled={unavailable || downloading} className={BUTTON}>{downloading ? <Loader2 size={13} className="animate-spin" /> : <Download size={13} />}下载预测模板</button>
        </div>
      </div>

      {!datasetId && <p className="flex items-start gap-2 rounded-lg border border-brand/20 bg-card px-3 py-2.5 text-[11px] text-brand"><AlertTriangle size={13} className="mt-0.5 shrink-0" />请先在第 1 步选择事件集，再下载模板或导入预测。</p>}
      {downloadError && <p role="alert" className="break-words text-[11px] text-rise">{downloadError}</p>}

      <div onDragOver={(event) => { event.preventDefault(); if (!unavailable) setDragging(true); }} onDragLeave={() => setDragging(false)} onDrop={dropFile} className={cls("min-w-0 rounded-lg border border-dashed bg-card p-5 text-center transition", dragging ? "border-brand bg-brand-soft/40" : "border-brand/30")}>
        <input ref={inputRef} type="file" accept=".csv,.json,.jsonl,text/csv,application/json,application/x-ndjson" aria-label="选择本地预测文件" className="sr-only" disabled={unavailable} onChange={(event) => { const selected = event.target.files?.[0]; if (selected) void chooseFile(selected); event.target.value = ""; }} />
        {file ? (
          <div className="flex min-w-0 flex-col items-center justify-between gap-3 text-left sm:flex-row">
            <div className="flex min-w-0 items-center gap-3">
              <FileSpreadsheet size={22} className="shrink-0 text-brand" />
              <div className="min-w-0"><p className="break-all text-[12px] font-semibold text-ink">{file.name}</p><p className="mt-1 text-[10px] text-faint">{fileSize(file.size)} · {phase === "preview" ? "正在读取并匹配事件…" : "文件已选择"}</p></div>
            </div>
            <div className="flex shrink-0 items-center gap-2">
              <button type="button" className={BUTTON} disabled={unavailable} onClick={() => inputRef.current?.click()}><Upload size={12} />替换文件</button>
              <button type="button" className={BUTTON} disabled={disabled || phase === "commit"} onClick={clearFile}><X size={12} />取消</button>
            </div>
          </div>
        ) : (
          <><Upload size={23} className="mx-auto text-brand" /><button type="button" className={cls(BUTTON, "mt-3 border-brand/30 text-brand")} disabled={unavailable} onClick={() => inputRef.current?.click()}>选择本地预测文件</button><p className="mt-2 text-[11px] leading-relaxed text-mute">也可拖入文件 · CSV、JSON、JSONL · 最大 10 MiB</p><p className="mt-1 text-[10px] leading-relaxed text-faint">模板已填入事件 ID 和当前 {horizon.toUpperCase().replace("T", "T+")} 预测窗口。每个事件补充一条预测；如需更换窗口，请先在第 3 步调整。</p></>
        )}
      </div>

      <details className="rounded-lg border border-edge bg-card px-3.5 py-3 text-[11px]">
        <summary className="cursor-pointer font-medium text-mute">文件需要哪些字段？</summary>
        <div className="mt-3 space-y-2 leading-relaxed text-mute">
          <p className="break-words"><code className="text-ink">event_id</code> 填模板中的事件 ID；<code className="text-ink">confidence</code> 填 0–1 的置信度，如 0.8 表示 80%；<code className="text-ink">rationale</code> 填预测理由。</p>
          <p className="break-words"><code className="text-ink">direction</code> 表示扣除市场基准影响后的超额方向：up 为正向、down 为负向、neutral 为中性，默认阈值为 ±0.5%。</p>
          <p className="break-words"><code className="text-ink">horizon</code> 必填且与第 3 步的评价窗口一致；<code className="text-ink">expected_return_pct</code> 为可选的资产自身收益率预测，2 表示资产收益 +2%，-1.5 表示 -1.5%。留空不计算该事件的收益率预测误差。</p>
          <p>所选事件集中的每个事件都需提供一条预测，不能重复或遗漏。JSON 使用对象数组，JSONL 每行一个对象；文件使用 UTF-8 编码，CSV 支持带引号的文本。</p>
        </div>
      </details>

      {error && <div role="alert" className="flex min-w-0 items-start gap-2 rounded-lg border border-rise/20 bg-rise/5 px-3 py-2.5 text-[11px] text-rise"><AlertTriangle size={13} className="mt-0.5 shrink-0" /><div className="min-w-0 break-words"><p>{error}</p>{file && !busy && <button type="button" disabled={disabled || !datasetId} onClick={() => void chooseFile(file)} className="mt-2 font-semibold underline underline-offset-2">重新预览文件</button>}</div></div>}
      {phase === "preview" && <p role="status" className="flex items-center gap-2 text-[11px] text-mute"><Loader2 size={13} className="animate-spin" />正在校验预测内容并匹配事件 ID…</p>}

      {alignedPreview && <div className="min-w-0 space-y-3" aria-live="polite">
        <div className="flex flex-wrap items-center justify-between gap-2"><h4 className="text-[12px] font-semibold text-ink">导入预览</h4><p className="text-[10px] text-mute">文件 {alignedPreview.total_rows} 条预测 · 事件集 {alignedPreview.event_count} 条事件</p></div>
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 xl:grid-cols-5">
          <Stat label="已匹配事件" value={alignedPreview.matched_count} />
          <Stat label="缺少预测" value={alignedPreview.missing_count} warning />
          <Stat label="重复事件" value={alignedPreview.duplicate_count} warning />
          <Stat label="不属于该事件集" value={alignedPreview.unknown_count} warning />
          <Stat label="格式错误" value={alignedPreview.format_error_count} warning />
        </div>

        {!alignedPreview.valid && <div role="alert" className="min-w-0 space-y-2 rounded-lg border border-rise/20 bg-rise/5 px-3.5 py-3 text-rise">
          <p className="flex items-center gap-2 text-[11.5px] font-semibold"><AlertTriangle size={13} className="shrink-0" />请修正文件后重新上传</p>
          <IdList label="缺少预测的事件" ids={alignedPreview.missing_ids} />
          <IdList label="不属于当前事件集" ids={alignedPreview.unknown_ids} />
          <IdList label="重复的事件" ids={alignedPreview.duplicate_ids} />
          {alignedPreview.errors.length > 0 && <ul className="list-inside list-disc space-y-1 text-[11px] leading-relaxed">{alignedPreview.errors.slice(0, 8).map((issue, index) => <li key={index} className="break-words">{issue.row != null && `第 ${issue.row} 行：`}{issue.message}</li>)}</ul>}
          {alignedPreview.error_count > 8 && <p className="text-[10px]">另有 {alignedPreview.error_count - 8} 项错误。请按模板逐行检查后重新预览。</p>}
        </div>}

        {alignedPreview.preview.length > 0 && <div className="min-w-0 overflow-hidden rounded-lg border border-edge bg-card">
          <div className="border-b border-edge px-3 py-2 text-[10px] text-mute">按事件集顺序显示前 {Math.min(5, alignedPreview.preview.length)} 条预测</div>
          <div className="overflow-x-auto"><table className="w-full min-w-[560px] table-fixed text-left text-[10.5px]"><thead className="bg-paper text-mute"><tr><th className="w-[24%] px-3 py-2 font-medium">事件 ID</th><th className="w-[11%] px-2 py-2 font-medium">超额方向</th><th className="w-[12%] px-2 py-2 font-medium">置信度</th><th className="w-[15%] px-2 py-2 font-medium">资产收益率预测</th><th className="px-3 py-2 font-medium">理由</th></tr></thead><tbody className="divide-y divide-edge">{alignedPreview.preview.slice(0, 5).map((row, index) => <tr key={`${row.event_id}-${index}`} className="align-top text-ink"><td className="break-all px-3 py-2.5 font-mono">{row.event_id}</td><td className="px-2 py-2.5">{{ up: "正向", down: "负向", neutral: "中性" }[row.direction] ?? row.direction}</td><td className="px-2 py-2.5">{row.confidence ?? "—"}</td><td className="px-2 py-2.5">{row.expected_return_pct == null ? "未提供" : `${row.expected_return_pct > 0 ? "+" : ""}${row.expected_return_pct}%`}</td><td className="break-words px-3 py-2.5 leading-relaxed"><span className="line-clamp-3" title={row.rationale}>{row.rationale}</span></td></tr>)}</tbody></table></div>
        </div>}

        {alignedPreview.valid && <div className="space-y-3 rounded-lg border border-jade/20 bg-jade-soft/35 p-3.5">
          <p className="flex items-start gap-2 text-[11px] leading-relaxed text-jade"><CheckCircle2 size={14} className="mt-0.5 shrink-0" /><span>校验通过，已匹配全部 {alignedPreview.matched_count} 个事件。其中 {alignedPreview.return_forecast_count} 条提供收益率数值。</span></p>
          <label className="block text-[11px] font-medium text-ink">导入后的事件集名称<input value={datasetName} maxLength={160} disabled={unavailable} onChange={(event) => setDatasetName(event.target.value)} className="mt-1.5 w-full min-w-0 rounded-lg border border-edgeDark/70 bg-card px-3 py-2 text-[12px] font-normal outline-none focus:border-brand disabled:opacity-50" placeholder="例如：Qwen · 事件预测结果" /></label>
          <div className="flex flex-col justify-between gap-3 sm:flex-row sm:items-center"><p className="text-[10.5px] leading-relaxed text-mute">确认后保存并选中带预测的新事件集，原事件集保留。随后前往第 3 步创建评测。</p><button type="button" onClick={() => void commit()} disabled={unavailable || !alignedPreview.preview_token} className="inline-flex shrink-0 items-center justify-center gap-1.5 rounded-lg bg-brand px-4 py-2.5 text-[12px] font-semibold text-white transition hover:bg-brand/90 disabled:cursor-not-allowed disabled:opacity-45">{phase === "commit" ? <Loader2 size={13} className="animate-spin" /> : <CheckCircle2 size={13} />}{phase === "commit" ? "正在导入…" : "确认导入并使用"}</button></div>
        </div>}
      </div>}
    </section>
  );
}
