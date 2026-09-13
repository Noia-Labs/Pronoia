export const EVENT_INPUT_TIME_ZONES = [
  { value: "Asia/Shanghai", label: "Asia/Shanghai · 中国标准时间" },
  { value: "Asia/Hong_Kong", label: "Asia/Hong_Kong · 香港时间" },
  { value: "America/New_York", label: "America/New_York · 美东时间（自动 DST）" },
  { value: "UTC", label: "UTC · 协调世界时" },
  { value: "Europe/London", label: "Europe/London · 英国时间（自动 DST）" },
  { value: "Asia/Tokyo", label: "Asia/Tokyo · 日本标准时间" },
] as const;

export type EventInputTimeZone = (typeof EVENT_INPUT_TIME_ZONES)[number]["value"];
export type DateTimeDisambiguation = "earlier" | "later";

export interface ZonedDateTimeResolution {
  /** ISO-8601 local timestamp with an explicit numeric UTC offset. */
  iso: string;
  /** The corresponding absolute Unix timestamp. */
  instantMs: number;
  offsetMinutes: number;
  /** True when the wall clock occurs twice during a daylight-saving fall-back. */
  ambiguous: boolean;
}

interface DateTimeParts {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

const LOCAL_DATE_TIME_RE = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;
const formatterCache = new Map<string, Intl.DateTimeFormat>();

function formatterFor(timeZone: string): Intl.DateTimeFormat {
  const cached = formatterCache.get(timeZone);
  if (cached) return cached;
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone,
    calendar: "iso8601",
    numberingSystem: "latn",
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
  // Force eager validation because some engines defer an invalid IANA zone
  // until the first format call.
  formatter.format(new Date(0));
  formatterCache.set(timeZone, formatter);
  return formatter;
}

function partsToEpoch(parts: DateTimeParts): number {
  const date = new Date(0);
  date.setUTCFullYear(parts.year, parts.month - 1, parts.day);
  date.setUTCHours(parts.hour, parts.minute, parts.second, 0);
  return date.getTime();
}

function parseLocalDateTime(value: string): DateTimeParts {
  const match = value.trim().match(LOCAL_DATE_TIME_RE);
  if (!match) throw new Error("请输入完整的本地日期与时间。");
  const parts: DateTimeParts = {
    year: Number(match[1]),
    month: Number(match[2]),
    day: Number(match[3]),
    hour: Number(match[4]),
    minute: Number(match[5]),
    second: Number(match[6] ?? 0),
  };
  const roundTrip = new Date(partsToEpoch(parts));
  if (
    roundTrip.getUTCFullYear() !== parts.year ||
    roundTrip.getUTCMonth() + 1 !== parts.month ||
    roundTrip.getUTCDate() !== parts.day ||
    roundTrip.getUTCHours() !== parts.hour ||
    roundTrip.getUTCMinutes() !== parts.minute ||
    roundTrip.getUTCSeconds() !== parts.second
  ) {
    throw new Error("日期或时间超出有效范围。");
  }
  return parts;
}

function zonedPartsAt(instantMs: number, timeZone: string): DateTimeParts {
  const values: Partial<Record<keyof DateTimeParts, number>> = {};
  for (const part of formatterFor(timeZone).formatToParts(new Date(instantMs))) {
    if (["year", "month", "day", "hour", "minute", "second"].includes(part.type)) {
      values[part.type as keyof DateTimeParts] = Number(part.value);
    }
  }
  if (Object.keys(values).length !== 6) throw new Error(`无法解析时区 ${timeZone}。`);
  return values as DateTimeParts;
}

function sameParts(left: DateTimeParts, right: DateTimeParts): boolean {
  return left.year === right.year && left.month === right.month && left.day === right.day &&
    left.hour === right.hour && left.minute === right.minute && left.second === right.second;
}

function offsetAt(instantMs: number, timeZone: string): number {
  const secondAligned = Math.trunc(instantMs / 1000) * 1000;
  return partsToEpoch(zonedPartsAt(secondAligned, timeZone)) - secondAligned;
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, "0");
}

function offsetSuffix(offsetMinutes: number): string {
  const sign = offsetMinutes < 0 ? "-" : "+";
  const absolute = Math.abs(offsetMinutes);
  return `${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`;
}

/**
 * Resolve a `datetime-local` wall clock in an explicit IANA timezone.
 *
 * The inverse lookup samples offsets around the requested wall clock, so it
 * observes daylight-saving transitions instead of applying today's offset.
 * Non-existent spring-forward times are rejected. Repeated fall-back times
 * use the requested deterministic disambiguation (the earlier instant by
 * default) and report `ambiguous=true` for an explicit UI disclosure.
 */
export function resolveZonedLocalDateTime(
  localDateTime: string,
  timeZone: string,
  disambiguation: DateTimeDisambiguation = "earlier",
): ZonedDateTimeResolution {
  const requested = parseLocalDateTime(localDateTime);
  const wallEpoch = partsToEpoch(requested);
  const possibleOffsets = new Set<number>();
  const sampleWindowHours = 48;
  for (let hours = -sampleWindowHours; hours <= sampleWindowHours; hours += 6) {
    possibleOffsets.add(offsetAt(wallEpoch + hours * 60 * 60 * 1000, timeZone));
  }

  const candidates = [...possibleOffsets]
    .map((offsetMs) => wallEpoch - offsetMs)
    .filter((candidate) => sameParts(zonedPartsAt(candidate, timeZone), requested))
    .filter((candidate, index, values) => values.indexOf(candidate) === index)
    .sort((left, right) => left - right);

  if (!candidates.length) {
    throw new Error(`该本地时间在 ${timeZone} 不存在（可能处于夏令时跳时）。`);
  }
  const instantMs = disambiguation === "later" ? candidates[candidates.length - 1] : candidates[0];
  const offsetMinutes = Math.round(offsetAt(instantMs, timeZone) / 60_000);
  const iso = `${pad(requested.year, 4)}-${pad(requested.month)}-${pad(requested.day)}` +
    `T${pad(requested.hour)}:${pad(requested.minute)}:${pad(requested.second)}${offsetSuffix(offsetMinutes)}`;
  return { iso, instantMs, offsetMinutes, ambiguous: candidates.length > 1 };
}

/** Format an absolute Date as the value expected by `input[type=datetime-local]`. */
export function formatLocalDateTimeInZone(date: Date, timeZone: string): string {
  const parts = zonedPartsAt(date.getTime(), timeZone);
  return `${pad(parts.year, 4)}-${pad(parts.month)}-${pad(parts.day)}T${pad(parts.hour)}:${pad(parts.minute)}`;
}
