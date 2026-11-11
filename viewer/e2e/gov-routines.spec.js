import { test, expect } from "@playwright/test";

/* Phase B 定时排程：创建 → 暂停 → 删除。真正的到点触发由后端单测覆盖；
   这里验证团队页把排程作为可操作事实呈现，而不是装饰。 */

test("治理团队:创建排程、暂停、删除", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await page.locator('[data-gov-mode="team"]').click();
  await expect(page.locator("#gov-team-roster .gov-bot-card")).toHaveCount(2);
  await expect(page.locator("#gov-routine-form")).toBeVisible();

  await page.locator("#gov-routine-bot").selectOption("auditor");
  await page.locator("#gov-routine-target").fill("fixtures/demo-target");
  await page.locator("#gov-routine-frequency").selectOption("每天");
  await page.locator("#gov-routine-time").fill("23:59");
  await page.locator("#gov-routine-form button[type=submit]").click();

  const row = page.locator("#gov-routine-list-host .gov-routine-row").first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(row).toContainText("审计员");
  await expect(row).toContainText("fixtures/demo-target");
  await expect(row).toContainText("每天 23:59");
  await expect(row.locator(".ai-avatar[data-ai-state]").first()).toBeVisible();
  const routineId = await row.getAttribute("data-routine-id");
  const routineRow = page.locator(`#gov-routine-list-host [data-routine-id="${routineId}"]`);

  const toggle = routineRow.locator("[data-gov-routine-toggle]");
  await expect(toggle).toBeChecked();
  await toggle.uncheck();
  await expect(routineRow.locator("[data-gov-routine-toggle]")).not.toBeChecked();

  await routineRow.locator("[data-gov-routine-delete]").click();
  await expect(routineRow).toHaveCount(0, { timeout: 10_000 });
});