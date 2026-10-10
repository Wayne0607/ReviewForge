from __future__ import annotations

import json
import threading

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Site
from reviewforge.engine.investigator import Investigator, budget_steps, build_workspace_executor
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind


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
        return results.get(key, "No results")

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


@pytest.mark.asyncio
async def test_long_tool_output_identifies_saved_citation_boundary_and_narrow_read():
    content = "x" * 1200 + "\n50: important_fact()\n" + "y" * 5500
    investigator = Investigator(_ScriptedToolLLM(), _executor({("read_file", (("path", "a.py"),)): content}))
    result = await investigator._run_tool("read_file", {"path": "a.py"})
    observation = investigator._observations[0]
    assert len(result) <= 6000
    assert "important_fact()" not in observation.excerpt
    assert result.index("End saved evidence excerpt") < result.index("important_fact()")
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
        },
        _hypothesis(),
        {"a.py"},
        steps=2,
    )
    assert verdict.verdict == "confirmed"


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


def _verdict(*, quote: str, ids: list[str] | None = None, sites: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "verdict": "confirmed",
            "evidence_ids": ids or ["obs_0"],
            "evidence_quote": quote,
            "additional_sites": sites or [],
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
        {"verdict": "refuted", "evidence_ids": ["obs_0"], "evidence_quote": quote}, _hypothesis(), {"a.py"}, steps=1
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
        {"verdict": "refuted", "evidence_ids": ["obs_1"], "evidence_quote": old},
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
