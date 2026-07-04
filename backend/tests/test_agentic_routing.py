"""Tests for #1: agentic tool loop is the default for all reviewers (allowlist overrides)."""

from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.planner import Planner, _looks_like_cross_pr_wrapper, _skip_reviewer_for_files
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


def test_planner_does_not_default_style_when_security_is_forced():
    planner = Planner(MockChatLLM(), build_registry())
    tasks = planner._merge_tasks(
        {"security_reviewer"},
        [],
        ["app.py"],
        first_round=True,
    )

    assert [t.reviewer for t in tasks] == ["security_reviewer"]


def test_planner_skips_low_signal_reviewers_for_fixtures():
    files = ["test_fixtures/codex_validation/frontend/AdminPreview.tsx"]

    assert _skip_reviewer_for_files("testing_reviewer", files)
    assert _skip_reviewer_for_files("accessibility_reviewer", files)
    assert not _skip_reviewer_for_files("security_reviewer", files)


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
