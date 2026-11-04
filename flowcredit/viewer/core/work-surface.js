"use strict";

import { esc } from "./format.js";

/* Suggestions only place a draft in the composer. Starting work remains an
   explicit operator action, exactly like Kimi's prompt suggestions. */
const WORK_PROMPTS = [
  ["01", "改造一个代码项目", "分析项目结构，完成一项明确的改造，并运行独立验证。"],
  ["02", "研究并整理结论", "研究这个主题，梳理关键事实、分歧与来源，形成一份可核验的报告："],
  ["03", "交付一份工作成果", "完成这项任务，将过程、验证结果和交付文件整理清楚："],
];

export function workPromptsHtml() {
  return `<div class="stage-prompts" aria-label="任务建议">${WORK_PROMPTS.map(([n, title, prompt]) =>
    `<button class="stage-prompt" type="button" data-stage-prompt="${esc(prompt)}"><span class="stage-prompt-no">${n}</span><span>${esc(title)}</span><span class="stage-prompt-arrow" aria-hidden="true">↗</span></button>`
  ).join("")}</div>`;
}

export function bindWorkspaceNavigation({ openPalette, setMobileNavigation }) {
  document.getElementById("side-search")?.addEventListener("click", openPalette);
  document.getElementById("workspace-menu")?.addEventListener("click", () => setMobileNavigation(true));
  document.querySelector(".sidebar-scrim")?.addEventListener("click", () => setMobileNavigation(false));
}

/* Run and trace links open Work content, so the sidebar must not retain the
 * persisted Chat mode while the main pane shows a run. */
export function modeForDeepLink(search) {
  const params = new URLSearchParams(String(search || ""));
  return params.get("run") || params.get("trace") ? "work" : null;
}

/* 审计台 hash 深链:#/audit(审计根,返回 "")或 #/audit/<dir>(指定包,返回 dir);
   其他 hash 返回 null。dir 规则与 viewer 服务端的目录白名单一致
   (^[a-z0-9][a-z0-9.-]{1,63}$),越界值当 null 处理,不进渲染。 */
export function auditHashTarget(hash) {
  const match = /^#\/audit(?:\/([a-z0-9][a-z0-9.-]{1,63}))?$/.exec(String(hash || ""));
  if (!match) return null;
  return match[1] || "";
}

export function bindLatestScroll() {
  const scroller = document.querySelector(".main");
  const button = document.getElementById("scroll-latest");
  if (!scroller || !button) return;
  const update = () => {
    const visible = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 240;
    button.hidden = !visible;
  };
  scroller.addEventListener("scroll", update, { passive: true });
  button.addEventListener("click", () => {
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  });
  new MutationObserver(update).observe(document.getElementById("main"), { childList: true, subtree: true });
  update();
}
