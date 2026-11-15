"use strict";

import { esc } from "./format.js";

export function bindWorkspaceNavigation({ openPalette, setMobileNavigation }) {
  document.getElementById("side-search")?.addEventListener("click", openPalette);
  document.getElementById("workspace-menu")?.addEventListener("click", () => setMobileNavigation(true));
  document.querySelector(".sidebar-scrim")?.addEventListener("click", () => setMobileNavigation(false));
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
    // “回到最新内容”只属于对话/运行流；治理台、审计台等功能页没有
    // 追加式内容，按钮会压住详情抽屉，不该出现。
    const mode = document.getElementById("main")?.dataset.view;
    const chatLike = mode === "chat" || mode === "run";
    const visible = chatLike
      && scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight > 240;
    button.hidden = !visible;
  };
  scroller.addEventListener("scroll", update, { passive: true });
  button.addEventListener("click", () => {
    scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  });
  new MutationObserver(update).observe(document.getElementById("main"), { childList: true, subtree: true });
  update();
}
