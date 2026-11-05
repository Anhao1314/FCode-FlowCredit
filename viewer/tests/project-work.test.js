import test from "node:test";
import assert from "node:assert/strict";
import { createProjectWork, directWorkRunSpec, isProjectTrace, parseVerificationCommand, projectDraftRows, projectRunSpec } from "../app/project-work.js";
import { esc } from "../core/format.js";

test("project verification parses arguments without a shell", () => {
  assert.deepEqual(parseVerificationCommand("pytest -q tests"), {
    name: "项目测试：pytest -q tests", command: "pytest", args: ["-q", "tests"], expectExitCode: 0, timeoutSeconds: 120,
  });
  assert.deepEqual(parseVerificationCommand('python3 "tests/a test.py"').args, ["tests/a test.py"]);
  assert.equal(parseVerificationCommand(""), null);
});

test("project UI escapes paths and separates file changes from patch", () => {
  const ui = createProjectWork({ esc });
  const body = ui.projectAttachBody({ path: "/tmp/<project>", verification: "pytest -q" });
  assert.match(body, /&lt;project&gt;/);
  assert.match(body, /隔离副本/);
  const result = ui.projectResultHtml({ task: { id: "task-1" }, project: {
    name: "demo", source: "/tmp/demo", filesChanged: [{ status: "M", path: "app.py" }],
    patch: "@@ -1 +1 @@\n-old\n+new", patchAvailable: true,
  } });
  assert.match(result, /data-project-file="app.py"/);
  assert.match(result, /下载 changes.patch/);
  assert.match(result, /project-diff-line del/);
  assert.match(projectDraftRows({ path: "/tmp/demo", verification: "pytest -q" }), /Runtime 独立复跑/);
  assert.equal(ui.projectContextHtml({ task: { binding: { spec: {} } } }), "");
});

test("Work starts directly with or without a project and keeps project settings persistent", () => {
  const ui = createProjectWork({ esc });
  const composer = ui.projectComposerHtml({ path: "/tmp/demo", verification: "pytest -q" });
  assert.match(composer, /data-project-context-edit/);
  assert.match(composer, /隔离副本/);
  const emptyComposer = ui.projectComposerHtml({ path: "", verification: "pytest -q" });
  assert.match(emptyComposer, /添加项目（可选）/);
  const editor = ui.projectComposerHtml({ path: "/tmp/demo", verification: "pytest -q" }, true);
  assert.match(editor, /data-project-context-done/);
  assert.match(editor, /data-project-path/);
  assert.match(editor, /data-project-verification/);
  assert.match(editor, /role="dialog"/);
  const spec = projectRunSpec({ path: "/tmp/demo", verification: "python3 -m unittest -v" });
  assert.equal(spec.mode, "open");
  assert.deepEqual(spec.project, { path: "/tmp/demo" });
  assert.equal(spec.verification.command, "python3");
  assert.deepEqual(spec.verification.args, ["-m", "unittest", "-v"]);
  assert.deepEqual(directWorkRunSpec({ path: "", verification: "" }), { mode: "open", reportPath: "FCODE_RESULT.md" });
  const directProject = directWorkRunSpec({ path: "/tmp/demo", verification: "pytest -q" });
  assert.deepEqual(directProject.project, { path: "/tmp/demo" });
  assert.equal(directProject.verification.command, "pytest");
  assert.throws(() => projectRunSpec({ path: "/tmp/demo", verification: "" }), /验证命令/);
});

test("project context and outcome are escaped and link to task views", () => {
  const ui = createProjectWork({ esc });
  const trace = {
    task: { id: "task-run-1", binding: { spec: { project: { path: "/tmp/<demo>" }, verification: { command: "pytest", args: ["-q"] } } } },
    terminal: { status: "succeeded" },
    project: { filesChanged: [{ path: "app.py", status: "M" }] },
  };
  const context = ui.projectContextHtml(trace);
  assert.match(context, /&lt;demo&gt;/);
  assert.match(context, /pytest -q/);
  const outcome = ui.projectOutcomeHtml(trace);
  assert.match(outcome, /任务已完成/);
  assert.match(outcome, /共变更 1 个文件/);
  assert.match(outcome, /data-work-tab="changes"/);
  assert.match(outcome, /data-work-tab="files"/);
  assert.doesNotMatch(outcome, /判断面/);
  const failed = ui.projectOutcomeHtml({ terminal: { status: "failed", failure: { message: "<失败>" } }, project: {} });
  assert.match(failed, /&lt;失败&gt;/);
  assert.match(failed, /data-act="retry"/);
});

test("project work views expose execution, changes and a complete workspace", () => {
  const ui = createProjectWork({ esc });
  const trace = {
    task: { id: "task-run-1", binding: { spec: { project: { path: "/tmp/demo" } } } },
    project: { filesChanged: [{ path: "app.py", status: "M" }] },
    projectWorkspace: { entries: [{ path: "app.py", bytes: 42 }, { path: "README.md", bytes: 18 }] },
  };
  assert.equal(isProjectTrace(trace), true);
  assert.equal(isProjectTrace({ task: { binding: { spec: {} } } }), false);
  const tabs = ui.projectTabsHtml(trace, "changes");
  assert.match(tabs, /变更 1/);
  assert.match(tabs, /data-work-tab="changes" aria-selected="true"/);
  const files = ui.projectFilesHtml(trace);
  assert.match(files, /data-project-file="README.md"/);
  assert.match(files, /18 B/);
  assert.match(files, /data-project-file-content/);
  assert.match(ui.projectResultHtml({ task: { id: "task-1" } }), /代码变更会出现在这里/);
});
