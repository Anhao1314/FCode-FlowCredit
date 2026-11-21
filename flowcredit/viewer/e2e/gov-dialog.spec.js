import { test, expect } from "@playwright/test";

/* 治理对话：正常模型对话 → gov_action 提案 → 操作员确认 → 团队运行 →
   刷新后线程与运行卡仍在。 */

test("治理对话:自然语言提案、确认派团队、线程持久", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await page.locator("#gov-new-dialog").click();
  await expect(page.locator("#gov-chat-home")).toBeVisible();
  await expect(page.locator("#composer-ta")).toBeVisible();
  await expect(page.locator("#model-btn")).toBeVisible();

  await page.locator("#composer-ta").fill("让审计员扫描 fixtures/demo-target");
  await page.locator("#send").click();

  const assistant = page.locator("#gov-chat-stream .gov-chat-msg.assistant").last();
  await expect(assistant).toContainText("离线 Mock 回复", { timeout: 15_000 });
  const proposal = assistant.locator("[data-gov-proposal]");
  await expect(proposal).toBeVisible();
  await expect(proposal).toContainText("审计员 · 扫描");
  await expect(proposal).toContainText("fixtures/demo-target");

  await proposal.locator("[data-gov-proposal-confirm]").click();
  const runCard = page.locator("#gov-chat-stream [data-gov-chat-run]").last();
  await expect(runCard).toBeVisible({ timeout: 20_000 });
  await expect(runCard).toContainText("审计员");
  await expect(proposal).toHaveCount(0);

  // 刷新：治理线程从服务端恢复，不再回到内存命令壳。
  await page.reload();
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await expect(page.locator("#gov-chat-stream")).toBeVisible();
  await expect(page.locator("#gov-chat-stream")).toContainText("离线 Mock 回复", { timeout: 15_000 });
  await expect(page.locator("#gov-chat-stream [data-gov-chat-run]").last())
    .toBeVisible({ timeout: 20_000 });
});
