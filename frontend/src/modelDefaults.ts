export const DEFAULT_MAX_OUTPUT_TOKENS = 65536;
export const DEFAULT_MODEL_TIMEOUT_SECONDS = 600;
export const MIN_MAX_OUTPUT_TOKENS = 32;
export const MAX_MAX_OUTPUT_TOKENS = 200000;
export const OUTPUT_TOKEN_LIMIT_HINT = "适用于该连接的预测、问答、自动评分和 Pronoia 多 Agent 流程。每次请求按任务分配额度，并受此上限约束；部分模型的思考过程也计入额度，实际还受服务商限制。调高上限有助于减少截断，也可能增加等待时间和用量，不能保证所有问题都能成功完成。";

export function parseOutputTokenLimit(value: string): number | null {
  if (!value.trim()) return null;
  const limit = Number(value);
  return Number.isInteger(limit) && limit >= MIN_MAX_OUTPUT_TOKENS && limit <= MAX_MAX_OUTPUT_TOKENS ? limit : null;
}
