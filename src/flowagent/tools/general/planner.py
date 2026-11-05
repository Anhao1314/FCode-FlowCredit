"""The general plan blueprints: research (fetch then write), script
(write → run → fix → re-run → file the results), and open (the model plans).

The deterministic (mock) provider follows these blueprints the way it follows
the finance one. In research mode the write todo starts as a placeholder and
every successful fetch revises its content, so the report the model finally
*proposes* is composed from the evidence that actually arrived. In script
mode the blueprint is the honest shape of a first attempt: write, run, fix
what the output showed was wrong, re-run, file the results — with the content
and the edit anchors left to the proposer. The deterministic mock fills them
from its own fixture (``providers/mock.py``); a real model fills them from the
objective. The gate, the approvals and the file-state verifier treat both
identically.

``open`` is not a third blueprint; it is the *absence* of one. A blueprint can
only cover tasks someone anticipated, and the general domain's whole point is
the ones nobody did. So in open mode the planner writes exactly the obligation
the verifier contract needs — a verifiable artifact at the end — and the model
proposes the steps that get there, each one validated by the host before it
enters the plan (``runtime/plan_extension.py``). Research and script keep their
blueprints untouched.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ...core.plan import Plan, Todo
from ..base import Observation

__all__ = ["GeneralPlanner", "REPORT_PATH", "EXCERPT_CHARS", "GENERAL_MODES"]

#: The delegation modes the general domain understands. ``open`` is the one
#: whose plan the model may extend; the other two are blueprints and are closed.
GENERAL_MODES: tuple[str, ...] = ("research", "script", "open")

REPORT_PATH = "research.md"
#: Where an open delegation files its artifact when the operator named none.
OPEN_REPORT_PATH = "report.md"
EXCERPT_CHARS = 900

_FOOTER = (
    "\n---\n\n"
    "> 说明：以上网页内容由 `fetch_url` 抓取，字节级 sha256 与抓取时间已随收据入链；\n"
    "> Runtime 复核的是文件状态哈希、预算与链完整性，**不独立核实网页内容真伪**\n"
    "> （content_truth: not-verified）。\n"
)


def _report(goal: str, sections: list[dict[str, Any]]) -> str:
    lines = [f"# 调研底稿：{goal}", ""]
    lines.append("来源摘录（按抓取顺序）：")
    lines.append("")
    for index, section in enumerate(sections, start=1):
        excerpt = section["text"][:EXCERPT_CHARS]
        if len(section["text"]) > EXCERPT_CHARS:
            excerpt += "…"
        title = section.get("title") or section["url"]
        lines.append(f"## 来源 {index}：{title}")
        lines.append(f"- URL: {section['url']}")
        lines.append(f"- 抓取时间: {section['retrieved_at']} · sha256: `{section['sha256'][:16]}…` · {section['bytes']} bytes")
        lines.append("")
        lines.append(excerpt)
        lines.append("")
    lines.append("## 小结")
    lines.append("")
    lines.append(f"围绕目标整理了 {len(sections)} 份来源的要点；结论以上文摘录为准，均可在事件链中回溯。")
    lines.append(_FOOTER)
    return "".join(lines)


# --- script mode -----------------------------------------------------------
#
# The blueprint is the *shape* of the work: write a script, run it, fix what
# the output showed was wrong, re-run, file the results. It carries intent —
# the script's path, the command — and **not** authored content: what goes
# inside the file, and which anchor the edit replaces, are the model's to
# propose. Pre-filling them here made every real delegation execute this
# checkout's milestone fixture instead of the operator's task (observed on a
# real Kimi run: the objective asked for one script, the file written was the
# demo's fibonacci draft). The deterministic mock, which cannot author
# anything, supplies its own fixture in ``providers/mock.py``.


def _results(goal: str, runs: list[dict[str, Any]], edits: list[dict[str, Any]] | None = None) -> str:
    lines = [f"# 运行结果：{goal}", ""]
    lines.append("脚本在受限终端（白名单 run_command）中执行，输出与退出码逐次记录如下；")
    lines.append("修复以 edit_file 的锚定替换完成，前后 blob 哈希与 git commit 均在链上。")
    lines.append("")
    for index, run in enumerate(runs, start=1):
        label = "首次运行（修复前）" if index == 1 else f"第 {index} 次运行"
        lines.append(f"## {label}")
        lines.append(f"- 命令: `{run['command']}` · 退出码 {run['exit_code']} · {run['duration_ms']} ms")
        lines.append("")
        lines.append("```")
        lines.append(run["output"].rstrip() or "(no output)")
        lines.append("```")
        lines.append("")
    if edits:
        lines.append("## 修复记录（提议者提交的锚点）")
        lines.append("")
        for index, edit in enumerate(edits, start=1):
            old_head = " ".join(str(edit.get("old_text") or "").split())[:80]
            new_head = " ".join(str(edit.get("new_text") or "").split())[:80]
            lines.append(f"- 第 {index} 次替换：`{old_head}` → `{new_head}`")
        lines.append("")
    if len(runs) >= 2:
        # Mechanically derived, never interpreted: the runtime files what the
        # terminal printed and what differed. Reading the numbers is the
        # model's job (and the operator's) — an earlier version asserted a
        # fibonacci-specific conclusion here for every run, including ones
        # whose script had nothing to do with fibonacci.
        lines.append("## 两次运行的差异（逐行比对，不解释语义）")
        lines.append("")
        first = (runs[0].get("output") or "").splitlines()
        second = (runs[-1].get("output") or "").splitlines()
        changed = [f"- 第 {i + 1} 行：`{a}` → `{b}`"
                   for i, (a, b) in enumerate(zip(first, second)) if a != b]
        if changed:
            lines.extend(changed[:20])
        else:
            lines.append("- 两次输出逐行一致")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("> Runtime 复核的是文件状态哈希、预算与链完整性；命令输出的语义正确性由上述判读说明，不独立重算。")
    lines.append("")
    return "\n".join(lines)


class GeneralPlanner:
    """Decomposes a delegation into its mode's blueprint.

    Evidence (fetched sections, command runs) lives on the instance, not in
    the plan: a todo's ``arguments`` are what the model proposes, and
    metadata there would be rejected as unknown parameters. Instance state
    is safe because ``Loop.rebuild_plan`` replays every committed observation
    through ``advance`` in order — a resumed run re-derives the same content.
    """

    def __init__(
        self,
        sources: list[str],
        *,
        report_path: str = REPORT_PATH,
        mode: str = "research",
        script_path: str = "script.py",
    ) -> None:
        self.sources = list(sources)
        self.report_path = report_path
        self.mode = mode if mode in GENERAL_MODES else "research"
        self.script_path = script_path
        self._sections: list[dict[str, Any]] = []
        self._runs: list[dict[str, Any]] = []
        self._edits: list[dict[str, Any]] = []

    def decompose(self, goal: str) -> Plan:
        self._sections = []
        self._runs = []
        if self.mode == "open":
            # The one obligation the host keeps: there must be a verifiable
            # artifact, because that is what "succeeded" is recomputed from. All
            # other steps come from the model, through the host's plan-extension
            # validator, and are required so a step cannot be quietly dropped to
            # look finished.
            return Plan(goal=goal, todos=[
                Todo(
                    id="out",
                    text=f"把最终产物写进 {self.report_path}（内容由提议者撰写）",
                    tool="write_file",
                    done_when=f"written:{self.report_path}",
                    arguments={"path": self.report_path},
                ),
            ], revision=1)
        if self.mode == "script":
            self._edits = []
            return Plan(goal=goal, todos=self._script_todos(), revision=1)
        if not self.sources:
            raise ValueError("a research delegation needs at least one source URL")
        todos: list[Todo] = []
        for index, url in enumerate(self.sources, start=1):
            todos.append(Todo(
                id=f"t{index}",
                text=f"抓取来源 {url}",
                tool="fetch_url",
                done_when=f"fetched:{url}",
                arguments={"url": url},
            ))
        todos.append(Todo(
            id="tw",
            text=f"撰写 {self.report_path}（内容来自已抓取来源）",
            tool="write_file",
            done_when=f"written:{self.report_path}",
            arguments={"path": self.report_path, "content": ""},
        ))
        return Plan(goal=goal, todos=todos, revision=1)

    def _script_todos(self) -> list[Todo]:
        return [
            Todo("t1", f"写出第一版脚本 {self.script_path}（内容由提议者撰写）", "write_file",
                 f"written:{self.script_path}",
                 {"path": self.script_path}),
            Todo("t2", f"在受限终端运行 {self.script_path}", "run_command",
                 f"ran:{self.script_path}",
                 {"command": "python3", "args": [self.script_path]}),
            Todo("t3", f"用锚定替换修复 {self.script_path} 里输出暴露的问题", "edit_file",
                 f"edited:{self.script_path}",
                 {"path": self.script_path}),
            Todo("t4", f"复跑修复后的 {self.script_path}", "run_command",
                 f"ran-fixed:{self.script_path}",
                 {"command": "python3", "args": [self.script_path]}),
            Todo("t5", f"把两次运行输出整理进 {self.report_path}", "write_file",
                 f"written:{self.report_path}",
                 {"path": self.report_path, "content": ""}),
        ]

    def advance(self, plan: Plan, observation: Observation) -> Plan:
        if observation.tool == "fetch_url":
            updated = self._settle_fetch(plan, observation)
            return self._compose_report(updated, observation)
        if observation.tool == "run_command":
            updated = self._settle_sequential(plan, "run_command", observation)
            return self._compose_results(updated, observation)
        if observation.tool == "edit_file":
            self._edits.append(dict(observation.arguments))
            return self._settle_sequential(plan, "edit_file", observation)
        if observation.tool == "write_file":
            return self._settle_write(plan, observation)
        return self._settle_by_identity(plan, observation)

    # ------------------------------------------------------------------ private

    def _settle_fetch(self, plan: Plan, observation: Observation) -> Plan:
        url = str(observation.arguments.get("url") or "")
        for todo in plan.todos:
            if todo.tool == "fetch_url" and todo.arguments.get("url") == url:
                if todo.status in ("done", "abandoned"):
                    return plan
                return plan.mark(todo.id, "done", evidence=(observation.result_hash,))
        return plan

    def _settle_sequential(self, plan: Plan, tool: str, observation: Observation) -> Plan:
        """Settle the first *pending* todo of this tool — run_command steps
        share their arguments (the same script runs twice), so order decides."""
        for todo in plan.todos:
            if todo.tool == tool and todo.status in ("pending", "active"):
                return plan.mark(todo.id, "done", evidence=(observation.result_hash,))
        return plan

    def _settle_by_identity(self, plan: Plan, observation: Observation) -> Plan:
        """Settle the first pending todo this observation actually satisfies.

        The branches above match on a blueprint's own terms (a path, a URL, run
        order). This one covers the todos the *model* added, for tools the
        blueprints never used — ``ls_workspace``, ``read_file``, ``ask_operator``,
        ``recall_observation``. A step is done when an observation of its tool
        carries the arguments that identify its target; a todo that declared no
        arguments is satisfied by any observation of its tool, which is what
        "list the workspace" means.

        Declared arguments only, and every one of them must match: a proposal
        that authors more than the todo asked for still settles it, the same
        identity-over-body rule the result verifier uses for its own binding.
        """
        for todo in plan.todos:
            if todo.tool != observation.tool or todo.status not in ("pending", "active"):
                continue
            observed = dict(observation.arguments or {})
            if all(observed.get(key) == value for key, value in (todo.arguments or {}).items()):
                return plan.mark(todo.id, "done", evidence=(observation.result_hash,))
        return plan

    def _settle_write(self, plan: Plan, observation: Observation) -> Plan:
        path = str(observation.arguments.get("path") or "")
        for todo in plan.todos:
            if todo.tool == "write_file" and todo.arguments.get("path") == path:
                if todo.status in ("done", "abandoned"):
                    return plan
                return plan.mark(todo.id, "done", evidence=(observation.result_hash,))
        return plan

    def _compose_report(self, plan: Plan, observation: Observation) -> Plan:
        """Fold the fetched evidence into the pending report todo's content.

        Research-only: composing the report from arrivals is what makes *this
        blueprint*'s placeholder real. An open delegation's artifact is the
        proposer's to write, and overwriting its content here would be the
        blueprint quietly doing the model's work — and a script delegation has
        no report to compose until its runs have finished.
        """
        if self.mode != "research":
            return plan
        self._sections.append(observation.result)
        existing = next((t for t in plan.todos if t.tool == "write_file"), None)
        if existing is None or existing.status in ("done", "abandoned"):
            return plan
        content = _report(plan.goal, self._sections)
        revised = replace(
            existing,
            arguments={
                "path": existing.arguments.get("path", REPORT_PATH),
                "content": content,
            },
        )
        todos = [revised if t.id == existing.id else t for t in plan.todos]
        return Plan(goal=plan.goal, todos=todos, revision=plan.revision)

    def _compose_results(self, plan: Plan, observation: Observation) -> Plan:
        """Fold each run's output into the pending results todo's content.

        Script-only, and identified by the blueprint's own todo ids: whatever a
        model adds to an open plan, its content stays the proposer's.
        """
        if self.mode != "script":
            return plan
        self._runs.append(observation.result)
        existing = next(
            (t for t in plan.todos
             if t.tool == "write_file" and t.id in ("t5", "tw")),
            None,
        )
        if existing is None or existing.status in ("done", "abandoned"):
            return plan
        content = _results(plan.goal, self._runs, self._edits)
        revised = replace(
            existing,
            arguments={
                "path": existing.arguments.get("path", self.report_path),
                "content": content,
            },
        )
        todos = [revised if t.id == existing.id else t for t in plan.todos]
        return Plan(goal=plan.goal, todos=todos, revision=plan.revision)
