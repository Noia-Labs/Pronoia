import {
  CheckCircle2,
  ChevronRight,
  CircleDashed,
  Loader2,
  Minus,
  Pause,
  Square,
  TrendingDown,
  TrendingUp,
  XCircle,
} from "lucide-react";
import type { BTDirection, BTEventStatus, BTStatus } from "../types";
import { cls } from "../utils";

/* ===================================== 基础原语（badge / 纯函数 / 文本块 / 下拉） ===================================== */

export function StatusBadge({ status, prefix = "" }: { status: BTStatus | string; prefix?: string }) {
  const cfg: Record<string, { label: string; className: string; icon: React.ReactNode }> = {
    pending: { label: "待启动", className: "bg-edge text-mute", icon: <CircleDashed size={11} /> },
    running: { label: "运行中", className: "bg-brand-soft text-brand", icon: <Loader2 size={11} className="animate-spin" /> },
    paused: { label: "已暂停", className: "bg-edge text-mute", icon: <Pause size={11} /> },
    done: { label: "已完成", className: "bg-jade-soft text-jade", icon: <CheckCircle2 size={11} /> },
    failed: { label: "失败", className: "bg-rise/10 text-rise", icon: <XCircle size={11} /> },
    cancelled: { label: "已取消", className: "bg-violet-soft text-violet", icon: <Square size={11} /> },
  };
  const c = cfg[status] ?? cfg.pending;
  return (
    <span className={cls("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10.5px] font-medium", c.className)}>
      {c.icon}{prefix}{c.label}
    </span>
  );
}

export function DirBadge({ d }: { d: BTDirection | string | null | undefined }) {
  if (!d) return <span className="text-faint">—</span>;
  const cfg: Record<string, { label: string; cls: string; icon: React.ReactNode }> = {
    up:    { label: "看涨", cls: "bg-fall/10 text-fall", icon: <TrendingUp size={10.5} /> },
    down:  { label: "看跌", cls: "bg-rise/10 text-rise", icon: <TrendingDown size={10.5} /> },
    neutral: { label: "中性", cls: "bg-edge text-mute", icon: <Minus size={10.5} /> },
  };
  const c = cfg[d] ?? { label: d, cls: "bg-edge text-mute", icon: null };
  return (
    <span className={cls("inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10.5px] font-medium", c.cls)}>
      {c.icon}{c.label}
    </span>
  );
}

export function EventStatusBadge({ status }: { status: BTEventStatus }) {
  const cfg: Record<BTEventStatus, { label: string; cls: string; icon: React.ReactNode }> = {
    pending: {
      label: "待执行",
      cls: "bg-edge text-mute",
      icon: <CircleDashed size={10.5} />,
    },
    processing: {
      label: "执行中",
      cls: "bg-amber-soft/70 text-amber",
      icon: <Loader2 size={10.5} className="animate-spin" />,
    },
    done: {
      label: "已完成",
      cls: "bg-jade-soft/70 text-jade",
      icon: <CheckCircle2 size={10.5} />,
    },
  };
  const c = cfg[status] ?? cfg.pending;
  return (
    <span className={cls("inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-medium", c.cls)}>
      {c.icon}
      {c.label}
    </span>
  );
}

/**
 * 判断 source_url 是否为 Pronoia 合成的占位链接（点出去一定 404）。
 * 此类链接点击时应当拦截 → 打开「完整事件说明」Modal，而非跳外部 404。
 */
export function isFeverSyntheticSourceUrl(url: string): boolean {
  if (!url) return false;
  // 用户手工录入事件没有外部网页；manual:// 仅作为可追溯的内部来源标识。
  if (url.startsWith("manual://")) return true;
  const FEVER_DATASET_IDS = [
    "cn_earnings_q2",
    "cn_insiders",
    "cn_policy",
    "us_earnings_q2",
    "hk_ratings",
  ];
  const eventIdLike = new RegExp(`(?:${FEVER_DATASET_IDS.join("|")})-\\d{2,4}`);
  if (eventIdLike.test(url)) return true;

  try {
    const u = new URL(url);
    const host = u.hostname;
    const search = u.search || "";
    const path = u.pathname || "";
    if (host.endsWith("cninfo.com.cn") && /announcementId=ann\d+h\d?_/.test(search)) return true;
    if (host.endsWith("cninfo.com.cn") && /announcementId=ann_insider_/.test(search)) return true;
    if (host.endsWith("sec.gov") && /-fever-/.test(path + search)) return true;
    if (host.endsWith("hkexnews.hk") && /fever_/.test(path + search)) return true;
    if (
      (host.endsWith("gov.cn") || host.endsWith("caixin.com") || host.endsWith("mofcom.gov.cn") || host.endsWith("pbc.gov.cn") || host.endsWith("csrc.gov.cn")) &&
      /(docid|fid|refid|id)=fever_/.test(search)
    ) {
      return true;
    }
    if (/(fever_|_fever_)/.test(url) && !host.includes("127.0.0.1") && !host.includes("localhost")) return true;
    return false;
  } catch {
    return false;
  }
}

export function fmtPct(v: number | null | undefined, digits = 1) {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function PreTextBlock({
  text,
  empty,
  emptyHint,
  code,
}: {
  text: string;
  empty: boolean;
  emptyHint: string;
  code?: boolean;
}) {
  if (empty) return <div className="whitespace-pre-wrap text-[12px] text-faint">{emptyHint}</div>;
  return (
    <pre
      className={cls(
        "whitespace-pre-wrap break-words text-[12px] leading-relaxed",
        code ? "font-mono text-ink" : "font-serif text-ink",
      )}
    >
      {text}
    </pre>
  );
}

export function FilterSelect({
  placeholder,
  value,
  options,
  onChange,
}: {
  placeholder: string;
  value: string;
  options: Array<string | { v: string; l: string }>;
  onChange: (v: string) => void;
}) {
  return (
    <div className="relative">
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cls(
          "h-7 appearance-none rounded-md border border-edge bg-card px-2 pr-6 text-[11.5px] outline-none transition focus:border-brand",
          value ? "text-ink" : "text-mute",
        )}
      >
        <option value="">{placeholder}（全部）</option>
        {options.map((o) => {
          const v = typeof o === "string" ? o : o.v;
          const l = typeof o === "string" ? o : o.l;
          return (
            <option key={v} value={v}>{l}</option>
          );
        })}
      </select>
      <ChevronRight
        size={12}
        className="pointer-events-none absolute right-1.5 top-1/2 -translate-y-1/2 rotate-90 text-faint"
      />
    </div>
  );
}
