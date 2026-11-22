/* FCode 的实时像素伙伴。

   造型借鉴 Claude Code 官方终端像素机器人的方块比例、深色竖眼与短四肢，
   但不复制官方素材：暖橙机体、信号天线与右侧光标尾都是内联 SVG 原语。

   - 眼睛跟随指针轻微偏移（app.js 的 pointermove 写入 --look-x/y）；
   - 待机眨眼与浮动，思考时敲击，等待时举手，成功时跳起庆祝；
   - 停止与失败各有独立表情，不只靠颜色区分；
   - 系统或应用开启 reduced-motion 时全部静止。

   状态仍来自真实事实（avatarStateFor），开屏角色始终保持 idle。 */

import { esc } from "./format.js";

export const AVATAR_INK = "#1B1B1F";
export const AVATAR_CANVAS = "#FBFAF9";

/* filled/mark 是旧投影字段，保留给调用方与测试的稳定接口。 */
export const AVATAR_STYLE = {
  idle: { mark: "hollow", label: "FCode" },
  thinking: { mark: "solid", label: "FCode · 正在生成" },
  pending: { mark: "hollow", label: "FCode · 等待批准" },
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

/* 操作员头像保持安静的黑白墨盘，不与像素伙伴争抢身份。 */
export function userAvatarHtml(size = 28) {
  return '<span class="chat-user-avatar" style="--ai-avatar-size:' + size + 'px" role="img" aria-label="操作员">'
    + '<svg viewBox="0 0 64 64" width="' + size + '" height="' + size + '" aria-hidden="true">'
    + '<circle cx="32" cy="32" r="32" fill="' + AVATAR_INK + '"/>'
    + '<circle cx="32" cy="25" r="9.5" fill="' + AVATAR_CANVAS + '"/>'
    + '<ellipse cx="32" cy="54" rx="13.5" ry="11" fill="' + AVATAR_CANVAS + '"/>'
    + "</svg></span>";
}

/* 三层容器分离指针跟随、思考扫视与眨眼，CSS transform 不相互覆盖。 */
function face(state) {
  const ink = "#282321";
  const faces = {
    idle: '<rect class="ai-eye" x="23" y="27" width="4" height="9" fill="' + ink + '"/>'
      + '<rect class="ai-eye" x="38" y="27" width="4" height="9" fill="' + ink + '"/>'
      + '<rect class="ai-avatar-mouth" x="29" y="40" width="7" height="2" fill="' + ink + '"/>',
    thinking: '<path class="ai-avatar-brows" d="M21 23h8v2h-8zM36 23h8v2h-8z" fill="' + ink + '"/>'
      + '<rect class="ai-eye" x="23" y="28" width="4" height="8" fill="' + ink + '"/>'
      + '<rect class="ai-eye" x="38" y="28" width="4" height="8" fill="' + ink + '"/>'
      + '<rect class="ai-avatar-mouth" x="30" y="40" width="5" height="4" fill="' + ink + '"/>',
    pending: '<path class="ai-avatar-brows" d="M21 25h8v2h-8zM36 23h8v2h-8z" fill="' + ink + '"/>'
      + '<rect class="ai-eye" x="23" y="29" width="4" height="6" fill="' + ink + '"/>'
      + '<rect class="ai-eye" x="38" y="28" width="4" height="7" fill="' + ink + '"/>'
      + '<path class="ai-avatar-mouth" d="M29 40h9v2h-9z" fill="' + ink + '"/>',
    ok: '<path class="ai-eye" d="M21 31v-3h3v-2h4v2h3v3h-3v-2h-4v2z" fill="' + ink + '"/>'
      + '<path class="ai-eye" d="M35 31v-3h3v-2h4v2h3v3h-3v-2h-4v2z" fill="' + ink + '"/>'
      + '<path class="ai-avatar-mouth" d="M27 39h3v3h6v-3h3v5H27z" fill="' + ink + '"/>',
    warn: '<path class="ai-avatar-brows" d="M21 24h8v2h-8zM36 24h8v2h-8z" fill="' + ink + '"/>'
      + '<rect class="ai-eye" x="23" y="30" width="4" height="5" fill="' + ink + '"/>'
      + '<rect class="ai-eye" x="38" y="30" width="4" height="5" fill="' + ink + '"/>'
      + '<path class="ai-avatar-mouth" d="M28 43v-3h3v2h5v-2h3v3h-3v-1h-5v1z" fill="' + ink + '"/>',
    bad: '<path class="ai-eye" d="M21 27h4l2 3 2-3h4l-4 5 4 5h-4l-2-3-2 3h-4l4-5z" fill="' + ink + '"/>'
      + '<path class="ai-eye" d="M34 27h4l2 3 2-3h4l-4 5 4 5h-4l-2-3-2 3h-4l4-5z" fill="' + ink + '"/>'
      + '<path class="ai-avatar-mouth" d="M27 41h4v-2h4v2h4v3h-4v-1h-4v1h-4z" fill="' + ink + '"/>',
  };
  return '<g class="ai-avatar-look"><g class="ai-avatar-scan"><g class="ai-avatar-eyes">'
    + (faces[state] || faces.idle) + "</g></g></g>";
}

function statePixels(state) {
  if (state === "thinking") {
    return '<g class="ai-avatar-thoughts" fill="#D97757" aria-hidden="true">'
      + '<rect x="53" y="18" width="4" height="4"/><rect x="57" y="12" width="3" height="3"/>'
      + '<rect x="52" y="7" width="2" height="2"/></g>';
  }
  if (state === "ok") {
    return '<g class="ai-avatar-confetti" fill="#D97757" aria-hidden="true">'
      + '<rect x="8" y="13" width="4" height="4"/><rect x="53" y="18" width="4" height="4"/>'
      + '<rect x="4" y="27" width="3" height="3"/><rect x="58" y="29" width="3" height="3"/>'
      + '<rect x="15" y="6" width="3" height="3"/><rect x="46" y="5" width="3" height="3"/></g>';
  }
  return "";
}

export function aiAvatarHtml(state = "idle", size = 28, labelOverride = "") {
  const key = AVATAR_STYLE[state] ? state : "idle";
  const style = AVATAR_STYLE[key];
  // 治理区等调用方带身份标签（bot 名）；对话侧沿用默认标签。
  const label = String(labelOverride || "").trim() || style.label;
  const ring = key === "thinking" || key === "pending";
  return '<span class="ai-avatar ai-avatar--' + key + ' ai-avatar-mascot' + (key === "thinking" ? " is-thinking" : "")
    + '" style="--ai-avatar-size:' + size + 'px" role="img" aria-label="' + esc(label) + '"'
    + ' data-ai-state="' + key + '" data-live="1">'
    + '<svg class="ai-avatar-sprite" viewBox="0 0 64 64" width="' + size + '" height="' + size + '"'
    + ' shape-rendering="crispEdges" aria-hidden="true">'
    + '<g class="ai-avatar-ground"><rect x="18" y="58" width="29" height="3" fill="#282321" opacity=".12"/></g>'
    + '<g class="ai-avatar-bounce">'
    + statePixels(key)
    + '<g class="ai-avatar-antenna" fill="#D97757"><rect x="30" y="10" width="4" height="9"/>'
    + '<rect x="27" y="6" width="10" height="5"/><rect x="30" y="4" width="4" height="2"/></g>'
    + '<g class="ai-avatar-tail" fill="#B85F45"><path d="M51 38h4v3h3v3h3v4h-3v3h-3v3h-4z"/></g>'
    + '<g class="ai-avatar-arm ai-avatar-arm--left" fill="#D97757"><path d="M9 28h6v14h-3v4H8V31h1z"/></g>'
    + '<g class="ai-avatar-arm ai-avatar-arm--right" fill="#D97757"><path d="M49 28h6v3h1v15h-4v-4h-3z"/></g>'
    + '<g class="ai-avatar-leg ai-avatar-leg--left" fill="#B85F45"><path d="M19 45h8v10h-2v4h-8v-4h2z"/></g>'
    + '<g class="ai-avatar-leg ai-avatar-leg--right" fill="#B85F45"><path d="M37 45h8v10h2v4h-8v-4h-2z"/></g>'
    + '<path class="ai-avatar-body" d="M18 17h28v2h4v27h-4v3H18v-3h-4V20h4z" fill="#D97757"/>'
    + '<path class="ai-avatar-body-shade" d="M14 40h36v6h-4v3H18v-3h-4z" fill="#C96B4F"/>'
    + '<rect class="ai-avatar-body-light" x="18" y="20" width="28" height="3" fill="#E99A80"/>'
    + face(key)
    + "</g></svg>"
    + (ring ? '<span class="ai-avatar-ring' + (key === "pending" ? " is-pending" : "") + '" aria-hidden="true"></span>' : "")
    + "</span>";
}
