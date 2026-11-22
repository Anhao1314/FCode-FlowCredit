import test from "node:test";
import assert from "node:assert/strict";

import {
  fmtCountdown, isWriteLike, approvalVerb, approvalActionTitle,
  approvalCommandHtml, approvalDiffHtml, approvalImpactHtml, approvalComputerHtml,
} from "../app/approval-card.js";

test("fmtCountdown:负数截 0,分钟补零", () => {
  assert.equal(fmtCountdown(65), "01:05");
  assert.equal(fmtCountdown(5), "00:05");
  assert.equal(fmtCountdown(-3), "00:00");
  assert.equal(fmtCountdown(3600), "60:00");
});

test("isWriteLike:写入类与 MCP 工具默认 consequential,读类不算", () => {
  for (const tool of ["write_file", "edit_file", "run_command", "working_paper_append", "browser_act"]) {
    assert.ok(isWriteLike(tool), tool);
  }
  assert.ok(isWriteLike("mcp__anything"));
  assert.ok(!isWriteLike("read_file"));
  assert.ok(!isWriteLike("ls_workspace"));
});

test("approvalVerb:写类动词优先,MCP 回退'写入',未知名回退空", () => {
  assert.equal(approvalVerb("run_command"), "执行");
  assert.equal(approvalVerb("mcp__vendor_tool"), "写入");
  assert.equal(approvalVerb("reconcile_basis"), "核对口径");
  assert.equal(approvalVerb("unknown_tool"), "");
});

test("approvalActionTitle:动词化标题,未知名用工具名兜底", () => {
  assert.equal(approvalActionTitle("edit_file"), "允许这次编辑？");
  assert.equal(approvalActionTitle("mcp__x"), "允许这次写入？");
  assert.equal(approvalActionTitle("weird_thing"), "允许这次 weird_thing 调用？");
});

test("approvalCommandHtml:完整命令行与治理要点,命令文本转义", () => {
  const html = approvalCommandHtml({ command: 'ls -la "a b"; rm -rf /', timeout: 45 });
  assert.ok(html.includes("apv-cmdline"));
  assert.ok(html.includes("&quot;a b&quot;"), "命令必须经 esc 转义");
  assert.ok(!html.includes('<code>ls -la "a b"'), "不得注入原始引号");
  assert.ok(html.includes("45 秒"));
  assert.ok(html.includes("白名单受限终端"));
});

test("approvalDiffHtml:按行分类着色;沙箱拒绝时如实显示错误", () => {
  const ok = approvalDiffHtml({ diff: "+add\n-del\n@@ -1 +1 @@\n ctx", additions: 1, removals: 1 });
  assert.ok(ok.includes("diff-line add"));
  assert.ok(ok.includes("diff-line del"));
  assert.ok(ok.includes("diff-line hunk"));
  assert.ok(ok.includes("+1 / −1"));
  const denied = approvalDiffHtml({ error: "SANDBOX DENY" });
  assert.ok(denied.includes("预览被沙箱拒绝"));
  assert.ok(denied.includes("SANDBOX DENY"));
});

test("approvalImpactHtml:影响对象命中目标键,空值行被过滤,值转义", () => {
  const html = approvalImpactHtml({ path: "ledger<x>.md", note: "", extra: 0 });
  assert.ok(html.includes("影响对象"));
  assert.ok(html.includes("path=ledger&lt;x&gt;.md"), "目标键值必须转义");
  assert.ok(!html.includes(">note<"), "空值参数不进影响区");
  assert.ok(html.includes("extra"));
});

test("approvalComputerHtml:会话级 scope 一次说清,破坏性默认阻止", () => {
  assert.ok(isWriteLike("computer_request"));
  assert.equal(approvalVerb("computer_request"), "授权电脑使用");
  const html = approvalComputerHtml({ computer: {
    mode: "both", apps: ["com.apple.TextEdit"], domains: ["example.com"],
    duration_seconds: 300, max_actions: 30, allow_destructive: false,
    reason: "整理文档",
  } });
  assert.ok(html.includes("电脑使用授权"));
  assert.ok(html.includes("com.apple.TextEdit"));
  assert.ok(html.includes("example.com"));
  assert.ok(html.includes("300 秒"));
  assert.ok(html.includes("阻止（默认）"));
  assert.ok(html.includes("整理文档"));
});
