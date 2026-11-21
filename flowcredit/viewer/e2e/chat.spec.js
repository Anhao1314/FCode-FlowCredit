import { test, expect } from "@playwright/test";

/* 对话工具循环：脚本化 write_file（FCODE_CHAT_MOCK_TOOLS）在离线环境
   确定性产生一张带真实 diff 的写入审批卡——允许/拒绝两条路都要走通。 */

async function newChat(page) {
  await page.goto("/");
  // 新对话落在 chat 语境的空态舞台(mode 仍是 empty);发出首条消息后才进 chat 视图
  await page.getByRole("button", { name: "新对话" }).first().click();
  await expect(page.locator("#composer-ta")).toHaveAttribute("placeholder", "给 FCode 发消息…");
}

test("写入审批:卡片含 diff 与动词化标题,允许后运行继续", async ({ page }) => {
  await newChat(page);
  const ta = page.locator("#composer-ta");
  await ta.fill("帮我在工作区写一个 e2e-note.txt");
  await ta.press("Enter");

  // 审批卡:写入影响区带真实 diff(主机执行前算好),标题动词化
  const approval = page.locator(".chat-tool-approval");
  await expect(approval).toBeVisible({ timeout: 20_000 });
  await expect(approval).toContainText("关键审批 · 这个调用需要你确认");
  await expect(approval).toContainText("+written by e2e");
  await expect(approval).toContainText("新建文件");
  await expect(approval).toContainText("e2e-note.txt");

  // 允许 → 工具卡转完成 → 跟进文本回答
  await approval.locator("[data-tool-allow]").click();
  await expect(page.locator(".chat-tool").first()).toContainText("完成", { timeout: 20_000 });
  await expect(page.locator(".chat-msg").last()).toContainText("离线 Mock 回复", { timeout: 20_000 });

  // 聊天线程的上下文容量圆圈：读 thread budget，而不是运行 trace
  await expect(page.locator("#ctx-ring")).toBeVisible();
  await expect(page.locator("#ctx-ring")).toHaveAttribute("title", /上下文容量 \d+%/);
  await expect(page.locator("#ctx-ring .ctx-pct")).toHaveCount(0);
});

test("拒绝审批:不执行写入,流程给出显式结果", async ({ page }) => {
  await newChat(page);
  const ta = page.locator("#composer-ta");
  await ta.fill("再写一次");
  await ta.press("Enter");
  const approval = page.locator(".chat-tool-approval");
  await expect(approval).toBeVisible({ timeout: 20_000 });
  await approval.locator("[data-tool-deny]").click();
  // 工具卡进入失败态,线程里不再有等待中的审批
  await expect(page.locator(".chat-tool").first()).toContainText(/失败|已拒绝/, { timeout: 20_000 });
  await expect(page.locator(".chat-tool-approval")).toHaveCount(0);
});

test("历史对话:权限菜单可键盘切换,下一条消息携带新档位", async ({ page }) => {
  const runtimeErrors = [];
  const failedResponses = [];
  page.on("pageerror", error => runtimeErrors.push(error.message));
  page.on("response", response => {
    if (response.status() >= 400) failedResponses.push(`${response.status()} ${response.url()}`);
  });
  await newChat(page);
  const permission = page.locator("#approval-btn");
  await expect(permission).toBeVisible();
  await expect(permission).toContainText("关键审批");

  const ta = page.locator("#composer-ta");
  await ta.fill("建立一段需要重新打开的对话");
  await ta.press("Enter");
  const approval = page.locator(".chat-tool-approval");
  await expect(approval).toBeVisible({ timeout: 20_000 });
  await approval.locator("[data-tool-allow]").click();
  await expect(page.locator(".chat-msg").last()).toContainText("离线 Mock 回复", { timeout: 20_000 });

  const activeThread = page.locator("[data-chat-id].active").first();
  await expect(activeThread).toBeVisible();
  const threadId = await activeThread.getAttribute("data-chat-id");
  expect(threadId).toBeTruthy();

  // 真正离开并重开历史线程，不能只验证新对话的初始舞台。
  await page.reload();
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator(`[data-chat-id="${threadId}"]`).first().click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "chat");
  await expect(permission).toBeVisible();

  await permission.click();
  await expect(permission).toHaveAttribute("aria-expanded", "true");
  await expect(page.locator('[data-approval-mode="consequential"]')).toBeFocused();
  await page.keyboard.press("End");
  await expect(page.locator('[data-approval-mode="off"]')).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(permission).toContainText("自动");
  await expect(permission).toHaveAttribute("aria-expanded", "false");
  await expect(permission).toBeFocused();
  await expect(page.locator("#toast")).toContainText("下一条消息生效");
  await expect.poll(() => page.evaluate(() => localStorage.getItem("fa.approval"))).toBe("off");

  const sent = page.waitForRequest(request => request.url().endsWith("/api/chat/send")
    && request.method() === "POST");
  await ta.fill("重开后按自动权限继续");
  await ta.press("Enter");
  expect((await sent).postDataJSON().approval).toBe("off");
  await expect(page.locator(".chat-msg").last()).toContainText("离线 Mock 回复", { timeout: 20_000 });
  await expect(page.locator(".chat-tool").last()).toContainText("完成");
  await expect(page.locator(".chat-tool-approval")).toHaveCount(0);

  await permission.click();
  await expect(page.locator('[data-approval-mode="off"]')).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Escape");
  await expect(page.locator("#approval-menu")).toBeHidden();
  await expect(permission).toBeFocused();

  await expect(page.locator("#toast")).toBeHidden();
  await permission.click();
  await expect(page.locator("#approval-menu")).toBeVisible();
  await page.setViewportSize({ width: 375, height: 800 });
  await expect(permission).toBeVisible();
  await expect(page.locator("#approval-menu")).toBeVisible();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  const menuBounds = await page.locator("#approval-menu").boundingBox();
  expect(menuBounds.x).toBeGreaterThanOrEqual(0);
  expect(menuBounds.x + menuBounds.width).toBeLessThanOrEqual(375);
  expect(runtimeErrors).toEqual([]);
  expect(failedResponses).toEqual([]);
});
