import { test, expect } from "@playwright/test";

/* 插件市场：侧栏入口 → 市场页 → 目录/筛选/搜索。安装动作的真实
   mcp.json 读写由后端单测覆盖；这里验证 UI 事实呈现不造假。 */

test("插件市场:侧栏入口打开目录,内置与 MCP 插件状态如实", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator(".side-market").click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "market");
  await expect(page.locator(".mkt-card")).toHaveCount(10);
  await expect(page.locator(".mkt-summary")).toContainText("10 个插件");
  await expect(page.locator(".side-market")).toHaveClass(/active/);
  await expect(page.locator(".composer-wrap")).toBeHidden();

  // 内置能力没有安装按钮；未安装 MCP 插件才有。
  await expect(page.locator('[data-plugin-id="code-search"]')).toContainText("无需安装");
  await expect(page.locator('[data-plugin-id="code-search"] [data-plugin-install]')).toHaveCount(0);
  await expect(page.locator('[data-plugin-id="filesystem"] [data-plugin-install]')).toBeVisible();

  // 分类过滤只换结果列表，不重绘搜索框。
  await page.locator('[data-mkt-search]').fill("fetch");
  await expect(page.locator("#mkt-count")).toContainText("2 / 10");
  await expect(page.locator("#mkt-grid .mkt-card")).toHaveCount(2);
  await page.locator('[data-mkt-cat="文件"]').click();
  await expect(page.locator("#mkt-count")).toContainText("0 / 10");
  await expect(page.locator("#mkt-grid")).toContainText("没有匹配的插件");
});