"""Regression tests for untrusted Planner task output."""

import json

from langchain_core.messages import AIMessage

from reviewforge.core.specs import build_registry
from reviewforge.core.state import TASK_RATIONALE_MAX_LENGTH, StateStore
from reviewforge.engine.planner import Planner
from reviewforge.engine.prompt import build_planner_prompt, build_reviewer_prompt


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

    assert [task.reviewer for task in tasks] == ["security_reviewer", "correctness_reviewer"]
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

    assert [task.reviewer for task in tasks] == ["security_reviewer", "correctness_reviewer"]


async def test_planner_retries_invalid_json_once() -> None:
    llm = _InvalidThenValidPlannerLLM()
    planner = Planner(llm, build_registry())  # type: ignore[arg-type]
    state = StateStore(repo="owner/repo", pr_number=76, files_changed=["app.py"], diff_summary="+value = 1")

    tasks = await planner.plan(state)

    assert llm.calls == 2
    assert [task.reviewer for task in tasks] == ["correctness_reviewer"]


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


def test_unknown_model_role_falls_back_to_tool_bounded_correctness() -> None:
    planner = Planner(_StaticPlannerLLM("{}"), build_registry())  # type: ignore[arg-type]
    content = json.dumps(
        {
            "tasks": [
                {
                    "reviewer": "api_contract_reviewer",
                    "files": ["src/calendar.ts"],
                    "rationale": "compare the changed interface with implementations",
                }
            ]
        }
    )

    tasks = planner._parse_response(content, allowed_files=["src/calendar.ts"])

    assert len(tasks) == 1
    assert tasks[0].reviewer == "correctness_reviewer"
    assert tasks[0].files == ["src/calendar.ts"]
    assert tasks[0].rationale == "compare the changed interface with implementations"


def test_planner_contract_advertises_runtime_bounds() -> None:
    tasks_contract = build_registry().get_agent("planner").output_contract["properties"]["tasks"]

    assert tasks_contract["maxItems"] == 6
    assert tasks_contract["items"]["properties"]["rationale"]["maxLength"] == TASK_RATIONALE_MAX_LENGTH


def test_prompts_do_not_encode_style_nits_as_behavioral_defects() -> None:
    registry = build_registry()
    planner_text = "\n".join(
        message["content"]
        for message in build_planner_prompt(
            {
                "registry": registry,
                "repo": "owner/repo",
                "pr_number": 118,
                "files_changed": ["src/service.py"],
                "diff_summary": "+value = None",
            }
        )
    )
    correctness_text = "\n".join(
        message["content"]
        for message in build_reviewer_prompt(
            {
                "registry": registry,
                "reviewer_type": "correctness",
                "agent_name": "correctness_reviewer",
                "files_to_review": ["src/service.py"],
                "diffs": {"src/service.py": "+value = None"},
            }
        )
    )
    documentation_text = "\n".join(
        message["content"]
        for message in build_reviewer_prompt(
            {
                "registry": registry,
                "reviewer_type": "documentation",
                "agent_name": "doc_reviewer",
                "files_to_review": ["docs/api.md"],
                "diffs": {"docs/api.md": "+Returns 202."},
            }
        )
    )

    assert "== None" not in correctness_text
    assert "公共 API/docstring 缺失都不派发" in planner_text
    assert "\n- -" not in documentation_text
