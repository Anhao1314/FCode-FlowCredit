import test from "node:test";
import assert from "node:assert/strict";
import { createChatConfig } from "../app/chat-config.js";

function harness({ chatTouched = false } = {}) {
  const chatChoice = { key: "mock" };
  const work = [];
  const config = createChatConfig({
    esc: String,
    num: String,
    chat: { active: false },
    chatModelChoice: chatChoice,
    saveChatModelChoice() {},
    paintModelBadge() {},
    toast() {},
    adoptDefaultWorkModel: key => work.push(key),
    getHealth: () => ({ live: true }),
    getChatModels: () => [{ key: "mock", available: true }, { key: "kimi", available: true }],
    setChatModels() {},
    getChatConfig: () => null,
    setChatConfig() {},
    getChatConfigSource: () => null,
    setChatConfigSource() {},
    getChatModelTouched: () => chatTouched,
  });
  return { config, chatChoice, work };
}

test("the configured default model is shared by Chat and Work", () => {
  const { config, chatChoice, work } = harness();
  config.adoptDefaultChatModel("kimi");
  assert.equal(chatChoice.key, "kimi");
  assert.deepEqual(work, ["kimi"]);
});

test("an explicit chat model choice is preserved while Work still adopts its default", () => {
  const { config, chatChoice, work } = harness({ chatTouched: true });
  config.adoptDefaultChatModel("kimi");
  assert.equal(chatChoice.key, "mock");
  assert.deepEqual(work, ["kimi"]);
});
