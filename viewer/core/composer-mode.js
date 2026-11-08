"use strict";

/* Composer 模式解析（S1 Chat/Work 分态切片）：唯一真相表。
 *
 * 同一个输入框按语境有四种提交语义——intake 补充描述 / chat 发消息 /
 * running 运行插话 / work 新委托。dispatch 只查本函数的结果；三类判据
 * （intake 覆盖层、chat 视图、运行中的会话）只活在这里，不再散布在
 * 提交路径里。优先级与既有行为逐条一致：intake > chat > running > work。
 */

export function composerMode({ intakeActive = false, chatActive = false, sessionRunning = false } = {}) {
  if (intakeActive) return "intake";
  if (chatActive) return "chat";
  if (sessionRunning) return "running";
  return "work";
}
