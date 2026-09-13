import type { BTStrategySpec } from "../../types";
import type { CreateDraft } from "../BacktestList";

type Condition = CreateDraft["quant_conditions"][number];
export const objectRecord = (value: unknown): Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
const PERCENT_FIELDS = new Set(["bar_return", "return", "moving_average", "ma_cross", "breakout", "amplitude", "volatility", "macd"]);
const FIELDS = new Set(["price", ...PERCENT_FIELDS, "volume_ratio", "rsi", "bollinger_position"]);
const OPERATORS = new Set(["above", "below", "crosses_above", "crosses_below"]);

/** Hydrate decimal thresholds into the percentages displayed by QuantEditor. */
export function hydrateQuantConfiguration(spec: BTStrategySpec, initial: CreateDraft): CreateDraft | null {
  const parameters = objectRecord(spec.parameters);
  const conditions: Condition[] = [];
  const groups: Record<string, "and" | "or"> = {};
  for (const phase of ["entry", "exit"] as const) {
    const group = objectRecord(parameters[phase]);
    const rawRules = group.conditions ?? group.rules;
    const combinator = String(group.combinator ?? "and").toLowerCase();
    if (!["and", "all", "or", "any"].includes(combinator) || !Array.isArray(rawRules) || !rawRules.length) return null;
    groups[phase] = ["or", "any"].includes(combinator) ? "or" : "and";
    for (const [index, value] of rawRules.entries()) {
      const raw = objectRecord(value);
      const legacyVolume = raw.field === "volume";
      const field = legacyVolume ? "volume_ratio" : String(raw.field ?? "");
      const operator = String(raw.operator ?? "");
      if (!FIELDS.has(field) || !OPERATORS.has(operator)) return null;
      const lookback = Number(raw.lookback ?? 1);
      const decimal = Number(raw.threshold ?? 0);
      const threshold = legacyVolume ? decimal + 1 : PERCENT_FIELDS.has(field) ? decimal * 100 : decimal;
      conditions.push({
        id: `${phase}-saved-${index}`, phase, field: field as Condition["field"], operator: operator as Condition["operator"],
        lookback, threshold, consecutive_count: Number(raw.consecutive_count ?? 1),
        fast_period: Number(raw.fast_period ?? Math.max(1, Math.floor(lookback / 2))),
        slow_period: Number(raw.slow_period ?? lookback), signal_period: Number(raw.signal_period ?? 9),
      });
    }
  }
  return { ...initial, strategy_version: String(spec.version ?? ""), quant_mode: "trading_strategy", quant_conditions: conditions, quant_entry_combinator: groups.entry, quant_exit_combinator: groups.exit };
}

/** Keep unexposed configuration fields while replacing edited conditions. */
export function mergeQuantConfiguration(spec: BTStrategySpec, draft: CreateDraft, serialize: (condition: Condition) => Record<string, unknown>): BTStrategySpec {
  const parameters = { ...objectRecord(spec.parameters) };
  for (const phase of ["entry", "exit"] as const) {
    const original = objectRecord(parameters[phase]);
    const rawRules = original.conditions ?? original.rules;
    const rules = Array.isArray(rawRules) ? rawRules : [];
    const conditions = draft.quant_conditions.filter((condition) => condition.phase === phase).map((condition) => {
      const savedIndex = condition.id.match(new RegExp(`^${phase}-saved-(\\d+)$`));
      const previous = savedIndex ? { ...objectRecord(rules[Number(savedIndex[1])]) } : {};
      // These derived fields must follow the currently selected factor.
      for (const key of ["field", "operator", "lookback", "threshold", "threshold_unit", "threshold_decimal", "threshold_display", "fast_period", "slow_period", "signal_period", "warmup_bars", "consecutive_count"]) delete previous[key];
      return { ...previous, ...serialize(condition) };
    });
    const group: Record<string, unknown> = { ...original, combinator: phase === "entry" ? draft.quant_entry_combinator : draft.quant_exit_combinator, conditions };
    delete group.rules;
    parameters[phase] = group;
  }
  return { ...spec, version: draft.strategy_version.trim() || null, parameters };
}

export function parseStrategyConfiguration(text: string): BTStrategySpec {
  const value: unknown = JSON.parse(text);
  const object = objectRecord(value);
  if (!Object.keys(object).length || typeof object.type !== "string") throw new Error("配置应为 JSON 对象，并包含模型 type 字段。");
  return object as BTStrategySpec;
}
