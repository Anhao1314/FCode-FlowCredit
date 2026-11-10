/* Browser inspection sidecar.
 *
 * The Python tool owns URL policy and artifact paths; this process only talks
 * to Playwright and returns structured evidence. It is intentionally
 * inspect-only: navigate, read DOM/text/elements, screenshot. It never clicks,
 * types or downloads. Only loopback pages are allowed, and every non-loopback
 * subresource request is aborted so a local preview cannot quietly leak out.
 */

import { chromium } from "playwright";
import { readFileSync, writeFileSync } from "node:fs";
import { URL } from "node:url";

const input = JSON.parse(readFileSync(0, "utf8"));
const started = Date.now();

function loopback(hostname) {
  const host = String(hostname || "").replace(/^\[|\]$/g, "").toLowerCase();
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

function fail(code, message) {
  writeFileSync(1, JSON.stringify({
    ok: false,
    error: { code, message: String(message || code) },
    duration_ms: Date.now() - started,
  }));
  process.exit(0);
}

const target = new URL(String(input.url || ""));
if (!["http:", "https:"].includes(target.protocol) || !loopback(target.hostname)) {
  fail("BROWSER_URL_DENIED", "browser only opens loopback http(s) URLs");
}
const action = String(input.action || "inspect");
if (!["inspect", "click", "fill", "press"].includes(action)) {
  fail("BROWSER_ACTION_INVALID", `unsupported browser action: ${action}`);
}
const selector = String(input.selector || "").slice(0, 300);
if (action !== "inspect" && !selector) {
  fail("BROWSER_SELECTOR_INVALID", "click/fill/press require a selector");
}

const maxText = Math.max(0, Math.min(Number(input.max_text) || 12000, 40000));
const maxElements = Math.max(1, Math.min(Number(input.max_elements) || 80, 200));
const waitMs = Math.max(0, Math.min(Number(input.wait_ms) || 300, 5000));
let browser;

try {
  browser = await chromium.launch({
    headless: true,
    args: ["--disable-dev-shm-usage"],
  });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
    acceptDownloads: false,
    ignoreHTTPSErrors: false,
  });
  await context.route("**/*", route => {
    let requestUrl;
    try {
      requestUrl = new URL(route.request().url());
    } catch {
      return route.abort();
    }
    if (["data:", "blob:"].includes(requestUrl.protocol) || loopback(requestUrl.hostname)) {
      return route.continue();
    }
    return route.abort();
  });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on("console", message => {
    if (message.type() === "error" && consoleErrors.length < 20) {
      consoleErrors.push(String(message.text() || "").slice(0, 300));
    }
  });
  page.on("pageerror", error => {
    if (consoleErrors.length < 20) consoleErrors.push(String(error && error.message || error).slice(0, 300));
  });

  await page.goto(target.toString(), {
    waitUntil: "domcontentloaded",
    timeout: Math.min(Number(input.navigate_timeout_ms) || 15000, 30000),
  });
  if (waitMs) await page.waitForTimeout(waitMs);

  const beforePath = action === "inspect" ? "" : String(input.before_path || "");
  if (beforePath) {
    await page.screenshot({
      path: beforePath,
      fullPage: Boolean(input.full_page),
      animations: "disabled",
    });
  }
  if (action !== "inspect") {
    const locator = page.locator(selector).first();
    if (!(await locator.count().catch(() => 0))) {
      const error = new Error(`selector not found: ${selector}`);
      error.code = "BROWSER_SELECTOR_NOT_FOUND";
      throw error;
    }
    if (action === "click") {
      await locator.click({ timeout: 8000, animations: "disabled" });
    } else if (action === "fill") {
      await locator.fill(String(input.text || ""), { timeout: 8000 });
    } else {
      await locator.press(String(input.key || "Enter"), { timeout: 8000 });
    }
    if (waitMs) await page.waitForTimeout(waitMs);
  }

  const title = await page.title().catch(() => "");
  const pageUrl = page.url();
  const text = await page.evaluate(limit => {
    const value = document.body ? document.body.innerText : "";
    return String(value || "").slice(0, limit);
  }, maxText).catch(() => "");
  const elements = await page.evaluate((limit) => {
    const selector = 'a,button,input,textarea,select,h1,h2,h3,[role="button"],[role="link"]';
    return Array.from(document.querySelectorAll(selector)).slice(0, limit).map(el => ({
      tag: String(el.tagName || "").toLowerCase(),
      text: String(el.innerText || el.value || el.getAttribute("aria-label") || "")
        .replace(/\s+/g, " ").trim().slice(0, 160),
      role: String(el.getAttribute("role") || ""),
      href: el.href ? String(el.href) : "",
      type: String(el.getAttribute("type") || ""),
      name: String(el.getAttribute("name") || ""),
    }));
  }, maxElements).catch(() => []);
  const metrics = await page.evaluate(() => ({
    width: document.documentElement ? document.documentElement.scrollWidth : 0,
    height: document.documentElement ? document.documentElement.scrollHeight : 0,
  })).catch(() => ({ width: 0, height: 0 }));

  let htmlBytes = 0;
  if (input.dom_path) {
    const html = await page.content();
    const clipped = String(html || "").slice(0, 2_000_000);
    writeFileSync(input.dom_path, clipped, "utf8");
    htmlBytes = Buffer.byteLength(clipped);
  }
  const afterPath = action === "inspect"
    ? String(input.screenshot_path || "")
    : String(input.after_path || "");
  if (afterPath) {
    await page.screenshot({
      path: afterPath,
      fullPage: Boolean(input.full_page),
      animations: "disabled",
    });
  }

  await context.close();
  await browser.close();
  browser = null;
  process.stdout.write(JSON.stringify({
    ok: true,
    action,
    selector,
    url: pageUrl,
    title: String(title || ""),
    text: String(text || ""),
    elements,
    console_errors: consoleErrors,
    document: metrics,
    html_bytes: htmlBytes,
    before_written: Boolean(beforePath),
    after_written: Boolean(afterPath),
    duration_ms: Date.now() - started,
  }));
} catch (error) {
  try { if (browser) await browser.close(); } catch { /* ignore */ }
  fail(error && error.code ? error.code : "BROWSER_PAGE_FAILED", error && error.message ? error.message : error);
}