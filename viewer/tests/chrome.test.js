import test from "node:test";
import assert from "node:assert/strict";
import { applyChromeState } from "../core/chrome.js";

class Classes {
  constructor() { this.values = new Set(); }
  toggle(name, force) { if (force) this.values.add(name); else this.values.delete(name); }
  contains(name) { return this.values.has(name); }
}

function element() {
  return {
    classList: new Classes(),
    attributes: {},
    inert: false,
    setAttribute(name, value) { this.attributes[name] = String(value); },
  };
}

function harness() {
  const app = element();
  const sideToggle = element();
  const tip = { textContent: "" };
  sideToggle.querySelector = selector => selector === ".side-tip-label" ? tip : null;
  const railPanel = element();
  const railToggle = element();
  return { app, sideToggle, railPanel, railToggle, tip };
}

test("侧栏折叠同步 class、提示与 aria-expanded", () => {
  const h = harness();
  applyChromeState(h, { sidebar: true });
  assert.equal(h.app.classList.contains("side-collapsed"), true);
  assert.equal(h.sideToggle.attributes["aria-expanded"], "false");
  assert.equal(h.sideToggle.attributes["aria-label"], "展开侧栏（⌘B）");
  assert.equal(h.tip.textContent, "展开侧栏");

  applyChromeState(h, { sidebar: false });
  assert.equal(h.app.classList.contains("side-collapsed"), false);
  assert.equal(h.sideToggle.attributes["aria-expanded"], "true");
  assert.equal(h.tip.textContent, "折叠侧栏");
});

test("详情区打开时进入可访问树并同步触发按钮", () => {
  const h = harness();
  applyChromeState(h, { rail: true });
  assert.equal(h.app.classList.contains("rail-open"), true);
  assert.equal(h.app.classList.contains("rail-hidden"), false);
  assert.equal(h.railPanel.attributes["aria-hidden"], "false");
  assert.equal(h.railPanel.inert, false);
  assert.equal(h.railToggle.attributes["aria-expanded"], "true");
});

test("详情区关闭时移出可访问树并禁止隐藏控件获得焦点", () => {
  const h = harness();
  applyChromeState(h, { rail: true });
  applyChromeState(h, { rail: false });
  assert.equal(h.app.classList.contains("rail-open"), false);
  assert.equal(h.app.classList.contains("rail-hidden"), true);
  assert.equal(h.railPanel.attributes["aria-hidden"], "true");
  assert.equal(h.railPanel.inert, true);
  assert.equal(h.railToggle.attributes["aria-expanded"], "false");
});

test("局部更新不改变另一区域的既有状态", () => {
  const h = harness();
  applyChromeState(h, { sidebar: true, rail: true });
  applyChromeState(h, { rail: false });
  assert.equal(h.app.classList.contains("side-collapsed"), true);
  assert.equal(h.sideToggle.attributes["aria-expanded"], "false");
  assert.equal(h.railPanel.inert, true);
});
