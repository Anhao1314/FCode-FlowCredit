import test from "node:test";
import assert from "node:assert/strict";

import { botAvatarHtml } from "../core/bot-avatar.js";

test("审计员与复核员是两套不同的几何，不只换字", () => {
  const auditor = botAvatarHtml("auditor", "idle", 32);
  const verifier = botAvatarHtml("verifier", "idle", 32);
  assert.match(auditor, /bot-avatar-auditor/);
  assert.match(auditor, /ba-sweep/);
  assert.match(auditor, /ba-eye/);
  assert.match(verifier, /bot-avatar-verifier/);
  assert.match(verifier, /ba-shield/);
  assert.match(verifier, /ba-check/);
  assert.match(verifier, /ba-orbit/);
  assert.doesNotMatch(auditor, /ba-shield/);
  assert.doesNotMatch(verifier, /ba-sweep/);
});

test("运行态进 class，尺寸进 CSS 变量，状态有中文可及名", () => {
  const running = botAvatarHtml("auditor", "running", 34);
  assert.match(running, /is-running/);
  assert.match(running, /--bot-size:34px/);
  assert.match(running, /aria-label="审计员 · 执行中"/);
  const failed = botAvatarHtml("verifier", "failed", 28);
  assert.match(failed, /is-failed/);
  assert.match(failed, /aria-label="复核员 · 失败"/);
  const pending = botAvatarHtml("auditor", "pending", 30);
  assert.match(pending, /is-pending/);
  assert.match(pending, /aria-label="审计员 · 待批准"/);
});

test("未知角色回落到审计员，未知状态回落到待命", () => {
  const html = botAvatarHtml("planner", "thinking", 20);
  assert.match(html, /bot-avatar-auditor/);
  assert.match(html, /is-idle/);
  assert.match(html, /--bot-size:20px/);
});

test("尺寸有下限，标签被转义", () => {
  const html = botAvatarHtml("auditor", "idle", 4, '<img src=x>');
  assert.match(html, /--bot-size:16px/);
  assert.equal(html.includes("<img"), false);
  assert.match(html, /&lt;img src=x&gt;/);
});