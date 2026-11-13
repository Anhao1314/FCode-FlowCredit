import { test, expect } from "@playwright/test";

/* 审计台（发现账本）：统一壳 surface 的核心链路——包列表、发现渲染、
   签名链徽章、操作员处置落链、深链直达。审计数据来自 E2E 隔离副本
   （globalSetup 复制 work/audit），处置写的是副本，不碰本机账本。 */

/* 审计台入口已收进治理区侧栏：先切到 Governance，再点「审计台」。 */
async function openAudit(page) {
  await page.locator('.zone-btn[data-zone="gov"]').click();
  await page.locator('#gov-surface-nav [data-open="audit"]').click();
  await expect(page.locator("#main")).toHaveAttribute("data-view", "audit");
  await expect(page.locator('#gov-surface-nav [data-open="audit"]'))
    .toHaveAttribute("aria-selected", "true");
}

test("包列表 → 发现账本 → 签名链徽章", async ({ page }) => {
  await page.goto("/");
  // 审计台入口已收进治理区侧栏：Governance → 审计台
  await openAudit(page);
  await expect(page.locator("#at-bundles .at-bitem").first()).toBeVisible({ timeout: 15_000 });

  await page.locator("#at-bundles .at-bitem").first().click();
  const main = page.locator("#at-main");
  await expect(main).toContainText("发现账本", { timeout: 15_000 });
  // 链完整性徽章:要么完整要么断裂,不显示第三种含糊态
  await expect(main.locator(".at-chain .at-badge").first()).toHaveText(/链完整|链断裂/);
  // URL 反映选中包(深链契约)
  await expect(page).toHaveURL(/#\/audit\/.+/);
});

test("操作员处置:选择后即时落链并刷新链区段", async ({ page }) => {
  await page.goto("/");
  await openAudit(page);
  await page.locator("#at-bundles .at-bitem").first().click();
  const select = page.locator(".at-disp-select").first();
  await expect(select).toBeVisible({ timeout: 15_000 });
  await select.selectOption("accepted_risk");
  await expect(page.locator(".at-disp-status").first()).toHaveText("已记录", { timeout: 15_000 });
});

test("#/audit/<id> 深链直达预选;无效包死链自愈回审计根", async ({ page }) => {
  // 先拿到一个真实 bundle id
  await page.goto("/");
  await openAudit(page);
  const first = page.locator("#at-bundles .at-bitem").first();
  await expect(first).toBeVisible({ timeout: 15_000 });
  const dir = await first.getAttribute("data-dir");

  await page.goto(`/#/audit/${dir}`);
  await expect(page.locator("#at-main")).toContainText("发现账本", { timeout: 15_000 });
  await expect(page.locator("#at-bundles .at-bitem.at-sel")).toHaveCount(1);

  // 死链:不存在的包 → URL 退回审计根,详情回占位
  await page.goto("/#/audit/no-such-bundle-e2e");
  await expect(page).toHaveURL(/#\/audit$/);
  await expect(page.locator("#at-main")).toContainText("从右侧「发现账本」选择一次审计");
});
