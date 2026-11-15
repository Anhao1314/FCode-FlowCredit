import test from "node:test";
import assert from "node:assert/strict";
import {
  mentionQueryAt, insertWorkspaceMention, workspaceMentionHtml,
} from "../core/workspace-mention.js";

test("mention starts at the beginning or after whitespace", () => {
  assert.deepEqual(mentionQueryAt("@app", 4), { start: 0, end: 4, query: "app" });
  const spaced = "看看 @src/ap";
  assert.deepEqual(mentionQueryAt(spaced, spaced.length), { start: 3, end: 10, query: "src/ap" });
  const bracketed = "看看 (@src/ap";
  assert.deepEqual(mentionQueryAt(bracketed, bracketed.length), { start: 4, end: 11, query: "src/ap" });
});

test("an email-like @ does not start a workspace reference", () => {
  assert.equal(mentionQueryAt("mail me a@b.com", 15), null);
  assert.equal(mentionQueryAt("看看 @app 再继续", 8), null);
});

test("a query cannot contain a second @", () => {
  assert.equal(mentionQueryAt("@a@b", 4), null);
});

test("insert replaces only the @token and leaves the rest", () => {
  const result = insertWorkspaceMention("看看 @src/ap 再改", 3, 10, "src/app.js");
  assert.equal(result.text, "看看 @src/app.js  再改");
  assert.equal(result.caret, "看看 @src/app.js ".length);
});

test("a path with spaces becomes one inline-code reference", () => {
  const result = insertWorkspaceMention("@old", 0, 4, "docs/my file.md");
  assert.equal(result.text, "`@docs/my file.md` ");
  assert.match(result.text, /^`@docs\/my file\.md` $/);
});

test("picker renders rows, active row and direct paths", () => {
  const html = workspaceMentionHtml({
    query: "app",
    index: 1,
    files: [{ path: "src/app.js", name: "app.js" }, { path: "src/app_test.js", name: "app_test.js" }],
  });
  assert.match(html, /@app/);
  assert.match(html, /data-mention-path="src\/app\.js"/);
  assert.match(html, /class="mention-item active" data-mention-path="src\/app_test\.js"/);
  assert.match(html, /src\//);
  assert.match(html, /Enter 插入/);
});

test("picker escapes paths and errors, and has loading/empty states", () => {
  const html = workspaceMentionHtml({
    query: "<q>",
    files: [{ path: 'src/<img>.js', name: "<img>.js" }],
    error: "<bad>",
  });
  assert.match(html, /&lt;q&gt;/);
  assert.match(html, /&lt;img&gt;\.js/);
  assert.match(html, /&lt;bad&gt;/);
  assert.doesNotMatch(html, /<img>/);
  assert.match(workspaceMentionHtml({ loading: true }), /正在查找/);
  assert.match(workspaceMentionHtml({ query: "zzz" }), /没有匹配的文件/);
});