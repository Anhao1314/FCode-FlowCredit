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

test("the chat default does not overwrite the run-model default", () => {
  const { config, chatChoice, work } = harness();
  config.adoptDefaultChatModel("kimi");
  assert.equal(chatChoice.key, "kimi");
  // Run models are a different surface (generic providers included); their
  // default comes from /api/models, not from the chat settings page.
  assert.deepEqual(work, []);
});

test("an explicit chat choice is preserved without touching the run model", () => {
  const { config, chatChoice, work } = harness({ chatTouched: true });
  config.adoptDefaultChatModel("kimi");
  assert.equal(chatChoice.key, "mock");
  assert.deepEqual(work, []);
});
