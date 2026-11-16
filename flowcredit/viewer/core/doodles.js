"use strict";

/* 手绘图标取用（自 app.js 拆分切片 4d 迁入，纯函数无状态）。
   图标数据由 icons-doodle.js / brand/seal-icons.js 注入 window。 */

/* 取手绘图标：mode=false 静止 / true 动画 / "draw" 只描边一次（不沸腾） */
export function doodleSvg(name, mode = false) {
  if (!name) return "";
  /* 印章线稿优先：家族语言统一（见 brand/seal-icons.js） */
  const seal = window.SEAL_ICONS && window.SEAL_ICONS.still && window.SEAL_ICONS.still[name];
  if (seal) return seal;
  const set = window.DOODLE_ICONS || {};
  const animated = set.animated && set.animated[name];
  if (mode === "draw" && animated) {
    return animated.replace('class="d-icon d-icon-anim ', 'class="d-icon d-icon-anim draw-only ');
  }
  if (mode && animated) return animated;
  return (set.still && set.still[name]) || "";
}

/* 内联手绘图标：data-doodle 槽位在启动/重绘后填充 */
export function hydrateDoodles(root = document) {
  const set = window.DOODLE_ICONS || {};
  const seal = window.SEAL_ICONS && window.SEAL_ICONS.still || {};
  for (const slot of root.querySelectorAll("[data-doodle]")) {
    if (slot.dataset.hydrated === "1") continue;
    const name = slot.dataset.doodle;
    /* 印章线稿优先：同名接管，家族语言统一 */
    if (seal[name]) { slot.innerHTML = seal[name]; slot.dataset.hydrated = "1"; continue; }
    const useAnimated = slot.dataset.doodleAnim === "1";
    const svg = (useAnimated && set.animated && set.animated[name])
      || (set.still && set.still[name]) || "";
    if (!svg) continue;
    slot.innerHTML = svg;
    slot.dataset.hydrated = "1";
  }
}
