import test from "node:test";
import assert from "node:assert/strict";

import {
  AVATAR_CANVAS, AVATAR_INK, AVATAR_STYLE, aiAvatarHtml, avatarStateFor,
  userAvatarHtml,
} from "../core/ai-avatar.js";

test("状态来自真实事实：失败/停止/生成中优先于运行状态", () => {
  assert.equal(avatarStateFor({ status: "error" }), "bad");
  assert.equal(avatarStateFor({ status: "cancelled" }), "warn");
  assert.equal(avatarStateFor({ status: "streaming" }), "thinking");
  assert.equal(avatarStateFor({}), "idle");
  assert.equal(avatarStateFor(null), "idle");
});

test("锚定的运行决定状态：只有 succeeded 才算完成", () => {
  assert.equal(avatarStateFor({ run: { status: "succeeded" } }), "ok");
  assert.equal(avatarStateFor({ run: { status: "failed" } }), "bad");
  assert.equal(avatarStateFor({ run: { status: "denied" } }), "bad");
  assert.equal(avatarStateFor({ run: { status: "recovery_required" } }), "warn");
  assert.equal(avatarStateFor({ run: { status: "stopped" } }), "warn");
  assert.equal(avatarStateFor({ run: { status: "running" } }), "thinking");
  assert.equal(avatarStateFor({ status: "done", text: "hi" }), "idle");
});

test("黑灰脸上只有两只白眼，没有嘴和腮红", () => {
  const html = aiAvatarHtml("idle", 64);
  assert.match(html, /class="ai-avatar ai-avatar--idle"/);
  assert.match(html, /--ai-avatar-size:64px/);
  assert.match(html, /viewBox="0 0 64 64"/);
  assert.match(html, /data-ai-state="idle"/);
  assert.match(html, /data-live="1"/);
  assert.equal((html.match(/class="ai-eye"/g) || []).length, 2);
  assert.equal((html.match(/fill="#FFFFFF"/g) || []).length, 2);
  assert.doesNotMatch(html, /ellipse/);
  assert.doesNotMatch(html, /opacity=/);
});

test("两层动效容器分离：指针跟随、生成扫视、眨眼各自一层", () => {
  const html = aiAvatarHtml("idle", 28);
  assert.match(html, /class="ai-avatar-look"/);
  assert.match(html, /class="ai-avatar-scan"/);
  assert.match(html, /class="ai-avatar-eyes"/);
  assert.match(html, /--ai-avatar-size:28px/);
  assert.match(html, /width="28" height="28"/);
  assert.match(html, /role="img" aria-label="FCode"/);
});

test("生成中多一层扫视与呼吸环，其它状态没有", () => {
  const thinking = aiAvatarHtml("thinking", 28);
  assert.match(thinking, /is-thinking/);
  assert.match(thinking, /ai-avatar-ring/);
  assert.match(thinking, /aria-label="FCode · 正在生成"/);
  assert.equal(aiAvatarHtml("ok", 28).includes("ai-avatar-ring"), false);
  assert.equal(aiAvatarHtml("bad", 28).includes("ai-avatar-ring"), false);
});

test("大小尺寸共用同一张脸，不靠小尺寸降级", () => {
  for (const size of [24, 28, 44, 64]) {
    const html = aiAvatarHtml("idle", size);
    assert.equal((html.match(/class="ai-eye"/g) || []).length, 2, `${size}px 仍有两只眼`);
    assert.match(html, new RegExp(`width="${size}" height="${size}"`));
  }
});

test("未知状态回落待命，标签仍转义", () => {
  const html = aiAvatarHtml("nonsense", 28);
  assert.match(html, /ai-avatar--idle/);
  assert.match(html, /data-ai-state="idle"/);
  for (const state of Object.keys(AVATAR_STYLE)) {
    assert.equal((aiAvatarHtml(state, 28).match(/class="ai-eye"/g) || []).length, 2);
  }
});

test("操作员头像仍是黑白墨盘，不与蓝色 AI 脸混淆", () => {
  const html = userAvatarHtml(28);
  assert.match(html, /class="chat-user-avatar"/);
  assert.match(html, new RegExp(`fill="${AVATAR_INK}"`));
  assert.match(html, new RegExp(`fill="${AVATAR_CANVAS}"`));
  assert.doesNotMatch(html, /ai-eye/);
});