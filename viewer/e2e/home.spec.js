import { test, expect } from "@playwright/test";

/* 首页壳层：渲染、模型徽章、⌘K 命令面板的开合与过滤。 */

test("Work 落地渲染:任务舞台 + 模型徽章 + 侧栏导航", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await expect(page.locator("#main")).toHaveAttribute("data-view", "empty");
  await expect(page.locator(".moonshot-title")).toContainText("Build your");
  await expect(page.locator("#chat-projects")).toContainText("Projects");
  // 模型徽章来自 paintModelBadge(新 composer-panel 模块):全新档案的
  // 默认模型可能落在云端条目或离线 mock,断言"有可读徽章"而非具体值
  await expect(page.locator("#model-label")).toHaveText(/kimi|deepseek|offline-mock|未连接/);
  // 功能导航已撤除:两区对话面由 Work/Governance 区切换器直达
  await expect(page.locator('#zone-switch [data-zone="work"]')).toBeVisible();
  await expect(page.locator('#zone-switch [data-zone="gov"]')).toBeVisible();
});

test("⌘K 命令面板:开合、过滤、键盘导航", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("ControlOrMeta+k");
  const overlay = page.locator("#palette");
  await expect(overlay).toBeVisible();
  // 空查询给完整动作清单
  await expect(page.locator("#palette-list .pal-item").first()).toBeVisible();
  await expect(page.locator("#palette-list .pal-group").first()).toHaveText("动作");
  // 过滤"委托"只剩新委托
  await page.locator("#palette-input").fill("委托");
  await expect(page.locator("#palette-list .pal-item")).toHaveCount(1);
  await expect(page.locator("#palette-list .pal-item .pal-label")).toHaveText("新委托 · 需求选择");
  // ArrowDown 高亮后 Enter 执行 → 进入 intake 面
  await page.locator("#palette-input").press("ArrowDown");
  await page.locator("#palette-input").press("Enter");
  await expect(page.locator("#app")).toHaveClass(/intake-mode/);
  await expect(page.locator(".intake-title")).toContainText("说清楚");
});

test("Escape 关闭命令面板", async ({ page }) => {
  await page.goto("/");
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.locator("#palette")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.locator("#palette")).toBeHidden();
});
