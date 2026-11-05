import { test, expect } from "@playwright/test";

/* 操作员长期记忆（M1）：/remember 沉淀 → API 可见 → 设置页管理 →
   删除即消失。记忆只在操作员明示时产生——E2E 同时守住这个边界。 */

test("/remember 写入 → 设置页可见 → 删除消失", async ({ page }) => {
  await page.goto("/");
  // chat 语境（新对话）里输入带参斜杠命令
  await page.getByRole("button", { name: "新对话" }).first().click();
  const ta = page.locator("#composer-ta");
  await ta.fill("/remember E2E 记忆：口径基准是 CNY");
  // 斜杠菜单出现且命中 /remember
  const menu = page.locator("#slash-menu");
  await expect(menu).toBeVisible();
  await expect(menu.locator(".slash-item").first()).toContainText("/remember");
  await ta.press("Enter");
  await expect(ta).toHaveValue("", "命令执行后清空输入框");

  // API 已落库
  const res = await page.request.get("/api/chat/memory");
  expect(res.ok()).toBeTruthy();
  const { memories } = await res.json();
  const entry = memories.find(m => m.text.includes("E2E 记忆：口径基准是 CNY"));
  expect(entry, "记忆条目已写入").toBeTruthy();

  // 设置页「记忆与产物」区可见该条目
  await page.locator("#settings-btn, #settings-mobile").first().click();
  await expect(page.locator("#settings-page")).toBeVisible();
  const row = page.locator(`[data-mem-row="${entry.id}"]`);
  await expect(row).toBeVisible();
  await expect(row).toContainText("E2E 记忆：口径基准是 CNY");

  // 删除 → 行消失 → API 里也消失
  await row.locator("[data-mem-del]").click();
  await expect(row).toHaveCount(0, { timeout: 10_000 });
  const after = await (await page.request.get("/api/chat/memory")).json();
  expect(after.memories.find(m => m.id === entry.id)).toBeUndefined();
});

test("/remember 空参数给出可见提示,不写库", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新对话" }).first().click();
  const ta = page.locator("#composer-ta");
  await ta.fill("/remember");
  await ta.press("Enter");
  await expect(ta).toHaveValue("");
  const { memories } = await (await page.request.get("/api/chat/memory")).json();
  expect(memories).toHaveLength(0);
});
