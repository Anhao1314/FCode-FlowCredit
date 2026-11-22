import { test, expect } from "@playwright/test";

/* R1: 从对话命令卡进入页面。必须等 boot 完成标记——boot 的 newChat()
   会把视图打回首页并清空输入框，早于 boot 输入的斜杠命令会被吞掉。 */
async function slashCommand(page, cmd) {
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.locator("#composer-ta").fill(cmd);
  await page.keyboard.press("Enter");
}


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
  // R1: 账本从对话命令卡进入
  await slashCommand(page, "/账本");
  await expect(page.locator("#main")).toHaveAttribute("data-view", "ledger");
  await page.getByText("E2E 铺垫：调研审计规则").first().click();
  await expect(page.locator("#thread")).toBeVisible({ timeout: 20_000 });
  const errorItem = page.locator('#thread [data-thread-item="error"]');
  await expect(errorItem).toBeVisible({ timeout: 20_000 });
  await expect(errorItem).toContainText("FETCH_HOST_DENIED");
  // 失败运行不得伪造成功答案
  await expect(page.locator('#thread [data-thread-item="answer"]')).toHaveCount(0);

  // 2. ⌘K → 新委托 → 需求确认(通用模板一条消息即就绪)
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

  // 3. intake 内直接打开 + 面板加来源(回归断言:+ 按钮在 intake 可达——
  //    曾被 CSS 与 paintChatChrome 双重隐藏,调研委托因此永远过不了守卫)
  await expect(page.locator("#composer-open")).toBeVisible();
  await page.locator("#composer-open").click();                 // 添加内容（第一层）
  await page.locator('[data-work-add="sources"]').click();      // 资料与来源（详情层，已展开）
  await page.locator("[data-attach-source-input]").fill("http://127.0.0.1:9/probe");
  await page.locator("[data-attach-source-add]").click();
  await expect(page.locator(".attach-source .src-url").first()).toHaveText(/127\.0\.0\.1/);
  await page.locator("[data-attach-back]").click();             // 回到添加内容
  await page.locator("#composer-open").click();                 // 收起菜单
  await page.waitForTimeout(250);                               // 收起动画 150ms
  await start.click();

  // 4. 运行接流:线程投影出目标条目与状态条目(live 断言到此为止——
  //    终态语义已由回放路径确定性覆盖)
  const thread = page.locator("#thread");
  await expect(thread).toBeVisible({ timeout: 20_000 });
  await expect(thread.locator('[data-thread-key="user:objective"]')).toContainText("调研 FCode 的审计规则");
  await expect(thread.locator('[data-thread-item="status"]')).toBeVisible();

  // 5. 账本里出现这条运行(失败也是一条如实记录)
  // R1: 账本从对话命令卡进入
  await slashCommand(page, "/账本");
  await expect(page.locator("#main")).toHaveAttribute("data-view", "ledger");
  await expect(page.locator("#main")).toContainText("调研 FCode 的审计规则");
});

test("Work direct：choices 确认后自动开跑，双击也只创建一次运行", async ({ page }) => {
  const objective = `执行任务 E2E direct choices ${Date.now()}`;
  let intakeCalls = 0;
  let runPosts = 0;
  page.on("request", request => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/runs") {
      runPosts += 1;
    }
  });
  await page.route("**/api/intake", async route => {
    intakeCalls += 1;
    if (intakeCalls === 1) {
      await route.fulfill({
        contentType: "application/json",
        body: JSON.stringify({
          reply: "还需要确认交付方式。", thought: "", spec: { mode: "open" }, task: null,
          choices: [
            { field: "delivery", value: "yes", label: "保留", description: "按原目标继续" },
            { field: "delivery", value: "no", label: "修改", description: "返回修改" },
          ],
        }),
      });
      return;
    }
    // 给双击留出确定性的在途窗口；第二次 click 必须被 thinking 守卫拒绝。
    await new Promise(resolve => setTimeout(resolve, 180));
    await route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({
        reply: "已就绪。", thought: "", spec: { mode: "open" }, choices: [],
        task: { objective, template: "general-research", approval: "off", spec: { mode: "open" } },
      }),
    });
  });

  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
  await page.locator("#composer-ta").fill(objective);
  await page.locator("#composer-ta").press("Enter");
  const choice = page.locator(".intake-choice", { hasText: "保留" }).first();
  await expect(choice).toBeVisible();
  await choice.dblclick();

  await expect(page.locator("#thread")).toBeVisible({ timeout: 20_000 });
  await expect(page.locator('[data-thread-key="user:objective"]')).toContainText(objective);
  await expect.poll(() => runPosts).toBe(1);
  expect(intakeCalls).toBe(2, "双击 choices 不得多发一次 /api/intake");

  await expect.poll(async () => {
    const response = await page.request.get("/api/runs");
    expect(response.ok()).toBeTruthy();
    const runs = await response.json();
    const matches = runs.filter(run => run.objective === objective);
    expect(matches).toHaveLength(1);
    return matches[0].status;
  }, { timeout: 60_000 }).toBe("succeeded");
  expect(runPosts).toBe(1);
  expect(intakeCalls).toBe(2);

  await page.reload();
  await page.waitForSelector('body[data-booted="1"]');
  expect(runPosts).toBe(1, "刷新不得重放建 run");
  const response = await page.request.get("/api/runs");
  const runs = await response.json();
  expect(runs.filter(run => run.objective === objective)).toHaveLength(1);
});
