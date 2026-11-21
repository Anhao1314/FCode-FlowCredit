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
  // 治理区默认是正常对话；团队是对话侧栏/下钻面
  await page.locator("#gov-new-dialog").click();
  await expect(page.locator("#gov-chat-home")).toBeVisible();
  await expect(page.locator("#composer-ta")).toBeVisible();
  await expect(page.locator('[data-gov-mode="dialog"]')).toHaveAttribute("aria-selected", "true");
  await expect(page.locator('#gov-surface-nav [data-open="audit"]')).toHaveAttribute("aria-selected", "false");
  await page.locator("#gov-chat-team-entry").click();
  await expect(page.locator("#gov-team-roster")).toBeVisible();
  await expect(page.locator("#gov-team-pending")).toBeVisible();
  await expect(page.locator(".composer-wrap")).toBeHidden();
  await page.locator("[data-gov-team-back]").click();
  await expect(page.locator("#gov-chat-home")).toBeVisible();
  await expect(page.locator("#composer-ta")).toBeVisible();

  // 审计记录行 → 工作台选中该目标（发现表 + 详情抽屉，可继续处置/派修复）。
  // 选一条有发现的记录：0 发现的 artifact 目标不会渲染处置控件。
  const row = page.locator("#gov-history [data-gov-hist]")
    .filter({ hasNotText: "0 发现" }).first();
  await expect(row).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#gov-history")).toContainText("审计记录");
  await row.click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await expect(page.locator("#gov-summary .gov-target-head")).toBeVisible({ timeout: 10_000 });
  // Regression: the team rail used to stay mounted, squeezing the audit target
  // title into a 0px column (one character per line).
  await expect(page.locator(".rail")).toBeHidden();
  const titleBox = await page.locator(".gov-target-title h2").boundingBox();
  expect(titleBox && titleBox.width).toBeGreaterThan(120);
  await expect(page.locator(".composer-wrap")).toBeHidden();
  await expect(page.locator("[data-gov-team-back]")).toBeVisible();
  // 探员候选入口：mock 模型下 fail-closed，诚实提示（候选不伪造）
  await expect(page.locator("[data-gov-hunt]")).toBeVisible();
  await page.locator("[data-gov-hunt]").click();
  await expect(page.locator("#toast")).toContainText("探员候选", { timeout: 15_000 });
  // 发现表一行 → 详情抽屉（证据引用 + 处置 + 解读出口）
  const findingRow = page.locator("#gov-findings-list [data-gov-finding]").first();
  await expect(findingRow).toBeVisible({ timeout: 10_000 });
  await findingRow.click();
  const detail = page.locator("#gov-detail-host .gov-detail-card");
  await expect(detail).toBeVisible();
  await expect(detail.locator(".gov-disp")).toBeVisible();
  // 解读出口在工作台，不在账本：点击后输出进治理对话（mock 下 fail-closed 诚实提示）
  await expect(detail.locator("[data-gov-explain-finding]")).toBeVisible();
  await detail.locator("[data-gov-explain-finding]").click();
  await expect(page.locator("#toast")).toContainText("AI 解读", { timeout: 15_000 });

  // 「查看证据」才进审计台密集视图——纯证据面，不再保留命令输入框
  await detail.locator("[data-gov-open-bundle]").click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "audit");
  await expect(page).toHaveURL(/#\/audit\//);
  await expect(page.locator("body")).toHaveAttribute("data-zone", "gov");
  // 审计台入口就在治理区侧栏，且当前面点亮
  await expect(page.locator('#gov-surface-nav [data-open="audit"]'))
    .toHaveAttribute("aria-selected", "true");
  await expect(page.locator(".composer-wrap")).toBeHidden();

  // 回 Work：对话段恢复，治理历史隐藏
  await page.locator('#zone-switch [data-zone="work"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "empty");
  await expect(page.locator("body")).toHaveAttribute("data-zone", "work");
  await expect(page.locator("#chat-chats")).toBeVisible();
  await expect(page.locator("#gov-history")).toBeHidden();
});
