"use strict";

/* E2E 全局装配：为 --repo 构造隔离副本。
 *
 * server 的运行时从 --repo 指向的 checkout 导入，审计数据固定读
 * <repo>/work/audit。为了不污染开发者本机的审计账本（处置会落链），
 * 这里建 /tmp 仓库：src 用符号链接指回真实源码，work/audit 复制一份
 * 一次性快照。runs 与 chat.sqlite 已由 FCODE_RUNS_DIR 隔离。
 */

import { cpSync, existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const E2E_REPO = join(tmpdir(), "fcode-e2e", "repo");

// 幂等装配：playwright 的 webServer 在 globalSetup 之前启动（冷启动时
// 隔离仓还不存在，Runtime 会判定 live mode OFF，一整批运行类用例连锁
// 失败）。配置文件在主进程加载时先调一次，globalSetup 再调时直接复用。
let prepared = false;

export function prepareE2ERepo() {
  if (prepared) return E2E_REPO;
  prepared = true;
  const projectRoot = resolve(fileURLToPath(import.meta.url), "../../..");
  const srcReal = join(projectRoot, "src");
  const auditReal = join(projectRoot, "work", "audit");

  rmSync(E2E_REPO, { recursive: true, force: true });
  mkdirSync(join(E2E_REPO, "work"), { recursive: true });
  // src 复制而不是符号链接：审计 CLI 用 Path(__file__).resolve() 定位
  // 规则集与默认输出目录，符号链接会把这两者都穿回真实仓库——
  // E2E 挖洞的 bundle 会污染开发者账本（真踩过）。
  cpSync(srcReal, join(E2E_REPO, "src"), { recursive: true });
  const rulesReal = join(projectRoot, "audit-rules");
  if (existsSync(rulesReal)) {
    cpSync(rulesReal, join(E2E_REPO, "audit-rules"), { recursive: true });
  }
  if (existsSync(auditReal)) {
    cpSync(auditReal, join(E2E_REPO, "work", "audit"), { recursive: true });
  }
  // 审计靶子也要能挖:fixtures/demo-target 是仓内可审计的源码样本,
  // S1 修洞闭环用例要真的跑一次 /挖洞(符号链接的 src 会被包含性检查拒绝)
  const fixturesReal = join(projectRoot, "fixtures", "demo-target");
  if (existsSync(fixturesReal)) {
    cpSync(fixturesReal, join(E2E_REPO, "fixtures", "demo-target"), { recursive: true });
  }

  // 模型钉死文件随端口生成:storageState 的 origin 必须与 webServer 一致
  const port = process.env.FCODE_E2E_PORT || "8610";
  writeFileSync(
    join(projectRoot, "viewer", "e2e", ".mock-model-state.json"),
    JSON.stringify({
      cookies: [],
      origins: [{
        origin: `http://127.0.0.1:${port}`,
        localStorage: [
          { name: "fa.model", value: '{"key":"mock"}' },
          { name: "fa.chatModel", value: '{"key":"mock","touched":true}' },
        ],
      }],
    }, null, 2) + "\n",
    "utf-8",
  );
  return E2E_REPO;
}

export default function globalSetup() {
  return prepareE2ERepo();
}
