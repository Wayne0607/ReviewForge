from __future__ import annotations

import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.lenses import LensSelection, build_lens_generator, run_lens, select_lenses
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind


def _diff(path: str, *lines: str) -> str:
    body = "\n".join(f"+{line}" for line in lines)
    return (
        f"diff --git a/{path} b/{path}\n"
        "new file mode 100644\n"
        "--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,{len(lines)} @@\n"
        f"{body}\n"
    )


def _unit(path: str, *, risk: float = 1.0, signals: list[dict] | None = None) -> SemanticUnit:
    return SemanticUnit(
        id=f"{path}:f",
        path=path,
        language="python",
        kind=UnitKind.SYMBOL,
        symbol="f",
        start_line=1,
        end_line=2,
        added_lines=[1, 2],
        risk_score=risk,
        risk_signals=list(signals or []),
    )


def _changeset(*units: SemanticUnit) -> SemanticChangeSet:
    return SemanticChangeSet(repo="owner/repo", pr_number=1, head_sha="abc", units=list(units))


def _names(state: StateStore, changeset: SemanticChangeSet, **kwargs) -> list[str]:
    return [selection.name for selection in select_lenses(state, changeset, **kwargs)]


def test_security_triggered_by_risk_signal() -> None:
    unit = _unit("app.py", signals=[{"type": "security-sensitive-symbol", "reference_count": 3}])
    state = StateStore(file_diffs={})
    assert "security" in _names(state, _changeset(unit))


def test_security_triggered_by_sink_regex() -> None:
    unit = _unit("app.py")
    state = StateStore(file_diffs={"app.py": _diff("app.py", "import pickle", "data = pickle.loads(user_input)")})
    assert "security" in _names(state, _changeset(unit))


def test_no_lens_for_plain_unit() -> None:
    unit = _unit("app.py")
    state = StateStore(file_diffs={"app.py": _diff("app.py", "def f():", "    return 1")})
    assert _names(state, _changeset(unit)) == []


def test_python_concurrency_contracts_are_shared_with_the_specialist_only():
    from reviewforge.engine.verification_guidance import python_concurrency_guidance

    guide = python_concurrency_guidance()
    concurrency = build_lens_generator(_ScriptedLLM(), "concurrency")._system_prompt()
    assert concurrency.count(guide) == 1
    for name in ("security", "localization", "accessibility", "dependency"):
        assert guide not in build_lens_generator(_ScriptedLLM(), name)._system_prompt()


def test_localization_triggered_by_path() -> None:
    assert "localization" in _names(StateStore(file_diffs={}), _changeset(_unit("src/locale/messages_en.properties")))
    assert "localization" not in _names(StateStore(file_diffs={}), _changeset(_unit("src/service.py")))


def test_accessibility_requires_markup_and_token() -> None:
    with_button = StateStore(file_diffs={"a.tsx": _diff("a.tsx", "return <button onClick={h}>ok</button>")})
    assert "accessibility" in _names(with_button, _changeset(_unit("a.tsx")))

    no_token = StateStore(file_diffs={"a.tsx": _diff("a.tsx", "return <div>ok</div>")})
    assert "accessibility" not in _names(no_token, _changeset(_unit("a.tsx")))


def test_concurrency_triggered_by_added_token() -> None:
    go = StateStore(file_diffs={"w.go": _diff("w.go", "go func() { work() }()")})
    assert "concurrency" in _names(go, _changeset(_unit("w.go")))

    clean = StateStore(file_diffs={"w.go": _diff("w.go", "x := y + 1")})
    assert "concurrency" not in _names(clean, _changeset(_unit("w.go")))


def test_dependency_triggered_by_manifest_path() -> None:
    assert "dependency" in _names(StateStore(file_diffs={}), _changeset(_unit("package.json")))
    assert "dependency" not in _names(StateStore(file_diffs={}), _changeset(_unit("src/index.js")))


def test_max_three_lenses_by_risk() -> None:
    units = [
        _unit("src/locale/en.properties", risk=1.0),  # localization
        _unit("package.json", risk=2.0),  # dependency
        _unit("src/sink.py", risk=4.0, signals=[{"type": "security-sensitive-symbol"}]),  # security
    ]
    shell = StateStore(
        file_diffs={
            "src/concurrent.go": _diff("src/concurrent.go", "go func() {}()"),
        }
    )
    units.append(_unit("src/concurrent.go", risk=3.0))  # concurrency
    names = select_lenses(shell, _changeset(*units), max_lenses=3)
    assert [selection.name for selection in names] == ["security", "concurrency", "dependency"]


class _ScriptedLLM(BaseChatModel):
    responses: list[str] = Field(default_factory=list)
    calls: list[list] = Field(default_factory=list)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "scripted"

    @property
    def _identifying_params(self):
        return {}


def test_format_contract_context_is_confined_to_the_v4_localization_lens() -> None:
    from pathlib import Path

    import reviewforge.engine.lenses as lens_module

    reference = "https://docs.oracle.com/en/java/javase/21/docs/api/java.base/java/text/MessageFormat.html"
    shared = Path(lens_module.__file__).parent.parent / "skills/localization_rules/SKILL.md"
    original = shared.read_bytes()
    localization = build_lens_generator(_ScriptedLLM(), "localization")._system_prompt()
    security = build_lens_generator(_ScriptedLLM(), "security")._system_prompt()

    assert "declared locale" in localization and reference in localization
    assert "{{name}}" in localization and "i18next-icu" in localization
    assert localization.count("https://www.i18next.com/translation-function/interpolation") == 1
    assert reference not in security and reference not in original.decode("utf-8")
    assert shared.read_bytes() == original


def test_generator_and_lenses_share_the_actual_investigator_tool_boundary():
    import re

    from reviewforge.engine.hypothesis_generator import HypothesisGenerator
    from reviewforge.engine.investigator import Investigator

    actual = {tool.name for tool in Investigator(_ScriptedLLM(), None)._build_tools()}
    for generator in [HypothesisGenerator(_ScriptedLLM()), build_lens_generator(_ScriptedLLM(), "security")]:
        prompt = generator._system_prompt()
        assert prompt.count("## Investigation capabilities") == 1
        tools = set(re.findall(r"\b(read_\w+|grep|find_\w+)\b", prompt.split("## Investigation capabilities", 1)[1]))
        assert tools == actual
        assert "no internet/advisory lookup or runtime execution" in prompt


@pytest.mark.asyncio
async def test_run_lens_upserts_with_lens_source() -> None:
    diff = _diff("app.py", "import pickle", "data = pickle.loads(user_input)")
    unit = _unit("app.py", signals=[{"type": "security-sensitive-symbol"}])
    state = StateStore(file_diffs={"app.py": diff})
    changeset = _changeset(unit)
    ledger = HypothesisLedger("run", "abc", "digest")
    selection = LensSelection(name="security", units=[unit.id], reason="security-sensitive-symbol", risk=1.0)
    llm = _ScriptedLLM(
        responses=[
            json.dumps(
                {
                    "hypotheses": [
                        {
                            "unit_id": unit.id,
                            "mechanism": "security-sink",
                            "anchor_symbol": "f",
                            "claim": "untrusted input reaches pickle",
                            "trigger": "request body is deserialized directly",
                            "impact": "remote code execution",
                            "open_question": "is user_input from the request?",
                            "refutation": "if user_input is a local constant, not an issue",
                            "severity": "error",
                            "sites": [{"path": "app.py", "line": 2, "excerpt": "pickle.loads(user_input)"}],
                        }
                    ],
                    "no_issue_units": [],
                }
            )
        ]
    )

    result = await run_lens(llm, "security", selection, state, ContextPack(), changeset, ledger)

    assert result.accepted == 1
    identity = f"{unit.id}::security-sink::f"
    assert ledger.items[identity].source == "lens:security"


@pytest.mark.asyncio
async def test_lens_smaller_input_budget_splits_without_losing_changed_units() -> None:
    units = [_unit("first.py"), _unit("second.py")]
    diffs = {unit.path: _diff(unit.path, "import pickle", "data = pickle.loads(user_input)") for unit in units}
    llm = _ScriptedLLM(
        responses=[
            json.dumps({"hypotheses": [], "no_issue_units": [{"unit_id": unit.id, "checked": "both lines inspected"}]})
            for unit in units
        ]
    )
    ledger = HypothesisLedger("run", "abc", "digest")
    selection = LensSelection(name="security", units=[unit.id for unit in units], reason="security", risk=1)
    result = await run_lens(
        llm,
        "security",
        selection,
        StateStore(file_diffs=diffs),
        ContextPack(),
        _changeset(*units),
        ledger,
        max_input_chars=1000,
        context_max_chars=0,
    )
    assert result.blocks == 2 and result.failed_blocks == 0
    assert set(ledger.no_issue_units) == {unit.id for unit in units}
    assert not ledger.unresolved_units
    assert all(len(call[1].content) <= 1000 for call in llm.calls)
    for unit in units:
        assert any(unit.path in call[1].content and "pickle.loads(user_input)" in call[1].content for call in llm.calls)
