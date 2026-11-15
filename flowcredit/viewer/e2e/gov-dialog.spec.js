import { test, expect } from "@playwright/test";

/* 治理对话面：默认入口是对话，不是工作台。命令 /挖洞 → 作业卡 →
   轮询完成 → 「查看结果」切到工作台。 */

test("治理对话:/挖洞 产生作业卡,完成后可切工作台复核", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await expect(page.locator("#gov-dialog-stream")).toBeVisible();
  await expect(page.locator(".composer-wrap")).toBeVisible();

  await page.locator("#composer-ta").fill("/挖洞 fixtures/demo-target");
  await page.keyboard.press("Enter");
  const job = page.locator("#gov-dialog-stream .gov-job", { hasText: "fixtures/demo-target" }).first();
  await expect(job).toBeVisible({ timeout: 15_000 });
  await expect(job.locator(".gov-job-state")).toContainText("完成", { timeout: 90_000 });

  await job.locator("[data-gov-hist]").click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await expect(page.locator("#gov-summary .gov-target-head")).toBeVisible({ timeout: 10_000 });
  await expect(page.locator(".composer-wrap")).toBeHidden();
  await expect(page.locator("#gov-findings-list [data-gov-finding]").first())
    .toBeVisible({ timeout: 10_000 });
});