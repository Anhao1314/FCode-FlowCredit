/* FCode 印章线稿符号库 —— 收据章 F 的家族语言。
   规则（design.html v3）：方圆血统（r≈4 圆角方）、24 网格 / 1.7 线宽、
   全圆角端点、单色 currentColor；绿点只由状态元素承担，不入符号。
   同名优先于 doodle 图标；此处未覆盖的名字回落 doodle。 */
(function () {
  const S = (inner, extra = "") =>
    `<svg class="s-icon" aria-hidden="true" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"${extra}>${inner}</svg>`;

  const still = {
    /* ---- 导航与动作 ---- */
    "plus": S('<rect x="4.5" y="4.5" width="15" height="15" rx="4"/><path d="M12 9v6M9 12h6"/>'),
    "kimi-plus": S('<path d="M12 5.5v13M5.5 12h13"/>'),
    "search": S('<rect x="4.5" y="4.5" width="10.5" height="10.5" rx="4"/><path d="M14 14l5.5 5.5"/>'),
    "gear": S('<path d="M4.5 7.5h15M4.5 12h15M4.5 16.5h15"/><rect x="8" y="5.8" width="3.4" height="3.4" rx="1.2" fill="currentColor" stroke="none"/><rect x="12.6" y="10.3" width="3.4" height="3.4" rx="1.2" fill="currentColor" stroke="none"/><rect x="7" y="14.8" width="3.4" height="3.4" rx="1.2" fill="currentColor" stroke="none"/>'),
    "layout-grid": S('<rect x="4.5" y="4.5" width="15" height="15" rx="3"/><path d="M4.5 12h15M12 4.5V12"/><rect x="6" y="6" width="4.5" height="4.5" rx="1.4" fill="currentColor" stroke="none"/>'),
    "calendar": S('<rect x="4.5" y="5.5" width="15" height="14" rx="3"/><path d="M4.5 10h15M8.5 3.5v4M15.5 3.5v4"/><rect x="10.2" y="12.6" width="3.6" height="3.6" rx="1.2" fill="currentColor" stroke="none"/>'),
    "clipboard-list": S('<rect x="5.5" y="4.5" width="13" height="15" rx="3"/><path d="M9 4.5h6M9 9.5h6M9 13h6M9 16.5h3.5"/>'),
    "book-open": S('<path d="M12 6.6C10 5.2 7.6 4.7 5 5v12.7c2.6-.3 5 .2 7 1.5 2-1.3 4.4-1.8 7-1.5V5c-2.6-.3-5 .2-7 1.6z"/><path d="M12 6.7v12.4"/>'),
    "receipt": S('<path d="M5.5 7.5a3 3 0 0 1 3-3h7a3 3 0 0 1 3 3v10.2l-2.17 1.55-2.16-1.55-2.17 1.55-2.16-1.55-2.17 1.55L5.5 17.7z"/><path d="M9 8.5h6M9 12h3.5"/>'),
    "key": S('<rect x="4" y="9" width="8" height="6.5" rx="2.4"/><path d="M12 12.2h7.5M16.5 12.2v3M19.5 12.2v2.2"/>'),
    "panel-rail": S('<rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M9 4.5v15"/>'),
    "panel-fold": S('<rect x="3.5" y="4.5" width="17" height="15" rx="3"/><path d="M15 4.5v15"/>'),

    /* ---- 运行生命周期 ---- */
    "arrow-up": S('<path d="M12 19V5.5"/><path d="M6.5 11 12 5.5 17.5 11"/>'),
    "stop": S('<circle cx="12" cy="12" r="8.5"/><rect x="9.2" y="9.2" width="5.6" height="5.6" rx="1.5" fill="currentColor" stroke="none"/>'),
    "gate": S('<path d="M6 20V8.5M18 20V8.5"/><path d="M6 8.5h12"/><circle cx="12" cy="14" r="1.9"/><path d="M4 20h16"/>'),
    "message": S('<path d="M4.5 7A2.5 2.5 0 0 1 7 4.5h10A2.5 2.5 0 0 1 19.5 7v6a2.5 2.5 0 0 1-2.5 2.5h-7L4.5 20z"/>'),
    "work": S('<rect x="3.5" y="8" width="17" height="11.5" rx="3"/><path d="M9 8V6.5A2.5 2.5 0 0 1 11.5 4h1A2.5 2.5 0 0 1 15 6.5V8M3.5 13h17"/>'),
    "steer": S('<path d="M13.2 3.5 6.8 13.4h4.4L10.8 20.5l6.4-9.9h-4.4z"/>'),
    "pin": S('<path d="M9.5 10V6.8A2.3 2.3 0 0 1 11.8 4.5h.4a2.3 2.3 0 0 1 2.3 2.3V10l2 2.7v1.3h-9v-1.3z"/><path d="M12 14v5.8"/>'),
    "reverify": S('<path d="M19.5 12a7.5 7.5 0 1 1-2.1-5.2"/><path d="M17.7 3.4v3.4h3.4"/><rect x="10.2" y="10.2" width="3.6" height="3.6" rx="1.2" fill="currentColor" stroke="none"/>'),

    /* ---- 证据与账本 ---- */
    "event": S('<path d="M12 3.5v4.2M12 16.3v4.2"/><rect x="9.2" y="9.2" width="5.6" height="5.6" rx="1.8"/>'),
    "tool": S('<path d="M12 3.5v4.5"/><rect x="8" y="8" width="8" height="4" rx="1.5"/><path d="M6.5 16.5c1.8-.9 3.6-1.3 5.5-1.3s3.7.4 5.5 1.3M8 20h8"/>'),
    "chain": S('<rect x="3.5" y="8.5" width="9.5" height="7" rx="2.6"/><rect x="11" y="8.5" width="9.5" height="7" rx="2.6"/>'),
    "budget": S('<rect x="4.5" y="4.5" width="15" height="15" rx="3"/><path d="M4.5 12h15M12 4.5V12"/><rect x="6" y="6" width="4.5" height="4.5" rx="1.4" fill="currentColor" stroke="none"/>'),
    "copy": S('<rect x="9" y="9" width="10.5" height="10.5" rx="3"/><path d="M15.5 5.7V5.5A1.9 1.9 0 0 0 13.6 3.6H5.4A1.9 1.9 0 0 0 3.5 5.5v8.2a1.9 1.9 0 0 0 1.9 1.9h.2"/>'),
    "report": S('<path d="M6 7.5a3 3 0 0 1 3-3h6a3 3 0 0 1 3 3v9.7l-2 1.5-2-1.5-2 1.5-2-1.5-2 1.5L6 17.2z"/><path d="M9.2 9h5.6M9.2 12.2h3.4"/><circle cx="14.8" cy="12.4" r="1" fill="currentColor" stroke="none"/>'),
    "warn": S('<path d="M12 4.2 20.6 19a1.6 1.6 0 0 1-1.4 2.4H4.8A1.6 1.6 0 0 1 3.4 19z"/><path d="M12 10v4"/><path d="M12 17.4v.01"/>'),

    /* ---- 沿用几何（已符合家族语言） ---- */
    "arrow-down": S('<path d="M12 5v13.5"/><path d="M6.5 13 12 18.5 17.5 13"/>'),
    "arrow-left": S('<path d="M19 12H5.5"/><path d="M11 6.5 5.5 12l5.5 5.5"/>'),
    "arrow-right": S('<path d="M5 12h13.5"/><path d="M13 6.5 18.5 12 13 17.5"/>'),
    "chevron-down": S('<path d="M6.5 9.5 12 15l5.5-5.5"/>'),
    "chevron-up": S('<path d="M6.5 14.5 12 9l5.5 5.5"/>'),
    "ellipsis": S('<circle cx="5.5" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.1" fill="currentColor" stroke="none"/><circle cx="18.5" cy="12" r="1.1" fill="currentColor" stroke="none"/>'),
    "check": S('<path d="M5 12.8 10 17.5 19 6.8"/>'),
    "link": S('<rect x="3.5" y="8.5" width="9.5" height="7" rx="2.6"/><rect x="11" y="8.5" width="9.5" height="7" rx="2.6"/>'),
    "cube": S('<rect x="5" y="5" width="14" height="14" rx="3"/><path d="M5 12h14M12 5v14"/>'),
  };

  /* 状态符号（带语义色，由调用方决定使用场景） */
  const state = {
    "verify-ok": S('<circle cx="12" cy="12" r="8.5"/><path d="M8 12.4l2.8 2.8L16.4 9"/>', ' style="color:var(--ok)"'),
    "gate-stamp": S('<rect x="4.5" y="4.5" width="15" height="15" rx="4"/><path d="M8.5 12l2.5 2.5L15.5 9"/>', ' style="color:var(--ok)"'),
    "gate-denied": S('<rect x="4.5" y="4.5" width="15" height="15" rx="4"/><path d="M9 9l6 6M15 9l-6 6"/>', ' style="color:var(--bad)"'),
    "unknown-warn": S('<circle cx="12" cy="12" r="8.5"/><path d="M12 8v4.5"/><path d="M12 16v.01"/>', ' style="color:var(--warn)"'),
  };

  window.SEAL_ICONS = { still, state };
  /* 动效：状态符号出现时的一次性印章落章（180ms）；循环脉冲只给运行中节点 */
  const style = document.createElement("style");
  style.textContent = `
    .s-icon { display: block; }
    .s-icon.stamp-in { animation: sealStamp 180ms var(--ease, ease); }
    @keyframes sealStamp { 0% { transform: scale(1.25); opacity: .4; } 100% { transform: scale(1); opacity: 1; } }
    .s-icon.pulse-loop { animation: sealPulse 1.6s ease-in-out infinite; }
    @keyframes sealPulse { 0%, 100% { opacity: 1; } 50% { opacity: .45; } }
  `;
  document.head.appendChild(style);
})();
