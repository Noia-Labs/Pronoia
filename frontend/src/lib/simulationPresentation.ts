export type Observation = { id: string; scenarioIndex: number; scenarioLabel: string; kind: "trigger" | "invalidation"; text: string; source?: string; window?: string; evidenceRefs?: string[]; needsReview?: boolean; reviewReasons?: string[] };

export function textItems(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string" && Boolean(item.trim())) : [];
}

export function observationWindow(payload: any): string | undefined {
  const source = payload?.source;
  const horizon = source?.horizon;
  const count = horizon?.value ?? source?.horizon_days;
  if (typeof count !== "number" || !Number.isFinite(count) || count <= 0) return undefined;
  const unit = ({ calendar_days: "个自然日", trading_days: "个交易日", rounds: "轮" } as Record<string, string>)[horizon?.kind ?? "calendar_days"];
  if (!unit) return undefined;
  // Preserve the recorded date in its original timezone. Historical artifacts
  // must never acquire a new deadline relative to the day they are opened.
  const recordedDate = (value: unknown) => typeof value === "string" && /^\d{4}-\d{2}-\d{2}(T|$)/.test(value) ? value.slice(0, 10) : undefined;
  const start = recordedDate(source?.as_of);
  const end = recordedDate(horizon?.end_at);
  if (start && end) return `${start} 至 ${end}（${count} ${unit}）`;
  return start ? `自 ${start} 起 ${count} ${unit}` : `${count} ${unit}（未记录起点）`;
}

export function observations(payload: any): Observation[] {
  const scenarios = Array.isArray(payload?.scenarios) ? payload.scenarios : [];
  return scenarios.flatMap((scenario: any, index: number) => {
    const cards = Array.isArray(scenario.observations) ? scenario.observations.filter((card: any) =>
      ["trigger", "invalidation"].includes(card?.kind) && typeof card.signal === "string" && card.signal.trim()
    ) : [];
    if (cards.length) return cards.map((card: any, itemIndex: number) => ({
      id: `${index}-${card.kind}-${itemIndex}`, scenarioIndex: index, scenarioLabel: scenario.label || `情景 ${index + 1}`,
      kind: card.kind, text: card.signal.trim(), source: typeof card.source === "string" ? card.source : undefined,
      window: typeof card.window === "string" ? card.window : undefined, evidenceRefs: textItems(card.evidence_refs),
      needsReview: card.review_status === "needs_review",
      reviewReasons: Array.isArray(card.review_findings) ? textItems(card.review_findings.map((finding: any) => finding?.message)) : [],
    }));
    const groups = [["trigger", scenario.triggers ?? scenario.trigger_conditions], ["invalidation", scenario.invalidation_conditions]] as const;
    return groups.flatMap(([kind, values]) => [...new Set(textItems(values).map((text) => text.trim()))].map((text, itemIndex) => ({
      id: `${index}-${kind}-${itemIndex}`, scenarioIndex: index, scenarioLabel: scenario.label || `情景 ${index + 1}`, kind, text,
    })));
  });
}

export function observationMarkdown(payload: any): string {
  const lines = ["# 条件情景观察清单", "", "以下为模拟提出的待核对条件，不代表已发生事实或发生概率。", ""];
  if (payload?.source?.question) lines.push(`研究问题：${payload.source.question}`);
  if (payload?.source?.as_of) lines.push(`证据截止：${payload.source.as_of}`);
  const window = observationWindow(payload);
  if (window) lines.push(`观察窗口：${window}`);
  let currentIndex = -1;
  for (const item of observations(payload)) {
    if (item.scenarioIndex !== currentIndex) {
      currentIndex = item.scenarioIndex;
      lines.push("", `## ${item.scenarioLabel}`, "");
      for (const notice of textItems(payload?.scenarios?.[currentIndex]?.review_notices)) lines.push(`待核对：${notice}`, "");
    }
    lines.push(`${item.needsReview ? "- 待核对后使用" : `- [ ] ${item.kind === "trigger" ? "留意信号" : "重新判断"}`}：${item.text}`);
    for (const reason of item.reviewReasons || []) lines.push(`  待核对：${reason}`);
    if (item.source) lines.push(`  核对渠道：${item.source}`);
    if (item.window) lines.push(`  复核窗口：${item.window}`);
    if (item.evidenceRefs?.length) lines.push(`  依据：${item.evidenceRefs.join("、")}`);
  }
  const evidence = Array.isArray(payload?.evidence) ? payload.evidence : [];
  const refs = new Set((Array.isArray(payload?.scenarios) ? payload.scenarios : []).flatMap((scenario: any) => textItems(scenario.evidence_refs)));
  const cited = evidence.filter((fact: any) => refs.has(fact.id));
  if (cited.length) {
    lines.push("", "## 依据资料", "");
    for (const fact of cited) lines.push(`- ${fact.id}：${fact.statement}${safeSourceUrl(fact.source_url) ? ` (${fact.source_url})` : ""}`);
  }
  return lines.join("\n");
}

export function safeSourceUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  try {
    const url = new URL(value);
    return ["https:", "http:"].includes(url.protocol) ? url.href : undefined;
  } catch { return undefined; }
}
