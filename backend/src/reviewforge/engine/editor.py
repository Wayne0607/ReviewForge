"""Deterministic publication of independently verified v3 facts.

V3 selection requires a current ProofGate receipt; comments preserve the
accepted original facts and citations without another model call. V1/v2 keep
their original LLM comment writing and deterministic failure template (§4.7).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from reviewforge.core.json_output import extract_json_value
from reviewforge.engine.hypothesis import Hypothesis, HypothesisLedger, HypothesisStatus, Site
from reviewforge.engine.prompts_v4 import load_prompt
from reviewforge.engine.proof_gate import accepted, completed

logger = logging.getLogger(__name__)

_SEVERITY_RANK = {"info": 0, "warning": 1, "error": 2}
_STRENGTH_RANK = {"none": 0, "weak": 1, "strong": 2}


@dataclass
class ConfirmedCluster:
    """One (mechanism, scoped_anchor) group of confirmed hypotheses."""

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
    if hypothesis.change_anchor is not None:
        return json.dumps(["change", hypothesis.change_anchor.id], ensure_ascii=False, separators=(",", ":"))
    symbol = hypothesis.identity.rsplit("::", 1)[-1]
    # A missing symbol describes no shared cause. Resource/file units with an
    # empty or module anchor must remain independent; a bare function name is
    # meaningful only within its primary source file. Explicit multi-site
    # hypotheses keep their sites, and the editor can still merge proven causes.
    scope = (
        ["symbol", hypothesis.sites[0].path, symbol]
        if symbol and symbol != "<module>"
        else ["unit", hypothesis.unit_id]
    )
    return json.dumps(scope, ensure_ascii=False, separators=(",", ":"))


def _max_severity(a: str, b: str) -> str:
    return a if _SEVERITY_RANK.get(a, 0) >= _SEVERITY_RANK.get(b, 0) else b


def _max_strength(a: str, b: str) -> str:
    return a if _STRENGTH_RANK.get(a, 0) >= _STRENGTH_RANK.get(b, 0) else b


def cluster_confirmed(ledger: HypothesisLedger) -> list[ConfirmedCluster]:
    """Group CONFIRMED hypotheses by mechanism and source-scoped anchor."""

    groups: dict[tuple[str, str], ConfirmedCluster] = {}
    distinct_risk_scopes = {
        (item.mechanism.value, _anchor(item))
        for item in ledger.items.values()
        if ledger.protocol_version >= 2 and item.coverage_identity
    }
    for hypothesis in sorted(ledger.items.values(), key=lambda item: item.identity):
        if hypothesis.status != HypothesisStatus.CONFIRMED or (
            ledger.protocol_version >= 3 and not accepted(hypothesis, ledger)
        ):
            continue
        key = (hypothesis.mechanism.value, _anchor(hypothesis))
        if key in distinct_risk_scopes:
            # The old function cluster must not undo v2's separation of
            # distinct defect premises. Exact task aliases still share a key;
            # further merging requires the editor's evidence-supported choice.
            key = (
                key[0],
                json.dumps(
                    ["risk", key[1], hypothesis.investigation_task_id or hypothesis.identity],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
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

    # Structural findings have no changed source line. A summary preserves the
    # confirmed issue without fabricating an inline coordinate or hiding it.
    eligible = [cluster for cluster in ordered if cluster.sites and not _structural(cluster)]
    inline: list[ConfirmedCluster] = list(eligible[: max(0, max_inline)])
    for cluster in eligible[max(0, max_inline) : max(max(0, max_inline), max_inline_overflow)]:
        if cluster.severity == "error" and cluster.strength == "strong":
            inline.append(cluster)
    inline_keys = {cluster.key for cluster in inline}
    summary = [cluster for cluster in ordered if cluster.key not in inline_keys]
    return inline, summary


def _evidence_text(hypothesis: Hypothesis) -> str:
    lines = []
    if hypothesis.assessment is not None:
        lines.append("Contract assessment:\n" + json.dumps(hypothesis.assessment.to_dict(), ensure_ascii=False))
    for observation in hypothesis.observations:
        if observation.status != "success":
            continue
        location = observation.path or observation.tool
        source_range = observation.excerpt_line_range or observation.line_range
        if source_range and source_range[0]:
            location += f":{source_range[0]}"
        lines.append(f"{observation.id} {location} (sha={observation.sha}):\n{observation.excerpt}")
    return "\n".join(lines)


def fallback_comment(cluster: ConfirmedCluster, *, output_language: str = "en") -> PublicationComment:
    """Deterministic template used when the editor LLM fails (§4.7 failure)."""

    site = cluster.sites[0]
    where = "\n".join(f"- {site.path}:{site.line}" for site in cluster.sites)
    hypothesis = cluster.hypotheses[0]
    labels = ("问题", "依据", "位置", "修复") if output_language == "zh-CN" else ("Issue", "Why", "Where", "Fix")
    why = "\n".join(
        filter(None, [hypothesis.trigger, hypothesis.impact, *[_evidence_text(h) for h in cluster.hypotheses]])
    )
    body = "\n\n".join(
        [
            f"{labels[0]}: {hypothesis.claim}",
            f"{labels[1]}: {why}",
            f"{labels[2]}:\n{where}",
            f"{labels[3]}: {_fix_suggestion(cluster, output_language)}",
        ]
    )
    return PublicationComment(
        hypothesis_ids=list(cluster.hypothesis_ids),
        path=site.path,
        line=site.line,
        title=cluster.hypotheses[0].claim[:60],
        body=body,
    )


def proof_comment(cluster: ConfirmedCluster, *, output_language: str = "en") -> PublicationComment:
    """Publish immutable reviewed facts; no second model may invent a new defect."""
    english = output_language != "zh-CN"
    parts = []
    for item in cluster.hypotheses:
        assessment = item.assessment
        if assessment is None:
            raise ValueError("publication requires an independently verified assessment")
        parts.extend(
            [
                item.claim,
                f"{'When' if english else '触发条件'}: {item.trigger}",
                f"{'Impact' if english else '影响'}: {item.impact}",
                f"{'Required' if english else '要求'}: {assessment.expected}",
                f"{'Observed' if english else '实际'}: {assessment.actual}",
            ]
        )
        by_id = {observation.id: observation for observation in item.observations}
        seen = set()
        for citation in [*assessment.expected_evidence, *assessment.actual_evidence]:
            key = (citation.observation_id, citation.quote)
            if key in seen:
                continue
            seen.add(key)
            observation = by_id[citation.observation_id]
            span = observation.excerpt_line_range or observation.line_range
            location = observation.path or observation.tool
            if span:
                location += f":{span[0]}"
            parts.append(f"{location}:\n" + "\n".join("> " + line for line in citation.quote.splitlines()))
    parts.append("\n".join(f"- {site.path}:{site.line}" for site in cluster.sites))
    return PublicationComment(
        hypothesis_ids=list(cluster.hypothesis_ids),
        path=cluster.sites[0].path,
        line=cluster.sites[0].line,
        title=cluster.hypotheses[0].claim[:60],
        body="\n\n".join(parts),
    )


def fallback_summary(cluster: ConfirmedCluster) -> tuple[str, str]:
    if _structural(cluster):
        return cluster.hypothesis_ids[0], _structural_summary(cluster)
    where = ", ".join(f"{site.path}:{site.line}" for site in cluster.sites)
    return cluster.hypothesis_ids[0], f"{cluster.hypotheses[0].claim} ({where})"


def _structural(cluster: ConfirmedCluster) -> bool:
    return any(item.change_anchor is not None for item in cluster.hypotheses)


def _structural_summary(cluster: ConfirmedCluster) -> str:
    """Keep grounded structural proof even if the editor omits/rewrites it."""
    parts = []
    for item in cluster.hypotheses:
        change = item.change_anchor
        if change is None:
            continue
        before = (
            f"https://github.com/{change.repo}/blob/{change.comparison_base_sha}/"
            f"{quote(change.previous_path, safe='/')}"
        )
        after = f"https://github.com/{change.head_repo}/blob/{change.head_sha}/{quote(change.path, safe='/')}"
        parts.append(
            f"{item.claim}\n\n{item.trigger}\n\n{item.impact}\n\n"
            f"[Before]({before}) → [After]({after}); `{change.previous_path}` → `{change.path}`."
        )
        if item.assessment is None:
            continue
        by_id = {observation.id: observation for observation in item.observations}
        parts.append(f"Expected: {item.assessment.expected}\n\nActual: {item.assessment.actual}")
        seen = set()
        for citation in [*item.assessment.expected_evidence, *item.assessment.actual_evidence]:
            observation = by_id.get(citation.observation_id)
            key = (citation.observation_id, citation.quote)
            if (
                key in seen
                or observation is None
                or observation.status != "success"
                or citation.quote not in observation.excerpt
            ):
                continue
            seen.add(key)
            label = observation.path or observation.tool
            if observation.path and observation.tool != "read_change":
                url = (
                    f"https://github.com/{change.head_repo}/blob/{observation.sha}/{quote(observation.path, safe='/')}"
                )
                if observation.excerpt_line_range:
                    url += f"#L{observation.excerpt_line_range[0]}"
                label = f"[{label}]({url})"
            parts.append(label + ":\n" + "\n".join("> " + line for line in citation.quote.splitlines()))
    return "\n\n".join(parts)


def confirmed_fact_digest(hypothesis: Hypothesis) -> str:
    """Track changes to published facts, including new sites of the same issue."""
    facts = {
        "claim": hypothesis.claim,
        "trigger": hypothesis.trigger,
        "impact": hypothesis.impact,
        "severity": hypothesis.severity,
        "strength": hypothesis.evidence_strength,
        "sites": sorted((site.path, site.line, site.excerpt) for site in hypothesis.sites),
    }
    if hypothesis.change_anchor is not None:
        facts["change_anchor"] = hypothesis.change_anchor.to_dict()
    if hypothesis.assessment is not None:
        facts["assessment"] = hypothesis.assessment.to_dict()
    return hashlib.sha256(json.dumps(facts, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def resumed_publication(
    ledger: HypothesisLedger,
    *,
    covered_ids: set[str],
    published_clusters: set[tuple[str, str]],
    inline_used: int,
    max_inline: int,
    max_inline_overflow: int,
    output_language: str,
) -> Publication:
    """Express only newly confirmed facts without repeating the editor LLM call.

    Already published clusters receive a summary supplement for new sites, not
    another inline comment. Both inline limits apply across the entire run.
    """
    pending = replace(ledger, items={key: item for key, item in ledger.items.items() if item.id not in covered_ids})
    ordered = order_clusters(cluster_confirmed(pending))
    fresh = [cluster for cluster in ordered if cluster.key not in published_clusters]
    inline, _ = split_for_publication(
        fresh, max(0, max_inline - inline_used), max(0, max_inline_overflow - inline_used)
    )
    inline_keys = {cluster.key for cluster in inline}
    return Publication(
        comments=[
            (proof_comment if ledger.protocol_version >= 3 else fallback_comment)(
                cluster, output_language=output_language
            )
            for cluster in inline
        ],
        summary_items=[fallback_summary(cluster) for cluster in ordered if cluster.key not in inline_keys],
        merged=[cluster.hypothesis_ids for cluster in inline if len(cluster.hypothesis_ids) > 1],
        unknown_ids=(
            [item.id for item in ledger.items.values() if item.status == HypothesisStatus.UNKNOWN]
            if ledger.protocol_version >= 3
            else []
        ),
        fallback=bool(ordered) and ledger.protocol_version < 3,
    )


def _fix_suggestion(cluster: ConfirmedCluster, language: str) -> str:
    trigger = cluster.hypotheses[0].trigger
    if language == "zh-CN":
        return f"处理触发条件：{trigger}" if trigger else "修复上述问题的根因。"
    return f"Address: {trigger}" if trigger else "Address the underlying mechanism above."


def _parse_publication(content: str) -> dict[str, Any] | None:
    parsed = extract_json_value(content or "", required_key="comments", allow_list=False)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("comments"), list):
        return None
    if not isinstance(parsed.get("summary_items", []), list):
        return None
    return parsed


def _site_index(ledger: HypothesisLedger) -> dict[str, set[tuple[str, int]]]:
    index: dict[str, set[tuple[str, int]]] = {}
    for item in ledger.items.values():
        if (
            item.status == HypothesisStatus.CONFIRMED
            and item.change_anchor is None
            and (ledger.protocol_version < 3 or accepted(item, ledger))
        ):
            index[item.id] = {(site.path, site.line) for site in item.sites}
    return index


def validate_comments(
    publication: Publication, ledger: HypothesisLedger, *, max_comments: int
) -> tuple[list[PublicationComment], list[str]]:
    """Require confirmed references, a valid anchor and every affected site."""

    sites = _site_index(ledger)
    valid: list[PublicationComment] = []
    rejected: list[str] = []
    for comment in publication.comments:
        if len(valid) >= max_comments:
            break
        referenced = set().union(*(sites.get(identity, set()) for identity in comment.hypothesis_ids))
        if (
            comment.hypothesis_ids
            and all(identity in sites for identity in comment.hypothesis_ids)
            and (comment.path, comment.line) in referenced
            and all(
                re.search(rf"(?<![\w/.-]){re.escape(path)}:{line}(?!\d)", comment.body) for path, line in referenced
            )
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
        inline_keys = {cluster.key for cluster in inline}
        for cluster in ordered:
            placement = "inline" if cluster.key in inline_keys else "summary"
            header = (
                f"- {cluster.hypothesis_ids} | {cluster.key} | "
                f"severity={cluster.severity} | strength={cluster.strength} | publication={placement}"
            )
            sites = ", ".join(f"{site.path}:{site.line}" for site in cluster.sites)
            details = [header, f"  sites: {sites}"]
            for hypothesis in cluster.hypotheses:
                details.extend(
                    [
                        f"  hypothesis_id: {hypothesis.id}",
                        f"  claim: {hypothesis.claim}",
                        f"  trigger: {hypothesis.trigger}",
                        f"  impact: {hypothesis.impact}",
                        f"  investigation: {hypothesis.verdict_reason}",
                        f"  observations:\n{_evidence_text(hypothesis)}",
                    ]
                )
                if hypothesis.change_anchor is not None:
                    details.append(
                        "  structural change (summary only): " + json.dumps(hypothesis.change_anchor.to_dict())
                    )
            confirmed_lines.append("\n".join(details))
        unknown_ids = [
            item.id
            for item in sorted(ledger.items.values(), key=lambda item: item.identity)
            if item.status == HypothesisStatus.UNKNOWN
        ]
        unknown_lines = [f"- {item.id}: {item.claim}" for item in ledger.items.values() if item.id in set(unknown_ids)]
        return "\n\n".join(
            [
                "## PR intent\n" + (getattr(pack, "pr_intent", "") or "（无）/(none)"),
                f"## Publication selection\nWrite comments only for publication=inline (at most {len(inline)}). "
                "Write summary_items only for publication=summary. Include all sites for merged comments.",
                "## Confirmed\n" + ("\n".join(confirmed_lines) or "（无）/(none)"),
                "## Unknown claims\n" + ("\n".join(unknown_lines) or "（无）/(none)"),
            ]
        )

    async def run(self, ledger: HypothesisLedger, pack: Any) -> Publication:
        """Cluster, select, and ask the editor LLM for comments.

        Falls back to the deterministic template when the LLM cannot produce a
        valid JSON object. V3 uses current independent proof receipts and never
        invokes a prose model or publishes an unverified fallback.
        """

        if ledger.protocol_version >= 3:
            return resumed_publication(
                ledger,
                covered_ids=set(),
                published_clusters=set(),
                inline_used=0,
                max_inline=self._max_inline,
                max_inline_overflow=self._max_inline_overflow,
                output_language=self._output_language,
            )
        ordered = order_clusters(cluster_confirmed(ledger))
        inline, summary = split_for_publication(ordered, self._max_inline, self._max_inline_overflow)
        unknown_ids = [
            item.id
            for item in sorted(ledger.items.values(), key=lambda item: item.identity)
            if item.status == HypothesisStatus.UNKNOWN
        ]

        if not inline:
            return Publication(
                comments=[], summary_items=self._summary_from({}, summary), merged=[], unknown_ids=unknown_ids
            )

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
            parsed = {}

        inline_ids = {identity for cluster in inline for identity in cluster.hypothesis_ids}
        comments = self._comments_from(parsed, inline_ids)
        valid, _rejected = validate_comments(
            Publication(comments=comments, summary_items=[], merged=[], unknown_ids=unknown_ids),
            ledger,
            max_comments=len(comments),
        )
        # Treat the deterministic cluster as the unit of coverage. A partial
        # model response cannot erase a cluster or split it into duplicate
        # comments, and a merge must cover each participating cluster in full.
        valid = [
            comment
            for comment in valid
            if all(
                not set(cluster.hypothesis_ids).intersection(comment.hypothesis_ids)
                or set(cluster.hypothesis_ids).issubset(comment.hypothesis_ids)
                for cluster in inline
            )
        ]
        selected: list[PublicationComment] = []
        covered: set[str] = set()
        fallback = False
        for cluster in inline:
            ids = set(cluster.hypothesis_ids)
            if ids <= covered:
                continue
            comment = next(
                (
                    item
                    for item in valid
                    if ids <= set(item.hypothesis_ids) and not covered.intersection(item.hypothesis_ids)
                ),
                None,
            )
            if comment is None:
                comment = fallback_comment(cluster, output_language=self._output_language)
                fallback = True
            selected.append(comment)
            covered.update(comment.hypothesis_ids)

        summary_items = self._summary_from(parsed, summary)
        return Publication(
            comments=selected,
            summary_items=summary_items,
            merged=[comment.hypothesis_ids for comment in selected if len(comment.hypothesis_ids) > 1],
            unknown_ids=unknown_ids,
            fallback=fallback,
        )

    def _comments_from(self, parsed: dict[str, Any], allowed_ids: set[str]) -> list[PublicationComment]:
        comments: list[PublicationComment] = []
        for raw in parsed.get("comments", []) or []:
            if not isinstance(raw, dict):
                continue
            ids = raw.get("hypothesis_ids")
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in allowed_ids for i in ids):
                continue
            ids = list(dict.fromkeys(ids))
            path = str(raw.get("path", "")).strip()
            line = raw.get("line")
            if not path or type(line) is not int or line <= 0:
                continue
            comments.append(
                PublicationComment(
                    hypothesis_ids=ids,
                    path=path,
                    line=line,
                    title=str(raw.get("title", ""))[:60],
                    body=str(raw.get("body", "")).strip(),
                    suggestion_patch=str(raw.get("suggestion_patch", "")).strip(),
                )
            )
        return comments

    def _summary_from(self, parsed: dict[str, Any], clusters: list[ConfirmedCluster]) -> list[tuple[str, str]]:
        allowed_ids = {identity for cluster in clusters for identity in cluster.hypothesis_ids}
        model_summary: dict[str, str] = {}
        for raw in parsed.get("summary_items", []) or []:
            if not isinstance(raw, dict):
                continue
            identity = str(raw.get("hypothesis_id", ""))
            one_line = str(raw.get("one_line") or "").strip()
            if identity in allowed_ids and one_line:
                model_summary.setdefault(identity, one_line)
        summary: list[tuple[str, str]] = []
        for cluster in clusters:
            if _structural(cluster):
                summary.append(fallback_summary(cluster))
                continue
            identity = cluster.hypothesis_ids[0]
            text = next((model_summary[i] for i in cluster.hypothesis_ids if i in model_summary), None)
            summary.append((identity, text or fallback_summary(cluster)[1]))
        return summary


def render_review_body(publication: Publication, ledger: HypothesisLedger, *, output_language: str = "auto") -> str:
    """Render the PR review body ``<details>`` from summary items + UNKNOWN claims."""

    english = output_language != "zh-CN"
    unverified = [
        item
        for item in ledger.items.values()
        if ledger.protocol_version >= 3 and item.status == HypothesisStatus.CONFIRMED and not accepted(item, ledger)
    ]
    if not publication.summary_items and not publication.unknown_ids and not unverified:
        return ""
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

    if unverified:
        label = "Not accepted by independent proof review" if english else "未通过独立证据核验"
        lines.append(f"\n**{label}**\n")
        rejected = sum(
            completed(item, ledger) and ledger.publication_reviews.get(item.id, {}).get("decision") == "refuted"
            for item in unverified
        )
        if english:
            lines.append(f"{rejected} claims contradicted; {len(unverified) - rejected} claims remain unverified.\n")
        else:
            lines.append(f"{rejected} 条疑点被证据反驳；{len(unverified) - rejected} 条疑点尚未完成核验。\n")

    lines.append("\n</details>\n")
    return "".join(lines)


__all__ = [
    "ConfirmedCluster",
    "Editor",
    "Publication",
    "PublicationComment",
    "cluster_confirmed",
    "confirmed_fact_digest",
    "fallback_comment",
    "order_clusters",
    "render_review_body",
    "resumed_publication",
    "split_for_publication",
    "validate_comments",
]
