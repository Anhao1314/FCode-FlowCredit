import { test, expect } from "@playwright/test";

/* 定时任务调度器：UI 建排程 → 已排程可见 → 到点由调度器自动创建运行。
   触发证明用「时间已过 + 当天未触发」的排程（每天 00:00）——调度器
   下一轮（≤20s）必须补触发，这是调度器接入的核心承诺。 */

test("UI 建排程 → 列表可见 → 调度器自动补触发过期任务", async ({ page }) => {
  const marker = `E2E 排程 ${Date.now()}`;
  await page.goto("/");
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: "定时任务" }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "scheduled");

  const ta = page.locator("#sch-objective");
  await ta.fill(marker);
  await page.locator("#sch-frequency").selectOption("每天");
  // 审批设为自动:触发的运行快速终态,不留 300s 审批挂起污染下个用例
  await page.locator("#sch-approval").selectOption("off");
  await page.locator("#sch-time").fill("00:00");
  await page.locator("#sch-save").click();
  await expect(page.locator("#main")).toContainText(marker);
  await expect(page.locator("#main")).toContainText("已排程");

  // 服务端确有这条排程，且“下次触发”描述可读
  const list = await (await page.request.get("/api/schedules")).json();
  const entry = list.schedules.find(s => s.objective === marker);
  expect(entry, "排程已上送服务端").toBeTruthy();
  expect(entry.next).toContain("待触发");

  // 调度器下一轮补触发：运行出现在 /api/runs（离线 runtime，失败也是
  // 一条如实记录——触发本身即调度器的承诺）
  await expect(async () => {
    const runs = await (await page.request.get("/api/runs")).json();
    const fired = runs.find(r => r.objective === marker);
    expect(fired, "调度器到点创建了运行").toBeTruthy();
  }).toPass({ timeout: 60_000, intervals: [5_000] });

  // 页面重进后显示上次触发状态(reload 回默认首页,需重新进入定时任务页)
  await page.reload();
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: "定时任务" }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "scheduled");
  await expect(page.locator("#main")).toContainText(marker);
  await expect(page.locator("#main")).toContainText("上次");
  // 来源可见性:触发的运行在账本亮「排程」chip——操作者能回答"这条运行是谁发起的"
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: /看板 · 账本/ }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "ledger");
  const firedRow = page.locator("[data-led-row]", { hasText: marker });
  await expect(firedRow).toBeVisible({ timeout: 15_000 });
  await expect(firedRow.locator(".led-lineage", { hasText: "排程" })).toBeVisible();

  // 收尾:删掉自己的排程——"每天 00:00"会在次日午夜再次触发,给后续
  // 用例留一个反复创建运行的环境(轮询器持续重绘,点击稳定性崩坏)
  const schedList = await (await page.request.get("/api/schedules")).json();
  const mine = schedList.schedules.find(t => t.objective === marker);
  if (mine) await page.request.post("/api/schedules/delete", { data: { id: mine.id } });
});

test("删除排程后不再出现", async ({ page }) => {
  const marker = `E2E 删除 ${Date.now()}`;
  // API 预置,UI 只走删除路径(保存路径已由上一个用例覆盖)
  const created = await page.request.post("/api/schedules", {
    data: { objective: marker, template: "full", approval: "off", frequency: "每天", time: "23:59" },
  });
  expect(created.ok()).toBeTruthy();
  // 前置:等运行环境安静——runs 轮询器每 2.5s 重绘侧栏,进行中的运行
  // (rounds/cost 持续变化)会让页面持续变动,点击稳定性检查一直失败。
  // 判定 = 连续两轮采样元数据一致(暂停/挂起的运行元数据冻结,不算扰动)。
  await expect(async () => {
    const running = (await (await page.request.get("/api/runs")).json())
      .filter(r => r.status === "running")
      .map(r => [r.id, r.rounds, r.events, r.cost && r.cost.costMicrousd].join(":"));
    const sig = running.join("|");
    if (!sig) return;                                  // 没有运行中的运行 = 安静
    if (globalThis.__schedQuietSig === sig) return;    // 元数据冻结 = 安静
    globalThis.__schedQuietSig = sig;
    throw new Error("runs still progressing");
  }).toPass({ timeout: 150_000, intervals: [3_000] });
  await page.goto("/");
  await page.getByRole("navigation", { name: "功能导航" }).getByRole("button", { name: "定时任务" }).click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "scheduled");
  const row = page.locator(".sch-row", { hasText: marker });
  test.setTimeout(180_000);
  await expect(row).toBeVisible({ timeout: 15_000 });
  await row.locator("[data-sch-del]").click();
  await expect(page.locator("#main")).not.toContainText(marker, { timeout: 15_000 });
  const list = await (await page.request.get("/api/schedules")).json();
  expect(list.schedules.find(t => t.objective === marker)).toBeUndefined();
});
