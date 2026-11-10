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

test("Chat 输入 @ 不打开 Work 引用器", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.getByRole("button", { name: /新对话/ }).first().click();
  const ta = page.locator("#composer-ta");
  await ta.fill("@README");
  await expect(page.locator("#workspace-mention-menu")).toBeHidden();
});