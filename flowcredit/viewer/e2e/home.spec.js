import { test, expect } from "@playwright/test";

/* 首页壳层：渲染、模型徽章、⌘K 命令面板的开合与过滤。 */

test("Work 落地渲染:任务舞台 + 模型徽章 + 侧栏导航", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await expect(page.locator("#main")).toHaveAttribute("data-view", "empty");
  await expect(page.locator("#chain-badge")).toContainText("无进行中的运行");
  await expect(page.locator(".moonshot-title")).toContainText("今天想");
  await expect(page.locator("#chat-projects")).toContainText("项目");
  // 模型徽章来自 paintModelBadge(新 composer-panel 模块):全新档案的
  // 默认模型可能落在云端条目或离线 mock,断言"有可读徽章"而非具体值
  await expect(page.locator("#model-label")).toHaveText(/kimi|deepseek|offline-mock|未连接/);
  // 功能导航已撤除:两区对话面由 Work/Governance 区切换器直达
  await expect(page.locator('#zone-switch [data-zone="work"]')).toBeVisible();
  await expect(page.locator('#zone-switch [data-zone="gov"]')).toBeVisible();
});

test("首页快捷建议只填入 composer,不自动提交", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  const chip = page.locator("[data-stage-prompt]").first();
  await expect(chip).toBeVisible();
  await chip.click();
  await expect(page.locator("#composer-ta")).toHaveValue(/审计这个目录/);
  await expect(page.locator("#main")).toHaveAttribute("data-view", "empty");
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
  // ArrowDown 高亮后 Enter 执行 → 进入 intake 面（通用委托是任务确认，不是聊天）
  await page.locator("#palette-input").press("ArrowDown");
  await page.locator("#palette-input").press("Enter");
  await expect(page.locator("#app")).toHaveClass(/intake-mode/);
  await expect(page.locator(".intake-title")).toContainText("还差一点信息");
  await expect(page.locator(".intake-msg")).toHaveCount(0);
});

test("Escape 关闭命令面板", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator("#side-search").focus();
  await page.keyboard.press("ControlOrMeta+k");
  await expect(page.locator("#palette")).toBeVisible();
  const input = page.locator("#palette-input");
  await expect(input).toBeFocused();
  await expect(input).toHaveAttribute("aria-expanded", "true");
  await expect(input).toHaveAttribute("aria-activedescendant", "palette-option-0");
  await expect(page.locator("#palette-option-0")).toHaveAttribute("aria-selected", "true");
  await input.press("Tab");
  await expect(input).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.locator("#palette")).toBeHidden();
  await expect(input).toHaveAttribute("aria-expanded", "false");
  await expect(page.locator("#side-search")).toBeFocused();
});

test("窄屏事件面板管理焦点，输入态 Escape 也能关闭", async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 900 });
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  const rail = page.locator("#task-rail");
  await page.locator("#rail-toggle").click();
  await expect(rail).toHaveAttribute("aria-modal", "true");
  await expect(page.locator(".rail-close")).toBeFocused();
  await page.locator("#composer-ta").focus();
  await page.keyboard.press("Escape");
  await expect(rail).toHaveAttribute("aria-hidden", "true");
  await expect(page.locator("#rail-toggle")).toBeFocused();
});

test("桌面事件面板保持非模态", async ({ page }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator("#rail-toggle").click();
  await expect(page.locator("#task-rail")).not.toHaveAttribute("aria-modal", "true");
  await expect(page.locator("#task-rail")).toHaveAttribute("aria-hidden", "false");
});
