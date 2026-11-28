"use strict";

/* Work 入口意图分流（chat / clarify / work）——纯确定性规则。

   这是 src/flowagent/intent_router.py 的前端镜像：两边共用
   fixtures/intent-router/cases.json 并各自被测试钉死，规则改动必须
   同步两侧。UI 不自行猜测任务类型——它消费这里的稳定契约：

     { route: "chat|clarify|work", via: "deterministic",
       reason: "...", certainty: "high|low" }

   - 高置信度（greeting / 明确任务 / 纯问句）本地即时分流，零额外调用；
   - certainty:"low" 的残余桶，UI 会先问 /api/intake/route（服务端可选
     语义分类器），失败时沿用本地的保守结果；
   - 语义分类器永远不可能扩大工具权限：它只决定入口，不决定 grant。 */

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

const QUESTION_OR_EXPLANATION = /(?:什么是|什么意思|为什么|为何|怎么|如何|怎样|解释一下?|介绍下?|讲解|讲讲|说说|你会|你能|能否|是否|吗\s*[？?]?$)|^(?:what|why|how|when|where|which|can you|could you|do you|are you|is |are |explain|describe|tell me)\b/i;
const DIRECT_EXECUTION_REQUEST = /(?:帮我|帮忙|请|直接|替我|为我|给我).{0,20}(?:修改|写入|执行|创建|生成|实现|修复|运行|测试|部署|安装|重构|开发|导出|读取|扫描|审计|整理|核对|设计)|\b(?:please|directly)\b.{0,40}\b(?:modify|write|execute|create|generate|implement|fix|run|test|deploy|install|refactor|develop|export|read|scan|audit|design)\b/i;
const CHINESE_WORK_ACTION = /(?:^|[\s，,。；;：:！!？?])(?:(?:请|麻烦|帮我|帮忙|替我|为我|给我|继续|直接|先|再|去|把|将|对)\s*){0,3}(?:创建|生成|修改|修复|重构|实现|开发|运行|执行|测试|验证|审计|扫描|部署|安装|构建|编译|设计|导出|读取|整理|核对|调研|检查|排查|更新|删除|新增|添加|优化|改造|写|修)(?!作(?:方法|文|业))/;
const CHINESE_OBJECT_ACTION = /(?:把|将).{1,80}(?:修改|修复|重构|实现|运行|执行|测试|验证|部署|安装|构建|编译|导出|删除|更新|优化|改造|整理|核对|改成|改为)/;
const ENGLISH_WORK_ACTION = /(?:^|\s)(?:create|write|fix|refactor|implement|develop|run|execute|test|verify|audit|scan|deploy|install|build|compile|design|export|read|inspect|update|delete|remove|add|optimi[sz]e|research|analy[sz]e|summari[sz]e|translate)\b/i;

const CONTINUATION = /继续|接着|然后|刚才|刚刚|之前|上次|那个|这个|它|同上/;
const ACTION_HINT = /查|找|算|写|做|处理|分析|汇总|总结|翻译|改|修|跑|生成|整理|核对|调研/;
const CONCRETE_OBJECT = /[\w\-./\\]+\.(?:py|js|ts|tsx|jsx|md|txt|json|csv|html|css|yaml|yml|ini|sh|go|rs|java|sql|toml)\b|bug|错误|报错|异常|问题|需求|文档|报告|脚本|页面|接口|测试|函数|模块|目录|项目|文件|数据|版本|配置|环境|服务|仓库|代码|表格|清单|摘要|结论/i;
const QUESTION_TAIL = /[？?]|吗\b|呢\b/i;
const TAIL_ACTION = /(?:直接|请|帮我|帮忙|然后|接着|再)\s*(?:修复|修改|运行|执行|创建|生成|写入|测试|部署|安装|重构|开发|实现|核对|整理|设计|扫描|审计|读取|导出)\s*[。.！!]?\s*$/;

export const ROUTES = { CHAT: "chat", CLARIFY: "clarify", WORK: "work" };

export function isDirectConversation(text) {
  const value = String(text || "").trim().toLowerCase();
  if (!value || value.length > 40) return false;
  const normalized = value.replace(/[\p{P}\p{S}\s]+/gu, "");
  return DIRECT_CONVERSATION.has(normalized);
}

function hasExplicitWorkIntent(value) {
  if (QUESTION_OR_EXPLANATION.test(value) && !DIRECT_EXECUTION_REQUEST.test(value)) return false;
  return CHINESE_WORK_ACTION.test(value) || CHINESE_OBJECT_ACTION.test(value)
    || ENGLISH_WORK_ACTION.test(value);
}

function concreteObject(value) {
  return CONCRETE_OBJECT.test(value);
}

function decision(route, reason, certainty = "high") {
  return { route, via: "deterministic", reason, certainty };
}

/** 稳定契约的三分流决策；与 Python 端 flowagent.intent_router 逐条对齐。 */
export function routeIntent(text, context = {}) {
  const value = String(text || "").trim();
  if (!value) return decision("chat", "empty-input");
  if (isDirectConversation(value)) return decision("chat", "greeting-or-smalltalk");
  if (value.startsWith("/")) return decision("work", "slash-command");
  if (hasExplicitWorkIntent(value)) return decision("work", "explicit-execution-intent");
  const question = QUESTION_OR_EXPLANATION.test(value);
  if (question && TAIL_ACTION.test(value)) {
    if (concreteObject(value)) return decision("work", "question-with-execution-and-object");
    return decision("clarify", "execution-tendency-missing-object");
  }
  if (question) return decision("chat", "question-or-explanation");
  if (CONTINUATION.test(value) && ACTION_HINT.test(value) && !concreteObject(value)) {
    if (context.has_project || context.recent_work_thread) {
      return decision("work", "continuation-with-context");
    }
    return decision("clarify", "continuation-without-context");
  }
  if (ACTION_HINT.test(value) && !QUESTION_TAIL.test(value)) {
    if (concreteObject(value)) return decision("work", "action-with-object");
    return decision("clarify", "execution-tendency-missing-object");
  }
  return decision("chat", "ambiguous-falls-back-to-chat", "low");
}

/** 旧的两分法契约：只有高置信 work 才绕过 Chat；clarify 归入"不走 Chat"。 */
export function workSubmissionRoute(text) {
  return routeIntent(text).route === "work" ? "work" : "chat";
}
