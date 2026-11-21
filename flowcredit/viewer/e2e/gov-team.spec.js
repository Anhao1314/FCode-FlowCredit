import { test, expect } from "@playwright/test";

/* Phase A 治理团队：花名册 → 派活给审计员 → 团队频道完成 →
   复核员验链 → 从消息回工作台。 */

test("治理团队:从对话进入团队，审计员扫描、复核员验链、消息回工作台", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await expect(page.locator("#composer-ta")).toBeVisible();
  await page.locator("#gov-chat-team-entry").click();

  await expect(page.locator("#gov-team-roster .gov-bot-card")).toHaveCount(3);
  await expect(page.locator("#gov-team-summary")).toContainText("常驻角色");
  await expect(page.locator("#gov-team-summary")).toContainText("手动 · 定时 · 审批");
  await expect(page.locator("#gov-team-pending")).toContainText("待批准");
  await expect(page.locator('#gov-team-roster .ai-avatar[data-ai-state]')).toHaveCount(3);
  // 头像不是静态图案：眨眼/呼吸持续在跑（与对话区同一张实时脸）
  await expect.poll(async () => page.locator('#gov-team-roster .ai-avatar').first()
    .locator('.ai-eye').first().evaluate(el => getComputedStyle(el).animationName)).toBe("ai-avatar-blink");
  await expect(page.locator(".composer-wrap")).toBeHidden();

  // 审计员：目标路径 → 团队频道出现运行消息并完成
  await page.locator("#gov-bot-select").selectOption("auditor");
  await page.locator("#gov-bot-target").fill("fixtures/demo-target");
  await page.locator("#gov-bot-dispatch button[type=submit]").click();
  // 实时状态：派活后花名册头像立即进入 thinking（扫视 + 呼吸环）
  await expect(page.locator('#gov-team-roster .ai-avatar[data-ai-state="thinking"]').first())
    .toBeVisible({ timeout: 15_000 });
  const auditorMessage = page.locator("#gov-team-channel .gov-bot-message", { hasText: "审计员" }).last();
  await expect(auditorMessage).toBeVisible({ timeout: 15_000 });
  await expect(auditorMessage).toContainText("完成", { timeout: 90_000 });
  await expect(auditorMessage).toContainText("发现");
  await expect(auditorMessage.locator("[data-gov-hist]")).toBeVisible();
  // 侧栏治理历史只剩审计记录——团队运行收进右上侧栏的频道流里
  await expect(page.locator("#gov-history")).not.toContainText("团队运行", { timeout: 12_000 });
  await expect(page.locator("#gov-history")).toContainText("审计记录");

  // 教一次：从完成的运行生成技能，进入 Bot 详情；保存预算设置
  await auditorMessage.locator("[data-gov-skill-run]").click();
  await expect(page.locator("#gov-bot-detail-host")).toBeVisible({ timeout: 10_000 });
  await expect(page.locator("#gov-bot-detail-host .gov-skill-row")).toHaveCount(1);
  await expect(page.locator("#gov-bot-detail-host")).toContainText("审计员");
  await expect(page.locator('#gov-bot-detail-host .ai-avatar[data-ai-state]').first()).toBeVisible();
  await page.locator("#gov-bot-daily-limit").fill("5");
  await page.locator("#gov-bot-settings-form button[type=submit]").click();
  await expect(page.locator("#gov-bot-daily-limit")).toHaveValue("5", { timeout: 10_000 });
  await page.locator("[data-gov-bot-back]").click();
  await expect(page.locator("#gov-team-roster .gov-bot-card")).toHaveCount(3);

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