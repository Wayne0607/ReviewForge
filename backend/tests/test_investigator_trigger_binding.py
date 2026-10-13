"""V4 declares the exact original case; this is not a semantic truth oracle."""

import copy
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import AIMessage
from test_investigator_typed_closure import Model, candidate, close, submission, tool_response, wire_submission, worker

from reviewforge.engine.context_pack import ContextPack
from reviewforge.engine.hypothesis import Hypothesis, Observation
from reviewforge.engine.investigator import _evidence_segments
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI
from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    estimate_request,
    investigation_request_scope,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["native", "text"])
@pytest.mark.parametrize("declared", [None, "", "Admin does not call check", "Owner calls check", "Admin calls check "])
async def test_missing_or_changed_trigger_cannot_switch_the_assessed_case(wire, declared):
    payload = wire_submission() if wire == "native" else submission()
    if declared is None:
        payload.pop("assessed_trigger", None)
    else:
        payload["assessed_trigger"] = declared
    response = tool_response(payload) if wire == "native" else AIMessage(content=json.dumps(payload))
    original = response.model_copy(deep=True)
    value = worker(Model(reply=response))
    observations = copy.deepcopy(value._observations)
    result = await close(value)
    assert result.verdict == "unknown" and result.outcome == "output-error"
    assert response == original and value._observations == observations


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["native", "text"])
@pytest.mark.parametrize(
    "comparison,verdict", [("conflict", "confirmed"), ("compatible", "refuted"), ("unresolved", "unknown")]
)
async def test_bound_case_keeps_existing_proof_verdicts(wire, comparison, verdict):
    payload = submission(comparison) | {"assessed_trigger": candidate().trigger}
    response = tool_response(wire_submission(payload)) if wire == "native" else AIMessage(content=json.dumps(payload))
    value = worker(Model(reply=response))
    result = await close(value)
    assert result.verdict == verdict
    binding = next(p for p in result.progress if p.get("phase") == "closing-trigger-binding")
    assert binding["trigger_sha256"] == hashlib.sha256(candidate().trigger.encode()).hexdigest()
    assert binding["source"] == "candidate.trigger" and binding["schema_version"] == "trigger-bound-flat-v1"
    assert len(binding["schema_sha256"]) == 64
    assert any(p.get("phase") == "closing-trigger-check" and p["matched"] for p in result.progress)


@pytest.mark.asyncio
async def test_positive_counterevidence_refutes_original_trigger_without_rewriting_it():
    raw = submission() | {
        "assessed_trigger": candidate().trigger,
        "assessment": None,
        "counterevidence": {
            "premise": "trigger",
            "statement": "A caller guard prevents this original case",
            "evidence": ["obs_0:e1"],
        },
    }
    result = await close(worker(Model(reply=tool_response(wire_submission(raw)))))
    assert result.verdict == "refuted" and result.counterevidence.premise == "trigger"


@pytest.mark.asyncio
@pytest.mark.parametrize("premise", ["mechanism", "impact"])
@pytest.mark.parametrize("saved_source", [True, False])
async def test_same_case_bad_outcome_counterevidence_requires_saved_positive_source(premise, saved_source):
    # A hand-authored proof tests the existing protocol, not model reasoning.
    # Original source and trigger stay unchanged; this does not amend the run.
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/investigator_unbound_trigger.json").read_text(encoding="utf-8")
    )
    value = worker(Model())
    missing = value._observations[-1]
    value._observations = [Observation(**row) for row in fixture["observations"]] + [missing]
    value._state.head_sha = fixture["observations"][0]["sha"]
    hypothesis = Hypothesis.from_dict(fixture["candidate"] | {"source": "test-replay"})
    source = value._observations[1]
    reference = next(
        identity
        for identity, citation in _evidence_segments(source).items()
        if "if (!isTeamAdminOrOwner" in citation.quote
    )
    raw = submission() | {
        "assessed_trigger": hypothesis.trigger,
        "assessment": None,
        "answer": "The original organizer or attendee case passes this guard.",
        "reason": "The claimed permission denial does not occur in the original case.",
        "counterevidence": {
            "premise": premise,
            "statement": "An organizer or attendee makes the shown FORBIDDEN condition false.",
            "evidence": [reference if saved_source else "obs_2:e1"],
        },
    }
    value._llm.reply = tool_response(wire_submission(raw))
    result = await value._close(
        value._closing_chat([], 24000, hypothesis=hypothesis),
        hypothesis,
        {source.path},
        token_limit=24000,
        steps=1,
    )
    if saved_source:
        assert result.verdict == "refuted" and result.counterevidence.premise == premise
        assert result.counterevidence.evidence[0].quote in source.excerpt
    else:
        assert result.verdict == "unknown" and result.outcome == "output-error"
    assert hypothesis.trigger == fixture["candidate"]["trigger"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "framing",
    ["prefix", "suffix", "two-objects", "fence", "duplicate-trigger", "nested-duplicate", "incomplete", "nonfinite"],
)
async def test_text_fallback_cannot_bypass_strict_json_framing_or_duplicate_checks(framing):
    text = json.dumps(submission() | {"assessed_trigger": candidate().trigger})
    if framing == "prefix":
        text = "Conclusion: " + text
    elif framing == "suffix":
        text += " trailing prose"
    elif framing == "two-objects":
        text += text
    elif framing == "fence":
        text = "```json\n" + text + "\n```"
    elif framing == "duplicate-trigger":
        text = text[:-1] + ',"assessed_trigger":"Admin calls check"}'
    elif framing == "nested-duplicate":
        text = text.replace('"comparison": "conflict"', '"comparison":"compatible","comparison":"conflict"')
    elif framing == "incomplete":
        text = text[:-1]
    else:
        text = text[:-1] + ',"extra":NaN}'
    result = await close(worker(Model(reply=AIMessage(content=text))))
    assert result.verdict == "unknown" and result.outcome == "output-error"


@pytest.mark.asyncio
async def test_binding_is_from_delivered_packet_and_never_inferred_from_response():
    value = worker(Model(reply=tool_response()))
    chat = value._closing_chat([], 24000, hypothesis=candidate())
    packet = json.loads(chat[-1].content)
    packet["candidate"]["trigger"] = "The exact packet case differs"
    chat[-1] = chat[-1].model_copy(update={"content": json.dumps(packet)})
    result = await value._close(chat, candidate(), {"check.ts"}, token_limit=24000, steps=3)
    assert result.verdict == "unknown" and result.outcome == "output-error"


@pytest.mark.asyncio
async def test_actual_unbound_relay_response_is_not_repaired_and_cannot_confirm():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/investigator_unbound_trigger.json").read_text(encoding="utf-8")
    )
    original_raw = copy.deepcopy(fixture["raw_tool_calls"])
    raw = {
        "id": "offline",
        "model": "fixture",
        "object": "chat.completion",
        "created": 1,
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls",
                "message": {"role": "assistant", "content": "", "tool_calls": original_raw},
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
    }
    captured = []

    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=raw)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        llm = ReasoningContentChatOpenAI(
            api_key="offline-only",
            base_url="https://example.invalid/v1",
            model="fixture",
            max_retries=0,
            http_async_client=client,
        )
        value = worker(llm)
        value._observations = [Observation(**row) for row in fixture["observations"]]
        hypothesis = candidate()
        hypothesis.trigger = fixture["candidate"]["trigger"]
        result = await value._close(
            value._closing_chat([], 24000, hypothesis=hypothesis), hypothesis, {"check.ts"}, token_limit=24000, steps=1
        )
    assert result.verdict == "unknown" and result.outcome == "output-error"
    assert fixture["raw_tool_calls"] == original_raw == raw["choices"][0]["message"]["tool_calls"]
    assert "assessed_trigger" not in json.loads(original_raw[0]["function"]["arguments"])
    assert len(captured) == 1 and captured[0]["tool_choice"] == "required"
    schema = captured[0]["tools"][0]["function"]["parameters"]
    assert "assessed_trigger" in schema["required"] and schema["properties"]["assessed_trigger"]["type"] == "string"


@pytest.mark.asyncio
@pytest.mark.parametrize("wire", ["native", "text"])
@pytest.mark.parametrize(
    "comparison,declared,verdict,outcome",
    [
        ("conflict", "Admin calls check", "confirmed", "resolved"),
        ("compatible", "Admin calls check", "refuted", "resolved"),
        ("unresolved", "Admin calls check", "unknown", "insufficient-evidence"),
        ("conflict", "Admin does not call check", "unknown", "output-error"),
    ],
)
async def test_real_sdk_keeps_bound_case_and_rejects_opposite_case(wire, comparison, declared, verdict, outcome):
    payload = submission(comparison) | {"assessed_trigger": declared}
    message = {"role": "assistant", "content": json.dumps(payload)}
    if wire == "native":
        message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "submit",
                    "type": "function",
                    "function": {"name": "submit_investigation", "arguments": json.dumps(wire_submission(payload))},
                }
            ],
        }
    original = copy.deepcopy(message)
    response = {
        "id": "offline",
        "model": "fixture",
        "object": "chat.completion",
        "created": 1,
        "choices": [{"index": 0, "finish_reason": "tool_calls" if wire == "native" else "stop", "message": message}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 80, "total_tokens": 200},
    }
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=response)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        model = ReasoningContentChatOpenAI(
            api_key="offline-only",
            base_url="https://example.invalid/v1",
            model="fixture",
            max_retries=0,
            http_async_client=client,
        )
        value = worker(model)
        observations = copy.deepcopy(value._observations)
        result = await close(value)
    assert result.verdict == verdict and result.outcome == outcome
    assert result.tokens == 200
    assert message == original and value._observations == observations
    assert len(requests) == 1 and requests[0]["tool_choice"] == "required"
    assert "assessed_trigger" in requests[0]["tools"][0]["function"]["parameters"]["required"]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [None, "", 42])
async def test_missing_original_packet_trigger_stops_before_a_model_request(trigger):
    model = Model()
    value = worker(model)
    chat = value._closing_chat([], 24000, hypothesis=candidate())
    packet = json.loads(chat[-1].content)
    if trigger is None:
        del packet["candidate"]["trigger"]
    else:
        packet["candidate"]["trigger"] = trigger
    chat[-1] = chat[-1].model_copy(update={"content": json.dumps(packet)})
    result = await value._close(chat, candidate(), {"check.ts"}, token_limit=24000, steps=3)
    assert result.verdict == "unknown" and result.outcome == "output-error"
    assert not model.calls


@pytest.mark.asyncio
async def test_added_binding_messages_and_schema_are_physically_reserved():
    model = Model()
    budget = RequestBudget(24000, editor_reserve=0)
    snapshots = []

    async def changed(snapshot):
        snapshots.append(snapshot)

    value = worker(BudgetedChatLLM(model, budget, phase="investigation", on_change=changed))
    with investigation_request_scope(12000) as task:
        value._task_request_budget = task
        await close(value, tokens=12000)
    messages, options = model.calls[0]
    assert "assessed_trigger" in options["tools"][0]["function"]["parameters"]["required"]
    assert "assessed_trigger" in messages[0].content
    assert (
        next(iter(snapshots[0]["pending"].values()))["reserved_tokens"]
        == estimate_request(messages, options) + options["max_tokens"]
    )
    planned = next(p for p in value._progress if p.get("stage") == "closing")
    assert planned["input_tokens"] == estimate_request(messages, options)


async def explore_once(monkeypatch, response):
    model = Model(reply=response)
    value = worker(model)
    hypothesis = candidate()
    hypothesis.observations = copy.deepcopy(value._observations)
    original = copy.deepcopy(hypothesis)
    monkeypatch.setattr(value, "_bootstrap", AsyncMock(return_value=False))
    result = await value.investigate(hypothesis, value._state, ContextPack())
    assert hypothesis == original
    assert len(model.calls) == 1
    assert not any(p.get("stage") == "closing" for p in result.progress)
    assert result.steps == 1 and result.tokens == 200
    return result, model


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["proof", "no-tool-terminal"])
@pytest.mark.parametrize("invalid", ["missing", "opposite", "framing", "duplicate", "mixed-progress", "mixed-tool"])
async def test_exploration_terminal_paths_cannot_bypass_trigger_binding(monkeypatch, shape, invalid):
    payload = submission() | {"assessed_trigger": candidate().trigger}
    if shape == "no-tool-terminal":
        payload["assessment"] = None
    if invalid == "missing":
        del payload["assessed_trigger"]
    elif invalid == "opposite":
        payload["assessed_trigger"] = "Admin does not call check"
    elif invalid == "mixed-progress":
        payload["progress"] = {"missing_premise": "contract", "question": "Continue reading"}
    content = json.dumps(payload)
    if invalid == "framing":
        content = f"```json\n{content}\n```"
    elif invalid == "duplicate":
        content = content[:-1] + ',"assessed_trigger":"Admin calls check"}'
    response = AIMessage(content=content)
    if invalid == "mixed-tool":
        response.tool_calls = [{"name": "read_file", "id": "read", "args": {"path": "consumer.ts"}}]
    original = response.model_copy(deep=True)
    result, _ = await explore_once(monkeypatch, response)
    assert result.verdict == "unknown" and result.outcome == "output-error"
    assert response == original


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["assessment", "counterevidence", "no-tool-terminal"])
async def test_exploration_keeps_same_case_refutation_or_unknown_without_closing(monkeypatch, shape):
    payload = submission("compatible") | {"assessed_trigger": candidate().trigger}
    if shape != "assessment":
        payload["assessment"] = None
    if shape == "counterevidence":
        payload["counterevidence"] = {
            "premise": "trigger",
            "statement": "The caller rejects this input",
            "evidence": ["obs_0:e1"],
        }
    result, model = await explore_once(monkeypatch, AIMessage(content=json.dumps(payload)))
    assert result.verdict == ("unknown" if shape == "no-tool-terminal" else "refuted")
    assert result.outcome == ("insufficient-evidence" if shape == "no-tool-terminal" else "resolved")
    assert any(p.get("phase") == "exploration-trigger-check" and p["matched"] for p in result.progress)
    assert "including an early result during exploration" in model.calls[0][0][0].content
    assert json.loads(model.calls[0][0][-1].content)["candidate"]["trigger"] == payload["assessed_trigger"]


@pytest.mark.parametrize("declared", [None, "Admin does not call check"])
def test_central_finalize_cannot_accept_a_model_authored_binding_boolean(declared):
    payload = submission() | {"trigger_checked": True}
    if declared is not None:
        payload["assessed_trigger"] = declared
    result = worker(Model())._finalize(payload, candidate(), {"check.ts"}, steps=1)
    assert result.verdict == "unknown" and result.outcome == "output-error"


def test_trigger_overlay_does_not_change_earlier_protocol_prompts():
    assert "assessed_trigger" in worker(Model())._system_prompt()
    for protocol in (1, 2, 3):
        assert "assessed_trigger" not in worker(Model(), protocol=protocol)._system_prompt()
