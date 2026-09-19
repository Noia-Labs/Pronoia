import type { Part } from "../types";

/** Status events replace their start marker, including when replaying history. */
export function researchProgressParts(parts: Part[], pending: boolean): Part[] {
  const lastHypotheses = parts.reduce((last, part, i) =>
    part.type === "agent_step" && part.phase === "hypotheses" ? i : last, -1);
  return parts.flatMap((part, i): Part[] => {
    if (part.type === "agent_step" && part.phase === "hypotheses") {
      if (i !== lastHypotheses) return [];
      if (!pending && part.verdict === "running") {
        return [{ ...part, verdict: "interrupted", note: "本轮响应已结束，未收到研究假设提炼结果。" }];
      }
    }
    // Older servers sent a status label as reasoning and never closed it.
    if (!pending && part.type === "thinking" && part.text.trim() === "正在提炼可证伪的研究假设…") {
      return lastHypotheses >= 0 ? [] : [{ type: "agent_step", phase: "hypotheses",
        agent: "router", verdict: "ended", note: "本轮响应已结束；此历史记录未保存假设提炼状态。" }];
    }
    return [part];
  });
}

export function friendlySimulationError(reason: unknown): string {
  const message = reason instanceof Error ? reason.message : String(reason || "");
  if (/model_auth_failed/i.test(message)) {
    return "模型服务未接受当前密钥，推演未能启动。请检查对应服务商的 Key 并重启服务；证据图仍保留。";
  }
  if (/model_unavailable/i.test(message)) {
    return "模型服务没有提供当前配置的模型，推演未能启动。请核对模型名称并重启服务；证据图仍保留。";
  }
  if (/model_connection_failed|APIConnectionError|Connection error\.|ConnectError/i.test(message)) {
    return "无法连接模型服务，推演未能完成。请检查模型地址、网络或代理设置后重新推演；证据图和已保存过程仍在。";
  }
  if (/model_rate_limited/i.test(message)) {
    return "模型服务暂时限制请求，推演尚未启动。请稍后重新推演；证据图仍保留。";
  }
  if (/model_readiness_failed/i.test(message)) {
    return "模型服务的连接检查未通过，推演尚未启动。请稍后重试或检查服务配置；证据图仍保留。";
  }
  if (message.includes("simulation_capacity_timeout")) {
    return "模型请求长时间未完成，本次等待已超时。资料已保存；请稍后重新推演。";
  }
  if (/tokenizer_unavailable|tiktoken/i.test(message)) {
    return "推演启动所需的分词器文件未就绪，角色推演未能启动。资料已保存；请先修复运行环境，再重新推演。";
  }
  if (message.includes("simulation_budget_exhausted")) {
    return "本次推演已达到模型用量上限，未能完成。资料已保存；可减少参与方后重新推演。";
  }
  if (/simulation_process_(failed|stopped)|MiroFish smoke run ended as/i.test(message)) {
    return "推演进程已退出，任务未完成。资料已保存；请先检查运行日志，再重新推演。";
  }
  if (message.includes("safety budget is exhausted")) {
    return "模拟服务本期模型额度已用完，需在额度恢复后继续。证据图和已保存过程仍在。";
  }
  if (/budget.*exhausted/i.test(message)) {
    return "本次推演已达到模型用量上限，未能完成结构化决策。证据图和已保存过程仍在；可减少参与方后重新推演。";
  }
  if (message.includes("structured decision collection failed")) {
    return "结构化决策未能生成，情景整理已停止。证据图和已保存过程仍在，请重新推演。";
  }
  if (message.includes("Ontology generation failed")) return "参与方关系整理暂时失败，可以从保存进度继续，或重新推演。";
  if (message.includes("ZEP read quota") || /rate limit/i.test(message)) return "服务暂时达到调用上限，请稍后重试。已提交的进度会保留。";
  if (message.includes("durable workflow checkpoint")) return "这次任务还没有可恢复的进度，请选择重新推演。";
  return message;
}

export function simulationNeedsNewRun(error?: string | null): boolean {
  return /structured decision collection failed|local smoke (token )?budget exhausted|tokenizer_unavailable|simulation_(process_(failed|stopped)|budget_exhausted)|MiroFish smoke run ended as/i.test(error || "");
}
