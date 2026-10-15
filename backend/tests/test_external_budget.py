"""A small grant must protect downstream work before admitting discovery."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage
from test_request_budget import _Model

from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    RequestBudgetExceededError,
    budget_snapshot_over_limit,
    external_request_budget_scope,
    investigation_request_scope,
    plan_request,
)


async def saved(_snapshot):
    pass


def test_intersection_precedes_stage_protection_and_does_not_double_subtract_task_budget():
    grant = RequestBudget(93_955, editor_reserve=0)
    product = RequestBudget(250_000, adaptive=True)
    with external_request_budget_scope(grant, identity="grant", on_change=saved):
        model = BudgetedChatLLM(_Model(), product, phase="discovery")
        product.allocate(verification=12_000, investigation=20_000, basis={})
        plan = plan_request(model, [HumanMessage(content="inspect")], fallback_available=250_000)
        assert plan.available_tokens == 61_955  # Old min(218000, 93955) spent the reserve.
        with investigation_request_scope(10_000):
            assert model.request_budget_view([], {}).available_tokens == 10_000
    assert product.limit == 250_000 and grant.limit == 93_955


@pytest.mark.asyncio
async def test_all_physical_retries_and_unknowns_charge_both_envelopes_and_restore():
    raw = _Model(fail=True)
    product, grant = RequestBudget(250_000, adaptive=True), RequestBudget(40_000, editor_reserve=0)
    snapshots = []

    async def persist(value):
        snapshots.append(copy.deepcopy(value))

    with external_request_budget_scope(grant, identity="grant", on_change=persist):
        model = RetryChatLLM(
            BudgetedChatLLM(raw, product, phase="discovery"), RetryPolicy(max_attempts=3, base_delay_seconds=0)
        )
        with pytest.raises(TimeoutError):
            await model.ainvoke([HumanMessage(content="inspect")], max_tokens=1000)
    assert len(raw.calls) == grant.serial == product.serial == 3
    assert grant.unreported == product.unreported > 0 and not grant.pending and not product.pending
    assert sum(bool(value["pending"]) for value in snapshots) == 3
    restored = RequestBudget(250_000, saved=product.snapshot())
    assert restored.available("editor") == 0
    with pytest.raises(ValueError, match="scope"):
        BudgetedChatLLM(raw, restored, phase="discovery")
    restored_grant = RequestBudget(40_000, editor_reserve=0, saved=grant.snapshot())
    with external_request_budget_scope(restored_grant, identity="grant", on_change=saved):
        BudgetedChatLLM(raw, restored, phase="discovery")
        assert restored.available("editor") == 40_000 - grant.unreported
    assert restored.unreported == restored_grant.unreported == grant.unreported


@pytest.mark.asyncio
async def test_external_pre_dispatch_checkpoint_failure_releases_every_reservation_without_http():
    raw, product, grant = _Model(), RequestBudget(250_000), RequestBudget(40_000, editor_reserve=0)

    async def failed(_snapshot):
        raise OSError("disk unavailable")

    with external_request_budget_scope(grant, identity="grant", on_change=failed):
        model = BudgetedChatLLM(raw, product, phase="investigation")
        with investigation_request_scope(10_000) as task:
            with pytest.raises(OSError):
                await model.ainvoke([HumanMessage(content="inspect")], max_tokens=1000)
            assert not task.pending and not task.unreported
    assert not raw.calls and not grant.pending and not product.pending
    assert grant.unreported == product.unreported == grant.reported == 0


@pytest.mark.asyncio
async def test_tighter_grant_stops_concurrent_requests_before_http_and_keeps_reserved_future():
    raw, product, grant = _Model(), RequestBudget(250_000, adaptive=True), RequestBudget(10_000, editor_reserve=0)
    with external_request_budget_scope(grant, identity="grant", on_change=saved):
        model = BudgetedChatLLM(raw, product, phase="discovery")
        product.allocate(verification=2000, investigation=3000, basis={})
        results = await asyncio.gather(
            *[model.ainvoke([HumanMessage(content="inspect")], max_tokens=3000) for _ in range(2)],
            return_exceptions=True,
        )
    assert sum(isinstance(value, RequestBudgetExceededError) for value in results) == 1
    assert len(raw.calls) == 1 and product.reported == grant.reported == 12
    assert not product.pending and not grant.pending


@pytest.mark.asyncio
async def test_source_grant_overrun_is_never_clamped_or_ignored():
    grant, product = RequestBudget(30_000, editor_reserve=0), RequestBudget(250_000, adaptive=True)
    with external_request_budget_scope(grant, identity="grant", on_change=saved):
        BudgetedChatLLM(_Model(), product, phase="editor")
        identity = grant.reserve("editor", 1000)
        grant.settle(identity, 30_001)
        assert product.over_limit and grant.over_limit and product.available("editor") == 0
        assert budget_snapshot_over_limit(product.snapshot())
        from reviewforge.engine.hypothesis import HypothesisLedger
        from reviewforge.engine.proof_gate import _over_budget

        ledger = HypothesisLedger("run", "head", "digest", protocol_version=7)
        ledger.request_budget = product.snapshot()
        assert _over_budget(ledger)


@pytest.mark.asyncio
async def test_closed_authorization_scope_cannot_dispatch_using_a_captured_model():
    raw, product, grant = _Model(), RequestBudget(250_000), RequestBudget(40_000, editor_reserve=0)
    with external_request_budget_scope(grant, identity="grant", on_change=saved):
        model = BudgetedChatLLM(raw, product, phase="editor")
    with pytest.raises(RequestBudgetExceededError):
        await model.ainvoke([])
    assert not raw.calls and not product.pending and not grant.pending


@pytest.mark.asyncio
async def test_downstream_reserve_can_fund_actual_blind_audit_and_proof_gate():
    from test_proof_gate import GateLLM, candidate, ledger
    from test_source_audit import audit, verdict

    from reviewforge.engine.budget_allocation import verification_forecast
    from reviewforge.engine.proof_gate import ProofGate, accepted

    item = candidate()
    book = ledger(item, protocol=7)
    raw = GateLLM(replies=[{"audits": [audit()]}, {"reviews": [verdict()]}])
    product, grant = RequestBudget(250_000, adaptive=True), RequestBudget(40_000, editor_reserve=0)

    async def checkpoint(snapshot):
        book.request_budget = snapshot

    with external_request_budget_scope(grant, identity="grant", on_change=saved):
        model = BudgetedChatLLM(raw, product, phase="editor", on_change=checkpoint)
        forecast = verification_forecast(model, book, [item], language="en")
        product.allocate(verification=forecast["verification_tokens"], investigation=0, basis=forecast)
        # Spend every allowed discovery token: the protected remainder must
        # still admit the real two independent publication requests.
        amount = product.available("discovery")
        product.settle(product.reserve("discovery", amount), amount)
        grant.settle(grant.reserve("editor", amount), amount)
        await checkpoint(product.snapshot())
        await ProofGate(model).run(book)
    assert len(raw.calls) == 2 and accepted(item, book)
    assert product.reported == grant.reported < grant.limit and not product.pending


@pytest.mark.asyncio
async def test_durable_benchmark_scope_is_exclusive_and_cannot_reset_or_reassign_grant(tmp_path, monkeypatch):
    # The runner's support modules are standalone scripts, not a package.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/benchmarks"))
    from authorized_budget import authorized_budget

    path = tmp_path / "grant.json"
    with authorized_budget(path, limit=30_000, identity="approved") as grant:
        with pytest.raises(FileExistsError):
            with authorized_budget(path, limit=30_000, identity="approved"):
                pass
        await BudgetedChatLLM(_Model(), RequestBudget(250_000), phase="editor").ainvoke([])
        assert grant.reported == 12
    assert json.loads(path.read_text())["budget"]["reported_tokens"] == 12
    with authorized_budget(path, limit=30_000, identity="approved") as restored:
        assert restored.reported == 12
    for limit, identity in [(40_000, "approved"), (30_000, "new")]:
        with pytest.raises(ValueError, match="changed"):
            with authorized_budget(path, limit=limit, identity=identity):
                pass
    assert not path.with_suffix(".json.lock").exists()
