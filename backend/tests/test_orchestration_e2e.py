"""End-to-end orchestrator test in mock mode — exercises the re-planning loop (#2/#3),
Scheduler (#4), and Verifier de-dupe stage (#5) without real LLM/GitHub."""

from reviewforge.core.events import EventBus
from reviewforge.core.scheduler import Scheduler
from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.mock_llm import MockChatLLM
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.verifier import Verifier
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


def _orchestrator():
    reg = build_registry()
    return Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=MockChatLLM(),
        reviewer_llm=MockChatLLM(),
        calibrator_llm=MockChatLLM(),
        db=None,
        agentic_default=False,  # single-shot so the mock pipeline is deterministic/fast
    )


async def test_orchestrator_runs_and_converges():
    orch = _orchestrator()
    state = StateStore(
        pr_number=1,
        repo="o/r",
        head_sha="abc",
        files_changed=["a.py"],
        diff_summary="--- a.py\n+import os\n+os.system(user_input)\n",  # forces security_reviewer
    )
    summary = await orch.run(state)
    # The re-planning loop converged (returned a summary rather than hanging).
    assert set(summary) >= {"total_findings", "confirmed", "false_positives", "tasks_completed", "tasks_failed"}
    assert summary["tasks_completed"] >= 1
    # Security aliases from non-security reviewers are filtered before they can create duplicate noise.
    assert summary["false_positives"] == 0
    # The security finding passed verification and was reported (commented).
    statuses = {f.status for f in state.list_findings()}
    assert "reported" in statuses or summary["confirmed"] >= 1


def test_verifier_merges_duplicates():
    v = Verifier()
    findings = [
        Finding(
            file="a.py", line=10, category="sql-injection", message="x", confidence=0.6, reviewer="security_reviewer"
        ),
        Finding(
            file="a.py", line=10, category="sql-injection", message="x", confidence=0.9, reviewer="performance_reviewer"
        ),
        Finding(file="b.py", line=5, category="style", message="y", confidence=0.4, reviewer="style_reviewer"),
    ]
    survivors, dropped = v.verify(findings)
    assert len(survivors) == 2  # the two a.py:10 dupes merged into one
    assert len(dropped) == 1
    merged = next(f for f in survivors if f.file == "a.py")
    assert merged.confidence == 0.9  # higher-confidence wins
    assert "performance_reviewer" in merged.reviewer and "security_reviewer" in merged.reviewer


def test_scheduler_orders_by_priority():
    sched = Scheduler()

    class T:
        def __init__(self, r):
            self.reviewer = r

    ordered = [t.reviewer for t in sched.order([T("style_reviewer"), T("security_reviewer"), T("doc_reviewer")])]
    assert ordered[0] == "security_reviewer"  # highest priority first
