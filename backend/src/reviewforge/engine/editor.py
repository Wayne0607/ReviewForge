"""Editor — deterministic selection + LLM comment writing (§4.7).

The editor is a single publication_gate call per PR.  Deterministic
preprocessing (cluster, rank, split) happens before any LLM call, so the
inline-vs-summary split and its ordering are reproducible.  On LLM failure the
confirmed hypotheses are still published through a deterministic template.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from reviewforge.core.json_output import extract_json_value
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Site
from reviewforge.engine.prompts_v4 import load_prompt

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
_STRENGTH_RANK = {"none": 0, "weak": 1, "strong": 2}


@dataclass
class ConfirmedCluster:
    """One (mechanism, anchor_symbol) group of confirmed hypotheses."""

    key: tuple[str, str]
    hypothesis_ids: list[str]
    hypotheses: list[Hypothesis]
    sites: list[Site]
    severity: str
    strength: str


@dataclass
class PublicationComment:
    hypothesis_ids: list[str]
    path: str
    line: int
    title: str
    body: str
    suggestion_patch: str = ""


@dataclass
class Publication:
    comments: list[PublicationComment]
    summary_items: list[tuple[str, str]]
    merged: list[list[str]]
    unknown_ids: list[str] = field(default_factory=list)
    fallback: bool = False


def _anchor(hypothesis: Hypothesis) -> str:
    return hypothesis.identity.rsplit("::", 1)[-1]


def _max_severity(a: str, b: str) -> str:
    return a if _SEVERITY_RANK.get(a, 0) >= _SEVERITY_RANK.get(b, 0) else b


def _max_strength(a: str, b: str) -> str:
    return a if _STRENGTH_RANK.get(a, 0) >= _STRENGTH_RANK.get(b, 0) else b


def cluster_confirmed(ledger: HypothesisLedger) -> list[ConfirmedCluster]:
    """Group CONFIRMED hypotheses by (mechanism, anchor_symbol) and merge sites."""

    groups: dict[tuple[str, str], ConfirmedCluster] = {}
    for hypothesis in sorted(ledger.items.values(), key=lambda item: item.identity):
        if hypothesis.status != HypothesisStatus.CONFIRMED:
            continue
        key = (hypothesis.mechanism.value, _anchor(hypothesis))
        cluster = groups.get(key)
        if cluster is None:
            cluster = ConfirmedCluster(
                key=key, hypothesis_ids=[], hypotheses=[], sites=[], severity="info", strength="none"
            )
            groups[key] = cluster
        cluster.hypothesis_ids.append(hypothesis.id)
        cluster.hypotheses.append(hypothesis)
        cluster.severity = _max_severity(cluster.severity, hypothesis.severity)
        cluster.strength = _max_strength(cluster.strength, hypothesis.evidence_strength)
        seen = {(site.path, site.line, site.excerpt) for site in cluster.sites}
        for site in hypothesis.sites:
            site_key = (site.path, site.line, site.excerpt)
            if site_key not in seen:
                cluster.sites.append(site)
                seen.add(site_key)
    return list(groups.values())


def _sort_key(cluster: ConfirmedCluster) -> tuple[int, int, int, tuple[str, str]]:
    return (
        _SEVERITY_RANK.get(cluster.severity, 0),
        _STRENGTH_RANK.get(cluster.strength, 0),
        min(len(cluster.sites), 3),
        cluster.key,
    )


def order_clusters(clusters: list[ConfirmedCluster]) -> list[ConfirmedCluster]:
    return sorted(clusters, key=_sort_key, reverse=True)


def split_for_publication(
    ordered: list[ConfirmedCluster], max_inline: int, max_inline_overflow: int
) -> tuple[list[ConfirmedCluster], list[ConfirmedCluster]]:
    """Split into inline (published as comments) and summary clusters."""

    inline: list[ConfirmedCluster] = list(ordered[: max(0, max_inline)])
    for cluster in ordered[max(0, max_inline) : max(max(0, max_inline), max_inline_overflow)]:
        if cluster.severity == "error" and cluster.strength == "strong":
            inline.append(cluster)
    inline_keys = {cluster.key for cluster in inline}
    summary = [cluster for cluster in ordered if cluster.key not in inline_keys]
    return inline, summary


def fallback_comment(cluster: ConfirmedCluster) -> PublicationComment:
    """Deterministic template used when the editor LLM fails (§4.7 failure)."""

    site = cluster.sites[0]
    where = "\n".join(f"- {site.path}:{site.line}" for site in cluster.sites)
    body = "\n\n".join(
        [
            f"Issue: {cluster.hypotheses[0].claim}",
            f"Why: {cluster.hypotheses[0].impact}",
            f"Where:\n{where}",
            f"Fix: {_fix_suggestion(cluster)}",
        ]
    )
    return PublicationComment(
        hypothesis_ids=list(cluster.hypothesis_ids),
        path=site.path,
        line=site.line,
        title=cluster.hypotheses[0].claim[:60],
        body=body,
    )


def _fix_suggestion(cluster: ConfirmedCluster) -> str:
    trigger = cluster.hypotheses[0].trigger
    return f"Address: {trigger}" if trigger else "Address the underlying mechanism above."


def _parse_publication(content: str) -> dict[str, Any] | None:
    parsed = extract_json_value(content or "", required_key="comments", allow_list=False)
    return parsed if isinstance(parsed, dict) else None


def _site_index(ledger: HypothesisLedger) -> dict[str, set[tuple[str, int]]]:
    index: dict[str, set[tuple[str, int]]] = {}
    for item in ledger.items.values():
        index[item.id] = {(site.path, site.line) for site in item.sites}
    return index


def validate_comments(
    publication: Publication, ledger: HypothesisLedger, *, max_comments: int
) -> tuple[list[PublicationComment], list[str]]:
    """Drop comments whose path:line is not a site of a referenced hypothesis."""

    sites = _site_index(ledger)
    valid: list[PublicationComment] = []
    rejected: list[str] = []
    for comment in publication.comments:
        if len(valid) >= max_comments:
            break
        if comment.hypothesis_ids and all(
            (comment.path, comment.line) in sites.get(identity, set()) for identity in comment.hypothesis_ids
        ):
            valid.append(comment)
        else:
            rejected.append(comment.path if comment.path else "<no-path>")
    return valid, rejected


class Editor:
    def __init__(
        self,
        llm: BaseChatModel,
        *,
        output_language: str = "en",
        max_inline: int = 5,
        max_inline_overflow: int = 8,
    ) -> None:
        self._llm = llm
        self._output_language = output_language
        self._max_inline = max(0, int(max_inline))
        self._max_inline_overflow = max(self._max_inline, int(max_inline_overflow))

    def _system_prompt(self) -> str:
        language = "简体中文" if self._output_language == "zh-CN" else "English"
        return load_prompt("editor", output_language=language)

    def _render(
        self,
        ordered: list[ConfirmedCluster],
        inline: list[ConfirmedCluster],
        summary: list[ConfirmedCluster],
        pack: Any,
        ledger: HypothesisLedger,
    ) -> str:
        confirmed_lines = []
        for cluster in ordered:
            header = (
                f"- {cluster.hypothesis_ids} | {cluster.key} | "
                f"severity={cluster.severity} | strength={cluster.strength}"
            )
            sites = ", ".join(f"{site.path}:{site.line}" for site in cluster.sites)
            confirmed_lines.append(f"{header}\n  claim: {cluster.hypotheses[0].claim}\n  sites: {sites}")
        unknown_ids = [
            item.id
            for item in sorted(ledger.items.values(), key=lambda item: item.identity)
            if item.status == HypothesisStatus.UNKNOWN
        ]
        unknown_lines = [f"- {item.id}: {item.claim}" for item in ledger.items.values() if item.id in set(unknown_ids)]
        return "\n\n".join(
            [
                "## PR intent\n" + (getattr(pack, "pr_intent", "") or "（无）/(none)"),
                "## Confirmed\n" + ("\n".join(confirmed_lines) or "（无）/(none)"),
                "## Unknown claims\n" + ("\n".join(unknown_lines) or "（无）/(none)"),
            ]
        )

    async def run(self, ledger: HypothesisLedger, pack: Any) -> Publication:
        """Cluster, select, and ask the editor LLM for comments.

        Falls back to the deterministic template when the LLM cannot produce a
        valid JSON object, so confirmed hypotheses are always publishable.
        """

        ordered = order_clusters(cluster_confirmed(ledger))
        inline, summary = split_for_publication(ordered, self._max_inline, self._max_inline_overflow)
        unknown_ids = [
            item.id
            for item in sorted(ledger.items.values(), key=lambda item: item.identity)
            if item.status == HypothesisStatus.UNKNOWN
        ]

        if not inline:
            return Publication(comments=[], summary_items=[], merged=[], unknown_ids=unknown_ids)

        messages = [
            SystemMessage(content=self._system_prompt()),
            HumanMessage(content=self._render(ordered, inline, summary, pack, ledger)),
        ]
        parsed = None
        try:
            response = await self._llm.ainvoke(messages)
            parsed = _parse_publication(getattr(response, "content", "") or "")
        except Exception as exc:
            logger.warning("editor LLM failed, using deterministic fallback: %s", exc)

        if parsed is None:
            comments = [fallback_comment(cluster) for cluster in inline]
            return Publication(comments=comments, summary_items=[], merged=[], unknown_ids=unknown_ids, fallback=True)

        comments = self._comments_from(parsed, ledger)
        valid, _rejected = validate_comments(
            Publication(comments=comments, summary_items=[], merged=[], unknown_ids=unknown_ids),
            ledger,
            max_comments=self._max_inline_overflow,
        )
        if not valid and inline:
            valid = [fallback_comment(cluster) for cluster in inline]
            return Publication(comments=valid, summary_items=[], merged=[], unknown_ids=unknown_ids, fallback=True)

        summary_items = self._summary_from(parsed, ledger)
        return Publication(
            comments=valid, summary_items=summary_items, merged=_merged_from(parsed), unknown_ids=unknown_ids
        )

    def _comments_from(self, parsed: dict[str, Any], ledger: HypothesisLedger) -> list[PublicationComment]:
        comments: list[PublicationComment] = []
        known_ids = {item.id for item in ledger.items.values()}
        for raw in parsed.get("comments", []) or []:
            if not isinstance(raw, dict):
                continue
            ids = [str(i) for i in raw.get("hypothesis_ids", []) or [] if i and str(i) in known_ids]
            path = str(raw.get("path", "")).strip()
            line = raw.get("line")
            if not ids or not path or not isinstance(line, int):
                continue
            comments.append(
                PublicationComment(
                    hypothesis_ids=ids,
                    path=path,
                    line=max(1, int(line)),
                    title=str(raw.get("title", ""))[:60],
                    body=str(raw.get("body", "")).strip(),
                    suggestion_patch=str(raw.get("suggestion_patch", "")).strip(),
                )
            )
        return comments

    def _summary_from(self, parsed: dict[str, Any], ledger: HypothesisLedger) -> list[tuple[str, str]]:
        known_ids = {item.id for item in ledger.items.values()}
        summary: list[tuple[str, str]] = []
        for raw in parsed.get("summary_items", []) or []:
            if not isinstance(raw, dict):
                continue
            identity = str(raw.get("hypothesis_id", ""))
            if identity in known_ids:
                summary.append((identity, str(raw.get("one_line", ""))))
        return summary


def _merged_from(parsed: dict[str, Any]) -> list[list[str]]:
    merged: list[list[str]] = []
    for raw in parsed.get("merged", []) or []:
        if isinstance(raw, list):
            pair = [str(item) for item in raw if item]
            if len(pair) > 1:
                merged.append(pair)
    return merged


def render_review_body(publication: Publication, ledger: HypothesisLedger, *, output_language: str = "auto") -> str:
    """Render the PR review body ``<details>`` from summary items + UNKNOWN claims."""

    english = output_language != "zh-CN"
    claims_by_id = {item.id: item.claim for item in ledger.items.values()}
    lines: list[str] = ["<details>\n<summary>Review summary</summary>\n"]

    if publication.summary_items:
        lines.append(f"\n**{'Summary' if english else '摘要'}**\n")
        for _identity, one_line in publication.summary_items:
            lines.append(f"- {one_line}\n")

    if publication.unknown_ids:
        wording = "could not be confirmed within budget" if english else "未能在预算内确认"
        lines.append(f"\n**{'Unconfirmed' if english else '未能确认'}**（{wording}）\n")
        for identity in publication.unknown_ids:
            lines.append(f"- {claims_by_id.get(identity, identity)}\n")

    lines.append("\n</details>\n")
    return "".join(lines)


__all__ = [
    "ConfirmedCluster",
    "Editor",
    "Publication",
    "PublicationComment",
    "cluster_confirmed",
    "fallback_comment",
    "order_clusters",
    "render_review_body",
    "split_for_publication",
    "validate_comments",
]
