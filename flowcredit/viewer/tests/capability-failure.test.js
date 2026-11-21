import test from "node:test";
import assert from "node:assert/strict";

import { capabilityFailure, capabilityFailureHtml } from "../app/task-thread.js";

/* A3 失败诊断卡：链上 capability_failed → typed facts → 中文为主的投影。 */

const TRACE = {
  events: [
    { type: "tool_succeeded", payload: { detail: { tool: "write_file" } } },
    { type: "capability_failed", payload: { detail: {
      kind: "motion", stage: "motion", reason: "ACTION_REPAIR_EXHAUSTED_NO_VALID",
      quality: { score: 40, composition_ok: true, issues: ["wheel speed 220 vs 0"] },
      required_fixes: [
        { code: "ANIMATION_NAME", expected: "wheel-spin / pedal", actual: "none",
          selector: ".wheel/.leg/.scenery", message: "rename the animation to the exact contract names" },
      ],
      artifact: { path: "index.html", bytes: 5537, sha256: "563114f4aefe1083befbd78314c1ed3f50f5546cb858be92044fb1685d1bb160" },
    } } },
    { type: "task_failed", payload: { detail: { code: "VIEWER_RUNNER_ERROR" } } },
  ],
};

test("capabilityFailure 取最后一条 capability_failed，只认链上事件", () => {
  const diag = capabilityFailure(TRACE);
  assert.equal(diag.stage, "motion");
  assert.equal(diag.reason, "ACTION_REPAIR_EXHAUSTED_NO_VALID");
  assert.equal(diag.artifact.path, "index.html");
  assert.equal(diag.requiredFixes.length, 1);
  assert.equal(capabilityFailure({ events: [] }), null);
  assert.equal(capabilityFailure(null), null);
  // 没有诊断事实（无 stage/无 fixes/无 artifact）→ null
  assert.equal(capabilityFailure({ events: [
    { type: "capability_failed", payload: { detail: { reason: "X" } } },
  ] }), null);
});

test("诊断卡：中文为主、原始码 mono、artifact/fixes/quality 齐全", () => {
  const html = capabilityFailureHtml(capabilityFailure(TRACE));
  assert.match(html, /失败诊断/);
  assert.match(html, /宿主检查 · 链上事件/);
  assert.match(html, /<code>motion<\/code>/);
  assert.match(html, /ANIMATION_NAME/);
  assert.match(html, /预期 <code>wheel-spin \/ pedal<\/code>/);
  assert.match(html, /实际 <code>none<\/code>/);
  assert.match(html, /\.wheel\/\.leg\/\.scenery/);
  assert.match(html, /index\.html/);
  assert.match(html, /5\.4 KB · 563114f4aefe1083/);
  assert.match(html, /质量分 40/);
  assert.match(html, /wheel speed 220 vs 0/);
  // 诊断与判断面分离：不含审阅按钮
  assert.doesNotMatch(html, /review-panel|判断/);
});

test("没有任何事实 → 空串，不渲染空段落", () => {
  assert.equal(capabilityFailureHtml(null), "");
});

test("用户可见文本转义", () => {
  const html = capabilityFailureHtml({
    stage: "motion", reason: "X",
    requiredFixes: [{ code: "<script>", message: "<b>x</b>" }],
    quality: { issues: ["<script>y</script>"] },
    artifact: { path: "<img>", bytes: 1, sha256: "ab" },
  });
  assert.equal(html.includes("<script>"), false);
  assert.equal(html.includes("<img>"), false);
  assert.match(html, /&lt;script&gt;/);
});
