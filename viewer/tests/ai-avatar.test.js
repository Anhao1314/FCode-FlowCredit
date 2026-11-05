import test from "node:test";
import assert from "node:assert/strict";

import {
  AVATAR_CANVAS, AVATAR_INK, AVATAR_STYLE, aiAvatarHtml, avatarStateFor,
} from "../core/ai-avatar.js";

test("状态来自真实事实：失败/停止/生成中优先于运行状态", () => {
  assert.equal(avatarStateFor({ status: "error" }), "bad");
  assert.equal(avatarStateFor({ status: "cancelled" }), "warn");
  assert.equal(avatarStateFor({ status: "streaming" }), "thinking");
  assert.equal(avatarStateFor({}), "idle");
  assert.equal(avatarStateFor(null), "idle");
});

test("锚定的运行决定脸：只有 succeeded 才算完成", () => {
  assert.equal(avatarStateFor({ run: { status: "succeeded" } }), "ok");
  assert.equal(avatarStateFor({ run: { status: "failed" } }), "bad");
  assert.equal(avatarStateFor({ run: { status: "denied" } }), "bad");
  assert.equal(avatarStateFor({ run: { status: "recovery_required" } }), "warn");
  assert.equal(avatarStateFor({ run: { status: "stopped" } }), "warn");
  assert.equal(avatarStateFor({ run: { status: "running" } }), "thinking");
  // 有回答但没有运行：待命，不冒充已完成
  assert.equal(avatarStateFor({ status: "done", text: "hi" }), "idle");
});

function big_stroke_check() {
  // 大尺寸的线稿圆用细笔画 2.2；落印章不描边（整块填墨）
  return /r="25.2" fill="none" stroke="#1B1B1F" stroke-width="2.2"/.test(aiAvatarHtml("idle", 64));
}

test("分级形象：28px 只给印章标记，44px 起才画脸（Kimi 同款分级）", () => {
  const small = aiAvatarHtml("idle", 28);
  assert.match(small, /class="ai-avatar ai-avatar--idle ai-avatar--mark"/);
  assert.match(small, /--ai-avatar-size:28px/);
  assert.match(small, /width="28" height="28"/);
  assert.match(small, /role="img" aria-label="FCode"/);
  assert.match(small, /data-ai-state="idle"/);
  // 小尺寸：圆 + 印章骨架（F 三笔 + 撕口 + 记号），没有脸
  assert.equal(small.includes("M32 12c8.4"), false, "28px 不画脸");
  assert.match(small, /M25 41V23h10/, "给的是印章 F 骨架");
  assert.equal((small.match(/<circle/g) || []).length, 2, "圆 + 空心点");
  assert.equal(big_stroke_check(), true);

  const big = aiAvatarHtml("idle", 64);
  assert.match(big, /--ai-avatar-size:64px/);
  assert.match(big, /viewBox="0 0 64 64"/);
  assert.match(big, /M32 12c8.4/, "大尺寸画 Q 版大头");
  assert.equal((big.match(/<ellipse/g) || []).length, 2, "待命是两只大圆眼");
  assert.match(big, /opacity="\.45"/, "腮红是两颗网点");
});

test("生成中多一圈呼吸环，其它状态没有", () => {
  assert.match(aiAvatarHtml("thinking", 28), /ai-avatar-ring/);
  assert.match(aiAvatarHtml("thinking", 28), /is-thinking/);
  assert.equal(aiAvatarHtml("ok", 28).includes("ai-avatar-ring"), false);
  assert.equal(aiAvatarHtml("bad", 28).includes("ai-avatar-ring"), false);
});

test("黑白两色：状态靠盖章与否 + 记号，不靠颜色", () => {
  assert.equal(AVATAR_INK, "#1B1B1F");
  assert.equal(AVATAR_CANVAS, "#FBFAF9");
  // 线稿圆：只描边；落印章：整块填墨
  for (const state of ["idle", "warn"]) {
    const html = aiAvatarHtml(state, 28);
    assert.match(html, /<circle cx="32" cy="32" r="25.2" fill="none" stroke="#1B1B1F"/, `${state} 是线稿圆`);
  }
  for (const state of ["thinking", "ok", "bad"]) {
    const html = aiAvatarHtml(state, 28);
    assert.match(html, /<circle cx="32" cy="32" r="26" fill="#1B1B1F"/, `${state} 是落印章`);
  }
  // 记号各自不同：空心点 / 实心点 / 竖记 / 斜划
  assert.match(aiAvatarHtml("idle", 28), /<circle cx="43" cy="43" r="3" fill="none"/);
  assert.match(aiAvatarHtml("ok", 28), /<circle cx="43" cy="43" r="3.4" fill="#FBFAF9"/);
  assert.match(aiAvatarHtml("warn", 28), /M43 39.4v7.2/);
  assert.match(aiAvatarHtml("bad", 28), /M48 16L16 48/);
  // 画面上不出现任何品牌以外的彩色
  for (const state of Object.keys(AVATAR_STYLE)) {
    const html = aiAvatarHtml(state, 64);
    const hex = html.match(/#[0-9A-Fa-f]{6}/g) || [];
    for (const value of hex) assert.ok([AVATAR_INK, AVATAR_CANVAS].includes(value.toUpperCase()), `${state} 只出现墨与画布色: ${value}`);
  }
});

test("落印章上的头是挖空的（画布色），线稿章上的头是墨色", () => {
  assert.match(aiAvatarHtml("idle", 64), /fill="#1B1B1F"\/><ellipse/, "线稿圆：墨头 + 墨眼");
  assert.match(aiAvatarHtml("ok", 64), /fill="#FBFAF9"\/><path d="M23.4 27.9/, "落印章：挖空头 + 墨色眼");
});

test("四种情绪各有眼形与嘴形（黑白里只能靠形状）", () => {
  const thinking = aiAvatarHtml("thinking", 64);
  const ok = aiAvatarHtml("ok", 64);
  const warn = aiAvatarHtml("warn", 64);
  const bad = aiAvatarHtml("bad", 64);
  assert.match(thinking, /<ellipse cx="32" cy="35"/, "生成中：闭眼 + 小圆嘴");
  assert.match(ok, /M23.4 27.9c1.6-2.7/, "完成：弯弯眼 + 笑嘴 + 腮红");
  assert.match(warn, /M23.6 27.3h5.8/, "停止：平眼 + 平嘴");
  assert.match(bad, /M23.6 25.1l5 4.6/, "失败：叉眼 + 平嘴");
  assert.notEqual(thinking, ok);
  assert.notEqual(warn, bad);
});

test("未知状态回落待命，不抛错也不画空", () => {
  const html = aiAvatarHtml("nonsense", 28);
  assert.match(html, /ai-avatar--idle/);
  assert.match(html, /data-ai-state="idle"/);
});

test("阈值边界：44 画脸，43 不画（尺寸线性缩放，viewBox 不变）", () => {
  assert.match(aiAvatarHtml("idle", 44), /M32 12c8.4/);
  assert.equal(aiAvatarHtml("idle", 43).includes("M32 12c8.4"), false);
  assert.match(aiAvatarHtml("idle", 43), /width="43" height="43"/);
});
