/* FCode · 纯函数内核
 *
 * 这一层只做确定性计算：规范 JSON、SHA-256、链校验、显示格式。
 * 不碰 DOM、不读状态，因此可以被控制台、报告生成器或后续视图模块复用。
 * 零依赖、零构建；浏览器原生 ES module 直接加载。
 */

export function canonical(value) {
  if (value === null || typeof value !== "object") return JSON.stringify(value);
  if (Array.isArray(value)) return "[" + value.map(canonical).join(",") + "]";
  return "{" + Object.keys(value).sort().map(k => JSON.stringify(k) + ":" + canonical(value[k])).join(",") + "}";
}

export async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest)).map(b => b.toString(16).padStart(2, "0")).join("");
}

/* 全链重算：序号、前向哈希、事件哈希、链头、计数，任何一项不符都算断链。 */
export async function verifyChain(trace) {
  const events = trace.events || [];
  if (!events.length) return { ok: false, reason: "trace 里没有事件" };
  let previous = null;
  for (let i = 0; i < events.length; i++) {
    const e = events[i];
    if (e.sequence !== i + 1) return { ok: false, reason: `第 ${i + 1} 个事件序号不连续` };
    if (e.prev_hash !== previous) return { ok: false, reason: `第 ${i + 1} 个事件没有链接到前一个` };
    const expected = await sha256Hex(canonical({
      sequence: e.sequence, type: e.type, payload: e.payload, prev_hash: e.prev_hash, at: e.at
    }));
    if (expected !== e.hash) return { ok: false, reason: `第 ${i + 1} 个事件（${e.type}）哈希不匹配` };
    previous = e.hash;
  }
  const chain = trace.chain || {};
  if (chain.head_hash !== previous) return { ok: false, reason: "链头与最后一个事件不一致" };
  if (chain.event_count !== events.length) return { ok: false, reason: "事件计数与实际不符" };
  return { ok: true, events: events.length, head: previous };
}

export const esc = value => String(value).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
export const pct = bp => (bp / 100).toFixed(2) + "%";
export const pp = bp => (bp >= 0 ? "+" : "") + (bp / 100).toFixed(2) + " pp";
export const num = n => Number(n).toLocaleString("en-US");
export const shortHash = h => String(h || "").slice(0, 12);
export const stripTags = html => String(html).replace(/<[^>]*>/g, "").replace(/\s+/g, " ").trim();
