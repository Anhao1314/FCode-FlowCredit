import { test, expect } from "@playwright/test";

/* 分区历史（Kimi 式）：Work 区显示对话/任务历史，Governance 区显示
   治理历史；两套互斥显隐（body[data-zone]）。团队运行回 Bot 详情，审计记录进工作台。 */

test("分区历史:Work 显示对话段,Governance 显示治理历史并深链审计台", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  // Work 区：对话段可见，治理历史隐藏
  await expect(page.locator("#chat-chats")).toBeVisible();
  await expect(page.locator("#gov-history")).toBeHidden();
  await expect(page.locator("body")).toHaveAttribute("data-zone", "work");

  // 切 Governance：治理历史出现，对话/任务段隐藏
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await expect(page.locator("body")).toHaveAttribute("data-zone", "gov");
  await expect(page.locator("#gov-history")).toBeVisible();
  await expect(page.locator("#chat-chats")).toBeHidden();
  await expect(page.locator("#chat-tasks")).toBeHidden();
  // 治理区默认是对话面：composer 可见，工作台通过分段控件进入
  await expect(page.locator("#gov-dialog-stream")).toBeVisible();
  await expect(page.locator(".composer-wrap")).toBeVisible();
  await page.locator('[data-gov-mode="workbench"]').click();
  await expect(page.locator("#gov-target-input")).toBeVisible({ timeout: 10_000 });
  await expect(page.locator(".composer-wrap")).toBeHidden();
  // 回对话面走区落地（对话行已清理）：Governance 区按钮落地即对话
  await page.locator('.zone-btn[data-open="gov"]').click();
  await expect(page.locator("#gov-dialog-stream")).toBeVisible();
  await expect(page.locator(".composer-wrap")).toBeVisible();

  // 审计记录行 → 工作台选中该目标（发现表 + 详情抽屉，可继续处置/派修复）。
  // 选一条有发现的记录：0 发现的 artifact 目标不会渲染处置控件。
  const row = page.locator("#gov-history [data-gov-hist]")
    .filter({ hasNotText: "0 发现" }).first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#gov-history")).toContainText("治理历史");
  await row.click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await expect(page.locator("#gov-summary .gov-target-head")).toBeVisible({ timeout: 10_000 });
  await expect(page.locator(".composer-wrap")).toBeHidden();
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
  // 审计台入口就在治理区侧栏，且当前面点亮
  await expect(page.locator('#gov-surface-nav [data-open="audit"]'))
    .toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".composer-wrap")).toBeVisible();
  await expect(page.locator("#model-btn")).toBeHidden();
  await expect(page.locator("#composer-ta")).toHaveAttribute("placeholder", /治理命令/);

  // 回 Work：对话段恢复，治理历史隐藏
  await page.locator('#zone-switch [data-zone="work"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "empty");
  await expect(page.locator("body")).toHaveAttribute("data-zone", "work");
  await expect(page.locator("#chat-chats")).toBeVisible();
  await expect(page.locator("#gov-history")).toBeHidden();
});
