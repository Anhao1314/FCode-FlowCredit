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
import { mkdirSync, rmSync } from "node:fs";
import { defineConfig, devices } from "@playwright/test";
import globalSetup, { E2E_REPO, prepareE2ERepo } from "./e2e/global-setup.js";

const PORT = Number(process.env.FCODE_E2E_PORT || 8610);
// 可覆盖端口与状态根，避免本机另一套 E2E 同时运行时互删 SQLite/隔离仓。
const stateRoot = process.env.FCODE_E2E_STATE_ROOT || join(tmpdir(), "fcode-e2e");
const runsDir = join(stateRoot, "runs");
// 每轮 E2E 从干净机器状态开始：清空上一轮的运行/对话/排程/记忆库。
// 只允许主进程做（TEST_WORKER_INDEX 未定义）：config 在每个 worker
// 里也会被加载，worker 里的删除会把运行中服务器的 sqlite 变成
// readonly；globalSetup 又跑在 webServer 启动之后。主进程 config
// 加载期是唯一早于 webServer 的窗口。
if (process.env.TEST_WORKER_INDEX === undefined) {
  rmSync(runsDir, { recursive: true, force: true });
  // 配置目录也必须隔离：否则 server 读开发者本机的 chat-config.json，
  // 真实默认模型(如 deepseek)会覆盖 E2E 固定 mock 的选择，且把真实
  // API key 带进离线用例。
  rmSync(join(stateRoot, "config"), { recursive: true, force: true });
  for (const db of ["scheduler.sqlite", "projects.sqlite", "chat.sqlite", "chat.sqlite-shm", "chat.sqlite-wal",
    "chat-memory.sqlite", "artifacts.sqlite", "governance.sqlite"]) {
    rmSync(join(stateRoot, db), { force: true });
  }
  // webServer 早于 globalSetup 启动；冷启动时隔离仓必须先存在，
  // 否则 Runtime 找不到 flowagent，整批运行类用例拿到 503。
  prepareE2ERepo();
}
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
    storageState: "./e2e/.mock-model-state.json",
  },
  globalSetup: "./e2e/global-setup.js",
  webServer: {
    // --repo 指向隔离副本(runtime 走符号链接,审计数据是复制快照);
    // 脚本化 chat 工具让"写文件审批卡"在离线确定性出现。
    command: `python3 -B viewer/server.py --repo "${repoForServer}" --port ${PORT} --pace 0`,
    cwd: "..",                     // flowcredit 仓库根(config 位于 viewer/)
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    timeout: 30_000,
    env: {
      ...process.env,
      FCODE_RUNS_DIR: runsDir,     // runs + chat.sqlite 全部落临时目录
      FCODE_CONFIG_DIR: join(stateRoot, "config"),  // 不读开发者本机 provider 配置
      FCODE_CHAT_MOCK_TOOLS: JSON.stringify([
        { name: "write_file", arguments: { path: "e2e-note.txt", content: "written by e2e" } },
      ]),
    },
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
});
