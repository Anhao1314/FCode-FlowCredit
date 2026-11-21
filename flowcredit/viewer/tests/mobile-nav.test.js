import test from "node:test";
import assert from "node:assert/strict";

import { MOBILE_NAV_CLOSE_SELECTOR, mobileNavShouldClose } from "../core/side-nav.js";

/* A2 移动抽屉：侧栏内点击是否关闭抽屉的纯谓词。
   真实 DOM 行为（scrim 点击、焦点归还）由 Playwright 探针验收。 */

test("关闭选择器覆盖全部离开列表的动作", () => {
  for (const part of ["[data-run]", "[data-chat-id]", "[data-open]", "#new-run", ".side-market"]) {
    assert.ok(MOBILE_NAV_CLOSE_SELECTOR.includes(part), part);
  }
});

test("closest 命中 → 关；未命中/无 closest/null → 不关不炸", () => {
  const hit = { closest: () => ({ role: "row" }) };
  assert.equal(mobileNavShouldClose(hit), true);
  const miss = { closest: () => null };
  assert.equal(mobileNavShouldClose(miss), false);
  assert.equal(mobileNavShouldClose(null), false);
  assert.equal(mobileNavShouldClose({}), false);
});
