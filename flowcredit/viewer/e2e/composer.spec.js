import { test, expect } from "@playwright/test";

/* Composer gestures: Work's `@` references a workspace path; Chat keeps its
   explicit attachment model and must not open the Work picker. */

test("Work 的 @ 插入工作区相对路径", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.keyboard.press("ControlOrMeta+k");
  await page.locator("#palette-input").fill("新委托");
  await page.locator("#palette-input").press("Enter");
  await expect(page.locator("#app")).toHaveClass(/intake-mode/);

  const ta = page.locator("#composer-ta");
  await ta.fill("@README");
  const menu = page.locator("#workspace-mention-menu");
  await expect(menu).toBeVisible();
  await expect(menu.locator(".mention-item").first()).toContainText("README.md");
  await menu.locator(".mention-item").first().click();
  await expect(ta).toHaveValue(/@\S*README\.md/);
  await expect(menu).toBeHidden();
});

test("新对话的模型菜单不越出视口", async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.getByRole("button", { name: /新对话/ }).first().click();
  await page.locator("#model-btn").click();
  const menu = page.locator("#chat-model-menu");
  await expect(menu).toBeVisible();
  await expect(menu.locator(".tpl-item").first()).toBeVisible();
  await expect(menu.locator(".model-current-dot")).toBeVisible();
  const box = await menu.boundingBox();
  const chip = await page.locator("#model-btn").boundingBox();
  const viewport = page.viewportSize();
  expect(box).toBeTruthy();
  expect(box.y).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(viewport.height);
  expect(box.y + box.height).toBeLessThanOrEqual(chip.y, "菜单应向上展开，卡片底边停在模型芯片上方");
});

test("Chat 输入 @ 不打开 Work 引用器", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.getByRole("button", { name: /新对话/ }).first().click();
  const ta = page.locator("#composer-ta");
  await ta.fill("@README");
  await expect(page.locator("#workspace-mention-menu")).toBeHidden();
});
test("Work 分流：歧义输入落在任务确认页，不建 Run 也不进 Chat", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-booted", "1");
  const taskPosts = [];
  const chatPosts = [];
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() !== "POST") return;
    if (path === "/api/runs") taskPosts.push(path);
    if (path === "/api/chat/send") chatPosts.push(path);
  });
  const input = page.locator("#composer-ta");
  await input.fill("继续刚才那个修改");
  await expect(page.locator("#send")).toBeEnabled();
  await page.locator("#send").click();

  // clarify: 任务确认页出现（intake-mode），没有创建运行，也没有走 chat 流。
  await expect(page.locator("#app")).toHaveClass(/intake-mode/);
  await expect(page.locator("#tb-title")).toContainText(/新委托|任务准备中/);
  await expect.poll(() => taskPosts.length, { timeout: 5000 }).toBe(0);
  await expect.poll(() => chatPosts.length, { timeout: 1000 }).toBe(0);
});

test("Work 分流：问候直接进入 Chat，不创建 Run", async ({ page }) => {
  await page.route("**/api/chat/send", async route => {
    const frames = [
      ["start", { thread_id: "greet-thread", thread_title: "你好", assistant_id: "a-greet" }],
      ["delta", { text: "你好，我在。" }],
      ["done", { message: { id: "a-greet", text: "你好，我在。", usage: {} } }],
    ];
    await route.fulfill({
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
      body: frames.map(([type, data]) => `data: ${JSON.stringify({ type, ...data })}\n\n`).join(""),
    });
  });
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-booted", "1");
  const runPosts = [];
  page.on("request", request => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/runs") {
      runPosts.push("/api/runs");
    }
  });
  const input = page.locator("#composer-ta");
  await input.fill("你好");
  await expect(page.locator("#send")).toBeEnabled();
  await page.locator("#send").click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "chat");
  await expect.poll(() => runPosts.length, { timeout: 3000 }).toBe(0);
});

test("「+」面板承载执行模式：电脑开启随 spec 提交，Fast 关闭后不带 fast", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-booted", "1");
  await page.locator("#composer-open").click();
  const modes = page.locator(".add-menu-modes");
  await expect(modes).toBeVisible();
  await expect(modes.locator("#fast-btn")).toHaveAttribute("aria-pressed", "true");
  await expect(modes.locator("#computer-btn")).toHaveAttribute("aria-pressed", "false");
  await expect(page.locator("#composer-mode-badge")).toBeHidden();

  await modes.locator("#computer-btn").click();
  await expect(modes.locator("#computer-btn")).toHaveAttribute("aria-pressed", "true");
  await modes.locator("#fast-btn").click();
  await expect(modes.locator("#fast-btn")).toHaveAttribute("aria-pressed", "false");
  const badge = page.locator("#composer-mode-badge");
  await expect(badge).toBeVisible();
  await expect(badge).toContainText("逐步");
  await expect(badge).toContainText("电脑");

  const intakePromise = page.waitForRequest(
    r => new URL(r.url()).pathname === "/api/intake" && r.method() === "POST");
  const runPromise = page.waitForRequest(
    r => new URL(r.url()).pathname === "/api/runs" && r.method() === "POST",
    { timeout: 20000 });
  await page.locator("#composer-ta").fill("帮我修复当前项目的登录 bug");
  await page.locator("#send").click();

  const intakeBody = (await intakePromise).postDataJSON();
  expect(intakeBody.spec.computerUse).toBe(true);
  const runBody = (await runPromise).postDataJSON();
  expect(runBody.spec.computerUse).toBe(true);
  expect("fast" in runBody.spec).toBe(false);
});
