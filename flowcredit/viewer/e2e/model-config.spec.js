import { test, expect } from "@playwright/test";

/* Slice C：真实 HTTP 的模型配置闭环。
   1. 设置页：自定义端点新增 → 共享目录即时出现 → 两步删除；
   2. 排程：保存时把 model/effort 固定进快照，列表与 API 回读一致。
   E2E server 的 config 目录是隔离的（playwright.config.js），
   这里做真实增删不会触碰操作员本机配置。 */

const PROVIDER = "e2eep";

test("连接测试:mock 成功,未配置端点返回结构化失败", async ({ request }) => {
  const ok = await request.post("/api/settings/model-config/test", {
    data: { provider: "mock" },
  });
  expect(ok.ok()).toBeTruthy();
  const okBody = await ok.json();
  expect(okBody.ok).toBe(true);

  const bad = await request.post("/api/settings/model-config/test", {
    data: { provider: "does-not-exist" },
  });
  expect(bad.ok()).toBeTruthy();
  const badBody = await bad.json();
  expect(badBody.ok).toBe(false);
  expect(badBody.code).toBeTruthy();
});

async function seedProvider(page) {
  const res = await page.request.post("/api/settings/model-config", {
    data: {
      provider: PROVIDER,
      values: {
        label: "E2E 端点",
        base_url: "http://127.0.0.1:9/v1",
        model: "e2e-model",
        input_price_microusd_per_mtok: "0",
        output_price_microusd_per_mtok: "0",
        price_as_of: "not-applicable",
        price_source: "operator-declared",
        supports_effort: "1",
      },
    },
  });
  expect(res.ok()).toBeTruthy();
}

test("设置页:自定义端点新增→目录出现→两步删除", async ({ page }) => {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator("#user-btn").click();
  await page.locator("#foot-menu").getByText("设置", { exact: true }).click();
  await expect(page.locator('#chatcfg-cards [data-chatcfg-card="kimi"]')).toBeVisible({ timeout: 15_000 });

  // 新增：表单只填必填项（价格审计字段已有默认值 0 / not-applicable / operator-declared）
  await page.locator("#chatcfg-add-key").fill(PROVIDER);
  await page.locator("#chatcfg-add-label").fill("E2E 端点");
  await page.locator("#chatcfg-add-base_url").fill("http://127.0.0.1:9/v1");
  await page.locator("#chatcfg-add-model").fill("e2e-model");
  await page.locator("[data-chatcfg-add]").click();

  const card = page.locator(`[data-chatcfg-card="${PROVIDER}"]`);
  await expect(card).toBeVisible();
  await expect(card).toContainText("E2E 端点");
  // 全局默认单选从共享目录动态生成
  await expect(page.locator(`#chatcfg-default [data-chatcfg-default="${PROVIDER}"]`)).toHaveCount(1);

  // 两步删除：第一次只武装，第二次真删
  const del = card.locator("[data-chatcfg-delete]");
  await del.click();
  await expect(del).toHaveText("确认删除？");
  await del.click();
  await expect(card).toHaveCount(0);
  await expect(page.locator(`#chatcfg-default [data-chatcfg-default="${PROVIDER}"]`)).toHaveCount(0);
});

test("排程:保存时固定 model/effort,列表与 API 回读一致", async ({ page }) => {
  await seedProvider(page);
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');

  // 对话内 /定时：斜杠菜单项才是执行入口（发送按钮会当普通消息）。
  // 落点两种：无线程 → 直达定时任务页；有线程 → 工作区卡再跳。
  await page.getByRole("button", { name: "新对话" }).first().click();
  await page.locator("#composer-ta").fill("/定时");
  const slashItem = page.locator('#slash-menu .slash-item[data-slash="定时"]');
  await expect(slashItem).toBeVisible();
  await slashItem.click();
  const jump = page.getByRole("button", { name: /定时任务页/ });
  if (await jump.count()) await jump.first().click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "scheduled", { timeout: 15_000 });

  // 共享目录包含刚种下的端点；选中它并给一个思考强度
  await expect(page.locator('#sch-model option[value="e2eep"]')).toHaveCount(1);
  await expect(page.locator("#sch-effort-label")).toBeHidden();
  await page.locator("#sch-model").selectOption("e2eep");
  await expect(page.locator("#sch-effort-label")).toBeVisible();

  await page.locator("#sch-objective").fill("E2E 模型快照排程");
  await page.locator("#sch-effort").selectOption("high");
  await page.locator("#sch-save").click();

  // 列表行显示已固定的模型；API 回读同一份事实
  await expect(page.locator(".sch-list")).toContainText("模型 e2eep · 高");
  const data = await page.request.get("/api/schedules").then(r => r.json());
  const mine = (data.schedules || []).find(s => s.objective === "E2E 模型快照排程");
  expect(mine).toBeTruthy();
  expect(mine.model).toBe("e2eep");
  expect(mine.effort).toBe("high");

  // 清理（净零）：删排程 + 删端点
  await page.request.post("/api/schedules/delete", { data: { id: mine.id } });
  await page.request.post("/api/settings/model-config", {
    data: { provider: PROVIDER, delete_provider: true },
  });
});
