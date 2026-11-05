import { test, expect } from "@playwright/test";

/* 功能面页面：判断记录、定时任务、设置页开合。 */

test("判断记录页渲染(空态可读)", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: "判断记录" }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "reviews");
  await expect(page.locator("#main")).toContainText("判断记录");
});

test("定时任务页:空态诚实标注调度器未接入", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: "定时任务" }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "scheduled");
  await expect(page.locator("#main")).toContainText("还没有定时任务草稿");
  await expect(page.locator("#main")).toContainText("不会自动触发");
});

test("设置页:开合与审批档位渲染", async ({ page }) => {
  await page.goto("/");
  await page.locator("#settings-btn, #settings-mobile").first().click();
  await expect(page.locator("#settings-page")).toBeVisible();
  await expect(page.locator("#settings-modes .set-mode")).toHaveCount(3);
  await page.locator("#settings-back").click();
  await expect(page.locator("#settings-page")).toBeHidden();
});
