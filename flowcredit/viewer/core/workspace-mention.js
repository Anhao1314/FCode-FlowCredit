import { esc } from "./format.js";

/* Work composer 的 `@` 手势：把一条工作区相对路径插进任务描述。
 *
 * Node-testable projection only: finding the `@token` under the caret,
 * replacing it with the chosen path, and rendering the picker. The network
 * call and DOM stay in app.js, so this file has no browser dependency.
 */

/* `@` only starts a reference at the beginning of the text or after
 * whitespace/brackets/quotes. "a@b.com" therefore stays an email. */
const MENTION_TAIL = /(^|[\s([{"'])@([^\s@]*)$/;

export function mentionQueryAt(text, caret) {
  const value = String(text == null ? "" : text);
  const pos = Number.isInteger(caret) ? caret : value.length;
  if (pos < 0 || pos > value.length) return null;
  const before = value.slice(0, pos);
  const match = MENTION_TAIL.exec(before);
  if (!match) return null;
  const query = match[2];
  return { start: pos - query.length - 1, end: pos, query };
}

export function insertWorkspaceMention(text, start, end, path) {
  const value = String(text == null ? "" : text);
  const target = String(path == null ? "" : path);
  if (!target) return { text: value, caret: start };
  // A path with spaces cannot be one `@token`; wrap it in inline code so the
  // model receives one unambiguous reference instead of two words.
  const token = target.includes(" ") ? "`@" + target + "`" : "@" + target;
  const next = value.slice(0, start) + token + " " + value.slice(end);
  return { text: next, caret: start + token.length + 1 };
}

export function workspaceMentionHtml({
  files = [],
  query = "",
  index = 0,
  loading = false,
  error = "",
} = {}) {
  const rows = (files || []).map((file, i) => {
    const path = typeof file === "string" ? file : String(file.path || "");
    const name = typeof file === "string"
      ? path.split("/").pop()
      : String(file.name || path.split("/").pop() || "");
    const slash = path.lastIndexOf("/");
    const dir = slash >= 0 ? path.slice(0, slash + 1) : "";
    return `<button type="button" class="mention-item${i === index ? " active" : ""}" data-mention-path="${esc(path)}"><span class="mention-name">${esc(name)}</span><span class="mention-dir">${esc(dir)}</span></button>`;
  }).join("");
  const body = loading
    ? `<div class="mention-state">正在查找…</div>`
    : rows
      ? `<div class="mention-list">${rows}</div>`
      : `<div class="mention-state">${query ? "没有匹配的文件" : "输入文件名或路径"}</div>`;
  return `<div class="mention-head"><span>引用工作区文件</span><span class="mention-query">@${esc(query)}</span></div>
    ${body}
    ${error ? `<div class="mention-error">${esc(error)}</div>` : ""}
    <div class="mention-foot">插入路径 · 模型用工具读取 · ↑↓ 选择 · Enter 插入</div>`;
}