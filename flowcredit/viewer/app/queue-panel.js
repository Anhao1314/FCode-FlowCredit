"use strict";

/* 运行中插话队列的纯渲染层。
 *
 * 对齐本机 Kimi 3.2.27 的 message-queue 组件：序号、紧凑折叠态、详情浮层、
 * hover 操作、拖动/键盘调序。这里不读全局、不发请求；服务端队列仍是顺序的
 * 事实源，调用方在移动成功后用返回的 list 做乐观更新，再等 SSE 权威同步。
 */

const KNOWN_STATES = new Set(["queued", "draft", "delivered", "failed"]);

function stateLabel(msg) {
  if (msg.state === "failed") return `未送达 · ${msg.error || ""}`;
  if (msg.state === "draft") return "草稿 · 未入链";
  if (msg.state === "delivered") return "已入链";
  if (msg.steered) return "⚡ 插话 · 下一轮优先入链";
  return "排队中 · 下一轮入链";
}

export function queueMove(list, id, delta) {
  const items = Array.isArray(list) ? [...list] : [];
  const from = items.findIndex(item => String(item && item.id) === String(id));
  if (from < 0) return { list: items, moved: false, from: -1, to: -1 };
  const to = from + Number(delta || 0);
  if (!Number.isInteger(to) || to < 0 || to >= items.length || to === from) {
    return { list: items, moved: false, from, to: from };
  }
  const [item] = items.splice(from, 1);
  items.splice(to, 0, item);
  return { list: items, moved: true, from, to };
}

export function queueMoveTo(list, id, toIndex) {
  const items = Array.isArray(list) ? [...list] : [];
  const from = items.findIndex(item => String(item && item.id) === String(id));
  if (from < 0) return { list: items, moved: false, from: -1, to: -1 };
  const target = Math.max(0, Math.min(items.length - 1, Number(toIndex)));
  if (!Number.isFinite(target)) return { list: items, moved: false, from, to: from };
  const [item] = items.splice(from, 1);
  const to = Math.max(0, Math.min(items.length, Math.round(target)));
  items.splice(to, 0, item);
  return { list: items, moved: from !== to, from, to };
}

/* 拖放目标索引：先移除被拖动项，再按目标项的 before/after 计算插入点。
   返回值允许等于剩余列表长度，表示拖到队尾。 */
export function queueDropIndex(list, draggedId, targetId, after = false) {
  const remaining = (Array.isArray(list) ? list : []).filter(
    item => String(item && item.id) !== String(draggedId),
  );
  const index = remaining.findIndex(item => String(item && item.id) === String(targetId));
  if (index < 0) return null;
  return index + (after ? 1 : 0);
}

export function queuePanelHtml(msgs, state = {}, esc = String) {
  const list = Array.isArray(msgs) ? msgs : [];
  if (!list.length) return "";
  const collapsed = !!state.collapsed;
  const editingId = String(state.editingId || "");
  const detailId = String(state.detailId || "");
  const running = !!state.running && !state.finished;
  const toText = value => esc(String(value == null ? "" : value));

  const head = `<div class="q-head">
    <button type="button" class="q-toggle" data-q-act="toggle" aria-expanded="${collapsed ? "false" : "true"}" aria-label="${collapsed ? "展开排队插话" : "折叠排队插话"}">${collapsed ? "⌄" : "⌃"}</button>
    <span>排队插话 · ${list.length} 条</span>
    <span class="grow"></span>
    <span class="q-hint">${collapsed ? "点击展开 · 顺序即生效顺序" : "拖动或 Alt+↑/↓ 调序 · 顺序即生效顺序"}</span>
  </div>`;

  if (collapsed) {
    return `<div class="queued-msgs collapsed" data-queue-panel aria-label="排队插话已折叠">${head}</div>`;
  }

  const items = list.map((msg, index) => {
    const id = String(msg.id || "");
    const stateClass = KNOWN_STATES.has(msg.state) ? msg.state : "queued";
    const position = index + 1;
    if (id && id === editingId) {
      return `<div class="q-item editing" data-mid="${toText(id)}" data-q-index="${position}">
        <span class="q-index" aria-label="第 ${position} 条">${position}</span>
        <input class="q-edit" data-q-edit-input value="${toText(msg.text)}" maxlength="600" spellcheck="false" aria-label="编辑第 ${position} 条插话">
        <div class="q-acts">
          <button type="button" class="q-act" data-q-act="save" title="保存 (↵)">✓</button>
          <button type="button" class="q-act" data-q-act="cancel" title="取消 (Esc)">×</button>
        </div>
      </div>`;
    }

    const reorderable = stateClass === "queued" && !!msg.serverId;
    const actions = [];
    if (stateClass === "queued" && running) {
      actions.push(`<button type="button" class="q-act bolt${msg.steered ? " done" : ""}" data-q-act="steer" title="${msg.steered ? "已插话 · 下一轮优先处理" : "立即插入（下一轮优先处理）"}" aria-label="立即插入">⚡</button>`);
    }
    if (stateClass === "draft") {
      actions.push(`<button type="button" class="q-act" data-q-act="refill" title="回填输入框" aria-label="回填输入框">↩</button>`);
    } else {
      actions.push(`<button type="button" class="q-act" data-q-act="edit" title="编辑" aria-label="编辑">✎</button>`);
      if (reorderable && index > 0) actions.push(`<button type="button" class="q-act" data-q-act="moveup" title="上移 (Alt+↑)" aria-label="上移">↑</button>`);
      if (reorderable && index < list.length - 1) actions.push(`<button type="button" class="q-act" data-q-act="movedown" title="下移 (Alt+↓)" aria-label="下移">↓</button>`);
      actions.push(`<button type="button" class="q-act danger" data-q-act="remove" title="删除" aria-label="删除">×</button>`);
    }

    const detail = id && id === detailId
      ? `<div class="q-detail-pop" role="dialog" aria-label="第 ${position} 条插话详情">
          <div class="q-detail-head"><span>第 ${position} 条 · 完整内容</span><span class="grow"></span><button type="button" class="q-act" data-q-act="close-detail" aria-label="关闭详情">×</button></div>
          <div class="q-detail-text">${toText(msg.text)}</div>
        </div>`
      : "";

    return `<div class="q-item ${stateClass}${msg.steered ? " steered" : ""}${detail ? " detail-open" : ""}" data-mid="${toText(id)}" data-q-index="${position}" tabindex="0" aria-keyshortcuts="Alt+ArrowUp Alt+ArrowDown" draggable="${reorderable ? "true" : "false"}">
      <span class="q-position">
        <span class="q-index" aria-label="第 ${position} 条">${position}</span>
        ${reorderable ? '<button type="button" class="q-drag" data-q-act="drag" title="拖动调序 / Alt+↑↓" aria-label="拖动调序">⠿</button>' : ""}
      </span>
      <div class="q-main">
        <button type="button" class="q-text" data-q-act="detail" title="查看完整内容">${toText(msg.text)}</button>
        <div class="q-state">${toText(stateLabel(msg))}</div>
      </div>
      <div class="q-acts">${actions.join("")}</div>
      ${detail}
    </div>`;
  }).join("");

  return `<div class="queued-msgs" data-queue-panel aria-label="排队插话">${head}<div class="q-list">${items}</div></div>`;
}
