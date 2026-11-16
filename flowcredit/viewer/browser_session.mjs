/* Long-lived browser session for one FCode workspace.
 *
 * One Chromium/context/page stays alive across tool calls, so inspect → click
 * → fill → press → inspect does not reload the page or lose client state.
 * Commands arrive as newline-delimited JSON on stdin and answers leave as one
 * JSON line on stdout. The Python host owns URL policy and artifact paths.
 */

import { chromium } from "playwright";
import { createInterface } from "node:readline";
import { writeFileSync } from "node:fs";
import { URL } from "node:url";

const IDLE_MS = Math.max(30_000, Number(process.env.FCODE_BROWSER_IDLE_MS) || 300_000);
let browser = null;
let context = null;
let page = null;
let currentUrl = "";
let consoleErrors = [];
let idleTimer = null;

function loopback(hostname) {
  const host = String(hostname || "").replace(/^\[|\]$/g, "").toLowerCase();
  return host === "localhost" || host === "127.0.0.1" || host === "::1";
}

function resetIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(async () => {
    await cleanup();
    process.exit(0);
  }, IDLE_MS);
}

function reply(payload) {
  process.stdout.write(JSON.stringify(payload) + "\n");
  resetIdle();
}

function failure(code, message, extra = {}) {
  return { ok: false, error: { code, message: String(message || code) }, ...extra };
}

function assertUrl(value) {
  const target = new URL(String(value || ""));
  if (!["http:", "https:"].includes(target.protocol) || !loopback(target.hostname)) {
    const error = new Error("browser only opens loopback http(s) URLs");
    error.code = "BROWSER_URL_DENIED";
    throw error;
  }
  return target;
}

async function cleanup() {
  clearTimeout(idleTimer);
  try { if (context) await context.close(); } catch { /* ignore */ }
  try { if (browser) await browser.close(); } catch { /* ignore */ }
  context = null;
  browser = null;
  page = null;
  currentUrl = "";
}

async function ensurePage() {
  if (page) return page;
  browser = await chromium.launch({ headless: true, args: ["--disable-dev-shm-usage"] });
  context = await browser.newContext({
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
  page = await context.newPage();
  page.on("console", message => {
    if (message.type() === "error" && consoleErrors.length < 20) {
      consoleErrors.push(String(message.text() || "").slice(0, 300));
    }
  });
  page.on("pageerror", error => {
    if (consoleErrors.length < 20) consoleErrors.push(String(error && error.message || error).slice(0, 300));
  });
  return page;
}

async function navigate(target, waitMs) {
  const url = assertUrl(target);
  const active = await ensurePage();
  if (url.toString() !== currentUrl) {
    consoleErrors = [];
    await active.goto(url.toString(), {
      waitUntil: "domcontentloaded",
      timeout: 15_000,
    });
    currentUrl = active.url();
  }
  if (waitMs) await active.waitForTimeout(waitMs);
  return active;
}

async function snapshot(active, command, textLimit, elementLimit) {
  const title = await active.title().catch(() => "");
  const text = await active.evaluate(limit => {
    const value = document.body ? document.body.innerText : "";
    return String(value || "").slice(0, limit);
  }, textLimit).catch(() => "");
  const elements = await active.evaluate((limit) => {
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
  }, elementLimit).catch(() => []);
  const metrics = await active.evaluate(() => ({
    width: document.documentElement ? document.documentElement.scrollWidth : 0,
    height: document.documentElement ? document.documentElement.scrollHeight : 0,
  })).catch(() => ({ width: 0, height: 0 }));
  if (command.dom_path) {
    const html = String(await active.content()).slice(0, 2_000_000);
    writeFileSync(command.dom_path, html, "utf8");
  }
  return {
    title,
    url: active.url(),
    text: String(text || ""),
    elements,
    document: metrics,
    console_errors: consoleErrors,
  };
}

async function screenshot(active, path, fullPage) {
  if (!path) return false;
  await active.screenshot({ path, fullPage: Boolean(fullPage), animations: "disabled" });
  return true;
}

async function handle(command) {
  const waitMs = Math.max(0, Math.min(Number(command.wait_ms) || 300, 5_000));
  const textLimit = Math.max(0, Math.min(Number(command.max_text) || 12_000, 40_000));
  const elementLimit = Math.max(1, Math.min(Number(command.max_elements) || 80, 200));
  const action = String(command.command || command.action || "");

  if (action === "close") {
    await cleanup();
    reply({ ok: true, closed: true });
    setTimeout(() => process.exit(0), 10);
    return;
  }
  if (action === "inspect") {
    const active = await navigate(command.url, waitMs);
    const data = await snapshot(active, command, textLimit, elementLimit);
    const beforeText = data.text;
    const beforeWritten = await screenshot(active, command.before_path, command.full_page);
    const afterWritten = await screenshot(active, command.screenshot_path, command.full_page);
    reply({
      ok: true, action: "inspect", selector: "", ...data,
      before_text: beforeText, after_text: data.text,
      before_written: beforeWritten, after_written: afterWritten,
      duration_ms: 0,
    });
    return;
  }
  if (!["click", "fill", "press"].includes(action)) {
    reply(failure("BROWSER_ACTION_INVALID", `unsupported browser action: ${action}`));
    return;
  }
  if (command.url) {
    await navigate(command.url, waitMs);
  }
  if (!page) {
    reply(failure("BROWSER_NO_SESSION", "call browser_inspect first to open a page"));
    return;
  }
  const active = page;
  const beforeData = await snapshot(active, command, textLimit, elementLimit);
  const beforeWritten = await screenshot(active, command.before_path, command.full_page);
  const selector = String(command.selector || "").slice(0, 300);
  if (!selector) {
    reply(failure("BROWSER_SELECTOR_INVALID", "click/fill/press require a selector",
      { before_written: beforeWritten }));
    return;
  }
  const locator = active.locator(selector).first();
  if (!(await locator.count().catch(() => 0))) {
    reply(failure("BROWSER_SELECTOR_NOT_FOUND", `selector not found: ${selector}`,
      { before_written: beforeWritten, before_text: beforeData.text }));
    return;
  }
  if (action === "click") {
    await locator.click({ timeout: 8_000, animations: "disabled" });
  } else if (action === "fill") {
    await locator.fill(String(command.text || ""), { timeout: 8_000 });
  } else {
    await locator.press(String(command.key || "Enter"), { timeout: 8_000 });
  }
  if (waitMs) await active.waitForTimeout(waitMs);
  const afterData = await snapshot(active, command, textLimit, elementLimit);
  const afterWritten = await screenshot(active, command.after_path, command.full_page);
  currentUrl = active.url();
  reply({
    ok: true, action, selector, ...afterData,
    before_text: beforeData.text, after_text: afterData.text,
    before_written: beforeWritten, after_written: afterWritten,
    duration_ms: 0,
  });
}

const lines = createInterface({ input: process.stdin, crlfDelay: Infinity });
resetIdle();
lines.on("line", line => {
  let command;
  try {
    command = JSON.parse(line);
  } catch {
    reply(failure("BROWSER_COMMAND_INVALID", "command is not JSON"));
    return;
  }
  const id = command.id;
  handle(command)
    .then(result => {
      if (result) reply({ id, ...result });
    })
    .catch(error => {
      reply({
        id,
        ...failure(error.code || "BROWSER_PAGE_FAILED", error.message || error),
      });
    });
});
lines.on("close", async () => {
  await cleanup();
  process.exit(0);
});