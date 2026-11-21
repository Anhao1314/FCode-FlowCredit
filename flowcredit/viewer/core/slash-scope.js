"use strict";

/* 治理台命令域（前端纯逻辑）：治理视图的斜杠菜单只列治理命令。

   设计依据：治理台是**零模型**的操作员控制台——委托/记忆/对话命令
   （/new /remember /账本 …）在这里没有语义，列出来只会制造噪音。
   /help 与 /clear 是控制台本身的操作（帮助、清空输入），保留。
   非治理视图（Work/对话）仍列全部命令——命令是同一套,作用域随视图收。 */

export const GOV_COMMANDS = ["/挖洞", "/audit", "/判断", "/链", "/help", "/clear"];

const GOV_ALLOW = new Set(GOV_COMMANDS);

/* 按视图过滤命令清单。commands 是 {cmd, desc, run} 形状;返回同形状数组。 */
export function filterSlashCommands(commands, { gov = false } = {}) {
  const list = commands || [];
  if (!gov) return list.slice();
  return list.filter(command => GOV_ALLOW.has(command.cmd));
}
