"""Regression tests for untrusted Planner task output."""

import json

from langchain_core.messages import AIMessage

from reviewforge.core.specs import build_registry
from reviewforge.core.state import TASK_RATIONALE_MAX_LENGTH, StateStore
from reviewforge.engine.planner import Planner


class _StaticPlannerLLM:
    def __init__(self, content: str) -> None:
        self._content = content

    async def ainvoke(self, _messages: list[object]) -> AIMessage:
        return AIMessage(content=self._content)


class _InvalidThenValidPlannerLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, _messages: list[object]) -> AIMessage:
        self.calls += 1
        if self.calls == 1:
            return AIMessage(content="analysis without JSON")
        return AIMessage(
            content=json.dumps(
                {"tasks": [{"reviewer": "style", "files": ["app.py"], "rationale": "observable behavior"}]}
            )
        )


async def test_overlong_rationale_is_truncated_without_failing_plan() -> None:
    content = json.dumps(
        {
            "tasks": [
                {
                    "reviewer": "security",
                    "files": ["app.py"],
                    "rationale": "  security   context  " * 100,
                },
                {"reviewer": "style", "files": ["app.py"], "rationale": "readability"},
            ]
        }
    )
    planner = Planner(_StaticPlannerLLM(content), build_registry())  # type: ignore[arg-type]
    state = StateStore(repo="owner/repo", pr_number=74, files_changed=["app.py"], diff_summary="+value = 1")

    tasks = await planner.plan(state)

    assert [task.reviewer for task in tasks] == [
        "security_reviewer",
        "style_reviewer",
        "correctness_reviewer",
    ]
    assert len(tasks[0].rationale) == TASK_RATIONALE_MAX_LENGTH
    assert tasks[0].rationale.startswith("security context")


async def test_plan_filters_absence_only_test_and_doc_tasks_for_source_only_change() -> None:
    content = json.dumps(
        {
            "tasks": [
                {"reviewer": "testing", "files": ["app.py"], "rationale": "no tests added"},
                {"reviewer": "documentation", "files": ["app.py"], "rationale": "no docstring"},
                {"reviewer": "security", "files": ["app.py"], "rationale": "semantic security review"},
            ]
        }
    )
    planner = Planner(_StaticPlannerLLM(content), build_registry())  # type: ignore[arg-type]
    state = StateStore(repo="owner/repo", pr_number=75, files_changed=["app.py"], diff_summary="+value = 1")

    tasks = await planner.plan(state)

    assert [task.reviewer for task in tasks] == [
        "security_reviewer",
        "correctness_reviewer",
        "style_reviewer",
    ]


async def test_planner_retries_invalid_json_once() -> None:
    llm = _InvalidThenValidPlannerLLM()
    planner = Planner(llm, build_registry())  # type: ignore[arg-type]
    state = StateStore(repo="owner/repo", pr_number=76, files_changed=["app.py"], diff_summary="+value = 1")

    tasks = await planner.plan(state)

    assert llm.calls == 2
    assert [task.reviewer for task in tasks] == ["style_reviewer", "correctness_reviewer"]


def test_malformed_task_is_skipped_without_losing_valid_siblings() -> None:
    planner = Planner(_StaticPlannerLLM("{}"), build_registry())  # type: ignore[arg-type]
    content = json.dumps(
        {
            "tasks": [
                {"reviewer": {"unexpected": "object"}, "files": ["app.py"]},
                {"reviewer": "security", "files": "app.py"},
                {
                    "reviewer": "testing",
                    "files": [None, "../secret.txt", "app.py", "app.py"],
                    "rationale": {"unexpected": "object"},
                },
                {"reviewer": "style", "files": ["not-changed.py"]},
            ]
        }
    )

    tasks = planner._parse_response(content, allowed_files=["app.py"])

    assert len(tasks) == 1
    assert tasks[0].reviewer == "testing_reviewer"
    assert tasks[0].files == ["app.py"]
    assert tasks[0].rationale == ""


def test_planner_contract_advertises_runtime_bounds() -> None:
    tasks_contract = build_registry().get_agent("planner").output_contract["properties"]["tasks"]

    assert tasks_contract["maxItems"] == 6
    assert tasks_contract["items"]["properties"]["rationale"]["maxLength"] == TASK_RATIONALE_MAX_LENGTH
