from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, Mechanism, Observation, Site
from reviewforge.engine.investigation_tasks import build_investigation_tasks, fair_investigation_tasks
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit, UnitKind


def candidate(anchor="run", *, claim="missing calendar dereference", severity="error"):
    return Hypothesis(
        f"h_{anchor}",
        f"a.ts::{claim}::{anchor}",
        f"unit_{anchor}",
        Mechanism.NULL_PATH,
        claim,
        "calendar absent",
        "TypeError",
        "Can calendar be absent?",
        "caller rejects missing calendar",
        [Site("a.ts", 12, "calendar?.id")],
        severity,
        "generator",
    )


def state():
    return StateStore(
        repo="owner/repo", pr_number=1, head_sha="head", files_changed=["a.ts"], file_diffs={"a.ts": "patch"}
    )


def refute(reference="obs_1:e1"):
    return json.dumps(
        {
            "assessment": None,
            "counterevidence": {
                "premise": "mechanism",
                "statement": "optional access guards the missing calendar",
                "evidence": [reference],
            },
        }
    )


def unknown():
    return json.dumps({"assessment": None, "counterevidence": None, "reason": "input contract missing"})


class RecordingLLM(BaseChatModel):
    turns: list = Field(default_factory=list)
    calls: list = Field(default_factory=list)
    usages: list = Field(default_factory=list)

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools_enabled=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(
            {"messages": list(messages), "limit": kwargs.get("max_tokens"), "tools": kwargs.get("tools_enabled", False)}
        )
        turn = self.turns.pop(0) if self.turns else unknown()
        message = AIMessage(content=turn) if isinstance(turn, str) else AIMessage(content="", tool_calls=turn)
        if self.usages:
            input_tokens, output_tokens = self.usages.pop(0)
            message.usage_metadata = {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "total_tokens": input_tokens + output_tokens,
            }
        return ChatResult(generations=[ChatGeneration(message=message)])

    @property
    def _llm_type(self):
        return "recording-investigation"


def source_executor(calls):
    async def execute(name, args):
        calls.append((name, args))
        if name == "read_diff":
            return "@@ -12 +12 @@\n-return calendar.id\n+return calendar?.id\n"
        return "return calendar?.id"

    return execute


@pytest.mark.asyncio
async def test_bootstrap_delivers_real_before_after_and_anchor_for_immediate_counterevidence():
    calls = []
    llm = RecordingLLM(turns=[refute()])
    result = await Investigator(llm, source_executor(calls), protocol_version=3).investigate(
        candidate(), state(), ContextPack()
    )
    assert result.verdict == "refuted" and result.steps == 2
    assert [name for name, _ in calls] == ["read_diff", "read_file"]
    prompt = str(llm.calls[0]["messages"])
    assert "-return calendar.id" in prompt and "+return calendar?.id" in prompt
    assert "obs_1:e1" in prompt
    assert len(llm.calls) == 1
    assert result.counterevidence.evidence[0].observation_id == "obs_1"
    assert any(event["phase"] == "bootstrap" for event in result.progress)


@pytest.mark.asyncio
async def test_bootstrap_uses_the_innermost_real_site_in_the_declared_file():
    calls = []
    item = replace(candidate(), unit_id="class", sites=[Site("a.ts", 8, "other"), Site("z.ts", 12, "calendar?.id")])
    changeset = SemanticChangeSet(
        "owner/repo",
        1,
        "head",
        units=[
            SemanticUnit("class", "z.ts", "typescript", UnitKind.SYMBOL, "Service", 1, 99),
            SemanticUnit("method", "z.ts", "typescript", UnitKind.SYMBOL, "run", 10, 18),
        ],
    )
    result = await Investigator(
        RecordingLLM(turns=[refute()]),
        source_executor(calls),
        protocol_version=3,
        changeset=changeset,
    ).investigate(item, state(), ContextPack())
    assert calls == [
        ("read_diff", {"path": "z.ts", "start": 12, "end": 12}),
        ("read_file", {"path": "z.ts", "start": 9, "end": 19}),
    ]
    assert result.verdict == "refuted"


@pytest.mark.asyncio
async def test_shared_source_results_keep_nearby_claims_independent_and_auditable():
    calls = []
    entered = asyncio.Event()

    async def execute(name, args):
        calls.append((name, args))
        if name == "read_diff":
            entered.set()
            await asyncio.sleep(0)
            return "-return calendar.id\n+return calendar?.id"
        return "return calendar?.id"

    ledger = HypothesisLedger("run", "head", "digest", protocol_version=3)
    ledger.upsert(candidate(claim="missing calendar dereference"))
    ledger.upsert(candidate(claim="missing calendar loses business data"))
    # Same source can settle one mechanism and leave the other contract unproven.
    llm = RecordingLLM(turns=[refute(), unknown()])
    await Investigator(llm, execute, protocol_version=3).run(ledger, state(), ContextPack(), concurrency=2)
    items = list(ledger.items.values())
    assert len(calls) == 2
    assert {item.status.value for item in items} == {"refuted", "unknown"}
    assert len(ledger.investigation_tasks) == 2
    assert items[0].observations[0].shared_result_id == items[1].observations[0].shared_result_id
    assert items[0].observations[0].shared_result_id
    assert all(item.investigation_progress for item in items)
    assert HypothesisLedger.from_dict(ledger.to_dict()).to_dict() == ledger.to_dict()


@pytest.mark.asyncio
async def test_caller_navigation_follows_actual_production_call_before_unknown():
    calls = []

    async def execute(name, args):
        calls.append((name, args))
        if name == "find_callers":
            return "\n".join(
                [
                    "- src/search.go:59: \tBuildIndex(ctx context.Context,",
                    "- src/search_test.go:20: BuildIndex(input)",
                    "- src/search.go:326: index, err := s.search.BuildIndex(ctx, key, reader, fields, 0)",
                ]
            )
        return "return calendar?.id" if name == "read_file" else "+return calendar?.id"

    llm = RecordingLLM(turns=[[{"name": "find_callers", "args": {"symbol": "BuildIndex"}, "id": "caller"}], unknown()])
    result = await Investigator(llm, execute, protocol_version=3).investigate(candidate(), state(), ContextPack())
    reads = [args for name, args in calls if name == "read_file"]
    assert reads == [{"path": "a.ts", "start": 9, "end": 20}, {"path": "src/search.go", "start": 318, "end": 338}]
    assert result.verdict == "unknown"
    assert "src/search.go" in str(llm.calls[1]["messages"])
    assert any(event["phase"] == "caller-followup" for event in result.progress)


@pytest.mark.asyncio
async def test_large_diff_records_target_before_after_without_inventing_a_short_hunk():
    patch = "@@ -1,101 +1,101 @@\n" + (" " + "padding" * 30 + "\n") * 99
    patch += "-return calendar.id\n+return calendar?.id\n tail\n"
    pr_state = state()
    pr_state.file_diffs["a.ts"] = patch
    item = replace(candidate(), sites=[Site("a.ts", 100, "return calendar?.id")])

    async def execute(name, args):
        return patch if name == "read_diff" else "return calendar?.id"

    llm = RecordingLLM(turns=[refute()])
    result = await Investigator(llm, execute, protocol_version=3).investigate(item, pr_state, ContextPack())
    observation = result.observations[0]
    assert "-return calendar.id\n+return calendar?.id" in observation.excerpt
    assert observation.excerpt in patch
    assert "@@" not in observation.excerpt
    initial = str(llm.calls[0]["messages"])
    assert "navigation_only" in initial and "before_after_complete" in initial
    assert result.verdict == "refuted"


@pytest.mark.asyncio
@pytest.mark.parametrize("closing", [False, True])
async def test_request_budget_rejection_is_budget_failure_without_provider_retry(closing):
    from reviewforge.engine.request_budget import RequestBudgetExceededError

    class RefusingLLM(RecordingLLM):
        def _generate(self, *args, **kwargs):
            raise RequestBudgetExceededError("test admission exhausted")

    result = await Investigator(
        RefusingLLM(),
        source_executor([]),
        protocol_version=3,
        max_steps=1 if closing else 6,
    ).investigate(candidate(), state(), ContextPack())
    assert result.verdict == "unknown" and result.outcome == "budget-exhausted"
    assert not result.retryable


@pytest.mark.asyncio
async def test_structured_missing_premise_requests_only_valid_read_only_navigation():
    calls = []
    progress = json.dumps(
        {
            "progress": {
                "missing_premise": "trigger",
                "question": "Does the named caller allow missing input?",
                "next_evidence": {"tool": "read_file", "args": {"path": "entry.ts", "start": 1, "end": 5}},
            }
        }
    )
    llm = RecordingLLM(turns=[progress, unknown()])
    result = await Investigator(llm, source_executor(calls), protocol_version=3).investigate(
        candidate(), state(), ContextPack()
    )
    assert calls[-1] == ("read_file", {"path": "entry.ts", "start": 1, "end": 5})
    assert result.verdict == "unknown"
    assert any(event.get("missing_premise") == "trigger" for event in result.progress)
    bad = json.loads(progress)
    bad["progress"]["next_evidence"]["tool"] = "write_file"
    result = await Investigator(
        RecordingLLM(turns=[json.dumps(bad)]), source_executor([]), protocol_version=3
    ).investigate(candidate(), state(), ContextPack())
    assert result.verdict == "unknown" and result.outcome == "output-error"


@pytest.mark.asyncio
async def test_same_evidence_query_closes_without_repeated_io_or_more_exploration():
    calls = []
    repeated = [{"name": "read_file", "args": {"path": "a.ts", "start": 9, "end": 20}, "id": "repeat"}]
    llm = RecordingLLM(turns=[repeated, refute()])
    result = await Investigator(llm, source_executor(calls), protocol_version=3).investigate(
        candidate(), state(), ContextPack()
    )
    assert result.verdict == "refuted"
    assert [call["tools"] for call in llm.calls] == [True, False]
    assert len(calls) == 2 and len(result.observations) == 2


@pytest.mark.asyncio
async def test_small_output_budget_skips_exploration_but_keeps_grounded_closure(monkeypatch):
    # Reproduce the 114-token case without sending a futile exploration call.
    llm = RecordingLLM(turns=[refute()])
    value = Investigator(llm, source_executor([]), protocol_version=3, max_steps=2)
    monkeypatch.setattr(value, "_estimate_input_tokens", lambda chat: 1000)
    monkeypatch.setattr(value, "_estimate_text_tokens", lambda text: 3886)
    result = await value.investigate(candidate(), state(), ContextPack())
    assert result.verdict == "refuted"
    assert [call["tools"] for call in llm.calls] == [False]
    assert any(
        event.get("event") == "close-before-small-output" and event["output_tokens"] == 114 for event in result.progress
    )


def test_same_anchor_variants_do_not_take_every_task_slot_and_different_severity_stays_first():
    items = [candidate(claim=f"risk {index}") for index in range(14)]
    items += [candidate("other"), candidate("third"), candidate("low", severity="warning")]
    tasks = build_investigation_tasks(items, head_sha="head", workspace_digest="digest")
    ordered = fair_investigation_tasks(tasks)
    assert {task.hypothesis.identity.rsplit("::", 1)[-1] for task in ordered[:3]} == {"run", "other", "third"}
    assert len(ordered) == len(tasks)
    assert ordered[-1].hypothesis.severity == "warning"


@pytest.mark.asyncio
async def test_bootstrap_saved_evidence_and_context_share_original_context_allowance():
    pack = ContextPack(
        units={
            "unit_run": UnitContext(
                "unit_run", [ContextSlice("caller", "entry.ts", 1, 2, "main", "x" * 12000, "caller", "head")]
            )
        }
    )
    llm = RecordingLLM(turns=[unknown()])
    result = await Investigator(llm, source_executor([]), protocol_version=3).investigate(candidate(), state(), pack)
    initial = llm.calls[0]["messages"]
    context = initial[1].content.split("## Context\n")[1].split("\n\n", 1)[0]
    assert len(context) + len(initial[2].content.removeprefix("## Recorded observations\n")) <= 12000
    assert result.verdict == "unknown"


@pytest.mark.asyncio
async def test_discovery_trigger_navigation_is_read_as_new_evidence_and_charged_to_context():
    calls = []
    basis = {
        "behavior": {"before": "old", "after": "new"},
        "trigger_basis": {"side": "context", "path": "entry.ts", "line": 8, "excerpt": "run(missing)"},
    }
    item = replace(candidate(), discovery_basis=basis)
    pack = ContextPack(
        units={
            "unit_run": UnitContext(
                "unit_run",
                [
                    ContextSlice("caller", "entry.ts", 8, 9, "main", "run(missing)\nnext()", "caller", "head"),
                ],
            )
        }
    )
    llm = RecordingLLM(turns=[unknown()])
    result = await Investigator(llm, source_executor(calls), protocol_version=3).investigate(item, state(), pack)
    assert calls[-1] == ("read_file", {"path": "entry.ts", "start": 5, "end": 16})
    assert result.observations[-1].path == "entry.ts"
    assert result.verdict == "unknown"
    initial = llm.calls[0]["messages"]
    assert "Discovery proof plan (navigation only; not saved evidence)" in initial[1].content
    assert any(event.get("tool_calls") == 3 for event in result.progress)
    forged = replace(
        item,
        discovery_basis={
            "trigger_basis": {"side": "context", "path": "invented.ts", "line": 8, "excerpt": "run(missing)"}
        },
    )
    rejected_calls = []
    await Investigator(
        RecordingLLM(turns=[unknown()]), source_executor(rejected_calls), protocol_version=3
    ).investigate(forged, state(), pack)
    assert len(rejected_calls) == 2


@pytest.mark.asyncio
async def test_unknown_retry_usage_stays_within_original_task_limit_and_is_persisted():
    from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
    from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget

    class TimeoutLLM(RecordingLLM):
        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            self.calls.append({"messages": messages})
            raise TimeoutError("scripted transport timeout")

    inner = TimeoutLLM()
    budget = RequestBudget(250_000)
    routed = RetryChatLLM(
        BudgetedChatLLM(inner, budget, phase="investigation"),
        policy=RetryPolicy(max_attempts=3, base_delay_seconds=0, max_delay_seconds=0),
    )
    ledger = HypothesisLedger("run", "head", "digest", protocol_version=3)
    ledger.upsert(candidate())
    ledger.upsert(candidate("second"))
    results = await Investigator(routed, source_executor([]), protocol_version=3, max_steps=4).run(
        ledger,
        state(),
        ContextPack(),
        token_budget=16_000,
    )
    result = results[0]
    assert result.tokens == 0
    assert 0 < result.unreported_tokens <= 16_000
    assert result.outcome == "budget-exhausted" and not result.retryable
    assert len(inner.calls) < 3
    item = next(iter(ledger.items.values()))
    assert item.investigation_unreported_tokens == result.unreported_tokens
    assert budget.snapshot()["unreported_reserved_tokens"] == result.unreported_tokens
    assert results[1].outcome == "policy-deferred"
    assert results[1].tokens == results[1].unreported_tokens == 0


@pytest.mark.asyncio
async def test_success_without_usage_is_unknown_cost_and_never_fabricated_as_paid_tokens():
    from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget

    budget = RequestBudget(250_000)
    llm = BudgetedChatLLM(RecordingLLM(turns=[refute()]), budget, phase="investigation")
    result = await Investigator(llm, source_executor([]), protocol_version=3).investigate(
        candidate(),
        state(),
        ContextPack(),
    )
    assert result.verdict == "refuted"
    assert result.tokens == 0 and result.unreported_tokens > 0
    assert budget.snapshot()["reported_tokens"] == 0
    assert budget.snapshot()["unreported_reserved_tokens"] == result.unreported_tokens


@pytest.mark.asyncio
async def test_wrong_head_checkpoint_observation_is_not_a_source_of_current_proof():
    old = Observation("obs_50", "read_file", "q", "old.ts", None, "old_head", "d", "old_fact", "success")
    item = replace(candidate(), observations=[old])
    llm = RecordingLLM(turns=[refute("obs_50:e1")])
    result = await Investigator(llm, source_executor([]), protocol_version=3).investigate(item, state(), ContextPack())
    assert result.verdict == "unknown" and result.outcome == "output-error"
    assert all(obs.sha == "head" for obs in result.observations)
