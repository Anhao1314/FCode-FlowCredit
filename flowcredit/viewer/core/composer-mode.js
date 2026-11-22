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

/* 纯对话快速路径只负责保留既有问候契约；完整 Work 分流见文件底部。
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

/* Work 入口采用保守的确定性规则：只有明确的执行/交付意图才自动创建 Run。
   这不是语义模型分类器；无法确定的输入统一交给真实 Chat，由模型回答或提出
   可确认的执行提案，避免一句问题或寒暄直接消耗任务预算。 */
const QUESTION_OR_EXPLANATION = /(?:什么是|什么意思|为什么|为何|怎么|如何|怎样|解释一下?|介绍下?|讲解|讲讲|说说|你会|你能|能否|是否|吗\s*[？?]?$)|^(?:what|why|how|when|where|which|can you|could you|do you|are you|is |are |explain|describe|tell me)\b/i;
const DIRECT_EXECUTION_REQUEST = /(?:帮我|帮忙|请|直接|替我|为我|给我).{0,20}(?:修改|写入|执行|创建|生成|实现|修复|运行|测试|部署|安装|重构|开发|导出|读取|扫描|审计|整理|核对|设计)|\b(?:please|directly)\b.{0,40}\b(?:modify|write|execute|create|generate|implement|fix|run|test|deploy|install|refactor|develop|export|read|scan|audit|design)\b/i;
const CHINESE_WORK_ACTION = /(?:^|[\s，,。；;：:！!？?])(?:(?:请|麻烦|帮我|帮忙|替我|为我|给我|继续|直接|先|再|去|把|将|对)\s*){0,3}(?:创建|生成|修改|修复|重构|实现|开发|运行|执行|测试|验证|审计|扫描|部署|安装|构建|编译|设计|导出|读取|整理|核对|调研|检查|排查|更新|删除|新增|添加|优化|改造|写|修)(?!作(?:方法|文|业))/;
const CHINESE_OBJECT_ACTION = /(?:把|将).{1,80}(?:修改|修复|重构|实现|运行|执行|测试|验证|部署|安装|构建|编译|导出|删除|更新|优化|改造|整理|核对|改成|改为)/;
const ENGLISH_WORK_ACTION = /(?:^|\s)(?:create|write|fix|refactor|implement|develop|run|execute|test|verify|audit|scan|deploy|install|build|compile|design|export|read|inspect|update|delete|remove|add|optimi[sz]e|research|analy[sz]e|summari[sz]e|translate)\b/i;

function hasExplicitWorkIntent(value) {
  if (QUESTION_OR_EXPLANATION.test(value) && !DIRECT_EXECUTION_REQUEST.test(value)) return false;
  return CHINESE_WORK_ACTION.test(value) || CHINESE_OBJECT_ACTION.test(value) || ENGLISH_WORK_ACTION.test(value);
}

export function workSubmissionRoute(text) {
  const value = String(text || "").trim();
  if (!value || isDirectConversation(value)) return "chat";
  // 斜杠命令仍由既有命令分发器处理，不被对话路由截获。
  if (value.startsWith("/")) return "work";
  return hasExplicitWorkIntent(value) ? "work" : "chat";
}
