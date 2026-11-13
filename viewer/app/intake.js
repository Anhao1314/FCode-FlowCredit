"use strict";

/* 委托入口（自 app.js 拆分切片 4a 迁入）。
 *
 * 用户先说话，后端 clarifier 再回复并给选项，前端不预设问卷。
 * 这里拥有 intake 会话状态对象（app.js 各处读写的都是同一份引用）：
 * 消息流、草稿快照（支持"上一步"）、偏好持久化（fa.intake）与
 * 模型预算预览（/api/models?template=）。app 侧状态经 getter 注入
 * （health/tplKey/approvalMode/modelChoice/modelBudget/settingsOpen
 * 都是会被别处重新赋值的 let），快照展开态（attach）按引用注入。
 */

import { esc, num } from "../core/format.js";
import { runModelFields } from "../core/model-choice.js";
import { parseVerificationCommand, projectDraftRows } from "./project-work.js";

const INTAKE_KEY = "fa.intake";

export function createIntake(deps) {
  const d = deps;

  const INTAKE_INTENTS = {
    compare: { label: "判断两期能不能直接比", short: "两期可比性" },
    restatement: { label: "重点看重述与口径变化", short: "重述与口径" },
    missing: { label: "资料可能不全，先看能否下结论", short: "资料不全" },
  };
  const INTAKE_OUTPUTS = {
    memo: { label: "一页结论 + 审计底稿", short: "结论 + 底稿" },
    trace: { label: "详细过程 + 原始 trace", short: "详细过程" },
    conclusion: { label: "只要结论", short: "只要结论" },
  };

  function loadIntakePrefs() {
    const raw = localStorage.getItem(INTAKE_KEY);
    try {
      const parsed = JSON.parse(raw || "{}");
      return {
        stored: !!raw,
        intent: INTAKE_INTENTS[parsed.intent] ? parsed.intent : "",
        output: INTAKE_OUTPUTS[parsed.output] ? parsed.output : "",
        approval: d.APPROVAL_MODES.some(([key]) => key === parsed.approval) ? parsed.approval : "",
      };
    } catch {
      return { stored: false, intent: "", output: "", approval: "" };
    }
  }

  const intakePrefs = loadIntakePrefs();
  const intake = {
    active: false,
    direct: false,
    messages: [],
    spec: {},
    choices: [],
    readyTask: null,
    thinking: false,
    snapshots: [],
    returnKey: null,
    returnLabel: "",
    origin: null,
  };

  function intakeBadge(choice) {
    if (intakePrefs.stored) {
      const preferred = intakePrefs[choice.field];
      if (preferred && preferred === choice.value) return "上次选择";
    }
    return choice.badge || "";
  }

  function intakeTemplateKey() {
    if (intake.readyTask && intake.readyTask.template) return intake.readyTask.template;
    // 通用委托是默认入口：在财务问卷出现之前，预算与草稿跟随当前模板。
    if (d.getTplKey() && d.isGeneralTemplate()) return d.getTplKey();
    return intake.spec.intent === "missing" ? "open-ended" : "full";
  }

  function intakeTemplate() {
    return d.getTemplates().find(t => t.key === intakeTemplateKey()) || {};
  }

  function intakeSavePrefs(spec) {
    const value = {
      intent: spec.intent || "",
      output: spec.output || "",
      approval: spec.approval || "",
    };
    try { localStorage.setItem(INTAKE_KEY, JSON.stringify(value)); } catch { /* quota */ }
    intakePrefs.stored = true;
    intakePrefs.intent = value.intent;
    intakePrefs.output = value.output;
    intakePrefs.approval = value.approval;
  }

  function intakeDraftHtml() {
    const template = intakeTemplate();
    const limits = template.limits || {};
    const intent = intake.spec.intent ? INTAKE_INTENTS[intake.spec.intent].label : "待选择";
    const output = intake.spec.output ? INTAKE_OUTPUTS[intake.spec.output].short : "待选择";
    const approval = intake.spec.approval
      ? d.approvalTitle(intake.spec.approval)
      : ((intake.readyTask || {}).approval ? d.approvalTitle(intake.readyTask.approval) : "待选择");
    const projectSelected = !!(template.module && template.module !== "finance-research") && !!d.attach.project.path.trim();
    const entry = (d.getModelBudget().models || []).find(m => m.key === d.getModelChoice().key);
    const scaled = !projectSelected && entry && entry.budget ? entry.budget : null;
    const operatorTuned = !!d.attach.budget;
    const shown = operatorTuned
      ? {
          max_calls: d.attach.budget.maxCalls,
          max_reads: d.attach.budget.maxReads,
          max_cost_microusd: d.attach.budget.maxCostMicrousd,
        }
      : (projectSelected
        ? { max_calls: 24, max_reads: 64, max_cost_microusd: 500000 }
        : (scaled || limits));
    const rescaled = scaled && (scaled.max_calls !== limits.max_calls
      || scaled.max_cost_microusd !== limits.max_cost_microusd
      || scaled.max_tokens !== limits.max_tokens);
    const budget = limits.max_calls
      ? `${num(shown.max_calls ?? limits.max_calls)} calls · ${num(shown.max_reads ?? limits.max_reads)} reads · ${num(shown.max_cost_microusd ?? limits.max_cost_microusd)} µUSD`
      : "—";
    const scopeNote = d.attach.scope.size && d.attach.scope.size !== d.attach.corpus.length
      ? `<span class="intake-scale-note">读取范围限定 ${d.attach.scope.size} 份资料</span>`
      : "";
    const scaleNote = operatorTuned
      ? `<span class="intake-scale-note">操作者已微调治理上限</span>`
      : (scaled && scaled.error
        ? `<span class="intake-scale-note">${esc(scaled.error)}</span>`
        : (rescaled && entry
          ? `<span class="intake-scale-note">已按 ${esc(entry.model)} 价格适配调用容量（硬上限不变）</span>`
          : ""));
    const general = !!(template.module && template.module !== "finance-research");
    const mode = general ? (((intake.readyTask || {}).spec || {}).mode || "") : "";
    const projectMode = general && !!d.attach.project.path.trim();
    const modeText = projectMode ? "项目：探索 → 修改 → 测试 → 修复 → 独立复跑"
      : mode === "script" ? "脚本：写 → 运行 → 修复 → 复跑"
      : mode === "research" ? "调研：抓取来源 → 写报告"
      : "按描述自动判断";
    const sourcesText = projectMode ? "本地项目隔离副本"
      : mode === "script" ? "工作区脚本，无需 URL"
      : `${d.attach.sources.length}/5 个 URL`;
    const headTag = general
      ? (template.title ? template.title.split("：")[0] : "通用委托")
      : "内置合成资料 · 恒星智算";
    const fast = !!(d.getFastMode && d.getFastMode());
    const detailRows = general
      ? `<div class="intake-row"><span>模式</span><b>${esc(modeText)}</b></div>
         <div class="intake-row"><span>来源</span><b>${esc(sourcesText)}</b>${d.attach.sources.length && !projectMode ? `<span class="intake-scale-note">只有 + 面板里列出的地址可被 fetch_url 访问</span>` : ""}</div>
         ${projectDraftRows(d.attach.project)}
         <div class="intake-row"><span>审批</span><b>${fast ? "Fast：免逐步审批" : esc(approval)}</b></div>`
      : `<div class="intake-row"><span>问题</span><b>${esc(intent)}</b></div>
         <div class="intake-row"><span>输出</span><b>${esc(output)}</b></div>
         <div class="intake-row"><span>审批</span><b>${esc(approval)}</b></div>
         <div class="intake-row"><span>资料</span><b>${d.attach.scope.size && d.attach.scope.size !== d.attach.corpus.length ? `限定 ${d.attach.scope.size} 份` : "全语料"}</b>${scopeNote}</div>`;
    const ready = !!intake.readyTask;
    return `<div class="intake-draft">
      <div class="intake-draft-head"><span>任务草稿</span><span class="intake-draft-tag">${esc(headTag)}</span></div>
      ${detailRows}
      <div class="intake-row"><span>预算</span><b>${esc(budget)}</b>${scaleNote}</div>
      <div class="intake-actions">
        <button class="a-btn" data-intake-cancel>取消</button>
        ${intake.snapshots.length ? `<button class="a-btn" data-intake-back>上一步</button>` : ""}
        <span class="grow"></span>
        <button class="a-btn strong" data-intake-start${ready ? "" : " disabled"}>${ready ? "开始委托" : "继续选择"}</button>
      </div>
    </div>`;
  }

  function intakeChoicesHtml(choices) {
    return `<div class="intake-choices">${choices.map(choice => {
      const badge = intakeBadge(choice);
      return `<button class="intake-choice" type="button"
        data-intake-choice data-intake-field="${esc(choice.field)}" data-intake-value="${esc(choice.value)}"
        data-intake-label="${esc(choice.label)}">
        <span class="t"><b>${esc(choice.label)}</b>${badge ? `<span class="intake-badge">${esc(badge)}</span>` : ""}</span>
        <small>${esc(choice.description || "")}</small>
      </button>`;
    }).join("")}</div>`;
  }

  function refreshModelBudget() {
    if (!d.getHealth() || !d.getHealth().live) return;
    const key = intakeTemplateKey();
    const mb = d.getModelBudget();
    if (mb.template === key && mb.models) return;
    d.fetchImpl(`/api/models?template=${encodeURIComponent(key)}`, { cache: "no-store" })
      .then(r => r.ok ? r.json() : null)
      .then(data => {
        if (!data || !Array.isArray(data.models)) return;
        d.setModelBudget({ template: key, models: data.models });
        if (intake.active) renderIntake();
      })
      .catch(() => { /* 预览保持模板草案数字，不影响创建 */ });
  }

  function renderIntake() {
    const main = document.getElementById("main");
    if (!intake.active) return;
    refreshModelBudget();

    /* 通用委托走任务确认，不复刻聊天线程：Work 的主语是任务，不是对话。
       用户原话作为任务引用，clarifier 的追问/选项放在草稿上方；财务问卷
       仍保留消息流（它确实需要多轮问答）。 */
    if (d.isGeneralTemplate()) {
      const taskText = [...intake.messages].reverse().find(message => message.role === "user")?.text || "";
      const lastAgent = [...intake.messages].reverse().find(message => message.role === "assistant");

      /* 直接开跑：提交后的短暂准备态，不显示草稿、不显示对话。就绪后由
         intakeRequest 自动创建运行；只有缺信息（choices）或创建被守卫拦下
         时才停在这里。 */
      if (intake.direct && !intake.readyTask && !intake.choices.length) {
        d.renderMain(`<div class="intake intake-work">
          <div class="intake-head">
            <div>
              <div class="intake-kicker"><span data-doodle="list-checks"></span> WORK / 新任务</div>
              <h1 class="intake-title">${intake.thinking ? "正在准备任务" : "任务已收到"}</h1>
              <p class="intake-sub">${intake.thinking ? "正在确认执行方式与预算；就绪后立即开始。" : "补充任务信息后继续。"}</p>
            </div>
          </div>
          <div class="intake-work-body">
            ${taskText ? `<div class="intake-task"><span>任务</span><p>${esc(taskText)}</p></div>` : ""}
            <p class="intake-work-note${intake.thinking ? " thinking" : ""}">${esc(intake.thinking ? "正在准备任务…" : (lastAgent?.text || "等待补充信息…"))}</p>
          </div>
        </div>`, { mode: "intake", scroll: "top" });
        d.hydrateDoodles(main);
        return;
      }

      const ready = !!intake.readyTask;
      const choices = !ready && !intake.thinking && intake.choices.length
        ? intakeChoicesHtml(intake.choices) : "";
      const note = !ready && lastAgent && !intake.thinking ? lastAgent.text : "";
      d.renderMain(`<div class="intake intake-work">
        <div class="intake-head">
          <div>
            <div class="intake-kicker"><span data-doodle="list-checks"></span> WORK / 任务确认</div>
            <h1 class="intake-title">${ready ? "确认后开始" : "还差一点信息"}</h1>
            <p class="intake-sub">${ready ? "任务在隔离副本中执行，原目录不改；预算、来源与审批已写入草稿。" : "补充一句，或从下面的选项里选一个。"}</p>
          </div>
        </div>
        <div class="intake-work-body">
          ${taskText ? `<div class="intake-task"><span>任务</span><p>${esc(taskText)}</p></div>` : ""}
          ${note ? `<p class="intake-work-note">${esc(note)}</p>` : ""}
          ${intake.thinking ? '<p class="intake-work-note thinking">正在理解你的任务…</p>' : ""}
          ${choices}
          ${intakeDraftHtml()}
        </div>
      </div>`, { mode: "intake", scroll: "top" });
      d.hydrateDoodles(main);
      return;
    }

    const messages = intake.messages.map((message, index) => {
      if (message.role === "user") {
        return `<div class="intake-msg user"><span class="who">你</span><p>${esc(message.text)}</p></div>`;
      }
      const last = index === intake.messages.length - 1;
      const thought = message.thought
        ? `<div class="intake-thought"><span>理解</span>${esc(message.thought)}</div>` : "";
      const choices = last && !intake.thinking && intake.choices.length
        ? intakeChoicesHtml(intake.choices) : "";
      return `<div class="intake-msg agent"><span class="who">FCode</span>
        <p class="intake-reply">${esc(message.text)}</p>${thought}${choices}</div>`;
    }).join("");
    const thinking = intake.thinking
      ? `<div class="intake-msg agent intake-thinking"><span class="who">FCode</span><p class="intake-reply">正在理解你的任务…</p></div>`
      : "";
    d.renderMain(`<div class="intake">
      <div class="intake-head">
        <div>
          <div class="intake-kicker"><span data-doodle="list-checks"></span> NEW DELEGATION</div>
          <h1 class="intake-title">先说清楚，再开始</h1>
          <p class="intake-sub">你先描述，我再给选项。最多三轮就能确认。</p>
        </div>
      </div>
      <div class="intake-thread">${messages}${thinking}${intakeDraftHtml()}</div>
    </div>`, { mode: "intake", scroll: "bottom" });
    d.hydrateDoodles(main);
  }

  function intakeSnapshot() {
    intake.snapshots.push({
      messages: intake.messages.map(message => ({ ...message })),
      spec: { ...intake.spec },
      choices: intake.choices.map(choice => ({ ...choice })),
      readyTask: intake.readyTask ? { ...intake.readyTask } : null,
    });
  }

  function startIntake(text = "", extras = null, options = {}) {
    if (!d.getHealth() || !d.getHealth().live) {
      d.toast("回放模式：用 python3 viewer/server.py 启动后才能创建委托");
      return;
    }
    const s = d.activeSession();
    if (s && s.running && !s.finished) {
      d.toast("当前任务还在运行：先停止，或等它结束后再开新委托");
      return;
    }
    d.leaveChat();
    if (d.getSettingsOpen()) d.closeSettingsPage(false);
    d.attach.open = false;
    d.renderAttachPanel();
    // paintChatChrome 在 chat 语境用内联样式藏掉 + 按钮；intake 草稿明确
    // 指向 + 面板（来源/预算），这里对称恢复，否则调研委托过不了来源守卫。
    const plus = document.getElementById("composer-open");
    if (plus) plus.style.display = "";
    intake.active = true;
    // 直接开跑只属于 Work 的任务提交；带对话上下文（chat-to-work）仍走确认页。
    intake.direct = !!options.direct && d.isGeneralTemplate() && !(extras && extras.origin);
    intake.messages = [];
    intake.spec = {};
    intake.origin = extras && extras.origin ? extras.origin : null;
    intake.choices = [];
    intake.readyTask = null;
    intake.thinking = false;
    intake.snapshots = [];
    intake.returnKey = d.store.key;
    intake.returnLabel = document.getElementById("tb-title").textContent || "当前视图";
    document.getElementById("app").classList.add("intake-mode");
    document.getElementById("tb-title").textContent = intake.direct ? "新任务 · 准备中" : "新委托 · 需求确认";
    document.getElementById("tb-chips").innerHTML = intake.direct
      ? '<span class="chip">新任务</span>'
      : (intake.origin
        ? `<span class="chip">需求确认</span><span class="chip">含对话上下文 · ${intake.origin.messages.length} 条</span>`
        : '<span class="chip">需求确认</span>');
    const ta = document.getElementById("composer-ta");
    ta.value = "";
    d.autosize(ta);
    ta.placeholder = d.isGeneralTemplate()
      ? "描述要完成的任务，Enter 交给 agent…"
      : "说说你想做什么，Enter 交给 agent…";
    const value = String(text || "").trim();
    if (!value) {
      intake.messages.push({
        role: "assistant",
        text: "先说说你想做什么。比如：调研一个主题并整理成 research.md；或者写个脚本跑通它。",
        thought: "还没有收到任务描述。",
        choices: [],
      });
      d.renderMode("intake");
      ta.focus();
      return;
    }
    intakeSnapshot();
    intake.messages.push({ role: "user", text: value });
    if (intake.direct) renderIntake();
    else d.renderMode("intake");
    intakeRequest(value, null);
  }

  function closeIntake(restore = true) {
    if (!intake.active) return;
    intake.active = false;
    intake.direct = false;
    intake.origin = null;
    document.getElementById("app").classList.remove("intake-mode");
    d.setComposerRunning(false);
    if (restore && intake.returnKey) {
      d.select(intake.returnKey, intake.returnLabel);
    } else if (restore) {
      d.renderMain(d.stageEmptyHtml(), { mode: "empty" });
      document.getElementById("tb-title").textContent = "FCode";
      document.getElementById("tb-chips").innerHTML = "";
    }
  }

  async function intakeRequest(message, choice) {
    if (intake.thinking) return;
    intake.thinking = true;
    intake.choices = [];
    renderIntake();
    const started = performance.now();
    try {
      const response = await d.fetchImpl("/api/intake", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message,
          choice,
          // 通用委托没有问卷，唯一决定自主度的就是输入框那个档位——随请求一起
          // 送过去，否则会产生"chip 显示自动、运行却在每步都停"的错觉。
          // 财务问卷的审批问题仍由问卷本身回答，不受这里影响。
          spec: d.isGeneralTemplate() ? { ...intake.spec, approval: d.getApprovalMode() } : intake.spec,
          history: intake.messages.slice(-16),
          template: d.getTplKey() || "",
        }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        throw new Error(error.error || `HTTP ${response.status}`);
      }
      const data = await response.json();
      // Keep the thinking state readable even when the local clarifier answers
      // instantly; a real model will naturally take longer.
      const elapsed = performance.now() - started;
      if (elapsed < 650) await new Promise(resolve => setTimeout(resolve, 650 - elapsed));
      intake.spec = data.spec || {};
      intake.choices = Array.isArray(data.choices) ? data.choices : [];
      intake.readyTask = data.task || null;
      intake.messages.push({
        role: "assistant",
        text: data.reply || "我在。继续说。",
        thought: data.thought || "",
        choices: intake.choices,
      });
      // Kimi 式 Work：任务一就绪就直接创建运行，不再让用户点一次「开始委托」。
      // 缺来源/验证命令时 startIntakeRun 会拦下并留在确认页，不消耗调用。
      if (intake.direct && intake.readyTask && !intake.choices.length) {
        await startIntakeRun();
      }
    } catch (error) {
      intake.messages.push({
        role: "assistant",
        text: `我没接上理解服务：${error.message}。你可以重试，或直接换一种说法。`,
        thought: "请求失败，未更新任务草稿。",
        choices: [],
      });
    } finally {
      intake.thinking = false;
      renderIntake();
    }
  }

  function intakeChoose(field, value, label) {
    if (!intake.active || intake.thinking) return;
    intakeSnapshot();
    intake.messages.push({ role: "user", text: label });
    intake.spec = { ...intake.spec, [field]: value };
    renderIntake();
    intakeRequest("", { field, value });
  }

  function intakeFreeText(text) {
    if (!intake.active || intake.thinking) return;
    const value = String(text || "").trim();
    if (!value) return;
    intakeSnapshot();
    intake.messages.push({ role: "user", text: value });
    renderIntake();
    intakeRequest(value, null);
  }

  function intakeBack() {
    const snapshot = intake.snapshots.pop();
    if (!snapshot) return;
    intake.messages = snapshot.messages;
    intake.spec = snapshot.spec;
    intake.choices = snapshot.choices;
    intake.readyTask = snapshot.readyTask;
    intake.thinking = false;
    renderIntake();
  }

  async function startIntakeRun() {
    if (!intake.active || !intake.readyTask) return;
    const task = intake.readyTask;
    const projectMode = !!d.attach.project.path.trim();
    const scriptMode = (task.spec && task.spec.mode) === "script";
    if (String(task.template) === d.getTplKey() && d.isGeneralTemplate() && !projectMode && !scriptMode && !d.attach.sources.length) {
      d.toast("调研委托至少需要 1 个来源 URL：点 + 添加（脚本或本地项目可留空）");
      return;
    }
    if (projectMode && !parseVerificationCommand(d.attach.project.verification)) {
      d.toast("请填写项目独立验证命令，例如 pytest -q");
      return;
    }
    const button = document.querySelector("[data-intake-start]");
    if (button) { button.disabled = true; button.textContent = "提交中…"; }
    try {
      // effort 只随支持思考强度的模型发送：服务端对不支持的组合是硬拒绝
      // (400 模型 X 不支持思考强度选择)，而 modelChoice.effort 会保留上一次
      // 支持 effort 的模型的档位——换到 deepseek 等模型后不能沿用。
      const modelFields = runModelFields(d.getModelChoice(), d.getModelCatalog());
      const response = await d.fetchImpl("/api/runs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          objective: task.objective,
          template: task.template,
          approval: task.approval,
          spec: { ...task.spec, ...d.delegationSpecExtras(), ...(intake.origin ? { origin_thread: intake.origin } : {}) },
          ...modelFields,
        }),
      });
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        const failure = new Error(error.error || `HTTP ${response.status}`);
        failure.code = error.code || "";
        throw failure;
      }
      const run = await response.json();
      intakeSavePrefs(task.spec);
      d.attach.scope.clear();
      d.attach.budget = null;
      d.attach.sources = [];
      d.attach.sourcesDraft = "";
      d.attach.mcp = { servers: d.attach.mcp.servers, loaded: false };
      d.renderAttachPanel();
      intake.active = false;
      intake.direct = false;
      intake.origin = null;
      document.getElementById("app").classList.remove("intake-mode");
      d.setComposerRunning(false);
      d.toast("委托已受理 · 开始执行");
      d.openLive(run.id, task.objective, run.template, d.getModelChoice().key);
      const s = d.activeSession();
      if (s) s.autoReport = task.spec.output === "memo";
      d.refreshRuns({ force: true });
    } catch (error) {
      if (error.code === "PROJECT_NOT_FOUND") {
        d.clearProjectContext();
        d.toast("项目目录已失效，请重新选择项目后重试；任务内容已保留");
      } else {
        d.toast("委托提交失败：" + error.message);
      }
      if (button) { button.disabled = false; button.textContent = "开始委托"; }
    }
  }

  return {
    intake, intakePrefs, renderIntake, startIntake, closeIntake,
    intakeChoose, intakeFreeText, intakeBack, startIntakeRun,
    intakeBadge, intakeTemplateKey, intakeTemplate, intakeSavePrefs,
    intakeRequest, intakeSnapshot, refreshModelBudget,
  };
}
