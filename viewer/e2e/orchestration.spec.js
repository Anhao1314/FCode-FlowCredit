import { test, expect } from "@playwright/test";

/* 子代理编排 E2E：确定性脚本缝（FCODE_MOCK_SUBAGENTS）驱动完整链路——
   父运行提议 spawn → 审批卡批准 → 两个只读子代理并行创建 → join →
   摘要回父 → 父写产物终态；账本亮父子徽章。 */

test("spawn 审批 → 子代理并行 → 摘要回父 → 账本徽章", async ({ page }) => {
  const marker = `E2E 编排 ${Date.now()}`;
  // 父运行经 API 预置:spec 键 mock_spawn_subagents 是规划器的确定性
  // 编排缝(离线 mock 跟计划走);审批 consequential → spawn 过审批卡
  const created = await page.request.post("/api/runs", {
    data: {
      objective: `${marker}：分两个子问题并行调查`,
      template: "general-research", approval: "consequential",
      spec: {
        mode: "open",
        mock_spawn_subagents: ["子问题A：列出工作区文件并总结", "子问题B：读取任一 md 文件并概括"],
      },
    },
  });
  expect(created.ok()).toBeTruthy();
  const parent = await created.json();

  await page.goto("/");
  // 从账本打开父运行(顺带覆盖 openLive 回放路径)
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: /看板 · 账本/ }).click();
  const parentRow = page.locator("[data-led-row]", { hasText: marker });
  await expect(parentRow).toBeVisible({ timeout: 15_000 });
  await parentRow.click();
  const thread = page.locator("#thread");
  await expect(thread).toBeVisible({ timeout: 20_000 });

  // spawn_subagents 的审批卡出现(写 effect → consequential 过审批)
  const decision = thread.locator('[data-thread-item="approval"] [data-decision="allow"]');
  await expect(decision).toBeVisible({ timeout: 30_000 });
  await decision.click();

  // join 后:spawn 工具卡完成,摘要回传(子运行 id 出现在结果里)
  const spawnCard = thread.locator('[data-thread-item="tool"]', { hasText: "spawn_subagents" }).first();
  await expect(spawnCard).toContainText("完成", { timeout: 120_000 });
  await expect(spawnCard).toContainText("run-", { timeout: 5_000 });

  // 子运行真实存在且为内部子代理模板
  const list = await (await page.request.get("/api/runs")).json();
  const children = list.filter(r => (r.spec || {}).parent_run_id === parent.id);
  expect(children.length, "两个子代理运行已创建").toBe(2);
  for (const child of children) {
    expect(child.template).toBe("general-subagent");
  }

  // 父运行终态(子代理只读调查后,汇总写入 research.md)
  await expect(async () => {
    const runs = await (await page.request.get("/api/runs")).json();
    const me = runs.find(r => r.id === parent.id);
    expect(me?.status).toBe("succeeded");
  }).toPass({ timeout: 120_000, intervals: [3_000] });

  // 账本父行亮扇出徽章
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: /看板 · 账本/ }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "ledger");
  await expect(parentRow.locator(".led-lineage")).toHaveText(/子代理 ×\d/);
});

test("子代理行在账本亮子代理徽章", async ({ page }) => {
  // 直接预置一条带 parent_run_id 的运行(徽章渲染不依赖编排全程)
  const marker = `E2E 子行 ${Date.now()}`;
  await page.goto("/");
  await page.request.post("/api/runs", {
    data: {
      objective: marker, template: "general-subagent", approval: "off",
      spec: { mode: "open", parent_run_id: "run-parent-e2e" },
    },
  });
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: /看板 · 账本/ }).click();
  const row = page.locator("[data-led-row]", { hasText: marker });
  await expect(row).toBeVisible({ timeout: 15_000 });
  await expect(row.locator(".led-lineage")).toHaveText("子代理");
});
