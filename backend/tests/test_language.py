from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.specs import build_registry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.language import cjk_ratio, extract_diff_comments, resolve_output_language
from reviewforge.engine.planner import Planner
from reviewforge.engine.prompt import build_reviewer_prompt
from reviewforge.engine.reviewers import BaseReviewer
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


class _CapturingLLM:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[object]] = []

    async def ainvoke(self, messages: list[object]) -> AIMessage:
        self.calls.append(messages)
        return AIMessage(content=self.content)


def test_legacy_output_language_default_and_env_override(monkeypatch, tmp_path):
    monkeypatch.delenv("REVIEWFORGE_OUTPUT_LANGUAGE", raising=False)
    assert ReviewForgeConfig().output_language == "zh-CN"
    assert ReviewForgeConfig.load(tmp_path / "missing.yaml").output_language == "zh-CN"

    monkeypatch.setenv("REVIEWFORGE_OUTPUT_LANGUAGE", "en")
    assert ReviewForgeConfig.load(tmp_path / "missing.yaml").output_language == "en"


def test_yaml_and_namespaced_output_language_are_supported(tmp_path):
    config_path = tmp_path / "reviewforge.yaml"
    config_path.write_text("review:\n  output_language: en\n", encoding="utf-8")
    assert ReviewForgeConfig.load(config_path).output_language == "en"

    config_path.write_text("output_language: auto\n", encoding="utf-8")
    assert ReviewForgeConfig.load(config_path).output_language == "auto"


def test_auto_language_uses_pr_body_and_added_diff_comments_only():
    state = StateStore(
        pr_body="这是一个中文说明，用于测试输出语言。",
        files_changed=["src/service.py"],
        file_diffs={"src/service.py": "@@ -1 +1,2 @@\n-value = 1\n+# 中文注释\n+value = '中文字符串不应计入注释'\n"},
    )
    config = SimpleNamespace(output_language="auto")

    assert resolve_output_language(state, config) == "zh-CN"
    assert resolve_output_language(StateStore(pr_body="plain English"), config) == "en"
    assert cjk_ratio("中文中文xx") > 0.30


def test_diff_comment_extractor_ignores_code_string_cjk():
    diff = """@@ -1 +1,4 @@
-old
+value = '中文字符串'
+# 中文注释
+// another 中文 comment
"""
    comments = extract_diff_comments(diff)
    assert "中文注释" in comments
    assert "another 中文 comment" in comments
    assert "中文字符串" not in comments


def test_diff_mapping_comments_are_sorted_by_key():
    diffs = {
        "z.py": "+# 乙注释\n+value = 1",
        "a.py": "+# 甲注释\n+value = 2",
    }

    assert extract_diff_comments(diffs) == " 甲注释\n 乙注释"


def test_english_reviewer_prompt_uses_english_output_fields(monkeypatch):
    ctx = {
        "registry": build_registry(),
        "reviewer_type": "correctness",
        "agent_name": "correctness_reviewer",
        "files_to_review": ["src/service.py"],
        "diffs": {"src/service.py": "@@ -1 +1 @@\n+return value\n"},
        "output_language": "en",
    }
    system = build_reviewer_prompt(ctx)[0]["content"]

    assert "## Language requirement" in system
    assert "message, suggestion, and reason fields MUST be written in English" in system
    assert "必须使用中文" not in system
    assert "message`: what the problem is (1–2 sentences, in English)" in system


def test_prompt_without_language_context_keeps_legacy_requirement_byte_for_byte(monkeypatch):
    monkeypatch.delenv("REVIEWFORGE_OUTPUT_LANGUAGE", raising=False)
    ctx = {
        "registry": build_registry(),
        "reviewer_type": "correctness",
        "agent_name": "correctness_reviewer",
        "files_to_review": [],
        "diffs": {},
    }
    system = build_reviewer_prompt(ctx)[0]["content"]
    assert (
        "## 语言要求\n\n所有 message、suggestion、reason 字段必须使用中文。category 和 severity 使用英文。"
        "代码标识符、路径、API 名称保留英文。"
    ) in system


async def test_config_and_state_language_reaches_real_planner_and_reviewer_messages():
    registry = build_registry()

    planner_llm = _CapturingLLM('{"tasks": []}')
    planner_config = ReviewForgeConfig(output_language="en")
    planner = Planner(planner_llm, registry, output_language=planner_config.output_language)  # type: ignore[arg-type]
    planner_state = StateStore(
        files_changed=["src/service.py"],
        diff_summary="+return value",
        pr_body="English change description",
    )

    await planner.plan(planner_state)

    planner_system = str(planner_llm.calls[0][0].content)  # type: ignore[attr-defined]
    assert "All message, suggestion, and reason fields MUST be written in English" in planner_system
    assert "必须使用中文" not in planner_system

    reviewer_llm = _CapturingLLM('{"findings": []}')
    reviewer_config = ReviewForgeConfig(output_language="auto")
    gateway = ToolGateway(registry, MockGitHubClient())
    reviewer = BaseReviewer(
        "correctness_reviewer",
        "correctness",
        reviewer_llm,  # type: ignore[arg-type]
        registry,
        gateway,
        output_language=reviewer_config.output_language,
    )
    reviewer_state = StateStore(
        files_changed=["src/service.py"],
        pr_body="这是中文变更说明，用于验证自动语言选择。",
        file_diffs={"src/service.py": "@@ -1 +1 @@\n+# 中文注释\n+return value"},
    )
    task = ReviewTask(reviewer="correctness_reviewer", files=["src/service.py"])

    await reviewer.execute_singleshot(task, reviewer_state)

    reviewer_system = str(reviewer_llm.calls[0][0].content)  # type: ignore[attr-defined]
    assert "所有 message、suggestion、reason 字段必须使用中文" in reviewer_system
    assert "All message, suggestion, and reason fields MUST be written in English" not in reviewer_system
