/**
 * External browser sidecar for computer use.
 *
 * This process is deliberately separate from `browser_session.mjs`: the
 * existing browser_* tools are loopback-only, and this one must not weaken
 * that policy. It runs a temporary Chromium profile with no user credentials,
 * no extensions, no downloads and a host-supplied domain allowlist. Every
 * request outside that allowlist is aborted at the route layer.
 *
 * Protocol: one JSON object per line on stdin, one JSON object per line on
 * stdout. The Python host owns policy and artifacts; this file only executes.
 */
import { chromium } from "playwright";
import fs from "node:fs";
import path from "node:path";

const IDLE_MS = Math.max(30_000, Number(process.env.FCODE_COMPUTER_IDLE_MS) || 300_000);
const MAX_TEXT = 12_000;
const MAX_ELEMENTS = 200;

let browser = null;
let context = null;
let page = null;
let scope = [];
let idleTimer = null;

function reply(value) {
  process.stdout.write(JSON.stringify(value) + "\n");
}

function failure(code, message, extra = {}) {
  return { ok: false, error: { code, message, ...extra } };
}

function touchIdle() {
  clearTimeout(idleTimer);
  idleTimer = setTimeout(async () => {
    try {
      if (browser) await browser.close();
    } catch { /* best effort */ }
    browser = null;
    context = null;
    page = null;
  }, IDLE_MS);
  idleTimer.unref?.();
}

function isPrivateHost(host) {
  const value = String(host || "").toLowerCase();
  if (!value || value === "localhost" || value.endsWith(".local")) return true;
  if (value.includes(":")) return true; // IPv6 literal
  const parts = value.split(".").map((part) => Number(part));
  if (parts.length !== 4 || parts.some((part) => !Number.isInteger(part))) {
    // A hostname is allowed; it is checked against the allowlist below.
    return false;
  }
  if (parts.some((part) => part < 0 || part > 255)) return true;
  return parts[0] === 10 || parts[0] === 127 || parts[0] === 0
    || (parts[0] === 169 && parts[1] === 254)
    || (parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31)
    || (parts[0] === 192 && parts[1] === 168);
}

function domainAllowed(host, domains) {
  const value = String(host || "").toLowerCase().replace(/\.$/, "");
  if (!value) return false;
  return domains.some((domain) => {
    const allowed = String(domain || "").toLowerCase().replace(/^\./, "");
    return allowed && (value === allowed || value.endsWith("." + allowed));
  });
}

function validateUrl(raw, domains) {
  let url;
  try {
    url = new URL(String(raw || ""));
  } catch {
    return { error: failure("URL_INVALID", "url is not valid") };
  }
  if (!["http:", "https:"].includes(url.protocol)) {
    return { error: failure("URL_DENIED", "only http(s) URLs are allowed") };
  }
  if (url.username || url.password) {
    return { error: failure("URL_DENIED", "URLs with credentials are not allowed") };
  }
  if (isPrivateHost(url.hostname) || !domainAllowed(url.hostname, domains)) {
    return { error: failure("URL_DENIED", "host is not in the session domain allowlist") };
  }
  return { url };
}

async function ensurePage(domains) {
  scope = Array.isArray(domains) ? domains : [];
  if (browser && context && page) return;
  browser = await chromium.launch({
    headless: true,
    args: ["--disable-blink-features=AutomationControlled"],
  });
  context = await browser.newContext({
    acceptDownloads: false,
    locale: "en-US",
    timezoneId: "America/New_York",
    viewport: { width: 1280, height: 800 },
    permissions: [],
  });
  await context.route("**/*", (route) => {
    let requestUrl;
    try {
      requestUrl = new URL(route.request().url());
    } catch {
      return route.abort();
    }
    if (["data:", "blob:"].includes(requestUrl.protocol)) return route.continue();
    if (!["http:", "https:"].includes(requestUrl.protocol)) return route.abort();
    if (isPrivateHost(requestUrl.hostname) || !domainAllowed(requestUrl.hostname, scope)) {
      return route.abort();
    }
    return route.continue();
  });
  page = await context.newPage();
  page.on("filechooser", (chooser) => chooser.setFiles([]).catch(() => {}));
  touchIdle();
}

async function collect() {
  const text = await page.evaluate(() => document.body ? document.body.innerText : "");
  const elements = await page.evaluate(() => {
    const nodes = Array.from(document.querySelectorAll(
      "a,button,input,textarea,select,[role=button],[role=link],[role=menuitem],[contenteditable=true]"
    )).slice(0, 200);
    let index = 0;
    return nodes.map((node) => {
      const rect = node.getBoundingClientRect();
      const item = {
        index: index++,
        tag: node.tagName.toLowerCase(),
        role: node.getAttribute("role") || "",
        text: (node.innerText || node.value || node.getAttribute("aria-label") || "")
          .trim().slice(0, 120),
        type: node.getAttribute("type") || "",
        name: node.getAttribute("name") || "",
        id: node.id || "",
        disabled: Boolean(node.disabled || node.getAttribute("aria-disabled") === "true"),
        x: Math.round(rect.x + window.scrollX),
        y: Math.round(rect.y + window.scrollY),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      };
      return item;
    });
  });
  return {
    url: page.url(),
    title: await page.title(),
    text: String(text || "").slice(0, MAX_TEXT),
    elements: elements.slice(0, MAX_ELEMENTS),
    width: await page.evaluate(() => document.documentElement.scrollWidth || 0),
    height: await page.evaluate(() => document.documentElement.scrollHeight || 0),
  };
}

function destructive(text, markers) {
  const value = String(text || "").toLowerCase();
  return markers.some((marker) => value.includes(String(marker).toLowerCase()));
}

async function commandLook(command) {
  const domains = Array.isArray(command.domains) ? command.domains : [];
  let checked = null;
  if (command.url) {
    checked = validateUrl(command.url, domains);
    if (checked.error) return checked.error;
  }
  await ensurePage(domains);
  if (checked) {
    await page.goto(checked.url.toString(), {
      waitUntil: "domcontentloaded", timeout: 15_000,
    });
    await page.waitForTimeout(300);
  }
  const data = await collect();
  if (command.screenshot_path) await page.screenshot({ path: command.screenshot_path });
  if (command.dom_path) {
    fs.mkdirSync(path.dirname(command.dom_path), { recursive: true });
    fs.writeFileSync(command.dom_path, await page.content(), "utf-8");
  }
  return { ok: true, ...data };
}

async function commandAct(command) {
  const domains = Array.isArray(command.domains) ? command.domains : [];
  let checked = null;
  if (command.url) {
    checked = validateUrl(command.url, domains);
    if (checked.error) return checked.error;
  }
  await ensurePage(domains);
  const started = Date.now();
  if (checked) {
    await page.goto(checked.url.toString(), {
      waitUntil: "domcontentloaded", timeout: 15_000,
    });
    await page.waitForTimeout(200);
  }
  const before = await page.evaluate(() => document.body ? document.body.innerText : "");
  if (command.before_path) await page.screenshot({ path: command.before_path });
  const action = String(command.action || "").toLowerCase();
  const selector = String(command.selector || "");
  if (!selector) return failure("BROWSER_SELECTOR_INVALID", "selector is required");
  const locator = page.locator(selector).first();
  if (await locator.count() === 0) {
    return failure("BROWSER_SELECTOR_NOT_FOUND", "selector matched no element");
  }
  if (action === "click") {
    const label = await locator.innerText().catch(() => "");
    if (!command.allow_destructive && destructive(label, command.destructive_markers || [])) {
      return failure("COMPUTER_DESTRUCTIVE_BLOCKED",
        "target looks destructive; request a session with allow_destructive=true");
    }
    await locator.click({ timeout: 8_000, animations: "disabled" });
  } else if (action === "fill") {
    await locator.fill(String(command.text || ""), { timeout: 8_000 });
  } else if (action === "press") {
    await locator.press(String(command.key || "Enter"), { timeout: 8_000 });
  } else {
    return failure("COMPUTER_ACTION_INVALID", "unsupported browser action");
  }
  await page.waitForTimeout(300);
  const after = await page.evaluate(() => document.body ? document.body.innerText : "");
  if (command.after_path) await page.screenshot({ path: command.after_path });
  return {
    ok: true,
    action,
    target: selector,
    text: String(after || "").slice(0, MAX_TEXT),
    dom_diff: before === after ? "" : "page text changed",
    width: await page.evaluate(() => document.documentElement.scrollWidth || 0),
    height: await page.evaluate(() => document.documentElement.scrollHeight || 0),
    duration_ms: Date.now() - started,
  };
}

async function handle(command) {
  touchIdle();
  const name = String(command.command || "").toLowerCase();
  if (name === "ping") return { ok: true, platform: "external-browser" };
  if (name === "release") {
    try {
      if (browser) await browser.close();
    } catch { /* best effort */ }
    browser = null;
    context = null;
    page = null;
    return { ok: true, released: true };
  }
  if (name === "look") return commandLook(command);
  if (name === "act") return commandAct(command);
  return failure("COMPUTER_COMMAND_INVALID", "unknown command");
}

let buffer = "";
process.stdin.setEncoding("utf-8");
process.stdin.on("data", async (chunk) => {
  buffer += chunk;
  let newline;
  while ((newline = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, newline).trim();
    buffer = buffer.slice(newline + 1);
    if (!line) continue;
    let command;
    try {
      command = JSON.parse(line);
    } catch {
      reply({ id: null, ok: false, error: { code: "COMPUTER_BAD_REQUEST", message: "invalid JSON" } });
      continue;
    }
    const id = command.id ?? null;
    try {
      const result = await handle(command);
      reply({ id, ...result });
    } catch (error) {
      reply({ id, ok: false, error: {
        code: "COMPUTER_FAILED", message: String(error?.message || error).slice(0, 300),
      } });
    }
  }
});

process.on("SIGTERM", async () => {
  try {
    if (browser) await browser.close();
  } catch { /* best effort */ }
  process.exit(0);
});
