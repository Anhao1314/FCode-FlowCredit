/* Pure DOM projection for the desktop chrome. No run, chat or network state. */

function focusWithoutScroll(element) {
  if (element && typeof element.focus === "function") element.focus({ preventScroll: true });
}

function narrowRail() {
  return typeof window !== "undefined" && window.innerWidth <= 1180;
}

/**
 * Apply sidebar/rail visibility and its accessibility contract.
 * The caller owns the persisted railOpen value; this function only projects UI.
 * At <=1180px the rail is a modal overlay; wider layouts keep it docked.
 */
export function applyChromeState(elements, { sidebar = null, rail = null } = {}) {
  const { app, sideToggle, railPanel, railToggle } = elements;
  if (!app) return;

  if (sidebar !== null) {
    const collapsed = !!sidebar;
    app.classList.toggle("side-collapsed", collapsed);
    if (sideToggle) {
      sideToggle.setAttribute("aria-label", collapsed ? "展开侧栏（⌘B）" : "折叠侧栏（⌘B）");
      sideToggle.setAttribute("aria-expanded", String(!collapsed));
      const tip = sideToggle.querySelector(".side-tip-label");
      if (tip) tip.textContent = collapsed ? "展开侧栏" : "折叠侧栏";
    }
  }

  if (rail !== null) {
    const open = !!rail;
    const wasOpen = app.classList.contains("rail-open");
    const modal = open && narrowRail();
    app.classList.toggle("rail-open", open);
    app.classList.toggle("rail-hidden", !open);
    if (railPanel) {
      railPanel.setAttribute("aria-hidden", String(!open));
      railPanel.inert = !open;
      if (modal) {
        railPanel.setAttribute("role", "dialog");
        railPanel.setAttribute("aria-modal", "true");
      } else {
        railPanel.removeAttribute?.("role");
        railPanel.removeAttribute?.("aria-modal");
      }
    }
    if (railToggle) railToggle.setAttribute("aria-expanded", String(open));
    if (modal && !wasOpen) focusWithoutScroll(railPanel.querySelector("[data-rail-close]"));
    else if (!open && wasOpen) focusWithoutScroll(railToggle);
  }
}
