import { test, expect } from "@playwright/test";

/* 分区历史（Kimi 式）：Work 区显示对话/任务历史，Governance 区显示
   审计历史；两套互斥显隐（body[data-zone]）。审计历史行点开 → 审计台深链。 */

test("分区历史:Work 显示对话段,Governance 显示审计历史并深链审计台", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  // Work 区：对话段可见，审计历史隐藏
  await expect(page.locator("#chat-chats")).toBeVisible();
  await expect(page.locator("#gov-history")).toBeHidden();
  await expect(page.locator("body")).toHaveAttribute("data-zone", "work");

  // 切 Governance：审计历史出现，对话/任务段隐藏
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await expect(page.locator("body")).toHaveAttribute("data-zone", "gov");
  await expect(page.locator("#gov-history")).toBeVisible();
  await expect(page.locator("#chat-chats")).toBeHidden();
  await expect(page.locator("#chat-tasks")).toBeHidden();

  // 审计历史行（E2E 仓库自带 bundle）→ 深链进审计台
  const row = page.locator("#gov-history [data-gov-hist]").first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#gov-history")).toContainText("审计历史");
  await row.click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "audit");
  await expect(page).toHaveURL(/#\/audit\//);
  // 审计台属治理区：切换器仍亮 Governance
  await expect(page.locator("body")).toHaveAttribute("data-zone", "gov");

  // 回 Work：对话段恢复，审计历史隐藏
  await page.locator('#zone-switch [data-zone="work"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "empty");
  await expect(page.locator("body")).toHaveAttribute("data-zone", "work");
  await expect(page.locator("#chat-chats")).toBeVisible();
  await expect(page.locator("#gov-history")).toBeHidden();
});
