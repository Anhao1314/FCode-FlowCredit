"use strict";

/* Composer 的两个浮层面（自 app.js 拆分切片 4c 迁入）：
 *
 * 1. 模型与思考强度选择器——本模块**独占** #work-model-menu（委托模型 +
 *    思考强度，数据来自服务端枚举）；对话模式的 #chat-model-menu 归
 *    chat-config.js。S1 分态后不存在共节点覆写。徽章按语境三画家：
 *    运行中只读显示实际模型 / 对话模式显示对话模型 / 空闲显示委托模型。
 * 2. “+”附加面板——真实委托输入：模板、本地项目、来源 URL / 语料选择、
 *    工具注册表（含 MCP 导入）、预算微调。手风琴单开，收起带 150ms 动画。
 *
 * 状态本体（attach/modelChoice/chatModelChoice/chat）按引用注入，
 * let 型目录（health/templates/tplKey/modelCatalog/modelBudget/chatModels）
 * 经 getter 注入。
 */

import { esc, num } from "../core/format.js";
import { parseVerificationCommand } from "./project-work.js";

export function createComposerPanel(deps) {
  const d = deps;

  /* ------------------------------------------ 模型与思考强度选择器 */

  function saveModelChoice() {
    localStorage.setItem("fa.model", JSON.stringify(d.getModelChoice()));
  }

  function adoptDefaultWorkModel(defaultKey) {
    const modelChoice = d.getModelChoice();
    if (modelChoice.touched || !defaultKey) return;
    const entry = d.getModelCatalog().find(model => model.key === defaultKey && model.available);
    if (!entry || modelChoice.key === entry.key) return;
    modelChoice.key = entry.key;
    saveModelChoice();
    paintModelBadge();
  }

  function liveRunEffortLabel(trace) {
    const rounds = (trace || {}).rounds || [];
    for (let i = rounds.length - 1; i >= 0; i--) {
      const effort = ((rounds[i].receipt || {}).metadata || {}).reasoning_effort;
      if (effort && d.EFFORT_LABEL[effort]) return d.EFFORT_LABEL[effort];
    }
    return "";
  }

  function paintModelBadge() {
    const labelEl = document.getElementById("model-label");
    const btn = document.getElementById("model-btn");
    paintFastToggle();
    if (!labelEl || !btn) return;
    const s = d.activeSession();
    if (s && s.running && !s.finished) return paintRunModelBadge(labelEl, btn, s);
    if (d.chat.active) return paintChatModelBadge(labelEl, btn);
    paintWorkModelBadge(labelEl, btn);
  }

  /* Fast 开关的点亮态。对话/治理视图隐藏按钮（paintChatChrome/paintGovChrome），
     这里只负责 Work 视图的 on/off 形态。 */
  function paintFastToggle() {
    const btn = document.getElementById("fast-btn");
    if (!btn) return;
    const on = d.getFastMode();
    btn.classList.toggle("on", on);
    btn.setAttribute("aria-pressed", on ? "true" : "false");
  }

  /* 运行中：徽章只读显示会话实际使用的模型与思考强度。 */
  function paintRunModelBadge(labelEl, btn, s) {
    const label = d.sessionModelLabel(s) || "运行中";
    const effort = liveRunEffortLabel(s.trace);
    labelEl.textContent = `${label}${effort ? " · " + effort : ""}`;
    btn.classList.add("readonly");
    btn.title = "当前运行实际使用的模型（只读）；选择器只对新委托生效";
  }

  /* 对话模式：徽章显示对话模型（chat-model-menu 的选择结果）。 */
  function paintChatModelBadge(labelEl, btn) {
    btn.classList.remove("readonly");
    const chatModels = d.getChatModels();
    const chatModelChoice = d.chatModelChoice;
    if (chatModels.length && chatModelChoice.key !== "mock"
        && !chatModels.some(m => m.key === chatModelChoice.key && m.available)) {
      chatModelChoice.key = "mock";
      d.saveChatModelChoice();
    }
    const chatEntry = chatModels.find(m => m.key === chatModelChoice.key)
      || chatModels.find(m => m.key === "mock");
    labelEl.textContent = !chatEntry || chatEntry.key === "mock"
      ? "offline-mock"
      : chatEntry.model;
    btn.title = "选择对话模型；在设置页填写 API 配置";
  }

  /* 空闲工作态：徽章显示委托模型与思考强度（work-model-menu 的选择结果）。 */
  function paintWorkModelBadge(labelEl, btn) {
    btn.classList.remove("readonly");
    const health = d.getHealth();
    if (!health || !health.live) {
      labelEl.textContent = "未连接 runtime";
      btn.title = "静态回放模式：composer 已整体禁用";
      return;
    }
    const modelChoice = d.getModelChoice();
    const modelCatalog = d.getModelCatalog();
    if (modelChoice.key !== "mock" && !modelCatalog.some(m => m.key === modelChoice.key && m.available)) {
      // 记忆的选择已不可用：回到 mock（菜单里保留 reason 供查看）
      modelChoice.key = "mock";
      saveModelChoice();
    }
    const entry = modelCatalog.find(m => m.key === modelChoice.key);
    labelEl.textContent = !entry || entry.key === "mock"
      ? "offline-mock"
      : `${entry.model}${entry.supportsEffort ? " · " + (d.EFFORT_LABEL[modelChoice.effort] || "中") : ""}`;
    btn.title = "选择模型与思考强度";
  }

  /* ------------------------------------------ “+”附加面板 */

  /* 通用委托模板（general-work）：附加面板换成“来源 URL”，不显示财务语料 */
  function isGeneralTemplate() {
    const tpl = d.getTemplates().find(t => t.key === d.getTplKey());
    return !!(tpl && tpl.module && tpl.module !== "finance-research");
  }

  function effectiveDraftBudget() {
    const template = d.getTemplates().find(t => t.key === d.getTplKey()) || {};
    if (isGeneralTemplate() && d.attach.project.path.trim()) {
      // 预览必须与实际执行一致：fast 信封(60/300/3M/1.2M)与严格信封不同，
      // 且 fast 不做价格折算收缩（服务端是单一事实源）。
      if (d.getFastMode()) {
        return { max_calls: 60, max_reads: 300, max_cost_microusd: 3_000_000, max_tokens: 1_200_000 };
      }
      return { max_calls: 40, max_reads: 160, max_cost_microusd: 1_000_000, max_tokens: 300_000 };
    }
    const entry = (d.getModelBudget().models || []).find(m => m.key === d.getModelChoice().key);
    return (entry && entry.budget) || template.limits || {};
  }

  function renderAttachPanel() {
    const panel = document.getElementById("attach-panel");
    const box = document.getElementById("composer-box");
    if (!panel) return;
    const btn = document.getElementById("composer-open");
    if (btn) {
      btn.classList.toggle("open", d.attach.open);
      btn.setAttribute("aria-expanded", d.attach.open ? "true" : "false");
      btn.title = d.attach.open ? "收起添加内容" : "添加内容";
    }
    box?.classList.toggle("open-attach", d.attach.open);
    if (d.attach.open) {
      clearTimeout(Number(panel.dataset.closeTimer || 0));
      panel.classList.remove("closing");
      panel.hidden = false;
    } else if (!panel.hidden) {
      // × 收起：先播 150ms 连体段收起动画，再真正 display:none
      clearTimeout(Number(panel.dataset.closeTimer || 0));
      panel.classList.add("closing");
      panel.dataset.closeTimer = String(setTimeout(() => {
        panel.hidden = true;
        panel.classList.remove("closing");
      }, 150));
      return;
    } else {
      return;
    }

    const limits = effectiveDraftBudget();
    const budget = d.attach.budget || {
      maxCalls: limits.max_calls || 0,
      maxReads: limits.max_reads || 0,
      maxCostMicrousd: limits.max_cost_microusd || 0,
    };
    const raised = d.attach.budget && (
      budget.maxCalls > (limits.max_calls || 0)
      || budget.maxReads > (limits.max_reads || 0)
      || budget.maxCostMicrousd > (limits.max_cost_microusd || 0)
    );
    const general = isGeneralTemplate();
    const sourceRows = d.attach.sources.map((url, index) => `
      <div class="attach-source">
        <span class="src-index">${index + 1}</span>
        <span class="src-url">${esc(url)}</span>
        <button class="src-remove" data-attach-source-remove="${index}" title="移除此来源">×</button>
      </div>`).join("");
    const sourcesGroup = `
      <section class="attach-group">
        <header><span>资料来源（URL）</span><span class="attach-note">${d.attach.sources.length}/5 · fetch_url 仅访问这些地址</span></header>
        ${sourceRows || '<div class="attach-empty">调研模式需要来源；脚本模式（"写脚本并运行"）可留空</div>'}
        ${d.attach.sources.length < 5 ? `
        <div class="attach-source-add">
          <input type="url" placeholder="https://example.com/page" data-attach-source-input value="${esc(d.attach.sourcesDraft)}">
          <button data-attach-source-add>添加</button>
        </div>` : ""}
        <div class="attach-rule">SSRF 防护在工具实现内强制：私网/环回地址一律拒绝，解析后校验实际连接 IP</div>
      </section>`;
    const mcpServers = d.attach.mcp.servers || [];
    const mcpGroup = mcpServers.length ? `
      <section class="attach-group">
        <header><span>MCP 导入</span><span class="attach-note">${mcpServers.filter(x => x.connected).length}/${mcpServers.length} 个 server 在线</span></header>
        ${mcpServers.map(server => `
          <div class="attach-tool">
            <div class="tool-line">
              <span class="name">${esc(server.server)}</span>
              <span class="effect">${server.connected ? `${server.tools.length} 工具` : "不可用"}</span>
            </div>
            <div class="summary">${server.connected
              ? esc(server.tools.join("  ·  ") || "无已导入工具")
              : esc(server.reason || "未连接")}</div>
            ${(server.refused || []).length ? `<div class="params">拒收 ${server.refused.length} 个：${esc(server.refused.map(r => r.tool).join(", "))}</div>` : ""}
          </div>`).join("")}
        <div class="attach-rule">导入即注册：门控/预算/收据同等生效；未标 readonly 的都过审批</div>
      </section>` : "";
    const docs = d.attach.corpus.map(doc => `
      <label class="attach-doc${d.attach.scope.has(doc.id) ? " checked" : ""}">
        <input type="checkbox" data-attach-doc="${esc(doc.id)}"${d.attach.scope.has(doc.id) ? " checked" : ""}>
        <span class="doc-name">${esc(doc.title)}</span>
        <span class="doc-kind">${esc(doc.kind)}</span>
        <span class="doc-period">${esc(doc.period)}</span>
      </label>`).join("");
    const tools = d.attach.tools.map(tool => {
      const required = Object.entries(tool.parameters || {})
        .filter(([, schema]) => schema && schema.required)
        .map(([name]) => name)
        .join(", ");
      return `<div class="attach-tool">
        <div class="tool-line"><span class="name">${esc(tool.name)}</span><span class="effect">${esc(tool.effect)}</span></div>
        <div class="summary">${esc(tool.summary)}</div>
        ${required ? `<div class="params">必填：${esc(required)}</div>` : '<div class="params">无必填参数</div>'}
      </div>`;
    }).join("");
    const tplKey = d.getTplKey();
    const tplItems = d.templateGroups().map(group =>
      group.items.map(t => `
        <button class="attach-tpl${t.key === tplKey ? " active" : ""}" data-attach-tpl="${esc(t.key)}">
          <span class="t">${esc(t.title.split("：")[0])}</span>
          <span class="d">${esc(t.story)}</span>
          ${t.key === tplKey ? '<span class="tick">✓ 当前</span>' : ""}
        </button>`).join("")
    ).join("");
    const curTpl = String((d.getTemplates().find(t => t.key === tplKey) || {}).title || tplKey).split("：")[0];
    const groups = [
      { key: "tpl", label: "委托模板", note: "决定授权、预算与故障注入",
        summary: esc(curTpl),
        body: `<div class="attach-tpls">${tplItems}</div>` },
      ...(general ? [{ key: "project", label: "本地项目 / 修 Bug", note: "真实仓库的隔离副本",
        summary: d.attach.project.path.trim() ? d.attach.project.path.trim().split("/").pop() : "未选择",
        body: d.projectWork.projectAttachBody(d.attach.project) }] : []),
      general
        ? { key: "sources", label: "外部来源", note: "fetch_url 可访问",
            summary: d.attach.sources.length ? `${d.attach.sources.length} 个来源` : "未添加",
            body: sourcesGroup }
        : { key: "sources", label: "资料选择", note: "不选 = 全语料（默认）",
            summary: d.attach.scope.size ? `已限定 ${d.attach.scope.size} 份` : "全语料（默认）",
            body: `<div class="attach-docs">${docs || '<div class="attach-empty">正在读取语料库…</div>'}</div>`
              + (d.attach.scope.size ? `<div class="attach-selected">已限定 ${d.attach.scope.size} 份：任务创建后读取范围真实受限</div>` : "") },
      { key: "tools", label: "工具注册表", note: "全部经门控与预算",
        summary: `${d.attach.tools.length} 个已注册工具`,
        body: `<div class="attach-tools">${tools || '<div class="attach-empty">正在读取注册表…</div>'}</div>${general ? mcpGroup : ""}<div class="attach-rule">工具由主机注册；任何调用须经门控与预算</div>` },
      { key: "budget", label: "预算微调", note: raised ? "已上调" : "默认 = 按模型价格适配的调用容量",
        summary: `${budget.maxCalls} calls · ${budget.maxReads} reads · ${num(budget.maxCostMicrousd)} µ`,
        body: `<div class="attach-budget">
          <label>calls <input type="number" min="1" inputmode="numeric" data-attach-budget="maxCalls" value="${Number(budget.maxCalls)}"></label>
          <label>reads <input type="number" min="1" inputmode="numeric" data-attach-budget="maxReads" value="${Number(budget.maxReads)}"></label>
          <label>µUSD <input type="number" min="1" inputmode="numeric" data-attach-budget="maxCostMicrousd" value="${Number(budget.maxCostMicrousd)}"></label>
        </div>
        <div class="attach-warning${raised ? " on" : ""}">${raised ? "上调将放宽治理上限；实际计费仍以收据为准" : "下调立即收紧；实际计费仍以收据为准"}</div>` },
    ];
    // 手风琴：四组默认收成单行摘要（当前配置一览），单开展开——对齐 kimi 的
    // 空间纪律：+ 菜单只占浮层，从不占据页面流。从「添加内容」进入时加一条
    // 返回入口，保持「入口四分类 → 详情」的两层关系可逆。
    const head = d.attach.fromMenu
      ? `<div class="attach-head"><button type="button" class="attach-back" data-attach-back>‹ 添加内容</button><span class="attach-head-note">本次委托</span></div>`
      : "";
    panel.innerHTML = head + groups.map(g => {
      const open = d.attach.openGroup === g.key;
      return `<section class="attach-group${open ? " open" : ""}">
        <button class="attach-sum" data-attach-sum="${g.key}" aria-expanded="${open}">
          <span class="sum-label">${esc(g.label)}</span>
          <span class="sum-value">${g.summary}</span>
          <span class="chev">${open ? "▾" : "▸"}</span>
        </button>
        ${open ? `<div class="attach-body">${g.body}</div>` : ""}
      </section>`;
    }).join("");
  }

  async function refreshAttachData() {
    const health = d.getHealth();
    const tplKey = d.getTplKey();
    if (!health || !health.live || !tplKey) return;
    const key = tplKey;
    const jobs = [];
    const modelBudget = d.getModelBudget();
    if (modelBudget.template !== key || !modelBudget.models) {
      jobs.push(d.fetchImpl(`/api/models?template=${encodeURIComponent(key)}`, { cache: "no-store" })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (data && Array.isArray(data.models) && d.getTplKey() === key) {
            d.setModelBudget({ template: key, models: data.models });
          }
        }).catch(() => {}));
    }
    if (d.attach.loadedTemplate !== key || !d.attach.corpus.length || !d.attach.tools.length) {
      jobs.push(Promise.all([
        d.fetchImpl(`/api/corpus?template=${encodeURIComponent(key)}`, { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
        d.fetchImpl(`/api/templates/${encodeURIComponent(key)}/tools`, { cache: "no-store" }).then(r => r.ok ? r.json() : null).catch(() => null),
      ]).then(([corpus, tools]) => {
        if (d.getTplKey() !== key) return;
        d.attach.corpus = corpus && Array.isArray(corpus.documents) ? corpus.documents : [];
        d.attach.tools = tools && Array.isArray(tools.tools) ? tools.tools : [];
        const ids = new Set(d.attach.corpus.map(doc => doc.id));
        d.attach.scope = new Set([...d.attach.scope].filter(id => ids.has(id)));
        d.attach.loadedTemplate = key;
      }));
    }
    const generalNow = d.getTemplates().find(t => t.key === key);
    if (generalNow && generalNow.module && generalNow.module !== "finance-research" && !d.attach.mcp.loaded) {
      jobs.push(d.fetchImpl("/api/mcp", { cache: "no-store" })
        .then(r => r.ok ? r.json() : null)
        .then(data => {
          if (data && Array.isArray(data.servers)) d.attach.mcp = { servers: data.servers, loaded: true };
        }).catch(() => { d.attach.mcp.loaded = true; }));
    }
    await Promise.all(jobs);
    if (d.attach.open) renderAttachPanel();
    if (d.getIntake().active) d.renderIntake();
  }

  function readAttachBudgetInputs() {
    const values = {};
    document.querySelectorAll("[data-attach-budget]").forEach(input => {
      values[input.dataset.attachBudget] = Number(input.value);
    });
    if (!values.maxCalls || !values.maxReads || !values.maxCostMicrousd) return;
    d.attach.budget = {
      maxCalls: values.maxCalls,
      maxReads: values.maxReads,
      maxCostMicrousd: values.maxCostMicrousd,
    };
    const raised = d.attach.budget.maxCalls > effectiveDraftBudget().max_calls
      || d.attach.budget.maxReads > effectiveDraftBudget().max_reads
      || d.attach.budget.maxCostMicrousd > effectiveDraftBudget().max_cost_microusd;
    const warning = document.querySelector(".attach-warning");
    if (warning) {
      warning.classList.toggle("on", raised);
      warning.textContent = raised
        ? "上调将放宽治理上限；实际计费仍以收据为准"
        : "下调立即收紧；实际计费仍以收据为准";
    }
  }

  function delegationSpecExtras() {
    const spec = {};
    if (isGeneralTemplate()) {
      // Fast（默认开）：一轮多动作 + 原生工具调用 + 免逐步审批；
      // 服务端据此走 worktree/信封/批量协议。关闭 = 严格路径。
      if (d.getFastMode()) spec.fast = true;
      if (d.attach.sources.length) spec.sources = [...d.attach.sources];
      if (d.attach.project.path.trim()) {
        const verification = parseVerificationCommand(d.attach.project.verification);
        if (!verification) throw new Error("请填写项目独立验证命令，例如 pytest -q");
        spec.mode = "open";
        spec.project = { path: d.attach.project.path.trim() };
        spec.verification = verification;
        spec.reportPath = "FCODE_RESULT.md";
      }
    } else {
      const allIds = new Set(d.attach.corpus.map(doc => doc.id));
      if (d.attach.scope.size && d.attach.scope.size !== allIds.size) {
        spec.scope = [...d.attach.scope];
      }
    }
    if (d.attach.budget) {
      spec.budget = { ...d.attach.budget };
    } else if (isGeneralTemplate() && d.attach.project.path.trim()) {
      // 项目模式:草稿预览的预算必须与实际执行一致(真实踩过 24 vs 8)
      const draft = effectiveDraftBudget();
      if (draft && draft.max_calls) {
        spec.budget = {
          maxCalls: draft.max_calls,
          maxReads: draft.max_reads,
          maxCostMicrousd: draft.max_cost_microusd,
          ...(draft.max_tokens ? { maxTokens: draft.max_tokens } : {}),
        };
      }
    }
    return spec;
  }

  /* 浮窗默认向上展开（Kimi 的锚点规则）；但舞台态 composer 贴近页面下沿时，
     向上会顶出画布顶部——最上面几行就点不到了。量一次：放不下就向下展开。
     水平锚点按 #model-btn 实测右缘写内联 right：CSS 的 54px 是常规 composer
     的常量，桌面变体 bar padding 更紧（51px），且窄屏媒体查询会改写 right。
     默认量工作菜单；对话菜单打开时由 app.js 传节点进来。 */
  function placeModelMenu(menu = document.getElementById("work-model-menu")) {
    if (!menu) return;
    const btn = document.getElementById("model-btn");
    const box = document.getElementById("composer-box");
    if (btn && box && box.contains(btn) && btn.getBoundingClientRect().width > 0) {
      // 绝对定位坐标相对包含块 padding box（composer-box 有 1px 边框），
      // 右对齐到按钮右缘；异常几何下不动内联值，退回 CSS 锚点。
      const border = 1;
      const right = Math.round(box.getBoundingClientRect().right - border
        - btn.getBoundingClientRect().right);
      if (right >= 0 && right < box.getBoundingClientRect().width / 2) {
        menu.style.right = `${right}px`;
        menu.style.left = "auto";
      }
    }
    menu.style.maxHeight = "";
    menu.style.overflowY = "";
    menu.style.overscrollBehavior = "";
    menu.classList.remove("drop-down");
    let rect = menu.getBoundingClientRect();
    if (rect.top < 8) {
      // 舞台态 composer 贴近底部：向下展开，但不能让菜单越过视口底边。
      // 按剩余高度限高并保留内部滚动，模型/思考强度仍然全部可达。
      menu.classList.add("drop-down");
      rect = menu.getBoundingClientRect();
      const available = Math.max(120, window.innerHeight - rect.top - 8);
      menu.style.maxHeight = `${available}px`;
      menu.style.overflowY = "auto";
      menu.style.overscrollBehavior = "contain";
    }
  }

  function renderModelMenu() {
    const menu = document.getElementById("work-model-menu");
    if (!menu) return;
    const modelCatalog = d.getModelCatalog();
    const modelChoice = d.getModelChoice();
    const items = modelCatalog.map(m => {
      const current = m.available && m.key === modelChoice.key;
      return `<button class="tpl-item model-item${current ? " active" : ""}${m.available ? "" : " disabled"}" data-model-pick="${esc(m.key)}"${m.available ? "" : " disabled"}>
        <span class="t"><span>${esc(m.label)}${current ? '<span class="check">✓</span>' : ""}</span><span class="m">${esc(m.model)}</span></span>
        <span class="d">${m.available ? (m.key === "mock" ? "离线确定性 mock · 演示与回归" : m.supportsEffort ? "云端模型 · 支持思考强度" : "本地推理") : esc(m.reason)}</span>
      </button>`;
    }).join("");
    // 思考强度块对"当前选中的模型"生效——菜单数据源是服务端枚举的
    // provider 清单，任何 supportsEffort 的条目（kimi 或主机声明的端点）
    // 选中时都能调档，不再写死单一 provider。
    const selected = modelCatalog.find(m => m.key === modelChoice.key);
    const effortHost = selected && selected.available && selected.supportsEffort
      ? selected : modelCatalog.find(m => m.key === "kimi" && m.available && m.supportsEffort);
    const effortBlock = effortHost ? `<div class="model-effort">
      <span class="me-head">思考强度${effortHost.key !== modelChoice.key ? ` · ${esc(effortHost.label)}` : ""}</span>
      <div class="me-opts">${d.EFFORT_LEVELS.map(level =>
        `<button class="me-opt${modelChoice.key === effortHost.key && modelChoice.effort === level ? " active" : ""}" data-effort-pick="${level}">${d.EFFORT_LABEL[level]}</button>`
      ).join("")}</div>
  
    </div>` : "";
    menu.innerHTML = items + effortBlock + `<div class="tpl-note">模型由操作者选择；门控 / 预算 / 审计对任何模型同等生效</div>`;
    if (!menu.hidden) placeModelMenu();
  }

  return {
    saveModelChoice, adoptDefaultWorkModel, liveRunEffortLabel, paintModelBadge,
    isGeneralTemplate, effectiveDraftBudget, renderAttachPanel, refreshAttachData,
    readAttachBudgetInputs, delegationSpecExtras, placeModelMenu, renderModelMenu,
  };
}
