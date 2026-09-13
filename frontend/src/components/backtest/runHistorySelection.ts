export type HistoryRecordType = "run" | "batch";

export interface SelectableHistoryEntry {
  id: string;
  recordType: HistoryRecordType;
  status: string;
  qaStatus?: string;
}

export const activeHistoryStatuses = new Set([
  "pending",
  "running",
  "paused",
  "interrupted",
  "starting",
  "queued",
  "cancelling",
]);

const deletionBlockedStatuses = new Set([
  "running",
  "paused",
  "interrupted",
  "starting",
  "queued",
  "cancelling",
]);

export function historyEntryKey(entry: Pick<SelectableHistoryEntry, "recordType" | "id">): string {
  return `${entry.recordType}:${entry.id}`;
}

export function canDeleteHistoryEntry(entry: Pick<SelectableHistoryEntry, "status" | "qaStatus">): boolean {
  return !deletionBlockedStatuses.has(entry.status)
    && !deletionBlockedStatuses.has(entry.qaStatus ?? "");
}

export function toggleVisibleHistorySelection(
  current: ReadonlySet<string>,
  visibleEntries: SelectableHistoryEntry[],
): Set<string> {
  const selectableKeys = visibleEntries.filter(canDeleteHistoryEntry).map(historyEntryKey);
  const allSelected = selectableKeys.length > 0 && selectableKeys.every((key) => current.has(key));
  const next = new Set(current);
  selectableKeys.forEach((key) => allSelected ? next.delete(key) : next.add(key));
  return next;
}
