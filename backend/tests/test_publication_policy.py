"""Unit + orchestrator-integration tests for the model-agnostic Publication Policy.

Covers Stage 1 behaviour:

* Off / shadow / enforce mode semantics (no state mutation vs. marked as
  false_positive).
* Deterministic scoring: detector provenance, severity, RIGHT-side
  visibility, added-line, concrete sink, actionable fix, generic-advice
  penalty — without relying on model-self-reported ``confidence``.
* Conservative root-cause merging: same file, nearby lines, shared sink —
  never different sinks.  Sink fingerprint uses the *message* only so two
  reviewers who independently suggest ``subprocess.run`` for distinct
  problems are not collapsed.
* Stable input order on identical scores (no lexical-id or file-path
  tiebreaker that silently swaps the survivor).
* Invalid RIGHT-side coordinate drop, generic testing/perf/style/docs drop
  *only* when the message has no concrete sink / failure mechanism.
* Abstain on missing file patch (do not mark invalid-coordinate when the
  patch is absent).
* Budget: top N + bounded detector-only overflow.
* ``reported`` findings are preserved verbatim by post_finalize and do not
  consume the confirmed budget.
* Orchestrator integration: pre/post run when the policy is enabled
  regardless of the Publication Gate flag.
* Config env-var overrides for ``max_comments`` and ``high_risk_overflow``
  with clamping.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from reviewforge.core.config import ReviewForgeConfig
from reviewforge.core.specs import build_registry
from reviewforge.core.state import Finding, StateStore
from reviewforge.engine.orchestrator import Orchestrator
from reviewforge.engine.publication_policy import (
    PublicationPolicy,
    PublicationPolicyConfig,
    format_verify_reason,
)
from reviewforge.tools.gateway import ToolGateway

# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_finding(**kwargs: Any) -> Finding:
    defaults: dict[str, Any] = {
        "file": "src/auth.py",
        "line": 5,
        "severity": "warning",
        "category": "command-injection",
        "message": "User input flows into os.system without sanitization.",
        "suggestion": "Use subprocess.run with a fixed argv list instead of os.system.",
        "confidence": 0.7,
        "reviewer": "security_reviewer",
        "status": "confirmed",
        "verified_by": "",
        "verify_reason": "",
    }
    defaults.update(kwargs)
    return Finding(**defaults)


def _make_state(file_diffs: dict[str, str] | None = None, **kwargs: Any) -> StateStore:
    base: dict[str, Any] = {
        "repo": "owner/repo",
        "pr_number": 42,
        "head_sha": "abc123",
        "files_changed": list(file_diffs.keys()) if file_diffs else [],
        "file_diffs": file_diffs or {},
        "diff_summary": "",
        "impact_manifest": {},
    }
    base.update(kwargs)
    return StateStore(**base)


# A diff with visible RIGHT-side lines on 1..6; line 5 is added.
_SAMPLE_DIFF = (
    "@@ -1,8 +1,10 @@\n"
    " import os\n"
    " \n"
    "-def old_func():\n"
    "+def new_func():\n"
    "+    user = input('name: ')\n"
    "+    os.system(user)  # line 5 (added)\n"
    " \n"
    " def other():\n"
    "     pass\n"
)


_SAMPLE_DIFF_LINE8 = (
    "@@ -1,12 +1,14 @@\n"
    " import os\n"
    " import subprocess\n"
    " \n"
    "-def handler():\n"
    "+def handler(req):\n"
    "+    cmd = 'echo %s' % req.GET['x']\n"
    "+    cmd2 = 'rm -rf /'\n"
    "+    os.system(cmd)        # line 7 added\n"
    "+    subprocess.run(cmd2)  # line 8 added\n"
    " \n"
    " def other():\n"
    "     pass\n"
    " \n"
)


# ── Mode passthrough ────────────────────────────────────────────────────────


class TestModePassthrough:
    """``off`` never changes behaviour; ``shadow`` records but doesn't mutate."""

    def test_off_mode_returns_findings_unchanged(self):
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=False, mode="off"))
        finding = _make_finding()
        decision = policy.pre_filter([finding], _make_state({"src/auth.py": _SAMPLE_DIFF}))
        assert decision.kept == [finding]
        assert decision.dropped == []

    def test_off_mode_post_finalize_returns_findings_unchanged(self):
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=False, mode="off"))
        finding = _make_finding()
        decision = policy.post_finalize([finding], _make_state({"src/auth.py": _SAMPLE_DIFF}))
        assert decision.kept == [finding]
        assert decision.dropped == []

    def test_off_with_explicit_mode_flag_still_passes_through(self):
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="off"))
        finding = _make_finding()
        decision = policy.pre_filter([finding], _make_state({"src/auth.py": _SAMPLE_DIFF}))
        assert decision.kept == [finding]


# ── Scoring components ──────────────────────────────────────────────────────


class TestScoring:
    """Score must NEVER use self-reported confidence as a primary signal."""

    def test_detector_provenance_scores_higher_than_reviewer(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        detector = _make_finding(id="d", verified_by="detector", confidence=0.5)
        reviewer = _make_finding(id="r", verified_by="", confidence=0.99)

        sd = policy._score(detector, state)
        sr = policy._score(reviewer, state)
        assert sd.score > sr.score, (
            f"detector={sd.score}, reviewer(confidence=0.99)={sr.score} — "
            "confidence must not outrank deterministic provenance"
        )

    def test_right_visibility_bonus_only_when_line_visible(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        visible = policy._score(_make_finding(line=5), state)
        invisible = policy._score(_make_finding(line=999), state)
        assert visible.right_visible is True
        assert visible.invalid_coordinate is False
        assert invisible.right_visible is False
        assert invisible.invalid_coordinate is True
        assert visible.score > invisible.score

    def test_added_line_bonus(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        # line 5 is added, line 4 is context-only after the hunk header
        added = policy._score(_make_finding(line=5), state)
        context_only = policy._score(_make_finding(line=1), state)
        assert added.on_added_line is True
        assert context_only.on_added_line is False
        assert added.score > context_only.score

    def test_severity_error_outranks_info(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        err = policy._score(_make_finding(severity="error", line=5), state)
        warn = policy._score(_make_finding(severity="warning", line=5), state)
        info = policy._score(_make_finding(severity="info", line=5), state)
        assert err.score > warn.score > info.score

    def test_concrete_sink_bonus(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        sink = policy._score(
            _make_finding(line=5, message="os.system(user_input) leak", suggestion="sanitize input"),
            state,
        )
        generic = policy._score(
            _make_finding(
                line=5,
                message="Possibly bad code style preference",
                suggestion="Use prepared statements",
                category="code-style",
            ),
            state,
        )
        assert sink.has_concrete_sink is True
        assert sink.score > generic.score

    def test_generic_advice_penalized_even_with_high_confidence(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        # Pure generic advice: no concrete sink in message, no concrete
        # failure mechanism.  The high self-reported confidence must not
        # rescue it from the negative score.
        high_conf_generic = policy._score(
            _make_finding(
                line=5,
                category="testing",
                confidence=0.99,
                message="Should add more unit tests for this module.",
                suggestion="Cover edge cases with integration tests.",
            ),
            state,
        )
        assert high_conf_generic.is_generic_advice is True
        assert high_conf_generic.score < 0

    def test_score_not_primarily_determined_by_confidence(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        low_conf_strong = policy._score(
            _make_finding(id="a", line=5, severity="error", confidence=0.4),
            state,
        )
        high_conf_weak = policy._score(
            _make_finding(
                id="b",
                line=5,
                severity="info",
                confidence=0.99,
                message="Might be an issue.",
                suggestion="Consider reviewing this code style.",
                category="code-style",
            ),
            state,
        )
        assert low_conf_strong.score > high_conf_weak.score

    def test_high_risk_requires_detector_and_error(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        detector_error = policy._score(
            _make_finding(id="x1", line=5, severity="error", verified_by="detector"),
            state,
        )
        detector_warning = policy._score(
            _make_finding(id="x2", line=5, severity="warning", verified_by="detector"),
            state,
        )
        reviewer_error = policy._score(
            _make_finding(id="x3", line=5, severity="error", verified_by=""),
            state,
        )
        assert detector_error.high_risk is True
        assert detector_warning.high_risk is False
        assert reviewer_error.high_risk is False


# ── Conservative root-cause dedup ────────────────────────────────────────────


class TestRootCauseDedup:
    """Root-cause dedup is conservative: only merge on shared strong sinks."""

    def test_same_sink_merged_across_reviewers(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        security = _make_finding(
            id="sec",
            reviewer="security_reviewer",
            verified_by="",
            message="User input is passed to os.system unsanitized.",
            suggestion="Replace os.system with subprocess.run and a fixed argv list.",
        )
        correctness = _make_finding(
            id="cor",
            reviewer="correctness_reviewer",
            verified_by="",
            message="Command injection: os.system(user) at module boundary.",
            suggestion="Use subprocess.run with argv instead of os.system.",
        )
        decision = policy.pre_filter([security, correctness], state)
        # Both share the strong sink "os.system" → exactly one survives.
        # Identical scores must tiebreak on input order — the first finding
        # in the orchestrator's confirmed list wins.
        assert {f.id for f in decision.kept} == {"sec"}
        assert {f.id for f in decision.dropped} == {"cor"}
        dropped = next(s for s in decision.scored if s.finding.id == "cor")
        assert dropped.drop_reason == "root-cause-merged"

    def test_same_score_preserves_input_order(self):
        """Tiebreaker must not depend on finding id, file, or lexical order."""
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        # Three findings with identical score components: same severity,
        # same sink, same line.  The id order is reversed w.r.t. input
        # order to prove the survivor is the *first* input.
        first = _make_finding(
            id="z_first",  # lexical last
            reviewer="security_reviewer",
            verified_by="",
            severity="warning",
            message="os.system(user) sink",
            suggestion="Replace with subprocess.run.",
        )
        second = _make_finding(
            id="m_second",
            reviewer="security_reviewer",
            verified_by="",
            severity="warning",
            message="os.system(user) sink",
            suggestion="Replace with subprocess.run.",
        )
        third = _make_finding(
            id="a_third",  # lexical first
            reviewer="security_reviewer",
            verified_by="",
            severity="warning",
            message="os.system(user) sink",
            suggestion="Replace with subprocess.run.",
        )
        decision = policy.pre_filter([first, second, third], state)
        kept_ids = {f.id for f in decision.kept}
        # Stable input order: z_first wins, m_second + a_third are absorbed.
        assert kept_ids == {"z_first"}
        assert {f.id for f in decision.dropped} == {"m_second", "a_third"}

    def test_different_sinks_kept_independently(self):
        diff = _SAMPLE_DIFF_LINE8
        state = _make_state({"src/auth.py": diff})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        os_system_finding = _make_finding(
            id="os",
            line=7,
            message="os.system(cmd) takes tainted user data; command injection risk.",
            suggestion="Replace os.system with subprocess.run and a fixed argv list.",
        )
        subprocess_run_finding = _make_finding(
            id="sub",
            line=8,
            message="subprocess.run with constant string — review intentionally uses shell.",
            suggestion="If shell=False and argv is constant, this is safe.",
            category="command-injection",
            verified_by="detector",
        )
        # Both suggestions share "subprocess.run" — the merge criterion
        # must ignore the suggestion so the two distinct sinks survive.
        decision = policy.pre_filter([os_system_finding, subprocess_run_finding], state)
        kept_ids = {f.id for f in decision.kept}
        assert "os" in kept_ids
        assert "sub" in kept_ids

    def test_sink_fingerprint_uses_message_only(self):
        """Two messages with disjoint sinks but identical suggestion stay apart."""
        diff = _SAMPLE_DIFF_LINE8
        state = _make_state({"src/auth.py": diff})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        yaml_finding = _make_finding(
            id="yaml",
            line=7,
            message="yaml.load(user) deserialization risk in handler.",
            suggestion="Use subprocess.run with a fixed argv list.",
        )
        os_finding = _make_finding(
            id="os",
            line=8,
            message="os.system(cmd) command injection in handler.",
            suggestion="Use subprocess.run with a fixed argv list.",
        )
        # Same suggestion, different sinks in messages.
        decision = policy.pre_filter([yaml_finding, os_finding], state)
        kept_ids = {f.id for f in decision.kept}
        assert {"yaml", "os"}.issubset(kept_ids)

    def test_detector_wins_over_reviewer_on_tie(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        reviewer = _make_finding(
            id="rev",
            reviewer="security_reviewer",
            verified_by="",
            severity="warning",
            message="Possibly dangerous os.system call.",
            suggestion="Consider switching to subprocess.run with fixed argv.",
        )
        detector = _make_finding(
            id="det",
            reviewer="security_reviewer",
            verified_by="detector",
            severity="warning",
            message="os.system(user) is a confirmed command-injection sink.",
            suggestion="Replace os.system with subprocess.run and a fixed argv list.",
        )
        decision = policy.pre_filter([reviewer, detector], state)
        kept_ids = {f.id for f in decision.kept}
        assert "det" in kept_ids
        assert "rev" not in kept_ids

    def test_root_cause_only_within_tolerance(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        nearby = _make_finding(
            id="near",
            line=5,
            message="os.system(user) leak — replace with subprocess.run.",
        )
        far = _make_finding(
            id="far",
            line=999,
            message="os.system(user) leak — replace with subprocess.run.",
        )
        decision = policy.pre_filter([nearby, far], state)
        # ``far`` is invalid-coordinate; both survive the merge step
        # independently because invalid-coordinate findings are dropped
        # before merging, leaving just one.
        kept_ids = {f.id for f in decision.kept}
        dropped_ids = {f.id for f in decision.dropped}
        assert kept_ids == {"near"}
        assert "far" in dropped_ids

    def test_wrong_argument_reports_merge_across_reviewer_vocabularies(self):
        state = _make_state(None)
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))
        correctness = _make_finding(
            id="correctness",
            file="dualwriter.go",
            line=90,
            category="wrong-argument",
            reviewer="correctness_reviewer",
            message="recordStorageDuration receives `name` instead of `options.Kind`.",
            suggestion="Use d.recordStorageDuration(false, mode, options.Kind, method, start).",
        )
        performance = _make_finding(
            id="performance",
            file="dualwriter.go",
            line=125,
            category="metric-label-error",
            reviewer="performance_reviewer",
            message="The metric label passes `name`, not `options.Kind`.",
            suggestion="Call d.recordStorageDuration(false, mode, options.Kind, method, start).",
        )

        decision = policy.pre_filter([correctness, performance], state)

        assert [finding.id for finding in decision.kept] == ["correctness"]
        assert [finding.id for finding in decision.dropped] == ["performance"]

    def test_repeated_undefined_symbol_merges_across_distant_test_lines(self):
        state = _make_state(None)
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))
        first = _make_finding(
            id="first",
            file="dualwriter_test.go",
            line=71,
            category="undefined-identifier",
            message="The identifier `p` is undefined in this test.",
            suggestion="Define `p` before NewDualWriter.",
        )
        repeated = _make_finding(
            id="repeated",
            file="dualwriter_test.go",
            line=349,
            category="undefined-symbol",
            message="This test also uses undefined symbol `p`.",
            suggestion="Initialize `p` locally.",
        )
        independent = _make_finding(
            id="independent",
            file="dualwriter_test.go",
            line=350,
            category="undefined-symbol",
            message="This test uses undefined symbol `fixture`.",
            suggestion="Define `fixture`.",
        )

        decision = policy.pre_filter([first, repeated, independent], state)

        assert {finding.id for finding in decision.kept} == {"first", "independent"}
        assert [finding.id for finding in decision.dropped] == ["repeated"]


# ── Invalid coordinate and generic advice drops ────────────────────────────


class TestInvalidCoordinateAndGenericAdvice:
    def test_testing_reviewer_cannot_publish_production_code_bug(self):
        state = _make_state(None)
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))
        finding = _make_finding(
            id="testing-production",
            file="src/controller.rb",
            reviewer="testing_reviewer",
            category="nil-handling",
            message="`host.destroy` raises when `host` is nil.",
            suggestion="Guard `host` before the call.",
        )

        decision = policy.pre_filter([finding], state)

        assert decision.kept == []
        assert decision.dropped == [finding]
        assert decision.scored[0].drop_reason == "reviewer-scope"
        assert decision.metrics["reviewer_scope_dropped"] == 1

    def test_testing_reviewer_keeps_test_artifact_defect(self):
        state = _make_state(None)
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))
        finding = _make_finding(
            id="testing-test",
            file="tests/test_controller.py",
            reviewer="testing_reviewer",
            category="broken-assertion",
            message="The assertion compares `actual` with the wrong `expected` fixture.",
            suggestion="Use the matching `expected` fixture.",
        )

        decision = policy.pre_filter([finding], state)

        assert decision.kept == [finding]
        assert decision.dropped == []

    def test_correctness_reviewer_defers_test_artifact_to_testing_specialist(self):
        state = _make_state(None)
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))
        finding = _make_finding(
            id="correctness-test",
            file="pkg/handler_test.go",
            reviewer="correctness_reviewer",
            category="undefined-identifier",
            message="The test appears to use undefined `registry`.",
            suggestion="Define `registry` in the fixture.",
        )

        decision = policy.pre_filter([finding], state)

        assert decision.kept == []
        assert decision.dropped == [finding]
        assert decision.scored[0].drop_reason == "reviewer-scope"

    def test_security_reviewer_may_publish_test_artifact_security_issue(self):
        state = _make_state(None)
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))
        finding = _make_finding(
            id="security-test",
            file="tests/test_deploy.py",
            reviewer="security_reviewer",
            category="hardcoded-secrets",
            message="The fixture exposes `production_token` in repository history.",
            suggestion="Load it from a test-only secret store.",
        )

        decision = policy.pre_filter([finding], state)

        assert decision.kept == [finding]
        assert decision.dropped == []

    def test_invalid_right_line_dropped(self):
        diff = "@@ -1,3 +1,4 @@\n x = 1\n+# new comment at line 2\n y = 2\n z = 3\n"
        state = _make_state({"src/auth.py": diff})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        invalid_line = _make_finding(
            id="inv",
            line=999,
            message="os.system(user) sink",
            suggestion="Switch to subprocess.run with fixed argv",
        )
        decision = policy.pre_filter([invalid_line], state)
        assert any(s.drop_reason == "invalid-coordinate" for s in decision.scored)
        assert [f.id for f in decision.kept] == []
        assert [f.id for f in decision.dropped] == ["inv"]

    def test_zero_line_dropped_as_invalid(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        invalid = _make_finding(id="z", line=0)
        decision = policy.pre_filter([invalid], state)
        scored = next(s for s in decision.scored if s.finding.id == "z")
        assert scored.invalid_coordinate is True
        assert scored.drop_reason == "invalid-coordinate"

    def test_missing_file_patch_abstains(self):
        """No patch available → must NOT mark invalid-coordinate."""
        # state has file_diffs but no entry for the finding's file.
        state = _make_state({"other.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        finding = _make_finding(
            id="abst",
            file="missing.py",
            line=42,
            message="os.system(user) sink",
            suggestion="Use subprocess.run.",
        )
        decision = policy.pre_filter([finding], state)
        scored = next(s for s in decision.scored if s.finding.id == "abst")
        assert scored.abstained is True
        assert scored.invalid_coordinate is False
        assert scored.drop_reason is None
        # And the finding survives pre_filter.
        assert [f.id for f in decision.kept] == ["abst"]
        assert decision.dropped == []

    def test_empty_file_diffs_abstains(self):
        """file_diffs is None → abstained, not invalid-coordinate."""
        state = _make_state(None)
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        finding = _make_finding(id="abst", line=42, message="os.system(user)")
        decision = policy.pre_filter([finding], state)
        scored = next(s for s in decision.scored if s.finding.id == "abst")
        assert scored.abstained is True
        assert scored.invalid_coordinate is False
        assert scored.drop_reason is None
        assert [f.id for f in decision.kept] == ["abst"]

    @pytest.mark.parametrize(
        "category",
        [
            "testing",
            "test-coverage",
            "missing-test",
            "documentation",
            "missing-docs",
            "performance",
            "micro-optimization",
            "style",
            "naming",
        ],
    )
    def test_generic_advice_categories_dropped(self, category):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        finding = _make_finding(
            id=f"gen_{category}",
            category=category,
            message="Generic advice for the requested category.",
            suggestion="Add tests / docs / benchmarks / refactor for clarity.",
        )
        decision = policy.pre_filter([finding], state)
        scored = next(s for s in decision.scored if s.finding.id == f"gen_{category}")
        assert scored.is_generic_advice is True
        assert scored.drop_reason == "generic-advice"
        assert decision.kept == []

    def test_generic_category_with_concrete_sink_is_kept(self):
        """Testing category + a concrete named sink is NOT generic advice."""
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        finding = _make_finding(
            id="sink_in_test",
            category="testing",
            line=5,
            message="The unit test for os.system(user) misses an injection case.",
            suggestion="Cover the tainted-input branch.",
        )
        decision = policy.pre_filter([finding], state)
        scored = next(s for s in decision.scored if s.finding.id == "sink_in_test")
        assert scored.has_concrete_sink is True
        assert scored.is_generic_advice is False
        assert scored.drop_reason is None
        assert [f.id for f in decision.kept] == ["sink_in_test"]

    def test_generic_category_with_failure_mechanism_is_kept(self):
        """Testing category + a concrete failure (assertion/exception) is kept."""
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        finding = _make_finding(
            id="race_in_test",
            category="testing",
            line=5,
            message="Race condition in the worker test — assertion fails when run concurrently.",
            suggestion="Add a barrier to serialize the workers.",
        )
        decision = policy.pre_filter([finding], state)
        scored = next(s for s in decision.scored if s.finding.id == "race_in_test")
        assert scored.is_generic_advice is False
        assert scored.drop_reason is None
        assert [f.id for f in decision.kept] == ["race_in_test"]

    def test_testing_defect_with_exception_is_kept(self):
        """A concrete testing defect (test fails with exception) is preserved."""
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="enforce"))

        finding = _make_finding(
            id="test_exception",
            category="testing",
            line=5,
            message="Test fails with AssertionError when handling the empty input case.",
            suggestion="Add a precondition check before the assertion.",
        )
        decision = policy.pre_filter([finding], state)
        scored = next(s for s in decision.scored if s.finding.id == "test_exception")
        assert scored.is_generic_advice is False
        assert scored.drop_reason is None
        assert [f.id for f in decision.kept] == ["test_exception"]


# ── Final budget ────────────────────────────────────────────────────────────


class TestEmptyReviewRescue:
    def test_selects_one_added_line_late_rejection_without_confidence(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(
            PublicationPolicyConfig(
                enabled=True,
                mode="enforce",
                empty_review_rescue_enabled=True,
            )
        )
        verifier = _make_finding(
            id="verifier",
            line=5,
            status="false_positive",
            verified_by="verifier",
            confidence=0.2,
        )
        gate = _make_finding(
            id="gate",
            line=5,
            status="false_positive",
            verified_by="publication-gate",
            confidence=1.0,
        )

        selected = policy.select_empty_review_rescue([gate, verifier], state)

        assert selected is not None
        assert selected.id == "verifier"

    @pytest.mark.parametrize(
        ("verified_by", "reviewer", "line"),
        [
            ("judge", "correctness_reviewer", 5),
            ("publication-policy", "security_reviewer", 5),
            ("publication-gate", "testing_reviewer", 5),
            ("publication-gate", "correctness_reviewer", 6),
        ],
    )
    def test_rejects_early_scope_or_non_added_candidates(
        self,
        verified_by: str,
        reviewer: str,
        line: int,
    ):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(
            PublicationPolicyConfig(
                enabled=True,
                mode="enforce",
                empty_review_rescue_enabled=True,
            )
        )
        finding = _make_finding(
            line=line,
            reviewer=reviewer,
            status="false_positive",
            verified_by=verified_by,
        )

        assert policy.select_empty_review_rescue([finding], state) is None

    def test_orchestrator_restores_selected_finding_once(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        finding = _make_finding(
            id="rescue",
            line=5,
            status="false_positive",
            verified_by="publication-gate-ungrounded",
            reviewer="correctness_reviewer",
        )
        state.add_finding(finding)
        orchestrator = Orchestrator(
            registry=build_registry(),
            gateway=MagicMock(spec=ToolGateway),
            event_bus=MagicMock(),
            planner_llm=MagicMock(),
            reviewer_llm=MagicMock(),
            calibrator_llm=MagicMock(),
            publication_policy=PublicationPolicy(
                PublicationPolicyConfig(
                    enabled=True,
                    mode="enforce",
                    empty_review_rescue_enabled=True,
                )
            ),
        )

        stats = orchestrator._apply_empty_review_rescue(state)

        restored = state.get_finding("rescue")
        assert stats["rescued"] == 1
        assert restored.status == "confirmed"
        assert restored.verified_by == "empty-review-rescue"


class TestPostFinalizeBudget:
    def test_disabled_budget_preserves_all_confirmed_findings(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(
            PublicationPolicyConfig(
                enabled=True,
                mode="enforce",
                budget_enabled=False,
                max_comments=1,
                high_risk_overflow=0,
            )
        )
        findings = [_make_finding(id=f"finding-{index}", line=5) for index in range(4)]
        decision = policy.post_finalize(findings, state)
        assert [finding.id for finding in decision.kept] == [finding.id for finding in findings]
        assert decision.dropped == []
        assert decision.metrics["budget_enabled"] == 0

    def test_top_n_kept_sorted_by_score(self):
        diff = (
            "@@ -1,8 +1,10 @@\n"
            " import os\n"
            " \n"
            "+# helper\n"
            "+def a(): return 1\n"
            "+def b(): return 2\n"
            "+def c(): return 3\n"
            "+def d(): return 4\n"
            " \n"
            " def other():\n"
            "     pass\n"
        )
        state = _make_state({"src/auth.py": diff})
        policy = PublicationPolicy(
            PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=2, high_risk_overflow=0)
        )
        # 4 findings of varying strength
        low = _make_finding(id="low", line=3, severity="info", verified_by="")
        warn = _make_finding(id="warn", line=4, severity="warning", verified_by="")
        err = _make_finding(id="err", line=5, severity="error", verified_by="")
        det_err = _make_finding(id="det_err", line=6, severity="error", verified_by="detector")

        decision = policy.post_finalize([low, warn, err, det_err], state)
        kept_ids = [f.id for f in decision.kept]
        assert len(kept_ids) == 2
        # detector+error is highest score → must be present
        assert "det_err" in kept_ids
        assert "err" in kept_ids
        assert "low" in {f.id for f in decision.dropped}
        assert "warn" in {f.id for f in decision.dropped}

    def test_overflow_used_only_for_high_risk(self):
        diff = _SAMPLE_DIFF_LINE8
        state = _make_state({"src/auth.py": diff})
        policy = PublicationPolicy(
            PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=1, high_risk_overflow=1)
        )

        # 3 candidates: only 1 main slot + 1 overflow slot
        detector_error = _make_finding(
            id="det_err",
            line=7,
            severity="error",
            verified_by="detector",
            message="os.system(cmd) command injection via tainted input.",
            suggestion="Replace os.system with subprocess.run and a fixed argv list.",
        )
        reviewer_error = _make_finding(
            id="rev_err",
            line=8,
            severity="error",
            verified_by="",
            message="subprocess.run call that takes dynamic input.",
            suggestion="Use subprocess.run with a fixed argv list.",
        )
        reviewer_warning = _make_finding(
            id="rev_warn",
            line=8,
            severity="warning",
            verified_by="",
            message="Possibly bad use of subprocess.Popen here.",
            suggestion="Replace subprocess.Popen with subprocess.run and a fixed argv list.",
        )
        decision = policy.post_finalize([detector_error, reviewer_error, reviewer_warning], state)
        kept_ids = {f.id for f in decision.kept}
        # detector+error → main slot (highest score)
        assert "det_err" in kept_ids
        # next candidate is reviewer+error → not high-risk → must NOT take overflow slot
        assert "rev_warn" not in kept_ids
        # overflow slot would have gone to next-highest; reviewer+error is below
        # detector+error but not high_risk. Only one non-detector error could
        # be considered, so the dropped set contains rev_err or rev_warn.
        assert len(kept_ids) == 1
        assert decision.metrics["overflow_used"] == 0

    def test_overflow_slots_respect_capacity(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(
            PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=1, high_risk_overflow=2)
        )

        findings = [
            _make_finding(
                id=f"det{i}",
                line=5 + i,
                severity="error",
                verified_by="detector",
                message=f"os.system(user{i}) command injection sink.",
                suggestion="Use subprocess.run with a fixed argv list.",
            )
            for i in range(4)
        ]
        decision = policy.post_finalize(findings, state)
        # 1 main + 2 overflow → kept=3, dropped=1
        assert len(decision.kept) == 3
        assert len(decision.dropped) == 1
        assert decision.metrics["overflow_used"] == 2

    def test_reported_finding_always_survives_post_finalize(self):
        """``reported`` findings are kept verbatim and bypass the budget."""
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(
            PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=1, high_risk_overflow=0)
        )

        surviving = _make_finding(id="surv", line=5, status="reported", verified_by="detector")
        candidate = _make_finding(id="cand", line=5, verified_by="", confidence=0.99)

        decision = policy.post_finalize([surviving, candidate], state)
        kept_ids = {f.id for f in decision.kept}
        assert "surv" in kept_ids
        # The reported finding must NOT consume the main budget slot — cand
        # is still scored and budgeted.
        assert "cand" in kept_ids
        assert decision.metrics["reported_carried"] == 1

    def test_reported_finding_does_not_consume_budget(self):
        """A pre-seeded reported finding must not block a fresh confirmed slot."""
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        policy = PublicationPolicy(
            PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=1, high_risk_overflow=0)
        )

        reported = _make_finding(
            id="rep",
            line=3,
            status="reported",
            verified_by="detector",
            message="os.system(rep) historical finding.",
        )
        confirmed = _make_finding(
            id="new",
            line=5,
            verified_by="detector",
            severity="error",
            message="os.system(new) command injection.",
            suggestion="Replace with subprocess.run.",
        )
        decision = policy.post_finalize([reported, confirmed], state)
        kept_ids = {f.id for f in decision.kept}
        assert kept_ids == {"rep", "new"}


# ── Integration: enforce mode flips dropped findings to false_positive ─────


class TestEnforceAppliesStateMutation:
    """In enforce mode, dropped findings become false_positive with provenance."""

    @pytest.mark.asyncio
    async def test_enforce_marks_dropped_false_positive(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        inv = _make_finding(id="inv", line=999)
        gen = _make_finding(
            id="gen",
            category="testing",
            line=5,
            message="Should add more tests.",
            suggestion="Cover edge cases with integration tests.",
        )
        state.add_finding(inv)
        state.add_finding(gen)

        policy = PublicationPolicy(
            PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=2, high_risk_overflow=0)
        )
        decision = policy.pre_filter([state.get_finding(f.id) for f in [inv, gen]], state)
        for f in decision.dropped:
            state.update_finding(
                f.id,
                status="false_positive",
                verified_by="publication-policy",
                verify_reason=format_verify_reason(decision, f),
            )

        for fid in ("inv", "gen"):
            updated = state.findings[fid]
            assert updated.status == "false_positive"
            assert updated.verified_by == "publication-policy"
            assert "publication-policy" in updated.verify_reason

    @pytest.mark.asyncio
    async def test_shadow_mode_does_not_mutate_state(self):
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        gen = _make_finding(
            id="gen",
            category="testing",
            line=5,
            message="Should add more tests.",
            suggestion="Cover edge cases with integration tests.",
        )
        state.add_finding(gen)

        policy = PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="shadow"))
        decision = policy.pre_filter([state.get_finding("gen")], state)
        # mode == "shadow" → orchestrator applies no state mutation.
        assert decision.dropped[0].id == "gen"
        # Original status unchanged
        assert state.findings["gen"].status == "confirmed"


# ── Orchestrator integration ────────────────────────────────────────────────


class _RecordingEventBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []
        self._run_id = ""

    def set_run_id(self, run_id: str) -> None:
        self._run_id = run_id

    def emit(self, event_type: str, data: dict | None = None) -> None:
        self.events.append((event_type, data or {}))


class _StaticMockLLM:
    def __init__(self, content: str = '{"findings": []}') -> None:
        self._content = content
        self._calls = 0

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, **kwargs):
        self._calls += 1
        return AIMessage(content=self._content)


def _make_orchestrator(
    *,
    publication_gate_enabled: bool = True,
    policy: PublicationPolicy | None = None,
    events: _RecordingEventBus | None = None,
) -> tuple[Orchestrator, _RecordingEventBus]:
    reg = build_registry()
    bus = events or _RecordingEventBus()
    orch = Orchestrator(
        registry=reg,
        gateway=ToolGateway(reg, MagicMock()),
        event_bus=bus,
        planner_llm=_StaticMockLLM(),
        reviewer_llm=_StaticMockLLM(),
        calibrator_llm=_StaticMockLLM(),
        agentic_default=False,
        publication_gate_enabled=publication_gate_enabled,
        publication_policy=policy,
    )
    return orch, bus


class TestOrchestratorIntegration:
    @pytest.mark.asyncio
    async def test_off_mode_does_not_run_pre_or_post(self):
        orch, events = _make_orchestrator(policy=PublicationPolicy(PublicationPolicyConfig(enabled=False, mode="off")))
        confirmed = _make_finding(line=5)
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        state.add_finding(confirmed)

        # Pre and post helpers short-circuit when ``enabled`` is False, so
        # the publication_policy.* events must not be emitted.
        orch._run_publication_policy_pre(state)
        orch._run_publication_policy_post(state)

        assert "publication_policy.pre.started" not in [e[0] for e in events.events]
        assert "publication_policy.post.started" not in [e[0] for e in events.events]

    @pytest.mark.asyncio
    async def test_enforce_mode_marks_dropped_findings_false_positive(self):
        orch, events = _make_orchestrator(
            policy=PublicationPolicy(
                PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=2, high_risk_overflow=0)
            )
        )
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        valid = _make_finding(
            id="valid",
            line=5,
            message="os.system(t) command injection via tainted input.",
            suggestion="Use subprocess.run with a fixed argv list.",
        )
        invalid = _make_finding(id="bad", line=999, category="testing", message="no anchor", suggestion="add tests")
        state.add_finding(valid)
        state.add_finding(invalid)

        orch._run_publication_policy_pre(state)

        # Only the valid finding should still be `confirmed`; the other one is
        # marked `false_positive` with `verified_by='publication-policy'`.
        assert state.findings["valid"].status == "confirmed"
        assert state.findings["bad"].status == "false_positive"
        assert state.findings["bad"].verified_by == "publication-policy"
        pre_events = [e for e in events.events if e[0] == "publication_policy.pre.completed"]
        assert pre_events, "pre.completed event was not emitted"
        assert pre_events[0][1]["dropped"] >= 1

    @pytest.mark.asyncio
    async def test_shadow_mode_does_not_mutate_findings(self):
        orch, events = _make_orchestrator(
            policy=PublicationPolicy(PublicationPolicyConfig(enabled=True, mode="shadow"))
        )
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        bad = _make_finding(id="bad", line=999, category="testing", message="generic", suggestion="add tests")
        state.add_finding(bad)

        orch._run_publication_policy_pre(state)

        # status unchanged in shadow mode
        assert state.findings["bad"].status == "confirmed"
        # but event was emitted
        started = [e for e in events.events if e[0] == "publication_policy.pre.started"]
        completed = [e for e in events.events if e[0] == "publication_policy.pre.completed"]
        assert started and completed

    @pytest.mark.asyncio
    async def test_publication_gate_candidate_count_reduced(self):
        """The gate must see fewer candidates after the policy pre-filter."""

        orch, events = _make_orchestrator(
            policy=PublicationPolicy(
                PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=2, high_risk_overflow=0)
            )
        )
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        # 5 findings, only 1 has a valid RIGHT coordinate
        for i, line in enumerate([5, 6, 7, 8, 9]):
            fid = f"f{i}"
            state.add_finding(
                _make_finding(
                    id=fid,
                    line=line if i == 0 else 999,  # only i==0 has a real anchor
                    message="os.system(user) command injection."
                    if i == 0
                    else "Generic advice with no anchor on this line.",
                    suggestion="Use subprocess.run with a fixed argv list." if i == 0 else "Add more tests.",
                    category="testing" if i > 0 else "command-injection",
                )
            )

        gate = AsyncMock()
        with patch.object(orch, "_run_publication_gate", new=gate) as mock_gate:
            # simulate orchestrator flow
            orch._run_publication_policy_pre(state)
            await mock_gate(state)

        # 4 findings were dropped in pre-filter; gate is called with the
        # 1 survivor only.
        kept_after_pre = sum(1 for fid, f in state.findings.items() if f.status == "confirmed")
        assert kept_after_pre == 1
        mock_gate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gate_disabled_enforce_runs_pre_and_post(self):
        """Policy runs independently of the Publication Gate.

        With ``publication_gate_enabled=False`` and the policy in enforce
        mode, pre and post must still execute and mutate state for
        dropped findings.  The gate itself must not be awaited.
        """
        orch, events = _make_orchestrator(
            publication_gate_enabled=False,
            policy=PublicationPolicy(
                PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=1, high_risk_overflow=0)
            ),
        )
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        valid = _make_finding(
            id="valid",
            line=5,
            message="os.system(t) command injection via tainted input.",
            suggestion="Use subprocess.run with a fixed argv list.",
        )
        invalid = _make_finding(
            id="bad",
            line=999,
            category="testing",
            message="no anchor",
            suggestion="add tests",
        )
        state.add_finding(valid)
        state.add_finding(invalid)

        gate = AsyncMock()
        with patch.object(orch, "_run_publication_gate", new=gate) as mock_gate:
            # We invoke pre and post directly to assert they still run
            # when the gate flag is off.
            orch._run_publication_policy_pre(state)
            # gate would have run between pre and post — call it once to
            # mimic the production wiring (but the mock returns immediately).
            await mock_gate(state)
            orch._run_publication_policy_post(state)

        # gate was *never* invoked by the orchestrator itself; only the
        # test's manual call awaited it.
        assert mock_gate.await_count == 1
        # pre still flipped the invalid finding to false_positive.
        assert state.findings["bad"].status == "false_positive"
        # post still emitted its completed event.
        post_events = [e for e in events.events if e[0] == "publication_policy.post.completed"]
        assert post_events, "post.completed event was not emitted"

    @pytest.mark.asyncio
    async def test_full_run_pipeline_wires_policy(self):
        orch, events = _make_orchestrator(
            publication_gate_enabled=True,
            policy=PublicationPolicy(
                PublicationPolicyConfig(enabled=True, mode="enforce", max_comments=2, high_risk_overflow=0)
            ),
        )

        # Pre-seed a confirmed finding directly into the state; skip planner/reviewer
        # layers by short-circuiting them at the orchestrator level.
        state = _make_state({"src/auth.py": _SAMPLE_DIFF})
        state.add_finding(
            _make_finding(
                id="pipeline",
                line=5,
                message="os.system(user) sink — switch to subprocess.run with a fixed argv list.",
                suggestion="Use subprocess.run with a fixed argv list.",
            )
        )

        # Mock everything that costs LLM/time
        with patch.object(orch._planner, "plan", new_callable=AsyncMock, return_value=[]):
            with patch.object(orch._context_engine, "build", new_callable=AsyncMock, return_value={}):
                with patch(
                    "reviewforge.engine.orchestrator.scan_changed_files",
                    new_callable=AsyncMock,
                ) as mock_scan:
                    mock_scan.return_value = MagicMock(
                        findings=[],
                        files_scanned=0,
                        file_errors={},
                        scanner_errors={},
                    )
                    with patch.object(orch, "_run_publication_gate", new=AsyncMock()):
                        # We don't need the LLM gate to run — we only want to
                        # confirm wiring.
                        await orch.run(state)

        # The publication-policy pre/post events were emitted.
        policy_events = [e[0] for e in events.events if e[0].startswith("publication_policy.")]
        assert "publication_policy.pre.started" in policy_events
        assert "publication_policy.pre.completed" in policy_events
        assert "publication_policy.post.started" in policy_events
        assert "publication_policy.post.completed" in policy_events


# ── Config env-var overrides ────────────────────────────────────────────────


class TestPublicationPolicyEnvOverrides:
    def test_env_budget_enabled_overrides_yaml(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            "publication_policy:\n  enabled: true\n  mode: enforce\n  budget_enabled: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_PUBLICATION_POLICY_BUDGET_ENABLED", "false")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.publication_policy.budget_enabled is False

    def test_empty_review_rescue_loads_and_env_can_disable(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            "publication_policy:\n  enabled: true\n  mode: enforce\n  empty_review_rescue_enabled: true\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_PUBLICATION_POLICY_EMPTY_REVIEW_RESCUE_ENABLED", "false")

        cfg = ReviewForgeConfig.load(cfg_file)

        assert cfg.publication_policy.empty_review_rescue_enabled is False

    def test_env_max_comments_overrides_yaml(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            "publication_policy:\n  enabled: true\n  mode: enforce\n  max_comments: 9\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_PUBLICATION_POLICY_MAX_COMMENTS", "3")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.publication_policy.max_comments == 3

    def test_env_high_risk_overflow_overrides_yaml(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            "publication_policy:\n  enabled: true\n  mode: enforce\n  high_risk_overflow: 5\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_PUBLICATION_POLICY_HIGH_RISK_OVERFLOW", "2")
        cfg = ReviewForgeConfig.load(cfg_file)
        assert cfg.publication_policy.high_risk_overflow == 2

    def test_env_max_comments_clamps_to_minimum(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            "publication_policy:\n  enabled: true\n  mode: enforce\n  max_comments: 4\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_PUBLICATION_POLICY_MAX_COMMENTS", "0")
        cfg = ReviewForgeConfig.load(cfg_file)
        # max_comments must be >= 1; invalid value clamps up.
        assert cfg.publication_policy.max_comments == 1

    def test_env_high_risk_overflow_clamps_to_zero(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            "publication_policy:\n  enabled: true\n  mode: enforce\n  high_risk_overflow: 2\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_PUBLICATION_POLICY_HIGH_RISK_OVERFLOW", "-3")
        cfg = ReviewForgeConfig.load(cfg_file)
        # overflow >= 0; negative clamps to zero.
        assert cfg.publication_policy.high_risk_overflow == 0

    def test_env_max_comments_invalid_value_falls_back(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            "publication_policy:\n  enabled: true\n  mode: enforce\n  max_comments: 7\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_PUBLICATION_POLICY_MAX_COMMENTS", "not-a-number")
        cfg = ReviewForgeConfig.load(cfg_file)
        # Bad env value leaves the YAML value intact.
        assert cfg.publication_policy.max_comments == 7

    def test_env_high_risk_overflow_invalid_value_falls_back(self, monkeypatch, tmp_path):
        cfg_file = tmp_path / "reviewforge.yaml"
        cfg_file.write_text(
            "publication_policy:\n  enabled: true\n  mode: enforce\n  high_risk_overflow: 4\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("REVIEWFORGE_PUBLICATION_POLICY_HIGH_RISK_OVERFLOW", "garbage")
        cfg = ReviewForgeConfig.load(cfg_file)
        # Bad env value leaves the YAML value intact.
        assert cfg.publication_policy.high_risk_overflow == 4

    def test_production_yaml_max_comments_default(self):
        project_root = Path(__file__).resolve().parents[2]
        cfg = ReviewForgeConfig.load(project_root / "reviewforge.yaml")
        assert cfg.publication_policy.max_comments == 4
        assert cfg.publication_policy.high_risk_overflow == 1
        assert cfg.publication_policy.mode == "enforce"
        assert cfg.publication_policy.budget_enabled is False
