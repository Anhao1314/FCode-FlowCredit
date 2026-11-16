import { test, expect } from "@playwright/test";

/* 目标级 fan-out：一个蜂群把两个目标排入队列，宿主按并发启动独立 run；
   蜂群卡显示进度与目标运行图，频道里的子运行来源为「蜂群」。 */

test("治理蜂群:多目标 fan-out、进度完成与频道来源", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await page.locator('[data-gov-mode="team"]').click();

  await expect(page.locator("#gov-swarm-form")).toBeVisible();
  await page.locator("#gov-swarm-targets").fill(
    "fixtures/demo-target\nfixtures/demo-target/mini_agent");
  await page.locator("#gov-swarm-concurrency").selectOption("2");
  await page.locator("#gov-swarm-form button[type=submit]").click();

  const card = page.locator("#gov-swarm-list-host .gov-swarm-card").first();
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card).toContainText("2 / 2 完成", { timeout: 90_000 });
  await expect(card.locator(".gov-swarm-run")).toHaveCount(2);
  await expect(card.locator('[data-gov-hist]').first()).toBeVisible();
  await expect(card.locator('[data-gov-swarm-cancel]')).toHaveCount(0);

  // 子运行进入统一频道，来源徽章是可区分的「蜂群」
  const swarmSource = page.locator("#gov-team-channel .gov-run-source", { hasText: "蜂群" }).first();
  await expect(swarmSource).toBeVisible({ timeout: 15_000 });
});
