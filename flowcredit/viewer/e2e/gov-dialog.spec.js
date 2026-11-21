import { test, expect } from "@playwright/test";

/* 治理对话面：默认入口是对话，不是工作台。命令 /挖洞 → 作业卡 →
   轮询完成 → 「查看结果」切到工作台。 */

test("治理首页:任务启动区保留三个动画角色,命令只填充且窄屏无溢出", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();

  await expect(page.locator(".gov-dlg")).toHaveClass(/is-home/);
  await expect(page.locator("#gov-home-title")).toHaveText("今天要审计什么？");
  const crew = page.locator(".gov-home-crew .gov-home-agent");
  await expect(crew).toHaveCount(3);
  await expect(page.locator('.gov-home-crew [data-agent-role="auditor"]')).toHaveAttribute("data-ai-state", "idle");
  await expect(page.locator('.gov-home-crew [data-agent-role="verifier"]')).toHaveAttribute("data-ai-state", "idle");
  await expect(page.locator('.gov-home-crew [data-agent-role="analyst"]')).toHaveAttribute("data-ai-state", "idle");
  const animation = await page.locator(".gov-home-agent .gov-agent-symbol").first()
    .evaluate(el => getComputedStyle(el).animationName);
  expect(animation).not.toBe("none");

  await page.locator('[data-gov-fill="/挖洞 "]').click();
  await expect(page.locator("#composer-ta")).toHaveValue("/挖洞 ");
  await expect(page.locator("#gov-dialog-stream .gov-entry.user")).toHaveCount(0);
  await page.locator('[data-gov-fill="/判断"]').click();
  await expect(page.locator("#composer-ta")).toHaveValue("/判断");
  await expect(page.locator("#gov-home-title")).toBeVisible();

  for (const width of [900, 375]) {
    await page.setViewportSize({ width, height: 800 });
    await expect(page.locator("#gov-home-title")).toBeVisible();
    await expect(page.locator(".gov-start")).toBeVisible();
    await expect.poll(() => page.locator(".center").evaluate(el => Math.round(el.getBoundingClientRect().left) <= 1)).toBe(true);
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), {
      message: `${width}px 侧栏过渡结束后不应出现横向溢出`,
    }).toBe(true);
  }
});

test("治理对话:/挖洞 产生作业卡,完成后可切工作台复核", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('#zone-switch [data-zone="gov"]').click();
  await expect(page.locator("#gov-dialog-stream")).toBeVisible();
  await expect(page.locator(".composer-wrap")).toBeVisible();

  await page.locator("#composer-ta").fill("/挖洞 fixtures/demo-target");
  await page.keyboard.press("Enter");
  const job = page.locator("#gov-dialog-stream .gov-job", { hasText: "fixtures/demo-target" }).first();
  await expect(job).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".gov-dlg")).not.toHaveClass(/is-home/);
  await expect(page.locator("#gov-home-title")).toHaveCount(0);
  await expect(job.locator(".gov-job-state")).toContainText("完成", { timeout: 90_000 });

  await job.locator("[data-gov-hist]").click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  await expect(page.locator("#gov-summary .gov-target-head")).toBeVisible({ timeout: 10_000 });
  await expect(page.locator(".composer-wrap")).toBeHidden();
  await expect(page.locator("#gov-findings-list [data-gov-finding]").first())
    .toBeVisible({ timeout: 10_000 });
});