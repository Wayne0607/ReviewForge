"""Fixed Git source lines preserve exact source bytes through verification and publication."""

from types import SimpleNamespace

import pytest
from test_v42_verifier import Model

from reviewforge.review_v42.contracts import Case, ReviewScope, Run
from reviewforge.review_v42.line_semantics import source_line_policy, source_lines, source_quote_lines, source_slice
from reviewforge.review_v42.publication import Publication, PublicationOutbox
from reviewforge.review_v42.store import ReviewStore
from reviewforge.review_v42.verifier import Verifier, _citation


def test_absent_and_explicit_source_line_policy():
    assert source_line_policy(SimpleNamespace(metadata={})) is None
    assert source_line_policy(SimpleNamespace(metadata={"source_line_policy": "git-lf-1"})) == "git-lf-1"


@pytest.mark.parametrize("policy", ["unknown", "", None, 0, False, [], {}])
def test_invalid_source_line_policy_is_never_a_legacy_fallback(policy):
    run = SimpleNamespace(metadata={"source_line_policy": policy})
    for operation in (
        lambda: source_line_policy(run),
        lambda: source_lines(run, "a\u2028b\n"),
        lambda: source_slice(run, "a\u2028b\n", 1, 1),
        lambda: source_quote_lines(run, "a\u2028b\n", "b"),
    ):
        with pytest.raises(ValueError, match="source line policy"):
            operation()


@pytest.mark.parametrize(
    "text,lines",
    [
        ("", []),
        ("a", ["a"]),
        ("a\n", ["a\n"]),
        ("\n", ["\n"]),
        ("a\r\nb\rc\u2028d\ve\nend", ["a\r\n", "b\rc\u2028d\ve\n", "end"]),
    ],
)
def test_lf_physical_lines_preserve_exact_original(text, lines):
    run = SimpleNamespace(metadata={"source_line_policy": "git-lf-1"})
    assert source_lines(run, text, keepends=True) == lines
    assert "".join(source_lines(run, text, keepends=True)) == text
    if lines:
        assert source_slice(run, text, 1, len(lines)) == text
        assert source_lines(run, text) == [line[:-1] if line.endswith("\n") else line for line in lines]
    else:
        with pytest.raises(ValueError):
            source_slice(run, text, 1, 1)
    with pytest.raises(ValueError):
        source_slice(run, text, 1, len(lines) + 1)


def setup_source(tmp_path, text, changed_lines, *, new=True):
    region = {
        "id": "region",
        "path": "a.py",
        "side": "head",
        "start_line": 10,
        "end_line": 12,
        "changed_lines": changed_lines,
    }
    metadata = {"changed_regions": [region]}
    if new:
        metadata["source_line_policy"] = "git-lf-1"
    store = ReviewStore(tmp_path / "line-db", Run("run", "repo", "base", "head", metadata=metadata))
    ref = store.put_evidence(
        text.encode("utf-8"), revision="head", side="head", kind="source", path="a.py", start_line=10, end_line=12
    )
    store.add(ReviewScope("scope", "run", "scope", regions=[region], status="reviewed"))
    store.add(
        Case(
            "case",
            "run",
            "scope",
            trigger="trigger",
            mechanism="mechanism",
            claims=[{"id": "claim", "statement": "claim", "impact": "impact"}],
            evidence_refs=[ref.id],
        )
    )
    return store, ref


def verdict(ref, quote, first, last):
    cite = {"ref": ref.id, "quote": quote, "start_line": first, "end_line": last}
    return {
        "verdict": "accept",
        "reason": "independently verified",
        "gaps": [],
        "facts": {
            name: {"statement": name, "citations": [dict(cite)]}
            for name in ("trigger", "actual", "expected", "change", "impact")
        },
    }


async def test_unicode_and_cr_are_not_fake_new_lines_or_wrong_changed_anchor(tmp_path):
    text = "prefix\u2028sub\rstandalone\vother\r\nactual()\nlast"
    store, ref = setup_source(tmp_path, text, [11])
    outcome = await Verifier(store, Model(verdict(ref, "actual()", 11, 11)), None).verify("case")
    assert outcome[0]["verdict"] == "accept"
    assert Publication(store).local_report()["comments"][0]["anchor"]["line"] == 11
    with pytest.raises(ValueError, match="quote absent"):
        _citation(store, {"ref": ref.id, "quote": "sub", "start_line": 11, "end_line": 11})


async def test_multiline_crlf_citation_is_exact_and_publication_anchor_matches(tmp_path):
    text = "alpha\r\nbeta\r\nlast"
    store, ref = setup_source(tmp_path, text, [11])
    result = verdict(ref, "alpha\r\nbeta", 10, 11)
    assert (await Verifier(store, Model(result), None).verify("case"))[0]["verdict"] == "accept"
    assert Publication(store).local_report()["comments"][0]["anchor"]["line"] == 11
    with pytest.raises(ValueError, match="quote absent"):
        _citation(store, {"ref": ref.id, "quote": "alpha\nbeta", "start_line": 10, "end_line": 11})


async def test_newline_at_end_of_quote_cannot_anchor_following_changed_line(tmp_path):
    store, ref = setup_source(tmp_path, "context\r\nchanged\nlast", [11])
    quote = "context\r\n"
    assert source_quote_lines(store.run, quote, quote, first_line=10) == [(10, 10)]
    assert (await Verifier(store, Model(verdict(ref, quote, 10, 10)), None).verify("case"))[0]["verdict"] == "accept"
    assert Publication(store).local_report()["comments"] == []


def test_old_policy_keeps_splitlines_and_old_range_matching(tmp_path):
    text = "alpha\u2028beta\ngamma"
    store, ref = setup_source(tmp_path, text, [11], new=False)
    assert source_lines(store.run, text) == text.splitlines()
    assert _citation(store, {"ref": ref.id, "quote": "beta", "start_line": 11, "end_line": 11}) == ref.id
    with pytest.raises(ValueError, match="quote absent"):
        _citation(store, {"ref": ref.id, "quote": "beta", "start_line": 10, "end_line": 10})


@pytest.mark.parametrize("invalid", ["future-policy", "", None, 123])
async def test_invalid_policy_blocks_empty_report_and_verifier_before_any_model_call(tmp_path, invalid):
    run = Run("empty", "repo", "base", "head", metadata={"source_line_policy": invalid})
    store = ReviewStore(tmp_path / "empty-db", run)
    model = Model()
    with pytest.raises(ValueError, match="source line policy"):
        await Verifier(store, model, None).verify("missing-case")
    assert model.calls == 0
    with pytest.raises(ValueError, match="source line policy"):
        Publication(store).local_report()
    with pytest.raises(ValueError, match="source line policy"):
        PublicationOutbox(store).freeze_batch()


@pytest.mark.parametrize("invalid", ["future-policy", None, 42])
async def test_invalid_policy_blocks_existing_verdict_citation_and_frozen_delivery(tmp_path, invalid):
    store, ref = setup_source(tmp_path, "alpha\r\nbeta\nlast", [11])
    original = verdict(ref, "beta", 11, 11)
    assert (await Verifier(store, Model(original), None).verify("case"))[0]["verdict"] == "accept"
    outbox = PublicationOutbox(store)
    frozen = outbox.freeze()
    assert len(frozen) == 1
    store.run.metadata["source_line_policy"] = invalid
    for operation in (
        lambda: _citation(store, {"ref": ref.id, "quote": "beta", "start_line": 11, "end_line": 11}),
        lambda: Publication(store).local_report(),
        lambda: outbox.freeze_batch(),
        lambda: outbox.prepare(frozen[0]["id"]),
    ):
        with pytest.raises(ValueError, match="source line policy"):
            operation()
    model = Model()
    with pytest.raises(ValueError, match="source line policy"):
        await Verifier(store, model, None).verify("case")
    assert model.calls == 0
