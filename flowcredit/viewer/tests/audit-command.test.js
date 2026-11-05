import test from "node:test";
import assert from "node:assert/strict";

import { auditStatusText, parseAuditCommand } from "../app/chat-ui.js";

test("/audit <target> 提取目标,前后空白被吞掉", () => {
  assert.equal(parseAuditCommand("/audit fixtures/demo-target"), "fixtures/demo-target");
  assert.equal(parseAuditCommand("  /audit   ./  "), "./");
  assert.equal(parseAuditCommand("/audit /abs/path"), "/abs/path");
});

test("非审计消息一律返回 null,普通斜杠命令不受影响", () => {
  assert.equal(parseAuditCommand(null), null);
  assert.equal(parseAuditCommand(""), null);
  assert.equal(parseAuditCommand("帮我整理发现"), null);
  assert.equal(parseAuditCommand("/audit"), null, "裸 /audit 没有目标,不触发");
  assert.equal(parseAuditCommand("/auditfoo"), null, "前缀粘连不是审计命令");
  assert.equal(parseAuditCommand("/new"), null);
});

test("作业状态到卡片区文案的映射", () => {
  assert.ok(auditStatusText({ status: "running", job_id: "audit-x" }).includes("⏳"));
  const done = auditStatusText({ status: "done", findings: 2, evidence: 2,
    audit_id: "evox-2026-11-03" });
  assert.ok(done.includes("✅") && done.includes("2 条发现"),
    "完成卡片给统一壳内审计面的深链");
  assert.ok(done.includes("[打开审计台](/#/audit/evox-2026-11-03)"),
    "链接用 markdown 形式,渲染为可点的壳内锚点");
  const doneNoId = auditStatusText({ status: "done", findings: 0, evidence: 0 });
  assert.ok(doneNoId.includes("[打开审计台](/#/audit)"),
    "作业缺 audit_id 时回落到审计面根深链");
  assert.ok(auditStatusText({ status: "failed", error: "boom" }).includes("boom"));
  assert.equal(auditStatusText(null), "审计任务不存在");
});
