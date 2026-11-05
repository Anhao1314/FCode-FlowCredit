import { test, expect } from "@playwright/test";

/* 委托全生命周期：失败回放(诚实终态) → + 面板配置来源 → 需求确认 →
   创建运行 → 接流投影 → 账本记录。
   确定性策略：终态断言走回放路径——API 预置一条必然失败的运行
   (loopback 来源,SSRF 守卫确定性拒绝,~12s 终态),live 断言只到
   「线程投影出目标与状态」。clarifier/runtime 全程离线。 */

test("诚实失败回放、+面板、需求确认、接流与账本", async ({ page }) => {
  // 0. 铺垫一条必然失败的运行:loopback 来源被 SSRF 守卫确定性拒绝
  const created = await page.request.post("/api/runs", {
    data: {
      objective: "E2E 铺垫：调研审计规则", template: "general-research",
      approval: "consequential",
      spec: { sources: ["http://127.0.0.1:9/annual-report"] },
    },
  });
  expect(created.ok()).toBeTruthy();
  const seed = await created.json();
  // 等 API 轮询到终态(失败)再打开页面——回放渲染才有确定性输入
  await expect(async () => {
    const res = await page.request.get(`/api/runs/${seed.id}/trace`);
    expect(res.ok()).toBeTruthy();
    const trace = await res.json();
    expect(trace.terminal?.status).toBe("failed");
  }).toPass({ timeout: 60_000, intervals: [2_000] });

  await page.goto("/");
  // 1. 账本 → 打开失败运行 → 回放如实渲染错误条目
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: /看板 · 账本/ }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "ledger");
  await page.getByText("E2E 铺垫：调研审计规则").first().click();
  await expect(page.locator("#thread")).toBeVisible({ timeout: 20_000 });
  const errorItem = page.locator('#thread [data-thread-item="error"]');
  await expect(errorItem).toBeVisible({ timeout: 20_000 });
  await expect(errorItem).toContainText("FETCH_HOST_DENIED");
  // 失败运行不得伪造成功答案
  await expect(page.locator('#thread [data-thread-item="answer"]')).toHaveCount(0);

  // 2. 运行视图的 + 面板加来源(状态跨视图保留;+ 面板只在运行视图可达)
  await page.locator("#composer-open").click();
  await page.locator("[data-attach-sum='sources']").click();
  await page.locator("[data-attach-source-input]").fill("http://127.0.0.1:9/probe");
  await page.locator("[data-attach-source-add]").click();
  await expect(page.locator(".attach-source .src-url").first()).toHaveText(/127\.0\.0\.1/);

  // 3. ⌘K → 新委托 → 需求确认(通用模板一条消息即就绪)
  await page.keyboard.press("ControlOrMeta+k");
  await page.locator("#palette-input").fill("新委托");
  await page.locator("#palette-input").press("Enter");
  await expect(page.locator("#app")).toHaveClass(/intake-mode/);
  const ta = page.locator("#composer-ta");
  await ta.fill("调研 FCode 的审计规则并整理成 research.md");
  await ta.press("Enter");

  const start = page.locator("[data-intake-start]");
  for (let round = 0; round < 5 && !(await start.isEnabled()); round += 1) {
    const choice = page.locator(".intake-choice").first();
    if (!(await choice.count())) break;
    await choice.click();
    await page.waitForTimeout(300);
  }
  await expect(start).toBeEnabled({ timeout: 20_000 });
  await start.click();

  // 4. 运行接流:线程投影出目标条目与状态条目(live 断言到此为止——
  //    终态语义已由回放路径确定性覆盖)
  const thread = page.locator("#thread");
  await expect(thread).toBeVisible({ timeout: 20_000 });
  await expect(thread.locator('[data-thread-key="user:objective"]')).toContainText("调研 FCode 的审计规则");
  await expect(thread.locator('[data-thread-item="status"]')).toBeVisible();

  // 5. 账本里出现这条运行(失败也是一条如实记录)
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: /看板 · 账本/ }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "ledger");
  await expect(page.locator("#main")).toContainText("调研 FCode 的审计规则");
});
