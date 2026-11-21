import { test, expect } from "@playwright/test";

/* 操作员长期记忆（M1/M2）：/remember 明示沉淀 + 操作员偏好保守自动提炼
   → API 可见 → 设置页管理 → 删除即消失。自动提炼只读操作员消息。 */

test("/remember 写入 → 设置页可见 → 删除消失", async ({ page }) => {
  await page.goto("/");
  // chat 语境（新对话）里输入带参斜杠命令
  await page.getByRole("button", { name: "新对话" }).first().click();
  const ta = page.locator("#composer-ta");
  await ta.fill("/remember E2E 记忆：口径基准是 CNY");
  // 斜杠菜单出现且命中 /remember
  const menu = page.locator("#slash-menu");
  await expect(menu).toBeVisible();
  await expect(menu.locator(".slash-item").first()).toContainText("/remember");
  await ta.press("Enter");
  await expect(ta).toHaveValue("", "命令执行后清空输入框");

  // API 已落库
  const res = await page.request.get("/api/chat/memory");
  expect(res.ok()).toBeTruthy();
  const { memories } = await res.json();
  const entry = memories.find(m => m.text.includes("E2E 记忆：口径基准是 CNY"));
  expect(entry, "记忆条目已写入").toBeTruthy();

  // 设置页「记忆与产物」区可见该条目
  await page.locator("#settings-btn, #settings-mobile").first().click();
  await expect(page.locator("#settings-page")).toBeVisible();
  await page.locator('[data-set-section="memory"]').click();
  const row = page.locator(`[data-mem-row="${entry.id}"]`);
  await expect(row).toBeVisible();
  await expect(row).toContainText("E2E 记忆：口径基准是 CNY");

  // 删除 → 行消失 → API 里也消失
  await row.locator("[data-mem-del]").click();
  await expect(row).toHaveCount(0, { timeout: 10_000 });
  const after = await (await page.request.get("/api/chat/memory")).json();
  expect(after.memories.find(m => m.id === entry.id)).toBeUndefined();
});

test("/remember 空参数给出可见提示,不写库", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新对话" }).first().click();
  const ta = page.locator("#composer-ta");
  await ta.fill("/remember");
  await ta.press("Enter");
  await expect(ta).toHaveValue("");
  const { memories } = await (await page.request.get("/api/chat/memory")).json();
  expect(memories).toHaveLength(0);
});

test("操作员偏好会被保守提炼为 auto 记忆", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "新对话" }).first().click();
  const ta = page.locator("#composer-ta");
  await ta.fill("以后都用 pytest，不要再用 unittest");
  await ta.press("Enter");

  let entry;
  await expect.poll(async () => {
    const { memories } = await (await page.request.get("/api/chat/memory")).json();
    entry = memories.find(m => m.auto && m.text.includes("pytest"));
    return Boolean(entry);
  }).toBe(true);
  expect(entry.text).toContain("以后都用 pytest");

  await page.request.post("/api/chat/memory/delete", { data: { id: entry.id } });
});
