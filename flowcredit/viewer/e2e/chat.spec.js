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
