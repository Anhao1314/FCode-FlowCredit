/* 统一意图表：composer 输入 → 车道。
 *
 * 设计（学 Claude Code 的可收缩性，保留治理边界）：
 * - 声明式、按序求值、首中即停——每行是一个可独立收缩的意图机制；
 * - 模型能力上升时删行即退化一步，不拆代码；
 * - 治理边界不在这张表里：执行侧的门（审批卡、apply-patch、封顶自动批）
 *   与路由无关，路由只决定"走哪条道"。
 * - 代价不对称决定阈值：误把问候送进 run 是浪费，误把任务送进对话丢工作——
 *   所以规则宁可保守（现在只有纯问候一行）。
 */

const CASUAL_GREETINGS = new Set([
  "你好", "您好", "你好呀", "您好呀", "哈喽", "哈罗", "嗨", "嗨嗨",
  "hi", "hello", "hey", "yo", "hiya",
  "在吗", "在么", "在不在",
  "谢谢", "多谢", "感谢", "thanks", "thankyou", "thx",
  "ping", "test", "测试", "在", "ok", "好",
]);

export function isCasualGreeting(text) {
  const cleaned = String(text || "")
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, "");
  if (!cleaned || cleaned.length > 8) return false;
  return CASUAL_GREETINGS.has(cleaned);
}

export const INTENT_RULES = [
  {
    id: "casual-greeting", lane: "chat",
    reason: "纯问候不起委托 run——转对话回答",
    test: isCasualGreeting,
  },
  {
    id: "default-task", lane: "task",
    reason: "其余输入走任务管线（intake/直接开跑）",
    test: () => true,
  },
];

export function classifyIntent(text) {
  for (const rule of INTENT_RULES) {
    if (rule.test(text)) {
      return { lane: rule.lane, rule: rule.id, reason: rule.reason };
    }
  }
  return { lane: "task", rule: "default-task", reason: "" };
}
