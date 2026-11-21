// P2：运行时证据采集——用本机 Chromium 加载目标页，采集网络请求清单、
// console 错误与 storage 键名（值不落盘：可能含会话令牌）。
// 用法：node web_runtime.mjs <url> <out.json>
// 无 playwright/浏览器时以非零退出并打印原因——调用方如实降级，绝不伪造。
import fs from 'node:fs';

const [url, outPath] = process.argv.slice(2);
if (!url || !outPath) {
  console.error('usage: node web_runtime.mjs <url> <out.json>');
  process.exit(2);
}

let chromium;
try {
  const mod = await import(new URL('./node_modules/playwright-core/index.js', import.meta.url).href);
  // CJS/ESM 互操作：chromium 可能在 default 上。
  chromium = mod.chromium || (mod.default && mod.default.chromium);
  if (!chromium) throw new Error('chromium export missing');
} catch (error) {
  console.error(`playwright unavailable: ${error.message}`);
  process.exit(3);
}

const evidence = {
  captured_at: new Date().toISOString(),
  url,
  requests: [],
  console_errors: [],
  storage_keys: {},
  page_title: '',
  status: 'ok',
  reason: '',
};

const proxy = (process.env.FCODE_AUDIT_PROXY || '').trim();
const ua = (process.env.FCODE_AUDIT_UA || '').trim();
const DEFAULT_UA = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36';

let browser;
try {
  browser = await chromium.launch({
    ...(proxy ? { proxy: { server: proxy } } : {}),
    // 保真模式：去掉 Playwright 默认的 AutomationControlled 特征。
    args: ['--disable-blink-features=AutomationControlled'],
  });
  // 与普通 Chrome 用户一致的上下文（locale/时区/视口/UA 完整一致）。
  const context = await browser.newContext({
    userAgent: ua || DEFAULT_UA,
    locale: 'en-US',
    timezoneId: 'America/New_York',
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 1,
  });
  await context.addInitScript(() => {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
  });
  const page = await context.newPage();
  const seen = new Set();
  page.on('request', (request) => {
    if (evidence.requests.length >= 120) return;
    try {
      const parsed = new URL(request.url());
      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return;
      const key = `${request.method()} ${parsed.origin}`;
      if (seen.has(key)) return;
      seen.add(key);
      evidence.requests.push({
        method: request.method(),
        origin: parsed.origin,
        first_path: parsed.pathname.slice(0, 80),
        resource: request.resourceType(),
      });
    } catch { /* malformed url */ }
  });
  page.on('console', (message) => {
    if (message.type() !== 'error' || evidence.console_errors.length >= 20) return;
    evidence.console_errors.push(String(message.text()).slice(0, 200));
  });
  // load 事件会被慢追踪器（GA/GTM）拖住——证据采集关心的是"发出了哪些
  // 请求"，domcontentloaded 即足够；goto 超时不判死，已采到的证据照常落盘。
  let gotoNote = '';
  try {
    await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 45000 });
  } catch (error) {
    gotoNote = `goto degraded: ${error.name || 'Error'}: ${String(error.message || '').slice(0, 120)}`;
  }
  await page.waitForTimeout(3500);
  if (gotoNote) evidence.reason = gotoNote;
  evidence.page_title = (await page.title()).slice(0, 120);
  // 反自动化挑战：如实记录为审计事实（覆盖受限），绝不尝试破解。
  const challengeProbe = `${page.url()} ${evidence.page_title}`.toLowerCase();
  const markers = ['just a moment', 'attention required', 'verify you are human', 'captcha', 'challenge-platform', 'turnstile'];
  const hit = markers.find((marker) => challengeProbe.includes(marker));
  if (hit) evidence.challenge = hit;
  evidence.webdriver_hidden = await page.evaluate(() => navigator.webdriver === undefined || navigator.webdriver === false);
  // 站内路由发现：渲染后的 DOM 链接（覆盖声明用，不自动抓取）。
  evidence.same_origin_links = await page.evaluate(() => {
    const origin = location.origin;
    const paths = new Set();
    for (const anchor of document.querySelectorAll('a[href]')) {
      try {
        const href = new URL(anchor.href, location.href);
        if (href.origin !== origin) continue;
        const path = href.pathname.replace(/\/$/, '') || '/';
        paths.add(path.slice(0, 80));
        if (paths.size >= 20) break;
      } catch { /* malformed */ }
    }
    return [...paths];
  });
  evidence.storage_keys = await page.evaluate(() => {
    const keys = {};
    for (const [store, area] of [['localStorage', window.localStorage], ['sessionStorage', window.sessionStorage]]) {
      keys[store] = [];
      for (let i = 0; i < Math.min(area.length, 40); i += 1) keys[store].push(area.key(i) || '');
    }
    return keys;
  });
} catch (error) {
  evidence.status = 'error';
  evidence.reason = `${error.name || 'Error'}: ${String(error.message || error).slice(0, 200)}`;
} finally {
  if (browser) await browser.close().catch(() => {});
  fs.writeFileSync(outPath, JSON.stringify(evidence, null, 1));
  process.exit(evidence.status === 'ok' ? 0 : 1);
}
