import { test, expect } from "@playwright/test";

function event(sequence, type, at, detail = {}) {
  return { sequence, type, at, payload: { detail }, prev_hash: null, hash: `hash-${sequence}` };
}

async function waitTerminal(request, id, status) {
  await expect(async () => {
    const response = await request.get(`/api/runs/${id}/trace`);
    expect(response.ok()).toBeTruthy();
    expect((await response.json()).terminal?.status).toBe(status);
  }).toPass({ timeout: 60_000, intervals: [1_000] });
}

test("Work 中的纯问候走对话流，不创建任务", async ({ page }) => {
  const taskPosts = [];
  let chatPayload = null;
  page.on("request", request => {
    const path = new URL(request.url()).pathname;
    if (request.method() === "POST" && ["/api/intake", "/api/runs"].includes(path)) taskPosts.push(path);
  });
  // 全套 E2E 的服务器会给每次 chat 都脚本化一次写工具，专供审批用例。
  // 这里隔离消息路由，只提供普通对话 SSE，避免那个全局测试夹具干扰断言。
  await page.route("**/api/chat/send", async route => {
    chatPayload = route.request().postDataJSON();
    const frames = [
      ["start", { thread_id: "greeting-thread", thread_title: "你好", assistant_id: "a-greeting" }],
      ["delta", { text: "你好，我在。有什么想聊的？" }],
      ["done", { message: { id: "a-greeting", text: "你好，我在。有什么想聊的？", usage: {} } }],
    ];
    const body = frames.map(([type, data]) => `data: ${JSON.stringify({ type, ...data })}\n\n`).join("");
    await route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body });
  });
  await page.goto("/");
  await expect(page.locator("body")).toHaveAttribute("data-booted", "1");
  const input = page.locator("#composer-ta");
  await input.fill("你好");
  await expect(page.locator("#send")).toBeEnabled();
  await page.locator("#send").click();

  await expect(page.locator("#main")).toHaveAttribute("data-view", "chat");
  await expect(page.locator(".chat-msg.user").last()).toContainText("你好");
  await expect(page.locator(".chat-msg.assistant").last()).toContainText("你好，我在。有什么想聊的？");
  await expect(page.locator('[data-thread-item="status"]')).toHaveCount(0);
  expect(chatPayload.text).toBe("你好");
  expect(taskPosts).toEqual([]);
});

for (const text of ["早上好呀", "什么是闭包？"]) {
  test(`Work 中的「${text}」先回答且不创建任务`, async ({ page }) => {
    const taskPosts = [];
    let chatPayload = null;
    page.on("request", request => {
      const path = new URL(request.url()).pathname;
      if (request.method() === "POST" && ["/api/intake", "/api/runs"].includes(path)) taskPosts.push(path);
    });
    await page.route("**/api/chat/send", async route => {
      chatPayload = route.request().postDataJSON();
      const frames = [
        ["start", { thread_id: "question-thread", thread_title: text, assistant_id: "a-question" }],
        ["delta", { text: "这是来自真实对话流的回答。" }],
        ["done", { message: { id: "a-question", text: "这是来自真实对话流的回答。", usage: {} } }],
      ];
      const body = frames.map(([type, data]) => `data: ${JSON.stringify({ type, ...data })}\n\n`).join("");
      await route.fulfill({ status: 200, headers: { "Content-Type": "text/event-stream" }, body });
    });
    await page.goto("/");
    await expect(page.locator("body")).toHaveAttribute("data-booted", "1");
    await page.locator("#composer-ta").fill(text);
    await page.locator("#send").click();

    await expect(page.locator("#main")).toHaveAttribute("data-view", "chat");
    await expect(page.locator(".chat-msg.user").last()).toContainText(text);
    await expect(page.locator(".chat-msg.assistant").last()).toContainText("这是来自真实对话流的回答。");
    expect(chatPayload.text).toBe(text);
    expect(taskPosts).toEqual([]);
  });
}

test("运行中的 Work 回合显示真实动作、活动详情和独立计时", async ({ page }) => {
  const id = "visual-live-thread";
  const started = new Date(Date.now() - 4000).toISOString();
  const snapshot = {
    task: { id: `task-${id}`, objective: "运行测试并检查输出", binding: { scenario: "general-research", spec: { mode: "open" } } },
    rounds: [{
      index: 1,
      proposal: { steps: [{ id: "test", tool: "run_command", arguments: { command: "npm", args: ["test"] } }] },
      decision: { allowed: true, code: "PASS", tool: "run_command" },
      observation: null,
      plan: { revision: 1, todos: [
        { id: "read", text: "读取项目配置", tool: "read_file", status: "done" },
        { id: "test", text: "运行前端测试", tool: "run_command", status: "active" },
      ] },
      plan_after: null,
    }],
    events: [
      event(1, "task_created", started),
      event(2, "model_started", started, { round: 1 }),
      event(3, "tool_started", started, { round: 1, tool: "run_command", arguments: { command: "npm", args: ["test"] } }),
    ],
    terminal: null,
  };
  await page.route("**/api/runs", route => {
    if (route.request().method() !== "GET") return route.continue();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify([{
      id, objective: snapshot.task.objective, template: "general-research", status: "running",
    }]) });
  });
  await page.route(`**/api/runs/${id}/stream`, route => route.fulfill({
    status: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    body: `retry: 5000\n\nevent: snapshot\ndata: ${JSON.stringify({ snapshot, pending: null, finished: false })}\n\n`,
  }));

  await page.goto(`/?run=${id}`);
  await expect(page.locator('[data-thread-item="status"]')).toContainText("正在运行命令");
  const tool = page.locator('[data-thread-item="tool"]');
  await expect(tool).toHaveClass(/running/);
  await expect(tool).toHaveAttribute("data-open", "true");
  await expect(tool.locator(".th-tool-id")).toHaveText("run_command");
  await expect(tool.locator(".th-tool-target")).toHaveText("(npm test)");
  await expect(tool.locator(".th-result-text")).toHaveText("…");
  await expect(tool.locator(".th-mark")).toHaveClass(/running/);
  await expect(tool.locator(".th-code")).toHaveText("npm test");
  await expect(page.locator('[data-thread-item="plan"]')).toHaveAttribute("open", "");
  const clock = page.locator("[data-thread-clock]");
  const before = await clock.textContent();
  await expect.poll(() => clock.textContent(), { timeout: 4000 }).not.toBe(before);
  await expect(page.locator('[data-thread-item="answer"]')).toHaveCount(0);
});

test("computer use 的操作证据在线程里显示前后截图与应用上下文", async ({ page }) => {
  const id = "visual-computer-thread";
  const started = new Date(Date.now() - 2000).toISOString();
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==",
    "base64",
  );
  const shot = name => ({
    path: `act-${name}.png`,
    url: `/api/runs/${id}/computer-frame?path=act-${name}.png`,
    bytes: png.length,
    sha256: "sha256-demo",
    width: 800,
    height: 600,
  });
  const snapshot = {
    task: {
      id: `task-${id}`, objective: "在 TextEdit 里写入一句话",
      binding: { scenario: "general-research", spec: { mode: "open", computerUse: true } },
    },
    rounds: [{
      index: 1,
      proposal: { steps: [{ id: "act", tool: "computer_act",
        arguments: { action: "click_element", element_id: "0/1" } }] },
      decision: { allowed: true, code: "PASS", tool: "computer_act" },
      observation: {
        tool: "computer_act",
        arguments: { action: "click_element", element_id: "0/1" },
        result: {
          ok: true, action: "click_element", mode: "desktop", target: "0/1",
          before: shot("before"), after: shot("after"),
          app: { name: "TextEdit", bundle_id: "com.apple.TextEdit" },
          window: { title: "未命名" },
          text: "已保存", duration_ms: 40,
        },
        result_hash: "hash-computer",
      },
      plan: { revision: 1, todos: [] },
      plan_after: null,
    }],
    events: [
      event(1, "task_created", started),
      event(2, "tool_succeeded", started, {
        round: 1, tool: "computer_act",
        arguments: { action: "click_element", element_id: "0/1" },
      }),
    ],
    terminal: null,
  };
  await page.route("**/api/runs", route => {
    if (route.request().method() !== "GET") return route.continue();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify([{
      id, objective: snapshot.task.objective, template: "general-research", status: "running",
    }]) });
  });
  await page.route(`**/api/runs/${id}/stream`, route => route.fulfill({
    status: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    body: `retry: 5000\n\nevent: snapshot\ndata: ${JSON.stringify({ snapshot, pending: null, finished: false })}\n\n`,
  }));
  await page.route("**/computer-frame*", route => route.fulfill({
    status: 200, contentType: "image/png", body: png,
  }));

  await page.goto(`/?run=${id}`);
  const tool = page.locator('[data-thread-item="tool"]').first();
  await expect(tool.locator(".th-tool-id")).toHaveText("computer_act");
  await expect(tool.locator(".th-tool-target")).toHaveText("(click_element · 0/1)");
  await expect(tool.locator(".th-browser-shot")).toHaveCount(2);
  await expect(tool.locator('.th-browser-shot img[alt="电脑截图 · 操作前"]')).toHaveCount(1);
  await expect(tool.locator('.th-browser-shot img[alt="电脑截图 · 操作后"]')).toHaveCount(1);
  await expect(tool.locator(".th-browser-meta")).toContainText("TextEdit");
  await expect(tool).toContainText("已保存");
});

test("同轮批量只读折叠为可展开分组，写动作独立成行", async ({ page }) => {
  const id = "visual-batch-group";
  const started = new Date(Date.now() - 8000).toISOString();
  const obs = (tool, args, result) => ({ tool, arguments: args, result });
  const snapshot = {
    task: { id: `task-${id}`, objective: "读取后修改", binding: { scenario: "general-research", spec: { mode: "open" } } },
    rounds: [{
      index: 1,
      proposal: { steps: [
        { id: "a", tool: "ls_workspace", arguments: {} },
        { id: "b", tool: "read_file", arguments: { path: "stats.py" } },
        { id: "c", tool: "grep_workspace", arguments: { pattern: "mean" } },
        { id: "d", tool: "edit_file", arguments: { path: "stats.py", old_text: "a", new_text: "b" } },
      ] },
      decisions: [{ allowed: true }, { allowed: true }, { allowed: true }, { allowed: true }],
      observations: [
        obs("ls_workspace", {}, { entries: [{ path: "stats.py" }] }),
        obs("read_file", { path: "stats.py" }, { path: "stats.py", bytes: 120 }),
        obs("grep_workspace", { pattern: "mean" }, { matches: [{ path: "stats.py", line: 5 }] }),
        obs("edit_file", { path: "stats.py", old_text: "a", new_text: "b" }, { path: "stats.py", bytes: 121 }),
      ],
      plan: { revision: 1, todos: [] },
      plan_after: null,
    }],
    events: [
      event(1, "task_created", started),
      event(2, "model_started", started, { round: 1 }),
    ],
    terminal: null,
  };
  await page.route("**/api/runs", route => {
    if (route.request().method() !== "GET") return route.continue();
    return route.fulfill({ contentType: "application/json", body: JSON.stringify([{
      id, objective: snapshot.task.objective, template: "general-research", status: "running",
    }]) });
  });
  await page.route(`**/api/runs/${id}/stream`, route => route.fulfill({
    status: 200,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache" },
    body: `retry: 5000\n\nevent: snapshot\ndata: ${JSON.stringify({ snapshot, pending: null, finished: false })}\n\n`,
  }));

  await page.goto(`/?run=${id}`);
  const group = page.locator('[data-thread-item="tool-group"]');
  await expect(group).toHaveCount(1);
  await expect(group.locator(".th-tool-id").first()).toHaveText("只读侦察");
  await expect(group.locator(".th-tool-target").first()).toHaveText("(×3)");
  await expect(group.locator(".th-result-text").first()).toContainText("等 3 项");
  // 默认折叠：成员行不可见；点头部展开后三个 ⏺ 成员出现。
  await expect(group).toHaveAttribute("data-open", "false");
  await expect(group.locator(".th-group-body .th-tool").first()).not.toBeVisible();
  await group.locator(".th-tool-head").first().click();
  await expect(group).toHaveAttribute("data-open", "true");
  await expect(group.locator(".th-group-body .th-tool")).toHaveCount(3);
  await expect(group.locator(".th-group-body .th-tool").first()).toBeVisible();
  // 写动作独立成行，带常驻 diff。
  const edit = page.locator('[data-thread-item="tool"]', { hasText: "edit_file" });
  await expect(edit).toHaveCount(1);
  await expect(edit.locator(".th-inline-diff")).toBeVisible();
});

test("成功与失败终态都可回顾真实过程，窄屏没有横向溢出", async ({ page }) => {
  const success = await page.request.post("/api/runs", {
    data: {
      objective: "Codex 线程：生成并复核交付文件",
      template: "general-research", approval: "off", spec: { mode: "open" },
    },
  });
  expect(success.ok()).toBeTruthy();
  const successId = (await success.json()).id;
  await waitTerminal(page.request, successId, "succeeded");
  await page.goto(`/?run=${successId}`);
  const answer = page.locator('[data-thread-item="answer"]');
  await expect(answer).toBeVisible();
  await expect(answer.locator(".th-artifact-path")).toBeVisible();
  await expect(answer).toContainText("文件状态已复核");
  await expect(page.locator('[data-thread-item="plan"]')).not.toHaveAttribute("open", "");
  const process = page.locator("[data-process-toggle]");
  await expect(process).toHaveAttribute("aria-expanded", "false");
  await expect(process).toContainText(/工作了 .* · \d+ 个步骤/);
  await process.click();
  const tool = page.locator('[data-thread-item="tool"]').first();
  await expect(tool).toBeVisible();
  await tool.locator("[data-thread-toggle]").click();
  await expect(tool).toHaveAttribute("data-open", "true");
  await expect(tool.locator(".th-diff")).toBeVisible();
  await expect(tool.locator(".th-diff-line.add").first()).toBeVisible();
  await page.setViewportSize({ width: 900, height: 900 });
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);

  const failed = await page.request.post("/api/runs", {
    data: {
      objective: "Codex 线程：来源拒绝恢复反馈",
      template: "general-research", approval: "off",
      spec: { sources: ["http://127.0.0.1:9/annual-report"] },
    },
  });
  expect(failed.ok()).toBeTruthy();
  const failedId = (await failed.json()).id;
  await waitTerminal(page.request, failedId, "failed");
  await page.goto(`/?run=${failedId}`);
  const error = page.locator('[data-thread-item="error"]');
  await expect(error).toBeVisible();
  await expect(error).toContainText("FETCH_HOST_DENIED");
  await expect(page.locator('[data-thread-item="answer"]')).toHaveCount(0);
  await expect(page.locator("[data-process-toggle]")).toHaveAttribute("aria-expanded", "false");
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
});
