/* Moonshot's monochrome, orbital visual language inside a calm Kimi-style
   workspace. The scene identifies FCode; it never competes with the task. */
import { esc } from "./format.js";
import { workPromptsHtml } from "./work-surface.js";

export function moonshotStageHtml(note, options = {}) {
  const chat = !!options.chat;
  const work = !!options.work;
  const mode = chat ? "CHAT" : "WORK";
  const title = chat ? ["Bring light", "to ideas."] : ["Build your", "own work."];
  const footnote = options.footnote || "";
  return `<section class="stage-empty moonshot-stage${chat ? " is-chat" : ""}" aria-label="${chat ? "开始对话" : "开始任务"}">
    <div class="moonshot-stage-top">
      <div class="moonshot-stage-copy">
        <div class="moonshot-kicker"><span class="moonshot-pixel" aria-hidden="true"></span><span>FCODE</span><span>/</span><span>LOCAL AGENT</span><span class="stage-badge">${mode}</span></div>
        <div class="stage-mark moonshot-title" aria-label="${title.join(" ")}">
          <span class="moonshot-title-line">${title[0]}</span>
          <span class="moonshot-title-line">${title[1]}<i class="moonshot-cursor" aria-hidden="true"></i></span>
        </div>
        <p class="stage-note">${esc(note)}</p>
      </div>
      <div class="moonshot-scene" aria-hidden="true">
        <svg viewBox="0 0 300 210">
          <g class="moonshot-orbits"><ellipse cx="172" cy="100" rx="123" ry="40"/><ellipse cx="172" cy="100" rx="94" ry="80"/><path d="M35 178 275 18M49 202 289 42"/></g>
          <g class="moonshot-moon"><circle cx="172" cy="100" r="51"/><path d="M192 58h7v7h-7zM208 72h4v4h-4zM143 102h6v6h-6zM158 137h8v8h-8zM195 137h4v4h-4z"/></g>
          <g class="moonshot-pixels"><path d="M62 42h4v4h-4zM93 151h3v3h-3zM260 165h4v4h-4zM243 32h3v3h-3z"/><rect x="269" y="91" width="7" height="7"/></g>
        </svg>
        <span>EXPLORATION → EVIDENCE</span>
      </div>
    </div>
    <div class="stage-dock" id="stage-dock"></div>
    ${work ? workPromptsHtml() : ""}
    <div class="stage-chips" id="stage-chips" aria-label="按模板开始委托"${work ? "" : " hidden"}></div>
    ${work && footnote ? `<p class="stage-foot">${esc(footnote)}</p>` : ""}
    <div class="moonshot-stage-foot" aria-hidden="true"><span>把光带进来</span><span>MODEL PROPOSES · HOST VERIFIES · YOU DECIDE</span></div>
  </section>`;
}
