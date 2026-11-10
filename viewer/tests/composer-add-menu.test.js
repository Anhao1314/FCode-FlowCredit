import test from "node:test";
import assert from "node:assert/strict";
import { composerAddMenuHtml } from "../core/composer-add-menu.js";

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