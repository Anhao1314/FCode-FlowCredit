import test from "node:test";
import assert from "node:assert/strict";

import { GOV_COMMANDS, filterSlashCommands } from "../core/slash-scope.js";

const commands = [
  { cmd: "/new", desc: "新建任务" },
  { cmd: "/挖洞", desc: "治理台:发起 REA 挖洞" },
  { cmd: "/audit", desc: "治理台:发起 REA 挖洞" },
  { cmd: "/判断", desc: "治理台:判断汇总" },
  { cmd: "/链", desc: "治理台:链验证" },
  { cmd: "/账本", desc: "工作区账本卡" },
  { cmd: "/remember", desc: "长期记忆" },
  { cmd: "/help", desc: "帮助" },
  { cmd: "/clear", desc: "清空输入框" },
];

test("治理视图只列治理命令(含 help/clear 控制台操作)", () => {
  const gov = filterSlashCommands(commands, { gov: true });
  assert.deepEqual(gov.map(c => c.cmd), ["/挖洞", "/audit", "/判断", "/链", "/help", "/clear"]);
  assert.deepEqual(GOV_COMMANDS, ["/挖洞", "/audit", "/判断", "/链", "/help", "/clear"]);
});

test("非治理视图列全部命令,且返回副本不改原数组", () => {
  const all = filterSlashCommands(commands, { gov: false });
  assert.equal(all.length, commands.length);
  all.pop();
  assert.equal(commands.length, 9, "过滤不得改动源清单");
  // 参数缺省 = 非治理视图
  assert.equal(filterSlashCommands(commands).length, 9);
});

test("空清单/缺参不炸", () => {
  assert.deepEqual(filterSlashCommands([], { gov: true }), []);
  assert.deepEqual(filterSlashCommands(undefined, { gov: true }), []);
});
