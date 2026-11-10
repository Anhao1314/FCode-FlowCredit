/* Animated identity avatars for governance bots.
 *
 * Each role gets its own geometry, not just a different initial:
 *   auditor  — radar sweep + scanning eye
 *   verifier — shield + drawing check + orbiting verification dot
 *
 * States: idle / running / done / failed. Running speeds the motion up;
 * failed switches the semantic stroke to the danger color. The surrounding
 * UI still owns the status text, so the avatar is decorative by default.
 */

import { esc } from "./format.js";

const ROLE_LABEL = {
  auditor: "审计员",
  verifier: "复核员",
};

const ROLE_SVG = {
  auditor: `<svg viewBox="0 0 40 40" aria-hidden="true" focusable="false">
    <circle class="ba-disc" cx="20" cy="20" r="20"></circle>
    <circle class="ba-ring" cx="20" cy="20" r="13.5"></circle>
    <g class="ba-sweep">
      <path class="ba-sweep-wedge" d="M20 20 L35.5 14.5 A16 16 0 0 1 35.5 25.5 Z"></path>
      <path class="ba-sweep-line" d="M20 20 L35.5 20"></path>
    </g>
    <circle class="ba-eye-ring" cx="20" cy="20" r="6.2"></circle>
    <circle class="ba-eye" cx="20" cy="20" r="3.8"></circle>
  </svg>`,
  verifier: `<svg viewBox="0 0 40 40" aria-hidden="true" focusable="false">
    <circle class="ba-disc" cx="20" cy="20" r="20"></circle>
    <path class="ba-shield" d="M20 4.8 32.6 9.6v9c0 7.7-5.1 13.5-12.6 17-7.5-3.5-12.6-9.3-12.6-17v-9z"></path>
    <path class="ba-check" d="M13.4 20.2 18 24.7 26.7 15.2"></path>
    <circle class="ba-orbit" cx="20" cy="20" r="16.4" stroke-dasharray="2.4 43"></circle>
  </svg>`,
};

const STATES = new Set(["idle", "running", "pending", "done", "failed"]);
const STATE_LABEL = {
  idle: "待命", running: "执行中", pending: "待批准", done: "完成", failed: "失败",
};

export function botAvatarHtml(role, state = "idle", size = 32, label = "") {
  const key = ROLE_SVG[role] ? role : "auditor";
  const tone = STATES.has(state) ? state : "idle";
  const pixels = Number.isFinite(Number(size)) ? Math.max(16, Number(size)) : 32;
  const name = label || ROLE_LABEL[key] || "治理 Bot";
  return `<span class="bot-avatar bot-avatar-${key} is-${tone}" data-bot-role="${esc(key)}"`
    + ` style="--bot-size:${pixels}px" role="img" aria-label="${esc(name)} · ${esc(STATE_LABEL[tone])}">`
    + ROLE_SVG[key] + "</span>";
}