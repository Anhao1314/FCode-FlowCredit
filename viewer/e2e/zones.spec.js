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

  // 审计历史行 → 工作台选中该目标（发现表 + 详情抽屉，可继续处置/派修复）。
  // 选一条有发现的记录：0 发现的 artifact 目标不会渲染处置控件。
  const row = page.locator("#gov-history [data-gov-hist]")
    .filter({ hasNotText: "0 发现" }).first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#gov-history")).toContainText("审计历史");
  await row.click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await expect(page.locator("#gov-summary .gov-target-head")).toBeVisible({ timeout: 10_000 });
  // 发现表一行 → 详情抽屉（证据引用 + 处置）
  const findingRow = page.locator("#gov-findings-list [data-gov-finding]").first();
  await expect(findingRow).toBeVisible({ timeout: 10_000 });
  await findingRow.click();
  const detail = page.locator("#gov-detail-host .gov-detail-card");
  await expect(detail).toBeVisible();
  await expect(detail.locator(".gov-disp")).toBeVisible();

  // 「查看证据」才进审计台密集视图——且保留治理 composer（零模型 console）
  await detail.locator("[data-gov-open-bundle]").click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "audit");
  await expect(page).toHaveURL(/#\/audit\//);
  await expect(page.locator("body")).toHaveAttribute("data-zone", "gov");
  await expect(page.locator(".composer-wrap")).toBeVisible();
  await expect(page.locator("#model-btn")).toBeHidden();
  await expect(page.locator("#composer-ta")).toHaveAttribute("placeholder", /治理命令/);

  // 回 Work：对话段恢复，审计历史隐藏
  await page.locator('#zone-switch [data-zone="work"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "empty");
  await expect(page.locator("body")).toHaveAttribute("data-zone", "work");
  await expect(page.locator("#chat-chats")).toBeVisible();
  await expect(page.locator("#gov-history")).toBeHidden();
});
