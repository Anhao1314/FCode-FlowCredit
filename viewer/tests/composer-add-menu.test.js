import test from "node:test";
import assert from "node:assert/strict";
import { composerAddMenuHtml, workAddMenuHtml } from "../core/composer-add-menu.js";

test("chat add menu presents files and workspace context", () => {
  const html = composerAddMenuHtml({ attachmentCount: 1 });
  assert.match(html, /添加内容/);
  assert.match(html, /上传文本文件/);
  assert.match(html, /引用工作区文件/);
  assert.match(html, /1\/3/);
  assert.doesNotMatch(html, /disabled/);
  assert.match(html, /不会执行工具/);
});

test("file row disables at three attachments", () => {
  const html = composerAddMenuHtml({ attachmentCount: 3 });
  assert.match(html, /已达 3 个上限/);
  assert.match(html, /data-add-action="files" disabled/);
});

test("workspace form renders and escapes an error", () => {
  const html = composerAddMenuHtml({ workspaceOpen: true, error: "<bad>" });
  assert.match(html, /data-add-workspace-form/);
  assert.match(html, /data-add-workspace-input/);
  assert.match(html, /&lt;bad&gt;/);
  assert.doesNotMatch(html, /<bad>/);
});

test("work add menu routes exactly four kinds of context", () => {
  const html = workAddMenuHtml({
    general: true, sourceCount: 2, project: "flowcredit", toolCount: 7,
    template: "通用委托", budget: "60 calls · 3,000,000 µ",
  });
  assert.match(html, /添加内容/);
  for (const title of ["资料与来源", "项目", "工具", "更多设置"]) assert.match(html, new RegExp(title));
  assert.equal((html.match(/data-work-add=/g) || []).length, 4);
  assert.match(html, /2\/5 个外部来源/);
  assert.match(html, /flowcredit/);
  assert.match(html, /7 个已注册工具/);
  assert.match(html, /60 calls/);
  assert.doesNotMatch(html, /disabled/);
  assert.match(html, /只影响本次委托/);
});

test("finance work menu disables project and shows corpus scope", () => {
  const html = workAddMenuHtml({ general: false, scopeCount: 3 });
  assert.match(html, /已限定 3 份语料/);
  assert.match(html, /data-work-add="project" disabled/);
  assert.match(html, /仅通用委托支持/);
});

test("work menu escapes project and template text", () => {
  const html = workAddMenuHtml({ project: "<img>.py", template: "<script>" });
  assert.match(html, /&lt;img&gt;\.py/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<img>/);
  assert.doesNotMatch(html, /<script>/);
});