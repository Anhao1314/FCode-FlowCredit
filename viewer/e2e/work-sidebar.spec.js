import { test, expect } from "@playwright/test";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

/* Work 侧栏：项目创建/展开、运行嵌在项目下、刷新持久化、设置与归档。
   运行用离线 mock + API 播种；主区始终是任务/对话舞台。 */

test("Work 侧栏：项目创建 → 运行嵌套 → 刷新 → 归档", async ({ page, request }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fcode-sidebar-"));
  fs.writeFileSync(path.join(dir, "README.md"), "demo\n");
  fs.writeFileSync(path.join(dir, "verify.py"), "raise SystemExit(0)\n");
  try {
    await page.goto("/");
    await page.waitForSelector('body[data-booted="1"]');
    await expect(page.locator("#main")).toHaveAttribute("data-view", "empty");
    await expect(page.locator("#chat-projects")).toContainText("Projects");

    await page.locator("[data-proj-add]").click();
    const form = page.locator("[data-proj-form]");
    await expect(form).toBeVisible();
    await form.locator('input[name="name"]').fill("E2E 侧栏项目");
    await form.locator('input[name="path"]').fill(dir);
    await form.locator('input[name="verification"]').fill("python3 verify.py");
    await form.locator('button[type="submit"]').click();

    const toggle = page.locator("[data-proj-toggle]").filter({ hasText: "E2E 侧栏项目" });
    await expect(toggle).toBeVisible();
    // 创建后自动选中并绑定 composer 上下文
    await expect(page.locator("#composer-ta")).toHaveAttribute("placeholder", /E2E 侧栏项目/);
    const block = page.locator("[data-proj-block]").filter({ hasText: "E2E 侧栏项目" });
    const projectId = await block.getAttribute("data-proj-block");
    expect(projectId).toMatch(/^prj-/);

    const created = await request.post("/api/runs", {
      data: {
        template: "general-research",
        objective: "侧栏项目的运行",
        projectId,
        approval: "off",
        model: "mock",
        spec: { mode: "open" },
      },
    });
    expect(created.ok()).toBeTruthy();

    await page.reload();
    await page.waitForSelector('body[data-booted="1"]');
    const reopened = page.locator("[data-proj-block]").filter({ hasText: "E2E 侧栏项目" });
    await expect(reopened).toBeVisible();
    await expect(reopened.locator(".proj-runs .run-row")).toContainText("侧栏项目的运行");

    // 点运行进运行视图；主区不是项目看板
    await reopened.locator(".proj-runs .run-row").first().click();
    await expect(page.locator("#main")).toHaveAttribute("data-view", "run");

    // 设置 → 两步确认归档（不用原生 confirm）；归档清掉 composer 绑定
    await page.locator("[data-proj-toggle]").filter({ hasText: "E2E 侧栏项目" }).hover();
    await reopened.locator("[data-proj-edit]").click();
    const archiveBtn = page.locator("[data-proj-archive]");
    await archiveBtn.click();
    await expect(archiveBtn).toHaveText("确认归档？");
    await archiveBtn.click();
    await expect(page.locator("[data-proj-toggle]").filter({ hasText: "E2E 侧栏项目" })).toHaveCount(0);
    await expect(page.locator("#composer-ta")).toHaveAttribute(
      "placeholder", "从项目开始，或直接描述任务…");
    await expect.poll(() => page.evaluate(() => {
      const saved = JSON.parse(localStorage.getItem("fa.workProject") || "{}");
      return saved.projectId || "";
    })).toBe("", "归档必须清掉 composer 的项目绑定");
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});
