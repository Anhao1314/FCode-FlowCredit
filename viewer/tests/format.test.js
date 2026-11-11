import test from "node:test";
import assert from "node:assert/strict";

import { esc, safeUrl } from "../core/format.js";

test("esc 覆盖单引号（属性注入的最后一块拼图）", () => {
  assert.equal(esc("<img src=x onerror='alert(1)'>"),
    "&lt;img src=x onerror=&#39;alert(1)&#39;&gt;");
});

test("safeUrl 只放行相对路径 / http(s) / 图片 data URL", () => {
  assert.equal(safeUrl("/runs/r1/shot.png"), "/runs/r1/shot.png");
  assert.equal(safeUrl("https://example.com/a.png"), "https://example.com/a.png");
  assert.equal(safeUrl("data:image/png;base64,AAA"), "data:image/png;base64,AAA");
  assert.equal(safeUrl("javascript:alert(1)"), "", "javascript: 必须被丢弃");
  assert.equal(safeUrl("data:text/html,<script>"), "", "非图片 data URL 丢弃");
  assert.equal(safeUrl("//evil.com/x"), "", "协议相对地址也拒绝");
  assert.equal(safeUrl(null), "");
});
