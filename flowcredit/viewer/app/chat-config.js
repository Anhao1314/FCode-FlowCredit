"use strict";

/* 对话模型目录与 Provider 设置；主应用通过访问器共享最新配置。 */
export function createChatConfig(deps) {
  const {
    esc, num, chat, chatModelChoice, saveChatModelChoice, paintModelBadge, toast,
    adoptDefaultWorkModel,
    getHealth, getChatModels, setChatModels, getChatConfig, setChatConfig,
    getChatConfigSource, setChatConfigSource, getChatModelTouched,
  } = deps;
  const CHATCFG_FIELDS = {
    kimi: [
      ["model", "模型", "kimi-k2.6"], ["api_key", "API Key", ""],
      ["input_price_microusd_per_mtok", "输入价 µUSD/MTok", "950000"],
      ["output_price_microusd_per_mtok", "输出价 µUSD/MTok", "4000000"],
      ["price_as_of", "价格日期（YYYY-MM-DD）", "2026-10-25"],
      ["price_source", "价格来源", "moonshot-cn-published"],
      ["min_interval_seconds", "最小间隔（秒）", "22"], ["max_completion_tokens", "最大补全 tokens", "2048"],
    ],
    deepseek: [
      ["model", "模型", "deepseek-chat"], ["api_key", "API Key", ""],
      ["input_price_microusd_per_mtok", "输入价 µUSD/MTok", ""],
      ["output_price_microusd_per_mtok", "输出价 µUSD/MTok", ""],
      ["price_as_of", "价格日期（YYYY-MM-DD）", ""], ["price_source", "价格来源", ""],
      ["min_interval_seconds", "最小间隔（秒）", "0"], ["max_completion_tokens", "最大补全 tokens", "2048"],
    ],
    local: [
      ["base_url", "端点（OpenAI 兼容）", "http://127.0.0.1:11434/v1"],
      ["model", "模型", "fcode-qwen3-4b"], ["api_key", "API Key（可选）", ""],
      ["max_completion_tokens", "最大补全 tokens", "2048"],
    ],
  };

  /* 新增自定义端点的表单字段。价格审计字段是后端合同：免费端点显式
     0 价格 + not-applicable / operator-declared，否则 Work 侧会认为
     配置不完整并拒绝构造 provider。 */
  const ADD_FIELDS = [
    ["key", "端点 Key（ASCII 字母数字）", "myendpoint"],
    ["label", "显示名称", ""],
    ["base_url", "Base URL（OpenAI 兼容）", "http://127.0.0.1:8080/v1"],
    ["model", "模型", ""],
    ["api_key", "API Key（可选）", ""],
    ["input_price_microusd_per_mtok", "输入价 µUSD/MTok", "0"],
    ["output_price_microusd_per_mtok", "输出价 µUSD/MTok", "0"],
    ["price_as_of", "价格日期 / 不适用说明", "not-applicable"],
    ["price_source", "价格来源", "operator-declared"],
    ["min_interval_seconds", "最小间隔（秒）", "0"],
    ["max_completion_tokens", "最大补全 tokens", "2048"],
    ["supports_effort", "支持思考强度（1/0）", "0"],
  ];
  const ADD_DEFAULTS = Object.fromEntries(ADD_FIELDS.map(([name, , placeholder]) => [name, placeholder]));
  const RESERVED_PROVIDER_KEYS = ["mock", "kimi", "deepseek", "local"];

  function fieldsForProvider(provider) {
    if (CHATCFG_FIELDS[provider]) return CHATCFG_FIELDS[provider];
    return [
      ["label", "显示名称", provider], ["base_url", "Base URL", "http://127.0.0.1:8080/v1"],
      ["model", "模型", ""], ["api_key", "API Key（可选）", ""],
      ["input_price_microusd_per_mtok", "输入价 µUSD/MTok", "0"],
      ["output_price_microusd_per_mtok", "输出价 µUSD/MTok", "0"],
      ["price_as_of", "价格日期 / 不适用说明", "not-applicable"],
      ["price_source", "价格来源", "operator-declared"],
      ["min_interval_seconds", "最小间隔（秒）", "0"],
      ["max_completion_tokens", "最大补全 tokens", "2048"],
      ["supports_effort", "支持思考强度（1/0）", "0"],
    ];
  }

  function isCustomProvider(provider) {
    return !Object.hasOwn(CHATCFG_FIELDS, provider);
  }

  function adoptDefaultChatModel(defaultKey) {
    adoptDefaultWorkModel(defaultKey);
    if (getChatModelTouched()) return;
    const models = getChatModels();
    const preferred = models.find(m => m.key === defaultKey && m.available);
    if (preferred && chatModelChoice.key !== preferred.key) {
      chatModelChoice.key = preferred.key;
      saveChatModelChoice();
      paintModelBadge();
      return;
    }
    if (models.some(m => m.key === chatModelChoice.key && m.available)) return;
    const entry = models.find(m => m.available);
    if (entry && chatModelChoice.key !== entry.key) {
      chatModelChoice.key = entry.key;
      saveChatModelChoice();
      paintModelBadge();
    }
  }

  async function loadChatModels() {
    const health = getHealth();
    if (!health || !health.live) return;
    try {
      const data = await fetch("/api/chat/models", { cache: "no-store" }).then(r => r.ok ? r.json() : null);
      if (data && Array.isArray(data.models)) {
        setChatModels(data.models);
        adoptDefaultChatModel(data.default);
        if (chat.active) paintModelBadge();
      }
    } catch { /* 菜单打开时会重试 */ }
  }

  function renderChatModelMenu() {
    const menu = document.getElementById("chat-model-menu");
    if (!menu) return;
    const items = getChatModels().map(m => {
      const current = m.available && m.key === chatModelChoice.key;
      return `<button class="tpl-item model-item${current ? " active" : ""}${m.available ? "" : " disabled"}" data-chatmodel-pick="${esc(m.key)}"${m.available ? "" : " disabled"}><span class="t"><span>${esc(m.label)}</span><span class="m">${esc(m.model)}</span>${current ? '<span class="model-current-dot" aria-label="当前模型"></span>' : ""}</span><span class="d">${m.available ? (m.remote && m.price ? `${num(m.price.inputMicrousdPerMtok)} / ${num(m.price.outputMicrousdPerMtok)} µUSD·MTok⁻¹` : "离线 · 不产生外部计费") : esc(m.reason)}</span></button>`;
    }).join("");
    menu.innerHTML = items + `<div class="tpl-note">对话与委托运行共用同一模型目录与全局默认值</div><button class="tpl-item" type="button" data-chatmodel-settings><span class="t"><span>管理模型配置…</span></span><span class="d">设置 → 全局模型目录：填写 API Key 与价格</span></button>`;
  }

  async function syncChatConfig() {
    const health = getHealth();
    if (!health || !health.live) return;
    try {
      const data = await fetch("/api/settings/model-config", { cache: "no-store" }).then(r => r.ok ? r.json() : null);
      if (!data) return;
      setChatConfig(data.config || { providers: {}, default_model: "mock" });
      setChatConfigSource(data.source || null);
      if (Array.isArray(data.models)) setChatModels(data.models);
      renderChatConfig(true);
    } catch { /* 设置页会显示加载失败 */ }
  }

  function renderChatConfig(preserveInputs = false) {
    const defaultSlot = document.getElementById("chatcfg-default");
    const cardsSlot = document.getElementById("chatcfg-cards");
    if (!defaultSlot || !cardsSlot) return;
    const before = {};
    if (preserveInputs) {
      document.querySelectorAll("#chatcfg input").forEach(input => {
        if (input.type !== "password") before[input.id] = input.value;
      });
    }
    const chatConfig = getChatConfig(), chatModels = getChatModels(), chatConfigSource = getChatConfigSource();
    const providers = (chatConfig && chatConfig.providers) || {};
    const builtinLabels = { kimi: "Kimi 云端", deepseek: "DeepSeek 云端", local: "本地模型", mock: "离线 Mock" };
    const providerLabel = key => builtinLabels[key]
      || String((chatModels.find(m => m.key === key) || {}).label || "")
      || String(providers[key]?.label || "")
      || key;
    const currentDefault = (chatConfig && chatConfig.default_model) || "mock";
    // 全局默认从共享目录动态生成：mock 永远可选，其余按目录可用性；
    // 新增/删除自定义端点后目录会随之变化，这里不再维护静态名单。
    const defaultKeys = ["mock", ...chatModels.filter(m => m.key !== "mock").map(m => m.key)];
    defaultSlot.innerHTML = `<span class="chatcfg-label">全局默认模型</span>` + defaultKeys.map(key => {
      const entry = chatModels.find(m => m.key === key);
      const usable = key === "mock" || (entry && entry.available);
      return `<button type="button" class="chatcfg-radio${currentDefault === key ? " active" : ""}" data-chatcfg-default="${esc(key)}"${usable ? "" : " disabled"} title="${usable ? "" : esc(entry ? entry.reason : "未配置")}"><span class="dot${usable ? " ok" : ""}"></span>${esc(providerLabel(key))}</button>`;
    }).join("");
    const sourceLine = (() => {
      if (!chatConfigSource || !chatConfigSource.path) return "";
      const fmt = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : "—";
      const stale = chatConfigSource.stale ? `<b class="bad">配置文件比本进程新（本进程加载于 ${esc(fmt(chatConfigSource.loadedAt))}）——重启后生效</b>` : `<span class="dim">本进程已加载最新配置</span>`;
      return `<p class="chatcfg-source">配置文件：<code>${esc(chatConfigSource.path)}</code> · 最后修改 ${esc(fmt(chatConfigSource.fileMtime))} · ${stale}</p>`;
    })();
    const scopeNote = provider => {
      if (provider === "local") return "本地端点可在设置页修改；与对话、委托运行共用。";
      if (isCustomProvider(provider)) return "自定义 OpenAI 兼容端点：操作员显式信任边界；与对话、委托运行共用。";
      return "同时用于对话与委托运行（设置 &gt; 环境 &gt; 默认）。云端端点固定为官方地址，不能在这里改。";
    };
    const cardHtml = (provider, fields) => {
      const stored = providers[provider] || {};
      const entry = chatModels.find(m => m.key === provider);
      let status;
      if (entry && entry.available) status = `<span class="chatcfg-status ok">可用 · ${esc(entry.model)}</span>`;
      else {
        const reason = entry ? entry.reason : "未配置";
        const missing = reason.includes("缺少") ? (reason.split("缺少")[1].match(/、/g) || []).length + 1 : 1;
        status = `<span class="chatcfg-status bad" title="${esc(reason)}">未配置 · 缺 ${missing} 项</span>`;
      }
      const inputs = fields.map(([name, label, placeholder]) => {
        const value = name === "api_key" ? "" : String(stored[name] ?? "");
        const hint = name === "api_key" ? (stored.api_key_set ? `已保存 ${esc(stored.api_key_masked || "")} · 留空保持不变` : "未设置") : "";
        return `<label class="chatcfg-field"><span>${esc(label)}${hint ? `<i>${hint}</i>` : ""}</span><input id="chatcfg-${provider}-${name}" type="${name === "api_key" ? "password" : "text"}" value="${esc(value)}" placeholder="${esc(placeholder)}" autocomplete="off" spellcheck="false"></label>`;
      }).join("");
      const title = providerLabel(provider);
      const actions = `<button type="button" class="chat-act strong" data-chatcfg-save="${esc(provider)}">保存</button>`
        + `${stored.api_key_set ? `<button type="button" class="chat-act" data-chatcfg-clearkey="${esc(provider)}">清除密钥</button>` : ""}`
        + `${isCustomProvider(provider) ? `<button type="button" class="chat-act" data-chatcfg-delete="${esc(provider)}">删除端点</button>` : ""}`;
      return `<div class="chatcfg-card" data-chatcfg-card="${esc(provider)}"><div class="chatcfg-card-head"><b>${esc(title)}</b>${isCustomProvider(provider) ? `<code class="chatcfg-key">${esc(provider)}</code>` : ""}${status}</div><p class="chatcfg-scope">${scopeNote(provider)}</p><div class="chatcfg-grid">${inputs}</div><div class="chatcfg-actions">${actions}<span class="chatcfg-note">留空保存 = 清除该项（回落到环境变量/默认）</span></div></div>`;
    };
    const customKeys = Object.keys(providers).filter(isCustomProvider);
    const addCard = `<div class="chatcfg-card chatcfg-add" data-chatcfg-add-card><div class="chatcfg-card-head"><b>添加自定义端点</b></div><p class="chatcfg-scope">OpenAI 兼容端点：操作员显式信任边界；保存到本机 chat-config.json，与对话、委托运行共用。</p><div class="chatcfg-grid">${ADD_FIELDS.map(([name, label]) => `<label class="chatcfg-field"><span>${esc(label)}</span><input id="chatcfg-add-${esc(name)}" type="${name === "api_key" ? "password" : "text"}" value="${esc(ADD_DEFAULTS[name])}" autocomplete="off" spellcheck="false"></label>`).join("")}</div><div class="chatcfg-actions"><button type="button" class="chat-act strong" data-chatcfg-add>添加端点</button><span class="chatcfg-note">Key、Base URL、模型必填；免费端点保持 0 价格 + not-applicable / operator-declared</span></div></div>`;
    cardsSlot.innerHTML = sourceLine
      + Object.entries(CHATCFG_FIELDS).map(([provider, fields]) => cardHtml(provider, fields)).join("")
      + customKeys.map(provider => cardHtml(provider, fieldsForProvider(provider))).join("")
      + addCard;
    if (preserveInputs) {
      for (const [id, value] of Object.entries(before)) {
        const input = document.getElementById(id);
        if (input) input.value = value;
      }
    }
  }

  async function postConfig(body) {
    const response = await fetch("/api/settings/model-config", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    setChatConfig(data.config);
    setChatConfigSource(data.source || getChatConfigSource());
    if (Array.isArray(data.models)) setChatModels(data.models);
    return data;
  }

  async function saveChatConfig(provider) {
    const fields = CHATCFG_FIELDS[provider] || (isCustomProvider(provider) ? fieldsForProvider(provider) : null);
    if (!provider || !fields) return;
    const values = {};
    for (const [name] of fields) {
      const input = document.getElementById(`chatcfg-${provider}-${name}`);
      if (!input) continue;
      const value = String(input.value || "").trim();
      if (name === "api_key" && !value) continue;
      values[name] = value;
    }
    try {
      await postConfig({ provider, values });
      renderChatConfig(true);
      paintModelBadge();
      toast("已保存（本机）");
    } catch (error) { toast("保存失败：" + error.message); }
  }

  /* 新增自定义端点：Key 是环境变量前缀，后端校验 ASCII 字母数字且不与
     内置重名；base_url/model 缺一不可，价格审计字段随表单默认提交。
     成功后清空表单（renderChatConfig(true) 会先还原输入，清空必须在其后）。 */
  async function addChatConfigProvider() {
    const values = {};
    let key = "";
    for (const [name] of ADD_FIELDS) {
      const input = document.getElementById(`chatcfg-add-${name}`);
      if (!input) continue;
      const value = String(input.value || "").trim();
      if (name === "key") { key = value.toLowerCase(); continue; }
      if (name === "api_key" && !value) continue;
      values[name] = value;
    }
    if (!/^[a-z0-9]+$/.test(key) || RESERVED_PROVIDER_KEYS.includes(key)) {
      toast("端点 Key 只能是 ASCII 字母数字，且不能与内置端点重名");
      return;
    }
    if (!values.base_url || !values.model) {
      toast("Base URL 与模型必填");
      return;
    }
    try {
      const data = await postConfig({ provider: key, values });
      renderChatConfig(true);
      if (data.default) adoptDefaultChatModel(data.default);
      paintModelBadge();
      for (const [name] of ADD_FIELDS) {
        const input = document.getElementById(`chatcfg-add-${name}`);
        if (input) input.value = ADD_DEFAULTS[name];
      }
      toast(`已添加自定义端点 ${key}`);
    } catch (error) { toast("添加失败：" + error.message); }
  }

  /* 删除自定义端点。后端在删除的是全局默认时回落 mock；这里用返回的
     default 同步 Chat 选择与 Work 徽章，避免界面继续显示已删除的端点。 */
  async function deleteChatConfigProvider(provider) {
    if (!isCustomProvider(provider)) return;
    const wasDefault = (getChatConfig() || {}).default_model === provider;
    try {
      const data = await postConfig({ provider, delete_provider: true });
      renderChatConfig(true);
      if (data.default) adoptDefaultChatModel(data.default);
      paintModelBadge();
      toast(`已删除自定义端点 ${provider}${wasDefault ? " · 全局默认回落 mock" : ""}`);
    } catch (error) { toast("删除失败：" + error.message); }
  }

  async function clearChatConfigKey(provider) {
    try {
      await postConfig({ provider, values: { api_key: "" } });
      renderChatConfig(true);
      paintModelBadge();
      toast("密钥已清除（回落到环境变量）");
    } catch (error) { toast("清除失败：" + error.message); }
  }

  async function setDefaultChatModel(key) {
    try {
      await postConfig({ default_model: key });
      adoptDefaultWorkModel?.(key);
      if (!getChatModelTouched()) {
        const entry = getChatModels().find(m => m.key === key && m.available);
        if (entry) { chatModelChoice.key = key; saveChatModelChoice(); }
      }
      renderChatConfig(true);
      paintModelBadge();
      toast(`全局默认模型：${key}`);
    } catch (error) { toast("设置失败：" + error.message); }
  }

  return {
    adoptDefaultChatModel, loadChatModels, renderChatModelMenu, syncChatConfig,
    renderChatConfig, saveChatConfig, clearChatConfigKey, setDefaultChatModel,
    addChatConfigProvider, deleteChatConfigProvider,
  };
}
