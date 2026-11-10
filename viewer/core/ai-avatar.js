/* 对话 AI 的实时形象。

   形象参考 Kimi 的蓝色圆脸：蓝色渐变圆 + 两只白色竖椭圆眼。差别在
   “实时”——它不是一张静态图：

   - 眼睛跟随指针轻微偏移（app.js 的 pointermove 写入 --look-x/y）；
   - 5.6s 一次的眨眼，4.8s 一次的呼吸；
   - 生成中眼睛左右扫视，并多一圈呼吸环；
   - 系统开启 reduced-motion 时全部静止。

   状态仍来自真实事实（avatarStateFor），颜色不背状态语义：脸始终是
   同一颗蓝脸，状态只改变扫视/圆环等动效。 */

import { esc } from "./format.js";

export const AVATAR_INK = "#1B1B1F";
export const AVATAR_CANVAS = "#FBFAF9";

/* 状态 → 标签与动效类。filled/mark 是旧投影字段，保留给调用方与测试的
   稳定接口；蓝色脸不再用它们决定画法。 */
export const AVATAR_STYLE = {
  idle: { mark: "hollow", label: "FCode" },
  thinking: { mark: "solid", label: "FCode · 正在生成" },
  ok: { mark: "solid", label: "FCode · 已完成" },
  warn: { mark: "tick", label: "FCode · 已停止" },
  bad: { mark: "slash", label: "FCode · 调用失败" },
};

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

/* 操作员头像：墨盘 + 画布人形（头 + 肩），与蓝色 AI 脸区分开。
   无状态——操作员的脸不随消息状态变化；形状全在 viewBox 内。 */
export function userAvatarHtml(size = 28) {
  return '<span class="chat-user-avatar" style="--ai-avatar-size:' + size + 'px" role="img" aria-label="操作员">'
    + '<svg viewBox="0 0 64 64" width="' + size + '" height="' + size + '" aria-hidden="true">'
    + '<circle cx="32" cy="32" r="32" fill="' + AVATAR_INK + '"/>'
    + '<circle cx="32" cy="25" r="9.5" fill="' + AVATAR_CANVAS + '"/>'
    + '<ellipse cx="32" cy="54" rx="13.5" ry="11" fill="' + AVATAR_CANVAS + '"/>'
    + "</svg></span>";
}

/* 两只白色竖椭圆眼。包三层：
   look（指针跟随）→ scan（生成中左右扫视）→ eyes（眨眼）。
   三层分开，CSS 动画不会互相覆盖 transform。 */
function eyes() {
  return '<g class="ai-avatar-look">'
    + '<g class="ai-avatar-scan">'
    + '<g class="ai-avatar-eyes">'
    + '<rect class="ai-eye" x="21.2" y="23.5" width="8" height="17" rx="4" fill="#FFFFFF"/>'
    + '<rect class="ai-eye" x="34.8" y="23.5" width="8" height="17" rx="4" fill="#FFFFFF"/>'
    + "</g></g></g>";
}

export function aiAvatarHtml(state = "idle", size = 28) {
  const key = AVATAR_STYLE[state] ? state : "idle";
  const style = AVATAR_STYLE[key];
  return '<span class="ai-avatar ai-avatar--' + key + (key === "thinking" ? " is-thinking" : "")
    + '" style="--ai-avatar-size:' + size + 'px" role="img" aria-label="' + esc(style.label) + '"'
    + ' data-ai-state="' + key + '" data-live="1">'
    + '<svg viewBox="0 0 64 64" width="' + size + '" height="' + size + '" aria-hidden="true">'
    + eyes()
    + "</svg>"
    + (key === "thinking" ? '<span class="ai-avatar-ring" aria-hidden="true"></span>' : "")
    + "</span>";
}