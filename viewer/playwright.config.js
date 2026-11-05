"use strict";

/* FCode 浏览器 E2E（Playwright）。
 *
 * 全部用离线确定性 mock（offline-deterministic-mock / offline-chat-mock），
 * 不需要任何 API key；运行目录与 chat.sqlite 通过 FCODE_RUNS_DIR 隔离到
 * 临时目录，绝不触碰开发者本机 8642 实例的数据。
 *
 * 运行：cd viewer && npx playwright test
 */

import { tmpdir } from "node:os";
import { join } from "node:path";
import { mkdirSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";
import globalSetup, { E2E_REPO } from "./e2e/global-setup.js";

const PORT = 8610;
const runsDir = join(tmpdir(), "fcode-e2e", "runs");
mkdirSync(runsDir, { recursive: true });
const repoForServer = process.env.FCODE_E2E_REPO || E2E_REPO;

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,          // 共享一台 E2E server,串行保确定性
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
    viewport: { width: 1440, height: 900 },
    // 钉死离线 mock 模型:防止 adoptDefaultChatModel 把新档案带去真实
    // provider(会烧 API 额度,且 CI 无 key 必挂)
    storageState: "./e2e/mock-model-state.json",
  },
  globalSetup: "./e2e/global-setup.js",
  webServer: {
    // --repo 指向隔离副本(runtime 走符号链接,审计数据是复制快照);
    // 脚本化 chat 工具让"写文件审批卡"在离线确定性出现。
    command: `python3 -B viewer/server.py --repo "${repoForServer}" --port 8610 --pace 0`,
    cwd: "..",                     // flowcredit 仓库根(config 位于 viewer/)
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      ...process.env,
      FCODE_RUNS_DIR: runsDir,     // runs + chat.sqlite 全部落临时目录
      FCODE_CHAT_MOCK_TOOLS: JSON.stringify([
        { name: "write_file", arguments: { path: "e2e-note.txt", content: "written by e2e" } },
      ]),
    },
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
