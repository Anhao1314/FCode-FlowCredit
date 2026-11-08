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
