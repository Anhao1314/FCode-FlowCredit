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
    await expect(page.locator("#chat-projects")).toContainText("项目");

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
    await expect(page.locator(".task-thread .th-user .th-user-text")).toBeVisible();
    await expect(page.locator(".task-thread .th-status .ai-avatar")).toBeVisible();

    // 变更/文件收进右侧扩展栏：主区线程不被浮层替换
    await page.locator('[data-work-tab="changes"]').click();
    await expect(page.locator(".rail")).toHaveAttribute("data-rail-mode", "changes");
    await expect(page.locator(".work-inspector")).toHaveCount(0);
    await page.locator(".rail-close").click();
    await expect(page.locator(".work-view-tab.active")).toHaveText("执行");

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

test("Work：已删除的项目目录在建 run 前拒绝，清绑定并保留任务", async ({ page, request }) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "fcode-stale-project-"));
  fs.writeFileSync(path.join(dir, "README.md"), "stale\n");
  let project = null;
  try {
    const created = await request.post("/api/projects", {
      data: { name: "E2E 失效项目", path: dir, verification: "true" },
    });
    expect(created.status()).toBe(201);
    project = await created.json();
    fs.rmSync(dir, { recursive: true, force: true });

    await page.addInitScript(value => {
      localStorage.setItem("fa.workProject", JSON.stringify(value));
    }, {
      path: project.path, verification: project.verification,
      projectId: project.id, name: project.name,
    });
    const objective = "保留这条被失效项目阻塞的任务";
    await page.route("**/api/intake", route => route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        reply: "已就绪。", thought: "", spec: { mode: "open" }, choices: [],
        task: { objective, template: "general-research", approval: "off", spec: { mode: "open" } },
      }),
    }));
    await page.goto("/");
    await page.waitForSelector('body[data-booted="1"]');
    const rejected = page.waitForResponse(response =>
      response.url().endsWith("/api/runs") && response.request().method() === "POST");
    await page.locator("#composer-ta").fill(objective);
    await page.locator("#composer-ta").press("Enter");
    expect((await rejected).status()).toBe(400);

    await expect(page.locator("#toast")).toContainText("重新选择项目");
    await expect(page.locator("#toast")).toContainText("任务内容已保留");
    await expect(page.locator("#app")).toHaveClass(/intake-mode/);
    await expect(page.locator(".intake-task")).toContainText(objective);
    await expect.poll(() => page.evaluate(() =>
      localStorage.getItem("fa.workProject") || "")).toBe("");

    const runs = await request.get(`/api/runs?projectId=${project.id}`);
    expect(runs.status()).toBe(200);
    expect(await runs.json()).toEqual([]);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    if (project) await request.post("/api/projects/archive", { data: { id: project.id } });
  }
});

test("900px 导航是可聚焦的模态抽屉，关闭后恢复触发按钮", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 900 });
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  const sidebar = page.locator(".sidebar");
  const trigger = page.locator("#workspace-menu");
  await expect(sidebar).toHaveAttribute("aria-hidden", "true");
  await expect(sidebar).toHaveAttribute("inert", "");
  await trigger.click();
  await expect(sidebar).toHaveAttribute("aria-hidden", "false");
  await expect(sidebar).toHaveAttribute("aria-modal", "true");
  await expect(page.locator("#sidebar-toggle")).toBeFocused();
  await expect(page.locator("#workspace")).toHaveAttribute("inert", "");
  await page.locator("#sidebar-toggle").click();
  await expect(trigger).toBeFocused();
  await expect(sidebar).toHaveAttribute("aria-hidden", "true");
  await trigger.click();
  await page.keyboard.press("Escape");
  await expect(trigger).toBeFocused();
  await expect(page.locator("#workspace")).not.toHaveAttribute("inert", "");
});
