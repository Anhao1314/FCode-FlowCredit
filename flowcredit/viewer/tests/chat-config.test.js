import test from "node:test";
import assert from "node:assert/strict";

import { esc } from "../core/format.js";
import { createChatConfig } from "../app/chat-config.js";

/* 设置页 = 全局模型目录：一份目录、一处默认值，自定义端点可增删。
   这里用假 DOM 断言渲染合同，用假 fetch 断言 POST 合同。 */

const SNAPSHOT = {
  config: { version: "fcode.chat-config/v1", providers: {}, default_model: "mock" },
  models: [{ key: "mock", available: true, label: "offline-mock", model: "mock" }],
  default: "mock",
  source: null,
};

function harness({ chatTouched = false, models, chatConfig = null } = {}) {
  const chatChoice = { key: "mock" };
  const work = [];
  const toasts = [];
  const posts = [];
  const els = {};
  const state = {
    models: models || [
      { key: "mock", available: true, label: "offline-mock", model: "mock" },
      { key: "kimi", available: true, label: "Kimi 云端", model: "kimi-k2.6" },
    ],
    config: chatConfig,
  };
  let fetchImpl = null;
  globalThis.document = {
    getElementById: id => (els[id] ||= { id, innerHTML: "", value: "", dataset: {}, textContent: "", isConnected: true, type: "text" }),
    querySelectorAll: () => [],
    createElement: () => ({ id: "", innerHTML: "", value: "", dataset: {}, textContent: "" }),
  };
  globalThis.fetch = async (url, init) => {
    const call = { url: String(url), body: init ? JSON.parse(init.body) : null };
    posts.push(call);
    if (!fetchImpl) throw new Error(`unexpected fetch ${url}`);
    return fetchImpl(call);
  };
  const config = createChatConfig({
    esc, num: String,
    chat: { active: false },
    chatModelChoice: chatChoice,
    saveChatModelChoice() {},
    paintModelBadge() {},
    toast: msg => toasts.push(msg),
    adoptDefaultWorkModel: key => work.push(key),
    getHealth: () => ({ live: true }),
    getChatModels: () => state.models,
    setChatModels() {},
    getChatConfig: () => state.config,
    setChatConfig: value => { state.config = value; },
    getChatConfigSource: () => null,
    setChatConfigSource() {},
    getChatModelTouched: () => chatTouched,
  });
  return { config, chatChoice, work, toasts, posts, els, setFetch: fn => { fetchImpl = fn; }, state };
}

test("the shared default synchronizes chat and work", () => {
  const { config, chatChoice, work } = harness();
  config.adoptDefaultChatModel("kimi");
  assert.equal(chatChoice.key, "kimi");
  assert.deepEqual(work, ["kimi"]);
});

test("an explicit chat choice is preserved while work adopts the shared default", () => {
  const { config, chatChoice, work } = harness({ chatTouched: true });
  config.adoptDefaultChatModel("kimi");
  assert.equal(chatChoice.key, "mock");
  assert.deepEqual(work, ["kimi"]);
});

test("default radio comes from the shared catalog, not a hardcoded list", () => {
  const { config, els } = harness({
    models: [
      { key: "mock", available: true, label: "offline-mock", model: "mock" },
      { key: "kimi", available: true, label: "Kimi 云端", model: "kimi-k2.6" },
      { key: "myep", available: false, label: "My EP", model: "gpt-x", reason: "缺少 base_url" },
    ],
    chatConfig: { version: "fcode.chat-config/v1", providers: { myep: { label: "My EP" } }, default_model: "mock" },
  });
  config.renderChatConfig(false);
  const html = els["chatcfg-default"].innerHTML;
  assert.match(html, /全局默认模型/);
  assert.doesNotMatch(html, /默认对话模型/);
  assert.match(html, /data-chatcfg-default="myep"/);
  assert.match(html, /data-chatcfg-default="myep" disabled title="缺少 base_url"/, "不可用的目录项禁用并带原因");
  const cards = els["chatcfg-cards"].innerHTML;
  assert.match(cards, /data-chatcfg-card="myep"/, "自定义端点有卡");
  assert.match(cards, /data-chatcfg-delete="myep"/, "自定义端点可删除");
  assert.match(cards, /data-chatcfg-add>/, "有新增端点表单");
  assert.doesNotMatch(cards, /data-chatcfg-delete="kimi"/, "内置端点不可删除");
  assert.match(cards, /<code class="chatcfg-key">myep<\/code>/, "自定义卡显示 key");
});

test("rendered dynamic text is escaped", () => {
  const { config, els } = harness({
    models: [
      { key: "mock", available: true, label: "offline-mock", model: "mock" },
      { key: "evil", available: false, label: '<img src=x onerror=alert(1)>', model: "m", reason: "缺少 base_url" },
    ],
    chatConfig: { version: "fcode.chat-config/v1", providers: { evil: { label: "<b>bad</b>" } }, default_model: "mock" },
  });
  config.renderChatConfig(false);
  const all = els["chatcfg-default"].innerHTML + els["chatcfg-cards"].innerHTML;
  assert.doesNotMatch(all, /<img src=x/);
  assert.doesNotMatch(all, /<b>bad<\/b>/);
  assert.match(all, /&lt;b&gt;bad&lt;\/b&gt;/);
});

test("chat model menu describes the shared catalog, not the env-only old contract", () => {
  const { config, els } = harness();
  config.renderChatModelMenu();
  const html = els["chat-model-menu"].innerHTML;
  assert.match(html, /共用同一模型目录与全局默认值/);
  assert.doesNotMatch(html, /仍由环境变量配置/);
});

test("saving a custom provider posts its fields", async () => {
  const { config, posts, els, setFetch, toasts } = harness({
    models: [
      { key: "mock", available: true, label: "offline-mock", model: "mock" },
      { key: "myep", available: true, label: "My EP", model: "gpt-x" },
    ],
    chatConfig: {
      version: "fcode.chat-config/v1",
      providers: { myep: { label: "My EP", base_url: "http://127.0.0.1:9/v1", model: "gpt-x" } },
      default_model: "mock",
    },
  });
  els["chatcfg-myep-base_url"] = { id: "chatcfg-myep-base_url", innerHTML: "", value: "http://127.0.0.1:9/v1", dataset: {}, textContent: "", isConnected: true, type: "text" };
  els["chatcfg-myep-model"] = { id: "chatcfg-myep-model", innerHTML: "", value: "gpt-x", dataset: {}, textContent: "", isConnected: true, type: "text" };
  setFetch(() => ({ ok: true, json: async () => SNAPSHOT }));
  await config.saveChatConfig("myep");
  const call = posts.find(p => p.url === "/api/settings/model-config");
  assert.ok(call, "保存走共享配置端点");
  assert.equal(call.body.provider, "myep");
  assert.equal(call.body.values.base_url, "http://127.0.0.1:9/v1");
  assert.equal(call.body.values.model, "gpt-x");
  assert.match(toasts.at(-1), /已保存/);
});

test("adding a custom endpoint posts the key with price audit fields and clears the form", async () => {
  const { config, posts, els, setFetch, toasts } = harness();
  for (const [id, value] of [
    ["chatcfg-add-key", "MyEp"], ["chatcfg-add-label", "我的端点"],
    ["chatcfg-add-base_url", "http://127.0.0.1:9/v1"], ["chatcfg-add-model", "gpt-x"],
    ["chatcfg-add-input_price_microusd_per_mtok", "0"],
    ["chatcfg-add-output_price_microusd_per_mtok", "0"],
    ["chatcfg-add-price_as_of", "not-applicable"],
    ["chatcfg-add-price_source", "operator-declared"],
    ["chatcfg-add-min_interval_seconds", "0"],
    ["chatcfg-add-max_completion_tokens", "2048"],
    ["chatcfg-add-supports_effort", "0"],
    ["chatcfg-add-completion_field", "max_completion_tokens"],
  ]) els[id] = { id, innerHTML: "", value, dataset: {}, textContent: "", isConnected: true, type: id === "chatcfg-add-api_key" ? "password" : "text" };
  setFetch(() => ({ ok: true, json: async () => SNAPSHOT }));
  await config.addChatConfigProvider();
  const call = posts.find(p => p.url === "/api/settings/model-config");
  assert.ok(call, "新增走共享配置端点");
  assert.equal(call.body.provider, "myep");
  assert.equal(call.body.values.base_url, "http://127.0.0.1:9/v1");
  assert.equal(call.body.values.model, "gpt-x");
  assert.equal(call.body.values.price_as_of, "not-applicable");
  assert.equal(call.body.values.price_source, "operator-declared");
  assert.equal(call.body.values.input_price_microusd_per_mtok, "0");
  assert.equal(call.body.values.supports_effort, "0");
  assert.equal(call.body.values.completion_field, "max_completion_tokens");
  assert.equal(els["chatcfg-add-key"].value, "myendpoint", "成功后表单回到默认值");
  assert.match(toasts.at(-1), /已添加自定义端点 myep/);
});

test("adding rejects reserved or non-alphanumeric keys without a POST", async () => {
  for (const key of ["kimi", "my ep", "my-ep"]) {
    const { config, posts, els, toasts } = harness();
    els["chatcfg-add-key"] = { id: "chatcfg-add-key", innerHTML: "", value: key, dataset: {}, textContent: "", isConnected: true, type: "text" };
    els["chatcfg-add-base_url"] = { id: "chatcfg-add-base_url", innerHTML: "", value: "http://127.0.0.1:9/v1", dataset: {}, textContent: "", isConnected: true, type: "text" };
    els["chatcfg-add-model"] = { id: "chatcfg-add-model", innerHTML: "", value: "m", dataset: {}, textContent: "", isConnected: true, type: "text" };
    await config.addChatConfigProvider();
    assert.equal(posts.length, 0, `${key} 不应发请求`);
    assert.match(toasts.at(-1), /端点 Key 只能是 ASCII 字母数字/);
  }
});

test("deleting a custom provider posts delete_provider and adopts the fallback default", async () => {
  const { config, posts, work, setFetch, toasts } = harness({
    models: [
      { key: "mock", available: true, label: "offline-mock", model: "mock" },
      { key: "myep", available: true, label: "My EP", model: "gpt-x" },
    ],
    chatConfig: {
      version: "fcode.chat-config/v1",
      providers: { myep: { label: "My EP", base_url: "http://127.0.0.1:9/v1", model: "gpt-x" } },
      default_model: "myep",
    },
  });
  setFetch(() => ({ ok: true, json: async () => ({ ...SNAPSHOT, default: "mock" }) }));
  await config.deleteChatConfigProvider("myep");
  const call = posts.find(p => p.url === "/api/settings/model-config");
  assert.ok(call);
  assert.equal(call.body.provider, "myep");
  assert.equal(call.body.delete_provider, true);
  assert.deepEqual(work, ["mock"], "默认回落 mock 时同步 Work 徽章");
  assert.match(toasts.at(-1), /已删除自定义端点 myep · 全局默认回落 mock/);
});

test("built-in providers cannot be deleted through the settings path", async () => {
  const { config, posts } = harness();
  await config.deleteChatConfigProvider("kimi");
  assert.equal(posts.length, 0);
});

test("advanced capability fields and connection test are rendered", () => {
  const { config, els } = harness();
  config.renderChatConfig(false);
  const html = els["chatcfg-cards"].innerHTML;
  assert.match(html, /chatcfg-advanced/);
  assert.match(html, /chatcfg-kimi-completion_field/);
  assert.match(html, /chatcfg-kimi-max_context_tokens/);
  assert.match(html, /data-chatcfg-test="kimi"/);
  assert.match(html, /chatcfg-add-template/);
});

test("provider templates fill endpoint and request-shape fields", () => {
  const { config, els, toasts } = harness();
  config.applyChatConfigTemplate("glm");
  assert.equal(els["chatcfg-add-base_url"].value, "https://open.bigmodel.cn/api/paas/v4");
  assert.equal(els["chatcfg-add-completion_field"].value, "max_tokens");
  assert.equal(els["chatcfg-add-json_mode"].value, "0");
  assert.match(toasts.at(-1), /GLM 模板/);
});

test("connection test posts the provider and reports the result", async () => {
  const { config, posts, setFetch, toasts } = harness();
  setFetch(() => ({
    ok: true,
    json: async () => ({ ok: true, provider: "kimi", model: "kimi-k2.6", reply: "OK" }),
  }));
  await config.testChatConfig("kimi");
  const call = posts.find(p => p.url === "/api/settings/model-config/test");
  assert.ok(call);
  assert.equal(call.body.provider, "kimi");
  assert.match(toasts.at(-1), /连接成功/);
});
