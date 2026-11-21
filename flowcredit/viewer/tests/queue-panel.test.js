import test from "node:test";
import assert from "node:assert/strict";

import {
  queuePanelHtml, queueMove, queueMoveTo, queueDropIndex,
} from "../app/queue-panel.js";

const esc = value => String(value)
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;");

const messages = () => [
  { id: "q1", serverId: "s1", text: "先看第一份文件", state: "queued" },
  { id: "q2", serverId: "s2", text: "再核对 <script>alert(1)</script>", state: "queued", steered: true },
  { id: "q3", text: "本地草稿", state: "draft" },
];

test("queueMove: 上移/下移保持列表不可变并拒绝越界", () => {
  const original = messages();
  const up = queueMove(original, "q2", -1);
  assert.deepEqual(up.list.map(item => item.id), ["q2", "q1", "q3"]);
  assert.equal(up.moved, true);
  assert.deepEqual(original.map(item => item.id), ["q1", "q2", "q3"], "输入不被修改");

  const down = queueMove(original, "q1", 1);
  assert.deepEqual(down.list.map(item => item.id), ["q2", "q1", "q3"]);
  assert.equal(queueMove(original, "q1", -1).moved, false);
  assert.equal(queueMove(original, "q3", 1).moved, false);
});

test("queueMoveTo/queueDropIndex: 拖到队尾与队首的索引一致", () => {
  const original = messages();
  const tail = queueMoveTo(original, "q1", 2);
  assert.deepEqual(tail.list.map(item => item.id), ["q2", "q3", "q1"]);
  assert.equal(tail.to, 2);
  assert.equal(queueDropIndex(original, "q1", "q3", true), 2, "拖到 q3 之后等于队尾索引");
  assert.equal(queueDropIndex(original, "q3", "q1", false), 0, "拖到 q1 之前等于队首索引");
  assert.equal(queueDropIndex(original, "q1", "q1", false), null, "拖到自己不产生目标");
  const same = queueMoveTo(original, "q1", 0);
  assert.equal(same.moved, false);
});

test("queuePanelHtml: 序号、折叠托盘、hover 操作与转义", () => {
  const html = queuePanelHtml(messages(), {
    running: true, finished: false,
  }, esc);
  assert.match(html, /data-q-index="1"/);
  assert.match(html, /data-q-index="2"/);
  assert.match(html, /data-q-act="moveup"/);
  assert.match(html, /data-q-act="movedown"/);
  assert.match(html, /data-q-act="steer"/);
  assert.match(html, /data-q-act="remove"/);
  assert.match(html, /Alt\+↑\/↓ 调序/);
  assert.match(html, /draggable="true"/);
  assert.ok(!html.includes("<script>alert(1)</script>"), "队列文本必须转义");
  assert.match(html, /&lt;script&gt;/);
});

test("queuePanelHtml: 折叠态只留一行摘要；详情态展示完整文本", () => {
  const collapsed = queuePanelHtml(messages(), { collapsed: true }, esc);
  assert.match(collapsed, /queued-msgs collapsed/);
  assert.match(collapsed, /aria-expanded="false"/);
  assert.doesNotMatch(collapsed, /class="q-list"/);

  const detail = queuePanelHtml(messages(), { detailId: "q2", running: true }, esc);
  assert.match(detail, /q-detail-pop/);
  assert.match(detail, /第 2 条 · 完整内容/);
  assert.match(detail, /再核对 &lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(detail, /data-q-act="close-detail"/);
});

test("queuePanelHtml: 编辑态保留保存/取消契约", () => {
  const html = queuePanelHtml(messages(), { editingId: "q1", running: true }, esc);
  assert.match(html, /data-q-edit-input/);
  assert.match(html, /data-q-act="save"/);
  assert.match(html, /data-q-act="cancel"/);
  assert.match(html, /maxlength="600"/);
});
