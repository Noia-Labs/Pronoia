import { cls } from "../../utils";

/** Quiet architectural marks, drawn as code so the Arena stays lightweight. */
export function ArenaFieldArt({ className = "" }: { className?: string }) {
  return <svg aria-hidden="true" focusable="false" viewBox="0 0 560 320" className={cls("pointer-events-none", className)} fill="none">
    <g stroke="currentColor" strokeWidth="0.8">
      <ellipse cx="280" cy="174" rx="247" ry="104" />
      <ellipse cx="280" cy="174" rx="227" ry="91" />
      <ellipse cx="280" cy="174" rx="199" ry="74" />
      <ellipse cx="280" cy="174" rx="173" ry="60" />
      <ellipse cx="280" cy="174" rx="145" ry="46" />
      <path d="M33 174v26c0 58 111 105 247 105s247-47 247-105v-26M53 211v20m29-8v24m31-13v27m34-18v29m36-22v29m38-24v31m39-28v33m40-33v33m39-35v33m38-37v31m36-39v29m34-43v27m31-48v24m29-56v20" />
      <path d="M280 32v244M4 174h552" strokeDasharray="3 7" opacity=".5" />
      <path d="m132 91 65 44m166 77 65 44M132 256l65-44m166-77 65-44" opacity=".6" />
      <path d="M252 183v-18a28 28 0 0 1 56 0v18M261 183v-18a19 19 0 0 1 38 0v18M244 184h72" strokeWidth="1.5" />
      <path d="m82 86 9-10m388 10-9-10M75 251l10 8m400-8-10 8" />
    </g>
    <circle cx="280" cy="32" r="3" fill="currentColor" />
    <circle cx="33" cy="174" r="2" fill="currentColor" />
    <circle cx="527" cy="174" r="2" fill="currentColor" />
  </svg>;
}

export function ArenaSeal({ className = "" }: { className?: string }) {
  return <svg aria-hidden="true" focusable="false" viewBox="0 0 64 64" className={cls("shrink-0", className)} fill="none" stroke="currentColor" strokeWidth="1.2">
    <circle cx="32" cy="32" r="29" strokeOpacity=".35" />
    <circle cx="32" cy="32" r="24" strokeOpacity=".2" />
    <path d="M16 44h32M19 40V29a13 13 0 0 1 26 0v11M25 40V29a7 7 0 0 1 14 0v11M16 40h32M20 48h24" />
    <path d="M32 9v5m0 36v5M9 32h5m36 0h5" strokeOpacity=".55" />
  </svg>;
}

export const arenaMatchNumber = (id: string) => id.replace(/[^a-z0-9]/gi, "").slice(-6).toUpperCase();

export function ArenaLineup({ names, count, compact = false }: { names: string[]; count: number; compact?: boolean }) {
  if (!names.length) return <p className="text-[12px] text-mute">{count} 个模型 · 阵容与配置见详情</p>;
  const visible = names.slice(0, 2);
  return <div className="min-w-0">
    <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_30px_minmax(0,1fr)] items-center gap-1.5">
      {visible.map((name, index) => <div key={`${index}-${name}`} className={cls("row-start-1 min-w-0", index === 1 && "col-start-3 text-right", index === 0 && "col-start-1")}>
        <span className={cls("mb-1 block font-mono text-[9px] tracking-[.14em]", index === 0 ? "text-brand" : "text-jade")}>{String(index + 1).padStart(2, "0")}</span>
        <p className={cls("break-words font-medium leading-relaxed text-ink", compact ? "line-clamp-2 text-[11px]" : "text-[12px]")}>{name}</p>
      </div>)}
      <span aria-hidden="true" className="col-start-2 row-start-1 font-serif text-center text-[13px] italic text-faint">vs</span>
      {visible.length === 1 && <span className="col-start-3 row-start-1 text-right text-[11px] text-mute">{count > 1 ? `另 ${count - 1} 个模型` : "单模型结果"}</span>}
    </div>
    {count > 2 && <p className="mt-2 text-center text-[10px] text-faint">另 {count - 2} 个模型同场比较</p>}
  </div>;
}
