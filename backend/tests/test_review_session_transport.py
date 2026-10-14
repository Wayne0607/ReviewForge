import asyncio
import json
from dataclasses import asdict

import httpx
import pytest
from pydantic import Field
from test_discovery_submission import fixture
from test_investigation_v3 import candidate, source_executor, state
from test_review_session_v5 import SessionModel, call, clean, packet, receipt

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import HypothesisLedger
from reviewforge.engine.hypothesis_generator import HypothesisGenerator
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI
from reviewforge.engine.request_budget import BudgetedChatLLM, RequestBudget
from reviewforge.engine.wire_audit import WireAudit


@pytest.mark.asyncio
async def test_real_sdk_dispatches_one_native_contract_and_accounts_every_feedback_receipt(tmp_path):
    review_state, changes, _, _ = fixture(2)
    ledger = HypothesisLedger("sdk", "head", "digest", protocol_version=5)
    requests = []
    audit = WireAudit(str(tmp_path))

    def server(request):
        payload = json.loads(request.content)
        requests.append(payload)
        names = [tool["function"]["name"] for tool in payload["tools"]]
        assert {"submit_hypothesis", "submit_no_issue", "defer_unit", "read_file", "find_definition"} <= set(names)
        assert payload["tool_choice"] == "required" and "response_format" not in payload
        data = json.loads(payload["messages"][1]["content"])
        calls = [{"id": "wrong", "type": "function", "function": {"name": "write_file", "arguments": "{}"}}]
        if len(requests) > 1:
            assert "controller_receipt" in payload["messages"][-1]["content"]
            calls = [
                {
                    "id": f"unit-{index}",
                    "type": "function",
                    "function": {
                        "name": "submit_no_issue",
                        "arguments": json.dumps(clean(unit)),
                    },
                }
                for index, unit in enumerate(data["pending_units"])
            ]
        return httpx.Response(
            200,
            json={
                "id": "offline-receipt",
                "object": "chat.completion",
                "created": 0,
                "model": "offline",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {"role": "assistant", "content": None, "tool_calls": calls},
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
            },
        )

    transport = httpx.MockTransport(server)
    with httpx.Client(
        transport=transport, event_hooks={"request": [audit.request], "response": [audit.response]}
    ) as sync:
        async with httpx.AsyncClient(
            transport=transport,
            event_hooks={
                "request": [audit.async_request],
                "response": [audit.async_response],
            },
        ) as asynchronous:
            model = ReasoningContentChatOpenAI(
                model="offline",
                api_key="never-log-this-secret",
                max_retries=0,
                base_url="https://example.invalid/v1",
                http_client=sync,
                http_async_client=asynchronous,
            )
            budget = RequestBudget(250_000, discovery_limit=80_000, editor_reserve=0)
            result = await HypothesisGenerator(
                BudgetedChatLLM(model, budget, phase="discovery"),
                executor=source_executor([]),
            ).run(review_state, ContextPack(), changes, ledger)
    snapshot = budget.snapshot()
    assert len(requests) == result.blocks == snapshot["request_count"] == 2
    assert (
        snapshot["reported_tokens"] == 240 and not snapshot["pending"] and snapshot["unreported_reserved_tokens"] == 0
    )
    assert len(ledger.no_issue_units) == 2 and not ledger.unresolved_units
    assert len(list(tmp_path.glob("*-request.json"))) == len(list(tmp_path.glob("*-response.json"))) == 2
    assert "never-log-this-secret" not in "".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.json"))


@pytest.mark.asyncio
async def test_shared_budget_rejects_before_dispatch_without_erasing_task_state():
    review_state, changes, _, _ = fixture(1)
    model = SessionModel(handler=lambda *args: receipt())
    budget = RequestBudget(100, editor_reserve=0)
    ledger = HypothesisLedger("budget", "head", "digest", protocol_version=5)
    await HypothesisGenerator(BudgetedChatLLM(model, budget, phase="discovery"), executor=source_executor([])).run(
        review_state,
        ContextPack(),
        changes,
        ledger,
    )
    assert not model.calls and not ledger.no_issue_units and ledger.unresolved_units
    snapshot = budget.snapshot()
    assert snapshot["reported_tokens"] == snapshot["request_count"] == 0 and not snapshot["pending"]
    record = next(iter(ledger.discovery_blocks.values()))
    assert record["session"]["turns"] == 1


@pytest.mark.asyncio
async def test_actual_evidence_is_checkpointed_before_an_interrupted_next_turn():
    saved = []

    async def checkpoint(observations, progress):
        saved.append({"observations": [asdict(obs) for obs in observations], "progress": list(progress)})

    def answer(messages, options, turn):
        if turn == 1:
            return receipt(call("read_file", {"path": "caller.ts", "start": 1, "end": 3}))
        raise asyncio.CancelledError()

    class AsyncInterruptModel(SessionModel):
        waiting: asyncio.Event = Field(default_factory=asyncio.Event)

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if self.calls:
                self.waiting.set()
                await asyncio.Event().wait()
            return self._generate(messages, stop, run_manager, **kwargs)

    model = AsyncInterruptModel(handler=answer)
    worker = Investigator(model, source_executor([]), protocol_version=5, on_progress=checkpoint)
    task = asyncio.create_task(worker.investigate(candidate(), state(), ContextPack()))
    await asyncio.wait_for(model.waiting.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(saved[-1]["observations"]) == 3
    assert saved[-1]["observations"][-1]["path"] == "caller.ts"
    assert any(event.get("event") == "request-plan" for event in saved[-1]["progress"])


@pytest.mark.asyncio
async def test_v5_routing_and_persisted_evidence_use_the_actual_pipeline_not_an_isolated_consumer():
    from types import SimpleNamespace

    from reviewforge.core.config import PipelineV4Config
    from reviewforge.core.events import EventBus
    from reviewforge.engine.pipeline_v4 import _run_llm_stages

    review_state, changes, _, _ = fixture(2)
    review_state.head_sha = "head"
    ledger = HypothesisLedger("integrated", "head", "digest", protocol_version=5)

    def answer(messages, options, turn):
        assert ledger.request_budget["allocation"]["future_candidate"] == 1
        return receipt(
            *[
                call("submit_no_issue", clean(unit), f"unit-{index}")
                for index, unit in enumerate(packet(messages)["pending_units"])
            ]
        )

    model = SessionModel(handler=answer)
    router = SimpleNamespace(get_llm=lambda *args: model, effective=lambda *args: {"model": "offline"})
    runtime = SimpleNamespace(_model_router=router, _events=EventBus(), _db=None)
    publication = await _run_llm_stages(
        runtime,
        review_state,
        changes,
        ContextPack(),
        ledger,
        SimpleNamespace(),
        PipelineV4Config(protocol_version=5),
        "en",
        {},
    )
    assert not publication.comments and len(ledger.no_issue_units) == 2 and not ledger.unresolved_units
    assert ledger.evidence_records and ledger.request_budget["reported_tokens"] == 120
    assert len(model.calls) == 1
    assert ledger.request_budget["allocation"]["future_candidate"] == 0
