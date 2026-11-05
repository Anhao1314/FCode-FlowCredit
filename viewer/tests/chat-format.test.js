import test from "node:test";
import assert from "node:assert/strict";
import { parseSseFrame, renderMarkdown } from "../core/chat-format.js";

test("escapes HTML before formatting", () => {
  const html = renderMarkdown('<img src=x onerror="alert(1)">');
  assert.ok(html.includes("&lt;img"));
  assert.ok(!html.includes("<img"));
});

test("renders headings, lists and fenced code", () => {
  const html = renderMarkdown("## 标题\n\n- 一\n- 二\n\n```js\nconst a = 1 < 2;\n```");
  assert.ok(html.includes("<h3>标题</h3>"));
  assert.ok(html.includes("<ul><li>一</li><li>二</li></ul>"));
  assert.ok(html.includes('<code data-lang="js">const a = 1 &lt; 2;</code>'));
});

test("renders inline code, bold and links safely", () => {
  const html = renderMarkdown("**粗** `x<y` [链接](https://example.com/a?b=1&c=2)");
  assert.ok(html.includes("<strong>粗</strong>"));
  assert.ok(html.includes("<code>x&lt;y</code>"));
  assert.ok(html.includes('href="https://example.com/a?b=1&amp;c=2"'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
});

test("an unclosed fence stays a code block", () => {
  const html = renderMarkdown("```py\nprint(1)");
  assert.ok(html.includes('<code data-lang="py">print(1)</code>'));
});

test("empty markdown is an ellipsis placeholder", () => {
  assert.equal(renderMarkdown(""), '<span class="dim">…</span>');
});

test("parses SSE data frames", () => {
  assert.deepEqual(parseSseFrame('event: x\ndata: {"a":1}\n'), { a: 1 });
  assert.deepEqual(parseSseFrame('data: {"a":\ndata: 1}'), { a: 1 });
  assert.equal(parseSseFrame(": keepalive"), null);
  assert.equal(parseSseFrame("data: nope"), null);
  assert.equal(parseSseFrame(""), null);
});

test("壳内审计深链:markdown 链接渲染为同文档锚点,不开新标签", async () => {
  const { renderMarkdown } = await import("../core/chat-format.js");
  const html = renderMarkdown("发现账本:[打开审计台](/#/audit/evox-2026-11-03)");
  assert.ok(html.includes('href="/#/audit/evox-2026-11-03"'), "深链 href 保留");
  assert.ok(!html.includes('target="_blank"'), "壳内导航不开新标签");
  // 外部链接行为不变
  const ext = renderMarkdown("[文档](https://example.com/x)");
  assert.ok(ext.includes('target="_blank"'), "外链保持新标签");
});
