/* Moonshot's monochrome wordmark inside a calm Kimi-style workspace.
   The stage identifies FCode; it never competes with the task. */
import { esc } from "./format.js";

const SUGGESTIONS = {
  work: [
    ["审计一个目录", "审计这个目录，先列出证据清单，再按严重程度排序。"],
    ["修一个 bug", "定位并修复这个 bug：先复现，再给出最小补丁和验证结果。"],
    ["整理项目结构", "整理这个项目的结构，指出职责不清和重复代码，并给出改动建议。"],
    ["写一个脚本", "写一个脚本完成这个任务，并附上运行方式和验证结果。"],
  ],
  chat: [
    ["解释这段代码", "解释这段代码在做什么，以及它最容易出错的地方。"],
    ["总结一份文档", "总结这份文档的关键结论、证据和仍未解决的问题。"],
    ["起草一个方案", "起草一个可执行方案，写清步骤、风险和验收方式。"],
    ["查一个事实", "查清这个事实，给出出处，并区分事实与推断。"],
  ],
  gov: [
    ["最近审计怎么样？", "最近一次审计怎么样？"],
    ["扫描一个目录", "让审计员扫描 fixtures/demo-target"],
    ["查看待批准", "现在有哪些待批准？"],
  ],
};

export function moonshotStageHtml(note, options = {}) {
  const gov = !!options.gov;
  const chat = !gov && !!options.chat;
  const mode = gov ? "GOV" : chat ? "CHAT" : "WORK";
  const title = gov
    ? ["Talk to your", "team."]
    : chat ? ["Bring proof", "to ideas."] : ["Build your", "own work."];
  const suggestions = (gov ? SUGGESTIONS.gov : chat ? SUGGESTIONS.chat : SUGGESTIONS.work)
    .map(([label, prompt]) => `<button type="button" class="stage-suggestion" data-stage-prompt="${esc(prompt)}">${esc(label)}</button>`)
    .join("");
  return `<section class="stage-empty moonshot-stage${chat ? " is-chat" : ""}" aria-label="${gov ? "开始治理对话" : chat ? "开始对话" : "开始任务"}">
    <div class="moonshot-stage-copy">
      <div class="moonshot-kicker"><span class="moonshot-pixel" aria-hidden="true"></span><span>FCODE</span><span class="stage-badge">${mode}</span></div>
      <div class="stage-mark moonshot-title" aria-label="${title.join(" ")}">
        <span class="moonshot-title-line">${title[0]}</span>
        <span class="moonshot-title-line">${title[1]}<i class="moonshot-cursor" aria-hidden="true"></i></span>
      </div>
      ${note ? `<p class="stage-note">${esc(note)}</p>` : ""}
    </div>
    <div class="stage-dock" id="stage-dock"></div>
    <div class="stage-suggestions" aria-label="快捷开始">${suggestions}</div>
  </section>`;
}
