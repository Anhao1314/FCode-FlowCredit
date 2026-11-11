import { test, expect } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/* Work 首页：项目持久化、项目运行进看板、刷新不丢、归档后消失。
   运行用离线 mock + API 播种，避免依赖真实 provider。 */

test("Work 首页：创建项目 → 项目运行入看板 → 刷新持久化 → 归档", async ({ page }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fcode-work-"));
  fs.writeFileSync(path.join(dir, "README.md"), "demo\n");
  try {
    await page.goto("/");
    await page.waitForSelector('body[data-booted="1"]');
    await expect(page.locator("#main")).toHaveAttribute("data-view", "work");

    await page.locator("[data-work-new-project]").first().click();
    const form = page.locator("[data-work-project-form]");
    await expect(form).toBeVisible();
    await form.locator('input[name="name"]').fill("E2E 项目");
    await form.locator('input[name="path"]').fill(dir);
    await form.locator('input[name="verification"]').fill("true");
    await form.locator('button[type="submit"]').click();

    const card = page.locator('[data-project-card]').filter({ hasText: "E2E 项目" });
    await expect(card).toBeVisible();
    const projectId = await card.getAttribute("data-project-card");
    expect(projectId).toMatch(/^prj-/);
    // 侧栏 Projects 也来自服务端快照
    await expect(page.locator("#chat-projects")).toContainText("E2E 项目");

    const created = await page.request.post("/api/runs", {
      data: {
        template: "general-research",
        objective: "E2E 项目运行",
        projectId,
        approval: "off",
        model: "mock",
        spec: { mode: "open" },
      },
    });
    expect(created.ok()).toBeTruthy();

    await page.reload();
    await page.waitForSelector('body[data-booted="1"]');
    await expect(page.locator("#main")).toHaveAttribute("data-view", "work");
    await expect(page.locator('[data-project-card]').filter({ hasText: "E2E 项目" })).toBeVisible();
    await expect(page.locator(".board-run").filter({ hasText: "E2E 项目运行" })).toBeVisible();

    // 设置 → 归档（确认框）
    await page.locator('[data-project-edit]').first().click();
    page.once("dialog", dialog => dialog.accept());
    await page.locator("[data-project-archive]").click();
    await expect(page.locator('[data-project-card]').filter({ hasText: "E2E 项目" })).toHaveCount(0);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
