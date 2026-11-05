import test from "node:test";
import assert from "node:assert/strict";

import { toolArgsSummary, toolCardHtml, toolBlocksHtml } from "../core/tool-card.js";

const render = text => `<md>${text}</md>`;

test("args 摘要:read_file 显示路径,ls_workspace 显示工作区", () => {
  assert.equal(toolArgsSummary("read_file", '{"path":"src/a.py"}'), "src/a.py");
  assert.equal(toolArgsSummary("ls_workspace", "{}"), "工作区文件");
  assert.equal(toolArgsSummary("other", '{"q":"x"}'), "q=x");
  assert.equal(toolArgsSummary("bad", "not-json"), "not-json");
});

test("工具卡:三态状态点 + 参数/结果/哈希,且全部转义", () => {
  const html = toolCardHtml({
    type: "tool", call_id: "call_1", name: "read_file",
    arguments: '{"path":"<script>alert(1)</script>"}',
    status: "ok", result: { content: "hi <b>" }, result_hash: "abcdef1234567890",
  });
  assert.match(html, /chat-tool ok/);
  assert.match(html, /read_file/);
  assert.ok(!html.includes("<script>"), "参数必须转义");
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /hash abcdef123456/);
  assert.match(html, /完成/);

  const running = toolCardHtml({ type: "tool", call_id: "c2", name: "ls_workspace",
    arguments: "{}", status: "running" });
  assert.match(running, /chat-tool running/);
  assert.match(running, /运行中/);
});

test("错误工具卡:显示错误码与消息", () => {
  const html = toolCardHtml({ type: "tool", call_id: "c3", name: "write_file",
    arguments: '{"path":"x"}', status: "error",
    error: { code: "TOOL_NOT_REGISTERED", message: "not allowed" } });
  assert.match(html, /TOOL_NOT_REGISTERED/);
  assert.match(html, /失败/);
});

test("blocks 按序渲染:文本用 markdown,工具用卡片", () => {
  const html = toolBlocksHtml([
    { type: "text", text: "先看文件" },
    { type: "tool", call_id: "c1", name: "read_file", arguments: '{"path":"a"}', status: "ok" },
    { type: "text", text: "看完了" },
  ], render);
  const toolIndex = html.indexOf("chat-tool");
  assert.ok(html.indexOf("<md>先看文件</md>") < toolIndex, "第一段文本在工具卡之前");
  assert.ok(html.indexOf("<md>看完了</md>") > toolIndex, "第二段文本在工具卡之后");
});
