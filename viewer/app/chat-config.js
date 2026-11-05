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

  function adoptDefaultChatModel(defaultKey) {
    adoptDefaultWorkModel?.(defaultKey);
    if (getChatModelTouched() || !defaultKey) return;
    const entry = getChatModels().find(m => m.key === defaultKey && m.available);
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
    const menu = document.getElementById("model-menu");
    if (!menu) return;
    const items = getChatModels().map(m => {
      const current = m.available && m.key === chatModelChoice.key;
      return `<button class="tpl-item model-item${current ? " active" : ""}${m.available ? "" : " disabled"}" data-chatmodel-pick="${esc(m.key)}"${m.available ? "" : " disabled"}><span class="t"><span>${esc(m.label)}${current ? '<span class="check">✓</span>' : ""}</span><span class="m">${esc(m.model)}</span></span><span class="d">${m.available ? (m.remote && m.price ? `${num(m.price.inputMicrousdPerMtok)} / ${num(m.price.outputMicrousdPerMtok)} µUSD·MTok⁻¹` : "离线 · 不产生外部计费") : esc(m.reason)}</span></button>`;
    }).join("");
    menu.innerHTML = items + `<div class="tpl-note">对话模型；委托运行的模型仍由环境变量配置</div><button class="tpl-item" type="button" data-chatmodel-settings><span class="t"><span>管理模型配置…</span></span><span class="d">设置 → 模型与工具：填写 API Key 与价格</span></button>`;
  }

  async function syncChatConfig() {
    const health = getHealth();
    if (!health || !health.live) return;
    try {
      const data = await fetch("/api/settings/chat-config", { cache: "no-store" }).then(r => r.ok ? r.json() : null);
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
    const fallbacks = { kimi: "Kimi 云端", deepseek: "DeepSeek 云端", local: "本地模型", mock: "离线 Mock" };
    const currentDefault = (chatConfig && chatConfig.default_model) || "mock";
    defaultSlot.innerHTML = `<span class="chatcfg-label">默认对话模型</span>` + ["mock", "kimi", "deepseek", "local"].map(key => {
      const entry = chatModels.find(m => m.key === key);
      const usable = key === "mock" || (entry && entry.available);
      return `<button type="button" class="chatcfg-radio${currentDefault === key ? " active" : ""}" data-chatcfg-default="${esc(key)}"${usable ? "" : " disabled"} title="${usable ? "" : esc(entry ? entry.reason : "未配置")}"><span class="dot${usable ? " ok" : ""}"></span>${esc(fallbacks[key])}</button>`;
    }).join("");
    const sourceLine = (() => {
      if (!chatConfigSource || !chatConfigSource.path) return "";
      const fmt = seconds => seconds ? new Date(seconds * 1000).toLocaleString() : "—";
      const stale = chatConfigSource.stale ? `<b class="bad">配置文件比本进程新（本进程加载于 ${esc(fmt(chatConfigSource.loadedAt))}）——重启后生效</b>` : `<span class="dim">本进程已加载最新配置</span>`;
      return `<p class="chatcfg-source">配置文件：<code>${esc(chatConfigSource.path)}</code> · 最后修改 ${esc(fmt(chatConfigSource.fileMtime))} · ${stale}</p>`;
    })();
    cardsSlot.innerHTML = sourceLine + Object.entries(CHATCFG_FIELDS).map(([provider, fields]) => {
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
      return `<div class="chatcfg-card" data-chatcfg-card="${provider}"><div class="chatcfg-card-head"><b>${esc(fallbacks[provider])}</b>${status}</div><p class="chatcfg-scope">同时用于对话与委托运行（设置 &gt; 环境 &gt; 默认）。云端端点固定为官方地址，不能在这里改。</p><div class="chatcfg-grid">${inputs}</div><div class="chatcfg-actions"><button type="button" class="chat-act strong" data-chatcfg-save="${provider}">保存</button>${stored.api_key_set ? `<button type="button" class="chat-act" data-chatcfg-clearkey="${provider}">清除密钥</button>` : ""}<span class="chatcfg-note">留空保存 = 清除该项（回落到环境变量/默认）</span></div></div>`;
    }).join("");
    if (preserveInputs) {
      for (const [id, value] of Object.entries(before)) {
        const input = document.getElementById(id);
        if (input) input.value = value;
      }
    }
  }

  async function postConfig(body) {
    const response = await fetch("/api/settings/chat-config", {
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
    if (!provider || !CHATCFG_FIELDS[provider]) return;
    const values = {};
    for (const [name] of CHATCFG_FIELDS[provider]) {
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
      toast(`默认对话模型：${key}`);
    } catch (error) { toast("设置失败：" + error.message); }
  }

  return {
    adoptDefaultChatModel, loadChatModels, renderChatModelMenu, syncChatConfig,
    renderChatConfig, saveChatConfig, clearChatConfigKey, setDefaultChatModel,
  };
}
