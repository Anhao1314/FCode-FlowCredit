import { test, expect } from "@playwright/test";

/* Slice P1：主代理规划子代理。
   E2E 环境无模型（mock，fail-closed），所以：
   1. 规划器不可用 → 400 + 结构化错误（不伪造计划）；
   2. 手动 steps 的计划 → 团队页可见 → 拒绝（零派发）。
   真模型规划与批准派发走 Python 单测（fake bots）+ 真模型 smoke。 */

const STEPS = [
  { agent_type: "auditor", objective: "审计目标", deps: [], done_when: "bundle 生成" },
  { agent_type: "verifier", objective: "复核签名链", deps: [1], done_when: "链通过" },
];

test("主代理规划:无模型时 fail-closed 返回结构化错误", async ({ request }) => {
  const res = await request.post("/api/gov/plans", {
    data: { objective: "审计一个不存在模型也能跑的目标" },
  });
  expect(res.status()).toBe(400);
  const body = await res.json();
  expect(body.error).toContain("规划");
});

test("主代理计划:手动 steps→团队页可见→拒绝零派发", async ({ page, request }) => {
  const res = await request.post("/api/gov/plans", {
    data: {
      objective: "审计 fixtures/demo-target 并复核",
      target: "fixtures/demo-target",
      steps: STEPS,
    },
  });
  expect(res.status()).toBe(201);
  const { plan } = await res.json();
  expect(plan.status).toBe("proposed");
  expect(plan.steps).toHaveLength(2);

  const list = await request.get("/api/gov/plans").then(r => r.json());
  expect((list.plans || []).some(item => item.id === plan.id)).toBe(true);

  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator('[data-open="gov"]').first().click();
  const teamEntry = page.getByRole("button", { name: /团队\s*→/ }).first();
  if (await teamEntry.count()) await teamEntry.click();
  const card = page.locator(`[data-gov-plan-card="${plan.id}"]`);
  await expect(card).toBeVisible({ timeout: 15_000 });
  await expect(card).toContainText("待批准");
  await expect(card).toContainText("审计员");
  await expect(card).toContainText("依赖 #1");

  // 拒绝：零派发，卡片进入终态
  await card.locator("[data-gov-plan-reject]").click();
  await expect(card).toContainText("已拒绝");
  await expect(card.locator("[data-gov-plan-approve]")).toHaveCount(0);

  // 详情 API 回读同一份事实
  const detail = await request.get(`/api/gov/plans/${plan.id}`).then(r => r.json());
  expect(detail.status).toBe("rejected");
});
