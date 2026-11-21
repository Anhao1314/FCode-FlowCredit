"use strict";

/* Composer 模式解析（S1 Chat/Work 分态切片）：唯一真相表。
 *
 * 同一个输入框按语境有五种提交语义——intake 补充描述 / gov 治理命令 /
 * chat 发消息 / running 运行插话 / work 新委托。dispatch 只查本函数的
 * 结果；判据（intake 覆盖层、gov 视图、chat 视图、运行中的会话）只活在
 * 这里，不再散布在提交路径里。优先级：intake > gov > chat > running > work
 * （gov 视图下 chat 必然不活跃，gov 先于 chat 只是表达从属关系）。
 */

export function composerMode({ intakeActive = false, govActive = false, chatActive = false, sessionRunning = false } = {}) {
  if (intakeActive) return "intake";
  if (govActive) return "gov";
  if (chatActive) return "chat";
  if (sessionRunning) return "running";
  return "work";
}

/* Work 同时承载对话与任务，但只自动分流“整条消息都明确是对话”的短输入。
   集合必须保持克制：模糊问题仍走原有任务链，不能靠关键词猜测并改变执行语义。
   去除标点/表情只用于识别“你好👋”等等价问候；“你好，帮我写文件”不会命中。 */
const DIRECT_CONVERSATION = new Set([
  "你好", "你好啊", "你好呀", "您好", "您好呀", "你们好", "大家好", "嗨", "哈喽",
  "hello", "hi", "hey", "yo", "goodmorning", "goodafternoon", "goodevening",
  "早上好", "上午好", "中午好", "下午好", "晚上好", "晚安", "在吗", "在么",
  "你好吗", "howareyou", "howareyoudoing", "最近怎么样", "近来如何",
  "谢谢", "谢谢你", "谢谢你们", "感谢", "非常感谢", "thanks", "thankyou", "thx",
  "再见", "拜拜", "bye", "goodbye", "seeyou", "回头见",
  "你是谁", "你叫什么", "你的名字是什么", "怎么称呼你", "介绍一下你自己", "自我介绍",
  "whoareyou", "whatisyourname", "你能做什么", "你会什么", "你有什么能力", "whatcanyoudo",
]);

export function isDirectConversation(text) {
  const value = String(text || "").trim().toLowerCase();
  if (!value || value.length > 40) return false;
  const normalized = value.replace(/[\p{P}\p{S}\s]+/gu, "");
  return DIRECT_CONVERSATION.has(normalized);
}
