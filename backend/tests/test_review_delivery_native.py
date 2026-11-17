import pytest
from langchain_core.messages import AIMessage, HumanMessage, messages_to_dict

from reviewforge.core.config import LLMConfig
from reviewforge.engine.model_router import ModelRouter
from reviewforge.engine.openai_compat import V42StreamingChatOpenAI
from reviewforge.review_delivery.providers import conversation_protocols, raw_models
from reviewforge.review_delivery.runtime import DeliveryRuntime
from reviewforge.review_v42.integration import _raw_models as historical_models
from tests.test_review_delivery_context import Gateway, Script, call, finish, store_at, workspace

NATIVE = {"lead": {"native_history": True}}


def continued(message, state):
    message.additional_kwargs["reasoning_content"] = state
    return message


def states(messages):
    return [m.additional_kwargs["reasoning_content"] for m in messages if isinstance(m, AIMessage)]


async def test_native_state_survives_multiple_tool_rounds_without_entering_workspace_or_other_sessions(tmp_path):
    store = store_at(tmp_path)

    def compare(messages):
        assert states(messages) == ["opaque-A", "opaque-B"]
        assert "opaque-A" not in messages[2].content
        text = "\n".join(str(m.content) for m in messages)
        assert text.count("UNIQUE ORIGINAL base.py") == text.count("UNIQUE ORIGINAL head.py") == 1
        return finish()

    lead = Script(
        [
            continued(call("read_source", {"path": "base.py"}), "opaque-A"),
            continued(call("read_source", {"path": "head.py"}), "opaque-B"),
            compare,
        ]
    )
    runtime = DeliveryRuntime(store, Gateway(), lead, Script([]), conversation_protocols=NATIVE)
    assert (await runtime.run("Compare"))["status"] == "complete"
    assert store.list_notes() == []
    other = store.create_session("lead")

    def separate(messages):
        assert states(messages) == [] and workspace(messages)["retained_sources"] == []
        return finish()

    runtime.models["lead"] = Script([separate])
    assert (await runtime._session(other["id"], "lead", "Separate question"))["status"] == "complete"


async def test_native_requests_extend_actual_messages_and_wire_prefix_while_workspace_changes(tmp_path):
    store = store_at(tmp_path)
    router = ModelRouter(
        LLMConfig(
            base_url="https://opencode.ai/inference/go/openai/v1", api_key="test-only", model="deepseek-v4.1-flash"
        )
    )
    adapter = raw_models(router, request_timeout=900)["v42_reviewer"]
    snapshots, wire = [], []

    class Appending(Script):
        async def ainvoke(self, messages):
            snapshot = messages_to_dict(messages)
            payload = adapter._get_request_payload(messages)["messages"]
            if snapshots:
                assert snapshot[: len(snapshots[-1])] == snapshots[-1]
                assert payload[: len(wire[-1])] == wire[-1]
            snapshots.append(snapshot)
            wire.append(payload)
            return await super().ainvoke(messages)

    def compare(messages):
        text = "\n".join(str(m.content) for m in messages)
        assert text.count("UNIQUE ORIGINAL base.py") == text.count("UNIQUE ORIGINAL head.py") == 1
        assert "content_in_session_history" in text
        assert workspace(messages)["progress"] == {"focus": "compare units", "next_steps": "conclude"}
        assert len(workspace(messages)["retained_sources"]) == 2
        return finish()

    lead = Appending(
        [
            continued(call("read_source", {"path": "base.py"}), "opaque-A"),
            continued(call("set_progress", {"focus": "compare units", "next_steps": "conclude"}), "opaque-focus"),
            continued(call("read_source", {"path": "head.py"}), "opaque-B"),
            continued(call("read_source", {"path": "base.py"}), "opaque-reread"),
            compare,
        ]
    )
    assert (await DeliveryRuntime(store, Gateway(), lead, Script([]), conversation_protocols=NATIVE).run("Compare"))[
        "status"
    ] == "complete"
    session = store.list_sessions()[0]["id"]
    assert len(store.delivered_sources(session)) == 2
    requests = [turn["payload"] for turn in store.list_turns(session) if turn["payload"]["type"] == "request"]
    assert all("context_state" in request for request in requests)
    # The earlier focus update remains at its actual chronological position.
    assert sum("compare units" in message["data"].get("content", "") for message in snapshots[-1]) == 1


async def test_native_pending_request_replays_exactly_after_restart(tmp_path):
    store = store_at(tmp_path)
    pending = []

    def fail(messages):
        pending.append(messages_to_dict(messages))
        assert states(messages) == ["opaque-A", "opaque-B"]
        raise RuntimeError("response unavailable")

    lead = Script(
        [
            continued(call("read_source", {"path": "base.py"}), "opaque-A"),
            continued(call("read_source", {"path": "head.py"}), "opaque-B"),
            fail,
        ]
    )
    with pytest.raises(RuntimeError, match="response unavailable"):
        await DeliveryRuntime(store, Gateway(), lead, Script([]), conversation_protocols=NATIVE).run("Compare")
    session = store.list_sessions()[0]["id"]
    assert len(store.delivered_sources(session)) == 1
    store.close()
    store = store_at(tmp_path)

    def replay(messages):
        assert messages_to_dict(messages) == pending[0]
        return finish()

    assert (
        await DeliveryRuntime(store, Gateway(), Script([replay]), Script([]), conversation_protocols=NATIVE).run(
            "Compare"
        )
    )["status"] == "complete"
    assert len(store.delivered_sources(session)) == 2


async def test_native_finding_verdict_cycle_keeps_bodies_and_provider_state_independent(tmp_path):
    store = store_at(tmp_path)
    requests = {"lead": [], "verifier": []}

    class Recording(Script):
        def __init__(self, role, steps):
            super().__init__(steps)
            self.role = role

        async def ainvoke(self, messages):
            snapshot = messages_to_dict(messages)
            if requests[self.role]:
                assert snapshot[: len(requests[self.role][-1])] == requests[self.role][-1]
            requests[self.role].append(snapshot)
            return await super().ainvoke(messages)

    def author(messages):
        assert "verifier-private" not in str(messages_to_dict(messages))
        return continued(
            call(
                "submit_finding",
                {
                    "claim": {"title": "Regression", "body": "Unsafe path introduced"},
                    "source_ids": [store.list_sources()[0]["id"]],
                },
            ),
            "lead-private",
        )

    def challenge(messages):
        assert "lead-private" not in str(messages_to_dict(messages))
        assert workspace(messages)["retained_sources"] == []
        return continued(call("read_saved_source", {"source_id": store.list_sources()[0]["id"]}), "verifier-private")

    def decide(messages):
        assert "lead-private" not in str(messages_to_dict(messages))
        assert "UNIQUE ORIGINAL head.py" in "\n".join(str(m.content) for m in messages)
        return continued(
            call(
                "submit_verdict",
                {
                    "verdict": "accept",
                    "source_ids": [store.list_sources()[0]["id"]],
                    "reason": "Independent refutation checked",
                },
            ),
            "verifier-private",
        )

    def complete(messages):
        assert "verifier-private" not in str(messages_to_dict(messages))
        assert store.list_findings()[0]["state"] == "accept"
        return finish()

    lead = Recording("lead", [continued(call("read_source", {"path": "head.py"}), "lead-private"), author, complete])
    verifier = Recording("verifier", [challenge, decide])
    runtime = DeliveryRuntime(
        store,
        Gateway(),
        lead,
        verifier,
        conversation_protocols={"lead": {"native_history": True}, "verifier": {"native_history": True}},
    )
    assert (await runtime.run("Review"))["status"] == "complete"
    assert len(store.list_verdicts()) == len(store.list_outbox()) == 1
    before = lead.calls, verifier.calls
    await runtime.run("Review")
    assert (lead.calls, verifier.calls) == before


async def test_native_release_removes_historical_original_bodies_without_dropping_protocol_state(tmp_path):
    store = store_at(tmp_path)

    def release(messages):
        return continued(call("release_source", {"source_id": store.list_sources()[0]["id"]}), "opaque-release")

    def check(messages):
        assert states(messages) == ["opaque-A", "opaque-B", "opaque-release"]
        text = "\n".join(str(m.content) for m in messages)
        assert "UNIQUE ORIGINAL base.py" not in text and "source_content_released" in text
        assert text.count("UNIQUE ORIGINAL head.py") == 1
        return finish()

    lead = Script(
        [
            continued(call("read_source", {"path": "base.py"}), "opaque-A"),
            continued(call("read_source", {"path": "head.py"}), "opaque-B"),
            release,
            check,
        ]
    )
    assert (await DeliveryRuntime(store, Gateway(), lead, Script([]), conversation_protocols=NATIVE).run("Compare"))[
        "status"
    ] == "complete"


async def test_go_headers_are_stable_per_persistent_session_and_different_between_sessions(tmp_path):
    store = store_at(tmp_path)
    headers = []

    class WithHeaders(Script):
        async def ainvoke(self, messages, **kwargs):
            headers.append(kwargs["extra_headers"])
            return await super().ainvoke(messages)

    model = WithHeaders([call("read_source", {"path": "base.py"}), finish(), finish()])
    runtime = DeliveryRuntime(
        store, Gateway(), model, Script([]), conversation_protocols={"lead": {"provider": "opencode-go"}}
    )
    await runtime.run("Review")
    other = store.create_session("lead")
    await runtime._session(other["id"], "lead", "Separate review")
    assert headers[0] == headers[1]
    assert headers[0]["x-opencode-session"] != headers[2]["x-opencode-session"]
    assert all(h["User-Agent"] == "ReviewForge/0.2.0" for h in headers)


def test_new_go_deepseek_route_preserves_native_field_without_changing_historical_streaming_adapter():
    router = ModelRouter(
        LLMConfig(
            base_url="https://opencode.ai/inference/go/openai/v1", api_key="test-only", model="deepseek-v4.1-flash"
        )
    )
    native = raw_models(router, request_timeout=900)["v42_verifier"]
    old = historical_models(router, request_timeout=900)["v42_verifier"]
    assert not isinstance(native, V42StreamingChatOpenAI) and isinstance(old, V42StreamingChatOpenAI)
    message = continued(call("read_source", {"path": "base.py"}), "opaque-own-provider-state")
    native_payload = native._get_request_payload([HumanMessage(content="Review"), message])
    historical_payload = old._get_request_payload([HumanMessage(content="Review"), message])
    assert native_payload["messages"][-1]["reasoning_content"] == "opaque-own-provider-state"
    assert "reasoning_content" not in historical_payload["messages"][-1]
    assert all(p["native_history"] for p in conversation_protocols(router).values())


@pytest.mark.parametrize(
    "base,model",
    [
        ("https://opencode.ai.example/inference/go/openai/v1", "deepseek-v4.1-flash"),
        ("https://opencode.ai/inference/go/openai/v1", "hy4-flash-free"),
    ],
)
def test_other_hosts_and_models_do_not_inherit_deepseek_native_history(base, model):
    policies = conversation_protocols(ModelRouter(LLMConfig(base_url=base, api_key="test-only", model=model)))
    assert not any(p["native_history"] for p in policies.values())


async def test_newly_read_and_immediately_released_original_is_delivered_before_it_can_be_referenced(tmp_path):
    store = store_at(tmp_path)
    saved = store.put_source("original", "head", b"FIRST DELIVERY REQUIRED", {"path": "source.py", "revision": "head"})
    request = AIMessage(
        content="",
        tool_calls=[
            {"name": "read_saved_source", "args": {"source_id": saved["id"]}, "id": "read"},
            {"name": "release_source", "args": {"source_id": saved["id"]}, "id": "release"},
        ],
    )

    def confirm(messages):
        assert workspace(messages)["retained_sources"] == []
        assert "FIRST DELIVERY REQUIRED" in "\n".join(str(m.content) for m in messages)
        return finish()

    assert (
        await DeliveryRuntime(
            store, Gateway(), Script([request, confirm]), Script([]), conversation_protocols=NATIVE
        ).run("Review")
    )["status"] == "complete"
    assert saved["id"] in store.delivered_sources(store.list_sessions()[0]["id"])


async def test_native_history_keeps_navigation_without_promoting_it_to_original_evidence(tmp_path):
    store = store_at(tmp_path)

    class Navigation(Gateway):
        async def __call__(self, name, args):
            value = await super().__call__(name, args)
            if args["path"] == "query":
                return {
                    **value,
                    "kind": "query",
                    "content": "UNIQUE NAVIGATION",
                    "metadata": {"status": "available", "navigation_only": True},
                }
            return value

    def new_result(messages):
        assert "UNIQUE NAVIGATION" in "\n".join(str(m.content) for m in messages)
        return call("read_source", {"path": "head.py"})

    def old_result(messages):
        text = "\n".join(str(m.content) for m in messages)
        assert text.count("UNIQUE NAVIGATION") == 1
        assert text.count("UNIQUE ORIGINAL head.py") == 1
        assert len(workspace(messages)["retained_sources"]) == 1
        return finish()

    lead = Script([call("read_source", {"path": "query"}), new_result, old_result])
    assert (await DeliveryRuntime(store, Navigation(), lead, Script([]), conversation_protocols=NATIVE).run("Review"))[
        "status"
    ] == "complete"
