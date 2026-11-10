import { test, expect } from "@playwright/test";

/* Composer gestures: Work's `@` references a workspace path; Chat keeps its
   explicit attachment model and must not open the Work picker. */

test("Work 的 @ 插入工作区相对路径", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.keyboard.press("ControlOrMeta+k");
  await page.locator("#palette-input").fill("新委托");
  await page.locator("#palette-input").press("Enter");
  await expect(page.locator("#app")).toHaveClass(/intake-mode/);

  const ta = page.locator("#composer-ta");
  await ta.fill("@README");
  const menu = page.locator("#workspace-mention-menu");
  await expect(menu).toBeVisible();
  await expect(menu.locator(".mention-item").first()).toContainText("README.md");
  await menu.locator(".mention-item").first().click();
  await expect(ta).toHaveValue(/@\S*README\.md/);
  await expect(menu).toBeHidden();
});

test("新对话的模型菜单不越出视口", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.getByRole("button", { name: /新对话/ }).first().click();
  await page.locator("#model-btn").click();
  const menu = page.locator("#chat-model-menu");
  await expect(menu).toBeVisible();
  await expect(menu.locator(".tpl-item").first()).toBeVisible();
  await expect(menu.locator(".model-current-dot")).toBeVisible();
  const box = await menu.boundingBox();
  const chip = await page.locator("#model-btn").boundingBox();
  const viewport = page.viewportSize();
  expect(box).toBeTruthy();
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
  expect(box.y + box.height).toBeLessThanOrEqual(chip.y, "菜单应向上展开，卡片底边停在模型芯片上方");
});

test("Chat 输入 @ 不打开 Work 引用器", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.getByRole("button", { name: /新对话/ }).first().click();
  const ta = page.locator("#composer-ta");
  await ta.fill("@README");
  await expect(page.locator("#workspace-mention-menu")).toBeHidden();
});