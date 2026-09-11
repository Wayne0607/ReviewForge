from __future__ import annotations

import json

import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field

from reviewforge.engine.editor import (
    Editor,
    Publication,
    PublicationComment,
    cluster_confirmed,
    order_clusters,
    render_review_body,
    split_for_publication,
    validate_comments,
)
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Mechanism, Site


def _hyp(
    index: int, mechanism: Mechanism, anchor: str, severity: str, strength: str, *, sites: list[Site] | None = None
) -> Hypothesis:
    identity = f"u{index}::{mechanism.value}::{anchor}"
    return Hypothesis(
        id=f"h_{index}",
        identity=identity,
        unit_id=f"u{index}",
        mechanism=mechanism,
        claim=f"claim {index}",
        trigger="t",
        impact="i",
        open_question="q",
        refutation="r",
        sites=sites or [Site(path="a.py", line=index, excerpt="excerpt")],
        severity=severity,
        source="generator",
        status=HypothesisStatus.CONFIRMED,
        evidence_strength=strength,
    )


def _ledger(*hypotheses: Hypothesis) -> HypothesisLedger:
    ledger = HypothesisLedger("run", "abc", "digest")
    for hypothesis in hypotheses:
        ledger.upsert(hypothesis)
    return ledger


def test_cluster_merges_sites_and_takes_max_severity_strength() -> None:
    ledger = _ledger(
        _hyp(1, Mechanism.NULL_PATH, "f", "warning", "weak"),
        _hyp(2, Mechanism.NULL_PATH, "f", "error", "strong", sites=[Site(path="a.py", line=99, excerpt="other")]),
    )
    clusters = cluster_confirmed(ledger)
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.severity == "error"
    assert cluster.strength == "strong"
    assert {(site.path, site.line) for site in cluster.sites} == {("a.py", 1), ("a.py", 99)}


def test_order_clusters_by_severity_strength_sites() -> None:
    weak = _hyp(1, Mechanism.NULL_PATH, "a", "error", "weak")
    strong = _hyp(2, Mechanism.LOCK_SCOPE, "b", "error", "strong")
    many = _hyp(
        3, Mechanism.I18N, "c", "warning", "strong", sites=[Site(path="a.py", line=i, excerpt="e") for i in range(1, 6)]
    )
    ledger = _ledger(weak, strong, many)
    ordered = order_clusters(cluster_confirmed(ledger))
    assert [cluster.key[1] for cluster in ordered] == ["b", "a", "c"]


def test_split_inline_cap_and_error_strong_overflow() -> None:
    error_strong = [_hyp(i, Mechanism.NULL_PATH, f"s{i}", "error", "strong") for i in range(8)]
    warning = [_hyp(100 + i, Mechanism.DOC, f"w{i}", "warning", "weak") for i in range(2)]
    ordered = order_clusters(cluster_confirmed(_ledger(*error_strong, *warning)))

    inline, summary = split_for_publication(ordered, max_inline=5, max_inline_overflow=8)
    assert len(inline) == 8  # 5 base + 3 error/strong overflow
    assert len(summary) == 2
    assert all(cluster.severity == "error" for cluster in inline)


def test_validate_comments_requires_site_path() -> None:
    hypothesis = _hyp(
        1, Mechanism.NULL_PATH, "f", "error", "strong", sites=[Site(path="a.py", line=10, excerpt="excerpt")]
    )
    ledger = _ledger(hypothesis)
    ok = PublicationComment(hypothesis_ids=["h_1"], path="a.py", line=10, title="t", body="b")
    bad = PublicationComment(hypothesis_ids=["h_1"], path="a.py", line=999, title="t", body="b")

    valid, rejected = validate_comments(
        Publication(comments=[ok, bad], summary_items=[], merged=[]), ledger, max_comments=5
    )
    assert len(valid) == 1
    assert valid[0].path == "a.py"
    assert rejected == ["a.py"]


class _ScriptedLLM(BaseChatModel):
    responses: list = Field(default_factory=list)
    model_config = ConfigDict(arbitrary_types_allowed=True)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        content = self.responses.pop(0) if self.responses else "{}"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=content))])

    @property
    def _llm_type(self):
        return "scripted"

    @property
    def _identifying_params(self):
        return {}


@pytest.mark.asyncio
async def test_editor_falls_back_when_llm_is_invalid() -> None:
    ledger = _ledger(_hyp(1, Mechanism.NULL_PATH, "f", "error", "strong"))
    editor = Editor(_ScriptedLLM(responses=["this is not json"]))

    publication = await editor.run(ledger, None)

    assert publication.fallback is True
    assert len(publication.comments) == 1
    assert publication.comments[0].hypothesis_ids == ["h_1"]


@pytest.mark.asyncio
async def test_editor_parses_valid_comments() -> None:
    hypothesis = _hyp(
        1, Mechanism.NULL_PATH, "f", "error", "strong", sites=[Site(path="a.py", line=10, excerpt="excerpt")]
    )
    ledger = _ledger(hypothesis)
    response = json.dumps(
        {
            "comments": [
                {
                    "hypothesis_ids": ["h_1"],
                    "path": "a.py",
                    "line": 10,
                    "title": "null deref",
                    "body": "Issue: ...\nWhy: ...\nWhere:\n- a.py:10\nFix: ...",
                    "suggestion_patch": "",
                }
            ],
            "summary_items": [],
            "merged": [],
        }
    )
    editor = Editor(_ScriptedLLM(responses=[response]))

    publication = await editor.run(ledger, None)

    assert publication.fallback is False
    assert len(publication.comments) == 1
    assert publication.comments[0].path == "a.py"


def test_render_review_body_lists_summary_and_unknown() -> None:
    ledger = _ledger(_hyp(1, Mechanism.NULL_PATH, "f", "error", "strong"))
    publication = Publication(
        comments=[],
        summary_items=[("h_1", "one-line summary")],
        merged=[],
        unknown_ids=["h_1"],
    )

    body = render_review_body(publication, ledger, output_language="en")

    assert "<details>" in body
    assert "one-line summary" in body
    assert "could not be confirmed within budget" in body
    assert "claim 1" in body  # UNKNOWN claim text resolved from the ledger


def test_render_review_body_uses_fixed_zh_wording() -> None:
    ledger = _ledger(_hyp(1, Mechanism.NULL_PATH, "f", "error", "strong"))
    publication = Publication(comments=[], summary_items=[], merged=[], unknown_ids=["h_1"])

    body = render_review_body(publication, ledger, output_language="zh-CN")

    assert "未能在预算内确认" in body
