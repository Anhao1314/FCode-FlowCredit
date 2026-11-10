import { test, expect } from "@playwright/test";

/* S1 修洞闭环：发现卡两个出口——
   ①「重新挖洞」:同目标再审计 → 作业卡轮询到完成(验证修复的闭环入口)
   ②「派 agent 修复」:发现带上下文送进 Work 委托确认页(验证目标由操作员补)

   注意:先在 E2E 仓库内挖一次(相对路径 fixtures/demo-target)。globalSetup
   拷来的历史 bundle 里是真实仓库的绝对路径,在沙箱里无法重挖——那正是
   「目标失效」的诚实反馈,不能拿来做闭环用例。 */

test("修洞闭环:重新挖洞到完成;派 agent 修复进委托确认页", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await page.locator('[data-gov-mode="workbench"]').click();
  await expect(page.locator("#gov-target-input")).toBeVisible({ timeout: 10_000 });

  // 仓内目标挖一次（工作台顶部表单；治理区默认是对话面）
  await page.locator("#gov-target-input").fill("fixtures/demo-target");
  await page.locator("#gov-new-audit button[type=submit]").click();
  const job = page.locator("#gov-activity .gov-job", { hasText: "fixtures/demo-target" }).first();
  await expect(job).toBeVisible({ timeout: 15_000 });
  await expect(job.locator(".gov-job-state")).toContainText("完成", { timeout: 90_000 });

  // 第一条发现 → 详情抽屉。收起的下拉里 option 不算 visible，
  // 按 DOM 计数据实断言「已修复」档存在。
  const findings1 = page.locator("#gov-findings-list [data-gov-finding]").first();
  await expect(findings1).toBeVisible({ timeout: 15_000 });
  await findings1.click();
  const detail1 = page.locator("#gov-detail-host .gov-detail-card");
  await expect(detail1.locator("[data-gov-repair]")).toBeVisible();
  await expect.poll(async () => detail1.locator('option[value="repaired"]').count()).toBeGreaterThan(0);

  // ① 重新挖洞 → 第二个作业（新 job 排最前）也轮询到完成
  await page.locator("#gov-summary [data-gov-redig]").click();
  await expect(page.locator("#gov-activity .gov-job", { hasText: "fixtures/demo-target" }))
    .toHaveCount(2, { timeout: 15_000 });
  await expect(async () => {
    const done = await page.locator("#gov-activity .gov-job", { hasText: "fixtures/demo-target" })
      .filter({ hasText: "完成" }).count();
    expect(done).toBe(2);
  }).toPass({ timeout: 90_000, intervals: [2_000] });

  // ② 派 agent 修复（最新 bundle 的发现）→ 委托确认页，目标带发现上下文
  await expect(page.locator("#gov-findings-list [data-gov-finding]").first())
    .toBeVisible({ timeout: 15_000 });
  const freshRow = page.locator("#gov-findings-list [data-gov-finding]").first();
  const findingId = (await freshRow.locator(".gov-row-id").textContent()).trim();
  await freshRow.click();
  const freshDetail = page.locator("#gov-detail-host .gov-detail-card");
  await expect(freshDetail.locator("[data-gov-repair]")).toBeVisible();
  await freshDetail.locator("[data-gov-repair]").click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "intake", { timeout: 15_000 });
  await expect(page.locator("#main")).toContainText("修复审计发现");
  await expect(page.locator("#main")).toContainText(findingId);
  await expect(page.locator("#main")).toContainText("要求：在工作区副本里定位并修复根因");
});
