/* Keyboard guards shared by every composer-facing keydown handler.
 *
 * 中文输入法（composition）组字期间，Enter 是「确认候选」，不是「发送」；
 * 不设防的 keydown 会在拼音确认的一刻把半截草稿直接提交出去。判定覆盖两条
 * 事实来源：
 *   - event.isComposing：compositionstart 与 compositionend 之间为 true；
 *   - event.keyCode === 229：部分引擎对组字期间的 keydown 统一编码（Safari
 *     在 compositionend 后的一拍仍可能给出 229）。
 *
 * 调用方约定：守卫必须放在各自 keydown 的最顶部、任何 preventDefault 与
 * 斜杠菜单分支之前——组字中的 Enter/↑↓/Tab 交给输入法，不进入应用语义。
 *
 * Pure: (event) -> boolean。无 DOM、无状态，app.js 的 composer / 排队编辑
 * 等 keydown 处理器复用同一判定。
 */

/** 该 keydown 是否处于输入法组字中（此时 Enter/方向键不触发提交语义）。 */
export function isImeComposing(event) {
  if (!event) return false;
  return event.isComposing === true || event.keyCode === 229;
}

/**
 * composer keydown 判定：把「这个按键想做什么」与「怎么做」分离。
 * 判定优先级与 app.js 的历史实现逐条对齐（不改变行为）：
 *   1. 输入法组字（isImeComposing）——恒为 "ime"，先于斜杠菜单分支；
 *   2. 斜杠菜单开启——↑↓/Tab/Enter(无 Shift)/Escape 归入 slash-* 动作，
 *      其余键（含 Shift+Enter）落出菜单分支；
 *   3. Enter（无 Shift）——提交；
 *   4. ↑ 在光标处于起点且有历史——回溯输入历史；↓ 在历史回溯中——前进；
 *   5. 其余（含 Shift+Enter 的默认换行）——"none"，不 preventDefault。
 * Pure: (event, ctx) -> action 字符串。无 DOM、无状态；app.js 的 composer
 * keydown 复用本判定并只负责执行效果（Node 可测）。
 *
 * @param {{
 *   key: string, shiftKey?: boolean, isComposing?: boolean, keyCode?: number,
 * }} event keydown 的最小形状（真实 KeyboardEvent 或测试对象均可）
 * @param {{
 *   slashMenuOpen?: boolean,  // #slash-menu 存在且未隐藏
 *   caretAtStart?: boolean,   // textarea.selectionStart === 0
 *   hasHistory?: boolean,     // promptHistory.length > 0
 *   historyActive?: boolean,  // historyIndex !== -1（正在回溯中）
 * }} ctx
 * @returns {"ime"|"slash-down"|"slash-up"|"slash-tab"|"slash-run"|"slash-close"|
 *           "submit"|"history-prev"|"history-next"|"none"}
 */
export function composerKeyAction(event, ctx = {}) {
  if (isImeComposing(event)) return "ime";
  const {
    slashMenuOpen = false,
    caretAtStart = false,
    hasHistory = false,
    historyActive = false,
  } = ctx || {};
  if (slashMenuOpen) {
    if (event.key === "ArrowDown") return "slash-down";
    if (event.key === "ArrowUp") return "slash-up";
    if (event.key === "Tab") return "slash-tab";
    if (event.key === "Enter" && !event.shiftKey) return "slash-run";
    if (event.key === "Escape") return "slash-close";
  }
  if (event.key === "Enter" && !event.shiftKey) return "submit";
  if (event.key === "ArrowUp" && caretAtStart && hasHistory) return "history-prev";
  if (event.key === "ArrowDown" && historyActive) return "history-next";
  return "none";
}

/**
 * 行内编辑（项目快编 / 排队消息编辑）的 keydown 判定：Enter 提交、Escape 取消，
 * 组字中的按键一律交还输入法。与历史实现一致：Enter 不区分 Shift（编辑框内
 * Shift+Enter 同样提交，属现状行为，此处不悄悄改语义）。
 * Pure: (event) -> "ime"|"commit"|"cancel"|"none"。
 */
export function commitCancelAction(event) {
  if (isImeComposing(event)) return "ime";
  if (event.key === "Enter") return "commit";
  if (event.key === "Escape") return "cancel";
  return "none";
}
