import pytest
from langchain_core.messages import AIMessage

from reviewforge.review_v42.contracts import Case, ReviewScope, Run
from reviewforge.review_v42.publication import Publication
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.verifier import Verifier


class Model:
    def __init__(self, *responses):
        self.responses, self.calls = iter(responses), 0

    def bind_tools(self, tools, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "args": response, "id": "1"}])


def setup(tmp_path, claims=None, changed_lines=None, extra_metadata=None):
    region = {
        "id": "r",
        "path": "a.py",
        "side": "head",
        "start_line": 4,
        "end_line": 5,
        "changed_lines": [5] if changed_lines is None else changed_lines,
    }
    metadata = {"changed_regions": [region], **(extra_metadata or {})}
    store = ReviewStore(tmp_path / "db", Run("run", "repo", "base", "head", metadata=metadata))
    ref = store.put_evidence(
        b"bad\nresult", revision="head", side="head", kind="source", path="a.py", start_line=4, end_line=5
    )
    store.add(ReviewScope("scope", "run", "scope", regions=[region], status="reviewed"))
    store.add(
        Case(
            "case",
            "run",
            "scope",
            trigger="x",
            mechanism="m",
            claims=claims or [{"id": "a", "statement": "s", "impact": "i"}],
            evidence_refs=[ref.id],
        )
    )
    return store, ref


def accepted(ref):
    cite = {"ref": ref.id, "quote": "result", "start_line": 5, "end_line": 5}
    return {
        "verdict": "accept",
        "reason": "verified",
        "gaps": [],
        "facts": {
            k: {"statement": k + " fact", "citations": [dict(cite)]}
            for k in ("trigger", "actual", "expected", "change", "impact")
        },
    }


@pytest.mark.asyncio
async def test_accept_freezes_and_publishes_only_changed_line(tmp_path):
    store, ref = setup(tmp_path)
    model = Model(accepted(ref))
    v = Verifier(store, model, None)
    first = await v.verify("case")
    assert await v.verify("case") == first and model.calls == 1
    report = Publication(store).local_report()
    assert report["status"] == "complete"
    assert report["comments"][0]["anchor"] == {"path": "a.py", "line": 5, "side": "RIGHT"}
    assert report["comments"] == Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_forged_quote_cannot_publish(tmp_path):
    store, ref = setup(tmp_path)
    bad = accepted(ref)
    bad["facts"]["actual"]["citations"][0]["quote"] = "invented"
    out = await Verifier(store, Model(bad, bad), None).verify("case")
    assert out[0]["verdict"] == "unresolved"
    assert not Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_reject_requires_quoted_counterevidence(tmp_path):
    store, ref = setup(tmp_path)
    reject = {
        "verdict": "reject",
        "reason": "premise false",
        "gaps": [],
        "facts": {
            "counterexample": {
                "statement": "trigger impossible",
                "citations": [{"ref": ref.id, "quote": "bad", "start_line": 4, "end_line": 4}],
            }
        },
    }
    assert (await Verifier(store, Model(reject), None).verify("case"))[0]["verdict"] == "reject"


@pytest.mark.asyncio
async def test_stale_case_and_context_line_are_not_comments(tmp_path):
    store, ref = setup(tmp_path, changed_lines=[4])
    await Verifier(store, Model(accepted(ref)), None).verify("case")
    assert not Publication(store).local_report()["comments"]
    store.update(Case, "case", 0, mechanism="other")
    report = Publication(store).local_report()
    assert report["status"] == "partial" and not report["comments"]


@pytest.mark.asyncio
async def test_claims_are_independent_and_gap_remains_on_case(tmp_path):
    claims = [{"id": "a", "statement": "one", "impact": "i"}, {"id": "b", "statement": "two", "impact": "j"}]
    store, ref = setup(tmp_path, claims=claims)
    unknown = {
        "verdict": "needs_evidence",
        "reason": "missing contract",
        "facts": {},
        "gaps": ["Which contract applies?"],
    }
    outcomes = await Verifier(store, Model(accepted(ref), unknown), None).verify("case")
    assert [v["verdict"] for v in outcomes] == ["accept", "needs_evidence"]
    report = Publication(store).local_report()
    assert report["status"] == "partial" and len(report["comments"]) == 1 and report["open_questions"]


@pytest.mark.asyncio
async def test_invalid_source_and_old_head_are_not_publishable(tmp_path):
    store, ref = setup(tmp_path)
    await Verifier(store, Model(accepted(ref)), None).verify("case")
    (store.blob_dir / ref.content_hash).write_bytes(b"tampered")
    report = Publication(store).local_report()
    assert report["status"] == "partial" and not report["comments"]


@pytest.mark.asyncio
async def test_provider_failure_is_unresolved_not_clean(tmp_path):
    store, ref = setup(tmp_path)
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await Verifier(store, Model(RuntimeError("provider unavailable")), None).verify("case")
    report = Publication(store).local_report()
    assert report["status"] == "partial" and not report["comments"]


@pytest.mark.asyncio
async def test_cited_context_is_not_changed_line(tmp_path):
    store, ref = setup(tmp_path, changed_lines=[4])
    result = accepted(ref)
    result["facts"]["actual"]["citations"][0].update(start_line=4, end_line=5)
    await Verifier(store, Model(result), None).verify("case")
    assert not Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_question_withdrawal_invalidates_old_verdict(tmp_path):
    from reviewforge.review_v42.contracts import Question

    store, ref = setup(tmp_path)
    q = Question("q", "run", "contract?", scope_id="scope", case_id="case", status="answered", evidence_refs=[ref.id])
    store.add(q)
    case = store.get(Case, "case")
    store.update(Case, "case", case.revision, question_ids=["q"])
    await Verifier(store, Model(accepted(ref)), None).verify("case")
    assert Publication(store).local_report()["comments"]
    store.update(Question, "q", 0, status="disputed", answer="")
    assert not Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_outbox_revalidates_and_ambiguous_delivery_requires_reconciliation(tmp_path):
    from reviewforge.review_v42.publication import PublicationOutbox

    store, ref = setup(tmp_path)
    await Verifier(store, Model(accepted(ref)), None).verify("case")
    outbox = PublicationOutbox(store)
    item = outbox.freeze()[0]
    assert outbox.freeze() == [item]
    assert outbox.prepare(item["id"]) == item
    outbox.mark_unknown(item["id"])
    with pytest.raises(ValueError):
        outbox.prepare(item["id"])
    outbox.reconcile(item["id"], delivered=False, evidence={"lookup": "not found"})
    assert outbox.prepare(item["id"]) == item
    outbox.acknowledge(item["id"], {"id": "remote-1"})
    outbox.acknowledge(item["id"], {"id": "remote-1"})
    assert outbox.get(item["id"])["status"] == "delivered"
    with pytest.raises(ValueError):
        outbox.prepare(item["id"])


@pytest.mark.asyncio
async def test_outbox_rejects_stale_permission_before_send(tmp_path):
    from reviewforge.review_v42.publication import PublicationOutbox

    store, ref = setup(tmp_path)
    await Verifier(store, Model(accepted(ref)), None).verify("case")
    outbox = PublicationOutbox(store)
    item = outbox.freeze()[0]
    store.update(Case, "case", 0, mechanism="changed conclusion")
    with pytest.raises(ValueError, match="no longer current"):
        outbox.prepare(item["id"])


@pytest.mark.asyncio
async def test_duplicate_raw_native_fields_cannot_create_verdict(tmp_path):
    from langchain_core.messages import AIMessage

    store, ref = setup(tmp_path)
    parsed = accepted(ref)
    raw = '{"verdict":"accept","verdict":"reject"}'
    response = AIMessage(
        content="",
        tool_calls=[{"name": "submit_verdict", "args": parsed, "id": "1"}],
        additional_kwargs={
            "reviewforge_raw_tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "submit_verdict", "arguments": raw}}
            ]
        },
    )

    class RawModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            return response

    result = await Verifier(store, RawModel(), None).verify("case")
    assert result[0]["verdict"] == "unresolved"
    assert not Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_truncated_or_mixed_native_submission_never_accepts(tmp_path):
    store, ref = setup(tmp_path)
    parsed = accepted(ref)
    bad = AIMessage(
        content="",
        tool_calls=[{"name": "submit_verdict", "args": parsed, "id": "1"}],
        additional_kwargs={
            "reviewforge_raw_tool_calls": [
                {"id": "1", "type": "function", "function": {"name": "submit_verdict", "arguments": '{"verdict":'}}
            ]
        },
    )

    class RawModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            return bad

    assert (await Verifier(store, RawModel(), None).verify("case"))[0]["verdict"] == "unresolved"


@pytest.mark.asyncio
async def test_mixed_submit_and_read_not_accepted(tmp_path):
    store, ref = setup(tmp_path)
    response = AIMessage(
        content="",
        tool_calls=[
            {"name": "submit_verdict", "args": accepted(ref), "id": "a"},
            {"name": "read_evidence", "args": {"ref": ref.id}, "id": "b"},
        ],
    )

    class RawModel:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            return response

    assert (await Verifier(store, RawModel(), None).verify("case"))[0]["verdict"] == "unresolved"
    assert not Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_outbox_crash_preserves_sending_until_explicit_reconciliation(tmp_path):
    from reviewforge.review_v42.publication import PublicationOutbox

    store, ref = setup(tmp_path)
    await Verifier(store, Model(accepted(ref)), None).verify("case")
    first = PublicationOutbox(store)
    batch = first.freeze_batch()
    item = batch["report"]["comments"][0]
    assert first.get_batch(batch["id"]) == batch
    first.prepare(item["id"])
    restarted = PublicationOutbox(ReviewStore(store.path, store.run))
    assert restarted.get(item["id"])["status"] == "sending"
    with pytest.raises(ValueError):
        restarted.prepare(item["id"])
    restarted.mark_unknown(item["id"])
    with pytest.raises(ValueError):
        restarted.prepare(item["id"])
    restarted.reconcile(item["id"], delivered=True, evidence={"remote_id": "123"})
    assert restarted.get(item["id"])["status"] == "delivered"
    with pytest.raises(ValueError):
        restarted.prepare(item["id"])


@pytest.mark.asyncio
async def test_question_withdrawal_revokes_frozen_outbox_permission(tmp_path):
    from reviewforge.review_v42.contracts import Question
    from reviewforge.review_v42.publication import PublicationOutbox

    store, ref = setup(tmp_path)
    store.add(Question("q", "run", "contract?", scope_id="scope", case_id="case", status="answered"))
    case = store.get(Case, "case")
    store.update(Case, "case", case.revision, question_ids=["q"])
    await Verifier(store, Model(accepted(ref)), None).verify("case")
    outbox = PublicationOutbox(store)
    identity = outbox.freeze()[0]["id"]
    store.update(Question, "q", 0, status="disputed")
    with pytest.raises(ValueError, match="no longer current"):
        outbox.prepare(identity)


@pytest.mark.asyncio
async def test_repeated_alternating_evidence_state_stops_unresolved(tmp_path):
    store, ref = setup(tmp_path)

    class RepeatingModel:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            return AIMessage(
                content="",
                tool_calls=[
                    {"name": "read_evidence", "args": {"ref": ref.id, "marker": self.calls % 2}, "id": str(self.calls)}
                ],
            )

    class Gateway:
        async def execute(self, name, args):
            return {"evidence_ref": ref.id, "content": "bad\nresult"}

    model = RepeatingModel()
    result = await Verifier(store, model, Gateway()).verify("case")
    assert result[0]["verdict"] == "unresolved"
    assert model.calls == 3


@pytest.mark.asyncio
async def test_verifier_gap_write_requires_current_lease_and_requeues(tmp_path):
    from reviewforge.review_v42.contracts import LeaseError, Question, RevisionConflict, Task

    store, ref = setup(tmp_path)
    unknown = {"verdict": "needs_evidence", "reason": "missing", "facts": {}, "gaps": ["Which contract?"]}
    store.add(Task("t", "run", "verify", "case", 0))
    expired = store.acquire_task("t", "worker", 1, now=0)
    with pytest.raises(LeaseError):
        await Verifier(store, Model(unknown), None).verify("case", task_id="t", lease_token=expired.lease_token)
    assert not store.list(Question) and store.get(Case, "case").revision == 0
    live = store.acquire_task("t", "replacement", 120)
    with pytest.raises(RevisionConflict):
        await Verifier(store, Model(unknown), None).verify("case", task_id="t", lease_token=live.lease_token)
    assert len(store.list(Question)) == 1 and store.get(Case, "case").revision == 1


def test_superseded_parent_does_not_force_partial_report(tmp_path):
    store = ReviewStore(tmp_path / "db", Run("run", "repo", "base", "head"))
    store.add(ReviewScope("scope", "run", "active child", status="reviewed"))
    store.add(ReviewScope("old", "run", "old parent", status="superseded"))
    assert Publication(store).local_report()["status"] == "complete"


def test_empty_candidate_cannot_be_a_clean_report(tmp_path):
    store, _ = setup(tmp_path)
    store.update(Case, "case", 0, claims=[])
    report = Publication(store).local_report()
    assert report["status"] == "partial" and report["unresolved"]


@pytest.mark.asyncio
async def test_tool_retry_can_recover_and_private_reasoning_is_not_replayed(tmp_path):
    store, ref = setup(tmp_path)

    class RetryModel:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 2:
                assert messages[2].additional_kwargs == {}
            if self.calls < 3:
                return AIMessage(
                    content="",
                    tool_calls=[{"name": "read_evidence", "args": {"ref": ref.id}, "id": str(self.calls)}],
                    additional_kwargs={"reasoning_content": "private"},
                )
            return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "args": accepted(ref), "id": "final"}])

    class RetryGateway:
        calls = 0

        async def execute(self, name, args):
            self.calls += 1
            if self.calls == 1:
                return {"error": "temporary failure"}
            return {"evidence_ref": ref.id, "content": "bad\nresult"}

    result = await Verifier(store, RetryModel(), RetryGateway()).verify("case")
    assert result[0]["verdict"] == "accept" and result[0]["result"]["tool_failures"] == []


@pytest.mark.asyncio
async def test_optional_tool_failure_can_be_explained_without_becoming_evidence(tmp_path):
    store, ref = setup(tmp_path)

    class OptionalModel:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(
                    content="", tool_calls=[{"name": "run_check", "args": {"test_spec": "optional"}, "id": "1"}]
                )
            result = accepted(ref)
            result["failure_explanation"] = "The original source directly establishes this behavior."
            return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "args": result, "id": "2"}])

    class Unavailable:
        async def execute(self, name, args):
            return {"metadata": {"status": "unavailable"}}

    result = await Verifier(store, OptionalModel(), Unavailable()).verify("case")
    assert result[0]["verdict"] == "accept"
    assert result[0]["result"]["tool_failures"]
    assert Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_external_issue_can_support_expected_but_not_head_execution(tmp_path):
    from reviewforge.review_v42.verifier import validate_result

    store, source = setup(tmp_path)
    issue = store.put_evidence(
        b"The operation must preserve the original value.",
        revision="2026-01-01T00:00:00Z",
        side="external",
        kind="issue",
        path="https://example.com/issues/1",
        metadata={
            "source_updated_at": "2026-01-01T00:00:00Z",
            "source_url": "https://example.com/issues/1",
            "captured_at": "2026-11-01T00:00:00Z",
            "historical_head_time": False,
        },
    )
    case = store.get(Case, "case")
    store.update(Case, "case", case.revision, evidence_refs=[source.id, issue.id])
    result = accepted(source)
    cite = {"ref": issue.id, "quote": "must preserve the original value"}
    result["facts"]["expected"]["citations"] = [cite]
    out = await Verifier(store, Model(result), None).verify("case")
    assert out[0]["verdict"] == "accept"
    assert "external issue snapshot" in Publication(store).local_report()["comments"][0]["body"]
    result["facts"]["actual"]["citations"].append(cite)
    with pytest.raises(ValueError, match="external snapshot"):
        validate_result(store, result, {source.id, issue.id})
    result = accepted(source)
    result["facts"]["change"]["citations"] = [cite]
    with pytest.raises(ValueError, match="external snapshot"):
        validate_result(store, result, {source.id, issue.id})


@pytest.mark.asyncio
async def test_pure_deletion_keeps_verified_facts_without_fake_inline_comment(tmp_path):
    from reviewforge.review_v42.publication import PublicationOutbox

    store, source = setup(tmp_path, changed_lines=[])
    await Verifier(store, Model(accepted(source)), None).verify("case")
    report = Publication(store).local_report()
    assert report["status"] == "partial"
    assert report["verified"][0]["verdict"] == "accept" and report["comments"] == []
    batch = PublicationOutbox(store).freeze_batch()
    assert batch["report"]["verified"][0]["verdict"] == "accept"
    assert batch["report"]["comments"] == []


def test_issue_cannot_masquerade_as_head_evidence(tmp_path):
    from reviewforge.review_v42.verifier import validate_result

    store, source = setup(tmp_path)
    fake = store.put_evidence(b"must do this", revision="head", side="head", kind="issue")
    result = accepted(source)
    result["facts"]["expected"]["citations"] = [{"ref": fake.id, "quote": "must do this"}]
    with pytest.raises(ValueError, match="external provenance"):
        validate_result(store, result, {source.id, fake.id})


@pytest.mark.asyncio
async def test_capacity_recovery_uses_independent_source_index_and_explicit_page(tmp_path):
    from reviewforge.review_v42.tools import EvidenceGateway

    store, ref = setup(tmp_path)

    class CapacityError(Exception):
        code = "context_length_exceeded"

    class CapacityModel:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise CapacityError()
            if self.calls == 2:
                import json

                packet = json.loads(messages[1].content)
                assert packet["evidence_directory"][0]["id"] == ref.id
                assert "evidence" not in packet and "reviewer" not in packet
                return AIMessage(
                    content="",
                    tool_calls=[
                        {"name": "read_evidence", "id": "one", "args": {"ref": ref.id, "start_line": 5, "end_line": 5}}
                    ],
                )
            return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "id": "two", "args": accepted(ref)}])

    out = await Verifier(store, CapacityModel(), EvidenceGateway(store, None)).verify("case")
    assert out[0]["verdict"] == "accept"


@pytest.mark.asyncio
async def test_repeated_capacity_failure_stays_unresolved(tmp_path):
    store, _ = setup(tmp_path)

    class CapacityError(Exception):
        code = "context_length_exceeded"

    class AlwaysTooLarge:
        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            raise CapacityError()

    out = await Verifier(store, AlwaysTooLarge(), None).verify("case")
    assert out[0]["verdict"] == "unresolved"
    assert "same recovered input" in out[0]["result"]["gaps"][0]
    assert not Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_recovery_cannot_cite_unread_source_or_ignore_counterevidence(tmp_path):
    store, ref = setup(tmp_path)
    counter = store.put_evidence(b"counter", revision="head", side="head", kind="source", path="counter.py")
    store.update(Case, "case", 0, counter_evidence_refs=[counter.id])

    class CapacityError(Exception):
        code = "context_length_exceeded"

    class GuessingModel:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise CapacityError()
            return AIMessage(
                content="", tool_calls=[{"name": "submit_verdict", "id": str(self.calls), "args": accepted(ref)}]
            )

    out = await Verifier(store, GuessingModel(), None).verify("case")
    assert out[0]["verdict"] == "unresolved"
    assert not Publication(store).local_report()["comments"]


@pytest.mark.asyncio
async def test_recovery_reads_all_counterevidence_using_pages(tmp_path):
    from reviewforge.review_v42.tools import EvidenceGateway

    store, ref = setup(tmp_path)
    counter = store.put_evidence(b"first\nsecond", revision="head", side="head", kind="source", path="counter.py")
    store.update(Case, "case", 0, counter_evidence_refs=[counter.id])

    class CapacityError(Exception):
        code = "context_length_exceeded"

    class ModelWithPages:
        calls = 0

        def bind_tools(self, tools, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                raise CapacityError()
            args = {
                2: {"ref": ref.id, "start_line": 5, "end_line": 5},
                3: {"ref": counter.id, "start_line": 1, "end_line": 1},
                4: {"ref": counter.id, "start_line": 2, "end_line": 2},
            }
            if self.calls in args:
                return AIMessage(
                    content="", tool_calls=[{"name": "read_evidence", "id": str(self.calls), "args": args[self.calls]}]
                )
            return AIMessage(content="", tool_calls=[{"name": "submit_verdict", "id": "done", "args": accepted(ref)}])

    out = await Verifier(store, ModelWithPages(), EvidenceGateway(store, None)).verify("case")
    assert out[0]["verdict"] == "accept"


@pytest.mark.asyncio
@pytest.mark.parametrize("language,label", [("zh-CN", "触发条件"), ("en", "Trigger"), ("auto", "Trigger")])
async def test_output_language_is_whitelisted_and_drives_comment_labels(tmp_path, language, label):
    import json

    store, ref = setup(tmp_path, extra_metadata={"output_language": language, "private_notes": "must-not-leak"})

    class CaptureModel(Model):
        async def ainvoke(self, messages):
            packet = json.loads(messages[1].content)
            assert packet.get("output_language") == (language if language in {"en", "zh-CN"} else None)
            assert "must-not-leak" not in messages[1].content
            return await super().ainvoke(messages)

    await Verifier(store, CaptureModel(accepted(ref)), None).verify("case")
    assert f"**{label}:**" in Publication(store).local_report()["comments"][0]["body"]
