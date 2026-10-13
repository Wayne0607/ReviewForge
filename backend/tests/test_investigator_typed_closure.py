from __future__ import annotations

import copy
import json

import httpx
import openai
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field, ValidationError

from reviewforge.core.state import StateStore
from reviewforge.engine.hypothesis import Hypothesis, Mechanism, Observation, Site
from reviewforge.engine.investigator import Investigator
from reviewforge.engine.openai_compat import ReasoningContentChatOpenAI
from reviewforge.engine.provider_retry import RetryChatLLM, RetryPolicy
from reviewforge.engine.request_budget import (
    BudgetedChatLLM,
    RequestBudget,
    estimate_request,
    investigation_request_scope,
)


def candidate():
    return Hypothesis(
        "h_close",
        "unit::wrong-operator::check",
        "unit",
        Mechanism.WRONG_OPERATOR,
        "Admin-only input is rejected",
        "Admin calls check",
        "Request denied",
        "What does the consumer require?",
        "Consumer only supports owners",
        [Site("check.ts", 1, "return admin && owner;")],
        "error",
        "generator",
    )


def submission(comparison="conflict"):
    return {
        "answer": "The saved consumer permits admin input.",
        "severity": "error",
        "assessment": {
            "expected": "Admin input must pass",
            "actual": "Admin-only input is rejected",
            "comparison": comparison,
            "expected_evidence": ["obs_0:e1"],
            "actual_evidence": ["obs_1:e1"],
        },
        "counterevidence": None,
        "reason": "Supported contract violation",
        "additional_sites": [],
    }


def wire_submission(raw=None):
    raw = submission() if raw is None else raw
    assessment, counter = raw["assessment"], raw["counterevidence"]
    return {
        "answer": raw["answer"],
        "severity": raw["severity"],
        "reason": raw["reason"],
        "assessment_comparison": assessment["comparison"] if assessment else "none",
        "assessment_expected": assessment["expected"] if assessment else "",
        "assessment_actual": assessment["actual"] if assessment else "",
        "assessment_expected_evidence": assessment["expected_evidence"] if assessment else [],
        "assessment_actual_evidence": assessment["actual_evidence"] if assessment else [],
        "counter_premise": counter["premise"] if counter else "none",
        "counter_statement": counter["statement"] if counter else "",
        "counter_evidence": counter["evidence"] if counter else [],
        "additional_site_paths": [site["path"] for site in raw["additional_sites"]],
        "additional_site_lines": [site["line"] for site in raw["additional_sites"]],
        "additional_site_excerpts": [site["excerpt"] for site in raw["additional_sites"]],
    }


def tool_response(args=None, *, name="submit_investigation"):
    return AIMessage(content="", tool_calls=[{"id": "submit", "name": name, "args": args or wire_submission()}])


class Model(BaseChatModel):
    reply: AIMessage = Field(default_factory=tool_response)
    calls: list = Field(default_factory=list)
    bindings: list = Field(default_factory=list)
    bad_request: bool = False

    @property
    def _llm_type(self):
        return "typed-closure-test"

    def bind_tools(self, tools, **kwargs):
        self.bindings.append((tools, kwargs))
        return self.bind(tools=[convert_to_openai_tool(tool) for tool in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append((list(messages), kwargs))
        if self.bad_request:
            raise openai.BadRequestError(
                "unsupported tool_choice",
                response=httpx.Response(
                    400, request=httpx.Request("POST", "https://example.invalid/v1/chat/completions")
                ),
                body=None,
            )
        reply = self.reply.model_copy(deep=True)
        reply.usage_metadata = {"input_tokens": 120, "output_tokens": 80, "total_tokens": 200}
        return ChatResult(generations=[ChatGeneration(message=reply)])


def worker(model, *, protocol=4):
    value = Investigator(model, None, protocol_version=protocol)
    value._state = StateStore(
        repo="o/r", pr_number=1, head_sha="head", file_diffs={"check.ts": "@@ -1 +1 @@\n-x\n+return admin && owner;"}
    )
    value._observations = [
        Observation(
            "obs_0",
            "read_file",
            "read",
            "consumer.ts",
            (1, 1),
            "head",
            "digest",
            "expect(check(admin)).toBe(true);",
            "success",
        ),
        Observation(
            "obs_1", "read_file", "read", "check.ts", (1, 1), "head", "digest", "return admin && owner;", "success"
        ),
        Observation("obs_2", "find_callers", "missing", "", None, "head", "digest", "", "not_found"),
    ]
    return value


async def close(value, *, tokens=24_000):
    return await value._close(
        value._closing_chat([], tokens, hypothesis=candidate()), candidate(), {"check.ts"}, token_limit=tokens, steps=3
    )


def test_submission_schema_uses_current_exact_refs_and_literal_comparison():
    tool = worker(Model())._closing_submission_tool()
    schema = convert_to_openai_tool(tool)["function"]
    assert schema["name"] == "submit_investigation"
    text = json.dumps(schema)
    assert all(value in text for value in ["conflict", "compatible", "unresolved", "obs_0:e1", "obs_1:e1"])
    assert "obs_2:e1" not in text
    assert all(value not in text for value in ["anyOf", "$ref", "$defs", '"null"'])
    parameters = schema["parameters"]
    assert set(parameters["properties"]) == set(parameters["required"])
    for value in parameters["properties"].values():
        assert value["type"] in {"string", "array"}
        if value["type"] == "array":
            assert value["items"]["type"] in {"string", "integer"}
    tool.args_schema.model_validate(wire_submission())
    for raw in [
        wire_submission(submission("The code probably intended OR")),
        {"assessment": None},
        wire_submission() | {"invented": "field"},
    ]:
        with pytest.raises(ValidationError):
            tool.args_schema.model_validate(raw)
    raw = wire_submission()
    raw["assessment_expected_evidence"] = ["obs_99:e1"]
    with pytest.raises(ValidationError):
        tool.args_schema.model_validate(raw)


def test_local_openai_adapter_maps_any_to_required_without_network():
    model = ReasoningContentChatOpenAI(
        model="fixture", api_key="local-test-only", max_retries=0, base_url="https://example.invalid/v1"
    )
    bound = model.bind_tools([worker(Model())._closing_submission_tool()], tool_choice="any")
    assert bound.kwargs["tool_choice"] == "required"
    assert len(bound.kwargs["tools"]) == 1
    assert "strict" not in bound.kwargs["tools"][0]["function"]
    assert "parallel_tool_calls" not in bound.kwargs


def test_no_saved_source_can_only_submit_unknown_without_invalid_empty_enum():
    value = worker(Model())
    value._observations = [value._observations[-1]]
    tool = value._closing_submission_tool()
    schema = convert_to_openai_tool(tool)
    assert '"enum": []' not in json.dumps(schema)
    unknown = wire_submission(submission() | {"assessment": None})
    tool.args_schema.model_validate(unknown)
    with pytest.raises(ValidationError):
        tool.args_schema.model_validate(wire_submission())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "comparison,verdict", [("conflict", "confirmed"), ("compatible", "refuted"), ("unresolved", "unknown")]
)
async def test_result_tool_uses_original_parsing_and_verdict_validation(monkeypatch, comparison, verdict):
    raw = submission(comparison)
    model = Model(reply=tool_response(wire_submission(raw)))
    value = worker(model)
    original = copy.deepcopy(value._observations)
    seen = []
    finalize = value._finalize

    def capture(parsed, *args, **kwargs):
        seen.append(parsed)
        return finalize(parsed, *args, **kwargs)

    monkeypatch.setattr(value, "_finalize", capture)
    result = await close(value)
    assert seen == [raw] and result.verdict == verdict
    assert value._observations == original
    assert len(model.calls) == 1
    assert model.bindings[0][1] == {"tool_choice": "any"}
    assert model.calls[0][1]["tools"][0]["function"]["name"] == "submit_investigation"
    assert "final request: no tools" not in model.calls[0][0][0].content
    assert '"assessment":{' not in model.calls[0][0][0].content
    assert "assessment_comparison" in model.calls[0][0][0].content


@pytest.mark.asyncio
async def test_counterevidence_keeps_original_mutual_exclusion_validation():
    raw = submission() | {
        "counterevidence": {"premise": "mechanism", "statement": "A guard prevents access", "evidence": ["obs_0:e1"]}
    }
    result = await close(worker(Model(reply=tool_response(wire_submission(raw)))))
    assert result.verdict == "unknown" and result.reason == "inconsistent-assessment"
    raw["assessment"] = None
    result = await close(worker(Model(reply=tool_response(wire_submission(raw)))))
    assert result.verdict == "refuted"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["unknown-tool", "multiple-tools", "bad-enum", "bad-ref", "missing-field", "invalid-tool"]
)
async def test_invalid_tool_result_is_unknown_even_if_content_contains_valid_json(kind):
    raw = wire_submission()
    response = tool_response(raw)
    response.content = json.dumps(submission())
    if kind == "unknown-tool":
        response.tool_calls[0]["name"] = "read_file"
    elif kind == "multiple-tools":
        response.tool_calls.append(copy.deepcopy(response.tool_calls[0]))
    elif kind == "bad-enum":
        response.tool_calls[0]["args"]["assessment_comparison"] = "prose cannot become conflict"
    elif kind == "bad-ref":
        response.tool_calls[0]["args"]["assessment_actual_evidence"] = ["obs_2:e1"]
    elif kind == "missing-field":
        del response.tool_calls[0]["args"]["assessment_comparison"]
    else:
        response.invalid_tool_calls = [
            {"name": "submit_investigation", "args": "broken", "id": "bad", "error": "invalid"}
        ]
    result = await close(worker(Model(reply=response)))
    assert result.verdict == "unknown" and result.outcome == "output-error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "stringified-assessment",
        "stringified-reference-array",
        "omitted-none-field",
        "nonempty-none-assessment",
        "nonempty-none-counter",
        "missing-assessment-evidence",
        "missing-counter-evidence",
        "unequal-sites",
        "string-line",
        "boolean-line",
    ],
)
async def test_flat_wire_rejects_malformed_fields_without_repair(case):
    raw = wire_submission()
    if case == "stringified-assessment":
        # The exact observed transport failure: a nested proof becomes a JSON string.
        raw = submission()
        raw["assessment"] = json.dumps(raw["assessment"])
    elif case == "stringified-reference-array":
        raw["assessment_expected_evidence"] = json.dumps(raw["assessment_expected_evidence"])
    elif case == "omitted-none-field":
        del raw["counter_statement"]
    elif case == "nonempty-none-assessment":
        raw["assessment_comparison"] = "none"
    elif case == "nonempty-none-counter":
        raw["counter_statement"] = "Text cannot silently disappear"
    elif case == "missing-assessment-evidence":
        raw["assessment_expected_evidence"] = []
    elif case == "missing-counter-evidence":
        raw = wire_submission(submission() | {"assessment": None})
        raw.update(counter_premise="mechanism", counter_statement="Missing source")
    else:
        raw.update(
            additional_site_paths=["check.ts"],
            additional_site_lines=[1],
            additional_site_excerpts=["return admin && owner;"],
        )
        if case == "unequal-sites":
            raw["additional_site_excerpts"] = []
        else:
            raw["additional_site_lines"] = ["1" if case == "string-line" else True]
    model = Model(reply=tool_response(raw))
    result = await close(worker(model))
    assert result.verdict == "unknown" and result.reason == "invalid-closing-submission"
    assert result.outcome == "output-error" and len(model.calls) == 1


@pytest.mark.asyncio
async def test_flat_none_is_explicit_unknown_and_sites_preserve_exact_binding(monkeypatch):
    raw = submission() | {
        "assessment": None,
        "additional_sites": [{"path": "check.ts", "line": 1, "excerpt": "return admin && owner;"}],
    }
    value = worker(Model(reply=tool_response(wire_submission(raw))))
    seen = []
    finalize = value._finalize

    def capture(parsed, *args, **kwargs):
        seen.append(parsed)
        return finalize(parsed, *args, **kwargs)

    monkeypatch.setattr(value, "_finalize", capture)
    result = await close(value)
    assert seen == [raw]
    assert result.verdict == "unknown" and result.outcome != "output-error"


@pytest.mark.asyncio
async def test_legal_complete_text_json_remains_compatible():
    result = await close(worker(Model(reply=AIMessage(content=json.dumps(submission())))))
    assert result.verdict == "confirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", [1, 2, 3])
async def test_previous_protocols_keep_text_closure(protocol):
    model = Model(reply=AIMessage(content=json.dumps(submission())))
    result = await close(worker(model, protocol=protocol))
    assert result.verdict == "confirmed" and not model.bindings
    assert "tools" not in model.calls[0][1] and "tool_choice" not in model.calls[0][1]


@pytest.mark.asyncio
async def test_schema_and_choice_are_reserved_at_actual_admission_without_extra_budget():
    model = Model()
    budget = RequestBudget(24_000, editor_reserve=0)
    snapshots = []

    async def changed(snapshot):
        snapshots.append(snapshot)

    value = worker(BudgetedChatLLM(model, budget, phase="investigation", on_change=changed))
    with investigation_request_scope(12_000) as task:
        value._task_request_budget = task
        result = await close(value, tokens=12_000)
        task_snapshot = task.snapshot()
    messages, options = model.calls[0]
    reservation = next(iter(snapshots[0]["pending"].values()))["reserved_tokens"]
    assert reservation == estimate_request(messages, options) + options["max_tokens"]
    assert value._progress[-1]["input_tokens"] == estimate_request(messages, options)
    assert task_snapshot["limit"] == 12_000 and task_snapshot["reported_tokens"] == result.tokens == 200
    assert budget.limit == 24_000


@pytest.mark.asyncio
async def test_schema_input_cannot_bypass_512_output_floor():
    model = Model()
    value = worker(model)
    await close(value, tokens=1_000)
    assert not model.calls


@pytest.mark.asyncio
async def test_provider_400_is_one_failed_request_without_fallback_payload():
    model = Model(bad_request=True)
    budget = RequestBudget(24_000, editor_reserve=0)
    llm = RetryChatLLM(
        BudgetedChatLLM(model, budget, phase="investigation"), RetryPolicy(max_attempts=3, base_delay_seconds=0)
    )
    with investigation_request_scope(24_000) as task:
        value = worker(llm)
        value._task_request_budget = task
        result = await close(value)
    assert len(model.calls) == 1
    assert result.verdict == "unknown" and result.outcome == "provider-error"
    assert not result.retryable and result.reason == "provider-parameters-rejected"
    assert model.calls[0][1]["tool_choice"] == "any"
    assert budget.snapshot()["unreported_reserved_tokens"] > 0
