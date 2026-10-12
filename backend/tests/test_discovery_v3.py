from __future__ import annotations

import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field

from reviewforge.core.state import StateStore
from reviewforge.engine.context_pack import ContextPack, ContextSlice, UnitContext
from reviewforge.engine.discovery import DiscoveryLimits, plan_discovery
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.semantic_diff import SemanticChangeSet, SemanticUnit


def unit(path: str, name: str = "update", risk: float = 1) -> SemanticUnit:
    return SemanticUnit(id=f"{path}:{name}", path=path, symbol=name, start_line=1, end_line=3, risk_score=risk)


def diff(before: str = "return old_value", after: str = "return new_value") -> str:
    return f"@@ -1,1 +1,1 @@\n-{before}\n+{after}"


def changeset(*units: SemanticUnit) -> SemanticChangeSet:
    return SemanticChangeSet(head_sha="abc", units=list(units))


def fact(path: str, side: str, excerpt: str) -> dict:
    return {"side": side, "path": path, "line": 1, "excerpt": excerpt}


def behavior(path: str) -> dict:
    return {
        "before": "Returns the stored old value for the same input.",
        "after": "Returns the replacement value for the same input.",
        "observable": "The caller receives the replacement value.",
        "source_facts": [fact(path, "before", "return old_value"), fact(path, "after", "return new_value")],
    }


def clean(u: SemanticUnit) -> dict:
    return {"unit_id": u.id, "checked": "Compared the value returned to callers.", "behavior": behavior(u.path)}


def candidate(u: SemanticUnit) -> dict:
    return {
        "unit_id": u.id,
        "mechanism": "contract-mismatch",
        "claim": "The replacement value violates the caller's persisted-value contract.",
        "trigger": "A caller requests the previously persisted value.",
        "impact": "The caller receives a different value.",
        "open_question": "Does the caller require the persisted old value?",
        "refutation": "The caller contract explicitly allows replacing the returned value.",
        "severity": "warning",
        "sites": [{"path": u.path, "line": 1, "excerpt": "return new_value"}],
        "behavior": behavior(u.path),
        "discrepancy": "The return expression changed while the caller still uses the persisted value.",
        "trigger_basis": fact(u.path, "before", "return old_value"),
    }


class ScriptedLLM(BaseChatModel):
    responses: list[str] = Field(default_factory=list)
    calls: list[list] = Field(default_factory=list)
    limits: list[int | None] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(messages)
        self.limits.append(kwargs.get("max_tokens"))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.responses.pop(0)))])

    @property
    def _llm_type(self):
        return "discovery-test"


def test_plan_covers_every_unit_once_and_shares_hunks():
    units = [unit("shared.py", f"f{i}") for i in range(13)] + [unit(f"file{i}.py") for i in range(20)]
    diffs = {u.path: diff() for u in units}
    plan = plan_discovery(changeset(*units), ContextPack(), diffs, limits=DiscoveryLimits())
    ids = [u.id for block in plan for u in block.units]
    assert sorted(ids) == sorted(u.id for u in units)
    assert len(ids) == len(set(ids))
    assert all(len(block.units) <= 12 for block in plan)
    for block in plan:
        assert block.diff_text.count("-    1 | return old_value") == len({u.path for u in block.units})


def test_related_changed_file_is_grouped_and_navigation_survives_context_budget():
    caller, callee, unrelated = unit("a.py", "caller", 3), unit("z.py", "callee"), unit("b.py", "other", 2)
    relation = ContextSlice("callee", callee.path, 1, 1, callee.symbol, "return new_value", "calls callee", "abc")
    pack = ContextPack(units={caller.id: UnitContext(caller.id, [relation])}, pr_intent="Change return values")
    plan = plan_discovery(
        changeset(caller, callee, unrelated),
        pack,
        {u.path: diff() for u in (caller, callee, unrelated)},
        limits=DiscoveryLimits(max_units=2, context_max_chars=0),
    )
    assert [u.id for u in plan[0].units] == [caller.id, callee.id]
    assert "calls callee" in plan[0].navigation
    assert "z.py:1-1" in plan[0].navigation
    assert "b.py" in plan[0].overview
    assert not plan[0].context_text


def test_condition_inversion_has_numbered_before_after_and_focus():
    u = unit("create.ts")
    patch = diff("...(!FEATURE_ENABLED && { slug })", "...(FEATURE_ENABLED ? { slug } : {})")
    block = plan_discovery(changeset(u), ContextPack(), {u.path: patch}, limits=DiscoveryLimits())[0]
    assert "-    1 | ...(!FEATURE_ENABLED && { slug })" in block.diff_text
    assert "+    1 | ...(FEATURE_ENABLED ? { slug } : {})" in block.diff_text
    assert "condition" in block.focus
    assert "true" in block.focus and "false" in block.focus
    assert "bug" not in block.focus.lower()


def test_large_single_hunk_preserves_full_source_under_legacy_hard_cap():
    u = unit("large.py")
    patch = diff(after="return new_value # " + "x" * 35000)
    block = plan_discovery(changeset(u), ContextPack(), {u.path: patch}, limits=DiscoveryLimits())[0]
    assert block.oversize
    assert "x" * 35000 in block.diff_text
    assert not block.failure


def test_impossible_oversize_unions_do_not_rebuild_source_for_every_pair(monkeypatch):
    import reviewforge.engine.discovery as discovery_module

    count = 100
    units = [unit(f"file{i:03}.py") for i in range(count)]
    patch = diff(after="return new_value # " + "x" * 35000)
    original = discovery_module._block
    calls = []

    def counted(*args, **kwargs):
        calls.append([u.id for u in args[0]])
        return original(*args, **kwargs)

    monkeypatch.setattr(discovery_module, "_block", counted)
    plan = plan_discovery(changeset(*units), ContextPack(), {u.path: patch for u in units}, limits=DiscoveryLimits())
    assert len(calls) == count
    assert [block.units[0].id for block in plan] == [u.id for u in units]
    assert all(len(block.units) == 1 and block.oversize and "x" * 35000 in block.diff_text for block in plan)


@pytest.mark.asyncio
async def test_invalid_candidate_is_unresolved_and_not_clean():
    u = unit("a.py")
    bad = candidate(u)
    bad.pop("impact")
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [bad], "no_issue_units": [clean(u)]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert result.failed_blocks == 1 and result.dropped_invalid == 1
    assert u.id in ledger.unresolved_units
    assert not ledger.no_issue_units and not ledger.items


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["discrepancy", "trigger_basis", "behavior"])
async def test_unsupported_or_missing_discovery_basis_is_not_assessed(field):
    u = unit("a.py")
    bad = candidate(u)
    bad.pop(field)
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [bad], "no_issue_units": []})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger)
    assert not ledger.items and u.id in ledger.unresolved_units
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_valid_candidate_with_uncertainty_words_is_accepted_without_blacklist():
    u = unit("a.py")
    valid = candidate(u)
    valid["claim"] = "The change is correct for new records but may violate the persisted-value contract."
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [valid], "no_issue_units": []})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 1 and not ledger.unresolved_units
    assert llm.limits == [8192]
    stored = next(iter(ledger.items.values()))
    assert stored.discovery_basis == {key: valid[key] for key in ("behavior", "discrepancy", "trigger_basis")}
    assert not stored.observations


@pytest.mark.asyncio
async def test_generic_clean_and_invented_before_fact_do_not_count_as_checks():
    u = unit("a.py")
    bad_clean = clean(u)
    bad_clean["behavior"]["source_facts"][0]["excerpt"] = "return invented_value"
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [bad_clean]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger)
    assert u.id in ledger.unresolved_units and not ledger.no_issue_units


@pytest.mark.asyncio
async def test_resume_only_revisits_failed_units():
    first, second = unit("a.py"), unit("b.py")
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [clean(first)]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    state = StateStore(file_diffs={u.path: diff() for u in (first, second)})
    generator = HypothesisGenerator(llm)
    await generator.run(state, ContextPack(), changeset(first, second), ledger)
    llm.responses.append(json.dumps({"hypotheses": [], "no_issue_units": [clean(second)]}))
    await generator.run(state, ContextPack(), changeset(first, second), ledger, resume=True)
    required = llm.calls[-1][1].content.split("## Required assessments", 1)[1]
    assert second.id in required and first.id not in required
    assert set(ledger.no_issue_units) == {first.id, second.id} and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_block_and_candidate_caps_are_explicitly_deferred():
    first, second = unit("a.py"), unit("b.py")
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [candidate(first)], "no_issue_units": []})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(llm, discovery_max_units=1, discovery_max_blocks=1).run(
        StateStore(file_diffs={u.path: diff() for u in (first, second)}),
        ContextPack(),
        changeset(first, second),
        ledger,
    )
    assert result.blocks == 1
    assert second.id in ledger.unresolved_units and second.id not in ledger.no_issue_units
    assert any(row["status"] == "capacity-deferred" for row in ledger.discovery_blocks.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 2])
async def test_old_protocols_keep_original_candidate_and_clean_schema(version):
    u = unit("a.py")
    legacy = candidate(u)
    for field in ("behavior", "trigger_basis", "discrepancy"):
        legacy.pop(field)
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [legacy], "no_issue_units": []})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=version)
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 1 and not ledger.unresolved_units
    assert "Discovery protocol v3" not in llm.calls[0][0].content


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["no_issue", "hypothesis"])
async def test_nearby_function_cannot_supply_another_units_behavior_or_primary_site(kind):
    first, second = unit("same.py", "first"), unit("same.py", "second")
    second.start_line, second.end_line = 8, 10
    patch = diff() + "\n@@ -8,1 +8,1 @@\n-return other_old\n+return other_new"
    own = clean(first) if kind == "no_issue" else candidate(first)
    if kind == "no_issue":
        own["behavior"]["source_facts"] = [
            {"side": "before", "path": first.path, "line": 8, "excerpt": "return other_old"},
            {"side": "after", "path": first.path, "line": 8, "excerpt": "return other_new"},
        ]
    else:
        own["sites"] = [{"path": first.path, "line": 8, "excerpt": "return other_new"}]
    other = clean(second)
    other["behavior"]["source_facts"] = [
        {"side": "before", "path": first.path, "line": 8, "excerpt": "return other_old"},
        {"side": "after", "path": first.path, "line": 8, "excerpt": "return other_new"},
    ]
    body = {
        "hypotheses": [own] if kind == "hypothesis" else [],
        "no_issue_units": [other] if kind == "hypothesis" else [own, other],
    }
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(ScriptedLLM(responses=[json.dumps(body)])).run(
        StateStore(file_diffs={first.path: patch}), ContextPack(), changeset(first, second), ledger
    )
    assert not ledger.items and first.id in ledger.unresolved_units
    assert second.id in ledger.no_issue_units and result.failed_blocks == 1


@pytest.mark.asyncio
async def test_independent_risks_in_one_unit_are_kept_with_original_v2_identity_variants():
    u = unit("a.py")
    first, second = candidate(u), candidate(u)
    second.update(
        claim="The changed value breaks the archived record format.",
        trigger="An archived record is loaded.",
        impact="The record becomes unreadable.",
    )
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [first, second], "no_issue_units": []})])
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert result.accepted == 2 and len(ledger.items) == 2 and not ledger.unresolved_units
    assert len({h.trigger for h in ledger.items.values()}) == 2


@pytest.mark.asyncio
async def test_candidate_cap_is_shared_across_blocks_and_lenses():
    first, second = unit("a.py"), unit("b.py")
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    llm = ScriptedLLM(
        responses=[
            json.dumps({"hypotheses": [candidate(first)], "no_issue_units": []}),
            json.dumps({"hypotheses": [candidate(second)], "no_issue_units": []}),
        ]
    )
    result = await HypothesisGenerator(llm, max_hypotheses=1, discovery_max_units=1).run(
        StateStore(file_diffs={u.path: diff() for u in (first, second)}),
        ContextPack(),
        changeset(first, second),
        ledger,
    )
    assert len(ledger.items) == 1 and result.dropped_overflow == 1
    assert second.id in result.capacity_deferred_units and second.id not in ledger.no_issue_units
    assert "New candidate allowance this block: 0" in llm.calls[-1][1].content
    lens = ScriptedLLM(responses=[json.dumps({"hypotheses": [candidate(second)], "no_issue_units": []})])
    await HypothesisGenerator(lens, max_hypotheses=1, source="lens:security").run(
        StateStore(file_diffs={second.path: diff()}), ContextPack(), changeset(second), ledger
    )
    assert len(ledger.items) == 1 and "**0 new candidates**" in lens.calls[0][0].content


def test_context_fact_cannot_quote_omitted_line_or_navigation_reason():
    u = unit("a.py")
    slice_ = ContextSlice(
        "caller",
        "caller.py",
        5,
        6,
        "run",
        "supported_call(argument)\nhidden_call(argument)",
        "reason not source",
        "abc",
    )
    pack = ContextPack(units={u.id: UnitContext(u.id, [slice_])})
    header = f"### {u.id} caller caller.py:5-6\nNavigation reason (not evidence): reason not source\n"
    budget = len(header) + len("    5 | supported_call(argument)") + 3
    block = plan_discovery(changeset(u), pack, {u.path: diff()}, limits=DiscoveryLimits(context_max_chars=budget))[0]
    assert block.source_matches({"side": "context", "path": "caller.py", "line": 5, "excerpt": "supported_call"})
    assert not block.source_matches({"side": "context", "path": "caller.py", "line": 6, "excerpt": "hidden_call"})
    assert not block.source_matches({"side": "context", "path": "caller.py", "line": 5, "excerpt": "reason not source"})
    assert "only 1/2 lines delivered" in "\n".join(block.unchecked)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch,side,excerpt",
    [
        ("@@ -0,0 +1,1 @@\n+return new_value", "after", "return new_value"),
        ("@@ -1,1 +0,0 @@\n-return old_value", "before", "return old_value"),
    ],
)
async def test_new_or_deleted_file_does_not_need_a_quote_from_nonexistent_side(patch, side, excerpt):
    u = unit("a.py")
    u.start_line, u.end_line = 0, 0
    check = clean(u)
    check["behavior"]["source_facts"] = [fact(u.path, side, excerpt)]
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [check]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(StateStore(file_diffs={u.path: patch}), ContextPack(), changeset(u), ledger)
    assert u.id in ledger.no_issue_units and not ledger.unresolved_units


@pytest.mark.asyncio
async def test_incomplete_hunk_does_not_become_source_backed_clean():
    u = unit("a.py")
    llm = ScriptedLLM()
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(llm).run(
        StateStore(file_diffs={u.path: "@@ -1,2 +1,2 @@\n-return old_value\n+return new_value"}),
        ContextPack(),
        changeset(u),
        ledger,
    )
    assert not llm.calls and "incomplete-diff" in ledger.unresolved_units[u.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("unit_id", {}), ("severity", []), ("trigger_basis", {"side": []})])
async def test_malformed_schema_values_are_output_failures(field, value):
    u = unit("a.py")
    bad = candidate(u)
    bad[field] = value
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [bad], "no_issue_units": []})])
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert result.failed_blocks == 1 and u.id in ledger.unresolved_units and not ledger.items


@pytest.mark.asyncio
async def test_all_blocks_and_checks_survive_checkpoint_round_trip():
    u = unit("a.py")
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(
        ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [clean(u)]})])
    ).run(StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger)
    restored = HypothesisLedger.from_dict(json.loads(json.dumps(ledger.to_dict())))
    assert restored.discovery_blocks == ledger.discovery_blocks
    llm = ScriptedLLM()
    await HypothesisGenerator(llm).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), restored, resume=True
    )
    assert not llm.calls


@pytest.mark.asyncio
async def test_policy_deferred_does_not_expand_block_cap_on_resume():
    first, second = unit("a.py"), unit("b.py")
    state = StateStore(file_diffs={u.path: diff() for u in (first, second)})
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [clean(first)]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    generator = HypothesisGenerator(llm, discovery_max_units=1, discovery_max_blocks=1)
    await generator.run(state, ContextPack(), changeset(first, second), ledger)
    await generator.run(state, ContextPack(), changeset(first, second), ledger, resume=True)
    assert len(llm.calls) == 1 and second.id in ledger.unresolved_units and second.id not in ledger.no_issue_units


class BudgetDeniedLLM(ScriptedLLM):
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from reviewforge.engine.request_budget import RequestBudgetExceededError

        self.calls.append(messages)
        raise RequestBudgetExceededError("test request is outside the remaining budget")


@pytest.mark.asyncio
async def test_request_budget_denial_is_not_misclassified_as_provider_failure():
    u = unit("a.py")
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(BudgetDeniedLLM()).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert ledger.unresolved_units[u.id] == "generator budget-exhausted"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["oversized_fact", "too_many_facts", "oversized_prose"])
async def test_discovery_navigation_metadata_limits_are_enforced(change):
    u = unit("a.py")
    bad = candidate(u)
    patch = diff()
    if change == "oversized_fact":
        patch = diff(after="return new_value # " + "x" * 250)
        bad["behavior"]["source_facts"][1]["excerpt"] = "return new_value # " + "x" * 250
    elif change == "too_many_facts":
        bad["behavior"]["source_facts"] *= 3
    else:
        bad["discrepancy"] = "x" * 601
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(
        ScriptedLLM(responses=[json.dumps({"hypotheses": [bad], "no_issue_units": []})])
    ).run(StateStore(file_diffs={u.path: patch}), ContextPack(), changeset(u), ledger)
    assert result.failed_blocks == 1 and not ledger.items and u.id in ledger.unresolved_units


def test_new_java_method_does_not_inherit_the_previous_methods_drifting_closing_brace():
    # Shape of the first real v3 callback: unchanged old closing brace becomes
    # the last newly added method's closing brace in the post-image.
    u = unit("ParserTest.java", "verifyNewCase")
    u.start_line, u.end_line = 48, 52
    patch = "\n".join(
        [
            "@@ -29,8 +29,26 @@ class ParserTest {",
            " ",
            "     @Test",
            "     void verifyExistingCase() {",
            "-        verifyOld();",
            "-        assertOldResult();",
            "+        verifyCurrent();",
            "+        assertCurrentResult();",
            "+    }",
            "+",
            "+    @Test",
            "+    void verifyExtraCase() {",
            "+        verifyExtra();",
            "+        assertExtraResult();",
            "+    }",
            "+",
            "+    @Test",
            "+    void verifyAnotherCase() {",
            "+        verifyAnother();",
            "+        assertAnotherResult();",
            "+    }",
            "+",
            "+    @Test",
            "+    void verifyNewCase() {",
            "+        verifyNew();",
            "+        assertNewResult();",
            "     }",
            " ",
            "     private void helper() {",
        ]
    )
    block = plan_discovery(changeset(u), ContextPack(), {u.path: patch}, limits=DiscoveryLimits())[0]
    assert block.unit_before_states[u.id] == "absent-new-declaration"
    assert any(f.side == "before" and f.line == 34 and f.right_line == 52 for f in block.facts)
    assert not any(f.side == "before" for f in block.unit_facts[u.id])
    check = {
        "unit_id": u.id,
        "checked": "Checked the new assertion.",
        "behavior": {
            "before": "This test did not exist.",
            "after": "The new test checks the new result.",
            "observable": "A wrong new result fails the test.",
            "source_facts": [{"side": "after", "path": u.path, "line": 51, "excerpt": "assertNewResult();"}],
        },
    }
    assert HypothesisGenerator._valid_behavior(check, u, block)


@pytest.mark.parametrize(
    "patch",
    [
        "@@ -1,3 +1,3 @@\n-public void run() {\n+public void run(int value) {\n     process();\n }",
        "@@ -1,3 +1,3 @@\n-public void oldName() {\n+public void run() {\n     process();\n }",
    ],
)
def test_signature_replacement_and_rename_do_not_claim_absent_before(patch):
    u = unit("Service.java", "run")
    block = plan_discovery(changeset(u), ContextPack(), {u.path: patch}, limits=DiscoveryLimits())[0]
    assert not block.unit_before_states[u.id].startswith("absent-")
    assert any(f.side == "before" and f.changed for f in block.unit_facts[u.id])


def test_unknown_declaration_correspondence_is_not_guessed_new():
    u = unit("Service.java", "run")
    patch = "@@ -2,2 +2,3 @@\n     process();\n+    processExtra();\n }"
    block = plan_discovery(changeset(u), ContextPack(), {u.path: patch}, limits=DiscoveryLimits())[0]
    assert block.unit_before_states[u.id] == "unresolved-declaration-correspondence"
    assert any(f.side == "before" for f in block.unit_facts[u.id])


def test_zero_context_insertion_state_describes_delivered_source_only():
    from reviewforge.engine.prompts_v4 import load_prompt

    u = unit("Service.java", "run")
    u.start_line, u.end_line = 50, 50
    block = plan_discovery(
        changeset(u),
        ContextPack(),
        {u.path: "@@ -49,0 +50,1 @@\n+    processExtra();"},
        limits=DiscoveryLimits(),
    )[0]
    assert block.unit_before_states[u.id] == "absent-before-in-delivered-source"
    prompt = load_prompt("discovery_v3", lens="general correctness", output_language="English", max_hypotheses=6)
    assert "a zero-context insertion can have this state" in prompt
    assert "It does not mean\nthe base file did not exist" in prompt
    assert "including when no target before source was delivered" in prompt


def test_unchanged_source_prints_both_explicit_coordinates_and_scope_is_exact():
    u = unit("Service.java", "run")
    u.start_line, u.end_line = 49, 51
    patch = "@@ -41,3 +48,4 @@\n public void run() {\n     process();\n+    processExtra();\n }"
    block = plan_discovery(changeset(u), ContextPack(), {u.path: patch}, limits=DiscoveryLimits())[0]
    assert "before=   42 RIGHT=   49 |     process();" in block.diff_text
    assert block.source_matches({"side": "before", "path": u.path, "line": 42, "excerpt": "process();"})
    assert not block.source_matches({"side": "before", "path": u.path, "line": 49, "excerpt": "process();"})
    user = HypothesisGenerator(ScriptedLLM())._render_discovery_message(
        block, HypothesisLedger("run", "abc", "digest", protocol_version=3), 6
    )
    assert "RIGHT lines: 49-51" in user and "before lines: 42-43" in user
    assert "RIGHT lines: 48" not in user


@pytest.mark.asyncio
async def test_missing_hunk_is_isolated_from_units_with_complete_source():
    missing, first, second = unit("renamed.properties", ""), unit("a.py"), unit("b.py")
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [clean(first), clean(second)]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    result = await HypothesisGenerator(llm).run(
        StateStore(file_diffs={first.path: diff(), second.path: diff(), missing.path: ""}),
        ContextPack(),
        changeset(missing, first, second),
        ledger,
    )
    assert set(ledger.no_issue_units) == {first.id, second.id}
    assert ledger.unresolved_units == {missing.id: "generator missing-diff"}
    assert result.blocks == 1 and result.failed_blocks == 1
    assert any(row["unit_ids"] == [missing.id] for row in ledger.discovery_blocks.values())


@pytest.mark.asyncio
async def test_missing_inputs_do_not_consume_the_valid_discovery_call_slots():
    missing = [unit(f"renamed{i}.properties", "", risk=10) for i in range(10)]
    first, second = unit("a.py"), unit("b.py")
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [clean(first), clean(second)]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(llm, discovery_max_blocks=1).run(
        StateStore(file_diffs={first.path: diff(), second.path: diff()}),
        ContextPack(),
        changeset(*missing, first, second),
        ledger,
    )
    assert len(llm.calls) == 1 and set(ledger.no_issue_units) == {first.id, second.id}
    assert all(ledger.unresolved_units[u.id] == "generator missing-diff" for u in missing)


@pytest.mark.asyncio
async def test_responsibilities_are_fused_into_first_call_and_rules_delivered_once():
    from reviewforge.engine.lenses import build_discovery_responsibilities
    from reviewforge.engine.verification_guidance import localization_guidance

    u = unit("messages_en.properties", "")
    state = StateStore(file_diffs={u.path: diff()})
    responsibilities, rules = build_discovery_responsibilities(state, changeset(u))
    assert responsibilities == {u.id: ["localization"]}
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [clean(u)]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(llm, unit_responsibilities=responsibilities, responsibility_rules=rules).run(
        state, ContextPack(), changeset(u), ledger
    )
    assert len(llm.calls) == 1
    full_input = "\n".join(message.content for message in llm.calls[0])
    assert full_input.count(localization_guidance()) == 1
    assert "responsibilities: localization" in full_input
    row = next(iter(ledger.discovery_blocks.values()))
    assert row["delivered_responsibilities"] == {u.id: ["localization"]}
    assert ledger.no_issue_units[u.id]


@pytest.mark.asyncio
async def test_irrelevant_or_missing_responsibility_rules_are_not_silently_delivered():
    u = unit("a.py")
    llm = ScriptedLLM(responses=[json.dumps({"hypotheses": [], "no_issue_units": [clean(u)]})])
    ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(llm, responsibility_rules={"unused": "UNRELATED RULE"}).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), ledger
    )
    assert "UNRELATED RULE" not in llm.calls[0][0].content
    missing_llm = ScriptedLLM()
    missing_ledger = HypothesisLedger("run", "abc", "digest", protocol_version=3)
    await HypothesisGenerator(missing_llm, unit_responsibilities={u.id: ["missing"]}).run(
        StateStore(file_diffs={u.path: diff()}), ContextPack(), changeset(u), missing_ledger
    )
    assert not missing_llm.calls and missing_ledger.unresolved_units[u.id] == "generator missing responsibility rules"
    assert "delivered_responsibilities" not in next(iter(missing_ledger.discovery_blocks.values()))
