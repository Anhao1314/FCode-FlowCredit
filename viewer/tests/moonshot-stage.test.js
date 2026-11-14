import test from "node:test";
import assert from "node:assert/strict";
import { moonshotStageHtml } from "../core/moonshot-stage.js";

test("Work opening scene keeps the composer contract and creator message", () => {
  const html = moonshotStageHtml("给想法一个运行空间", { work: true, footnote: "每步可验证" });
  assert.match(html, /Build your/);
  assert.match(html, /own work\./);
  assert.match(html, /id="stage-dock"/);
  assert.match(html, /class="stage-suggestions"/);
  assert.match(html, /data-stage-prompt="审计这个目录/);
  assert.doesNotMatch(html, /stage-prompts|stage-chips|class="stage-foot"/);
});

test("Chat gets its own opening message and hides delegation templates", () => {
  const html = moonshotStageHtml("整理思路", { chat: true });
  assert.match(html, /Bring proof/);
  assert.match(html, /to ideas\./);
  assert.doesNotMatch(html, /Bring light/);
  assert.match(html, />CHAT<\/span>/);
});

test("Operator text is escaped in the opening scene", () => {
  const html = moonshotStageHtml("<img src=x><script>bad()</script>", { work: true });
  assert.doesNotMatch(html, /<img src=x>/);
  assert.doesNotMatch(html, /<script>bad/);
  assert.match(html, /&lt;img src=x&gt;/);
  assert.match(html, /&lt;script&gt;bad\(\)&lt;\/script&gt;/);
});
