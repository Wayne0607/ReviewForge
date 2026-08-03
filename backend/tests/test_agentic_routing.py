"""Tests for #1: agentic tool loop is the default for all reviewers (allowlist overrides)."""

from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import ReviewTask, StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.planner import (
    Planner,
    _correctness_files,
    _documentation_files,
    _localization_files,
    _looks_like_cross_pr_wrapper,
    _skip_reviewer_for_change,
    _skip_reviewer_for_files,
)
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def _orch(**kw):
    reg = build_registry()
    return Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=MockChatLLM(),
        reviewer_llm=MockChatLLM(),
        calibrator_llm=MockChatLLM(),
        db=None,
        **kw,
    )


def test_agentic_default_on_for_all_reviewers():
    orch = _orch(agentic_default=True)
    assert orch._create_reviewer("security_reviewer")._agentic is True
    assert orch._create_reviewer("style_reviewer")._agentic is True


def test_allowlist_overrides_default():
    orch = _orch(agentic_reviewers=["style_reviewer"], agentic_default=True)
    assert orch._create_reviewer("security_reviewer")._agentic is False
    assert orch._create_reviewer("style_reviewer")._agentic is True


def test_default_off_makes_all_single_shot():
    orch = _orch(agentic_default=False)
    assert orch._create_reviewer("security_reviewer")._agentic is False


def test_skill_attached_to_reviewer():
    # #6 integration: security reviewer gets its SKILL.md attached via the orchestrator
    orch = _orch(agentic_default=False)
    r = orch._create_reviewer("security_reviewer")
    orch._attach_skill(r)  # language-aware routing; security_rules is universal so matches without language
    assert r._skill_name == "security_rules"
    assert len(r._skill_body) > 50


def test_security_and_correctness_coexist_on_first_round():
    """First-round correctness baseline is injected even when security_reviewer is forced."""
    planner = Planner(MockChatLLM(), build_registry())
    tasks = planner._merge_tasks(
        {"security_reviewer"},
        [],
        ["app.py"],
        first_round=True,
    )

    assert [t.reviewer for t in tasks] == ["security_reviewer", "correctness_reviewer"]
    assert tasks[-1].files == ["app.py"]


def test_llm_correctness_subset_expands_to_all_eligible_production_files():
    """LLM proposing a correctness task on a subset of files gets expanded to all production files."""
    planner = Planner(MockChatLLM(), build_registry())
    llm_tasks = [
        ReviewTask(
            reviewer="correctness_reviewer",
            files=["src/a.py"],
            rationale="subset rationale",
        ),
    ]
    files = ["src/a.py", "src/b.py", "tests/test_b.py"]

    tasks = planner._merge_tasks(set(), llm_tasks, files, first_round=True)

    correctness = [t for t in tasks if t.reviewer == "correctness_reviewer"]
    assert len(correctness) == 1
    assert set(correctness[0].files) == {"src/a.py", "src/b.py"}
    assert correctness[0].rationale == "subset rationale"


def test_duplicate_llm_correctness_proposals_collapse_to_one_with_first_rationale():
    """Multiple LLM correctness proposals collapse to exactly one; first rationale is kept."""
    planner = Planner(MockChatLLM(), build_registry())
    llm_tasks = [
        ReviewTask(reviewer="correctness_reviewer", files=["src/a.py"], rationale="first rationale"),
        ReviewTask(reviewer="security_reviewer", files=["src/a.py"], rationale="security finding"),
        ReviewTask(reviewer="correctness_reviewer", files=["src/b.py"], rationale="second rationale"),
    ]
    files = ["src/a.py", "src/b.py"]

    tasks = planner._merge_tasks(set(), llm_tasks, files, first_round=True)

    correctness = [t for t in tasks if t.reviewer == "correctness_reviewer"]
    assert len(correctness) == 1
    assert set(correctness[0].files) == {"src/a.py", "src/b.py"}
    assert correctness[0].rationale == "first rationale"
    reviewers = [t.reviewer for t in tasks]
    assert reviewers.count("correctness_reviewer") == 1
    assert "security_reviewer" in reviewers


def test_cross_pr_style_fallback_false_does_not_inject_correctness():
    """When style_fallback=False (cross-PR wrapper), no correctness baseline is injected."""
    planner = Planner(MockChatLLM(), build_registry())
    tasks = planner._merge_tasks(set(), [], ["app.py"], first_round=True, style_fallback=False)

    assert tasks == []


def test_replan_does_not_inject_correctness_baseline():
    """When first_round=False (replanning), no correctness baseline is injected."""
    planner = Planner(MockChatLLM(), build_registry())
    tasks = planner._merge_tasks(set(), [], ["app.py"], first_round=False, style_fallback=True)

    assert tasks == []


def test_planner_defaults_production_source_to_correctness():
    planner = Planner(MockChatLLM(), build_registry())

    tasks = planner._merge_tasks(set(), [], ["src/service.py", "tests/test_service.py"], first_round=True)

    assert [task.reviewer for task in tasks] == ["correctness_reviewer"]
    assert tasks[0].files == ["src/service.py"]


def test_planner_skips_low_signal_reviewers_for_fixtures():
    files = ["test_fixtures/codex_validation/frontend/AdminPreview.tsx"]

    assert _skip_reviewer_for_files("testing_reviewer", files)
    assert _skip_reviewer_for_files("accessibility_reviewer", files)
    assert not _skip_reviewer_for_files("security_reviewer", files)


def test_planner_routes_low_signal_reviewers_only_with_changed_evidence():
    source_files = ["src/service.py"]
    assert _skip_reviewer_for_change("testing_reviewer", source_files, "+def service(): pass")
    assert not _skip_reviewer_for_change("testing_reviewer", ["tests/test_service.py"], "+def test_service()")
    assert _skip_reviewer_for_change("doc_reviewer", source_files, "+value = 1")
    assert not _skip_reviewer_for_change("doc_reviewer", ["docs/api.md"], "+The service returns a result.")
    assert _skip_reviewer_for_change(
        "doc_reviewer",
        ["src/raw.rs"],
        "+pub unsafe fn read_raw(ptr: *const u8) -> u8 { *ptr }",
    )
    assert _skip_reviewer_for_change("doc_reviewer", ["test_fixtures/sample.py"], "+value = 1")
    assert _skip_reviewer_for_change("doc_reviewer", ["examples/guide.md"], "+Example text")
    assert _skip_reviewer_for_change("style_reviewer", ["docs/api.md"], "+    import requests")

    assert _skip_reviewer_for_change("localization_reviewer", ["src/service.py"], '+logger.info("ready")')
    assert not _skip_reviewer_for_change(
        "localization_reviewer", ["web/save-button.tsx"], "+return <button>Save changes</button>"
    )
    assert _skip_reviewer_for_change("style_reviewer", source_files, "+value = 1")
    assert not _skip_reviewer_for_change("style_reviewer", source_files, "+    import requests")


def test_planner_leaves_simple_alt_and_label_sinks_to_phase0_detector():
    simple = """+export function Form() {
+  return <><img src={avatar} /><input name="email" onChange={save} /></>;
+}
"""

    assert _skip_reviewer_for_change("accessibility_reviewer", ["src/form.tsx"], simple)


def test_planner_keeps_accessibility_reviewer_for_complex_interaction_semantics():
    custom_control = '+<div onClick={activate} tabIndex={0} role="button">Open</div>'
    keyboard_flow = "+modalRef.current?.focus()"
    native_button = "+<button><Icon /></button>"

    assert not _skip_reviewer_for_change("accessibility_reviewer", ["src/control.tsx"], custom_control)
    assert not _skip_reviewer_for_change("accessibility_reviewer", ["src/modal.tsx"], keyboard_flow)
    assert not _skip_reviewer_for_change("accessibility_reviewer", ["src/button.tsx"], native_button)


def test_planner_keeps_testing_review_for_an_actual_security_fix():
    diff = """@@ -1,2 +1,3 @@
-return eval(user_input)
+safe_value = sanitize(user_input)
+return safe_value
"""

    assert not _skip_reviewer_for_change("testing_reviewer", ["src/parser.py"], diff)


def test_localization_routing_selects_production_resources_and_bounds_scope():
    files = [
        "src/test/resources/messages_de.properties",
        "themes/messages/messages_lt.properties",
        "web/locales/zh-CN.json",
        "config/settings.json",
        *[f"translations/messages_{index}.properties" for index in range(30)],
    ]

    selected = _localization_files(files)

    assert selected[:2] == ["themes/messages/messages_lt.properties", "web/locales/zh-CN.json"]
    assert len(selected) == 16
    assert all("src/test" not in path for path in selected)


def test_localization_source_routing_is_scoped_to_each_file_diff():
    files = ["web/save-button.tsx", "src/jobs.py", "ml/model.py"]
    file_diffs = {
        "web/save-button.tsx": "+return <button>Save changes</button>",
        "src/jobs.py": '+title = "Internal batch job"',
        "ml/model.py": '+label = "positive"',
    }

    selected = _localization_files(files, "\n".join(file_diffs.values()), file_diffs=file_diffs)

    assert selected == ["web/save-button.tsx"]


def test_documentation_routing_includes_docs_but_excludes_examples_and_fixtures():
    files = [
        "docs/api.md",
        "README",
        "examples/guide.md",
        "test_fixtures/docs/sample.md",
        "docs/logo.png",
        "src/readme_parser.py",
        "src/changelog_service.ts",
        "docs/generated/api.md",
        "tests/snapshots/rendered.md",
        "testdata/cases/output.md",
    ]

    assert _documentation_files(files) == ["docs/api.md", "README"]


def test_localization_routing_excludes_generic_properties_and_supports_non_latin_ui():
    files = ["gradle.properties", "web/save-button.tsx"]
    file_diffs = {
        "gradle.properties": "+org.gradle.parallel=true",
        "web/save-button.tsx": "+return <button>保存</button>",
    }

    assert _localization_files(files, file_diffs=file_diffs) == ["web/save-button.tsx"]


def test_correctness_routing_keeps_only_production_source_files():
    files = [
        "src/service.java",
        "src/service_test.py",
        "tests/view.spec.tsx",
        "themes/messages_lt.properties",
        "go.work.sum",
        "vendor/generated.go",
        "web/controller.ts",
    ]

    assert _correctness_files(files) == ["src/service.java", "web/controller.ts"]


async def test_planner_forces_localization_reviewer_for_locale_resources():
    planner = Planner(MockChatLLM(), build_registry())
    state = StateStore(
        pr_number=1,
        repo="o/r",
        files_changed=["themes/messages/messages_lt.properties", "src/service.java"],
        diff_summary="+totpStep1=Installa una delle seguenti applicazioni",
    )

    tasks = await planner.plan(state)
    task = next(item for item in tasks if item.reviewer == "localization_reviewer")

    assert task.files == ["themes/messages/messages_lt.properties"]


async def test_planner_routes_explicit_ui_text_without_an_empty_localization_task():
    planner = Planner(MockChatLLM(), build_registry())
    state = StateStore(
        pr_number=2,
        repo="o/r",
        files_changed=["web/save-button.tsx"],
        diff_summary="+export const SaveLabel = () => <p>Save changes</p>",
        file_diffs={"web/save-button.tsx": "+export const SaveLabel = () => <p>Save changes</p>"},
    )

    tasks = await planner.plan(state)

    assert [task.reviewer for task in tasks] == ["localization_reviewer", "correctness_reviewer"]
    assert tasks[0].files == ["web/save-button.tsx"]
    assert all(task.files for task in tasks)


async def test_planner_routes_docs_as_one_bounded_specialist_task():
    planner = Planner(MockChatLLM(), build_registry())
    state = StateStore(
        pr_number=3,
        repo="o/r",
        files_changed=["docs/api.md", "examples/demo.md"],
        diff_summary="+The endpoint now returns 202.",
    )

    tasks = await planner.plan(state)

    assert [task.reviewer for task in tasks] == ["doc_reviewer"]
    assert tasks[0].files == ["docs/api.md"]


def test_merge_unions_forced_doc_and_localization_evidence_with_llm_subsets():
    planner = Planner(MockChatLLM(), build_registry())
    files = ["docs/api.md", "web/save-button.tsx", "locales/en.json", "src/api.py"]
    file_diffs = {
        "docs/api.md": "+The API returns 202.",
        "web/save-button.tsx": "+return <button>Save changes</button>",
        "locales/en.json": '+{"save": "Save changes"}',
        "src/api.py": "+def save(): pass",
    }
    tasks = planner._merge_tasks(
        {"doc_reviewer", "localization_reviewer"},
        [
            ReviewTask(reviewer="doc_reviewer", files=["src/api.py"], rationale="LLM doc subset"),
            ReviewTask(
                reviewer="localization_reviewer",
                files=["web/save-button.tsx"],
                rationale="LLM locale subset",
            ),
        ],
        files,
        diff="\n".join(file_diffs.values()),
        file_diffs=file_diffs,
    )

    by_reviewer = {task.reviewer: task for task in tasks}
    assert by_reviewer["doc_reviewer"].files == ["docs/api.md"]
    assert set(by_reviewer["localization_reviewer"].files) == {
        "locales/en.json",
        "web/save-button.tsx",
    }


def test_merge_enforces_final_task_cap_without_dropping_security_or_correctness():
    planner = Planner(MockChatLLM(), build_registry())
    files = ["src/app.py", "web/view.tsx", "locales/en.json", "docs/api.md", "tests/test_app.py"]
    file_diffs = {
        "web/view.tsx": "+return <button>Save</button>",
        "locales/en.json": '+{"save": "Save"}',
    }
    reviewers = [
        "style_reviewer",
        "doc_reviewer",
        "testing_reviewer",
        "localization_reviewer",
        "accessibility_reviewer",
        "performance_reviewer",
        "dependency_reviewer",
        "security_reviewer",
        "correctness_reviewer",
    ]
    tasks = planner._merge_tasks(
        set(),
        [ReviewTask(reviewer=name, files=files, rationale=name) for name in reviewers],
        files,
        diff="\n".join(file_diffs.values()),
        file_diffs=file_diffs,
    )

    assert len(tasks) == 6
    assert {"security_reviewer", "correctness_reviewer"} <= {task.reviewer for task in tasks}


def test_cross_pr_wrapper_changes_skip_style_fallback():
    planner = Planner(MockChatLLM(), build_registry())
    tasks = planner._merge_tasks(
        set(), [], ["cross_pr_live/report_endpoint.py"], first_round=True, style_fallback=False
    )

    assert tasks == []


def test_detects_tiny_cross_pr_wrapper_diff():
    diff = """--- cross_pr_live/report_endpoint.py (+5 -0)
+from cross_pr_live.risky_ops import run_report_query
+
+def export_report(conn, account_id):
+    return run_report_query(conn, "reports", account_id)
"""

    assert _looks_like_cross_pr_wrapper(["cross_pr_live/report_endpoint.py"], diff)


def test_direct_security_code_is_not_treated_as_wrapper():
    diff = """--- cross_pr_live/risky_ops.py (+3 -0)
+def run(conn, table):
+    return conn.execute(f"SELECT * FROM {table}")
"""

    assert not _looks_like_cross_pr_wrapper(["cross_pr_live/risky_ops.py"], diff)


async def test_planner_returns_no_tasks_for_cross_pr_wrapper():
    planner = Planner(MockChatLLM(), build_registry())
    state = StateStore(
        pr_number=1,
        repo="o/r",
        files_changed=["cross_pr_live/report_endpoint.py"],
        diff_summary="""--- cross_pr_live/report_endpoint.py (+5 -0)
+from cross_pr_live.risky_ops import run_report_query
+
+def export_report(conn, account_id):
+    return run_report_query(conn, "reports", account_id)
""",
    )

    assert await planner.plan(state) == []
