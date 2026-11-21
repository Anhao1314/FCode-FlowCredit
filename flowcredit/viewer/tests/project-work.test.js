import test from "node:test";
import assert from "node:assert/strict";
import { createProjectWork, isProjectTrace, parseVerificationCommand, projectDraftRows } from "../app/project-work.js";
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
  assert.match(result, /data-project-apply/, "补丁可应用到原项目（冲突检测在服务端）");
  assert.match(result, /project-diff-line del/);
  assert.match(projectDraftRows({ path: "/tmp/demo", verification: "pytest -q" }), /Runtime 独立复跑/);
  assert.equal(ui.projectContextHtml({ task: { binding: { spec: {} } } }), "");
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

test("project delivery puts images and video in the conversation answer", () => {
  const ui = createProjectWork({ esc });
  const html = ui.projectDeliveryHtml({
    task: { id: "task-run-1" },
    project: { filesChanged: [
      { status: "M", path: "render.py" },
      { status: "A", path: "preview.gif" },
      { status: "A", path: "demo.mp4" },
    ] },
  });
  assert.match(html, /data-project-delivery/);
  assert.match(html, /3 个文件/);
  assert.match(html, /workspace-media\?path=preview.gif/);
  assert.match(html, /<img[^>]+src=/);
  assert.match(html, /<video[^>]+controls/);
  assert.match(html, /render.py/);
  // A non-project trace (or a run with no changes) has nothing to deliver.
  assert.equal(ui.projectDeliveryHtml({ task: { id: "task-1" } }), "");
  const hostile = ui.projectDeliveryHtml({
    task: { id: "task-x" },
    project: { filesChanged: [{ status: "A", path: 'x"><img src=x>.gif' }] },
  });
  assert.ok(!hostile.includes("<img src=x>"));
});

test("a single-file HTML deliverable gets a sandboxed preview action", () => {
  const ui = createProjectWork({ esc });
  const html = ui.projectDeliveryHtml({
    task: { id: "task-run-1" },
    project: { filesChanged: [{ status: "M", path: "index.html" }] },
  });
  assert.match(html, /data-project-html="index.html"/);
  assert.match(html, /data-run-id="run-1"/);
  assert.match(html, /预览动画/);
  assert.ok(!html.includes("th-delivery-file"));
});

test("a motion quality verdict is visible without opening the rail", () => {
  const ui = createProjectWork({ esc });
  const html = ui.projectDeliveryHtml({
    task: { id: "task-run-1" },
    project: {
      filesChanged: [{ status: "M", path: "index.html" }],
      quality: { status: "fail", score: 69, issues: ["41.9% slip", "crank/leg mismatch"] },
    },
  });
  assert.match(html, /data-project-quality/);
  assert.match(html, /质量未通过/);
  assert.match(html, /69\/100/);
  assert.match(html, /41\.9% slip/);
});

test("the quality card shows sampled frames and contact metrics", () => {
  const ui = createProjectWork({ esc });
  const html = ui.projectDeliveryHtml({
    task: { id: "task-run-9" },
    project: {
      filesChanged: [{ status: "M", path: "index.html" }],
      quality: {
        status: "fail", score: 65, frame_count: 4, contact: "contact.png",
        max_foot_pedal_px: 62, leg_wheel_overlap_px2: 0,
        issues: ["foot leaves pedal: max centre distance 62px"],
      },
    },
  });
  assert.match(html, /quality-frame\?name=contact\.png/);
  assert.match(html, /<img[^>]+contact\.png/);
  assert.match(html, /脚-支撑 62px/);
  assert.match(html, /腿\/轮 0px²/);
  assert.match(html, /4 帧/);
  assert.match(html, /foot leaves pedal/);
});

test("unverified coverage is never rendered as a quality pass", () => {
  const ui = createProjectWork({ esc });
  const html = ui.projectDeliveryHtml({
    task: { id: "task-run-10" },
    project: {
      filesChanged: [{ status: "M", path: "index.html" }],
      quality: {
        status: "unverified", score: 14, frame_count: 4, hip_count: 0,
        min_leg_length_px: 31, max_leg_start_to_hip_px: 0,
        coverage: {
          required: ["text_invariants", "frame_capture", "foot_pedal", "hip_anchor"],
          covered: ["text_invariants", "frame_capture", "foot_pedal"],
          uncovered: ["hip_anchor"], complete: false,
        },
        issues: ["coverage: hip_anchor is not measured"],
      },
    },
  });
  assert.match(html, /质量未验证/);
  assert.match(html, /未验证 · 3\/4/);
  assert.match(html, /覆盖 3\/4/);
  assert.match(html, /腿长 31px/);
  assert.match(html, /髋 0/);
  assert.doesNotMatch(html, /质量通过/);
});

test("a failed project run still shows the artifact and quality state", () => {
  const ui = createProjectWork({ esc });
  const html = ui.projectArtifactStateHtml({
    task: {
      id: "task-run-11",
      binding: { spec: { project: { path: "/tmp/pelican" } } },
    },
    project: {
      filesChanged: [
        { status: "A", path: "ik.py" },
        { status: "M", path: "index.html" },
      ],
      quality: {
        status: "fail", score: 0, frame_count: 4, contact: "contact.png",
        coverage: {
          required: ["frame_capture", "animation_contract", "foot_pedal", "leg_presence",
            "hip_anchor", "leg_length", "leg_endpoint", "text_invariants"],
          covered: ["frame_capture"], complete: false,
        },
        issues: ["no .foot element: foot-to-pedal contact cannot be measured"],
      },
    },
  });
  assert.match(html, /data-artifact-state/);
  assert.match(html, /产物状态/);
  assert.match(html, /文件已写入，未验收/);
  assert.match(html, /当前产物尚未验收/);
  assert.match(html, /质量 0\/100/);
  assert.match(html, /覆盖 1\/8/);
  assert.match(html, /<details class="th-artifact-quality fail" data-artifact-quality>/);
  assert.doesNotMatch(html, /质量未通过/);
  assert.equal((html.match(/质量 0\/100/g) || []).length, 1);
  assert.match(html, /quality-frame\?name=contact\.png/);
  assert.match(html, /no \.foot element/);
  assert.match(html, /主产物<\/b>index\.html/);
  assert.match(html, /辅助文件<\/b>ik\.py/);
  assert.ok(html.indexOf("index.html") < html.indexOf("ik.py"), "the primary HTML artifact is listed first");
  assert.equal(ui.projectArtifactStateHtml({ task: { id: "task-run-12" } }), "");
});

test("a failed run without a quality probe is not rendered as verified", () => {
  const ui = createProjectWork({ esc });
  const html = ui.projectArtifactStateHtml({
    task: {
      id: "task-run-13",
      binding: { spec: { project: { path: "/tmp/pelican" } } },
    },
    project: { filesChanged: [{ status: "M", path: "index.html" }] },
  });
  assert.match(html, /尚未检查/);
  assert.doesNotMatch(html, /data-project-quality/);
  assert.doesNotMatch(html, /质量通过/);
});
