import { test, expect } from "@playwright/test";

/* R1: 从对话命令卡进入页面。必须等 boot 完成标记——boot 的 newChat()
   会把视图打回首页并清空输入框，早于 boot 输入的斜杠命令会被吞掉。 */
async function slashCommand(page, cmd) {
  await page.waitForSelector('body[data-booted="1"]', { state: "attached" });
  await page.locator("#composer-ta").fill(cmd);
  await page.keyboard.press("Enter");
}


/* 功能面页面：判断记录、定时任务、设置页开合。
   R1 收敛后四页不占侧栏导航——从对话命令卡进入(这也是用户真实路径):
   /判断 → 治理台判断卡 → 判断记录;  /定时 → 定时任务页(无线程时回落)。 */

test("判断记录页渲染(从治理台判断卡进入,空态可读)", async ({ page }) => {
  await page.goto("/");
  // 治理命令从对话唤起:切治理台 + 落判断卡
  await slashCommand(page, "/判断");
  await expect(page.locator("#main")).toHaveAttribute("data-view", "gov");
  const card = page.locator("#gov-dialog-stream .gov-judgments");
  await expect(card).toBeVisible({ timeout: 10_000 });
  await expect(page.locator(".composer-wrap")).toBeVisible();
  // 卡内锚点进判断记录页
  await card.locator('[data-open="reviews"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "reviews");
  await expect(page.locator("#main")).toContainText("判断记录");
});

test("定时任务页:空态与服务端同步语义", async ({ page }) => {
  await page.goto("/");
  // 无线程时 /定时 回落定时任务页(卡片是对话入口,页面是详情面)
  await slashCommand(page, "/定时");
  await expect(page.locator("#main")).toHaveAttribute("data-view", "scheduled");
  await expect(page.locator("#main")).toContainText("还没有排程");
  await expect(page.locator("#main")).toContainText("到点自动创建运行", "调度器已接入的诚实标注");
});

test("设置页:开合与审批档位渲染", async ({ page }) => {
  await page.goto("/");
  await page.locator("#settings-btn, #settings-mobile").first().click();
  await expect(page.locator("#settings-page")).toBeVisible();
  await expect(page.locator("#settings-modes .set-mode")).toHaveCount(3);
  await page.locator("#settings-back").click();
  await expect(page.locator("#settings-page")).toBeHidden();
});
