import { test, expect } from "@playwright/test";

/* Phase A 治理团队：花名册 → 派活给审计员 → 团队频道完成 →
   复核员验链 → 从消息回工作台。 */

test("治理团队:审计员扫描、复核员验链、消息回工作台", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await page.locator('[data-gov-mode="team"]').click();

  await expect(page.locator("#gov-team-roster .gov-bot-card")).toHaveCount(2);
  await expect(page.locator("#gov-team-summary")).toContainText("常驻角色");
  await expect(page.locator("#gov-team-summary")).toContainText("Phase B");
  await expect(page.locator("#gov-team-roster .bot-avatar-auditor")).toBeVisible();
  await expect(page.locator("#gov-team-roster .bot-avatar-verifier")).toBeVisible();
  await expect(page.locator(".composer-wrap")).toBeHidden();
  // 身份头像不是静态图案：审计员扫掠、复核员轨道都有 CSS 动画
  await expect.poll(async () => page.locator(".bot-avatar-auditor .ba-sweep")
    .evaluate(el => getComputedStyle(el).animationName)).toBe("botSweep");
  await expect.poll(async () => page.locator(".bot-avatar-verifier .ba-orbit")
    .evaluate(el => getComputedStyle(el).animationName)).toBe("botOrbit");

  // 审计员：目标路径 → 团队频道出现运行消息并完成
  await page.locator("#gov-bot-select").selectOption("auditor");
  await page.locator("#gov-bot-target").fill("fixtures/demo-target");
  await page.locator("#gov-bot-dispatch button[type=submit]").click();
  const auditorMessage = page.locator("#gov-team-channel .gov-bot-message", { hasText: "审计员" }).first();
  await expect(auditorMessage).toBeVisible({ timeout: 15_000 });
  await expect(auditorMessage).toContainText("完成", { timeout: 90_000 });
  await expect(auditorMessage).toContainText("发现");
  await expect(auditorMessage.locator("[data-gov-hist]")).toBeVisible();

  // 复核员：留空目标 = 最新一次 bundle
  await page.locator("#gov-bot-select").selectOption("verifier");
  await page.locator("#gov-bot-target").fill("");
  await page.locator("#gov-bot-dispatch button[type=submit]").click();
  const verifierMessage = page.locator("#gov-team-channel .gov-bot-message", { hasText: "复核员" }).last();
  await expect(verifierMessage).toBeVisible({ timeout: 15_000 });
  await expect(verifierMessage).toContainText("完成", { timeout: 30_000 });
  await expect(verifierMessage).toContainText("链完整");

  // 复核消息 → 工作台证据面
  await verifierMessage.locator("[data-gov-hist]").click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await expect(page.locator("#gov-summary .gov-target-head")).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#gov-findings-list [data-gov-finding]").first()).toBeVisible();
});