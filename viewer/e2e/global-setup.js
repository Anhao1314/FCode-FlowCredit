"use strict";

/* E2E 全局装配：为 --repo 构造隔离副本。
 *
 * server 的运行时从 --repo 指向的 checkout 导入，审计数据固定读
 * <repo>/work/audit。为了不污染开发者本机的审计账本（处置会落链），
 * 这里建 /tmp 仓库：src 用符号链接指回真实源码，work/audit 复制一份
 * 一次性快照。runs 与 chat.sqlite 已由 FCODE_RUNS_DIR 隔离。
 */

import { cpSync, existsSync, mkdirSync, symlinkSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

export const E2E_REPO = join(tmpdir(), "fcode-e2e", "repo");

export default function globalSetup() {
  const projectRoot = resolve(fileURLToPath(import.meta.url), "../../..");
  const srcReal = join(projectRoot, "src");
  const auditReal = join(projectRoot, "work", "audit");

  rmSync(E2E_REPO, { recursive: true, force: true });
  mkdirSync(join(E2E_REPO, "work"), { recursive: true });
  if (!existsSync(join(E2E_REPO, "src"))) symlinkSync(srcReal, join(E2E_REPO, "src"));
  if (existsSync(auditReal)) {
    cpSync(auditReal, join(E2E_REPO, "work", "audit"), { recursive: true });
  }
  return E2E_REPO;
}
