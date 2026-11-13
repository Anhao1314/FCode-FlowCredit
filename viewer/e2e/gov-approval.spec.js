import { test, expect } from "@playwright/test";

/* 定时运行审批：打开 routine 审批后，到点不直接执行，先在团队频道
   生成「待批准」运行；操作员批准后才真正跑。 */

test("治理团队:定时运行等待批准，批准后完成", async ({ page }) => {
  // “00:00”今天一定已经过去（除非恰好运行在 00:00 前 20s 的调度窗），
  // 避免跨午夜时 due 时间落到明天导致 flake。
  const hhmm = "00:00";

  await page.request.post("/api/gov/bots/auditor/settings", {
    data: { daily_run_limit: 8, routine_approval: true },
  });
  await page.request.post("/api/gov/routines", {
    data: {
      bot_id: "auditor",
      target: "fixtures/demo-target",
      frequency: "每天",
      time: hhmm,
    },
  });

  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await page.locator('[data-gov-mode="team"]').click();
  await expect(page.locator("#gov-team-roster .gov-bot-card")).toHaveCount(2);

  // 待批准置顶：先定位 pending 卡，再按 run id 跟踪它（批准后会移到时间流）。
  const pending = page.locator("#gov-team-channel .gov-bot-message", { hasText: "待批准" }).first();
  await expect(pending).toBeVisible({ timeout: 45_000 });
  const runId = await pending.getAttribute("data-gov-run-card");
  const runMessage = page.locator(`#gov-team-channel [data-gov-run-card="${runId}"]`);
  await expect(runMessage.locator("[data-gov-run-approve]")).toBeVisible();
  await expect(runMessage.locator("[data-gov-run-reject]")).toBeVisible();

  await runMessage.locator("[data-gov-run-approve]").click();
  await expect(runMessage).not.toContainText("待批准", { timeout: 15_000 });
  await expect(runMessage).toContainText("完成", { timeout: 30_000 });
  await expect(runMessage.locator("[data-gov-hist]")).toBeVisible();
});