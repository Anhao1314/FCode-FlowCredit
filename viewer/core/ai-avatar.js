/* 对话 AI 的头像（形象 + 头像框）。

   形态按 Kimi 的构造 1:1 来，形象是 FCode 自己的（2026-10-26 定稿方案 B）：
   - Kimi 工作端 `.avatar`：28×28、`border-radius: 50%`、浅底、内容居中；
     `detail-avatar`：24×24 + 0.5px `Separators-S1`（`#00000021`）细描边；
     首页的 AI 形象是一段 Rive 动画（`kimi_avatar_web-*.riv`），而消息流里
     默认**不画**头像（Segment 的 `hideAvatar`）。
   - 所以：圆形 + 细描边 + 分级形象（小尺寸给标记、大尺寸给脸）这三条是抄的。

   **黑白**（不要绿色，保持黑白）：只用墨 `#1B1B1F` 与画布 `#FBFAF9` 两色。
   状态因此不靠颜色，靠**盖章与否 + 记号**：

     idle     线稿圆：细描边 + 空心点
     thinking 落印章：实心圆 + 外圈呼吸（1.6s，reduced-motion 时静止）
     ok       落印章：实心（盖下去＝完成）
     warn     线稿圆 + 一道竖记（存疑/已停）
     bad      落印章 + 一道斜划（划掉）

   **Q 萌**：大尺寸里坐一颗 Q 版印豆——大头、大眼（挖空）、小嘴、两颗网点腮红。
   头与五官互为反色，所以线稿圆上是墨头挖空五官，落印章上是挖空头配墨色五官。
   28px 只留标记（印章 F 骨架 + 记号）：小尺寸画脸必糊。 */

import { esc } from "./format.js";

export const AVATAR_INK = "#1B1B1F";
export const AVATAR_CANVAS = "#FBFAF9";

/* 状态 → 画法。filled = 落印章（实心圆），否则线稿圆；mark 是圆内的记号。 */
export const AVATAR_STYLE = {
  idle: { filled: false, mark: "hollow", label: "FCode" },
  thinking: { filled: true, mark: "solid", label: "FCode · 正在生成" },
  ok: { filled: true, mark: "solid", label: "FCode · 已完成" },
  warn: { filled: false, mark: "tick", label: "FCode · 已停止" },
  bad: { filled: true, mark: "slash", label: "FCode · 调用失败" },
};

/* 画脸的最小尺寸：再小就只能看见标记（Kimi 也是小尺寸给标记、大尺寸给脸）。 */
export const FACE_MIN_SIZE = 44;

/* 消息投影 → 状态。先看这条回答自己的状态，再看它锚定的运行：
   没有事实就当待命，绝不猜「成功」。 */
export function avatarStateFor(message) {
  const msg = message || {};
  if (msg.status === "error") return "bad";
  if (msg.status === "cancelled") return "warn";
  if (msg.status === "streaming") return "thinking";
  const run = msg.run || {};
  if (run.status === "succeeded") return "ok";
  if (run.status === "failed" || run.status === "denied") return "bad";
  if (run.status === "recovery_required" || run.status === "stopped") return "warn";
  if (run.status === "running") return "thinking";
  return "idle";
}

/* 圆：落印章整块填墨，线稿圆只描边（Kimi 的 0.5px 细线，按尺寸放大到看得见）。 */
function disc(style, stroke) {
  if (style.filled) return '<circle cx="32" cy="32" r="26" fill="' + AVATAR_INK + '"/>';
  return '<circle cx="32" cy="32" r="25.2" fill="none" stroke="' + AVATAR_INK + '" stroke-width="' + stroke + '"/>';
}

/* 圆内右下角的记号：空心点 / 实心点 / 竖记 / 斜划。 */
function stateMark(style) {
  if (style.mark === "hollow") {
    return '<circle cx="43" cy="43" r="3" fill="none" stroke="' + AVATAR_INK + '" stroke-width="2"/>';
  }
  if (style.mark === "solid") {
    return '<circle cx="43" cy="43" r="3.4" fill="' + AVATAR_CANVAS + '"/>';
  }
  if (style.mark === "tick") {
    return '<path d="M43 39.4v7.2" stroke="' + AVATAR_INK + '" stroke-width="2.6" stroke-linecap="round" fill="none"/>';
  }
  return '<path d="M48 16L16 48" stroke="' + AVATAR_CANVAS + '" stroke-width="2.8" stroke-linecap="round" fill="none"/>';
}

/* 小尺寸（<44）的标记：印章的 F 骨架 + 底边撕口 + 记号，缩在圆里。 */
function sealGlyph(ink, cut) {
  return '<path d="M25 41V23h10M25 32h7" stroke="' + ink + '" stroke-width="4.4" stroke-linecap="round" fill="none"/>'
    + '<path d="M36 41h-3M30 41h-4" stroke="' + ink + '" stroke-width="4.4" stroke-linecap="round" fill="none"/>'
    + '<path d="M40 41.5h3.6M43.6 39.9v3.2" stroke="' + cut + '" stroke-width="2.2" stroke-linecap="round" fill="none"/>';
}

/* Q 版印豆的头（64 网格）。头与五官互为反色。 */
function qHead(headInk) {
  return '<path d="M32 12c8.4 0 14.6 6 14.6 13.8 0 4.2-1.3 7.8-3.5 10.4v13.2l-2.5 2-2.5-2-2.5 2-2.5-2-2.6 2-2.5-2-2.5 2-2.5-2V36.2c-2.2-2.6-3.5-6.2-3.5-10.4C17.4 18 23.6 12 32 12z" fill="' + headInk + '"/>';
}

function qFace(state, ink) {
  const eyes = '<ellipse cx="26.6" cy="27" rx="3.2" ry="3.8" fill="' + ink + '"/>'
    + '<ellipse cx="37.4" cy="27" rx="3.2" ry="3.8" fill="' + ink + '"/>';
  const cheer = '<path d="M23.4 27.9c1.6-2.7 4.2-2.7 5.8 0" stroke="' + ink + '" stroke-width="2.2" stroke-linecap="round" fill="none"/>'
    + '<path d="M34.8 27.9c1.6-2.7 4.2-2.7 5.8 0" stroke="' + ink + '" stroke-width="2.2" stroke-linecap="round" fill="none"/>';
  const shut = '<path d="M23.6 27.3h5.8" stroke="' + ink + '" stroke-width="2.2" stroke-linecap="round" fill="none"/>'
    + '<path d="M34.6 27.3h5.8" stroke="' + ink + '" stroke-width="2.2" stroke-linecap="round" fill="none"/>';
  const sad = '<path d="M23.6 25.1l5 4.6M28.6 25.1l-5 4.6" stroke="' + ink + '" stroke-width="2" stroke-linecap="round" fill="none"/>'
    + '<path d="M34.4 25.1l5 4.6M39.4 25.1l-5 4.6" stroke="' + ink + '" stroke-width="2" stroke-linecap="round" fill="none"/>';
  const smile = '<path d="M28.6 33.8c1.4 2.4 5.4 2.4 6.8 0" stroke="' + ink + '" stroke-width="2.2" stroke-linecap="round" fill="none"/>';
  const tiny = '<path d="M30 34h4" stroke="' + ink + '" stroke-width="2.1" stroke-linecap="round" fill="none"/>';
  const oho = '<ellipse cx="32" cy="35" rx="2.6" ry="3.2" fill="' + ink + '"/>';
  const blush = '<circle cx="21.6" cy="31.8" r="1.5" fill="' + ink + '" opacity=".45"/>'
    + '<circle cx="42.4" cy="31.8" r="1.5" fill="' + ink + '" opacity=".45"/>';
  if (state === "thinking") return shut + oho;
  if (state === "ok") return cheer + smile + blush;
  if (state === "bad") return sad + tiny;
  if (state === "warn") return shut + tiny;
  return eyes + smile + blush;
}

export function aiAvatarHtml(state = "idle", size = 28) {
  const key = AVATAR_STYLE[state] ? state : "idle";
  const style = AVATAR_STYLE[key];
  const thinking = key === "thinking";
  const withFace = size >= FACE_MIN_SIZE;
  const stroke = withFace ? 2.2 : 2.8;
  // 头与五官互为反色：线稿圆＝墨头挖空五官，落印章＝挖空头配墨色五官
  const headInk = style.filled ? AVATAR_CANVAS : AVATAR_INK;
  const faceInk = style.filled ? AVATAR_INK : AVATAR_CANVAS;
  return '<span class="ai-avatar ai-avatar--' + key + (thinking ? " is-thinking" : "")
    + (withFace ? "" : " ai-avatar--mark")
    + '" style="--ai-avatar-size:' + size + 'px" role="img" aria-label="' + esc(style.label) + '" data-ai-state="' + key + '">'
    + '<svg viewBox="0 0 64 64" width="' + size + '" height="' + size + '" aria-hidden="true">'
    + disc(style, stroke)
    + (withFace ? qHead(headInk) + qFace(key, faceInk) : sealGlyph(AVATAR_INK, AVATAR_CANVAS))
    + stateMark(style)
    + "</svg>"
    + (thinking ? '<span class="ai-avatar-ring" aria-hidden="true"></span>' : "")
    + "</span>";
}