/* Chat display helpers: Markdown rendering and SSE frame parsing.
 *
 * Pure functions only — no DOM, no app state. They live outside app.js so the
 * Node built-in test runner can exercise the escaping/formatting rules without
 * a browser.
 */

import { esc } from "./format.js";

export function renderMarkdown(text) {
  const raw = String(text || "");
  if (!raw.trim()) return `<span class="dim">…</span>`;
  const parts = raw.split(/```/);
  let html = "";
  parts.forEach((part, index) => {
    if (index % 2 === 1) {
      const newline = part.indexOf("\n");
      const language = newline >= 0 ? part.slice(0, newline).trim() : "";
      const code = newline >= 0 ? part.slice(newline + 1) : part;
      html += `<pre class="chat-code"><code${language ? ` data-lang="${esc(language)}"` : ""}>${esc(code.replace(/\n$/, ""))}</code></pre>`;
      return;
    }
    html += markdownText(part);
  });
  return html;
}

function markdownText(text) {
  const out = [];
  let list = null;
  let paragraph = [];
  const flushList = () => {
    if (!list) return;
    out.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`);
    list = null;
  };
  const flushParagraph = () => {
    if (!paragraph.length) return;
    out.push(`<p>${paragraph.map(inlineMarkdown).join("<br>")}</p>`);
    paragraph = [];
  };
  for (const line of String(text).split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) { flushParagraph(); flushList(); continue; }
    const heading = /^(#{1,3})\s+(.*)$/.exec(trimmed);
    if (heading) {
      flushParagraph(); flushList();
      const level = Math.min(4, heading[1].length + 1);
      out.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
      continue;
    }
    const unordered = /^[-*]\s+(.*)$/.exec(trimmed);
    const ordered = /^\d+[.)]\s+(.*)$/.exec(trimmed);
    if (unordered || ordered) {
      flushParagraph();
      const tag = unordered ? "ul" : "ol";
      if (!list || list.tag !== tag) { flushList(); list = { tag, items: [] }; }
      list.items.push(`<li>${inlineMarkdown((unordered || ordered)[1])}</li>`);
      continue;
    }
    if (trimmed.startsWith(">")) {
      flushParagraph(); flushList();
      out.push(`<blockquote>${inlineMarkdown(trimmed.slice(1).trim())}</blockquote>`);
      continue;
    }
    flushList();
    paragraph.push(trimmed);
  }
  flushParagraph(); flushList();
  return out.join("");
}

function inlineMarkdown(text) {
  let value = esc(text);
  value = value.replace(/`([^`]+)`/g, (_, code) => `<code>${code}</code>`);
  value = value.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  value = value.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  // 壳内审计深链:同文档 hash 导航,不开新标签;hashchange 监听负责进审计面。
  value = value.replace(/\[([^\]]+)\]\((\/#\/audit(?:\/[a-z0-9][a-z0-9.-]{0,63})?)\)/g,
    '<a href="$2">$1</a>');
  value = value.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
  value = value.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>');
  return value;
}

export function parseSseFrame(frame) {
  let data = "";
  for (const line of String(frame || "").split("\n")) {
    if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  try { return JSON.parse(data); } catch { return null; }
}
