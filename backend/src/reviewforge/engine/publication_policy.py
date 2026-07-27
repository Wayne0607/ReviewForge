"""Publication Policy — model-agnostic ranking and pruning for confirmed findings.

Stage 1 of the model-agnostic publication policy. Pure-logic, no LLM, no
trust in the model-self-reported ``Finding.confidence``:

* ``pre_filter`` runs *before* the expensive Publication Gate. It deduplicates
  cross-reviewer root causes, drops confirmed findings whose anchor is not a
  visible RIGHT-side line, and suppresses generic test / performance /
  documentation / style advice that has not earned a specific anchor. This
  reduces how many candidates the gate's tool loop must inspect.
* ``post_finalize`` runs *after* the Publication Gate. It sorts surviving
  confirmed findings by deterministic score and caps the final set to
  ``max_comments`` plus a bounded ``high_risk_overflow`` reserved for
  detector-backed error findings.  Findings already at ``status="reported"``
  pass through unchanged so a retry never silently drops a published comment.

Modes:

* ``off``     — passthrough. Returns confirmed findings unchanged.
* ``shadow``  — emits events; never mutates the state store. Production uses
                this during replay validation before enforce is enabled.
* ``enforce`` — marks dropped findings as ``false_positive`` with
                ``verified_by='publication-policy'`` and a specific reason.

Both detector-backed and reviewer findings are scored through one pipeline.
Deterministic provenance (``verified_by in {'detector', 'detector-auto'}``)
is preferred over self-reported confidence; root-cause dedup resolves a
concrete sink and exact RIGHT-side diff line from ``finding.message``. Broad
sink families and suggestions are never enough to merge findings, so distinct
calls in the same file remain independently publishable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.detectors.unified_diff import iter_added_lines, iter_right_lines
from reviewforge.engine.verifier import (
    _DOTTED_IDENTIFIER,
    _SINK_FAMILY_PATTERNS,
    _SINK_PATTERNS,
)

_VALID_MODES = frozenset({"off", "shadow", "enforce"})

# Reuse the verifier's detector provenance vocabulary so the policy agrees
# with the rest of the pipeline on what counts as deterministic evidence.
_DETECTOR_PROVENANCE = frozenset({"detector", "detector-auto"})

# Tolerance (lines) under which two findings can be considered the same root
# cause.  Aligns with the Verifier's ``_NEARBY_LINE_TOLERANCE`` (= 3) for
# exact matches and ``_ROOT_CAUSE_LINE_TOLERANCE`` (= 20) for LLM root-cause
# grouping.  The publication policy uses a focused window (10) so reviewers
# describing distinct sinks in different methods of the same file survive.
_ROOT_CAUSE_LINE_TOLERANCE = 10

# Generic advice category buckets — these suggestions are rarely anchored to a
# concrete sink and almost never survive a deterministic right-side check.
_GENERIC_TEST_CATEGORIES = frozenset(
    {
        "incomplete-coverage",
        "missing-integration-test",
        "missing-test",
        "missing-tests",
        "missing-test-coverage",
        "missing-unit-test",
        "mock-validation",
        "test-coverage",
        "test-quality",
        "testing",
        "untested-code",
    }
)
_GENERIC_DOC_CATEGORIES = frozenset(
    {
        "documentation",
        "missing-api-documentation",
        "missing-doc",
        "missing-docs",
        "missing-docstring",
        "missing-documentation",
        "missing-parameter-doc",
        "safety-doc",
    }
)
_GENERIC_PERFORMANCE_CATEGORIES = frozenset(
    {
        "efficiency",
        "micro-optimization",
        "optimization",
        "performance",
        "unnecessary-computation",
        "unnecessary-linear-count",
    }
)
_GENERIC_STYLE_CATEGORIES = frozenset(
    {
        "code-style",
        "convention",
        "idiom",
        "imports",
        "naming",
        "optional-misuse",
        "readability",
        "style",
    }
)
_GENERIC_CATEGORIES = (
    _GENERIC_TEST_CATEGORIES | _GENERIC_DOC_CATEGORIES | _GENERIC_PERFORMANCE_CATEGORIES | _GENERIC_STYLE_CATEGORIES
)


# Heuristic: a "fix language" pattern covering concrete actionable replacements
# rather than the absence-only boilerplate filtered by the actionability gate.
_FIX_LANGUAGE = re.compile(
    r"\b(?:use\s+prepared|switch\s+to\s+|consider\s+|replace\s+\w+\s+with\s+|"
    r"add\s+(?:a\s+|the\s+)?(?:check|guard|validation|test|assertion|"
    r"try|catch|finally|default|fallback)|apply\s+|"
    r"saniti[sz]e|escape|encode|parameterize|prepared|"
    r"set\s+\w+\s+to\s+|use\s+\w+\s*=)"
    r"|"
    r"将.{0,20}改为|改用|替换为|补充|加上|增加|确保|参数化|转义|校验",
    re.IGNORECASE,
)

# Failure-mechanism tokens that mark a finding as carrying concrete, observable
# behaviour.  A generic testing/perf/style category is only suppressed when
# none of these tokens (and no concrete sink) appear in the message.
_FAILURE_MECHANISM = re.compile(
    r"\b(?:assert(?:ion|s)?|expect(?:ed|ation|s)?|verifyRaises|throws?|threw|"
    r"raise[sd]?|raises\s+\w+|exception|error|err\b|errcode|errno|"
    r"traceback|stacktrace|stack\s+trace|race\s+condition|race|deadlock|"
    r"timeout|timed[\s-]?out|hang[s]?|crash(?:es|ed)?|panic|"
    r"null\s*pointer|nullptr|nil\s+pointer|segfault|core\s+dump|"
    r"off[\s-]by[\s-]one|regression|regressed|broken|bug|defect|flaky|"
    r"fails?\s+(?:on|when)|test\s+(?:fails?|failure)|"
    r"\d+%|coverage\s*[:=]?\s*\d+%|"
    r"trace|signal\s+SIG|abort)"
    r"|"
    r"断言|异常|抛错|报错|失败|超时|死锁|竞态|崩溃|段错误|空指针|回归|缺陷|栈跟踪|堆栈",
    re.IGNORECASE,
)


# ── Public dataclasses ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class PublicationPolicyConfig:
    """Tunables for the publication policy.

    ``enabled=False`` (the library default) keeps the policy inactive even if
    ``mode`` is set explicitly.  This lets YAML carry a forward-looking
    configuration that does nothing until operations turn the bit on.
    """

    enabled: bool = False
    mode: str = "off"
    budget_enabled: bool = True
    max_comments: int = 4
    high_risk_overflow: int = 1

    def normalized_mode(self) -> str:
        candidate = (self.mode or "").strip().lower()
        return candidate if candidate in _VALID_MODES else "off"

    def normalized_max_comments(self) -> int:
        try:
            value = int(self.max_comments)
        except (TypeError, ValueError):
            return 4
        return max(1, value)

    def normalized_high_risk_overflow(self) -> int:
        try:
            value = int(self.high_risk_overflow)
        except (TypeError, ValueError):
            return 1
        return max(0, value)


@dataclass
class ScoredFinding:
    """One scoring result. The orchestrator can serialize it via ``to_dict``."""

    finding: Finding
    score: float
    reasons: tuple[str, ...]
    is_detector: bool
    right_visible: bool
    on_added_line: bool
    has_concrete_sink: bool
    has_actionable_fix: bool
    is_generic_advice: bool
    invalid_coordinate: bool
    high_risk: bool
    sink_sites: frozenset[tuple[int, str]]
    abstained: bool = False
    drop_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding.id,
            "file": self.finding.file,
            "line": self.finding.line,
            "score": round(self.score, 3),
            "is_detector": self.is_detector,
            "right_visible": self.right_visible,
            "on_added_line": self.on_added_line,
            "has_concrete_sink": self.has_concrete_sink,
            "has_actionable_fix": self.has_actionable_fix,
            "is_generic_advice": self.is_generic_advice,
            "invalid_coordinate": self.invalid_coordinate,
            "abstained": self.abstained,
            "high_risk": self.high_risk,
            "drop_reason": self.drop_reason,
            "reasons": list(self.reasons),
        }


@dataclass
class PolicyDecision:
    """Outcome of one policy pass over a confirmed finding list."""

    kept: list[Finding]
    dropped: list[Finding]
    scored: list[ScoredFinding]
    metrics: dict[str, int] = field(default_factory=dict)

    @property
    def dropped_ids(self) -> list[str]:
        return [finding.id for finding in self.dropped]

    def to_dict(self) -> dict[str, Any]:
        return {
            "kept_count": len(self.kept),
            "dropped_count": len(self.dropped),
            "metrics": dict(self.metrics),
            "scored": [s.to_dict() for s in self.scored],
        }


# ── Helpers ─────────────────────────────────────────────────────────────────


def _is_detector(finding: Finding) -> bool:
    return (finding.verified_by or "").strip().lower() in _DETECTOR_PROVENANCE


def _sink_tokens(message: str) -> set[str]:
    """Sink fingerprint from a finding message only (suggestion is excluded).

    Suggestions commonly share a recommended API across distinct problems
    (e.g. ``subprocess.run with argv list`` for any command-exec sink); using
    them would falsely merge two unrelated root causes.  Only the original
    finding message participates in the fingerprint.
    """
    text = str(message or "")
    tokens: set[str] = set()
    for name, pattern in _SINK_PATTERNS:
        if pattern.search(text):
            tokens.add(name)
    for name, pattern in _SINK_FAMILY_PATTERNS:
        if pattern.search(text):
            tokens.add(name)
    for match in _DOTTED_IDENTIFIER.finditer(text):
        cleaned = re.sub(r"\s+", "", match.group(0)).lower()
        if cleaned:
            tokens.add(cleaned)
    return tokens


def _concrete_sink_tokens(text: str) -> set[str]:
    """Return specific sink identifiers, excluding broad sink families.

    Family labels such as ``family:command-exec`` are useful for scoring but
    cannot identify a call site: ``os.system`` and ``subprocess.run`` both
    belong to that family. Dotted identifiers are accepted only when the
    verifier's sink patterns recognize the identifier itself, avoiding an
    unrelated name such as ``request.user`` becoming a dedup key.
    """
    value = str(text or "")
    tokens = {name for name, pattern in _SINK_PATTERNS if pattern.search(value)}
    sink_patterns = _SINK_PATTERNS + _SINK_FAMILY_PATTERNS
    for match in _DOTTED_IDENTIFIER.finditer(value):
        identifier = re.sub(r"\s+", "", match.group(0)).lower()
        if identifier and any(pattern.search(identifier) for _, pattern in sink_patterns):
            tokens.add(identifier)

    # The verifier's named ``subprocess`` pattern intentionally groups several
    # APIs. Once the exact dotted call is available, retaining that broad token
    # would incorrectly equate subprocess.run with subprocess.Popen.
    if any(token.startswith("subprocess.") for token in tokens):
        tokens.discard("subprocess")
    return tokens


def _sink_sites(finding: Finding, state: StateStore) -> frozenset[tuple[int, str]]:
    """Resolve an unambiguous ``(diff line, concrete sink)`` call-site identity.

    The exact anchored RIGHT-side line is preferred. If the anchor describes a
    nearby setup/flow line rather than the call itself, the message's concrete
    sink may resolve to one nearby visible line. Zero or multiple matches mean
    the evidence is ambiguous, so dedup abstains instead of merging.
    """
    file_diffs = getattr(state, "file_diffs", None) or {}
    patch = file_diffs.get(finding.file)
    if not patch:
        return frozenset()

    right_lines = dict(iter_right_lines(patch))
    if finding.line not in right_lines:
        return frozenset()

    message_tokens = _concrete_sink_tokens(finding.message)
    anchor_tokens = _concrete_sink_tokens(right_lines[finding.line])
    if anchor_tokens:
        resolved = anchor_tokens & message_tokens if message_tokens else anchor_tokens
        if len(resolved) == 1:
            return frozenset((finding.line, token) for token in resolved)
        return frozenset()

    if not message_tokens:
        return frozenset()

    candidates: set[tuple[int, str]] = set()
    for line, content in right_lines.items():
        if abs(line - finding.line) > _ROOT_CAUSE_LINE_TOLERANCE:
            continue
        for token in _concrete_sink_tokens(content) & message_tokens:
            candidates.add((line, token))
    return frozenset(candidates) if len(candidates) == 1 else frozenset()


def _has_concrete_sink(finding: Finding) -> bool:
    return bool(_sink_tokens(finding.message))


def _has_actionable_fix(finding: Finding) -> bool:
    text = (finding.message or "") + "\n" + (finding.suggestion or "")
    return bool(_FIX_LANGUAGE.search(text))


def _right_visibility(finding: Finding, state: StateStore) -> tuple[bool, bool, bool]:
    """``(right_visible, on_added_line, abstained)`` for the finding's anchor.

    A GitHub inline comment requires a RIGHT-side line that is visible inside
    a syntactically valid hunk.  ``iter_right_lines`` already discards
    unanchored or truncated patches, so a missing result means the coordinate
    is unsafe.

    Abstention rule: when ``file_diffs`` carries no patch for the finding's
    file (empty diff, file outside the PR, or unparseable patch), we cannot
    decide the anchor's validity.  We must not flag the coordinate as
    invalid — that would silently drop findings whose anchors are valid but
    unavailable to the policy.  ``abstained=True`` signals "no opinion".
    """
    if finding.line <= 0 or not finding.file:
        return False, False, False
    file_diffs = getattr(state, "file_diffs", None) or {}
    patch = file_diffs.get(finding.file)
    if not patch:
        # No patch available — abstain rather than declare invalid.
        return True, True, True
    right_visible = finding.line in {line for line, _ in iter_right_lines(patch)}
    on_added_line = finding.line in {line for line, _ in iter_added_lines(patch)}
    return right_visible, on_added_line, False


def _is_generic_advice(finding: Finding) -> bool:
    """Generic test/perf/docs/style advice that rarely carries a concrete sink.

    A finding is treated as generic advice only when *all* of the following
    hold:

    * its category belongs to a generic bucket (testing, docs, performance,
      style, etc.);
    * its message does not name a concrete sink (``os.system``,
      ``subprocess.run``, ``yaml.load``, ``pickle.loads``, …); and
    * its message does not describe a specific failure mechanism
      (assertion, exception, race, crash, error return, timeout, regression
      trace, etc.).

    A finding in a generic category that *does* cite a sink or a failure
    mechanism — for example "the unit test for ``process_user`` fails with
    ``AssertionError``" or "this branch races on ``cache.update``" — is a
    concrete defect and is preserved.
    """
    category = (finding.category or "").strip().lower()
    if category not in _GENERIC_CATEGORIES:
        return False
    if _has_concrete_sink(finding):
        return False
    if _FAILURE_MECHANISM.search(finding.message or ""):
        return False
    return True


# ── PublicationPolicy ──────────────────────────────────────────────────────


class PublicationPolicy:
    """Deterministic ranking and pruning, model-agnostic.

    The policy never reads ``Finding.confidence`` as the primary sort key.
    Score components are explicit, weighted, and driven by evidence that an
    embedding application or a different LLM cannot easily invent.
    """

    _ROOT_CAUSE_LINE_TOLERANCE = _ROOT_CAUSE_LINE_TOLERANCE

    # Weights for the score components. Chosen so that
    #   detector + right-visible + concrete-sink ≈ 36
    #   review error       + right-visible + actionable-fix ≈ 18
    # A generic-advice reviewer is reliably penalized below zero so it is
    # excluded from budget slots even when its self-reported confidence is high.
    _SCORE_DETECTOR = 25.0
    _SCORE_SEVERITY = {"error": 8.0, "warning": 3.0, "info": 0.0}
    _SCORE_RIGHT_VISIBLE = 5.0
    _SCORE_ADDED_LINE = 4.0
    _SCORE_CONCRETE_SINK = 6.0
    _SCORE_ACTIONABLE_FIX = 5.0
    _SCORE_GENERIC_PENALTY = -15.0

    def __init__(self, config: PublicationPolicyConfig | None = None) -> None:
        self._config = config or PublicationPolicyConfig()

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def mode(self) -> str:
        return self._config.normalized_mode()

    @property
    def enabled(self) -> bool:
        # ``enabled=False`` keeps the policy inert regardless of mode.  When
        # the flag is set, the mode is the single source of truth for behaviour.
        return bool(self._config.enabled) and self.mode != "off"

    def config(self) -> PublicationPolicyConfig:
        return self._config

    def pre_filter(
        self,
        confirmed: Iterable[Finding],
        state: StateStore,
    ) -> PolicyDecision:
        """Clean confirmed findings before the Publication Gate runs.

        Drops invalid RIGHT-side coordinates and generic advice, then merges
        cross-reviewer root causes that point at the same concrete sink.
        """

        confirmed_list = list(confirmed)
        if not confirmed_list:
            return PolicyDecision(kept=[], dropped=[], scored=[])

        if self.mode == "off":
            return PolicyDecision(
                kept=list(confirmed_list),
                dropped=[],
                scored=[],
                metrics={"input": len(confirmed_list), "kept": len(confirmed_list), "dropped": 0, "mode": "off"},
            )

        scored = [self._score(finding, state) for finding in confirmed_list]
        for scored_finding in scored:
            if scored_finding.invalid_coordinate:
                scored_finding.drop_reason = "invalid-coordinate"
            elif scored_finding.is_generic_advice:
                scored_finding.drop_reason = "generic-advice"

        pre_filtered = [s for s in scored if s.drop_reason is None]
        deduped = self._dedup_root_causes(pre_filtered)

        kept_scored = deduped
        kept = [s.finding for s in kept_scored]
        dropped = [s.finding for s in scored if s.finding.id not in {k.finding.id for k in kept_scored}]

        metrics = {
            "input": len(scored),
            "invalid_coordinate_dropped": sum(1 for s in scored if s.drop_reason == "invalid-coordinate"),
            "generic_advice_dropped": sum(1 for s in scored if s.drop_reason == "generic-advice"),
            "root_cause_merged_dropped": sum(1 for s in scored if s in pre_filtered and s not in deduped),
            "kept": len(kept),
            "dropped": len(dropped),
        }
        return PolicyDecision(kept=kept, dropped=dropped, scored=scored, metrics=metrics)

    def post_finalize(
        self,
        confirmed: Iterable[Finding],
        state: StateStore,
    ) -> PolicyDecision:
        """Sort + budget after the Publication Gate has run.

        Keeps the top ``max_comments`` by deterministic score plus up to
        ``high_risk_overflow`` detector-backed error findings that did not
        make the main cut.  Findings already at ``status="reported"`` are
        carried over unchanged and do not count against the budget.
        """

        confirmed_list = list(confirmed)
        reported = [finding for finding in confirmed_list if getattr(finding, "status", "") == "reported"]
        budget_pool = [finding for finding in confirmed_list if getattr(finding, "status", "") != "reported"]
        if not confirmed_list:
            return PolicyDecision(kept=[], dropped=[], scored=[])

        if self.mode == "off":
            return PolicyDecision(
                kept=list(confirmed_list),
                dropped=[],
                scored=[],
                metrics={
                    "input": len(confirmed_list),
                    "reported_carried": len(reported),
                    "kept": len(confirmed_list),
                    "dropped": 0,
                    "budget_exceeded_dropped": 0,
                    "overflow_used": 0,
                },
            )

        if not self._config.budget_enabled:
            return PolicyDecision(
                kept=list(confirmed_list),
                dropped=[],
                scored=[],
                metrics={
                    "input": len(confirmed_list),
                    "reported_carried": len(reported),
                    "kept": len(confirmed_list),
                    "dropped": 0,
                    "budget_enabled": 0,
                    "budget_exceeded_dropped": 0,
                    "overflow_used": 0,
                },
            )

        if not budget_pool:
            return PolicyDecision(
                kept=list(reported),
                dropped=[],
                scored=[],
                metrics={
                    "input": len(confirmed_list),
                    "reported_carried": len(reported),
                    "kept": len(reported),
                    "dropped": 0,
                    "budget_exceeded_dropped": 0,
                    "overflow_used": 0,
                },
            )

        scored = [self._score(finding, state) for finding in budget_pool]
        scored.sort(
            key=lambda s: (
                -s.score,
                0 if s.is_detector else 1,
                s.finding.line,
                s.finding.id,
            )
        )

        budget = self._config.normalized_max_comments()
        overflow = self._config.normalized_high_risk_overflow()

        main_kept = scored[:budget]
        overflow_used = 0
        for scored_finding in scored[budget:]:
            if overflow_used >= overflow:
                break
            if scored_finding.high_risk:
                main_kept.append(scored_finding)
                overflow_used += 1

        kept_ids = {s.finding.id for s in main_kept}
        kept = list(reported) + [s.finding for s in main_kept]
        dropped_scored = [s for s in scored if s.finding.id not in kept_ids]
        for s in dropped_scored:
            s.drop_reason = "budget-exceeded"
        dropped = [s.finding for s in dropped_scored]

        metrics = {
            "input": len(scored),
            "reported_carried": len(reported),
            "kept": len(kept),
            "dropped": len(dropped),
            "budget_exceeded_dropped": len(dropped_scored),
            "overflow_used": overflow_used,
            "overflow_capacity": overflow,
            "max_comments": budget,
        }
        return PolicyDecision(kept=kept, dropped=dropped, scored=scored, metrics=metrics)

    # ── Scoring ──────────────────────────────────────────────────────────

    def _score(self, finding: Finding, state: StateStore) -> ScoredFinding:
        is_detector = _is_detector(finding)
        right_visible, on_added_line, abstained = _right_visibility(finding, state)
        # Invalid coordinate requires *both* that a patch exists (we did not
        # abstain) *and* that the line is not visible on the RIGHT.
        invalid_coordinate = not abstained and (finding.line <= 0 or not right_visible)

        has_concrete_sink = _has_concrete_sink(finding)
        sink_sites = _sink_sites(finding, state)
        has_actionable_fix = _has_actionable_fix(finding)
        is_generic_advice = _is_generic_advice(finding)

        score = 0.0
        reasons: list[str] = []

        if is_detector:
            score += self._SCORE_DETECTOR
            reasons.append("detector-provenance")
        severity_bonus = self._SCORE_SEVERITY.get(finding.severity, 0.0)
        if severity_bonus:
            score += severity_bonus
            reasons.append(f"severity:{finding.severity}")
        if right_visible and not abstained:
            score += self._SCORE_RIGHT_VISIBLE
            reasons.append("right-visible")
        if on_added_line and not abstained:
            score += self._SCORE_ADDED_LINE
            reasons.append("added-line")
        if has_concrete_sink:
            score += self._SCORE_CONCRETE_SINK
            reasons.append("concrete-sink")
        if has_actionable_fix:
            score += self._SCORE_ACTIONABLE_FIX
            reasons.append("actionable-fix")
        if is_generic_advice:
            score += self._SCORE_GENERIC_PENALTY
            reasons.append("generic-advice-penalty")

        high_risk = is_detector and finding.severity == "error" and not invalid_coordinate and not abstained

        return ScoredFinding(
            finding=finding,
            score=score,
            reasons=tuple(reasons),
            is_detector=is_detector,
            right_visible=right_visible,
            on_added_line=on_added_line,
            has_concrete_sink=has_concrete_sink,
            has_actionable_fix=has_actionable_fix,
            is_generic_advice=is_generic_advice,
            invalid_coordinate=invalid_coordinate,
            high_risk=high_risk,
            sink_sites=sink_sites,
            abstained=abstained,
        )

    # ── Conservative root-cause dedup ────────────────────────────────────

    def _dedup_root_causes(self, scored: list[ScoredFinding]) -> list[ScoredFinding]:
        """Conservative root-cause merge.

        Two findings collapse only when:

        * Same ``file``;
        * Lines within ``_ROOT_CAUSE_LINE_TOLERANCE``; and
        * Both resolve to the same unambiguous concrete sink and RIGHT-side
          diff line.

        Resolution uses ``finding.message`` and the exact diff line, never the
        suggestion. Broad families such as command execution are insufficient,
        and ambiguous or multiple nearby call sites cause dedup to abstain.

        When two findings are merge-eligible we keep the higher-evidence one
        (deterministic provenance preferred, score as a tiebreaker).  When
        everything else ties we preserve **input order** — the first
        occurrence wins.  Random finding-id or file-path lexical order must
        never silently swap which finding survives, because that changes
        which reviewer is credited and which message is published.

        Findings in the same file but with disjoint sinks stay independent.
        """

        if len(scored) < 2:
            return list(scored)

        # Capture original input index so identical-score findings tiebreak
        # on the order the orchestrator saw them, never on lexical id.
        input_index = {id(s.finding): index for index, s in enumerate(scored)}

        survivors: list[ScoredFinding] = []
        for scored_finding in sorted(
            scored,
            key=lambda s: (
                -s.score,
                0 if s.is_detector else 1,
                s.finding.line,
                input_index[id(s.finding)],
            ),
        ):
            absorbed = False
            for kept in survivors:
                if not self._can_merge_root_causes(kept, scored_finding):
                    continue
                # ``kept`` wins on tie (it was inserted first with stronger
                # score/provenance, and ties preserve input order).
                scored_finding.drop_reason = "root-cause-merged"
                absorbed = True
                break
            if not absorbed:
                survivors.append(scored_finding)
        return survivors

    @classmethod
    def _can_merge_root_causes(cls, kept: ScoredFinding, candidate: ScoredFinding) -> bool:
        if kept.finding.file != candidate.finding.file:
            return False
        if abs(kept.finding.line - candidate.finding.line) > cls._ROOT_CAUSE_LINE_TOLERANCE:
            return False
        # Both findings must resolve to one concrete call site. Broad family
        # overlap (for example command execution) and suggestion text never
        # participate; an empty identity means resolution was ambiguous.
        if not (kept.sink_sites and candidate.sink_sites):
            return False
        return bool(kept.sink_sites & candidate.sink_sites)


# ── Helpers used by the orchestrator to format reasons ──────────────────────


def format_verify_reason(decision: PolicyDecision, dropped_finding: Finding) -> str:
    """Build a concise verify_reason string for the dropped finding."""
    target: ScoredFinding | None = None
    for scored_finding in decision.scored:
        if scored_finding.finding.id == dropped_finding.id:
            target = scored_finding
            break
    if target is None:
        return "publication-policy: dropped (no detailed score available)"

    components: list[str] = []
    if target.reasons:
        components.append(", ".join(target.reasons))
    if target.score or target.score == 0:
        components.append(f"score={target.score:.2f}")
    if target.drop_reason:
        components.append(f"reason={target.drop_reason}")
    return "publication-policy: " + "; ".join(components)[:500]
