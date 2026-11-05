import test from "node:test";
import assert from "node:assert/strict";
import { isImeComposing, composerKeyAction, commitCancelAction } from "../core/keyboard.js";

test("composition 中的 keydown 不触发提交语义", () => {
  assert.equal(isImeComposing({ isComposing: true, keyCode: 13 }), true);
  assert.equal(isImeComposing({ isComposing: true, keyCode: 0 }), true);
});

test("keyCode 229（组字统一编码）即使 isComposing 已翻转也拦截", () => {
  assert.equal(isImeComposing({ isComposing: false, keyCode: 229 }), true);
  assert.equal(isImeComposing({ keyCode: 229 }), true);
});

test("普通按键与普通 Enter 放行", () => {
  assert.equal(isImeComposing({ isComposing: false, keyCode: 13 }), false);
  assert.equal(isImeComposing({ keyCode: 13 }), false);
  assert.equal(isImeComposing({ isComposing: false, key: "ArrowDown" }), false);
});

test("空事件与缺失字段安全放行（不抛错）", () => {
  assert.equal(isImeComposing(null), false);
  assert.equal(isImeComposing(undefined), false);
  assert.equal(isImeComposing({}), false);
});

/* ---------- composerKeyAction：composer 的 Enter / Shift+Enter / IME 判定 ---------- */

test("普通 Enter 提交，Shift+Enter 落回默认换行（none，不 preventDefault）", () => {
  assert.equal(composerKeyAction({ key: "Enter" }), "submit");
  assert.equal(composerKeyAction({ key: "Enter", shiftKey: false }), "submit");
  assert.equal(composerKeyAction({ key: "Enter", shiftKey: true }), "none");
});

test("组字中的按键恒为 ime，且先于斜杠菜单分支", () => {
  assert.equal(composerKeyAction({ key: "Enter", isComposing: true }), "ime");
  assert.equal(composerKeyAction({ key: "Enter", keyCode: 229 }), "ime");
  // 守卫先于菜单：组字中的 ↑ 在菜单开启时也交还输入法，不移动候选项
  assert.equal(
    composerKeyAction({ key: "ArrowUp", isComposing: true }, { slashMenuOpen: true }),
    "ime",
  );
});

test("斜杠菜单开启时 ↑↓/Tab/Enter/Escape 归入 slash-* 动作", () => {
  const ctx = { slashMenuOpen: true };
  assert.equal(composerKeyAction({ key: "ArrowDown" }, ctx), "slash-down");
  assert.equal(composerKeyAction({ key: "ArrowUp" }, ctx), "slash-up");
  assert.equal(composerKeyAction({ key: "Tab" }, ctx), "slash-tab");
  assert.equal(composerKeyAction({ key: "Enter" }, ctx), "slash-run");
  assert.equal(composerKeyAction({ key: "Escape" }, ctx), "slash-close");
});

test("菜单开启时 Shift+Enter 与普通字符仍落出菜单分支", () => {
  const ctx = { slashMenuOpen: true };
  assert.equal(composerKeyAction({ key: "Enter", shiftKey: true }, ctx), "none");
  assert.equal(composerKeyAction({ key: "a" }, ctx), "none");
});

test("菜单关闭时 ↑↓ 走输入历史判定：仅光标在起点且有历史才回溯", () => {
  assert.equal(
    composerKeyAction({ key: "ArrowUp" }, { caretAtStart: true, hasHistory: true }),
    "history-prev",
  );
  // 光标不在起点：不劫持（让光标在多行草稿内自由移动）
  assert.equal(
    composerKeyAction({ key: "ArrowUp" }, { caretAtStart: false, hasHistory: true }),
    "none",
  );
  // 没有历史可回溯
  assert.equal(composerKeyAction({ key: "ArrowUp" }, { caretAtStart: true, hasHistory: false }), "none");
});

test("↓ 仅在历史回溯进行中前进，否则交还光标移动", () => {
  assert.equal(composerKeyAction({ key: "ArrowDown" }, { historyActive: true }), "history-next");
  assert.equal(composerKeyAction({ key: "ArrowDown" }, { historyActive: false }), "none");
  assert.equal(composerKeyAction({ key: "ArrowDown" }), "none");
});

test("菜单开启时 ↑ 优先于历史回溯（slash-up 胜出）", () => {
  assert.equal(
    composerKeyAction(
      { key: "ArrowUp" },
      { slashMenuOpen: true, caretAtStart: true, hasHistory: true },
    ),
    "slash-up",
  );
});

test("无修饰的普通字符与修饰组合键均为 none（不误触）", () => {
  assert.equal(composerKeyAction({ key: "a" }), "none");
  assert.equal(composerKeyAction({ key: "Backspace" }), "none");
});

/* ---------- commitCancelAction：行内编辑的提交/取消判定 ---------- */

test("行内编辑：Enter 提交、Escape 取消", () => {
  assert.equal(commitCancelAction({ key: "Enter" }), "commit");
  assert.equal(commitCancelAction({ key: "Escape" }), "cancel");
});

test("行内编辑：组字中的 Enter 是确认候选，不是提交", () => {
  assert.equal(commitCancelAction({ key: "Enter", isComposing: true }), "ime");
  assert.equal(commitCancelAction({ key: "Enter", keyCode: 229 }), "ime");
});

test("行内编辑：其余键为 none；Shift+Enter 沿用现状同样提交（不悄悄改语义）", () => {
  assert.equal(commitCancelAction({ key: "a" }), "none");
  assert.equal(commitCancelAction({ key: "Enter", shiftKey: true }), "commit");
});
