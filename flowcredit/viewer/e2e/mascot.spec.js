import { test, expect } from "@playwright/test";

const STATES = ["idle", "thinking", "pending", "ok", "warn", "bad"];

async function boot(page) {
  await page.goto("/");
  await page.waitForSelector('body[data-booted="1"]');
}

test("Work 开屏角色有完整身体、真实 idle 状态与指针跟随", async ({ page }) => {
  await boot(page);
  const mascot = page.locator(".stage-mascot .ai-avatar-mascot");
  await expect(mascot).toBeVisible();
  await expect(mascot).toHaveAttribute("data-ai-state", "idle");
  await expect(mascot.locator(".ai-eye")).toHaveCount(2);
  await expect(mascot.locator(".ai-avatar-arm")).toHaveCount(2);
  await expect(mascot.locator(".ai-avatar-leg")).toHaveCount(2);
  await expect(mascot.locator(".ai-avatar-antenna")).toBeVisible();
  await expect(mascot.locator(".ai-avatar-tail")).toBeVisible();
  const animation = await mascot.locator("svg").evaluate(el => getComputedStyle(el).animationName);
  expect(animation).not.toBe("none");

  await page.mouse.move(20, 20);
  await expect.poll(() => mascot.evaluate(el => el.style.getPropertyValue("--look-x"))).not.toBe("0px");
  const first = await mascot.evaluate(el => el.style.getPropertyValue("--look-x"));
  await page.mouse.move(1380, 820);
  await expect.poll(() => mascot.evaluate(el => el.style.getPropertyValue("--look-x"))).not.toBe(first);
});

test("六种状态渲染不同表情和动作，思考与庆祝有专属像素层", async ({ page }) => {
  await boot(page);
  await page.evaluate(async states => {
    const { aiAvatarHtml } = await import("/core/ai-avatar.js");
    const host = document.createElement("div");
    host.id = "mascot-state-test-host";
    host.style.cssText = "position:fixed;inset:0;z-index:9999;display:grid;grid-template-columns:repeat(3,120px);place-content:center;gap:24px;background:#fbfaf9";
    host.innerHTML = states.map(state => `<div data-mascot-case="${state}" style="display:grid;place-items:center;gap:8px">${aiAvatarHtml(state, 96)}<span>${state}</span></div>`).join("");
    document.body.append(host);
  }, STATES);

  for (const state of STATES) {
    const mascot = page.locator(`[data-mascot-case="${state}"] .ai-avatar-mascot`);
    await expect(mascot).toHaveAttribute("data-ai-state", state);
    await expect(mascot.locator(".ai-eye")).toHaveCount(2);
    await expect(mascot.locator(".ai-avatar-mouth")).toHaveCount(1);
    const animation = await mascot.locator("svg").evaluate(el => getComputedStyle(el).animationName);
    expect(animation).not.toBe("none");
  }
  await expect(page.locator('[data-mascot-case="thinking"] .ai-avatar-thoughts rect')).toHaveCount(3);
  await expect(page.locator('[data-mascot-case="thinking"] .ai-avatar-ring')).toBeVisible();
  await expect(page.locator('[data-mascot-case="pending"] .ai-avatar-ring')).toBeVisible();
  await expect(page.locator('[data-mascot-case="ok"] .ai-avatar-confetti rect')).toHaveCount(6);
  await expect(page.locator('[data-mascot-case="ok"] .ai-avatar-ring')).toHaveCount(0);
  await expect(page.locator('[data-mascot-case="bad"] .ai-avatar-ring')).toHaveCount(0);
});

test("减少动态效果时角色仍完整可见，所有部件停止动画", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await boot(page);
  const mascot = page.locator(".stage-mascot .ai-avatar-mascot");
  await expect(mascot).toBeVisible();
  await expect(mascot).toHaveAttribute("data-ai-state", "idle");
  for (const selector of ["svg", ".ai-eye", ".ai-avatar-tail", ".ai-avatar-ground"]) {
    const animation = await mascot.locator(selector).first().evaluate(el => getComputedStyle(el).animationName);
    expect(animation).toBe("none");
  }
  await page.mouse.move(1300, 700);
  await expect(mascot).toHaveCSS("--look-x", "0px");

  await page.evaluate(async () => {
    const { aiAvatarHtml } = await import("/core/ai-avatar.js");
    document.querySelector(".stage-mascot").innerHTML = aiAvatarHtml("pending", 84);
  });
  const pending = page.locator(".stage-mascot .ai-avatar--pending");
  const arm = pending.locator(".ai-avatar-arm--right");
  expect(await arm.evaluate(el => getComputedStyle(el).transform)).not.toBe("none");
  expect(await arm.evaluate(el => getComputedStyle(el).animationName)).toBe("none");
});

test("治理首页保留主伙伴和三个子代理，各断点没有横向溢出", async ({ page }) => {
  await boot(page);
  await page.locator('#zone-switch [data-zone="gov"]').click();
  const companion = page.locator(".gov-home-companion .ai-avatar-mascot");
  await expect(companion).toBeVisible();
  await expect(companion).toHaveAttribute("data-ai-state", "idle");
  await expect(page.locator(".gov-home-crew .gov-home-agent")).toHaveCount(3);

  for (const width of [375, 768, 1024, 1440]) {
    await page.setViewportSize({ width, height: 900 });
    await expect(companion).toBeVisible();
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), {
      message: `${width}px 不应出现横向溢出`,
    }).toBe(true);
  }
});
