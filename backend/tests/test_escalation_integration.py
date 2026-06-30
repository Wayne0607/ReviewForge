"""Regression test for the escalation/calibration coupling fix.

Before the fix, escalation ran THEN the calibrator re-judged the same findings — and
the calibrator auto-confirms security categories, overwriting escalation's verdict.
Now they are mutually exclusive: escalation handles trace findings and its verdict is final.
"""

import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from reviewforge.core.events import EventBus
from reviewforge.core.specs import build_registry
from reviewforge.core.state import StateStore
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.tools.gateway import ToolGateway
from reviewforge.tools.mock_github import MockGitHubClient


class _VerdictLLM(BaseChatModel):
    """Reviewer → emits a sql-injection finding; escalation → returns a false_positive verdict."""

    class Config:
        arbitrary_types_allowed = True

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        sysmsg = messages[0].content if messages else ""
        if "核实器" in sysmsg:  # escalation verifier prompt
            content = json.dumps(
                {"verdict": "false_positive", "confidence": 0.15, "reason": "上游已参数化，非真实漏洞"}
            )
        elif "planner" in sysmsg.lower():
            content = json.dumps({"tasks": [{"reviewer": "security_reviewer", "files": ["a.py"]}]})
        else:  # reviewer
            content = json.dumps(
                {
                    "findings": [
                        {
                            "file": "a.py",
                            "line": 5,
                            "severity": "error",
                            "category": "sql-injection",
                            "message": "string-concat SQL",
                            "suggestion": "use params",
                            "confidence": 0.6,
                        }
                    ]
                }
            )
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "verdict"

    @property
    def _identifying_params(self):
        return {}

    def bind_tools(self, tools, **kw):
        return self


async def test_escalation_verdict_survives_calibrator():
    reg = build_registry()
    llm = _VerdictLLM()
    orch = Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=EventBus(),
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        db=None,
        agentic_default=False,
        escalation_enabled=True,
    )
    state = StateStore(
        pr_number=1,
        repo="o/r",
        head_sha="h",
        files_changed=["a.py"],
        diff_summary='--- a.py\n+query = "SELECT * FROM t WHERE id=" + str(uid)',
    )
    await orch.run(state)

    sqli = [f for f in state.list_findings() if f.category == "sql-injection"]
    assert sqli, "expected a sql-injection finding from the reviewer"
    # Escalation judged it false_positive; the calibrator must NOT flip it back to confirmed.
    # (Duplicates may also be marked false_positive by the Verifier merge step — also fine.)
    assert all(f.status == "false_positive" for f in sqli), [f.status for f in sqli]
    assert any(f.verified_by == "escalation" for f in sqli), [f.verified_by for f in sqli]
