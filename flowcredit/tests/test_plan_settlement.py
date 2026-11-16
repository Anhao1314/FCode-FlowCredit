"""Edit observations settle only the todo whose artifact anchor they used."""

from __future__ import annotations

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from flowagent.core.plan import Plan, Todo  # noqa: E402
from flowagent.tools.base import Observation  # noqa: E402
from flowagent.tools.general.planner import GeneralPlanner  # noqa: E402


def edit_todo(todo_id: str, *, path: str = "index.html", old_text: str | None = None) -> Todo:
    arguments = {"path": path}
    if old_text is not None:
        arguments["old_text"] = old_text
    return Todo(
        id=todo_id,
        text=f"edit {todo_id}",
        tool="edit_file",
        done_when=f"done:{todo_id}",
        arguments=arguments,
    )


def edit_observation(old_text: str, *, path: str = "index.html", result_hash: str = "observation") -> Observation:
    return Observation(
        tool="edit_file",
        arguments={"path": path, "old_text": old_text, "new_text": "replacement"},
        result={"path": path, "changed": True},
        result_hash=result_hash,
    )


def statuses(plan: Plan) -> dict[str, str]:
    return {todo.id: todo.status for todo in plan.todos}


class EditSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = GeneralPlanner([], mode="open", report_path="FCODE_RESULT.md")

    def test_css_observation_does_not_settle_unanchored_scene_siblings(self) -> None:
        plan = Plan(goal="motion", todos=[
            Todo("out", "deliver", "write_file", "written:FCODE_RESULT.md",
                 {"path": "FCODE_RESULT.md"}),
            edit_todo("scene1"),
            edit_todo("scene2"),
        ])
        advanced = self.planner.advance(plan, edit_observation("</style>", result_hash="css"))
        self.assertEqual(statuses(advanced), {"out": "pending", "scene1": "pending", "scene2": "pending"})

    def test_exact_anchor_selects_the_matching_edit_todo(self) -> None:
        plan = Plan(goal="motion", todos=[
            edit_todo("styles", old_text="</style>"),
            edit_todo("scene1", old_text="<!-- SECTION: scene -->"),
            edit_todo("scene2", old_text="<g id=\"wheel\"></g>"),
        ])
        after_css = self.planner.advance(plan, edit_observation("</style>", result_hash="css"))
        self.assertEqual(statuses(after_css), {"styles": "done", "scene1": "pending", "scene2": "pending"})
        after_scene = self.planner.advance(
            after_css,
            edit_observation("<!-- SECTION: scene -->", result_hash="scene"),
        )
        self.assertEqual(statuses(after_scene), {"styles": "done", "scene1": "done", "scene2": "pending"})
        self.assertEqual(after_scene.todos[1].evidence, ("scene",))

    def test_single_unanchored_blueprint_edit_keeps_legacy_settlement(self) -> None:
        plan = Plan(goal="script", todos=[edit_todo("t3", path="script.py")])
        advanced = self.planner.advance(
            plan,
            edit_observation("return old", path="script.py", result_hash="fixed"),
        )
        self.assertEqual(advanced.todos[0].status, "done")
        self.assertEqual(advanced.todos[0].evidence, ("fixed",))

    def test_an_edit_for_another_path_settles_nothing(self) -> None:
        plan = Plan(goal="script", todos=[edit_todo("t3", path="script.py")])
        advanced = self.planner.advance(plan, edit_observation("return old", path="other.py"))
        self.assertEqual(advanced.todos[0].status, "pending")


if __name__ == "__main__":
    unittest.main()
