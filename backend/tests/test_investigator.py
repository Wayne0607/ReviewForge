from __future__ import annotations

import json
import threading
from dataclasses import replace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Site
from reviewforge.engine.investigator import Investigator, budget_steps, build_workspace_executor
from reviewforge.engine.semantic_diff import Provenance, SemanticChangeSet, SemanticUnit, UnitKind


def _hypothesis(*, severity: str = "warning", refutation: str = "", open_question: str = "") -> Hypothesis:
    return Hypothesis(
        id="h_test",
        identity="a.py:f::null-path::f",
        unit_id="a.py:f",
        mechanism=Mechanism.NULL_PATH,
        claim="未处理空值",
        trigger="输入为空时",
        impact="运行时崩溃",
        open_question=open_question or "输入是否可能为空？",
        refutation=refutation or "若上游已校验非空则不成立",
        sites=[Site(path="a.py", line=2, excerpt="return user_input")],
        severity=severity,
        source="generator",
    )


def _state(paths: list[str] | None = None, *, diffs: dict[str, str] | None = None) -> StateStore:
    return StateStore(
        repo="owner/repo",
        pr_number=1,
        head_sha="abc123",
        files_changed=paths or ["a.py"],
        file_diffs=diffs or {"a.py": "diff"},
    )


def _source_workspace(tmp_path, source: str):
    from reviewforge.tools.workspace import PRHeadWorkspace, WorkspaceInfo

    (tmp_path / "a.py").write_text(source, encoding="utf-8")
    info = WorkspaceInfo("owner/repo", "owner/repo", "abc123", tmp_path, 1, len(source), "d", False, "tarball")
    return PRHeadWorkspace(info, None, fallback_repo="owner/repo", temp_dir=tmp_path)


class _ScriptedToolLLM(BaseChatModel):
    turns: list = Field(default_factory=list)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        if not self.turns:
            content = json.dumps({"verdict": "unknown", "answer": "", "reason": "no turns left"})
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])
        turn = self.turns.pop(0)
        if isinstance(turn, str):
            message = AIMessage(content=turn)
        else:
            message = AIMessage(content="", tool_calls=[dict(turn)])
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self):
        return "scripted-tools"

    @property
    def _identifying_params(self):
        return {}


def _executor(results: dict[tuple[str, tuple], str]):
    async def _exec(name: str, args: dict) -> str:
        key = (name, tuple(sorted(args.items())))
        if key in results:
            return results[key]
        # Path-only fixtures represent a source excerpt, not a full file.
        if name == "read_file":
            return results.get((name, (("path", args.get("path")),)), "No results")
        return "No results"

    return _exec


def _read_file_call(path: str = "a.py"):
    return {"name": "read_file", "args": {"path": path}, "id": "call_1"}


def test_budget_by_severity() -> None:
    assert budget_steps(_hypothesis(severity="error")) == 6
    assert budget_steps(_hypothesis(severity="warning")) == 4
    assert budget_steps(_hypothesis(severity="info")) == 2
    # bonuses (caller -> +2, schema in question -> +2) still cap at 8
    boosted = _hypothesis(
        severity="error", refutation="caller already validates", open_question="does the schema allow null?"
    )
    assert budget_steps(boosted) == 8


def _ranked_hypothesis(identity: str, mechanism: Mechanism, sites: int, severity: str = "warning"):
    return replace(
        _hypothesis(severity=severity),
        id=identity,
        identity=identity,
        unit_id=identity,
        mechanism=mechanism,
        sites=[Site(path=f"{identity}.py", line=line, excerpt="return user_input") for line in range(1, sites + 1)],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrency", [1, 3])
async def test_investigation_cap_preserves_severity_and_mechanism_breadth(monkeypatch, concurrency):
    items = [
        _ranked_hypothesis("critical", Mechanism.NULL_PATH, 1, "error"),
        *[_ranked_hypothesis(f"resource-{i}", Mechanism.I18N, 10 - i) for i in range(5)],
        _ranked_hypothesis("single-site-code", Mechanism.WRONG_ARGUMENT, 1),
    ]
    calls = []

    async def investigate(worker, hypothesis, *args, **kwargs):
        calls.append(hypothesis.identity)
        return worker._result(verdict="unknown", reason="checked", severity=hypothesis.severity)

    monkeypatch.setattr(Investigator, "investigate", investigate)
    ledger = HypothesisLedger("run", "head", "digest")
    for item in reversed(items):
        ledger.upsert(item)
    results = await Investigator(_ScriptedToolLLM(), _executor({})).run(
        ledger, _state(), ContextPack(), max_hypotheses_per_pr=3, concurrency=concurrency
    )
    assert calls == ["critical", "resource-0", "single-site-code"]
    assert len(results) == len(items)
    assert all(item.status is HypothesisStatus.UNKNOWN for item in ledger.items.values())
    assert all(ledger.items[f"resource-{i}"].verdict_reason == "budget-exhausted" for i in range(1, 5))
    assert ledger.items["single-site-code"].verdict_reason == "checked"


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [0, -1, 2])
async def test_investigation_order_keeps_single_mechanism_priority_and_closed_items(monkeypatch, cap):
    items = [_ranked_hypothesis(f"error-{i}", Mechanism.I18N, 10 - i, "error") for i in range(3)]
    items += [_ranked_hypothesis("warning", Mechanism.SECURITY_SINK, 20)]
    closed = _ranked_hypothesis("closed", Mechanism.LOCK_SCOPE, 50, "error")
    closed.status = HypothesisStatus.REFUTED
    calls = []

    async def investigate(worker, hypothesis, *args, **kwargs):
        calls.append(hypothesis.identity)
        return worker._result(verdict="unknown", reason="checked", severity=hypothesis.severity)

    monkeypatch.setattr(Investigator, "investigate", investigate)
    ledger = HypothesisLedger("run", "head", "digest")
    for item in [closed, *reversed(items)]:
        ledger.upsert(item)
    await Investigator(_ScriptedToolLLM(), _executor({})).run(
        ledger, _state(), ContextPack(), max_hypotheses_per_pr=cap, concurrency=3
    )
    assert calls == [f"error-{i}" for i in range(max(0, cap))]
    assert ledger.items["closed"].status is HypothesisStatus.REFUTED
    assert ledger.items["warning"].verdict_reason == "budget-exhausted"


def test_investigation_input_selects_unit_hunks_and_all_additional_sites():
    hypothesis = _hypothesis()
    hypothesis.sites.append(Site(path="b.py", line=20, excerpt="another affected call"))
    unit = SemanticUnit(
        id=hypothesis.unit_id,
        path="a.py",
        language="python",
        kind=UnitKind.SYMBOL,
        symbol="f",
        start_line=1,
        end_line=12,
    )
    changeset = SemanticChangeSet(repo="owner/repo", pr_number=1, head_sha="abc123", units=[unit])
    state = _state(
        diffs={
            "a.py": "@@ -1,2 +1,2 @@\n def f():\n+return user_input\n"
            "@@ -10 +10 @@\n-validate(user_input)\n+skip_validation()\n"
            "@@ -200 +200 @@\n+UNRELATED_CHANGE\n",
            "b.py": "@@ -20 +20 @@\n+another affected call\n@@ -900 +900 @@\n+OTHER_UNRELATED\n",
        }
    )
    user = Investigator(_ScriptedToolLLM(), _executor({}), changeset=changeset)._render_user(
        hypothesis, state, ContextPack()
    )
    assert "-validate(user_input)" in user and "+skip_validation()" in user
    assert "### b.py\n@@ -20 +20 @@" in user
    assert "UNRELATED" not in user


def _locale_hypothesis():
    path = "messages_zh_CN.properties"
    unit = SemanticUnit(
        id="resource-cn",
        path=path,
        kind=UnitKind.RESOURCE,
        start_line=1,
        end_line=1,
        provenance=Provenance(source="resource-suffix", note="locale=zh_CN"),
    )
    hypothesis = _hypothesis(severity="error")
    hypothesis.unit_id = unit.id
    hypothesis.mechanism = Mechanism.I18N
    hypothesis.claim = "A changed Simplified Chinese entry uses Traditional Chinese."
    hypothesis.open_question = "Is this key referenced by a template?"
    hypothesis.sites = [Site(path=path, line=1, excerpt="step=安裝手機應用程式")]
    return hypothesis, unit


def test_resource_verification_boundary_preserves_local_contracts_per_site():
    hypothesis, unit = _locale_hypothesis()
    sibling = SemanticUnit(
        id="resource-tw",
        path="messages_zh_TW.properties",
        kind=UnitKind.RESOURCE,
        provenance=Provenance(note="locale=zh_TW,entries=1"),
    )
    unrelated = SemanticUnit(
        id="resource-en",
        path="messages_en.properties",
        kind=UnitKind.RESOURCE,
        provenance=Provenance(note="locale=en"),
    )
    hypothesis.sites.append(Site(path=sibling.path, line=1, excerpt="step=安裝手機應用程式"))
    investigator = Investigator(
        _ScriptedToolLLM(), _executor({}), changeset=SemanticChangeSet(units=[unit, sibling, unrelated])
    )
    prompt = investigator._render_user(hypothesis, _state(), ContextPack())
    boundary = prompt.split("## Verification boundary\n", 1)[1]
    facts = json.loads(boundary.split("\n", 1)[0])
    assert facts == [
        {"path": unit.path, "provenance": "locale=zh_CN"},
        {"path": sibling.path, "provenance": "locale=zh_TW,entries=1"},
    ]
    assert unrelated.path not in boundary
    assert "runtime consumer" in boundary and "formatter" in boundary
    assert "## Verification boundary" in str(investigator._closing_chat([AIMessage(content=prompt)], 24000))


@pytest.mark.parametrize("case", ["symbol", "null-path", "wrong-argument", "missing-unit"])
def test_resource_boundary_does_not_waive_runtime_or_unmatched_contracts(case):
    hypothesis, unit = _locale_hypothesis()
    if case == "symbol":
        unit.kind = UnitKind.SYMBOL
    elif case == "missing-unit":
        hypothesis.unit_id = "absent"
    else:
        hypothesis.mechanism = Mechanism(case)
    investigator = Investigator(_ScriptedToolLLM(), _executor({}), changeset=SemanticChangeSet(units=[unit]))
    assert "## Verification boundary" not in investigator._render_user(hypothesis, _state(), ContextPack())


@pytest.mark.parametrize(
    ("mechanism", "kind", "path", "expected"),
    [
        (Mechanism.CONTRACT_MISMATCH, UnitKind.RESOURCE, "messages_en.properties", True),
        (Mechanism.I18N, UnitKind.SYMBOL, "formatter.java", True),
        (Mechanism.CONTRACT_MISMATCH, UnitKind.RESOURCE, "package.json", False),
        (Mechanism.NULL_PATH, UnitKind.SYMBOL, "service.py", False),
    ],
)
def test_investigation_receives_relevant_contract_knowledge_through_closure(mechanism, kind, path, expected):
    hypothesis = _hypothesis()
    hypothesis.mechanism = mechanism
    unit = SemanticUnit(id=hypothesis.unit_id, path=path, kind=kind)
    investigator = Investigator(_ScriptedToolLLM(), _executor({}), changeset=SemanticChangeSet(units=[unit]))
    user = investigator._render_user(hypothesis, _state(), ContextPack())
    assert ("## Verification guidance" in user) is expected
    if expected:
        assert "i18next-icu" in user and "{{name}}" in user
        assert "import-only hits" in user
        closing = investigator._closing_chat([AIMessage(content=user)], 24000)
        assert str(closing).count("## Verification guidance") == 1


@pytest.mark.parametrize(
    ("path", "source", "expected"),
    [
        ("a.py", "process = ctx.Process(target=work)", True),
        ("a.py", "return user_input", False),
        ("Worker.java", "process = ctx.Process(target=work)", False),
    ],
)
def test_python_contract_knowledge_survives_investigation_closure_without_becoming_evidence(path, source, expected):
    hypothesis = _hypothesis()
    hypothesis.sites = [Site(path=path, line=1, excerpt=source)]
    unit = SemanticUnit(id=hypothesis.unit_id, path=path, kind=UnitKind.SYMBOL, start_line=1, end_line=1)
    investigator = Investigator(_ScriptedToolLLM(), _executor({}), changeset=SemanticChangeSet(units=[unit]))
    user = investigator._render_user(hypothesis, _state(diffs={path: f"@@ -1 +1 @@\n+{source}\n"}), ContextPack())
    assert ("### Python concurrency contracts" in user) is expected
    if expected:
        closing = investigator._closing_chat([AIMessage(content=user)], 24000)
        assert str(closing).count("### Python concurrency contracts") == 1
        assert not investigator._observations
        result = investigator._finalize(
            {
                "verdict": "confirmed",
                "assessment": {
                    "expected": "All processes must be joined explicitly",
                    "actual": "The process is not joined explicitly",
                    "expected_evidence": ["obs_0:e1"],
                    "actual_evidence": ["obs_0:e1"],
                    "comparison": "conflict",
                },
            },
            hypothesis,
            {path},
            steps=0,
        )
        assert result.verdict == "unknown" and result.strength == "none"


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["step=安裝手機應用程式", "No results"])
async def test_locale_boundary_still_requires_recorded_source_evidence(content):
    hypothesis, unit = _locale_hypothesis()
    patch = "@@ -1 +1 @@\n-step=安装手机应用程序\n+step=安裝手機應用程式\n"
    llm = _ScriptedToolLLM(turns=[_read_file_call(unit.path), _verdict(quote=hypothesis.sites[0].excerpt)])
    executor = _executor({("read_file", (("path", unit.path),)): content})
    result = await Investigator(llm, executor, changeset=SemanticChangeSet(units=[unit])).investigate(
        hypothesis, _state(paths=[unit.path], diffs={unit.path: patch}), ContextPack()
    )
    if content == "No results":
        assert result.verdict == "unknown" and result.reason == "ungrounded"
    else:
        assert result.verdict == "confirmed" and result.strength == "weak"
    assert len(result.observations) == 1 and result.observations[0].path == unit.path


@pytest.mark.asyncio
async def test_long_tool_output_identifies_saved_citation_boundary_and_narrow_read():
    content = "x" * 1200 + "\n50: important_fact()\n" + "y" * 5500
    investigator = Investigator(_ScriptedToolLLM(), _executor({("read_file", (("path", "a.py"),)): content}))
    result = await investigator._run_tool("read_file", {"path": "a.py"})
    observation = investigator._observations[0]
    assert len(result) <= 6000
    assert "important_fact()" not in observation.excerpt
    assert "important_fact()" not in result
    assert observation.excerpt in result
    assert "narrower line range" in result

    investigator._executor = _executor(
        {("read_file", (("end", 50), ("path", "a.py"), ("start", 50))): "50: important_fact()"}
    )
    await investigator._run_tool("read_file", {"path": "a.py", "start": 50, "end": 50})
    verdict = investigator._finalize(
        {
            "verdict": "confirmed",
            "evidence_ids": ["obs_1"],
            "evidence_quote": "important_fact()",
            "assessment": _assessment("important_fact()", "obs_1"),
        },
        _hypothesis(),
        {"a.py"},
        steps=2,
    )
    assert verdict.verdict == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["read_file", "read_diff", "grep", "find_definition", "find_callers"])
async def test_all_tool_views_hide_unsaved_proof_and_still_reject_it(tool):
    content = "x" * 1200 + "unsaved_proof()"

    async def execute(name, args):
        return content

    investigator = Investigator(_ScriptedToolLLM(), execute)
    result = await investigator._run_tool(tool, {"path": "a.py"})
    observation = investigator._observations[0]
    assert "unsaved_proof()" not in result
    assert observation.excerpt == "x" * 1200 and observation.excerpt in result
    assert observation.status == "success"
    verdict = investigator._finalize(
        {"verdict": "confirmed", "assessment": _assessment("unsaved_proof()", "obs_0")},
        _hypothesis(),
        {"a.py"},
        steps=1,
    )
    assert verdict.verdict == "unknown" and verdict.reason == "ungrounded-assessment"


@pytest.mark.asyncio
async def test_wide_read_requires_narrow_recording_before_model_receives_the_proof(tmp_path):
    class RecordingLLM(_ScriptedToolLLM):
        seen: list = Field(default_factory=list)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.seen.append(list(messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    contract = "assert transform('valid') == 'ready'"
    behavior = "def transform(value):\n    return None"
    workspace = _source_workspace(
        tmp_path, "# Copyright header before the relevant source.\n" * 79 + contract + "\n" + behavior
    )
    state = _state()
    assessment = _assessment("return None", "obs_1")
    assessment["expected"] = "Valid input must produce ready."
    assessment["expected_evidence"] = [{"observation_id": "obs_1", "quote": contract}]
    llm = RecordingLLM(
        turns=[
            {"name": "read_file", "args": {"path": "a.py", "start": 1, "end": 100}, "id": "wide"},
            {"name": "read_file", "args": {"path": "a.py", "start": 80, "end": 82}, "id": "narrow"},
            json.dumps({"verdict": "confirmed", "assessment": assessment}),
        ]
    )
    result = await Investigator(llm, build_workspace_executor(workspace, state)).investigate(
        _hypothesis(), state, ContextPack()
    )
    assert result.verdict == "confirmed" and result.assessment is not None
    assert len(result.observations) == 2
    assert contract not in result.observations[0].excerpt
    assert contract in result.observations[1].excerpt and behavior in result.observations[1].excerpt
    first_view = [message.content for message in llm.seen[1] if message.type == "tool"]
    assert first_view and all(contract not in content and "return None" not in content for content in first_view)
    second_view = [message.content for message in llm.seen[2] if message.type == "tool"]
    assert any(contract in content and behavior in content for content in second_view)


@pytest.mark.asyncio
async def test_confirmed_downgraded_when_ungrounded() -> None:
    hypothesis = _hypothesis()
    llm = _ScriptedToolLLM(
        turns=[
            _read_file_call(),
            json.dumps(
                {
                    "verdict": "confirmed",
                    "answer": "输入确实可为空",
                    "evidence_ids": ["obs_0"],
                    "evidence_quote": "return user_input",
                    "severity": "error",
                    "additional_sites": [],
                    "reason": "未校验",
                }
            ),
        ]
    )
    investigator = Investigator(llm, _executor({("read_file", (("path", "a.py"),)): "No results"}))
    result = await investigator.investigate(hypothesis, _state(), ContextPack())

    assert result.verdict == "unknown"
    assert result.reason == "ungrounded"
    assert result.observations[0].status == "not_found"


@pytest.mark.asyncio
async def test_refuted_on_not_found_only_is_downgraded() -> None:
    hypothesis = _hypothesis()
    llm = _ScriptedToolLLM(
        turns=[
            _read_file_call(),
            json.dumps(
                {
                    "verdict": "refuted",
                    "answer": "找不到下游使用",
                    "evidence_ids": ["obs_0"],
                    "evidence_quote": "No results",
                    "severity": "warning",
                    "additional_sites": [],
                    "reason": "搜不到调用方",
                }
            ),
        ]
    )
    investigator = Investigator(llm, _executor({("read_file", (("path", "a.py"),)): "No results"}))
    result = await investigator.investigate(hypothesis, _state(), ContextPack())

    assert result.verdict == "unknown"
    assert result.strength == "none"


@pytest.mark.asyncio
async def test_grounded_confirmed_keeps_strength() -> None:
    hypothesis = _hypothesis()
    content = "def f():\n    return user_input"
    llm = _ScriptedToolLLM(
        turns=[
            _read_file_call(),
            json.dumps(
                {
                    "verdict": "confirmed",
                    "answer": "函数直接把 user_input 返回给用户",
                    "evidence_ids": ["obs_0"],
                    "evidence_quote": "return user_input",
                    "severity": "error",
                    "additional_sites": [],
                    "reason": "缺少校验",
                    "assessment": _assessment("return user_input"),
                }
            ),
        ]
    )
    executor = _executor({("read_file", (("path", "a.py"),)): content})
    investigator = Investigator(llm, executor)

    result = await investigator.investigate(hypothesis, _state(), ContextPack(), changed_paths={"a.py"})
    assert result.verdict == "confirmed"
    assert result.strength == "weak"  # observation path is inside the diff
    assert result.observations[0].status == "success"
    assert result.observations[0].excerpt == content


@pytest.mark.asyncio
async def test_outside_diff_evidence_is_strong() -> None:
    hypothesis = _hypothesis()
    content = "def validate(x):\n    return x is not None"
    llm = _ScriptedToolLLM(
        turns=[
            {"name": "read_file", "args": {"path": "lib/validate.py"}, "id": "call_1"},
            json.dumps(
                {
                    "verdict": "refuted",
                    "answer": "上游已有非空校验",
                    "evidence_ids": ["obs_0"],
                    "evidence_quote": "return x is not None",
                    "severity": "warning",
                    "additional_sites": [],
                    "reason": "调用方已校验",
                    "assessment": _assessment("return x is not None", comparison="compatible"),
                }
            ),
        ]
    )
    executor = _executor({("read_file", (("path", "lib/validate.py"),)): content})
    investigator = Investigator(llm, executor)

    result = await investigator.investigate(hypothesis, _state(), ContextPack(), changed_paths={"a.py"})
    assert result.verdict == "refuted"
    assert result.strength == "strong"


def test_apply_verdict_transitions_and_merges_observations() -> None:
    ledger = HypothesisLedger("run", "abc123", "digest")
    hypothesis = _hypothesis()
    ledger.upsert(hypothesis)

    from reviewforge.engine.hypothesis import Observation

    first = Observation(
        id="obs_1",
        tool="read_file",
        query="q",
        path="a.py",
        line_range=None,
        sha="s",
        result_digest="d",
        excerpt="x",
        status="success",
    )
    second = Observation(
        id="obs_2",
        tool="grep",
        query="q2",
        path="a.py",
        line_range=None,
        sha="s",
        result_digest="d2",
        excerpt="y",
        status="success",
    )

    ledger.apply_verdict(
        hypothesis.identity, status="confirmed", evidence_strength="weak", verdict_reason="ok", observations=[first]
    )
    ledger.apply_verdict(
        hypothesis.identity,
        status="confirmed",
        evidence_strength="strong",
        verdict_reason="more",
        observations=[second],
    )

    item = ledger.items[hypothesis.identity]
    assert item.status.value == "confirmed"
    assert item.evidence_strength == "strong"
    assert item.attempts == 2
    assert {observation.id for observation in item.observations} == {"obs_1", "obs_2"}


def test_concurrent_upsert_never_loses_sites() -> None:
    ledger = HypothesisLedger("run", "abc123", "digest")

    def add_site(line: int) -> None:
        site = Site(path="a.py", line=line, excerpt="return user_input")
        ledger.upsert(
            Hypothesis(
                id="h_test",
                identity="a.py:f::null-path::f",
                unit_id="a.py:f",
                mechanism=Mechanism.NULL_PATH,
                claim="未处理空值",
                trigger="t",
                impact="i",
                open_question="q",
                refutation="r",
                sites=[site],
                severity="warning",
                source="generator",
            )
        )

    threads = [threading.Thread(target=add_site, args=(line,)) for line in range(1, 21)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(ledger.items["a.py:f::null-path::f"].sites) == 20


def _assessment(quote: str, identity: str = "obs_0", comparison: str = "conflict") -> dict:
    # These existing tests exercise citation layout, strength and budgets.
    # Independent-premise semantic fixtures live in test_investigation_assessment.
    return {
        "expected": "The quoted contract governs the claimed behavior.",
        "actual": "The quoted source establishes the behavior under review.",
        "comparison": comparison,
        "expected_evidence": [{"observation_id": identity, "quote": quote}],
        "actual_evidence": [{"observation_id": identity, "quote": quote}],
    }


def _verdict(*, quote: str, ids: list[str] | None = None, sites: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "verdict": "confirmed",
            "evidence_ids": ids or ["obs_0"],
            "evidence_quote": quote,
            "additional_sites": sites or [],
            "assessment": _assessment(quote, (ids or ["obs_0"])[0]),
        }
    )


@pytest.mark.asyncio
async def test_not_found_inside_source_is_successful_evidence() -> None:
    content = 'def f():\n    raise ValueError("not found")'
    llm = _ScriptedToolLLM(turns=[_read_file_call(), _verdict(quote='raise ValueError("not found")')])
    result = await Investigator(llm, _executor({("read_file", (("path", "a.py"),)): content})).investigate(
        _hypothesis(), _state(), ContextPack()
    )

    assert result.verdict == "confirmed"
    assert result.observations[0].status == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("glob", ["", "*.py"])
async def test_search_scope_is_not_an_outside_diff_file(glob: str) -> None:
    call = {"name": "grep", "args": {"pattern": "return user_input", "glob": glob}, "id": "search"}
    executor = _executor({("grep", tuple(sorted(call["args"].items()))): "- a.py:2: return user_input"})
    llm = _ScriptedToolLLM(turns=[call, _verdict(quote="return user_input")])
    result = await Investigator(llm, executor).investigate(_hypothesis(), _state(), ContextPack())

    assert result.verdict == "confirmed"
    assert result.strength == "weak"


@pytest.mark.asyncio
async def test_unquoted_external_observation_does_not_promote_strength() -> None:
    llm = _ScriptedToolLLM(
        turns=[
            _read_file_call(),
            _read_file_call("lib/unrelated.py"),
            _verdict(quote="return user_input", ids=["obs_0", "obs_1"]),
        ]
    )
    executor = _executor(
        {
            ("read_file", (("path", "a.py"),)): "return user_input",
            ("read_file", (("path", "lib/unrelated.py"),)): "unrelated_constant = 42",
        }
    )
    result = await Investigator(llm, executor).investigate(_hypothesis(), _state(), ContextPack())

    assert result.verdict == "confirmed"
    assert result.strength == "weak"


@pytest.mark.asyncio
async def test_additional_sites_require_verbatim_right_side_evidence() -> None:
    sites = [
        {"path": "a.py", "line": 2, "excerpt": "return user_input"},
        {"path": "a.py", "line": 2, "excerpt": "invented code"},
        {"path": "a.py", "line": 999, "excerpt": "return user_input"},
        {"path": "lib/other.py", "line": 2, "excerpt": "return user_input"},
    ]
    llm = _ScriptedToolLLM(turns=[_read_file_call(), _verdict(quote="return user_input", sites=sites)])
    result = await Investigator(llm, _executor({("read_file", (("path", "a.py"),)): "return user_input"})).investigate(
        _hypothesis(), _state(diffs={"a.py": "@@ -0,0 +1,2 @@\n+def f():\n+    return user_input\n"}), ContextPack()
    )

    assert result.additional_sites == [Site(path="a.py", line=2, excerpt="return user_input")]


@pytest.mark.asyncio
async def test_resume_does_not_reuse_observation_ids() -> None:
    from reviewforge.engine.hypothesis import Observation

    hypothesis = _hypothesis()
    hypothesis.observations = [Observation("obs_0", "grep", "q", "", None, "abc123", "d", "No results", "not_found")]
    llm = _ScriptedToolLLM(turns=[_read_file_call(), _verdict(quote="return user_input", ids=["obs_1"])])
    result = await Investigator(llm, _executor({("read_file", (("path", "a.py"),)): "return user_input"})).investigate(
        hypothesis, _state(), ContextPack()
    )

    assert result.verdict == "confirmed"
    assert result.observations[-1].id == "obs_1"


@pytest.mark.asyncio
async def test_repeated_tool_call_executes_at_most_twice() -> None:
    calls = []

    async def execute(name, args):
        calls.append((name, args))
        return "return user_input"

    llm = _ScriptedToolLLM(
        turns=[_read_file_call(), _read_file_call(), _read_file_call(), _verdict(quote="return user_input")]
    )
    result = await Investigator(llm, execute).investigate(_hypothesis(), _state(), ContextPack())

    assert len(calls) == 2
    assert len(result.observations) == 2
    assert result.verdict == "confirmed"


@pytest.mark.asyncio
async def test_search_quote_from_outside_diff_is_strong() -> None:
    call = {"name": "grep", "args": {"pattern": "return user_input"}, "id": "search"}
    executor = _executor({("grep", (("pattern", "return user_input"),)): "- lib/caller.py:2: return user_input"})
    llm = _ScriptedToolLLM(turns=[call, _verdict(quote="return user_input")])
    result = await Investigator(llm, executor).investigate(_hypothesis(), _state(), ContextPack())

    assert result.verdict == "confirmed"
    assert result.strength == "strong"


@pytest.mark.asyncio
async def test_api_fallback_executor_uses_async_pinned_reader() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    workspace = SimpleNamespace(
        source="api-fallback",
        read=Mock(side_effect=AssertionError("sync API read")),
        read_async=AsyncMock(return_value="line 1\nline 2\npinned content\nline 4\nline 5"),
    )
    result = await build_workspace_executor(workspace, _state())("read_file", {"path": "a.py", "start": 3, "end": 5})
    assert result == "pinned content\nline 4\nline 5"
    workspace.read_async.assert_awaited_once_with("a.py")


@pytest.mark.asyncio
async def test_windowed_source_supports_exact_multiline_citation(tmp_path) -> None:
    source = "header\n    verifySafeHtml();\n} catch (IOException e) {\n    throw error;\nfooter\n"
    workspace = _source_workspace(tmp_path, source)
    investigator = Investigator(_ScriptedToolLLM(), build_workspace_executor(workspace, _state()))
    await investigator._run_tool("read_file", {"path": "a.py", "start": 2, "end": 4})
    observation = investigator._observations[0]
    quote = "verifySafeHtml();\n} catch (IOException e) {\n    throw error;"

    assert quote in observation.excerpt
    assert observation.line_range == (2, 4)
    assert "footer" not in observation.excerpt
    result = investigator._finalize(
        {
            "verdict": "refuted",
            "evidence_ids": ["obs_0"],
            "evidence_quote": quote,
            "assessment": _assessment(quote, comparison="compatible"),
        },
        _hypothesis(),
        {"a.py"},
        steps=1,
    )
    assert result.verdict == "refuted"
    fabricated = investigator._finalize(
        {
            "verdict": "refuted",
            "evidence_ids": ["obs_0"],
            "evidence_quote": quote.replace("throw error", "return safely"),
        },
        _hypothesis(),
        {"a.py"},
        steps=1,
    )
    assert fabricated.verdict == "unknown" and fabricated.reason == "ungrounded"


@pytest.mark.asyncio
async def test_windowed_source_preserves_real_numeric_prefixes_and_whitespace(tmp_path) -> None:
    source = "ignore\n42: literal prefix  \n\n    indented content\n"
    workspace = _source_workspace(tmp_path, source)
    executor = build_workspace_executor(workspace, _state())

    assert (
        await executor("read_file", {"path": "a.py", "start": 2, "end": 4})
        == "42: literal prefix  \n\n    indented content\n"
    )
    assert await executor("read_file", {"path": "a.py"}) == source


@pytest.mark.asyncio
async def test_raw_source_citation_still_rejects_unsaved_context(tmp_path) -> None:
    source = "x" * 1500 + "\nnot_saved_proof()\n"
    workspace = _source_workspace(tmp_path, source)
    investigator = Investigator(_ScriptedToolLLM(), build_workspace_executor(workspace, _state()))
    await investigator._run_tool("read_file", {"path": "a.py", "start": 1, "end": 2})

    assert len(investigator._observations[0].excerpt) == 1200
    result = investigator._finalize(
        {"verdict": "confirmed", "evidence_ids": ["obs_0"], "evidence_quote": "not_saved_proof()"},
        _hypothesis(),
        {"a.py"},
        steps=1,
    )
    assert result.verdict == "unknown" and result.reason == "ungrounded"


@pytest.mark.asyncio
async def test_diff_window_records_before_after_evidence_past_the_full_diff_limit():
    from types import SimpleNamespace

    old = "message=existing language defect with <a href='old'>link</a>"
    new = "message=existing language defect with link"
    patch = "@@ -1 +1 @@\n-" + "x" * 7000 + "\n+" + "y" * 7000 + f"\n@@ -101 +101 @@\n-{old}\n+{new}\n"
    state = _state(diffs={"a.py": patch})
    executor = build_workspace_executor(SimpleNamespace(), state)
    assert await executor("read_diff", {"path": "a.py"}) == patch  # Old callers keep the full view.
    investigator = Investigator(_ScriptedToolLLM(), executor)
    await investigator._run_tool("read_diff", {"path": "a.py"})
    assert old not in investigator._observations[0].excerpt
    result = await investigator._run_tool("read_diff", {"path": "a.py", "start": 101, "end": 101})
    assert "@@ -1 +1 @@" not in result
    assert old in investigator._observations[1].excerpt and new in investigator._observations[1].excerpt
    verdict = investigator._finalize(
        {
            "verdict": "refuted",
            "evidence_ids": ["obs_1"],
            "evidence_quote": old,
            "assessment": _assessment(old, "obs_1", "compatible"),
        },
        _hypothesis(),
        {"a.py"},
        steps=2,
    )
    assert verdict.verdict == "refuted"


@pytest.mark.asyncio
async def test_missing_diff_window_cannot_be_evidence_of_refutation():
    from types import SimpleNamespace

    investigator = Investigator(_ScriptedToolLLM(), build_workspace_executor(SimpleNamespace(), _state()))
    await investigator._run_tool("read_diff", {"path": "a.py", "start": 900, "end": 900})
    assert investigator._observations[0].status == "not_found"


@pytest.mark.asyncio
async def test_diff_window_tool_schema_accepts_line_coordinates():
    investigator = Investigator(_ScriptedToolLLM(), _executor({}))
    schema = next(tool for tool in investigator._build_tools() if tool.name == "read_diff").args
    assert "start" in schema and "end" in schema


@pytest.mark.asyncio
async def test_investigator_stops_when_token_budget_is_exhausted() -> None:
    class ExpensiveLLM(_ScriptedToolLLM):
        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            result = super()._generate(messages, stop, run_manager, **kwargs)
            result.generations[0].message.usage_metadata = {
                "input_tokens": 7900,
                "output_tokens": 100,
                "total_tokens": 8000,
            }
            return result

    llm = ExpensiveLLM(turns=[_read_file_call(), _verdict(quote="return user_input")])
    result = await Investigator(llm, _executor({}), max_steps=2).investigate(_hypothesis(), _state(), ContextPack())
    assert result.verdict == "unknown" and result.reason == "token-exhausted"
    assert result.tokens == 8000
    assert len(llm.turns) == 1


class _BudgetAwareLLM(_ScriptedToolLLM):
    calls: list[dict] = Field(default_factory=list)
    quote: str = "return user_input"
    fail_final: bool = False

    def bind_tools(self, tools, **kwargs):
        return self.bind(investigation_tools=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        tools_enabled = kwargs.get("investigation_tools", False)
        self.calls.append({"tools": tools_enabled, "messages": list(messages), "limit": kwargs.get("max_tokens")})
        if tools_enabled:
            message = AIMessage(content="", tool_calls=[_read_file_call()])
            input_tokens, output_tokens = 2500, 100
        else:
            if self.fail_final:
                raise RuntimeError("closing provider failure")
            message = AIMessage(content=_verdict(quote=self.quote))
            input_tokens, output_tokens = 2000, 200
        message.usage_metadata = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.mark.asyncio
async def test_investigator_reserves_a_grounded_verdict_before_replaying_long_tools():
    llm = _BudgetAwareLLM()
    content = "return user_input\n" + "x" * 5980
    result = await Investigator(llm, _executor({("read_file", (("path", "a.py"),)): content}), max_steps=2).investigate(
        _hypothesis(), _state(), ContextPack()
    )

    assert result.verdict == "confirmed"
    assert result.tokens <= 8000
    assert [call["tools"] for call in llm.calls] == [True, False]
    closing = llm.calls[-1]["messages"]
    assert "return user_input" in str(closing)
    assert "obs_0" in str(closing)
    assert "x" * 1200 not in str(closing)  # Only code-written, saved excerpts are needed to decide.
    assert not any(message.type == "tool" for message in closing)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "quote"),
    [("x" * 1200 + "not saved fact" + "x" * 4780, "not saved fact"), ("No results", "No results")],
)
async def test_budget_closure_cannot_promote_unsaved_or_not_found_evidence(content, quote):
    llm = _BudgetAwareLLM(quote=quote)
    result = await Investigator(llm, _executor({("read_file", (("path", "a.py"),)): content}), max_steps=2).investigate(
        _hypothesis(), _state(), ContextPack()
    )
    assert result.verdict == "unknown" and result.reason == "ungrounded"
    if quote == "not saved fact":
        assert quote not in str(llm.calls[-1]["messages"])


@pytest.mark.asyncio
async def test_budget_closure_provider_failure_is_retryable():
    llm = _BudgetAwareLLM(fail_final=True)
    result = await Investigator(
        llm, _executor({("read_file", (("path", "a.py"),)): "return user_input\n" + "x" * 5980}), max_steps=2
    ).investigate(_hypothesis(), _state(), ContextPack())
    assert result.verdict == "unknown" and result.retryable
    assert "provider error" in result.reason
    assert [call["tools"] for call in llm.calls] == [True, False]


@pytest.mark.asyncio
async def test_single_step_budget_can_read_evidence_and_close():
    class CheapLLM(_BudgetAwareLLM):
        def _generate(self, *args, **kwargs):
            result = super()._generate(*args, **kwargs)
            result.generations[0].message.usage_metadata = {
                "input_tokens": 500,
                "output_tokens": 100,
                "total_tokens": 600,
            }
            return result

    llm = CheapLLM()
    result = await Investigator(
        llm, _executor({("read_file", (("path", "a.py"),)): "return user_input"}), max_steps=1
    ).investigate(_hypothesis(), _state(), ContextPack())
    assert result.verdict == "confirmed" and result.tokens == 1200
    assert [call["tools"] for call in llm.calls] == [True, False]


def test_input_forecast_includes_tool_arguments_and_measured_provider_overhead():
    from types import SimpleNamespace

    small = [AIMessage(content="", tool_calls=[_read_file_call()])]
    large = [AIMessage(content="", tool_calls=[_read_file_call("x" * 8000)])]
    assert Investigator._estimate_input_tokens(large) > Investigator._estimate_input_tokens(small) + 1900

    investigator = Investigator(_ScriptedToolLLM(), _executor({}))
    response = SimpleNamespace(
        content="ok",
        usage_metadata={},
        response_metadata={"token_usage": {"prompt_tokens": 3500, "completion_tokens": 50}},
    )
    investigator._record_tokens(response, small, schema_tokens=500)
    assert investigator._tokens == 3550
    assert investigator._estimate_input_tokens(small) + 500 + investigator._input_overhead == 3500
