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
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.orchestrator import Orchestrator, _should_escalate_finding
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


class _MissingTestLLM(BaseChatModel):
    """Only planner/reviewer calls are allowed; quality gating must be zero-token."""

    calls: int = 0

    class Config:
        arbitrary_types_allowed = True

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        self.calls += 1
        sysmsg = messages[0].content if messages else ""
        if "planner" in sysmsg.lower():
            content = json.dumps({"tasks": [{"reviewer": "testing_reviewer", "files": ["service.py"]}]})
        elif "对抗性验证器" not in sysmsg and "最终裁决者" not in sysmsg and "核实器" not in sysmsg:
            content = json.dumps(
                {
                    "findings": [
                        {
                            "file": "service.py",
                            "line": 1,
                            "severity": "warning",
                            "category": "missing-test",
                            "message": "新增公共函数没有测试。",
                            "suggestion": "添加正常和异常路径测试。",
                            "confidence": 0.6,
                        }
                    ]
                },
                ensure_ascii=False,
            )
        else:
            raise AssertionError("actionability finding reached escalation/calibration LLM")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "missing-test"

    @property
    def _identifying_params(self):
        return {}

    def bind_tools(self, tools, **kw):
        return self


class _SafeCommandLLM(BaseChatModel):
    """Emit a fuzzy command-injection claim that static code evidence can disprove."""

    calls: int = 0

    class Config:
        arbitrary_types_allowed = True

    def _generate(self, messages, stop=None, run_manager=None, **kw):
        self.calls += 1
        sysmsg = messages[0].content if messages else ""
        if "planner" in sysmsg.lower():
            content = json.dumps({"tasks": [{"reviewer": "security_reviewer", "files": ["helpers.py"]}]})
        elif self.calls == 2:
            content = json.dumps(
                {
                    "findings": [
                        {
                            "file": "helpers.py",
                            "line": 5,
                            "severity": "warning",
                            "category": "command-injection",
                            "message": "The host argument can inject shell commands.",
                            "suggestion": "Validate the host before invoking ping.",
                            "confidence": 0.6,
                        }
                    ]
                }
            )
        else:
            raise AssertionError("provably safe command finding reached an LLM verification path")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "safe-command"

    @property
    def _identifying_params(self):
        return {}

    def bind_tools(self, tools, **kw):
        return self


def test_high_confidence_security_skips_agentic_escalation():
    high_confidence = Finding(
        file="a.py",
        line=5,
        severity="error",
        category="sql-injection",
        message="string-concat SQL",
        confidence=0.8,
        reviewer="security_reviewer",
    )
    fuzzy_confidence = Finding(
        file="a.py",
        line=5,
        severity="error",
        category="sql-injection",
        message="string-concat SQL",
        confidence=0.6,
        reviewer="security_reviewer",
    )

    assert _should_escalate_finding(high_confidence, 0.4, 0.7) is False
    assert _should_escalate_finding(fuzzy_confidence, 0.4, 0.7) is True


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


async def test_actionability_gate_runs_before_escalation_split():
    reg = build_registry()
    llm = _MissingTestLLM()
    events = []
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    orch = Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=event_bus,
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        db=None,
        agentic_default=False,
        escalation_enabled=True,
    )
    state = StateStore(
        pr_number=2,
        repo="o/r",
        head_sha="h2",
        files_changed=["service.py"],
        diff_summary="--- service.py (+2 -0)\n@@ -0,0 +1,2 @@\n+def service():\n+    return 1",
    )

    await orch.run(state)

    findings = state.list_findings()
    assert len(findings) == 1
    assert findings[0].status == "false_positive"
    assert findings[0].verified_by == "actionability-gate"
    event_types = [event.event_type for event in events]
    assert "actionability.completed" in event_types
    assert "escalation.started" not in event_types
    assert "calibration.started" not in event_types
    assert llm.calls == 2


async def test_code_evidence_gate_runs_before_escalation_split():
    reg = build_registry()
    llm = _SafeCommandLLM()
    events = []
    event_bus = EventBus()
    event_bus.subscribe(events.append)
    orch = Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MockGitHubClient()),
        event_bus=event_bus,
        planner_llm=llm,
        reviewer_llm=llm,
        calibrator_llm=llm,
        db=None,
        agentic_default=False,
        escalation_enabled=True,
    )
    state = StateStore(
        pr_number=3,
        repo="o/r",
        head_sha="h3",
        files_changed=["helpers.py"],
        diff_summary=(
            "--- /dev/null\n"
            "+++ b/helpers.py\n"
            "@@ -0,0 +1,3 @@\n"
            '+def ping_host(host: str = "localhost") -> str:\n'
            "+    return subprocess.check_output(\n"
            '+        ["ping", "-c", "1", host]).decode()'
        ),
    )

    await orch.run(state)

    findings = state.list_findings()
    assert len(findings) == 1
    assert findings[0].status == "false_positive"
    assert findings[0].verified_by == "code-evidence"
    event_types = [event.event_type for event in events]
    assert "code_evidence.completed" in event_types
    assert "escalation.started" not in event_types
    assert "calibration.started" not in event_types
    assert llm.calls == 2
